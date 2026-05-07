"""Agent registry: ties role -> AgentSpec -> Runner -> system prompt."""

from .registry import AgentRegistry, AgentInvocation, InvocationResult
from .prompts import SYSTEM_PROMPTS

__all__ = [
    "AgentRegistry",
    "AgentInvocation",
    "InvocationResult",
    "SYSTEM_PROMPTS",
]
