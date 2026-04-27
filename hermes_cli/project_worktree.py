"""Git-worktree helpers for the Discord-orchestrated project system.

Layout per `plans/discord-orchestration-spec/02-technical-design.md` §6:

- Integration worktree : ``.worktrees/<scope_id>/<slug>/_integration/``
- Stream worktree      : ``.worktrees/<scope_id>/<slug>/<stream>/``

Branch convention:

- Integration branch : ``project/<scope_id>/<slug>/_integration``
- Stream branch      : ``project/<scope_id>/<slug>/<stream>``

All ``scope_id`` values pass through the same sanitizer used by
``tools.workflow_tools._safe_task_id_for_filename`` so that an untrusted
scope id (e.g. ``"../../etc"``) cannot escape the ``.worktrees`` root.
``slug`` and ``stream`` values are expected to already be simple
directory names (validated upstream by ``tools.workflow_tools.slugify``
and ``_validate_streams``); they are re-checked here as defense in
depth.

Creation is idempotent: a worktree that already exists is returned as-is.
Removal delegates to ``git worktree remove`` and ``git branch -D`` and
refuses to touch anything outside the ``.worktrees/`` tree of the repo.

Per design §10, this module is mistake-resistance — not a security
boundary. A determined adversarial agent that invokes ``git`` directly
can of course do anything git allows.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from tools.workflow_tools import _safe_task_id_for_filename


_DEFAULT_WORKTREES_DIR = ".worktrees"

# A stream name is a simple directory segment: no separators, no ``..``,
# no leading dot. Upstream callers normalize these via workflow_tools
# `_validate_streams`; this is a second-line check so misuse in tests
# or one-off scripts never synthesizes a traversal segment here.
_BAD_SEGMENT_CHARS = frozenset({"/", "\\", "\x00"})
_INTEGRATION_SEGMENT = "_integration"

# In-process locks keyed by (scope_id, slug). Two concurrent callers in
# the same process serialize through this lock so check-then-create
# races become check-then-create-under-lock. Cross-process safety
# additionally relies on the per-call retry below catching git's
# "cannot lock ref / already exists" errors and re-checking state.
_PROJECT_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_PROJECT_LOCKS_GUARD = threading.Lock()

# Substrings git emits when a concurrent worktree-add or branch-create
# raced us. Treat these as "the desired state may now exist; re-check".
_RETRYABLE_GIT_ERRORS = (
    "cannot lock ref",
    "already exists",
    "already checked out",
    "is already registered",
)
_MAX_RETRIES = 3
_RETRY_BACKOFF_S = 0.05


def _project_lock(scope_id: str, slug: str) -> threading.Lock:
    """Return the in-process lock for (scope_id, slug)."""
    key = (scope_id, slug)
    with _PROJECT_LOCKS_GUARD:
        lock = _PROJECT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROJECT_LOCKS[key] = lock
        return lock


def _is_retryable_git_error(exc: "WorktreeError") -> bool:
    msg = str(exc).lower()
    return any(token in msg for token in _RETRYABLE_GIT_ERRORS)


class WorktreeError(RuntimeError):
    """Raised when a worktree operation fails or is rejected."""


@dataclass(frozen=True)
class WorktreeHandles:
    """Resolved paths and branch names for one (scope_id, slug) pair."""

    scope_id: str
    slug: str
    worktree_root: Path
    integration_worktree: Path
    integration_branch: str


def _validate_segment(value: str, *, label: str) -> str:
    """Return *value* unchanged if it is a valid single path segment.

    A valid segment is non-empty, contains no path separators or NULs,
    is not ``.`` or ``..``, does not start with ``-`` (so it cannot be
    confused with a git flag), and does not start with ``.`` (hidden
    files — also reserved for ``_integration`` style sentinels handled
    explicitly).
    """
    if not isinstance(value, str) or not value:
        raise WorktreeError(f"{label} must be a non-empty string")
    if any(ch in _BAD_SEGMENT_CHARS for ch in value):
        raise WorktreeError(f"{label} {value!r} contains a path separator or NUL")
    if value in (".", ".."):
        raise WorktreeError(f"{label} must not be '.' or '..'")
    if value.startswith("-"):
        raise WorktreeError(f"{label} must not start with '-'")
    if value.startswith("."):
        raise WorktreeError(f"{label} must not start with '.'")
    return value


def _repo_root() -> Path:
    """Return the *primary* repo root regardless of caller cwd.

    Uses ``git rev-parse --git-common-dir`` (the parent of which is the
    primary repo's working tree) rather than ``--show-toplevel``.

    *Why:* ``--show-toplevel`` returns the *current* working tree's root.
    Inside a linked worktree (e.g. ``.worktrees/<scope>/<slug>/_integration``)
    that resolves to the linked tree itself, so a caller running there
    would derive ``<linked_tree>/.worktrees/<scope>/...`` — a *nested*
    `.worktrees` tree, breaking idempotency and leaking state out of the
    primary repo.  ``--git-common-dir`` always points at the shared
    `.git` directory of the primary repo, so its parent is the primary
    working tree.

    Tests run with ``monkeypatch`` chdir'd into a tmp repo so this picks
    up the test repo.
    """
    out = _run_git(["rev-parse", "--git-common-dir"], cwd=Path.cwd())
    common = Path(out.strip())
    if not common.is_absolute():
        # In the primary repo ``git`` returns ``.git``; resolve relative
        # to the cwd it was invoked under.
        common = (Path.cwd() / common).resolve()
    root = common.parent
    if not root.is_dir():
        raise WorktreeError(f"git rev-parse returned invalid repo root: {root!r}")
    return root


def _run_git(args: Iterable[str], *, cwd: Path) -> str:
    """Run ``git <args>`` under *cwd*. Raise WorktreeError on failure."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise WorktreeError("git executable not found on PATH") from exc
    except subprocess.CalledProcessError as exc:
        raise WorktreeError(
            f"git {' '.join(args)} failed (exit {exc.returncode}): "
            f"{exc.stderr.strip() or exc.stdout.strip()}"
        ) from exc
    return proc.stdout


# =============================================================================
# Path / branch templates
# =============================================================================

def _safe_scope(scope_id: str) -> str:
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise WorktreeError("scope_id must be a non-empty string")
    return _safe_task_id_for_filename(scope_id.strip())


def worktrees_root(repo_root: Optional[Path] = None) -> Path:
    """Return the repo's ``.worktrees/`` directory (does not create it)."""
    root = repo_root if repo_root is not None else _repo_root()
    return root / _DEFAULT_WORKTREES_DIR


def project_worktree_root(
    scope_id: str, slug: str, *, repo_root: Optional[Path] = None
) -> Path:
    """Return ``.worktrees/<scope_id>/<slug>/`` for this project."""
    _validate_segment(slug, label="slug")
    return worktrees_root(repo_root) / _safe_scope(scope_id) / slug


def integration_worktree_path(
    scope_id: str, slug: str, *, repo_root: Optional[Path] = None
) -> Path:
    """Return ``.worktrees/<scope_id>/<slug>/_integration/``."""
    return project_worktree_root(scope_id, slug, repo_root=repo_root) / _INTEGRATION_SEGMENT


def stream_worktree_path(
    scope_id: str, slug: str, stream: str, *, repo_root: Optional[Path] = None
) -> Path:
    """Return ``.worktrees/<scope_id>/<slug>/<stream>/`` for *stream*."""
    _validate_segment(stream, label="stream")
    if stream == _INTEGRATION_SEGMENT:
        raise WorktreeError(
            f"stream name {stream!r} collides with the reserved _integration slot"
        )
    return project_worktree_root(scope_id, slug, repo_root=repo_root) / stream


def project_branch_name(scope_id: str, slug: str) -> str:
    """Return ``project/<scope_id>/<slug>/_integration`` (integration branch).

    Uses ``_integration`` as a leaf segment so the integration branch
    is a peer of stream branches under the same ``project/<scope>/<slug>/``
    namespace.  This avoids a git ref-hierarchy conflict: git stores refs
    as filesystem paths, so ``refs/heads/.../slug`` (a file) would prevent
    creating ``refs/heads/.../slug/stream`` (needs ``slug/`` as a dir).
    """
    _validate_segment(slug, label="slug")
    return f"project/{_safe_scope(scope_id)}/{slug}/{_INTEGRATION_SEGMENT}"


def stream_branch_name(scope_id: str, slug: str, stream: str) -> str:
    """Return ``project/<scope_id>/<slug>/<stream>`` (stream branch)."""
    _validate_segment(slug, label="slug")
    _validate_segment(stream, label="stream")
    if stream == _INTEGRATION_SEGMENT:
        raise WorktreeError(
            f"stream name {stream!r} collides with the reserved _integration slot"
        )
    return f"project/{_safe_scope(scope_id)}/{slug}/{stream}"


# =============================================================================
# Creation / removal
# =============================================================================

def _branch_exists(branch: str, *, repo_root: Path) -> bool:
    try:
        _run_git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=repo_root,
        )
        return True
    except WorktreeError:
        return False


