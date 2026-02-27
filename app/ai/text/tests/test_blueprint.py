"""
Sample tests for the Blueprint text agent.

These tests validate the agent setup, configuration, and pipeline structure
without requiring external API keys or LLM calls.

Run with:
    python -m pytest app/ai/text/tests/test_blueprint.py -v

Or run directly:
    python app/ai/text/tests/test_blueprint.py
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

from app.ai.text.blueprint.agent import (
    _STAGE_PROGRESS,
    DIALOGUE_ENHANCER,
    ORCHESTRATOR_SYSTEM_PROMPT,
    REVIEWER,
    TEMPLATE_ARCHITECT,
    PipelineStage,
    PipelineStatus,
    _agent_display_name,
    _extract_agent_name,
    _import_template_tools,
    _make_status,
)
from app.ai.text.blueprint.prompts import (
    ARCHITECT_SYSTEM_PROMPT,
    ENHANCER_SYSTEM_PROMPT,
    REVIEWER_SYSTEM_PROMPT,
)
from app.ai.text.playground import (
    _register_defaults,
    get_agent,
    list_agents,
    register_agent,
)

# ---------------------------------------------------------------------------
# Subagent configuration tests
# ---------------------------------------------------------------------------


class TestSubagentDefinitions:
    """Verify subagent configurations are correctly defined."""

    def test_architect_has_required_fields(self):
        assert TEMPLATE_ARCHITECT["name"] == "template-architect"
        assert "description" in TEMPLATE_ARCHITECT
        assert "system_prompt" in TEMPLATE_ARCHITECT
        assert "model" in TEMPLATE_ARCHITECT
        # tools is [] in static definition; populated at runtime by create_blueprint_agent
        assert "tools" in TEMPLATE_ARCHITECT

    def test_enhancer_has_required_fields(self):
        assert DIALOGUE_ENHANCER["name"] == "dialogue-enhancer"
        assert "description" in DIALOGUE_ENHANCER
        assert "system_prompt" in DIALOGUE_ENHANCER
        assert "model" in DIALOGUE_ENHANCER
        assert "gpt-4o" in DIALOGUE_ENHANCER["model"]

    def test_reviewer_has_required_fields(self):
        assert REVIEWER["name"] == "reviewer"
        assert "description" in REVIEWER
        assert "system_prompt" in REVIEWER
        assert "model" in REVIEWER

    def test_all_agents_use_different_names(self):
        names = {
            TEMPLATE_ARCHITECT["name"],
            DIALOGUE_ENHANCER["name"],
            REVIEWER["name"],
        }
        assert len(names) == 3, "All subagent names must be unique"


# ---------------------------------------------------------------------------
# System prompt tests
# ---------------------------------------------------------------------------


class TestSystemPrompts:
    """Verify system prompts contain critical instructions."""

    def test_architect_reads_codebase(self):
        assert "template/types.py" in ARCHITECT_SYSTEM_PROMPT
        assert "order-confirmation.json" in ARCHITECT_SYSTEM_PROMPT
        assert "hooks.py" in ARCHITECT_SYSTEM_PROMPT

    def test_architect_defines_json_structure(self):
        assert "merchant" in ARCHITECT_SYSTEM_PROMPT
        assert "template_name" in ARCHITECT_SYSTEM_PROMPT
        assert "flow" in ARCHITECT_SYSTEM_PROMPT
        assert "initial_node" in ARCHITECT_SYSTEM_PROMPT
        assert "nodes" in ARCHITECT_SYSTEM_PROMPT

    def test_architect_defines_node_structure(self):
        assert "node_name" in ARCHITECT_SYSTEM_PROMPT
        assert "task_messages" in ARCHITECT_SYSTEM_PROMPT
        assert "role_messages" in ARCHITECT_SYSTEM_PROMPT
        assert "functions" in ARCHITECT_SYSTEM_PROMPT
        assert "pre_actions" in ARCHITECT_SYSTEM_PROMPT
        assert "post_actions" in ARCHITECT_SYSTEM_PROMPT

    def test_architect_defines_hook_patterns(self):
        assert "update_outcome_in_database" in ARCHITECT_SYSTEM_PROMPT
        assert "send_http_request" in ARCHITECT_SYSTEM_PROMPT
        assert "static" in ARCHITECT_SYSTEM_PROMPT
        assert "llm" in ARCHITECT_SYSTEM_PROMPT

    def test_architect_defines_terminal_node_rules(self):
        assert "end_conversation" in ARCHITECT_SYSTEM_PROMPT
        assert "mute_stt" in ARCHITECT_SYSTEM_PROMPT
        assert "unmute_stt" in ARCHITECT_SYSTEM_PROMPT

    def test_enhancer_targets_dialogue_fields(self):
        assert "role_messages" in ENHANCER_SYSTEM_PROMPT
        assert "task_messages" in ENHANCER_SYSTEM_PROMPT
        assert "description" in ENHANCER_SYSTEM_PROMPT

    def test_enhancer_prohibits_formatting(self):
        assert "markdown" in ENHANCER_SYSTEM_PROMPT.lower()
        assert "asterisks" in ENHANCER_SYSTEM_PROMPT.lower()
        assert "emoji" in ENHANCER_SYSTEM_PROMPT.lower()

    def test_enhancer_includes_cultural_rules(self):
        assert "Sir" in ENHANCER_SYSTEM_PROMPT
        assert "Madam" in ENHANCER_SYSTEM_PROMPT
        assert "Hindi" in ENHANCER_SYSTEM_PROMPT

    def test_enhancer_protects_bot_identity(self):
        prompt_lower = ENHANCER_SYSTEM_PROMPT.lower()
        assert "bot" in prompt_lower
        assert "never reveal" in prompt_lower or "never suggest" in prompt_lower

    def test_reviewer_reads_codebase(self):
        assert "template/types.py" in REVIEWER_SYSTEM_PROMPT
        assert "order-confirmation.json" in REVIEWER_SYSTEM_PROMPT

    def test_reviewer_has_validation_checks(self):
        assert "Schema Compliance" in REVIEWER_SYSTEM_PROMPT
        assert "Flow Integrity" in REVIEWER_SYSTEM_PROMPT
        assert "Terminal Node" in REVIEWER_SYSTEM_PROMPT
        assert "Hook Validation" in REVIEWER_SYSTEM_PROMPT
        assert "Variable Consistency" in REVIEWER_SYSTEM_PROMPT

    def test_orchestrator_defines_pipeline(self):
        assert "template-architect" in ORCHESTRATOR_SYSTEM_PROMPT
        assert "dialogue-enhancer" in ORCHESTRATOR_SYSTEM_PROMPT
        assert "reviewer" in ORCHESTRATOR_SYSTEM_PROMPT
        assert "write_todos" in ORCHESTRATOR_SYSTEM_PROMPT

    def test_orchestrator_defines_file_convention(self):
        assert "blueprint_draft.json" in ORCHESTRATOR_SYSTEM_PROMPT
        assert "blueprint_enhanced.json" in ORCHESTRATOR_SYSTEM_PROMPT
        assert "blueprint_final.json" in ORCHESTRATOR_SYSTEM_PROMPT

    def test_architect_has_template_awareness_instructions(self):
        assert "list_templates_tool" in ARCHITECT_SYSTEM_PROMPT
        assert "get_template_by_id_tool" in ARCHITECT_SYSTEM_PROMPT

    def test_orchestrator_has_template_reference_instructions(self):
        assert "list_templates_tool" in ORCHESTRATOR_SYSTEM_PROMPT
        assert "get_template_by_id_tool" in ORCHESTRATOR_SYSTEM_PROMPT
        assert "Template Reference Awareness" in ORCHESTRATOR_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Pipeline status model tests
# ---------------------------------------------------------------------------


class TestPipelineStatus:
    """Verify pipeline status tracking works correctly."""

    def test_all_stages_have_progress(self):
        for stage in PipelineStage:
            assert stage in _STAGE_PROGRESS, f"Stage {stage} missing from progress map"

    def test_progress_increases_monotonically(self):
        ordered_stages = [
            PipelineStage.INITIALIZING,
            PipelineStage.PLANNING,
            PipelineStage.ARCHITECT_RUNNING,
            PipelineStage.ARCHITECT_COMPLETE,
            PipelineStage.ENHANCER_RUNNING,
            PipelineStage.ENHANCER_COMPLETE,
            PipelineStage.REVIEWER_RUNNING,
            PipelineStage.REVIEWER_COMPLETE,
            PipelineStage.COMPLETED,
        ]
        for i in range(1, len(ordered_stages)):
            prev = _STAGE_PROGRESS[ordered_stages[i - 1]]
            curr = _STAGE_PROGRESS[ordered_stages[i]]
            assert curr >= prev, (
                f"Progress should increase: {ordered_stages[i-1]}={prev} "
                f"-> {ordered_stages[i]}={curr}"
            )

    def test_completed_is_100(self):
        assert _STAGE_PROGRESS[PipelineStage.COMPLETED] == 100

    def test_error_is_negative(self):
        assert _STAGE_PROGRESS[PipelineStage.ERROR] < 0

    def test_make_status_creates_valid_model(self):
        import time

        start = time.time()
        status = _make_status(
            PipelineStage.ARCHITECT_RUNNING,
            "Testing...",
            start,
            agent_name="template-architect",
        )
        assert isinstance(status, PipelineStatus)
        assert status.stage == PipelineStage.ARCHITECT_RUNNING
        assert status.message == "Testing..."
        assert status.agent_name == "template-architect"
        assert status.progress_pct == 15
        assert status.elapsed_secs >= 0


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelperFunctions:
    """Verify utility functions work correctly."""

    def test_extract_agent_name_from_dict(self):
        assert _extract_agent_name({"name": "reviewer"}) == "reviewer"
        assert _extract_agent_name({"agent_name": "reviewer"}) == "reviewer"

    def test_extract_agent_name_from_prompt(self):
        result = _extract_agent_name(
            {"prompt": "delegate to template-architect to generate"}
        )
        assert result == "template-architect"

    def test_extract_agent_name_returns_none_for_non_dict(self):
        assert _extract_agent_name("not a dict") is None
        assert _extract_agent_name(None) is None

    def test_extract_agent_name_returns_raw_name(self):
        # Returns the name as-is; caller checks against _AGENT_STAGE_MAP
        assert _extract_agent_name({"name": "unknown-agent"}) == "unknown-agent"

    def test_agent_display_name(self):
        assert "Claude" in _agent_display_name("template-architect")
        assert "GPT-4o" in _agent_display_name("dialogue-enhancer")
        assert "Claude" in _agent_display_name("reviewer")
        assert _agent_display_name("unknown") == "unknown"


# ---------------------------------------------------------------------------
# Template tools tests
# ---------------------------------------------------------------------------


class TestTemplateTools:
    """Verify template awareness tools are configured correctly."""

    def test_import_template_tools_returns_list(self):
        tools = _import_template_tools()
        # Returns list (may be empty if langchain_core not installed)
        assert isinstance(tools, list)

    def test_architect_description_mentions_tools(self):
        assert "list" in TEMPLATE_ARCHITECT["description"].lower()
        assert "fetch" in TEMPLATE_ARCHITECT["description"].lower()


# ---------------------------------------------------------------------------
# Playground registry tests
# ---------------------------------------------------------------------------


class TestPlaygroundRegistry:
    """Verify the playground agent registry works correctly."""

    def test_register_and_retrieve_agent(self):
        register_agent(
            name="test-agent",
            description="A test agent",
            invoke_fn=AsyncMock(),
            stream_fn=AsyncMock(),
        )
        agent = get_agent("test-agent")
        assert agent is not None
        assert agent["name"] == "test-agent"
        assert agent["description"] == "A test agent"

    def test_list_agents_includes_registered(self):
        register_agent(
            name="list-test-agent",
            description="For listing test",
            invoke_fn=AsyncMock(),
        )
        agents = list_agents()
        names = [a["name"] for a in agents]
        assert "list-test-agent" in names

    def test_get_unknown_agent_returns_none(self):
        assert get_agent("nonexistent-agent-xyz") is None

    def test_register_defaults_adds_blueprint(self):
        _register_defaults()
        agent = get_agent("blueprint")
        assert agent is not None
        assert agent["name"] == "blueprint"
        assert agent["invoke_fn"] is not None
        assert agent["stream_fn"] is not None


# ---------------------------------------------------------------------------
# Template structure validation tests
# ---------------------------------------------------------------------------


class TestTemplateStructureValidation:
    """Validate the reference template follows all documented rules."""

    @classmethod
    def setup_class(cls):
        """Load the reference template once for all tests."""
        template_path = Path(
            "app/ai/voice/agents/breeze_buddy/examples/templates/"
            "order-confirmation.json"
        )
        if template_path.exists():
            with open(template_path) as f:
                cls.template = json.load(f)
            cls.has_template = True
        else:
            cls.template = {}
            cls.has_template = False

    def test_template_loaded(self):
        if not self.has_template:
            return  # Skip if template file not present
        assert "flow" in self.template
        assert "nodes" in self.template["flow"]

    def test_initial_node_exists(self):
        if not self.has_template:
            return
        flow = self.template["flow"]
        initial = flow["initial_node"]
        node_names = [n["node_name"] for n in flow["nodes"]]
        assert initial in node_names, f"initial_node '{initial}' not in nodes"

    def test_all_transitions_valid(self):
        if not self.has_template:
            return
        flow = self.template["flow"]
        node_names = {n["node_name"] for n in flow["nodes"]}
        for node in flow["nodes"]:
            for func in node.get("functions", []):
                target = func.get("transition_to")
                if target:
                    assert target in node_names, (
                        f"Node '{node['node_name']}' function "
                        f"'{func['function_name']}' transitions to "
                        f"'{target}' which does not exist"
                    )

    def test_terminal_nodes_have_end_conversation(self):
        if not self.has_template:
            return
        for node in self.template["flow"]["nodes"]:
            if not node.get("functions"):
                post_handlers = [a.get("handler") for a in node.get("post_actions", [])]
                assert "end_conversation" in post_handlers, (
                    f"Terminal node '{node['node_name']}' missing "
                    f"end_conversation in post_actions"
                )

    def test_no_orphan_nodes(self):
        if not self.has_template:
            return
        flow = self.template["flow"]
        initial = flow["initial_node"]
        reachable = {initial}
        changed = True
        while changed:
            changed = False
            for node in flow["nodes"]:
                if node["node_name"] in reachable:
                    for func in node.get("functions", []):
                        target = func.get("transition_to")
                        if target and target not in reachable:
                            reachable.add(target)
                            changed = True

        all_nodes = {n["node_name"] for n in flow["nodes"]}
        orphans = all_nodes - reachable
        assert not orphans, f"Orphan nodes (unreachable): {orphans}"


# ---------------------------------------------------------------------------
# Run tests directly
# ---------------------------------------------------------------------------


def _run_tests():
    """Run all tests and print results."""
    import traceback

    test_classes = [
        TestSubagentDefinitions,
        TestSystemPrompts,
        TestPipelineStatus,
        TestHelperFunctions,
        TestTemplateTools,
        TestPlaygroundRegistry,
        TestTemplateStructureValidation,
    ]

    total = 0
    passed = 0
    failed = 0
    errors = []

    for cls in test_classes:
        instance = cls()
        if hasattr(cls, "setup_class"):
            cls.setup_class()

        for attr_name in sorted(dir(instance)):
            if not attr_name.startswith("test_"):
                continue

            total += 1
            test_name = f"{cls.__name__}.{attr_name}"

            try:
                method = getattr(instance, attr_name)
                result = method()
                if asyncio.iscoroutine(result):
                    asyncio.get_event_loop().run_until_complete(result)
                passed += 1
                print(f"  PASS  {test_name}")
            except AssertionError as e:
                failed += 1
                errors.append((test_name, str(e)))
                print(f"  FAIL  {test_name}: {e}")
            except Exception as e:
                failed += 1
                errors.append((test_name, traceback.format_exc()))
                print(f"  ERROR {test_name}: {e}")

    print(f"\n{'='*60}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    if errors:
        print(f"\nFailed tests:")
        for name, err in errors:
            print(f"  - {name}: {err[:200]}")

    return failed == 0


if __name__ == "__main__":
    print("=== Blueprint Agent Tests ===\n")
    success = _run_tests()
    sys.exit(0 if success else 1)
