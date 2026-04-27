"""Tests for ToolDispatchContext auto-bind in tools.registry (P6)."""

from __future__ import annotations

import json

import pytest

from tools.registry import (
    ToolDispatchContext,
    register_auto_bind_toolset,
    registry,
    unregister_auto_bind_toolset,
    use_dispatch_context,
)


# Tests register fake tools under the "_p6_test" toolset.  The dispatcher
# only auto-binds for toolsets on the allowlist, so opt this one in for
# the duration of the test module and clean up after.
@pytest.fixture(autouse=True)
def _allow_p6_test_toolset():
    register_auto_bind_toolset("_p6_test")
    try:
        yield
    finally:
        unregister_auto_bind_toolset("_p6_test")


# -----------------------------------------------------------------------------
# Fixture: register a fake tool that echoes its received args back as JSON.
# Cleaned up via deregister to keep the global registry pristine for siblings.
# -----------------------------------------------------------------------------


@pytest.fixture
def echo_tool():
    name = "_p6_echo_scope_id_for_test"

    def _handler(args, **kwargs):
        return json.dumps(args)

    registry.register(
        name=name,
        toolset="_p6_test",
        schema={
            "description": "echo scope_id back for tests",
            # Declare the auto-bind keys so schema-gated injection
            # actually fires for them — real tools always declare
            # their args in parameters.properties.
            "parameters": {
                "type": "object",
                "properties": {
                    "scope_id": {"type": "string"},
                    "slug": {"type": "string"},
                    "stream_name": {"type": "string"},
                    "channel_id": {"type": "string"},
                    "user_id": {"type": "string"},
                    "other": {"type": "string"},
                },
            },
        },
        handler=_handler,
    )
    try:
        yield name
    finally:
        registry.deregister(name)


# -----------------------------------------------------------------------------
# args wins over context, context fills missing args, blank counts as missing
# -----------------------------------------------------------------------------


def test_args_scope_id_used_when_present(echo_tool):
    ctx = ToolDispatchContext(scope_id="from-context")
    out = json.loads(registry.dispatch(echo_tool, {"scope_id": "from-args"}, context=ctx))
    assert out["scope_id"] == "from-args"


def test_context_fills_missing_scope_id(echo_tool):
    ctx = ToolDispatchContext(scope_id="from-context")
    out = json.loads(registry.dispatch(echo_tool, {}, context=ctx))
    assert out["scope_id"] == "from-context"


def test_context_fills_blank_string_scope_id(echo_tool):
    """Orchestrator persona is told to pass empty string when it has no value
    so that auto-bind kicks in.  Make sure the dispatcher honors that."""
    ctx = ToolDispatchContext(scope_id="from-context")
    out = json.loads(registry.dispatch(echo_tool, {"scope_id": ""}, context=ctx))
    assert out["scope_id"] == "from-context"


def test_context_fills_none_scope_id(echo_tool):
    ctx = ToolDispatchContext(scope_id="from-context")
    out = json.loads(registry.dispatch(echo_tool, {"scope_id": None}, context=ctx))
    assert out["scope_id"] == "from-context"


def test_no_context_no_scope_id_passes_through(echo_tool):
    out = json.loads(registry.dispatch(echo_tool, {"scope_id": "explicit"}))
    assert out["scope_id"] == "explicit"


def test_context_kwarg_does_not_leak_to_handler(echo_tool):
    """Handlers don't accept context= — make sure dispatch pops it."""
    captured = {}

    def _handler(args, **kwargs):
        captured["kwargs"] = kwargs
        return json.dumps(args)

    registry.deregister(echo_tool)
    registry.register(
        name=echo_tool,
        toolset="_p6_test",
        schema={"description": "capture kwargs"},
        handler=_handler,
    )
    ctx = ToolDispatchContext(scope_id="from-context")
    registry.dispatch(echo_tool, {}, context=ctx, task_id="abc")
    # task_id is a legitimate dispatch kwarg and should pass through;
    # context is dispatcher-internal and must NOT.
    assert "context" not in captured["kwargs"]
    assert captured["kwargs"].get("task_id") == "abc"


def test_ambient_context_via_use_dispatch_context(echo_tool):
    """The session worker uses use_dispatch_context() rather than threading
    an explicit kwarg through AIAgent — make sure that ambient binding works."""
    with use_dispatch_context(ToolDispatchContext(scope_id="ambient-scope")):
        out = json.loads(registry.dispatch(echo_tool, {}))
    assert out["scope_id"] == "ambient-scope"


def test_explicit_context_kwarg_overrides_ambient(echo_tool):
    """Explicit kwarg wins over the ambient contextvar."""
    with use_dispatch_context(ToolDispatchContext(scope_id="ambient")):
        out = json.loads(
            registry.dispatch(
                echo_tool, {}, context=ToolDispatchContext(scope_id="explicit")
            )
        )
    assert out["scope_id"] == "explicit"


