"""LLM-based test scenario generator for Breeze Buddy templates.

Strategy
--------
1. Enumerate every unique root-to-leaf path through the template's flow graph.
2. Split paths into small batches and call the LLM in **parallel** — one
   asyncio task per batch.  This eliminates the single-call token-limit
   problem and makes generation faster.
3. The tier controls how many scenario *variants* are generated per path and
   how focused / creative the LLM prompt is:

   BASIC    — 1 scenario per path  (fast, faithful baseline)
   ADVANCED — 2-3 scenarios per path (different user phrasings & styles)
   PRO      — 4-5 scenarios per path (diverse personas, edge-case wording,
                                      language mix, emotional tones)

Optimisation notes
------------------
- Basic: batches of 5 paths per call (low token budget, temperature 0.3)
- Advanced: 1 path per call, ask for 2-3 variants (temperature 0.6)
- Pro: 1 path per call, ask for 4-5 variants (temperature 0.8)
Running all calls concurrently with asyncio.gather keeps total wall-clock
time proportional to the slowest single call rather than the sum.
"""

import asyncio
import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from anthropic import AsyncAnthropicVertex

from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.ai.voice.agents.breeze_buddy.test.types import (
    FunctionFailureConfig,
    GeneratedScenario,
    GenerationTier,
    TurnInput,
)
from app.core.config.dynamic import (
    VERTEX_CLAUDE_MODEL,
    VERTEX_CREDENTIALS_JSON,
    VERTEX_PROJECT_ID,
    VERTEX_REGION,
)
from app.core.logger import logger

# ---------------------------------------------------------------------------
# Flow graph types
# ---------------------------------------------------------------------------

# A single hop in a path: (from_node, via_function, to_node)
_Hop = Tuple[str, str, str]
# A full path: ordered list of hops from initial_node to an end node
_Path = List[_Hop]

_EXAMPLE_HTTP_ERRORS = [
    {"status": "error", "message": "timeout"},
    {"status": "error", "message": "person_not_found"},
    {"status": 0, "error": "internal_server_error"},
]

# ---------------------------------------------------------------------------
# Tier configuration
# ---------------------------------------------------------------------------

_TIER_CONFIG: Dict[str, Dict[str, Any]] = {
    GenerationTier.BASIC: {
        "variants_min": 1,
        "variants_max": 1,
        "batch_size": 5,  # paths per LLM call
        "temperature": 0.3,
        "max_tokens": 3000,  # per call
    },
    GenerationTier.ADVANCED: {
        "variants_min": 2,
        "variants_max": 3,
        "batch_size": 1,  # one path per call — full focus
        "temperature": 0.6,
        "max_tokens": 5000,
    },
    GenerationTier.PRO: {
        "variants_min": 4,
        "variants_max": 5,
        "batch_size": 1,
        "temperature": 0.8,
        "max_tokens": 8000,
    },
}


# ---------------------------------------------------------------------------
# Flow graph analysis
# ---------------------------------------------------------------------------


