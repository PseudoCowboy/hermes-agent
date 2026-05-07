"""Agent <-> orchestrator wire format.

We don't have JSON-RPC with the CLI agents, just plaintext output. The
orchestrator parses a tiny set of inline markers from the agent's stdout to
classify the response. Agents are instructed (via prompts) to use these
markers; if absent, the orchestrator falls back to "treat the whole reply as
the message".

Markers (one per line, anywhere in the output):

- ``<<MYTHOS:STATUS:received>>``       — agent acknowledges work
- ``<<MYTHOS:STATUS:working>>``        — agent is mid-task (rare; one-shot CLI)
- ``<<MYTHOS:STATUS:blocked>>``        — agent is blocked, expects a question
- ``<<MYTHOS:STATUS:question>>``       — agent has a clarifying question
- ``<<MYTHOS:STATUS:finished>>``       — agent finished; main payload above
- ``<<MYTHOS:STATUS:failed>>``         — agent failed
- ``<<MYTHOS:DECISION:approve>>``      — review agent recommends approval
- ``<<MYTHOS:DECISION:request_changes>>``  — review agent requests changes
- ``<<MYTHOS:DECISION:block>>``        — review agent blocks (severe)

For the draft plan agent, a ``<<MYTHOS:DESIGN_BEGIN>>` ... ``<<MYTHOS:DESIGN_END>>``
block fences the actual design spec body so the orchestrator can extract it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional


STATUS_RECEIVED = "received"
STATUS_WORKING = "working"
STATUS_BLOCKED = "blocked"
STATUS_QUESTION = "question"
STATUS_FINISHED = "finished"
STATUS_FAILED = "failed"

DECISION_APPROVE = "approve"
DECISION_REQUEST_CHANGES = "request_changes"
DECISION_BLOCK = "block"

_STATUS_RE = re.compile(r"<<MYTHOS:STATUS:([a-z_]+)>>")
_DECISION_RE = re.compile(r"<<MYTHOS:DECISION:([a-z_]+)>>")
_DESIGN_RE = re.compile(
    r"<<MYTHOS:DESIGN_BEGIN>>(?P<body>.*?)<<MYTHOS:DESIGN_END>>",
    re.DOTALL,
)


@dataclass
class AgentReply:
    raw: str
    statuses: List[str]
    decision: Optional[str]
    design_body: Optional[str]
    visible_text: str

    @property
    def finished(self) -> bool:
        return STATUS_FINISHED in self.statuses

    @property
    def failed(self) -> bool:
        return STATUS_FAILED in self.statuses

    @property
    def asks_question(self) -> bool:
        return STATUS_QUESTION in self.statuses or STATUS_BLOCKED in self.statuses


def _strip_markers(text: str) -> str:
    text = _STATUS_RE.sub("", text)
    text = _DECISION_RE.sub("", text)
    text = _DESIGN_RE.sub("", text)
    return text.strip()


def parse_agent_output(raw: str) -> AgentReply:
    statuses = _STATUS_RE.findall(raw)
    decision_match = _DECISION_RE.search(raw)
    design_match = _DESIGN_RE.search(raw)
    design_body = design_match.group("body").strip() if design_match else None
    visible = _strip_markers(raw)
    if not statuses and not decision_match:
        # Treat as a finished response by default — many CLIs won't use markers.
        statuses = [STATUS_FINISHED]
    return AgentReply(
        raw=raw,
        statuses=statuses,
        decision=decision_match.group(1) if decision_match else None,
        design_body=design_body,
        visible_text=visible or raw.strip(),
    )
