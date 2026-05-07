"""Mythos: Discord-native multi-agent development orchestrator.

A roster of mythologically-named agents (Athena, Prometheus, Argus,
Hephaestus, Apollo, Atlas) take a user's idea typed into a Discord
channel and turn it into a reviewed spec plus a parallel, decomposed
implementation across role-specific child channels — each agent backed
by the coding CLI best suited for its role.

See SETUP.md for operator instructions.
"""

from mythos.roles import Role, RoleBinding, ROLE_BINDINGS
from mythos.config import MythosConfig, load_config
from mythos.state import ProjectState, ProjectPhase

__all__ = [
    "Role",
    "RoleBinding",
    "ROLE_BINDINGS",
    "MythosConfig",
    "load_config",
    "ProjectState",
    "ProjectPhase",
]
