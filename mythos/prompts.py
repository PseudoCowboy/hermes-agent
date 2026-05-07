"""Prompt builders for each Mythos role.

Prompts are built per-call from project state. They share a common preamble
that pins the role, the channel constraint, and the marker grammar from
``mythos.protocol``.
"""

from __future__ import annotations

from textwrap import dedent
from typing import List, Optional

from mythos.config import (
    ROLE_ATHENA,
    ROLE_PROMETHEUS,
    ROLE_ARGUS,
    ROLE_HEPHAESTUS,
    ROLE_APOLLO,
    ROLE_ATLAS,
)


_PROTOCOL_REMINDER = dedent(
    """
    OUTPUT PROTOCOL — important.
    Your output is parsed by an orchestrator. Use the marker grammar below.
    Each marker must appear on its own line.

    - <<MYTHOS:STATUS:received>>     when you have read the assignment
    - <<MYTHOS:STATUS:question>>     when you need clarification (then write the question)
    - <<MYTHOS:STATUS:finished>>     when your task is complete
    - <<MYTHOS:STATUS:failed>>       when you cannot complete the task

    Reviewers (Argus) additionally emit ONE decision marker:
    - <<MYTHOS:DECISION:approve>>            ready for user approval
    - <<MYTHOS:DECISION:request_changes>>    revisions needed
    - <<MYTHOS:DECISION:block>>              cannot proceed

    The draft plan agent (Prometheus) wraps its design spec in a fence:
        <<MYTHOS:DESIGN_BEGIN>>
        ... markdown design spec ...
        <<MYTHOS:DESIGN_END>>

    Keep prose terse. Channel-scoped: never tell the user to look elsewhere;
    write the answer in this channel.
    """
).strip()


def _channel_reminder(role: str, channel_kind: str) -> str:
    return (
        f"You are agent **{role.title()}**. You may only post in the {channel_kind} channel "
        "you were invoked from. Do not reference or contact other channels."
    )


def athena_intake_prompt(user_request: str, project_slug: str) -> str:
    return dedent(
        f"""
        {_channel_reminder(ROLE_ATHENA, "main")}

        You are the main coordinator for a new project request just posted in the main Discord channel:

        ```
        {user_request}
        ```

        Tasks:
        1. Acknowledge the request in 1–2 sentences.
        2. Confirm a short, kebab-case project slug you'd use; we have already
           tentatively chosen "{project_slug}" — if it's reasonable, accept it.
        3. State that a dedicated project channel is being created.

        Do NOT design or implement anything. {_PROTOCOL_REMINDER}
        End with <<MYTHOS:STATUS:finished>>.
        """
    ).strip()


def prometheus_draft_prompt(
    user_request: str,
    project_slug: str,
    prior_questions_and_answers: Optional[List[str]] = None,
    review_feedback: Optional[str] = None,
    previous_design: Optional[str] = None,
) -> str:
    history = ""
    if prior_questions_and_answers:
        history = "\nPrior Q&A in this project channel:\n" + "\n".join(
            f"- {qa}" for qa in prior_questions_and_answers
        )
    revision = ""
    if review_feedback and previous_design:
        revision = dedent(
            f"""

            REVISION REQUESTED. The previous design was:
            ----- previous design -----
            {previous_design}
            ----- end previous design -----

            Reviewer (Argus) feedback to address:
            {review_feedback}
            """
        ).strip()
    return dedent(
        f"""
        {_channel_reminder(ROLE_PROMETHEUS, "project")}

        You are the draft plan agent. Project slug: {project_slug}.
        User's original request:
        ```
        {user_request}
        ```
        {history}
        {revision}

        Produce a concise design spec (markdown) covering:
          - Overview & goals
          - User-facing behavior / UX
          - Frontend work items
          - Backend work items
          - Test plan
          - Risks / open questions

        If essential information is missing, you MAY first ask up to TWO
        clarifying questions instead of writing the design. In that case emit
        <<MYTHOS:STATUS:question>> and your question(s), then
        <<MYTHOS:STATUS:finished>>. Otherwise wrap the design in
        <<MYTHOS:DESIGN_BEGIN>> ... <<MYTHOS:DESIGN_END>> and end with
        <<MYTHOS:STATUS:finished>>.

        {_PROTOCOL_REMINDER}
        """
    ).strip()


