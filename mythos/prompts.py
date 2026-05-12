"""Per-role prompt templates.

Each agent gets a focused, role-specific instruction prefix. The
prompts are deliberately terse — the heavy lifting is done by the
underlying CLIs, and we want the templates to fit in one screen so
operators can audit them.
"""

from __future__ import annotations

from typing import Optional

from mythos.roles import Role


def prometheus_draft(intake_text: str, prior_spec: Optional[str], feedback: Optional[str]) -> str:
    """Prompt Prometheus to write or revise the design spec."""
    parts = [
        "You are PROMETHEUS, the Draft Plan Agent in the Mythos multi-agent",
        "Discord development system. Your job is to take a user's free-form",
        "feature request and produce a clear, structured design spec.",
        "",
        "Write the spec in Markdown. Cover, in this order:",
        "  1. Goals (what this is + who it's for)",
        "  2. Scope — in & out",
        "  3. Key components / architecture",
        "  4. Data shapes / interfaces",
        "  5. External dependencies",
        "  6. Open questions you cannot resolve without the user",
        "",
        "Be concise. Aim for ≤ 1500 words. If you have ≤ 2 clarifying",
        "questions, list them at the very top under '## Clarifying",
        "Questions'; otherwise omit that section. Do not invent answers",
        "the user has not given you.",
        "",
        f"User's request:\n---\n{intake_text}\n---",
    ]
    if prior_spec:
        parts.extend(
            [
                "",
                "Your previous draft is below — revise it according to the feedback.",
                "Output only the new full spec, not a diff.",
                "",
                "Previous draft:\n---",
                prior_spec,
                "---",
            ]
        )
    if feedback:
        parts.extend(
            [
                "",
                "Feedback to incorporate:",
                "---",
                feedback,
                "---",
            ]
        )
    parts.extend(
        [
            "",
            "Write the spec now. Do not add commentary before or after the spec.",
        ]
    )
    return "\n".join(parts)


def argus_review(spec_text: str) -> str:
    """Prompt Argus to review the spec."""
    return "\n".join(
        [
            "You are ARGUS, the Review Agent in the Mythos multi-agent system.",
            "Your job is to peer-review a design spec written by Prometheus.",
            "",
            "Output exactly two sections:",
            "  ## Verdict",
            "  One line: APPROVED  or  CHANGES_REQUESTED",
            "  ## Comments",
            "  Bullet-list of concrete, actionable comments. Each bullet should",
            "  point at a specific section of the spec. Be tough but fair.",
            "",
            "Do not rewrite the spec. Comment only.",
            "",
            "Spec to review:",
            "---",
            spec_text,
            "---",
        ]
    )


def hermes_decompose(spec_text: str) -> str:
    """Prompt Hermes to decompose an approved spec into role work-items."""
    return "\n".join(
        [
            "You are HERMES, the Main Agent in the Mythos multi-agent system.",
            "The spec below has been approved by the user. Decompose the work",
            "into three role-specific work-items, one each for FRONTEND",
            "(Apollo), BACKEND (Atlas), and TEST (Hephaestus).",
            "",
            "Output Markdown with three sections in this order:",
            "  ## Frontend (Apollo)",
            "  ## Backend (Atlas)",
            "  ## Test (Hephaestus)",
            "",
            "Each section must contain: a one-line goal, an Inputs subsection,",
            "an Outputs subsection, and an Acceptance Criteria subsection.",
            "If a role has no work in this spec, write 'No work for this role'",
            "in that section.",
            "",
            "Approved spec:",
            "---",
            spec_text,
            "---",
        ]
    )


def specialist_implement(role: Role, spec_text: str, work_item: str) -> str:
    """Prompt a specialist (Apollo/Atlas/Hephaestus) to implement its slice."""
    role_name = role.value.upper()
    return "\n".join(
        [
            f"You are {role_name}, the {role.value} specialist agent in the",
            "Mythos multi-agent system. The user-approved spec and your",
            "specific work-item are below.",
            "",
            "Implement the work in the current working directory. When done,",
            "write a one-paragraph summary of what you did and which files",
            "you created or changed. If you cannot proceed without a",
            "clarification from the user, end your output with a line that",
            "starts exactly with 'QUESTION:' followed by the question; the",
            "system will route it to your channel.",
            "",
            "Approved spec:",
            "---",
            spec_text,
            "---",
            "",
            "Your work-item:",
            "---",
            work_item,
            "---",
        ]
    )


def hermes_intake_ack(intake_text: str, slug: str) -> str:
    """Quick ack message Hermes posts in #main."""
    return (
        f"Got your idea — spinning up project `{slug}`. "
        f"I'll create a dedicated channel and hand it to Prometheus to draft a spec."
    )
