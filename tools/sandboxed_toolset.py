"""SandboxedToolset — path-allowlist wrapper for file/terminal tools.

Per `plans/discord-orchestration-spec/02-technical-design.md` §10, this
is **mistake resistance, not a security sandbox**. It prevents the
common accidental-escape patterns so a distracted implementer agent
can't, say, overwrite ``/etc/passwd`` by typo. It does not contain a
motivated adversarial agent that invokes ``git`` directly, writes shell
one-liners that reach out to the network, or calls kernel syscalls.

Wraps an existing :class:`tools.registry.ToolRegistry` so the sandbox
layer stays above dispatch without editing any individual tool's code:

- an allowlist of tool names the sandbox will forward,
- a *root* directory every filesystem argument must land under,
- explicit rejection of absolute paths and paths containing ``..``
  (no ``/etc/passwd`` via typed-out absolute, no ``../../escape`` via
  relative-but-traversing),
- terminal tool calls are forced to run with ``workdir=<root>`` so a
  bare ``cat /etc/passwd`` still executes but does so from inside the
  stream worktree — not the bot's checkout.

Tool arguments inspected for paths (case-sensitive, matching the
registered schemas in this repo):

- ``path``       — read_file / write_file / search_files / edit_file
- ``file_path``  — anything following the global convention
- ``workdir``    — terminal tool

See :data:`_PATH_ARG_KEYS`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Set

logger = logging.getLogger(__name__)


# Known argument keys across the repo's registered tools that carry a
# filesystem path. Keep this small and explicit; missing keys mean a
# tool can't be sandboxed and MUST be excluded from the allowlist.
_PATH_ARG_KEYS: tuple[str, ...] = ("path", "file_path", "workdir")

# Argument keys that force a working directory. These get rewritten
# to the sandbox root (instead of rejected) when absent.
_WORKDIR_ARG_KEYS: tuple[str, ...] = ("workdir",)


class SandboxViolation(ValueError):
    """Raised when an argument escapes the sandbox root."""


@dataclass
class SandboxedToolset:
    """Wrap a :class:`ToolRegistry` with a path-allowlist + tool allowlist.

    Parameters
    ----------
    root:
        Directory every filesystem argument must resolve under. Usually
        a stream worktree (see :mod:`hermes_cli.project_worktree`).
    registry:
        The underlying :class:`tools.registry.ToolRegistry` — the
        dispatch is delegated here once arguments have been validated.
    allowed_tools:
        Iterable of tool names the sandbox will forward. Calls to any
        other name raise ``SandboxViolation`` (or return a structured
        error via :meth:`dispatch`).
    force_workdir:
        If True, terminal-style tools get ``workdir`` rewritten to the
        sandbox root whenever the caller omits it (mistake resistance).
        Set False only for tests that want to observe the raw rejection.
    """

    root: Path
    registry: Any
    allowed_tools: Set[str] = field(default_factory=set)
    force_workdir: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            raise TypeError("root must be a pathlib.Path")
        self.root = self.root.resolve()
        if not self.root.is_dir():
            raise SandboxViolation(
                f"sandbox root {self.root!r} does not exist or is not a directory"
            )
        # Normalize to a frozen set so accidental mutation after
        # construction can't silently broaden the allowlist.
        self.allowed_tools = frozenset(self.allowed_tools)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_allowed(self, name: str) -> bool:
        return name in self.allowed_tools

    def validate_path(self, value: str, *, arg_name: str = "path") -> Path:
        """Resolve *value* relative to ``root`` and assert containment.

        Raises :class:`SandboxViolation` if *value* is empty, absolute,
        or resolves outside the sandbox root. Returns the resolved path
        on success (callers may rewrite the arg to this resolved form
        if they want the downstream tool to see a canonical path).
        """
        if not isinstance(value, str) or not value:
            raise SandboxViolation(
                f"{arg_name} must be a non-empty string"
            )
        raw = Path(value)
        if raw.is_absolute():
            raise SandboxViolation(
                f"{arg_name} {value!r} is absolute; sandbox rejects "
                f"all absolute paths (root={self.root})"
            )
        # Resolve strictly relative to root. ``Path.resolve()`` on
        # Python 3.11 with strict=False is enough — we only need the
        # normalized-against-root form to check containment.
        resolved = (self.root / raw).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise SandboxViolation(
                f"{arg_name} {value!r} escapes sandbox root {self.root}"
            ) from exc
        return resolved

    def validate_args(self, name: str, args: dict) -> dict:
        """Return a copy of *args* with all sandboxable path keys validated.

        Does not mutate *args*. Raises :class:`SandboxViolation` on the
        first violation. If *force_workdir* is enabled and ``workdir``
        is absent, injects the sandbox root. Unknown-name tools (not in
        the allowlist) raise :class:`SandboxViolation` before any path
        check runs.
        """
        if name not in self.allowed_tools:
            raise SandboxViolation(
                f"tool {name!r} is not in this sandbox allowlist"
            )
        if not isinstance(args, dict):
            raise SandboxViolation("args must be a dict")

        out = dict(args)
        for key in _PATH_ARG_KEYS:
            if key in out and out[key] is not None and out[key] != "":
                self.validate_path(out[key], arg_name=key)

        if self.force_workdir:
            for key in _WORKDIR_ARG_KEYS:
                if key not in out or out[key] in (None, ""):
                    out[key] = str(self.root)

        return out

    def dispatch(self, name: str, args: dict, **kwargs) -> str:
        """Validate + forward to the underlying registry.

        Returns a JSON error string on sandbox violations so the caller
        contract matches :meth:`ToolRegistry.dispatch` — a sandboxed
        tool never raises past the dispatch boundary.
        """
        try:
            safe_args = self.validate_args(name, args)
        except SandboxViolation as exc:
            logger.info("sandbox rejected %s: %s", name, exc)
            return json.dumps({
                "error": f"sandbox violation: {exc}",
                "tool": name,
            })
        return self.registry.dispatch(name, safe_args, **kwargs)


# =============================================================================
# Convenience constructor
# =============================================================================

def build_stream_sandbox(
    root: Path,
    *,
    registry: Any,
    extra_tools: Optional[Iterable[str]] = None,
) -> SandboxedToolset:
    """Construct a sandbox suitable for a per-stream implementer session.

    Allowlist defaults to the read/write/edit/search/terminal set the
    design §10 calls out (``read``/``write``/``edit``/``bash``/
    ``terminal``/``glob``/``grep`` — reconciled below against the names
    actually registered in this repo). Callers can pass extra tool
    names they want routed through the same path-allowlist.
    """
    # These names are the ones registered today (tools/file_tools.py,
    # tools/terminal_tool.py). Additional tool names should be added
    # here by callers via *extra_tools*; the sandbox refuses to forward
    # anything it doesn't explicitly whitelist.
    default_tools = {
        "read_file",
        "write_file",
        "edit_file",
        "search_files",
        "terminal",
    }
    if extra_tools:
        default_tools |= set(extra_tools)
    return SandboxedToolset(
        root=root,
        registry=registry,
        allowed_tools=default_tools,
    )
