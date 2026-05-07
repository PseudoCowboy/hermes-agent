"""Prompt assembly for each agent role.

Each function returns a single prompt string that gets fed to the agent's
CLI. Prompts are intentionally explicit about scope and channel
confinement so even a model with no Mythos training stays on-rails.
"""

from __future__ import annotations

from typing import Iterable

from mythos.agents import AGENT_ROSTER, AgentRole
from mythos.state import Project, ReviewComments, Workstream, WorkstreamKind


def _channel_label(project: Project) -> str:
    return project.project_channel_name or f"<channel for project {project.id}>"


# ---- Draft plan agent ----------------------------------------------------

def draft_plan_prompt(project: Project, prior_review: ReviewComments | None) -> str:
    spec = AGENT_ROSTER[AgentRole.DRAFT_PLAN]
    body = [
        f"You are {spec.name}, the draft plan agent for the Mythos multi-agent "
        f"development system.",
        "",
        f"Project ID: {project.id}",
        f"Project channel: {_channel_label(project)}",
        f"Original user request:",
        "```",
        project.request.strip(),
        "```",
        "",
    ]
    if prior_review is not None:
        body.extend([
            "Previous review feedback that you must address in this revision:",
            "```",
            prior_review.body.strip(),
            "```",
            "",
        ])
        last_spec = project.specs[-1] if project.specs else None
        if last_spec is not None:
            body.extend([
                "Your prior draft (revise this):",
                "```",
                last_spec.body.strip(),
                "```",
                "",
            ])

    body.extend([
        "Your job:",
        "1. If the request lacks important details, output a CLARIFY block at the top:",
        "   `CLARIFY:` followed by 1–3 specific questions, one per line.",
        "2. Otherwise produce a full design spec with these sections:",
        "   - Goal",
        "   - User Stories",
        "   - Functional Requirements",
        "   - Frontend Work",
        "   - Backend Work",
        "   - Testing Plan",
        "",
        "Be concrete. The downstream specialists will implement directly from this spec.",
        "Output ONLY the spec (or CLARIFY block) — no preamble or sign-off.",
    ])
    return "\n".join(body)


# ---- Review agent --------------------------------------------------------

def review_prompt(project: Project) -> str:
    spec = AGENT_ROSTER[AgentRole.REVIEW]
    latest = project.latest_spec()
    if latest is None:
        raise RuntimeError("review_prompt called with no design spec")
    body = [
        f"You are {spec.name}, the review agent.",
        "",
        f"Review the design spec for project {project.id}.",
        "",
        f"Original user request:",
        "```",
        project.request.strip(),
        "```",
        "",
        f"Design spec (version {latest.version}) to review:",
        "```",
        latest.body.strip(),
        "```",
        "",
        "Output your review with these sections:",
        "- Summary (1–2 sentences)",
        "- Blocking Issues (if any — if none, write 'NONE')",
        "- Suggestions (non-blocking)",
        "",
        "If you have BLOCKING issues, end your review with the literal token "
        "`STATUS: REVISE`. Otherwise end with `STATUS: APPROVE`.",
    ]
    return "\n".join(body)


# ---- Main agent (Athena) -------------------------------------------------

def main_intake_prompt(request: str) -> str:
    spec = AGENT_ROSTER[AgentRole.MAIN]
    return (
        f"You are {spec.name}, the main coordinator agent. Decide whether the "
        f"following Discord message is a software project request that should "
        f"start a new project, or a casual message that should not.\n\n"
        f"Message:\n```\n{request.strip()}\n```\n\n"
        f"Reply with one line. Either:\n"
        f"  PROJECT: <a 2–4 word slug suitable for a Discord channel name>\n"
        f"or\n"
        f"  IGNORE: <one-sentence reason>"
    )


def main_decompose_prompt(project: Project) -> str:
    spec = AGENT_ROSTER[AgentRole.MAIN]
    latest = project.latest_spec()
    body = [
        f"You are {spec.name}, the main coordinator. The user has approved the "
        f"design spec. Decide which workstreams to spin up.",
        "",
        f"Approved spec (version {latest.version if latest else '?'}):",
        "```",
        latest.body.strip() if latest else "<missing>",
        "```",
        "",
        "Reply with one line per workstream you want to create, drawn from this set:",
        "  WORKSTREAM: frontend",
        "  WORKSTREAM: backend",
        "  WORKSTREAM: test",
        "",
        "Always include `WORKSTREAM: test` if either frontend or backend is included.",
    ]
    return "\n".join(body)


# ---- Specialist agents (Apollo, Atlas, Hephaestus) -----------------------

def specialist_prompt(
    project: Project,
    workstream: Workstream,
    role: AgentRole,
) -> str:
    spec = AGENT_ROSTER[role]
    latest_spec = project.latest_spec()
    latest_review = project.latest_review()
    body = [
        f"You are {spec.name}, the {workstream.kind.value} agent for project "
        f"{project.id}.",
        "",
        f"Your assigned channel is `{workstream.channel_name}`. You MUST only "
        f"post questions, progress updates, and completion messages in that "
        f"channel — never anywhere else.",
        "",
        "Approved design spec:",
        "```",
        (latest_spec.body.strip() if latest_spec else "<missing>"),
        "```",
        "",
    ]
    if latest_review is not None:
        body.extend([
            "Most recent review notes (for context):",
            "```",
            latest_review.body.strip(),
            "```",
            "",
        ])

    body.extend([
        f"Scope: implement the {workstream.kind.value} portion of the approved "
        f"design.",
        "",
        "When you finish, end your output with the literal token `STATUS: DONE`. "
        "If you are blocked or need clarification, end with `STATUS: BLOCKED` "
        "followed by your question on the next line.",
    ])
    return "\n".join(body)


def joined_messages(messages: Iterable[str]) -> str:
    return "\n\n".join(m.strip() for m in messages if m and m.strip())