def _worktree_is_registered(path: Path, *, repo_root: Path) -> bool:
    """Return True if *path* appears in ``git worktree list --porcelain``."""
    try:
        out = _run_git(["worktree", "list", "--porcelain"], cwd=repo_root)
    except WorktreeError:
        return False
    target = str(path.resolve())
    for line in out.splitlines():
        if line.startswith("worktree "):
            existing = line.split(" ", 1)[1].strip()
            if Path(existing).resolve() == Path(target):
                return True
    return False


def _worktree_head_branch(path: Path) -> Optional[str]:
    """Return the current branch name in *path*, or None on detached/error.

    Used by the idempotent fast path to confirm an already-registered
    worktree is still on the branch we expected.  Without this check,
    a worktree whose HEAD has drifted (someone ran ``git checkout
    other-branch`` inside it) would be silently accepted.
    """
    try:
        out = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
    except WorktreeError:
        return None
    branch = out.strip()
    if not branch or branch == "HEAD":
        return None
    return branch


def ensure_integration_worktree(
    scope_id: str,
    slug: str,
    *,
    base: str = "main",
    repo_root: Optional[Path] = None,
) -> WorktreeHandles:
    """Idempotently create the per-project integration worktree.

    Creates (if needed) the branch
    ``project/<scope_id>/<slug>/_integration`` starting at *base*, then
    adds a worktree at ``.worktrees/<scope_id>/<slug>/_integration/``
    pointing at that branch. Re-entrant: a fully-materialized
    (branch + worktree + dir) setup with HEAD on the expected branch is
    left alone.

    Concurrency: in-process callers serialize on a per-(scope, slug)
    lock; cross-process races on the same git repo are absorbed by a
    bounded retry that re-checks state when git emits its
    "cannot lock ref" / "already exists" / "already checked out" /
    "is already registered" family of errors.

    Drift detection: the fast path verifies that the existing worktree's
    HEAD matches the expected branch.  A drifted worktree raises
    :class:`WorktreeError` rather than silently returning a misbound
    handle (matches the design's "no surprises" contract).
    """
    root = repo_root if repo_root is not None else _repo_root()
    branch = project_branch_name(scope_id, slug)
    wt_path = integration_worktree_path(scope_id, slug, repo_root=root)

    def _build_handles() -> WorktreeHandles:
        return WorktreeHandles(
            scope_id=scope_id,
            slug=slug,
            worktree_root=project_worktree_root(scope_id, slug, repo_root=root),
            integration_worktree=wt_path,
            integration_branch=branch,
        )

    with _project_lock(scope_id, slug):
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        last_err: Optional[WorktreeError] = None
        for attempt in range(_MAX_RETRIES):
            if wt_path.is_dir() and _worktree_is_registered(wt_path, repo_root=root):
                head = _worktree_head_branch(wt_path)
                if head == branch:
                    return _build_handles()
                if head is None:
                    raise WorktreeError(
                        f"integration worktree {wt_path!r} exists but HEAD "
                        f"is detached or unreadable; expected branch {branch!r}"
                    )
                raise WorktreeError(
                    f"integration worktree {wt_path!r} is on branch {head!r} "
                    f"but expected {branch!r}; refusing to silently rebind"
                )
            try:
                if _branch_exists(branch, repo_root=root):
                    _run_git(["worktree", "add", str(wt_path), branch], cwd=root)
                else:
                    _run_git(
                        ["worktree", "add", "-b", branch, str(wt_path), base],
                        cwd=root,
                    )
                return _build_handles()
            except WorktreeError as exc:
                if not _is_retryable_git_error(exc):
                    raise
                last_err = exc
                time.sleep(_RETRY_BACKOFF_S * (attempt + 1))
        # Exhausted retries — surface the last git error as the cause.
        raise WorktreeError(
            f"failed to create integration worktree {wt_path!r} after "
            f"{_MAX_RETRIES} retries"
        ) from last_err


