"""Project lifecycle state.

Tracks which phase a project is in, the channel-id mapping, and the
approval round counter. The state machine is owned by the Project
Manager / Approval Mediator; this module only models the data.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from mythos.roles import Role


class ProjectPhase(str, Enum):
    INTAKE = "intake"             # Hermes just received the request
    DRAFTING = "drafting"         # Prometheus is writing the spec
    REVIEWING = "reviewing"       # Argus is reviewing
    AWAITING_USER = "awaiting_user"  # User must approve / request changes
    DECOMPOSING = "decomposing"   # Hermes is creating child channels + work items
    IMPLEMENTING = "implementing" # Specialists are running
    COMPLETE = "complete"
    ESCALATED = "escalated"       # >max approval rounds with no convergence


@dataclass
class ProjectState:
    """Per-project mutable state."""

    slug: str
    intake_text: str
    user_id: int
    created_at: float = field(default_factory=time.time)
    phase: ProjectPhase = ProjectPhase.INTAKE
    approval_round: int = 0

    # Discord channel IDs assigned by the project manager
    category_id: Optional[int] = None
    channels: Dict[str, int] = field(default_factory=dict)

    # The reverse mapping: channel id → role assigned to it
    channel_role: Dict[int, Role] = field(default_factory=dict)

    # Spec history — list of file basenames (e.g. "spec-v1.md")
    spec_versions: List[str] = field(default_factory=list)

    # Latest review verdict + comments — "approved", "changes_requested", or None
    last_review_verdict: Optional[str] = None
    last_review_text: Optional[str] = None

    # Outstanding clarifying question source channel (if any) — for tests
    pending_question_channel: Optional[int] = None

    def to_dict(self) -> Dict:
        return {
            "slug": self.slug,
            "intake_text": self.intake_text,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "phase": self.phase.value,
            "approval_round": self.approval_round,
            "category_id": self.category_id,
            "channels": dict(self.channels),
            "channel_role": {str(k): v.value for k, v in self.channel_role.items()},
            "spec_versions": list(self.spec_versions),
            "last_review_verdict": self.last_review_verdict,
            "last_review_text": self.last_review_text,
        }

    @classmethod
    def from_dict(cls, d: Dict) -> "ProjectState":
        s = cls(
            slug=d["slug"],
            intake_text=d.get("intake_text", ""),
            user_id=int(d.get("user_id", 0)),
            created_at=float(d.get("created_at", time.time())),
            phase=ProjectPhase(d.get("phase", "intake")),
            approval_round=int(d.get("approval_round", 0)),
            category_id=d.get("category_id"),
            channels={k: int(v) for k, v in (d.get("channels") or {}).items()},
            spec_versions=list(d.get("spec_versions") or []),
            last_review_verdict=d.get("last_review_verdict"),
            last_review_text=d.get("last_review_text"),
        )
        s.channel_role = {
            int(k): Role(v) for k, v in (d.get("channel_role") or {}).items()
        }
        return s