def argus_review_prompt(design_body: str, project_slug: str, version: int) -> str:
    return dedent(
        f"""
        {_channel_reminder(ROLE_ARGUS, "project")}

        You are the review agent. Review the design spec below for project
        "{project_slug}" (version {version}). Look for: missing acceptance
        criteria, ambiguous requirements, risky scope, and major gaps in the
        frontend / backend / test breakdown.

        ----- design v{version} -----
        {design_body}
        ----- end design -----

        Write review comments as a short bullet list, then emit ONE decision marker:
          - <<MYTHOS:DECISION:approve>> if ready for user sign-off
          - <<MYTHOS:DECISION:request_changes>> if revisions are needed
          - <<MYTHOS:DECISION:block>> only for severe blockers

        End with <<MYTHOS:STATUS:finished>>.
        {_PROTOCOL_REMINDER}
        """
    ).strip()


def athena_decision_prompt(
    design_body: str, review_comments: str, decision: str
) -> str:
    return dedent(
        f"""
        {_channel_reminder(ROLE_ATHENA, "project")}

        You are the main coordinator. The reviewer (Argus) has finished:
        decision = `{decision}`.

        Reviewer comments:
        {review_comments}

        Latest design:
        {design_body}

        Compose a short message for the project channel that:
          - summarises the design in 2–3 lines
          - lists the reviewer's main points
          - if decision == approve: explicitly asks the user to type
            `approve` (or `reject`) to gate implementation
          - if decision == request_changes: tells the user a revision is in flight
          - if decision == block: explains the blocker and asks the user how to proceed

        {_PROTOCOL_REMINDER}
        End with <<MYTHOS:STATUS:finished>>.
        """
    ).strip()


def _impl_prompt(
    role: str,
    role_title: str,
    channel_kind: str,
    project_slug: str,
    design_body: str,
    workspace_path: str,
    extra_instructions: str = "",
) -> str:
    return dedent(
        f"""
        {_channel_reminder(role, channel_kind)}

        You are {role_title} working on project "{project_slug}".
        Your isolated workspace is: {workspace_path}
        Do all file work inside that directory. Do not touch any other path.

        Approved design spec:
        ----- design -----
        {design_body}
        ----- end design -----

        {extra_instructions}

        First emit <<MYTHOS:STATUS:received>> and a 1-line plan.
        Then implement. If you need clarification, emit
        <<MYTHOS:STATUS:question>> and ask in this channel only — do not
        cross-post.
        Finish with a short summary of changes and <<MYTHOS:STATUS:finished>>.

        {_PROTOCOL_REMINDER}
        """
    ).strip()


def apollo_frontend_prompt(project_slug: str, design_body: str, workspace_path: str) -> str:
    return _impl_prompt(
        ROLE_APOLLO,
        "Apollo, the Frontend agent (Gemini)",
        "frontend",
        project_slug,
        design_body,
        workspace_path,
        "Implement the frontend portion of the design.",
    )


def atlas_backend_prompt(project_slug: str, design_body: str, workspace_path: str) -> str:
    return _impl_prompt(
        ROLE_ATLAS,
        "Atlas, the Backend agent (Claude Code)",
        "backend",
        project_slug,
        design_body,
        workspace_path,
        "Implement the backend portion of the design.",
    )


def hephaestus_test_prompt(
    project_slug: str,
    design_body: str,
    workspace_path: str,
    frontend_summary: str,
    backend_summary: str,
) -> str:
    extras = dedent(
        f"""
        Implementation summaries from the other agents:
          - Apollo (frontend): {frontend_summary or '(none reported)'}
          - Atlas  (backend):  {backend_summary or '(none reported)'}

        Write/run validation tests against the workspace.
        """
    ).strip()
    return _impl_prompt(
        ROLE_HEPHAESTUS,
        "Hephaestus, the Test agent (Codex)",
        "test",
        project_slug,
        design_body,
        workspace_path,
        extras,
    )
