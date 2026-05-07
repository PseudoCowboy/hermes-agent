"""Approval mediator: spec → review → user → approve/change-request loop."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


_APPROVE_PATTERNS = (
    re.compile(r"^\s*approve(d)?\b", re.IGNORECASE),
    re.compile(r"^\s*looks good\b", re.IGNORECASE),
    re.compile(r"^\s*lgtm\b", re.IGNORECASE),
    re.compile(r"^\s*ship it\b", re.IGNORECASE),
    re.compile(r"^\s*✓"),  # ✓
)

_CHANGE_PATTERNS = (
    re.compile(r"^\s*request changes\b", re.IGNORECASE),
    re.compile(r"^\s*changes?\b[:,\.\-]", re.IGNORECASE),
    re.compile(r"^\s*needs changes\b", re.IGNORECASE),
    re.compile(r"^\s*reject\b", re.IGNORECASE),
)


@dataclass
class UserVerdict:
    """Outcome of parsing a user's reply during AWAITING_USER."""

    approved: bool
    feedback: Optional[str]


def parse_user_response(text: str) -> Optional[UserVerdict]:
    """Decide whether the user message is an approve, a change request,
    or neither (return None to ignore).

    Heuristics:
      * Lines starting with 'approve' / 'lgtm' / 'looks good' / '✓' →
        approved.
      * Lines starting with 'request changes' / 'changes:' / 'reject' →
        change request; the entire message body is the feedback.
      * Any other freeform text > 10 chars during AWAITING_USER is treated
        as a change request (the text becomes the feedback). This matches
        the user-scenario where the user replies with substantive feedback
        rather than a literal verb. Use ``approve`` etc. to short-circuit.
    """
    if not text:
        return None
    t = text.strip()
    if not t:
        return None

    for pat in _APPROVE_PATTERNS:
        if pat.search(t):
            return UserVerdict(approved=True, feedback=None)

    for pat in _CHANGE_PATTERNS:
        if pat.search(t):
            return UserVerdict(approved=False, feedback=t)

    if len(t) >= 10:
        return UserVerdict(approved=False, feedback=t)
    return None


_VERDICT_LINE = re.compile(
    r"verdict[:\s]+(approved|changes_requested)", re.IGNORECASE
)


def parse_review_verdict(review_text: str) -> str:
    """Pull APPROVED / CHANGES_REQUESTED out of an Argus review.

    Falls back to CHANGES_REQUESTED if the verdict line is malformed —
    we'd rather over-prompt than rubber-stamp.
    """
    m = _VERDICT_LINE.search(review_text or "")
    if not m:
        return "changes_requested"
    return m.group(1).lower()