def ensure_stream_worktree(
    scope_id: str,
    slug: str,
    stream: str,
    *,
    repo_root: Optional[Path] = None,
) -> Path:
    """Idempotently create a per-stream worktree off the integration branch.

    The caller must have already created the integration worktree via
    :func:`ensure_integration_worktree`; this function refuses if the
    integration branch is absent, so a stream can never be created off
    a stale base.

    Concurrency and drift handling mirror
    :func:`ensure_integration_worktree`.
    """
    root = repo_root if repo_root is not None else _repo_root()
    integration = project_branch_name(scope_id, slug)
    if not _branch_exists(integration, repo_root=root):
        raise WorktreeError(
            f"integration branch {integration!r} does not exist; "
            "call ensure_integration_worktree() first"
        )

    branch = stream_branch_name(scope_id, slug, stream)
    wt_path = stream_worktree_path(scope_id, slug, stream, repo_root=root)

    with _project_lock(scope_id, slug):
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        last_err: Optional[WorktreeError] = None
        for attempt in range(_MAX_RETRIES):
            if wt_path.is_dir() and _worktree_is_registered(wt_path, repo_root=root):
                head = _worktree_head_branch(wt_path)
                if head == branch:
                    return wt_path
                if head is None:
                    raise WorktreeError(
                        f"stream worktree {wt_path!r} exists but HEAD "
                        f"is detached or unreadable; expected branch {branch!r}"
                    )
                raise WorktreeError(
                    f"stream worktree {wt_path!r} is on branch {head!r} "
                    f"but expected {branch!r}; refusing to silently rebind"
                )
            try:
                if _branch_exists(branch, repo_root=root):
                    _run_git(["worktree", "add", str(wt_path), branch], cwd=root)
                else:
                    _run_git(
                        ["worktree", "add", "-b", branch, str(wt_path), integration],
                        cwd=root,
                    )
                return wt_path
            except WorktreeError as exc:
                if not _is_retryable_git_error(exc):
                    raise
                last_err = exc
                time.sleep(_RETRY_BACKOFF_S * (attempt + 1))
        raise WorktreeError(
            f"failed to create stream worktree {wt_path!r} after "
            f"{_MAX_RETRIES} retries"
        ) from last_err


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def remove_worktree(
    path: Path,
    *,
    delete_branch: bool = False,
    force: bool = False,
    repo_root: Optional[Path] = None,
) -> None:
    """Remove *path* via ``git worktree remove``.

    Refuses if *path* is not under ``<repo_root>/.worktrees/`` — this
    prevents a bug in an upstream caller from removing a top-level
    checkout. Passes ``--force`` to git when *force* is true.

    If *delete_branch* is set and the worktree was checked out on a
    branch named ``project/...``, that branch is deleted after the
    worktree is removed.
    """
    root = repo_root if repo_root is not None else _repo_root()
    if not _is_under(path, worktrees_root(root)):
        raise WorktreeError(
            f"refusing to remove {path!r}: not under {worktrees_root(root)!r}"
        )

    # Capture the branch BEFORE removing the worktree so we can clean
    # it up afterwards. Once the worktree is gone, the branch name may
    # still exist but we have no easy way to rederive it from path.
    branch: Optional[str] = None
    if delete_branch:
        try:
            out = _run_git(
                ["rev-parse", "--abbrev-ref", "HEAD"],
                cwd=path,
            )
            b = out.strip()
            if b.startswith("project/"):
                branch = b
        except WorktreeError:
            branch = None

    args = ["worktree", "remove", str(path)]
    if force:
        args.insert(2, "--force")
    try:
        _run_git(args, cwd=root)
    except WorktreeError:
        # A worktree that was deleted from disk but left in git's
        # metadata can usually be cleared with prune + a second remove
        # attempt; the caller can retry with force=True if needed.
        _run_git(["worktree", "prune"], cwd=root)
        if path.exists():
            raise

    if branch:
        try:
            _run_git(["branch", "-D", branch], cwd=root)
        except WorktreeError:
            # Branch deletion is best-effort; leaving the branch is
            # non-destructive so do not fail the removal for this.
            pass


# =============================================================================
# Introspection
# =============================================================================

def list_registered_worktrees(repo_root: Optional[Path] = None) -> list[Path]:
    """Return every path reported by ``git worktree list --porcelain``."""
    root = repo_root if repo_root is not None else _repo_root()
    out = _run_git(["worktree", "list", "--porcelain"], cwd=root)
    paths: list[Path] = []
    for line in out.splitlines():
        if line.startswith("worktree "):
            paths.append(Path(line.split(" ", 1)[1].strip()))
    return paths


def list_project_worktrees(
    scope_id: str, slug: str, *, repo_root: Optional[Path] = None
) -> list[Path]:
    """Return the registered worktrees that live under this project's root."""
    project_root = project_worktree_root(scope_id, slug, repo_root=repo_root)
    return [p for p in list_registered_worktrees(repo_root=repo_root)
            if _is_under(p, project_root)]
