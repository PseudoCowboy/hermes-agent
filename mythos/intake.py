"""Intake classifier: decides which messages in the main channel become projects.

Intentionally lightweight — a real deployment can swap this for an LLM
call. The default heuristic is:
  - non-empty after strip
  - contains a project verb / noun keyword
  - long enough to plausibly describe work (>= 5 words)
  - not a known meta command (`!cancel`, `?help`, etc.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List


META_PREFIXES = ("!", "/", "?", ".")
APPROVAL_PATTERNS = (
    r"^(?:lgtm|approve(?:d)?|approved!|ship it|looks good|sounds good)\b",
    r"^👍",
    r"^:white_check_mark:",
    r"^:thumbsup:",
)


@dataclass
class IntakeDecision:
    is_project: bool
    reason: str


class IntakeClassifier:
    def __init__(self, keywords: Iterable[str]):
        self._keywords = [k.lower() for k in keywords]

    def classify(self, content: str) -> IntakeDecision:
        text = (content or "").strip()
        if not text:
            return IntakeDecision(False, "empty")
        if text.startswith(META_PREFIXES):
            return IntakeDecision(False, "meta-command prefix")
        words = text.split()
        if len(words) < 4:
            return IntakeDecision(False, "too short")
        lowered = text.lower()
        if not any(kw in lowered for kw in self._keywords):
            return IntakeDecision(False, "no project keyword matched")
        return IntakeDecision(True, "matched project intake heuristics")


def is_approval(content: str) -> bool:
    text = (content or "").strip().lower()
    if not text:
        return False
    for pat in APPROVAL_PATTERNS:
        if re.match(pat, text):
            return True
    return False


def is_revision_request(content: str) -> bool:
    text = (content or "").strip().lower()
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "change", "revise", "instead", "actually",
            "but ", "rework", "re-do", "redo", "fix the",
        )
    )