def _build_graph(flow: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Build an adjacency map: node_name → list of outbound function edges."""
    graph: Dict[str, List[Dict[str, Any]]] = {}
    for node in flow.get("nodes", []):
        name = node.get("node_name", "")
        edges = []
        for f in node.get("functions", []):
            fn = f.get("function_name") or f.get("name")
            if not fn:
                continue
            required_args = list(f.get("required", [])) or list(
                f.get("properties", {}).keys()
            )
            outcome_values = [
                hook["expected_fields"]["outcome"]["value"]
                for hook in f.get("hooks", [])
                if (
                    isinstance(hook.get("expected_fields", {}).get("outcome"), dict)
                    and hook["expected_fields"]["outcome"].get("source") == "static"
                )
            ]
            edges.append(
                {
                    "function": fn,
                    "transition_to": f.get("transition_to"),
                    "description": f.get("description", "")[:200],
                    "required_args": required_args,
                    "outcome_values": outcome_values,
                }
            )
        graph[name] = edges
    return graph


def _get_end_nodes(flow: Dict[str, Any]) -> List[str]:
    """Return names of all terminal nodes (end_conversation action or no functions)."""
    return [
        node.get("node_name", "")
        for node in flow.get("nodes", [])
        if any(
            isinstance(a, dict) and a.get("handler") == "end_conversation"
            for a in node.get("post_actions", [])
        )
        or not node.get("functions")
    ]


def _enumerate_paths(
    graph: Dict[str, List[Dict[str, Any]]],
    initial_node: str,
    end_nodes: List[str],
) -> List[_Path]:
    """
    DFS from initial_node to enumerate every unique root-to-leaf path.

    Each path is a list of hops: (from_node, via_function, to_node).
    Cycles are broken by not revisiting a node already on the current path.
    Self-loops (transition_to == current) are captured as a single extra
    variant — the node appearing twice means "the bot retried once".
    """
    end_set = set(end_nodes)
    paths: List[_Path] = []

    def dfs(current: str, path: _Path, visited: set) -> None:
        if current in end_set:
            paths.append(list(path))
            return

        edges = graph.get(current, [])
        if not edges:
            # Dead-end non-terminal — record anyway (structural gap)
            paths.append(list(path))
            return

        for edge in edges:
            target = edge["transition_to"]
            fn = edge["function"]

            if target == current:
                # Self-loop: generate one variant that traverses the loop once
                loop_hop: _Hop = (current, fn, current)
                if current not in visited:
                    dfs(current, path + [loop_hop], visited | {current})
                continue

            if not target or target in visited:
                continue

            hop: _Hop = (current, fn, target)
            dfs(target, path + [hop], visited | {current})

    dfs(initial_node, [], {initial_node})
    return paths


def _path_label(path: _Path) -> str:
    """Human-readable one-line description of a path (function names only)."""
    if not path:
        return "direct end"
    return " → ".join(f"{fn}()" for _, fn, _ in path)


def _path_end_node(path: _Path, initial_node: str) -> str:
    """Return the final node of a path."""
    return path[-1][2] if path else initial_node


# ---------------------------------------------------------------------------
# Flow summary (shared context passed to every LLM call)
# ---------------------------------------------------------------------------


def _extract_flow_summary(template: TemplateModel) -> Dict[str, Any]:
    """Build the structured flow context included in every LLM prompt."""
    flow = template.flow
    nodes_raw = flow.get("nodes", [])

    nodes_info = []
    for node in nodes_raw:
        node_name = node.get("node_name", "unknown")
        task_content = next(
            (
                m.get("content", "")[:400]
                for m in node.get("task_messages", [])
                if isinstance(m, dict) and m.get("role") == "system"
            ),
            "",
        )
        funcs = [
            {
                "name": f.get("function_name") or f.get("name"),
                "description": f.get("description", "")[:200],
                "transition_to": f.get("transition_to"),
                "required_args": list(f.get("required", []))
                or list(f.get("properties", {}).keys()),
            }
            for f in node.get("functions", [])
            if f.get("function_name") or f.get("name")
        ]
        nodes_info.append(
            {"node_name": node_name, "task_summary": task_content, "functions": funcs}
        )

    global_functions = [
        {
            "name": gf.get("name"),
            "description": gf.get("description", "")[:200],
            "type": gf.get("type", "http"),
            **(
                {"example_error_responses": _EXAMPLE_HTTP_ERRORS}
                if gf.get("type", "http") == "http"
                else {}
            ),
        }
        for gf in flow.get("global_functions", [])
    ]

    return {
        "template_name": template.name,
        "initial_node": flow.get("initial_node", ""),
        "end_nodes": _get_end_nodes(flow),
        "payload_keys": list((template.expected_payload_schema or {}).keys()),
        "nodes": nodes_info,
        "global_functions": global_functions,
    }


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

_SCENARIO_FIELD_RULES = """\
## Field rules for every scenario object
- `id`: unique snake_case string derived from the path + variant index
- `name`: one human-readable sentence — who the user is and what they do
- `scenario_type`: a short snake_case label you invent that describes what
  is distinctive about this scenario (e.g. `positive_feedback`,
  `whatsapp_clarification`, `low_rating_retry`, `language_switch`,
  `initial_hesitation`). Do NOT use a fixed list — choose a label that
  accurately reflects the user behaviour and template domain.
- `description`: one sentence summarising what makes this variant distinct
- `payload_example`: populate with realistic values (vary across variants)
- `turns`: every user turn from the bot's opening to the end node:
  - `user_message`: what the user says (empty string for silence/abandonment)
  - `expect_function_call`: exact function name or null
  - `expect_no_function_call`: true for clarification / interruption turns
  - `expect_node`: the node the bot lands on AFTER processing this turn
      (i.e. the `to_node` of the hop triggered by this turn's function call),
      or null if this turn does not trigger a node transition.
      Example: for a turn that calls submit_rating() which transitions
      initial --[submit_rating()]--> ask_feedback_node, set
      expect_node = "ask_feedback_node", NOT "initial".
  - `simulate_function_failures`: [] unless this is an api_failure scenario
- `expected_outcome`: outcome hook value from the last function, or null
- `expected_final_node`: MUST match the path's end node exactly

## Hard constraints
1. Output a JSON array ONLY — no markdown fences, no extra text.
2. Follow the path EXACTLY — do not skip hops or invent new transitions.
3. Only use function names that appear in the template structure above.
4. If a function has required_args, add a data-collection turn before it:
     data turn → expect_no_function_call: true
     next turn  → expect_function_call: <function>
5. A self-loop hop (same node name appears twice) = one extra retry turn on
   that node before the exit — model it as a clarification attempt.

## Critical: user messages must NOT look ahead
Think of this as a real phone call. The user only hears what the bot just said;
they have no knowledge of future turns.
- Turn 0 user_message: the user ONLY responds to the bot's opening greeting.
  They MUST NOT reference topics, questions, or information the bot has not
  yet raised (e.g. WhatsApp links, ratings, platforms — if those come later).
- Every subsequent user_message: a natural reaction to the bot's immediately
  preceding reply — nothing more.
- NEVER write a user message that references a topic before the bot introduces it.
- NEVER combine a response to the current question with content belonging to
  a future bot question.
"""


def _build_basic_batch_prompt(
    flow_summary: Dict[str, Any],
    payload_example: Dict[str, Any],
    paths: List[_Path],
    path_offset: int,
) -> str:
    """Prompt for Basic tier: one scenario per path, batch of up to 5 paths."""
    initial_node: str = flow_summary["initial_node"]
    end_nodes: List[str] = flow_summary["end_nodes"]

    path_specs = []
    for i, path in enumerate(paths, path_offset + 1):
        end = _path_end_node(path, initial_node)
        hops = (
            "\n".join(
                f"  {j}. {frm} --[{fn}()]--> {to}"
                for j, (frm, fn, to) in enumerate(path, 1)
            )
            or "  (starts on an end node)"
        )
        path_specs.append(f"Path {i} (ends at: {end}):\n{hops}")

    paths_block = "\n\n".join(path_specs)

    return f"""You are a QA engineer writing automated test scenarios for a voice bot.

## Template Structure
```json
{json.dumps(flow_summary, indent=2)}
```

## Sample Payload
```json
{json.dumps(payload_example, indent=2)}
```

## Your Task
Write exactly ONE realistic test scenario for EACH path below.
Use straightforward, cooperative user language — the user follows
the bot's instructions and completes the conversation normally.

## Paths to cover ({len(paths)} paths)
{paths_block}

{_SCENARIO_FIELD_RULES}
Each end node must be one of: {end_nodes}

Return ONLY the JSON array starting with `[` and ending with `]`."""


def _build_variant_prompt(
    flow_summary: Dict[str, Any],
    payload_example: Dict[str, Any],
    path: _Path,
    path_index: int,
    variants_min: int,
    variants_max: int,
    tier: GenerationTier,
) -> str:
    """Prompt for Advanced/Pro tier: multiple variants for a single path."""
    initial_node: str = flow_summary["initial_node"]
    end_nodes: List[str] = flow_summary["end_nodes"]
    end = _path_end_node(path, initial_node)

    hops = (
        "\n".join(
            f"  {j}. {frm} --[{fn}()]--> {to}"
            for j, (frm, fn, to) in enumerate(path, 1)
        )
        or "  (starts on an end node)"
    )

    if tier == GenerationTier.ADVANCED:
        variation_guidance = """\
## Variation guidance (Advanced tier — 2-3 variants)
Variant 1: Cooperative user — clear, direct answers, no hesitation.
Variant 2: Hesitant user — asks a clarifying question mid-conversation
           before eventually completing the same path.
Variant 3 (optional): Brief user — very short, terse replies; maybe
           initially vague but the bot guides them to the end node."""
    else:  # PRO
        variation_guidance = """\
## Variation guidance (Pro tier — 4-5 variants)
Variant 1: Cooperative user — clear, direct, polite.
Variant 2: Hesitant / unsure user — needs a bit of reassurance before
           proceeding; asks one clarifying question.
Variant 3: Impatient / terse user — short replies, slightly annoyed tone,
           but still completes the path.
Variant 4: Verbose / chatty user — gives extra context, small talk, then
           follows the path naturally.
Variant 5 (optional): Non-native speaker — simple vocabulary, minor
           grammatical errors, but intent is clear and the path completes.

Each variant must use DIFFERENT phrasing, tone, and payload values.
Make the user messages feel like real voice transcripts — conversational,
not written English. Include filler words, restarts, or short affirmations
("yeah", "sure", "uh", "actually") where natural."""

    return f"""You are a senior QA engineer writing realistic end-to-end test scenarios
for a voice bot. Your goal is to produce scenarios that are indistinguishable
from real user conversations — varied, natural, and human.

## Template Structure
```json
{json.dumps(flow_summary, indent=2)}
```

## Sample Payload
```json
{json.dumps(payload_example, indent=2)}
```

## The single path you must cover (Path {path_index})
End node: {end}
{hops}

{variation_guidance}

## Your Task
Write {variants_min}–{variants_max} scenario variants, all following the
EXACT same path above but with different user personalities and phrasing.
Suffix each `id` with `_v1`, `_v2`, etc. to distinguish variants.

{_SCENARIO_FIELD_RULES}
The `expected_final_node` MUST be: {end}
All end nodes for this template: {end_nodes}

Return ONLY the JSON array starting with `[` and ending with `]`."""


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_llm_response(raw: str) -> List[Dict[str, Any]]:
    cleaned = re.sub(r"```(?:json)?", "", raw).strip()
    match = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if not match:
        raise ValueError("LLM response did not contain a JSON array")
    return json.loads(match.group(0))


def _parse_scenario(d: Dict[str, Any]) -> Optional[GeneratedScenario]:
    try:
        turns = [
            TurnInput(
                user_message=str(t.get("user_message", "")),
                expect_function_call=t.get("expect_function_call") or None,
                expect_no_function_call=bool(t.get("expect_no_function_call", False)),
                expect_node=t.get("expect_node") or None,
                simulate_function_failures=[
                    FunctionFailureConfig(
                        function_name=sf["function_name"],
                        error_response=sf.get(
                            "error_response",
                            {"status": "error", "message": "Simulated failure"},
                        ),
                    )
                    for sf in t.get("simulate_function_failures", [])
                    if isinstance(sf, dict) and sf.get("function_name")
                ],
            )
            for t in d.get("turns", [])
        ]

        scenario_type = (
            str(d.get("scenario_type", "other") or "other").strip() or "other"
        )

        scenario_id = d.get("id") or f"scenario_{uuid.uuid4().hex[:8]}"
        return GeneratedScenario(
            id=scenario_id,
            name=d.get("name", scenario_id),
            scenario_type=scenario_type,
            description=d.get("description") or None,
            payload_example=d.get("payload_example") or {},
            turns=turns,
            expected_outcome=d.get("expected_outcome") or None,
            expected_final_node=d.get("expected_final_node") or None,
        )
    except Exception as exc:
        logger.warning("Skipping malformed scenario: {} — raw={}", exc, repr(d))
        return None


# ---------------------------------------------------------------------------
# LLM call helper
# ---------------------------------------------------------------------------


async def _call_llm(
    client: AsyncAnthropicVertex,
    prompt: str,
    temperature: float,
    max_tokens: int,
    label: str,
    model: str,
) -> List[GeneratedScenario]:
    """Single Claude-on-Vertex call → parsed list of GeneratedScenario objects."""
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=(
            "You are an expert QA engineer for voice-bot products. "
            "Output ONLY valid JSON arrays with no surrounding text or markdown."
        ),
        messages=[{"role": "user", "content": prompt}],
    )
    raw = next(
        (block.text for block in response.content if hasattr(block, "text")),
        "",
    )
    logger.debug("LLM call [{}]: {} chars in response", label, len(raw))
    try:
        dicts = _parse_llm_response(raw)
    except Exception as exc:
        logger.warning(
            "LLM call [{}] failed to parse: {} — snippet: {}", label, exc, raw[:300]
        )
        return []
    return [s for s in (_parse_scenario(d) for d in dicts) if s]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def generate_scenarios(
    template: TemplateModel,
    payload_example: Optional[Dict[str, Any]] = None,
    tier: GenerationTier = GenerationTier.BASIC,
) -> List[GeneratedScenario]:
    """
    Generate test scenarios for every unique path through the template's flow graph.

    The tier controls depth:
      BASIC    — 1 scenario per path (one parallel batch per ~5 paths)
      ADVANCED — 2-3 variants per path (one parallel call per path)
      PRO      — 4-5 variants per path (one parallel call per path)

    All LLM calls run concurrently with asyncio.gather, so total wall-clock
    time equals the slowest single call regardless of how many paths exist.
    """
    if payload_example is None:
        payload_example = {
            k: f"<{k}>" for k in (template.expected_payload_schema or {})
        }

    flow = template.flow
    initial_node = flow.get("initial_node", "")
    graph = _build_graph(flow)
    end_nodes = _get_end_nodes(flow)
    paths = _enumerate_paths(graph, initial_node, end_nodes)

    cfg = _TIER_CONFIG[tier]
    batch_size: int = cfg["batch_size"]
    temperature: float = cfg["temperature"]
    max_tokens: int = cfg["max_tokens"]
    variants_min: int = cfg["variants_min"]
    variants_max: int = cfg["variants_max"]

    logger.info(
        "Template {} ({}): {} paths, tier={}, batch_size={} → launching parallel LLM calls",
        template.id,
        template.name,
        len(paths),
        tier.value,
        batch_size,
    )
    for i, p in enumerate(paths, 1):
        logger.debug("  Path {}: {}", i, _path_label(p))

    flow_summary = _extract_flow_summary(template)

    # Build Vertex client — resolves credentials from dynamic config (Redis),
    # falling back to VERTEX_CREDENTIALS_JSON / GOOGLE_CREDENTIALS_JSON env vars.
    # project_id is extracted from the credentials JSON unless VERTEX_PROJECT_ID
    # is set explicitly.
    _project_id = await VERTEX_PROJECT_ID()
    _credentials_json = await VERTEX_CREDENTIALS_JSON()
    _region = await VERTEX_REGION()
    _model = await VERTEX_CLAUDE_MODEL()
    _credentials = None
    if _credentials_json:
        try:
            import json as _json

            from google.oauth2 import service_account  # type: ignore[import-untyped]

            _creds_dict = _json.loads(_credentials_json)
            if not _project_id:
                _project_id = _creds_dict.get("project_id", "")
            _credentials = service_account.Credentials.from_service_account_info(
                _creds_dict,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        except Exception as _exc:
            logger.warning(
                "Could not build Vertex credentials from VERTEX_CREDENTIALS_JSON: {}",
                _exc,
            )

    if not _project_id:
        raise ValueError(
            "Vertex project_id could not be resolved. "
            "Set VERTEX_PROJECT_ID or ensure VERTEX_CREDENTIALS_JSON contains project_id."
        )

    async with AsyncAnthropicVertex(
        project_id=_project_id,
        region=_region,
        credentials=_credentials,
    ) as client:
        if tier == GenerationTier.BASIC:
            # Group paths into batches; one call per batch
            tasks = []
            for offset in range(0, len(paths), batch_size):
                batch = paths[offset : offset + batch_size]
                prompt = _build_basic_batch_prompt(
                    flow_summary, payload_example, batch, offset
                )
                label = f"basic-batch-{offset // batch_size + 1}"
                tasks.append(
                    _call_llm(client, prompt, temperature, max_tokens, label, _model)
                )
        else:
            # Advanced / Pro: one call per path for maximum focus and quality
            tasks = []
            for i, path in enumerate(paths, 1):
                prompt = _build_variant_prompt(
                    flow_summary,
                    payload_example,
                    path,
                    i,
                    variants_min,
                    variants_max,
                    tier,
                )
                tasks.append(
                    _call_llm(
                        client, prompt, temperature, max_tokens, f"path-{i}", _model
                    )
                )

        results_per_task: List[List[GeneratedScenario]] = await asyncio.gather(*tasks)

    scenarios: List[GeneratedScenario] = [
        s for batch_result in results_per_task for s in batch_result
    ]

    if not scenarios:
        raise ValueError(
            f"No valid scenarios generated for template {template.id} "
            f"(tier={tier.value}, {len(paths)} paths, {len(tasks)} LLM calls)"
        )

    logger.info(
        "Generated {} scenarios for template {} (tier={}, {} paths, {} LLM calls)",
        len(scenarios),
        template.id,
        tier.value,
        len(paths),
        len(tasks),
    )
    return scenarios