def test_ambient_context_resets_after_with_block(echo_tool):
    with use_dispatch_context(ToolDispatchContext(scope_id="ambient")):
        pass
    out = json.loads(registry.dispatch(echo_tool, {}))
    # No ambient, no explicit — args.scope_id stays absent.
    assert "scope_id" not in out


def test_slug_and_stream_name_also_auto_bind(echo_tool):
    ctx = ToolDispatchContext(
        scope_id="s1", slug="my-project", stream_name="frontend"
    )
    out = json.loads(registry.dispatch(echo_tool, {}, context=ctx))
    assert out["scope_id"] == "s1"
    assert out["slug"] == "my-project"
    assert out["stream_name"] == "frontend"


def test_channel_id_and_user_id_also_auto_bind(echo_tool):
    """Discord-specific bindings — orchestrator's main channel + the
    operator's user_id (used by discord_wait_for_reaction) must be
    fillable from context too, otherwise the orchestrator persona
    can't post or wait for ✅/❌ without remembering ids."""
    ctx = ToolDispatchContext(channel_id="999", user_id="42")
    out = json.loads(registry.dispatch(echo_tool, {}, context=ctx))
    assert out["channel_id"] == "999"
    assert out["user_id"] == "42"


def test_auto_bind_skipped_when_schema_does_not_declare_key():
    """Schema-gating: a tool that doesn't list ``slug`` in its
    parameters.properties must NOT receive a silent ``slug`` injection
    from context.  Without this gate, an unrelated future/MCP tool
    that happens to take a ``slug`` arg would receive the project slug
    and quietly change semantics.
    """
    name = "_p6_no_slug_declared"

    def _handler(args, **kwargs):
        return json.dumps(args)

    registry.register(
        name=name,
        toolset="_p6_test",
        schema={
            "description": "tool that does not declare slug",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope_id": {"type": "string"},
                    # NOTE: slug intentionally omitted.
                },
            },
        },
        handler=_handler,
    )
    try:
        ctx = ToolDispatchContext(scope_id="s1", slug="leaky-slug")
        out = json.loads(registry.dispatch(name, {}, context=ctx))
        assert out["scope_id"] == "s1"
        assert "slug" not in out, (
            f"slug leaked into a tool that doesn't declare it: {out}"
        )
    finally:
        registry.deregister(name)


def test_auto_bind_skipped_when_no_parameters_in_schema():
    """A schema with no ``parameters`` block at all → no auto-bind.
    Defensive: don't inject context into a tool whose surface is
    unknown to us.
    """
    name = "_p6_bare_schema"

    def _handler(args, **kwargs):
        return json.dumps(args)

    registry.register(
        name=name,
        toolset="_p6_test",
        schema={"description": "no parameters declared"},
        handler=_handler,
    )
    try:
        ctx = ToolDispatchContext(scope_id="s1", slug="leaky")
        out = json.loads(registry.dispatch(name, {}, context=ctx))
        assert out == {}
    finally:
        registry.deregister(name)


def test_dispatch_refuses_when_session_closed(echo_tool):
    """If the dispatch context's ``closed_check`` returns True, the
    dispatcher must short-circuit with an error and NOT invoke the
    handler.  Regression for the late-tool-call window between
    teardown setting ``session.closed = True`` and the worker thread's
    in-flight ``agent.run_conversation`` exiting.
    """
    handler_called = {"n": 0}

    def _counting_handler(args, **kwargs):
        handler_called["n"] += 1
        return json.dumps(args)

    registry.deregister(echo_tool)
    registry.register(
        name=echo_tool,
        toolset="_p6_test",
        schema={
            "description": "counts handler invocations",
            "parameters": {
                "type": "object",
                "properties": {"scope_id": {"type": "string"}},
            },
        },
        handler=_counting_handler,
    )

    ctx = ToolDispatchContext(scope_id="s1", closed_check=lambda: True)
    raw = registry.dispatch(echo_tool, {}, context=ctx)
    out = json.loads(raw)
    assert "error" in out
    assert "closed" in out["error"].lower()
    assert handler_called["n"] == 0, "handler ran despite closed session"


def test_dispatch_proceeds_when_closed_check_returns_false(echo_tool):
    """``closed_check`` returning False is the normal hot path — must
    not interfere with auto-bind or handler execution."""
    ctx = ToolDispatchContext(
        scope_id="s1", closed_check=lambda: False
    )
    out = json.loads(registry.dispatch(echo_tool, {}, context=ctx))
    assert out["scope_id"] == "s1"


def test_dispatch_tolerates_broken_closed_check(echo_tool):
    """A ``closed_check`` that raises must fail CLOSED — refuse the
    tool call rather than silently let it through.  This is a safety
    gate: if we can't tell whether the session is alive, assume it
    isn't.  Otherwise a buggy check would let late tool calls mutate
    a deleted project."""
    def _boom():
        raise RuntimeError("closed_check exploded")

    ctx = ToolDispatchContext(scope_id="s1", closed_check=_boom)
    out = json.loads(registry.dispatch(echo_tool, {}, context=ctx))
    assert "error" in out
    assert "closed" in out["error"].lower()

