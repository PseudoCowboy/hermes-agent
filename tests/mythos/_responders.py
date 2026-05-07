"""Shared mock CLI responders for mythos integration tests.

Each builder returns a callable that the :class:`mythos.cli_runtime.CLIRuntime`
uses in place of a real subprocess. Responders return JSON-shaped output that
matches what the role prompt asks for.
"""

from __future__ import annotations

import json

from mythos.cli_runtime import CLIInvocation, CLIResult
from mythos.models import AgentRole


def _ok(invocation: CLIInvocation, payload: dict) -> CLIResult:
    return CLIResult(
        role=invocation.role,
        runtime=invocation.runtime,
        exit_code=0,
        stdout=json.dumps(payload),
        stderr="",
        duration_seconds=0.01,
        workspace=str(invocation.workspace),
    )


def athena_intake_responder(display_name: str = "Translator Extension"):
    def _resp(invocation: CLIInvocation) -> CLIResult:
        return _ok(
            invocation,
            {
                "display_name": display_name,
                "summary": "Browser extension that translates selected text on double-click.",
                "intake_message": "Got it — spinning up a project channel.",
            },
        )

    return _resp


def athena_decompose_responder(workstreams: list[dict] | None = None):
    if workstreams is None:
        workstreams = [
            {"type": "frontend", "scope": "Build the popup + content script for double-click translation."},
            {"type": "backend", "scope": "Bundled dictionary lookup library + IPC."},
            {"type": "test", "scope": "Happy path + edge cases (no selection, multiple words, etc)."},
        ]

    def _resp(invocation: CLIInvocation) -> CLIResult:
        return _ok(
            invocation,
            {
                "workstreams": workstreams,
                "channel_message": "Decomposition done. Specialists have their own channels.",
            },
        )

    return _resp


def prometheus_responder(
    *,
    needs_clarification: bool = False,
    questions: list[str] | None = None,
    spec_markdown: str = "## Spec\nA Chrome translator extension.",
):
    def _resp(invocation: CLIInvocation) -> CLIResult:
        if needs_clarification:
            return _ok(
                invocation,
                {
                    "needs_clarification": True,
                    "clarifying_questions": questions or ["Which languages should be supported?"],
                    "channel_message": "I need a quick clarification before drafting.",
                },
            )
        return _ok(
            invocation,
            {
                "needs_clarification": False,
                "spec_markdown": spec_markdown,
                "channel_message": "Spec drafted; pinging Argus to review.",
            },
        )

    return _resp


def prometheus_sequenced_responder(responses: list[dict]):
    """Return responder that yields each response in sequence per call."""

    state = {"i": 0}

    def _resp(invocation: CLIInvocation) -> CLIResult:
        i = min(state["i"], len(responses) - 1)
        state["i"] += 1
        return _ok(invocation, responses[i])

    return _resp


def argus_responder(*, recommendation: str = "accept", review: str = "Looks great. Approve."):
    def _resp(invocation: CLIInvocation) -> CLIResult:
        return _ok(
            invocation,
            {
                "recommendation": recommendation,
                "severity": "info" if recommendation == "accept" else "major",
                "review_markdown": review,
                "channel_message": f"Review: {recommendation}.",
            },
        )

    return _resp


def specialist_responder(role: AgentRole, *, artifact_path: str, artifact_content: str):
    def _resp(invocation: CLIInvocation) -> CLIResult:
        if role == AgentRole.HEPHAESTUS:
            return _ok(
                invocation,
                {
                    "test_plan_markdown": "Test plan: …",
                    "test_artifact_path": artifact_path,
                    "test_artifact_content": artifact_content,
                    "channel_message": "Test plan ready.",
                },
            )
        key_plan = "frontend_plan_markdown" if role == AgentRole.APOLLO else "backend_plan_markdown"
        return _ok(
            invocation,
            {
                key_plan: "Implementation plan…",
                "artifact_path": artifact_path,
                "artifact_content": artifact_content,
                "channel_message": f"{role.value} done.",
            },
        )

    return _resp


def failing_responder(error: str = "CLI binary not found"):
    def _resp(invocation: CLIInvocation) -> CLIResult:
        return CLIResult(
            role=invocation.role,
            runtime=invocation.runtime,
            exit_code=127,
            stdout="",
            stderr=error,
            duration_seconds=0.01,
            workspace=str(invocation.workspace),
            error=error,
        )

    return _resp
