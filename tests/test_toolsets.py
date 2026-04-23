"""Tests for toolsets.py — toolset resolution, validation, and composition."""

import pytest

from toolsets import (
    TOOLSETS,
    get_toolset,
    resolve_toolset,
    resolve_multiple_toolsets,
    get_all_toolsets,
    get_toolset_names,
    validate_toolset,
    create_custom_toolset,
    get_toolset_info,
)


class TestGetToolset:
    def test_known_toolset(self):
        ts = get_toolset("web")
        assert ts is not None
        assert "web_search" in ts["tools"]

    def test_unknown_returns_none(self):
        assert get_toolset("nonexistent") is None


class TestResolveToolset:
    def test_leaf_toolset(self):
        tools = resolve_toolset("web")
        assert set(tools) == {"web_search", "web_extract"}

    def test_composite_toolset(self):
        tools = resolve_toolset("debugging")
        assert "terminal" in tools
        assert "web_search" in tools
        assert "web_extract" in tools

    def test_cycle_detection(self):
        # Create a cycle: A includes B, B includes A
        TOOLSETS["_cycle_a"] = {"description": "test", "tools": ["t1"], "includes": ["_cycle_b"]}
        TOOLSETS["_cycle_b"] = {"description": "test", "tools": ["t2"], "includes": ["_cycle_a"]}
        try:
            tools = resolve_toolset("_cycle_a")
            # Should not infinite loop — cycle is detected
            assert "t1" in tools
            assert "t2" in tools
        finally:
            del TOOLSETS["_cycle_a"]
            del TOOLSETS["_cycle_b"]

    def test_unknown_toolset_returns_empty(self):
        assert resolve_toolset("nonexistent") == []

    def test_all_alias(self):
        tools = resolve_toolset("all")
        assert len(tools) > 10  # Should resolve all tools from all toolsets

    def test_star_alias(self):
        tools = resolve_toolset("*")
        assert len(tools) > 10


class TestResolveMultipleToolsets:
    def test_combines_and_deduplicates(self):
        tools = resolve_multiple_toolsets(["web", "terminal"])
        assert "web_search" in tools
        assert "web_extract" in tools
        assert "terminal" in tools
        # No duplicates
        assert len(tools) == len(set(tools))

    def test_empty_list(self):
        assert resolve_multiple_toolsets([]) == []


class TestValidateToolset:
    def test_valid(self):
        assert validate_toolset("web") is True
        assert validate_toolset("terminal") is True

    def test_all_alias_valid(self):
        assert validate_toolset("all") is True
        assert validate_toolset("*") is True

    def test_invalid(self):
        assert validate_toolset("nonexistent") is False


class TestGetToolsetInfo:
    def test_leaf(self):
        info = get_toolset_info("web")
        assert info["name"] == "web"
        assert info["is_composite"] is False
        assert info["tool_count"] == 2

    def test_composite(self):
        info = get_toolset_info("debugging")
        assert info["is_composite"] is True
        assert info["tool_count"] > len(info["direct_tools"])

    def test_unknown_returns_none(self):
        assert get_toolset_info("nonexistent") is None


class TestCreateCustomToolset:
    def test_runtime_creation(self):
        create_custom_toolset(
            name="_test_custom",
            description="Test toolset",
            tools=["web_search"],
            includes=["terminal"],
        )
        try:
            tools = resolve_toolset("_test_custom")
            assert "web_search" in tools
            assert "terminal" in tools
            assert validate_toolset("_test_custom") is True
        finally:
            del TOOLSETS["_test_custom"]


class TestToolsetConsistency:
    """Verify structural integrity of the built-in TOOLSETS dict."""

    def test_all_toolsets_have_required_keys(self):
        for name, ts in TOOLSETS.items():
            assert "description" in ts, f"{name} missing description"
            assert "tools" in ts, f"{name} missing tools"
            assert "includes" in ts, f"{name} missing includes"

    def test_all_includes_reference_existing_toolsets(self):
        for name, ts in TOOLSETS.items():
            for inc in ts["includes"]:
                assert inc in TOOLSETS, f"{name} includes unknown toolset '{inc}'"

    def test_hermes_platforms_share_core_tools(self):
        """All hermes-* platform toolsets should share the core tool set.

        Mutating workflow tools are platform-gated (CLI + Discord only),
        so we compare the core intersection rather than raw equality.
        """
        from toolsets import _HERMES_WORKFLOW_MUTATING_TOOLS
        mutating = set(_HERMES_WORKFLOW_MUTATING_TOOLS)
        platforms = ["hermes-cli", "hermes-telegram", "hermes-discord", "hermes-whatsapp", "hermes-slack", "hermes-signal", "hermes-homeassistant"]
        tool_sets = [set(TOOLSETS[p]["tools"]) - mutating for p in platforms]
        # Core (non-mutating-workflow) tools should be identical across platforms.
        for ts in tool_sets[1:]:
            assert ts == tool_sets[0]

    def test_mutating_workflow_tools_only_on_cli_and_discord(self):
        """Repo-mutating workflow tools must be opt-in per platform."""
        from toolsets import _HERMES_WORKFLOW_MUTATING_TOOLS
        mutating = set(_HERMES_WORKFLOW_MUTATING_TOOLS)
        allowed = {"hermes-cli", "hermes-discord"}
        for name, ts in TOOLSETS.items():
            if not name.startswith("hermes-"):
                continue
            overlap = set(ts["tools"]) & mutating
            if name in allowed:
                assert overlap == mutating, f"{name} missing mutating workflow tools: {mutating - overlap}"
            else:
                assert overlap == set(), f"{name} must not expose mutating workflow tools: {overlap}"

    def test_hermes_cli_includes_workflow_tools(self):
        tools = set(resolve_toolset("hermes-cli"))
        for tool in {
            "workflow_create_project",
            "workflow_save_plan",
            "workflow_approve_plan",
            "workflow_status",
            "workflow_decompose",
            "workflow_handoff",
            "workflow_checkpoint",
            "workflow_sync_tasks",
            "workflow_review_task",
        }:
            assert tool in tools


class TestWorkflowToolsetRegistration:
    """The registered `toolset` metadata for each workflow tool must match
    the split in toolsets.py — otherwise parent-toolset derivation in
    delegate_tool collapses workflow-readonly into the full-mutating set
    (and vice versa)."""

    def test_workflow_status_registered_as_readonly(self):
        # Ensure tool modules are imported so the registry is populated.
        import model_tools  # noqa: F401
        from tools.registry import registry

        assert registry.get_toolset_for_tool("workflow_status") == "workflow-readonly"

    def test_mutating_workflow_tools_registered_as_mutating(self):
        import model_tools  # noqa: F401
        from tools.registry import registry
        from toolsets import _HERMES_WORKFLOW_MUTATING_TOOLS

        for tool in _HERMES_WORKFLOW_MUTATING_TOOLS:
            assert registry.get_toolset_for_tool(tool) == "workflow-mutating", (
                f"{tool} should be in the workflow-mutating toolset"
            )
