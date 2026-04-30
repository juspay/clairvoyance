"""
Breeze Buddy template test runner.

Two modes:
  structural — no LLM; validates node/function existence and transitions (~1 ms/scenario).
  llm        — sends real messages to Azure OpenAI and asserts on function calls/nodes (~2–10 s/scenario).
"""

import json
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.ai.voice.agents.breeze_buddy.test.types import (
    GeneratedScenario,
    ScenarioRunResult,
    TurnResult,
)
from app.core.config.static import (
    AZURE_BREEZE_BUDDY_OPENAI_MODEL,
    AZURE_OPENAI_API_KEY,
    AZURE_OPENAI_API_VERSION,
    AZURE_OPENAI_ENDPOINT,
)
from app.core.logger import logger

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _get_node(template: TemplateModel, name: str) -> Optional[Dict[str, Any]]:
    """Return the raw node dict for *name*, or None."""
    for node in template.flow.get("nodes", []):
        if node.get("node_name") == name:
            return node
    return None


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


def _fn_name(f: Dict[str, Any]) -> str:
    """Resolve the function name from a flow function dict."""
    return f.get("function_name") or f.get("name") or ""


def _func_map(node: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Build a name→def map for all functions in *node*."""
    return {_fn_name(f): f for f in node.get("functions", []) if _fn_name(f)}


def _to_openai_tool(f: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a flow function dict to an OpenAI tools-array entry."""
    return {
        "type": "function",
        "function": {
            "name": _fn_name(f),
            "description": f.get("description", ""),
            "parameters": {
                "type": "object",
                "properties": f.get("properties", {}),
                "required": f.get("required", []),
            },
        },
    }


def _normalise_messages(raw: List[Any]) -> List[Dict[str, Any]]:
    """Normalise task messages to plain dicts (handles Pydantic objects)."""
    result = []
    for m in raw:
        if hasattr(m, "model_dump"):
            result.append(m.model_dump())
        elif isinstance(m, dict):
            result.append(m)
    return result


def _render_messages(
    messages: List[Dict[str, Any]], variables: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Substitute {key} placeholders in message content."""
    out = []
    for msg in messages:
        if "content" in msg:
            content = str(msg["content"])
            for k, v in variables.items():
                content = content.replace(f"{{{k}}}", str(v))
            out.append({**msg, "content": content})
        else:
            out.append(msg)
    return out


def _record_failure(
    simulated_failures: List[Dict[str, Any]],
    turn_index: int,
    fn: str,
    error: Dict[str, Any],
) -> None:
    simulated_failures.append(
        {"turn_index": turn_index, "function": fn, "error_injected": error}
    )


def _make_scenario_result(
    scenario: GeneratedScenario,
    *,
    passed: bool,
    failure_reason: Optional[str],
    turns: List[TurnResult],
    nodes_visited: List[str],
    final_node: Optional[str] = None,
    actual_outcome: Optional[str] = None,
    total_ms: Optional[float] = None,
    simulated_failures: Optional[List[Dict[str, Any]]] = None,
) -> ScenarioRunResult:
    return ScenarioRunResult(
        scenario_id=scenario.id,
        scenario_name=scenario.name,
        scenario_type=scenario.scenario_type,
        passed=passed,
        failure_reason=failure_reason,
        turns=turns,
        nodes_visited=list(dict.fromkeys(nodes_visited)),
        final_node=final_node,
        actual_outcome=actual_outcome,
        total_latency_ms=total_ms,
        simulated_failures=simulated_failures or [],
    )


# ---------------------------------------------------------------------------
# Structural runner
# ---------------------------------------------------------------------------


def _run_structural(
    template: TemplateModel, scenario: GeneratedScenario
) -> ScenarioRunResult:
    """Validate node/function existence and transitions without calling the LLM."""
    start = time.time()
    flow = template.flow
    current = flow.get("initial_node", "")
    nodes_visited = [current]
    turn_results: List[TurnResult] = []
    passed = True
    failure_reason: Optional[str] = None
    simulated_failures: List[Dict[str, Any]] = []
    global_funcs = {gf.get("name"): gf for gf in flow.get("global_functions", [])}

    for idx, turn in enumerate(scenario.turns):
        t0 = time.time()
        node = _get_node(template, current)
        turn_fail: Optional[str] = None
        fn_status = "not_called"
        fn_error: Optional[Dict[str, Any]] = None
        failure_sim = {
            fc.function_name: fc.error_response
            for fc in turn.simulate_function_failures
        }

        if node is None:
            turn_fail = f"Node '{current}' not found in template flow"
            passed = False
        else:
            available = _func_map(node)

            # Implicit bot-driven node advance (expect_node differs from current)
            if turn.expect_node and turn.expect_node != current:
                declared = _get_node(template, turn.expect_node)
                if declared is None:
                    turn_fail = f"expect_node '{turn.expect_node}' does not exist"
                    passed = False
                else:
                    declared_fns = _func_map(declared)
                    fn = turn.expect_function_call
                    if fn and fn in available:
                        turn_fail = (
                            f"expect_node '{turn.expect_node}' declared but '{fn}' "
                            f"belongs to current node '{current}'"
                        )
                        passed = False
                    elif fn and fn not in declared_fns and fn not in global_funcs:
                        turn_fail = f"'{fn}' not found in expect_node '{turn.expect_node}' or global functions"
                        passed = False
                    else:
                        current = turn.expect_node
                        nodes_visited.append(current)
                        node = declared
                        available = declared_fns

            if turn_fail is None:
                fn = turn.expect_function_call
                if turn.expect_no_function_call and fn:
                    turn_fail = f"Contradictory: expect_no_function_call=True and expect_function_call='{fn}'"
                    passed = False
                elif fn and fn not in available and fn not in global_funcs:
                    turn_fail = (
                        f"'{fn}' not found in node '{current}' or global functions"
                    )
                    passed = False
                elif fn:
                    if fn in failure_sim:
                        fn_status = "simulated_failure"
                        fn_error = failure_sim[fn]
                        _record_failure(simulated_failures, idx, fn, fn_error)
                    else:
                        fn_status = "ok"
                        if fn in available:
                            next_node = available[fn].get("transition_to")
                            if next_node and next_node != current:
                                current = next_node
                                nodes_visited.append(current)

        turn_results.append(
            TurnResult(
                turn_index=idx,
                user_message=turn.user_message,
                bot_response="[structural — no LLM call]",
                function_called=turn.expect_function_call,
                function_execution_status=fn_status,
                function_error_injected=fn_error,
                node_name=current,
                passed=turn_fail is None,
                failure_reason=turn_fail,
                latency_ms=(time.time() - t0) * 1000,
            )
        )
        if turn_fail and failure_reason is None:
            failure_reason = turn_fail

    # End-node validation
    end_nodes = _get_end_nodes(flow)
    if passed and end_nodes and current not in end_nodes:
        passed = False
        failure_reason = (
            f"Scenario ended on '{current}' — expected one of {end_nodes}. "
            "Add turns to reach a terminal node."
        )

    return _make_scenario_result(
        scenario,
        passed=passed,
        failure_reason=failure_reason,
        turns=turn_results,
        nodes_visited=nodes_visited,
        final_node=current,
        total_ms=(time.time() - start) * 1000,
        simulated_failures=simulated_failures,
    )


# ---------------------------------------------------------------------------
# LLM runner
# ---------------------------------------------------------------------------


async def _call_llm(
    client: Any,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    model: str,
) -> Tuple[str, Optional[str], Optional[Dict[str, Any]]]:
    """Call OpenAI and return (text, function_name, function_args)."""
    kwargs: Dict[str, Any] = {"model": model, "messages": messages, "temperature": 0.0}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    resp = await client.chat.completions.create(**kwargs)
    msg = resp.choices[0].message
    text = msg.content or ""
    fn_name: Optional[str] = None
    fn_args: Optional[Dict[str, Any]] = None

    if msg.tool_calls:
        tc = msg.tool_calls[0]
        fn_name = tc.function.name
        try:
            fn_args = json.loads(tc.function.arguments)
        except Exception:
            fn_args = {}

    return text, fn_name, fn_args


async def _run_llm(
    template: TemplateModel,
    scenario: GeneratedScenario,
    model: str,
    client: Any,
) -> ScenarioRunResult:
    """Execute a scenario via Azure OpenAI."""
    start = time.time()
    flow = template.flow
    current = flow.get("initial_node", "")
    nodes_visited = [current]
    turn_results: List[TurnResult] = []
    passed = True
    failure_reason: Optional[str] = None
    actual_outcome: Optional[str] = None
    simulated_failures: List[Dict[str, Any]] = []

    # Build payload variable map
    payload_vars = {k: f"<{k}>" for k in (template.expected_payload_schema or {})}
    payload_vars.update(scenario.payload_example)

    # Initialise conversation messages from the starting node
    node = _get_node(template, current)
    if node is None:
        return _make_scenario_result(
            scenario,
            passed=False,
            failure_reason=f"Initial node '{current}' not found in template",
            turns=[],
            nodes_visited=[],
        )

    messages = _render_messages(
        _normalise_messages(node.get("task_messages", [])), payload_vars
    )
    global_tools = [_to_openai_tool(gf) for gf in flow.get("global_functions", [])]

    for idx, turn in enumerate(scenario.turns):
        t0 = time.time()
        turn_fail: Optional[str] = None
        failure_sim = {
            fc.function_name: fc.error_response
            for fc in turn.simulate_function_failures
        }

        # Build tools for current node
        node = _get_node(template, current)
        node_tools = []
        fmap: Dict[str, Dict[str, Any]] = {}
        if node:
            for f in node.get("functions", []):
                node_tools.append(_to_openai_tool(f))
                fmap[_fn_name(f)] = f

        messages.append({"role": "user", "content": turn.user_message})

        try:
            text, fn_called, fn_args = await _call_llm(
                client, messages, node_tools + global_tools, model
            )
        except Exception as exc:
            turn_fail = f"LLM call failed: {exc}"
            passed = False
            if failure_reason is None:
                failure_reason = turn_fail
            turn_results.append(
                TurnResult(
                    turn_index=idx,
                    user_message=turn.user_message,
                    bot_response="",
                    function_execution_status="not_called",
                    node_name=current,
                    passed=False,
                    failure_reason=turn_fail,
                    latency_ms=(time.time() - t0) * 1000,
                )
            )
            break

        latency_ms = (time.time() - t0) * 1000

        # Assertions
        if turn.expect_no_function_call and fn_called:
            turn_fail = (
                f"Expected no function call but got '{fn_called}({fn_args})' "
                f"[node: {current}, user: {turn.user_message!r}]"
            )
            passed = False
        elif turn.expect_function_call and fn_called != turn.expect_function_call:
            turn_fail = f"Expected '{turn.expect_function_call}' but got '{fn_called}'"
            passed = False

        if failure_reason is None and turn_fail:
            failure_reason = turn_fail

        # Tool result injection and conversation history update
        fn_status = "not_called"
        fn_error: Optional[Dict[str, Any]] = None

        if fn_called:
            tool_call_id = f"call_{idx:04d}"
            if fn_called in failure_sim:
                tool_payload = failure_sim[fn_called]
                fn_status = "simulated_failure"
                fn_error = tool_payload
                _record_failure(simulated_failures, idx, fn_called, tool_payload)
                logger.debug(
                    "Injecting failure for '{}' on turn {}: {}",
                    fn_called,
                    idx,
                    tool_payload,
                )
            else:
                tool_payload = {"status": "ok"}
                fn_status = "ok"

            messages.extend(
                [
                    {
                        "role": "assistant",
                        "content": text or None,
                        "tool_calls": [
                            {
                                "id": tool_call_id,
                                "type": "function",
                                "function": {
                                    "name": fn_called,
                                    "arguments": json.dumps(fn_args or {}),
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": json.dumps(tool_payload),
                    },
                ]
            )

            if fn_status == "ok" and fn_called in fmap:
                # Extract outcome from hook
                for hook in fmap[fn_called].get("hooks", []):
                    outcome = hook.get("expected_fields", {}).get("outcome", {})
                    if isinstance(outcome, dict) and outcome.get("source") == "static":
                        actual_outcome = outcome.get("value")

                # Transition to next node
                next_node = fmap[fn_called].get("transition_to")
                if next_node and next_node != current:
                    current = next_node
                    nodes_visited.append(current)
                    new_node = _get_node(template, current)
                    if new_node:
                        messages = _render_messages(
                            _normalise_messages(new_node.get("task_messages", [])),
                            payload_vars,
                        )

        else:
            fn_status = "not_called"
            messages.append({"role": "assistant", "content": text})

        # Validate expect_node AFTER any transition — the scenario's expect_node
        # is the node the bot should be on after processing this turn.
        if turn.expect_node and turn.expect_node != current:
            node_fail = (
                f"Expected node '{turn.expect_node}' after turn {idx} "
                f"but ended on '{current}'"
            )
            if turn_fail is None:
                turn_fail = node_fail
            passed = False

        if failure_reason is None and turn_fail:
            failure_reason = turn_fail

        turn_results.append(
            TurnResult(
                turn_index=idx,
                user_message=turn.user_message,
                bot_response=text,
                function_called=fn_called,
                function_args=fn_args,
                function_execution_status=fn_status,
                function_error_injected=fn_error,
                node_name=current,
                passed=turn_fail is None,
                failure_reason=turn_fail,
                latency_ms=latency_ms,
            )
        )

    # End-node validation
    end_nodes = _get_end_nodes(flow)
    if passed and end_nodes and current not in end_nodes:
        passed = False
        failure_reason = f"Scenario ended on '{current}' — expected one of {end_nodes}."

    return _make_scenario_result(
        scenario,
        passed=passed,
        failure_reason=failure_reason,
        turns=turn_results,
        nodes_visited=nodes_visited,
        final_node=current,
        actual_outcome=actual_outcome,
        total_ms=(time.time() - start) * 1000,
        simulated_failures=simulated_failures,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def run_scenarios_structural(
    template: TemplateModel,
    scenarios: List[GeneratedScenario],
) -> List[ScenarioRunResult]:
    """Run all scenarios in structural mode (no LLM, synchronous)."""
    return [_run_structural(template, s) for s in scenarios]


async def run_scenarios_llm(
    template: TemplateModel,
    scenarios: List[GeneratedScenario],
    on_progress: Optional[Callable] = None,
) -> List[ScenarioRunResult]:
    """
    Run all scenarios sequentially via Azure OpenAI.

    Args:
        on_progress: Optional async callback(completed, total, result).
    """
    from openai import AsyncAzureOpenAI

    model = AZURE_BREEZE_BUDDY_OPENAI_MODEL
    results: List[ScenarioRunResult] = []

    async with AsyncAzureOpenAI(
        api_key=AZURE_OPENAI_API_KEY,
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        api_version=AZURE_OPENAI_API_VERSION,
    ) as client:
        for i, scenario in enumerate(scenarios):
            logger.info(
                "Running LLM scenario {}/{}: {}", i + 1, len(scenarios), scenario.id
            )
            try:
                result = await _run_llm(template, scenario, model, client)
            except Exception as exc:
                logger.error(
                    "Scenario '{}' raised: {}", scenario.id, exc, exc_info=True
                )
                result = _make_scenario_result(
                    scenario,
                    passed=False,
                    failure_reason=str(exc),
                    turns=[],
                    nodes_visited=[],
                )
            results.append(result)
            if on_progress:
                await on_progress(i + 1, len(scenarios), result)

    return results
