"""Project slug generation and validation.

A slug is the single identifier that ties together a Discord category
name, a workspace directory, an in-process project record, and per-role
log file paths. Slugs are derived from the user's intake message text
plus a short hash for uniqueness.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Optional


_SLUG_TOKEN = re.compile(r"[a-z0-9]+")
_DEFAULT_PREFIX = "proj"
_MAX_LEN = 32


def _normalize_words(text: str, max_words: int = 4) -> list[str]:
    text = (text or "").lower()
    words = _SLUG_TOKEN.findall(text)
    # Drop very common stopwords that add no signal
    stop = {
        "a", "an", "the", "i", "want", "to", "build", "make", "for",
        "with", "and", "or", "of", "on", "in", "is", "be", "we",
        "would", "like", "create", "please",
    }
    words = [w for w in words if w not in stop and len(w) > 1]
    return words[:max_words]


def make_slug(
    intake_text: str,
    *,
    today: Optional[date] = None,
    prefix: str = _DEFAULT_PREFIX,
    seed: Optional[str] = None,
) -> str:
    """Derive a project slug from intake text.

    Format: ``<prefix>-<words>-<YYYYMMDD>-<6hex>``. Stable across calls
    when ``seed`` is provided (used in tests).
    """
    today = today or date.today()
    words = _normalize_words(intake_text) or ["project"]
    body = "-".join(words)

    salt = seed if seed is not None else f"{intake_text}|{today.isoformat()}"
    digest = hashlib.sha1(salt.encode("utf-8")).hexdigest()[:6]
    raw = f"{prefix}-{body}-{today.strftime('%Y%m%d')}-{digest}"
    # Discord channel names: lowercase, dashes, no leading/trailing dash, ≤100 chars.
    raw = re.sub(r"-+", "-", raw).strip("-")
    if len(raw) > 90:
        raw = raw[:90].strip("-")
    return raw


def is_valid_slug(slug: str) -> bool:
    if not slug or len(slug) > 100:
        return False
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9\-]{0,99}", slug))
