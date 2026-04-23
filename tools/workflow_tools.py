#!/usr/bin/env python3
"""
Workflow Tools — repo-first project workflow management.

Provides deterministic tools for creating projects, approving plans,
and inspecting workflow status. All state lives in repo files under
a NanoClaw-style project layout:

    groups/shared_project/active/<project-slug>/
        control/
        coordination/
        plans/
        workstreams/
        archive/

Design:
- Repo files are the source of truth, not in-memory state.
- State files (plan-state.json, task-state.json, manifest.json) are
  always parseable JSON.
- Tools return JSON strings like all Hermes tools.
- No Discord-specific logic — pure file/state operations.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from tools.registry import registry, tool_error

try:  # pragma: no cover - Windows fallback is exercised indirectly
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


# =============================================================================
# Constants
# =============================================================================

# Base directory for active projects (relative to CWD or a configured root).
_DEFAULT_PROJECTS_ROOT = "groups/shared_project/active"

# Directories scaffolded by workflow_create_project.
_PROJECT_DIRS = ("control", "coordination", "plans", "workstreams", "archive")

# Internal workflow directories that should never be user-authored artifacts.
_WORKFLOW_INTERNAL_DIRS = frozenset({".workflow-locks", ".workflow-staging"})

# Reasonable upper bound for a human-written checkbox task title.
_MAX_CHECKBOX_TASK_TITLE_LEN = 500

# Valid plan states (ordered lifecycle).
VALID_PLAN_STATES = (
    "draft", "clarifying", "under_review", "approved", "decomposed",
)

# Valid task states (ordered lifecycle).
VALID_TASK_STATES = (
    "pending", "in_progress", "implemented", "in_review",
    "approved", "changes_requested", "blocked",
)

# Allowed plan-state transitions (from -> set of valid targets).
PLAN_TRANSITIONS = {
    "draft":        {"clarifying", "under_review", "approved"},
    "clarifying":   {"under_review", "draft"},
    "under_review": {"approved", "clarifying"},
    "approved":     {"decomposed"},
    "decomposed":   set(),  # terminal
}

# Allowed task-state transitions (from -> set of valid targets).
TASK_TRANSITIONS = {
    "pending":            {"in_progress", "blocked"},
    "in_progress":        {"implemented", "blocked", "pending"},
    "implemented":        {"in_review", "changes_requested"},
    "in_review":          {"approved", "changes_requested"},
    "approved":           set(),  # terminal
    "changes_requested":  {"in_progress"},
    "blocked":            {"in_progress", "pending"},
}

# Completion modes a workstream may declare.
VALID_COMPLETION_MODES = frozenset({"code", "report", "design", "research"})

# Valid statuses for a handoff record.
VALID_HANDOFF_STATUSES = frozenset({"pending", "ready", "accepted", "blocked"})

# Valid review verdicts that produce durable review artifacts.
VALID_REVIEW_VERDICTS = frozenset({"approved", "changes_requested"})


# =============================================================================
# Path helpers
# =============================================================================

def _projects_root() -> Path:
    """Return the absolute path to the active-projects root.

    Uses WORKFLOW_PROJECTS_ROOT env var if set, otherwise falls back to
    ``_DEFAULT_PROJECTS_ROOT`` resolved against the current working directory.
    """
    override = os.environ.get("WORKFLOW_PROJECTS_ROOT")
    if override:
        return Path(override).resolve()
    return Path.cwd() / _DEFAULT_PROJECTS_ROOT


def slugify(name: str) -> str:
    """Convert a project name to a filesystem-safe slug.

    >>> slugify("Billing Rewrite")
    'billing-rewrite'
    >>> slugify("  my--project!!  ")
    'my-project'
    """
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    # collapse repeated dashes
    s = re.sub(r"-{2,}", "-", s)
    return s


def project_path(slug: str) -> Path:
    """Return the absolute path for a given project slug."""
    return _projects_root() / slug


def _now_iso() -> str:
    """UTC ISO-8601 timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _contained_child(parent: Path, child_name: str) -> Path | None:
    """Resolve *child_name* under *parent* and verify it is a direct child.

    Returns the resolved child path if it is exactly one level below *parent*,
    or None if the name escapes (``../``), resolves to parent itself, or
    contains path separators (``foo/bar``).
    """
    if os.sep in child_name or "/" in child_name:
        return None
    resolved = (parent / child_name).resolve()
    parent_resolved = parent.resolve()
    if resolved == parent_resolved or resolved.parent != parent_resolved:
        return None
    return resolved


def _project_lock_path(slug: str) -> Path:
    """Return the advisory lock path for a project slug."""
    return _projects_root() / ".workflow-locks" / f"{slug}.lock"


def _project_staging_root(root: Path) -> Path:
    """Return the root directory for hidden workflow staging artifacts."""
    return root / ".workflow-staging"


@contextmanager
def _project_lock(slug: str):
    """Serialize mutating workflow operations for one project.

    Uses an advisory `flock` when available (macOS/Linux). On platforms without
    `fcntl`, this degrades to a no-op context manager while atomic writes still
    prevent truncate-on-crash corruption.
    """
    if not slug:
        yield
        return

    lock_path = _project_lock_path(slug)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _fsync_directory(path: Path) -> None:
    """Best-effort directory fsync for crash-safe atomic file replacement."""
    try:
        dir_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _atomic_write_text(path: Path, text: str, *, overwrite: bool) -> bool:
    """Atomically write text to `path`.

    When `overwrite` is false, the file is created only if it does not already
    exist. Returns `True` when the write succeeded and `False` when the target
    already existed in no-overwrite mode.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

        if overwrite:
            os.replace(tmp_path, path)
            _fsync_directory(path.parent)
            return True

        try:
            os.link(tmp_path, path)
        except FileExistsError:
            return False
        else:
            _fsync_directory(path.parent)
            return True
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass


def _atomic_write_many_text(files: list[tuple[Path, str]]) -> None:
    """Best-effort transactional rewrite of multiple text files.

    Stages all target contents first, then replaces each target. If a later
    replace fails, previously replaced files are rolled back to their original
    contents. This cannot make a multi-file commit crash-atomic, but it avoids
    ordinary partial updates when one write in a small batch fails.
    """
    staged_files: list[tuple[Path, Path]] = []
    originals: dict[Path, str | None] = {}
    replaced: list[Path] = []

    try:
        for path, text in files:
            path.parent.mkdir(parents=True, exist_ok=True)
            originals[path] = path.read_text(encoding="utf-8") if path.is_file() else None

            fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=str(path.parent))
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            staged_files.append((path, tmp_path))

        for path, tmp_path in staged_files:
            os.replace(tmp_path, path)
            _fsync_directory(path.parent)
            replaced.append(path)
    except OSError as exc:
        rollback_error = None
        for path in reversed(replaced):
            original = originals.get(path)
            try:
                if original is None:
                    if path.exists():
                        path.unlink()
                        _fsync_directory(path.parent)
                else:
                    _atomic_write_text(path, original, overwrite=True)
            except OSError as rollback_exc:
                rollback_error = rollback_exc
                break
        if rollback_error is not None:
            raise OSError(
                f"multi-file write failed and rollback also failed: {rollback_error}"
            ) from exc
        raise
    finally:
        for _path, tmp_path in staged_files:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass


def _cleanup_directory_tree(path: Path) -> None:
    """Best-effort recursive cleanup for hidden staging directories."""
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


# =============================================================================
# State file helpers
# =============================================================================

# Sentinel for distinguishing "file missing" from "file corrupt".
_MISSING = object()
_CORRUPT = object()


def _json_text(data) -> str:
    """Render JSON data using the canonical on-disk formatting."""
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def read_json_file(path: Path):
    """Read and parse a JSON file.

    Returns:
        Parsed data on success.
        ``_MISSING`` if the file does not exist.
        ``_CORRUPT`` if the file exists but is not valid JSON.
    """
    if not path.is_file():
        return _MISSING
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _CORRUPT


def write_json_file(path: Path, data) -> None:
    """Write *data* as pretty-printed JSON.  Creates parent dirs as needed."""
    write_text_file(path, _json_text(data))


def write_text_file(path: Path, text: str) -> None:
    """Write *text* to a file.  Creates parent dirs as needed.  No overwrite check."""
    _atomic_write_text(path, text, overwrite=True)


def safe_write_text_file(path: Path, text: str) -> bool:
    """Write *text* only if *path* does not already exist.

    Returns True if the file was written, False if it already existed.
    """
    return _atomic_write_text(path, text, overwrite=False)


def _normalize_markdown_text(text: str, default_heading: str) -> str:
    """Return normalized markdown text with a trailing newline."""
    if not isinstance(text, str):
        raise ValueError("markdown content must be a string")
    stripped = text.strip()
    if not stripped:
        stripped = f"# {default_heading}\n\nNone."
    return stripped + "\n"


_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_task_id_for_filename(value) -> str:
    """Render a task id into a safe filename component.

    Task ids come from on-disk state that may be user-authored. Prevent
    path separators, control chars, and leading dots from escaping the
    containing directory when we build artifact filenames from them.

    To avoid collisions after sanitization, truncation, or case-folding
    (e.g. on macOS's default case-insensitive filesystem), any value
    that isn't a pure decimal integer gets a short content-addressed
    suffix derived from the raw value.
    """
    raw = "" if value is None else str(value)
    sanitized = _FILENAME_SAFE_RE.sub("_", raw)
    sanitized = sanitized.lstrip(".")
    # Pure decimal integer ids (the common case) are already unique and
    # case-insensitive-safe — keep their original shape for readable
    # artifact filenames.
    if sanitized and sanitized == raw and sanitized.isdigit():
        return sanitized[:80]
    truncated = sanitized[:60] if sanitized else "task"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{truncated}-{digest}"


def _sanitize_markdown_inline(text: str) -> str:
    """Render arbitrary free text safely inside line-oriented markdown logs."""
    normalized = str(text).strip().replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.replace("\n", r"\n")
    normalized = normalized.replace("`", r"\`")
    if normalized.startswith(("#", ">", "-", "*")):
        normalized = "\\" + normalized
    return normalized


def _render_progress_entry(
    ts: str,
    note: str,
    *,
    actor: str | None = None,
    task_id=None,
    new_status: str | None = None,
) -> str:
    """Render one progress.md bullet line."""
    actor_tag = f" ({_sanitize_markdown_inline(actor)})" if actor else ""
    task_tag = (
        f" [task {_sanitize_markdown_inline(str(task_id))}"
        + (f" -> {_sanitize_markdown_inline(new_status)}" if new_status else "")
        + "]"
        if task_id is not None
        else ""
    )
    safe_note = _sanitize_markdown_inline(note)
    return f"- [{ts}]{actor_tag}{task_tag} {safe_note}\n"


def _render_handoff_entry(
    ts: str,
    from_stream: str,
    to_stream: str,
    status: str,
    description: str,
    artifact_path: str | None = None,
) -> str:
    """Render one sanitized handoff.md bullet line."""
    line = (
        f"- [{ts}] {_sanitize_markdown_inline(from_stream)} -> {_sanitize_markdown_inline(to_stream)} "
        f"[{_sanitize_markdown_inline(status)}]: "
        f"{_sanitize_markdown_inline(description)}"
    )
    if artifact_path:
        line += f" (artifact: {_sanitize_markdown_inline(artifact_path)})"
    return line + "\n"


def _append_markdown_block(path: Path, header: str, block: str) -> None:
    """Append a markdown block to a file via atomic rewrite."""
    write_text_file(path, _build_appended_markdown(path, header, block))


def _append_markdown_line(path: Path, header: str, line: str) -> None:
    """Append a single line to a markdown log file via atomic rewrite."""
    _append_markdown_block(path, header, line)


def _read_text_with_header(path: Path, header: str) -> str:
    """Return existing file content or a default markdown header."""
    return path.read_text(encoding="utf-8") if path.is_file() else header


def _build_appended_markdown(path: Path, header: str, block: str) -> str:
    """Return the full markdown file text after appending *block*."""
    return _read_text_with_header(path, header) + block


def _normalize_task_id_value(value) -> str | None:
    """Normalize a task id for comparisons.

    Workflow schemas allow `task_id` as `str|int`. We normalize both to a
    string key so tool-call coercion (`"1"` -> `1`) does not break lookups.
    Floats and bools are rejected rather than implicitly stringified.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    return None


def _task_ids_match(stored, incoming) -> bool:
    """Compare a task id from JSON storage to an incoming id from a tool call.

    Tool-call args go through a generic coercer that will turn "1" into 1 when
    a schema permits both types, so a tool call can arrive with an int when
    the stored id is a string (and vice versa). Accept either by also
    comparing string representations.
    """
    stored_key = _normalize_task_id_value(stored)
    incoming_key = _normalize_task_id_value(incoming)
    if stored_key is None or incoming_key is None:
        return False
    return stored_key == incoming_key


def _normalize_string_list(values, field_name: str) -> list[str]:
    """Validate and normalize an optional list of non-empty strings."""
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError(f"{field_name} must be a list of non-empty strings")

    cleaned = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a list of non-empty strings")
        cleaned.append(value.strip())
    return cleaned


def _normalize_task_title_key(title: str) -> str | None:
    """Normalize a task title for metadata-preserving sync matches."""
    if not isinstance(title, str):
        return None
    normalized = title.strip().casefold()
    return normalized or None


def _resolve_plan_artifact_path(plan_dir: Path) -> Path | None:
    """Return the preferred plan artifact path for a plan directory."""
    for filename in ("plan-v2.md", "plan.md"):
        candidate = plan_dir / filename
        if candidate.is_file():
            return candidate
    return None


def _load_stream_manifest_entry(ws_dir: Path, stream: str):
    """Return the manifest entry dict for *stream*, or None if unavailable."""
    manifest = read_json_file(ws_dir / "manifest.json")
    if isinstance(manifest, dict):
        maybe = manifest.get(stream)
        if isinstance(maybe, dict):
            return maybe
    return None


def _load_task_state_dict(state_path: Path, stream: str):
    """Read and validate a workstream task-state file."""
    raw = read_json_file(state_path)
    if raw is _MISSING:
        return None, tool_error(f"task-state.json missing in stream '{stream}'")
    if raw is _CORRUPT or not isinstance(raw, dict) or not isinstance(raw.get("tasks"), list):
        return None, tool_error(f"task-state.json in stream '{stream}' is not a valid task-state object")
    return raw, None


def _find_task_entry(raw_task_state: dict, task_id):
    """Find a task entry by id, accepting string/int id coercion."""
    for task in raw_task_state["tasks"]:
        if isinstance(task, dict) and _task_ids_match(task.get("id"), task_id):
            return task
    return None


def _validate_task_id_argument(task_id, *, field_name: str = "task_id") -> tuple[object | None, str | None]:
    """Validate a task id supplied to a tool handler."""
    if task_id is None:
        return None, None
    if _normalize_task_id_value(task_id) is None:
        return None, tool_error(f"{field_name} must be a non-empty string or integer")
    return task_id, None


def _validate_parsed_checkbox_tasks(parsed_tasks: list[dict[str, str]]) -> tuple[bool, str | None]:
    """Validate parsed checkbox tasks before seeding task-state.json."""
    seen_titles: set[str] = set()
    for idx, task in enumerate(parsed_tasks, start=1):
        if not isinstance(task, dict):
            return False, f"parsed task {idx} is not an object"
        title = task.get("title")
        status = task.get("status")
        if not isinstance(title, str) or not title.strip():
            return False, f"parsed task {idx} is missing a non-empty title"
        if len(title.strip()) > _MAX_CHECKBOX_TASK_TITLE_LEN:
            return False, (
                f"parsed task {idx} title exceeds {_MAX_CHECKBOX_TASK_TITLE_LEN} characters"
            )
        if not isinstance(status, str) or status not in {"pending", "approved"}:
            return False, f"parsed task {idx} has invalid status {status!r}"
        title_key = _normalize_task_title_key(title)
        if title_key in seen_titles:
            return False, f"tasks.md in stream contains duplicate checkbox task title '{title.strip()}'"
        seen_titles.add(title_key)
    return True, None


def _validate_review_gate(
    entry: dict | None,
    *,
    stream: str,
    task_id,
    new_status: str,
    actor: str | None,
) -> str | None:
    """Return a tool_error JSON string when a task approval violates stream policy."""
    completion_mode = entry.get("completionMode") if entry else None
    stream_owner = entry.get("owner") if entry else None
    stream_reviewer = entry.get("reviewer") if entry else None

    if new_status != "approved":
        return None

    if entry is None:
        return tool_error(
            f"cannot approve task {task_id}: manifest entry for stream '{stream}' is missing or unreadable"
        )
    if not isinstance(completion_mode, str) or completion_mode not in VALID_COMPLETION_MODES:
        return tool_error(
            f"cannot approve task {task_id}: manifest entry for stream '{stream}' is missing a valid completionMode"
        )
    if not isinstance(stream_owner, str) or not stream_owner:
        return tool_error(
            f"cannot approve task {task_id}: manifest entry for stream '{stream}' is missing an owner"
        )

    if completion_mode == "code":
        if not actor:
            return tool_error(
                f"approving task {task_id} on code stream '{stream}' requires an explicit actor (reviewer)"
            )
        if not isinstance(stream_reviewer, str) or not stream_reviewer:
            return tool_error(
                f"cannot approve task {task_id}: manifest entry for code stream '{stream}' is missing a reviewer"
            )
        if actor == stream_owner:
            return tool_error(
                f"code stream owner '{actor}' cannot self-approve task {task_id}; a distinct reviewer must approve"
            )
        if actor != stream_reviewer:
            return tool_error(
                f"only the designated reviewer '{stream_reviewer}' may approve task {task_id} on code stream '{stream}' (actor was '{actor}')"
            )

    return None


def _validate_review_actor(
    entry: dict | None,
    *,
    stream: str,
    task_id,
    reviewer: str,
) -> str | None:
    """Return a tool_error JSON string when a review recorder violates stream policy."""
    if entry is None:
        return tool_error(
            f"cannot review task {task_id}: manifest entry for stream '{stream}' is missing or unreadable"
        )

    completion_mode = entry.get("completionMode")
    owner = entry.get("owner")
    designated_reviewer = entry.get("reviewer")

    if not isinstance(completion_mode, str) or completion_mode not in VALID_COMPLETION_MODES:
        return tool_error(
            f"cannot review task {task_id}: manifest entry for stream '{stream}' is missing a valid completionMode"
        )
    if not isinstance(owner, str) or not owner:
        return tool_error(
            f"cannot review task {task_id}: manifest entry for stream '{stream}' is missing an owner"
        )

    if completion_mode == "code":
        if not isinstance(designated_reviewer, str) or not designated_reviewer:
            return tool_error(
                f"cannot review task {task_id}: manifest entry for code stream '{stream}' is missing a reviewer"
            )
        if reviewer == owner:
            return tool_error(
                f"code stream owner '{reviewer}' cannot review task {task_id}; a distinct reviewer must record the verdict"
            )
        if reviewer != designated_reviewer:
            return tool_error(
                f"only the designated reviewer '{designated_reviewer}' may review task {task_id} on code stream '{stream}' (reviewer was '{reviewer}')"
            )

    return None


def _apply_task_checkpoint_state(
    *,
    ws_dir: Path,
    stream: str,
    state_path: Path,
    task_id,
    new_status: str,
    actor: str | None,
    evidence: list[str],
    ts: str,
) -> tuple[dict | None, dict | None, str | None]:
    """Validate and apply a task-state transition in memory.

    Returns `(raw_state, task, None)` on success or `(None, None, tool_error_json)`
    on failure. Callers are responsible for persisting the returned state.
    """
    raw_state, state_error = _load_task_state_dict(state_path, stream)
    if state_error is not None:
        return None, None, state_error

    task = _find_task_entry(raw_state, task_id)
    if task is None:
        return None, None, tool_error(f"task_id {task_id} not found in stream '{stream}'")

    current = task.get("status", "pending")
    if not isinstance(current, str):
        return None, None, tool_error(
            f"task {task_id} in stream '{stream}' has a non-string status ({type(current).__name__}); fix task-state.json first"
        )

    allowed = TASK_TRANSITIONS.get(current, set())
    if new_status not in allowed:
        return None, None, tool_error(
            f"invalid transition {current} -> {new_status} (allowed from '{current}': {sorted(allowed) or 'none'})"
        )

    entry = _load_stream_manifest_entry(ws_dir, stream)
    review_gate_error = _validate_review_gate(
        entry,
        stream=stream,
        task_id=task_id,
        new_status=new_status,
        actor=actor,
    )
    if review_gate_error is not None:
        return None, None, review_gate_error

    task["status"] = new_status
    task["updatedAt"] = ts
    if new_status == "approved" and actor:
        raw_state["lastReviewedBy"] = actor

    existing_ev = task.get("evidence") or []
    if not isinstance(existing_ev, list):
        existing_ev = []
    for item in evidence:
        if item not in existing_ev:
            existing_ev.append(item)
    if evidence:
        task["evidence"] = existing_ev

    return raw_state, task, None


def _existing_tasks_by_title(tasks: list[object]) -> dict[str, dict]:
    """Index task entries by normalized title, skipping malformed items."""
    by_title: dict[str, dict] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue
        title_key = _normalize_task_title_key(task.get("title"))
        if title_key and title_key not in by_title:
            by_title[title_key] = task
    return by_title


def _preserve_task_review_metadata(seed_task: dict, existing_task: dict) -> bool:
    """Copy durable review metadata from an existing task onto a synced task.

    Returns ``True`` when the preserved data indicates durable review history.
    """
    preserved_review_history = False

    existing_rounds = existing_task.get("reviewRounds")
    if isinstance(existing_rounds, int) and existing_rounds >= 0:
        seed_task["reviewRounds"] = existing_rounds
        preserved_review_history = existing_rounds > 0

    existing_evidence = existing_task.get("evidence")
    if isinstance(existing_evidence, list):
        filtered_evidence = []
        for item in existing_evidence:
            if isinstance(item, str) and item not in filtered_evidence:
                filtered_evidence.append(item)
        if filtered_evidence:
            seed_task["evidence"] = filtered_evidence
            preserved_review_history = True

    existing_updated_at = existing_task.get("updatedAt")
    if isinstance(existing_updated_at, str) and existing_updated_at:
        seed_task["updatedAt"] = existing_updated_at

    return preserved_review_history


def _render_review_report_markdown(stream: str, report: dict, task: dict | None = None) -> str:
    """Render an appended markdown section for ``review-report.md``."""
    title = None
    if isinstance(task, dict):
        raw_title = task.get("title")
        if isinstance(raw_title, str) and raw_title.strip():
            title = raw_title.strip()

    lines = [
        f"## Task {_sanitize_markdown_inline(report['taskId'])} — Round {_sanitize_markdown_inline(report['round'])}",
        "",
        f"- Timestamp: {_sanitize_markdown_inline(report['timestamp'])}",
        f"- Reviewer: {_sanitize_markdown_inline(report['reviewer'])}",
        f"- Verdict: {_sanitize_markdown_inline(report['verdict'])}",
    ]
    if title:
        lines.append(f"- Title: {_sanitize_markdown_inline(title)}")
    lines.extend([
        "",
        "### Summary",
        "",
        _sanitize_markdown_inline(report["summary"]),
        "",
        "### Behavior Coverage",
        "",
    ])
    if report["behaviorCoverage"]:
        lines.extend(f"- {_sanitize_markdown_inline(item)}" for item in report["behaviorCoverage"])
    else:
        lines.append("- None recorded.")

    lines.extend([
        "",
        "### Issues",
        "",
    ])
    if report["issues"]:
        lines.extend(f"- {_sanitize_markdown_inline(item)}" for item in report["issues"])
    else:
        lines.append("- None.")

    lines.extend([
        "",
        "### Missing Evidence",
        "",
    ])
    if report["missingEvidence"]:
        lines.extend(f"- {_sanitize_markdown_inline(item)}" for item in report["missingEvidence"])
    else:
        lines.append("- None.")

    if report.get("artifacts"):
        lines.extend([
            "",
            "### Artifacts",
            "",
        ])
        lines.extend(f"- {_sanitize_markdown_inline(item)}" for item in report["artifacts"])

    lines.extend([
        "",
        f"Next action: {_sanitize_markdown_inline(report['nextAction'])}",
        "",
    ])
    return "\n".join(lines)


def _parse_checkbox_tasks_markdown(content: str) -> list[dict[str, str]]:
    """Extract checkbox tasks from ``tasks.md`` content.

    Recognizes top-level markdown checkboxes in the form ``- [ ] Task`` and
    ``- [x] Task``. Checked items map to ``approved`` so seeded task-state
    reflects work already marked complete in the human-readable artifact.
    Indented/nested checkboxes are intentionally ignored.
    """
    parsed = []
    for line in content.splitlines():
        match = re.match(r"^- \[([ xX])\]\s+(.+?)\s*$", line)
        if not match:
            continue
        title = match.group(2).strip()
        if not title:
            continue
        parsed.append({
            "title": title,
            "status": "approved" if match.group(1).lower() == "x" else "pending",
        })
    return parsed


# =============================================================================
# Tool: workflow_save_plan
# =============================================================================

def workflow_save_plan(
    project_name: str,
    plan_slug: str,
    plan_markdown: str,
    artifact: str = "plan",
    state: str | None = None,
    questions_markdown: str | None = None,
    decision_log_markdown: str | None = None,
    task_id: str = None,
) -> str:
    """Persist a plan artifact and its durable planning state.

    This is the deterministic persistence primitive for workflow-plan skills.
    It creates or updates ``plans/<plan-slug>/plan.md`` or ``plan-v2.md``,
    keeps ``plan-state.json`` parseable, and optionally writes
    ``questions.md`` / ``decision-log.md``.

    Human approval remains a separate step handled by ``workflow_approve_plan``.
    This tool therefore refuses terminal states such as ``approved`` and
    ``decomposed``.
    """
    if not project_name or not project_name.strip():
        return tool_error("project_name is required")
    if not isinstance(plan_slug, str) or not plan_slug.strip():
        return tool_error("plan_slug is required")
    if not isinstance(plan_markdown, str) or not plan_markdown.strip():
        return tool_error("plan_markdown is required")
    if artifact is not None and not isinstance(artifact, str):
        return tool_error("artifact must be a string")
    if state is not None and not isinstance(state, str):
        return tool_error("state must be a string")
    if questions_markdown is not None and not isinstance(questions_markdown, str):
        return tool_error("questions_markdown must be a string")
    if decision_log_markdown is not None and not isinstance(decision_log_markdown, str):
        return tool_error("decision_log_markdown must be a string")

    plan_slug = plan_slug.strip()

    slug = slugify(project_name)
    if not slug:
        return tool_error(f"Could not derive a valid slug from '{project_name}'")

    with _project_lock(slug):
        root = project_path(slug)
        if not root.is_dir():
            return tool_error(f"Project '{slug}' does not exist at {root}")

        plans_dir = root / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        plan_dir = _contained_child(plans_dir, plan_slug)
        if plan_dir is None:
            return tool_error(
                f"Invalid plan_slug '{plan_slug}': must be a simple directory name, not a path"
            )

        artifact_key = (artifact or "plan").strip().lower()
        artifact_filename = {
            "plan": "plan.md",
            "plan-v2": "plan-v2.md",
        }.get(artifact_key)
        if artifact_filename is None:
            return tool_error("artifact must be either 'plan' or 'plan-v2'")

        desired_state = (state or ("under_review" if artifact_key == "plan-v2" else "draft")).strip()
        if desired_state not in VALID_PLAN_STATES:
            return tool_error(
                f"invalid state {desired_state!r}. Allowed: {list(VALID_PLAN_STATES)}"
            )
        if desired_state in {"approved", "decomposed"}:
            return tool_error(
                "workflow_save_plan cannot set terminal states 'approved' or 'decomposed'; "
                "use workflow_approve_plan for human approval"
            )

        state_path = plan_dir / "plan-state.json"
        raw_state = read_json_file(state_path)
        if raw_state is _CORRUPT:
            return tool_error(
                f"plan-state.json in plans/{plan_slug}/ is corrupted JSON. Fix or remove it before saving the plan."
            )
        if raw_state is _MISSING:
            plan_state = {}
        elif isinstance(raw_state, dict):
            plan_state = raw_state
        else:
            return tool_error(
                f"plan-state.json in plans/{plan_slug}/ has unexpected shape "
                f"(expected object, got {type(raw_state).__name__}). Fix or remove it before saving the plan."
            )

        current_state = plan_state.get("state", "draft")
        if not isinstance(current_state, str):
            return tool_error(
                f"plan-state.json in plans/{plan_slug}/ has a non-string 'state' field "
                f"({type(current_state).__name__}). Fix or remove it before saving the plan."
            )
        if current_state in {"approved", "decomposed"}:
            return tool_error(
                f"plan '{plan_slug}' is already in terminal state '{current_state}'. "
                "Create a new plan slug instead of mutating an approved/decomposed plan."
            )
        if desired_state != current_state:
            allowed = PLAN_TRANSITIONS.get(current_state, set())
            if desired_state not in allowed:
                return tool_error(
                    f"Cannot change plan state from '{current_state}' to '{desired_state}'. "
                    f"Allowed transitions from '{current_state}': {sorted(allowed) or 'none'}"
                )

        plan_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = plan_dir / artifact_filename
        artifact_text = _normalize_markdown_text(plan_markdown, "Plan")
        write_text_file(artifact_path, artifact_text)

        written_files = [f"plans/{plan_slug}/{artifact_filename}"]
        if questions_markdown is not None:
            questions_path = plan_dir / "questions.md"
            write_text_file(
                questions_path,
                _normalize_markdown_text(questions_markdown, "Open Questions"),
            )
            written_files.append(f"plans/{plan_slug}/questions.md")
        if decision_log_markdown is not None:
            decision_log_path = plan_dir / "decision-log.md"
            write_text_file(
                decision_log_path,
                _normalize_markdown_text(decision_log_markdown, "Decision Log"),
            )
            written_files.append(f"plans/{plan_slug}/decision-log.md")

        now = _now_iso()
        if not isinstance(plan_state.get("createdAt"), str) or not plan_state.get("createdAt"):
            plan_state["createdAt"] = now
        plan_state["planSlug"] = plan_slug
        plan_state["state"] = desired_state
        plan_state["updatedAt"] = now
        plan_state["lastUpdatedArtifact"] = artifact_filename
        if artifact_key == "plan-v2":
            plan_state["finalizedAt"] = now
        if questions_markdown is not None:
            plan_state["questionsPath"] = f"plans/{plan_slug}/questions.md"
        if decision_log_markdown is not None:
            plan_state["decisionLogPath"] = f"plans/{plan_slug}/decision-log.md"
        write_json_file(state_path, plan_state)
        written_files.append(f"plans/{plan_slug}/plan-state.json")

        return json.dumps({
            "success": True,
            "project_slug": slug,
            "plan_slug": plan_slug,
            "artifact": artifact_filename,
            "artifact_path": str(artifact_path),
            "plan_state": plan_state,
            "files_written": written_files,
        }, ensure_ascii=False)


# =============================================================================
# Tool: workflow_create_project
# =============================================================================

def workflow_create_project(project_name: str, task_id: str = None) -> str:
    """Scaffold a new project directory with starter files.

    Creates the canonical directory structure and minimal starter files
    without overwriting any existing content.
    """
    if not project_name or not project_name.strip():
        return tool_error("project_name is required")

    slug = slugify(project_name)
    if not slug:
        return tool_error(f"Could not derive a valid slug from '{project_name}'")

    with _project_lock(slug):
        root = project_path(slug)

        # Create directory structure.
        created_dirs = []
        for d in _PROJECT_DIRS:
            dir_path = root / d
            dir_path.mkdir(parents=True, exist_ok=True)
            created_dirs.append(d)

        # Scaffold starter files (safe_write_text_file skips existing ones).
        files_written = []
        files_skipped = []

        draft_plan = (
            f"# {project_name}\n\n"
            "## Intent\n\n"
            "<!-- Describe what you want to build and why -->\n\n"
            "## Scope\n\n"
            "<!-- What is in scope -->\n\n"
            "## Non-Goals\n\n"
            "<!-- What is explicitly out of scope -->\n\n"
            "## Constraints\n\n"
            "<!-- Deadlines, tech constraints, dependencies -->\n\n"
            "## Open Questions\n\n"
            "<!-- Unresolved items -->\n"
        )
        if safe_write_text_file(root / "control" / "draft-plan.md", draft_plan):
            files_written.append("control/draft-plan.md")
        else:
            files_skipped.append("control/draft-plan.md")

        status_board = (
            "# Status Board\n\n"
            f"Project: {project_name}\n"
            f"Created: {_now_iso()}\n\n"
            "## Overview\n\n"
            "No workstreams created yet.\n"
        )
        if safe_write_text_file(root / "coordination" / "status-board.md", status_board):
            files_written.append("coordination/status-board.md")
        else:
            files_skipped.append("coordination/status-board.md")

        deps = (
            "# Dependencies\n\n"
            "No cross-stream dependencies yet.\n"
        )
        if safe_write_text_file(root / "coordination" / "dependencies.md", deps):
            files_written.append("coordination/dependencies.md")
        else:
            files_skipped.append("coordination/dependencies.md")

        integration = (
            "# Integration Points\n\n"
            "No integration points defined yet.\n"
        )
        if safe_write_text_file(root / "coordination" / "integration-points.md", integration):
            files_written.append("coordination/integration-points.md")
        else:
            files_skipped.append("coordination/integration-points.md")

        return json.dumps({
            "success": True,
            "project_slug": slug,
            "project_path": str(root),
            "directories_created": created_dirs,
            "files_written": files_written,
            "files_skipped": files_skipped,
        }, ensure_ascii=False)


# =============================================================================
# Tool: workflow_approve_plan
# =============================================================================

def workflow_approve_plan(
    project_name: str,
    plan_slug: str | None = None,
    task_id: str = None,
) -> str:
    """Approve a plan: copy the latest plan artifact into control/approved-plan.md
    and update the plan-state.json.

    Requires that a plan artifact already exists under ``plans/<plan-slug>/``.
    If *plan_slug* is omitted, auto-discovers the single plan directory.
    """
    if not project_name or not project_name.strip():
        return tool_error("project_name is required")

    slug = slugify(project_name)
    if not slug:
        return tool_error(f"Could not derive a valid slug from '{project_name}'")

    with _project_lock(slug):
        root = project_path(slug)

        if not root.is_dir():
            return tool_error(f"Project '{slug}' does not exist at {root}")

        plans_dir = root / "plans"
        if not plans_dir.is_dir():
            return tool_error("No plans/ directory found in project")

        # Resolve plan slug.
        if plan_slug:
            plan_dir = _contained_child(plans_dir, plan_slug)
            if plan_dir is None:
                return tool_error(
                    f"Invalid plan_slug '{plan_slug}': "
                    "must be a simple directory name, not a path"
                )
        else:
            # Auto-discover: look for subdirectories in plans/.
            subdirs = [d for d in plans_dir.iterdir() if d.is_dir()]
            if len(subdirs) == 0:
                return tool_error(
                    "No plan directories found under plans/. "
                    "Create a plan first (e.g. plans/<plan-slug>/plan.md)."
                )
            if len(subdirs) > 1:
                names = [d.name for d in subdirs]
                return tool_error(
                    f"Multiple plans found: {names}. "
                    "Specify plan_slug to choose one."
                )
            plan_dir = subdirs[0]
            plan_slug = plan_dir.name

        if not plan_dir.is_dir():
            return tool_error(f"Plan directory plans/{plan_slug}/ does not exist")

        # Find the best plan artifact: prefer plan-v2.md, fall back to plan.md.
        plan_file = _resolve_plan_artifact_path(plan_dir)
        if plan_file is None or not plan_file.is_file():
            return tool_error(
                f"No plan artifact (plan.md or plan-v2.md) found in plans/{plan_slug}/"
            )

        # Read current plan state if it exists.
        state_path = plan_dir / "plan-state.json"
        raw_state = read_json_file(state_path)
        if raw_state is _CORRUPT:
            return tool_error(
                f"plan-state.json in plans/{plan_slug}/ is corrupted JSON. "
                "Fix or remove the file before approving."
            )
        if raw_state is _MISSING:
            state = {}
        elif isinstance(raw_state, dict):
            state = raw_state
        else:
            return tool_error(
                f"plan-state.json in plans/{plan_slug}/ has unexpected shape "
                f"(expected object, got {type(raw_state).__name__}). "
                "Fix or remove the file before approving."
            )
        current_state = state.get("state", "draft")
        if not isinstance(current_state, str):
            return tool_error(
                f"plan-state.json in plans/{plan_slug}/ has a non-string "
                f"'state' field ({type(current_state).__name__}). "
                "Fix or remove the file before approving."
            )

        # Validate transition.
        allowed = PLAN_TRANSITIONS.get(current_state, set())
        if "approved" not in allowed:
            return tool_error(
                f"Cannot approve plan in state '{current_state}'. "
                f"Allowed transitions from '{current_state}': {sorted(allowed) or 'none'}"
            )

        # Copy plan to control/approved-plan.md and update plan-state.json
        # as one best-effort atomic batch so a mid-write failure can't leave
        # an approved-plan.md file paired with a stale (still-draft) state.
        plan_content = plan_file.read_text(encoding="utf-8")
        approved_path = root / "control" / "approved-plan.md"

        state["planSlug"] = plan_slug
        state["state"] = "approved"
        state["approvedAt"] = _now_iso()
        state["approvedFrom"] = plan_file.name

        try:
            _atomic_write_many_text([
                (approved_path, plan_content),
                (state_path, _json_text(state)),
            ])
        except OSError as exc:
            return tool_error(f"failed to approve plan: {exc}")

        return json.dumps({
            "success": True,
            "project_slug": slug,
            "plan_slug": plan_slug,
            "approved_from": plan_file.name,
            "approved_plan_path": str(approved_path),
            "plan_state": state,
        }, ensure_ascii=False)


# =============================================================================
# Tool: workflow_status
# =============================================================================

def workflow_status(project_name: str, task_id: str = None) -> str:
    """Summarise file-backed project status.

    Reads control/, plans/, workstreams/, and coordination/ to produce
    a machine-readable status snapshot.  Never reads chat history.
    """
    if not project_name or not project_name.strip():
        return tool_error("project_name is required")

    slug = slugify(project_name)
    if not slug:
        return tool_error(f"Could not derive a valid slug from '{project_name}'")

    root = project_path(slug)

    if not root.is_dir():
        return tool_error(f"Project '{slug}' does not exist at {root}")

    # --- Plan status ---
    all_plans = []
    plans_dir = root / "plans"
    if plans_dir.is_dir():
        for sub in sorted(plans_dir.iterdir()):
            if sub.is_dir():
                st = read_json_file(sub / "plan-state.json")
                if st is _CORRUPT:
                    state_value = "corrupt-state-file"
                elif st is _MISSING:
                    state_value = "no-state-file"
                elif isinstance(st, dict):
                    state_value = st.get("state", "unknown")
                else:
                    state_value = "invalid-state-shape"
                plan_info = {
                    "slug": sub.name,
                    "state": state_value,
                    "artifacts": [f.name for f in sorted(sub.iterdir()) if f.is_file()],
                }
                all_plans.append(plan_info)

    # --- Approved plan? ---
    approved_plan_exists = (root / "control" / "approved-plan.md").is_file()

    # --- Workstreams ---
    workstreams = []
    ws_dir = root / "workstreams"
    manifest = None
    manifest_error = None
    if ws_dir.is_dir():
        raw_manifest = read_json_file(ws_dir / "manifest.json")
        if raw_manifest is _CORRUPT:
            manifest_error = "corrupt-manifest"
        elif raw_manifest is _MISSING:
            pass  # manifest stays None — workstreams simply won't have owner/reviewer info
        elif isinstance(raw_manifest, dict):
            manifest = raw_manifest
        else:
            manifest_error = "invalid-manifest-shape"
        for sub in sorted(ws_dir.iterdir()):
            if sub.is_dir():
                raw_task_state = read_json_file(sub / "task-state.json")
                tasks_summary = None
                if raw_task_state is _CORRUPT:
                    tasks_summary = {"error": "corrupt-task-state-file"}
                elif raw_task_state is not _MISSING:
                    # NanoClaw shape: {"tasks": [...], "currentTask": N, "lastReviewedBy": ...}
                    # Each task entry uses "status", not "state".
                    if isinstance(raw_task_state, dict) and "tasks" in raw_task_state:
                        task_list = raw_task_state["tasks"]
                        if not isinstance(task_list, list):
                            tasks_summary = {"error": "invalid-tasks-shape"}
                        else:
                            tasks_summary = {
                                "total": len(task_list),
                                "by_status": {},
                                "currentTask": raw_task_state.get("currentTask"),
                                "lastReviewedBy": raw_task_state.get("lastReviewedBy"),
                            }
                            for t in task_list:
                                if not isinstance(t, dict):
                                    continue
                                s = t.get("status", "unknown")
                                if not isinstance(s, str):
                                    s = "invalid-status-type"
                                tasks_summary["by_status"][s] = tasks_summary["by_status"].get(s, 0) + 1
                    elif isinstance(raw_task_state, list):
                        # Legacy flat-list shape (tolerate for flexibility).
                        tasks_summary = {
                            "total": len(raw_task_state),
                            "by_status": {},
                        }
                        for t in raw_task_state:
                            if not isinstance(t, dict):
                                continue
                            s = t.get("status", t.get("state", "unknown"))
                            if not isinstance(s, str):
                                s = "invalid-status-type"
                            tasks_summary["by_status"][s] = tasks_summary["by_status"].get(s, 0) + 1

                ws_info = {
                    "stream": sub.name,
                    "has_scope": (sub / "scope.md").is_file(),
                    "has_tasks": (sub / "tasks.md").is_file(),
                    "task_state": tasks_summary,
                    "has_review_report": (sub / "review-report.md").is_file(),
                }
                # Add manifest info if available.
                if manifest and sub.name in manifest:
                    m = manifest[sub.name]
                    if isinstance(m, dict):
                        ws_info["owner"] = m.get("owner")
                        ws_info["reviewer"] = m.get("reviewer")
                        ws_info["completionMode"] = m.get("completionMode")
                workstreams.append(ws_info)

    # --- Coordination ---
    coord_dir = root / "coordination"
    coordination = {
        "has_status_board": (coord_dir / "status-board.md").is_file() if coord_dir.is_dir() else False,
        "has_dependencies": (coord_dir / "dependencies.md").is_file() if coord_dir.is_dir() else False,
        "has_integration_points": (coord_dir / "integration-points.md").is_file() if coord_dir.is_dir() else False,
    }

    result = {
        "success": True,
        "project_slug": slug,
        "project_path": str(root),
        "plans": all_plans,
        "approved_plan_exists": approved_plan_exists,
        "workstreams": workstreams,
        "coordination": coordination,
    }
    if manifest_error:
        result["manifest_error"] = manifest_error

    return json.dumps(result, ensure_ascii=False)


# =============================================================================
# Availability check
# =============================================================================

def check_workflow_requirements() -> bool:
    """Workflow tools have no external requirements — always available."""
    return True


# =============================================================================
# OpenAI Function-Calling Schemas
# =============================================================================

WORKFLOW_CREATE_PROJECT_SCHEMA = {
    "name": "workflow_create_project",
    "description": (
        "Create a new project workspace with the standard directory layout "
        "for multi-agent workflow development. Scaffolds control/, "
        "coordination/, plans/, workstreams/, and archive/ directories with "
        "starter files. Existing files are never overwritten.\n\n"
        "The project is created under groups/shared_project/active/<slug>/."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_name": {
                "type": "string",
                "description": "Human-readable project name (e.g. 'Billing Rewrite'). Will be slugified for the directory name.",
            },
        },
        "required": ["project_name"],
    },
}

WORKFLOW_SAVE_PLAN_SCHEMA = {
    "name": "workflow_save_plan",
    "description": (
        "Persist a file-backed planning artifact under plans/<plan-slug>/. "
        "Writes plan.md or plan-v2.md, keeps plan-state.json parseable, and "
        "optionally writes questions.md and decision-log.md. Use this during "
        "planning and refinement. Human approval must still happen later via "
        "workflow_approve_plan."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_name": {
                "type": "string",
                "description": "Project name or slug.",
            },
            "plan_slug": {
                "type": "string",
                "description": "Simple directory name under plans/ for this plan.",
            },
            "plan_markdown": {
                "type": "string",
                "description": "Markdown content for the plan artifact.",
            },
            "artifact": {
                "type": "string",
                "enum": ["plan", "plan-v2"],
                "description": "Which plan artifact to write. Defaults to 'plan'.",
            },
            "state": {
                "type": "string",
                "enum": ["draft", "clarifying", "under_review"],
                "description": "Planning state to record. Defaults to draft for plan.md and under_review for plan-v2.md.",
            },
            "questions_markdown": {
                "type": "string",
                "description": "Optional markdown content for plans/<plan-slug>/questions.md.",
            },
            "decision_log_markdown": {
                "type": "string",
                "description": "Optional markdown content for plans/<plan-slug>/decision-log.md.",
            },
        },
        "required": ["project_name", "plan_slug", "plan_markdown"],
    },
}

WORKFLOW_APPROVE_PLAN_SCHEMA = {
    "name": "workflow_approve_plan",
    "description": (
        "Approve a plan for a project. Copies the plan artifact "
        "(plan-v2.md or plan.md) into control/approved-plan.md and updates "
        "plan-state.json to 'approved'. An approved plan is required before "
        "workstream decomposition can proceed.\n\n"
        "The plan must already exist under plans/<plan-slug>/. "
        "If only one plan exists, plan_slug can be omitted."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_name": {
                "type": "string",
                "description": "Project name or slug.",
            },
            "plan_slug": {
                "type": "string",
                "description": "Plan directory name under plans/. Omit to auto-discover if only one plan exists.",
            },
        },
        "required": ["project_name"],
    },
}

WORKFLOW_DECOMPOSE_SCHEMA = {
    "name": "workflow_decompose",
    "description": (
        "Decompose an approved plan into workstreams. Requires "
        "control/approved-plan.md to exist and workstreams/manifest.json to "
        "NOT exist. Creates per-stream directories (scope.md, tasks.md, "
        "progress.md, handoffs.md, task-state.json) and a manifest.json. "
        "Updates the plan-state.json to 'decomposed' when the plan can be "
        "resolved unambiguously."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_name": {"type": "string"},
            "plan_slug": {
                "type": "string",
                "description": "Plan directory name under plans/. Omit to auto-discover if only one plan exists.",
            },
            "streams": {
                "type": "array",
                "description": "List of workstream objects. Each stream: name (simple directory name), owner, completionMode in {code,report,design,research}, reviewer (required and distinct from owner when completionMode='code'), acceptanceCriteria (non-empty list of strings), dependencies (list of other stream names), optional scope (markdown string).",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "owner": {"type": "string"},
                        "reviewer": {"type": ["string", "null"]},
                        "completionMode": {
                            "type": "string",
                            "enum": ["code", "report", "design", "research"],
                        },
                        "dependencies": {"type": "array", "items": {"type": "string"}},
                        "acceptanceCriteria": {"type": "array", "items": {"type": "string"}},
                        "scope": {"type": "string"},
                    },
                    "required": ["name", "owner", "completionMode", "acceptanceCriteria"],
                },
            },
        },
        "required": ["project_name", "streams"],
    },
}

WORKFLOW_HANDOFF_SCHEMA = {
    "name": "workflow_handoff",
    "description": (
        "Record a handoff between two workstreams. Appends a matching entry "
        "to both streams' handoffs.md. Both streams must already exist."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_name": {"type": "string"},
            "from_stream": {"type": "string"},
            "to_stream": {"type": "string"},
            "description": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["pending", "ready", "accepted", "blocked"],
                "description": "Defaults to 'pending'.",
            },
            "artifact_path": {
                "type": "string",
                "description": "Optional repo-relative path to an artifact.",
            },
        },
        "required": ["project_name", "from_stream", "to_stream", "description"],
    },
}

WORKFLOW_CHECKPOINT_SCHEMA = {
    "name": "workflow_checkpoint",
    "description": (
        "Record a progress checkpoint on a workstream. Always appends the "
        "note to progress.md. When task_id and new_status are both provided, "
        "validates the task-state transition, updates task-state.json, "
        "and refuses owner self-approval on code-mode streams."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_name": {"type": "string"},
            "stream": {"type": "string"},
            "note": {"type": "string"},
            "task_id": {"type": ["integer", "string"]},
            "new_status": {
                "type": "string",
                "enum": [
                    "pending", "in_progress", "implemented", "in_review",
                    "approved", "changes_requested", "blocked",
                ],
            },
            "actor": {
                "type": "string",
                "description": "Agent/role doing the checkpoint. Required for approval moves on code streams.",
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of repo-relative evidence paths to append to the task.",
            },
        },
        "required": ["project_name", "stream", "note"],
    },
}

WORKFLOW_SYNC_TASKS_SCHEMA = {
    "name": "workflow_sync_tasks",
    "description": (
        "Parse checkbox tasks from workstreams/<stream>/tasks.md into "
        "task-state.json. Uses the stream contract in workstreams/manifest.json "
        "to stamp owner/reviewer metadata and refuses to overwrite a non-empty "
        "task-state.json unless overwrite=true."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_name": {"type": "string"},
            "stream": {"type": "string"},
            "overwrite": {
                "type": "boolean",
                "description": "Set true to replace an existing non-empty task-state.json from tasks.md.",
            },
        },
        "required": ["project_name", "stream"],
    },
}

WORKFLOW_REVIEW_TASK_SCHEMA = {
    "name": "workflow_review_task",
    "description": (
        "Review a workstream task and persist durable review artifacts. "
        "Validates the review verdict, updates task-state.json, appends "
        "workstreams/<stream>/review-report.md, and writes a machine-readable "
        "JSON review report under workstreams/<stream>/evidence/."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_name": {"type": "string"},
            "stream": {"type": "string"},
            "task_id": {"type": ["integer", "string"]},
            "verdict": {
                "type": "string",
                "enum": ["approved", "changes_requested"],
            },
            "reviewer": {
                "type": "string",
                "description": "Reviewer/agent recording the review.",
            },
            "summary": {
                "type": "string",
                "description": "Short review summary used in progress.md and review-report.md.",
            },
            "issues": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific review findings. Leave empty for a clean approval.",
            },
            "behavior_coverage": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Acceptance criteria or behaviors explicitly checked during review.",
            },
            "missing_evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Evidence that should have existed but was missing during review.",
            },
            "evidence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional repo-relative evidence paths to attach to the task and review report.",
            },
        },
        "required": ["project_name", "stream", "task_id", "verdict", "reviewer", "summary"],
    },
}

WORKFLOW_STATUS_SCHEMA = {
    "name": "workflow_status",
    "description": (
        "Get the current status of a project by reading file-backed state. "
        "Reports plan state, approved-plan presence, workstream progress, "
        "and coordination file status. All data comes from repo files — "
        "never from chat history."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_name": {
                "type": "string",
                "description": "Project name or slug.",
            },
        },
        "required": ["project_name"],
    },
}


# =============================================================================
# Tool: workflow_decompose
# =============================================================================

def _validate_streams(streams):
    """Return (error_string or None, normalized_list).

    Normalizes each stream dict; rejects unknown/duplicate/unsafe shapes.
    """
    if not isinstance(streams, list) or len(streams) == 0:
        return "streams must be a non-empty list", []

    names_seen = []
    normalized = []
    for idx, raw in enumerate(streams):
        if not isinstance(raw, dict):
            return f"stream[{idx}] must be an object", []

        name = raw.get("name", "")
        if not isinstance(name, str) or not name.strip():
            return f"stream[{idx}].name is required", []
        # Reject path-like names immediately — they will also fail
        # _contained_child when we try to materialize them.
        if os.sep in name or "/" in name or name in (".", "..") or name.startswith("-"):
            return f"stream name '{name}' must be a simple directory name", []
        # Stricter slug-like check.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            return f"stream name '{name}' must be a simple directory name", []
        if name in names_seen:
            return f"duplicate stream name '{name}'", []
        names_seen.append(name)

        owner = raw.get("owner", "")
        if not isinstance(owner, str) or not owner.strip():
            return f"stream '{name}' requires a non-empty owner", []
        owner = owner.strip()

        mode = raw.get("completionMode", "")
        if not isinstance(mode, str) or mode not in VALID_COMPLETION_MODES:
            return (
                f"stream '{name}' has invalid completionMode {mode!r}. "
                f"Allowed: {sorted(VALID_COMPLETION_MODES)}"
            ), []

        reviewer = raw.get("reviewer")
        if isinstance(reviewer, str):
            reviewer = reviewer.strip() or None
        elif reviewer is not None:
            return f"stream '{name}' reviewer must be a string or null", []
        if mode == "code":
            if not reviewer:
                return f"code stream '{name}' requires a reviewer", []
            if reviewer == owner:
                return (
                    f"code stream '{name}' requires a reviewer distinct from owner"
                ), []

        criteria = raw.get("acceptanceCriteria", [])
        if not isinstance(criteria, list) or len(criteria) == 0:
            return f"stream '{name}' requires non-empty acceptanceCriteria", []
        if not all(isinstance(c, str) and c.strip() for c in criteria):
            return f"stream '{name}' acceptanceCriteria must be non-empty strings", []

        deps = raw.get("dependencies", [])
        if not isinstance(deps, list):
            return f"stream '{name}' dependencies must be a list", []
        if not all(isinstance(d, str) and d for d in deps):
            return f"stream '{name}' dependencies must be a list of non-empty strings", []

        scope = raw.get("scope")
        if scope is not None and not isinstance(scope, str):
            return f"stream '{name}' scope must be a string if provided", []

        normalized.append({
            "name": name,
            "owner": owner,
            "reviewer": reviewer if reviewer else None,
            "completionMode": mode,
            "dependencies": list(deps),
            "acceptanceCriteria": [c.strip() for c in criteria],
            "scope": scope,
        })

    # Validate dependency graph references known streams.
    known = set(names_seen)
    for s in normalized:
        for dep in s["dependencies"]:
            if dep not in known:
                return (
                    f"stream '{s['name']}' has unknown dependency '{dep}'"
                ), []

    return None, normalized


def _render_scope(stream: dict) -> str:
    """Default scope.md template for a stream."""
    bullets = "\n".join(f"- {c}" for c in stream["acceptanceCriteria"])
    reviewer = stream["reviewer"] or "(none)"
    return (
        f"# {stream['name']}\n\n"
        f"Owner: {stream['owner']}\n"
        f"Reviewer: {reviewer}\n"
        f"Completion mode: {stream['completionMode']}\n\n"
        "## Acceptance Criteria\n\n"
        f"{bullets}\n"
    )


def workflow_decompose(
    project_name: str,
    streams=None,
    plan_slug: str | None = None,
    task_id: str = None,
) -> str:
    """Decompose an approved plan into workstreams.

    Requires ``control/approved-plan.md`` to exist. Refuses to overwrite an
    existing ``workstreams/manifest.json``.
    """
    if not project_name or not project_name.strip():
        return tool_error("project_name is required")

    slug = slugify(project_name)
    if not slug:
        return tool_error(f"Could not derive a valid slug from '{project_name}'")

    with _project_lock(slug):
        root = project_path(slug)
        if not root.is_dir():
            return tool_error(f"Project '{slug}' does not exist at {root}")

        approved_plan = root / "control" / "approved-plan.md"
        if not approved_plan.is_file():
            return tool_error(
                "Decomposition requires an approved plan "
                "(control/approved-plan.md missing). Run workflow_approve_plan first."
            )

        ws_dir = root / "workstreams"
        ws_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = ws_dir / "manifest.json"
        if manifest_path.exists():
            return tool_error(
                "workstreams/manifest.json already exists — project is already decomposed"
            )
        existing_ws_entries = sorted(entry.name for entry in ws_dir.iterdir())
        if existing_ws_entries:
            return tool_error(
                "workstreams/ already contains entries "
                f"({existing_ws_entries}) but no manifest.json. "
                "Archive or remove them before re-running workflow_decompose."
            )

        err, norm = _validate_streams(streams or [])
        if err is not None:
            return tool_error(err)

        # Resolve plan_slug if provided; otherwise auto-discover a single plan.
        plans_dir = root / "plans"
        resolved_plan_dir = None
        if plan_slug:
            resolved_plan_dir = _contained_child(plans_dir, plan_slug) if plans_dir.is_dir() else None
            if resolved_plan_dir is None or not resolved_plan_dir.is_dir():
                return tool_error(f"plan_slug '{plan_slug}' not found under plans/")
        elif plans_dir.is_dir():
            subdirs = [d for d in plans_dir.iterdir() if d.is_dir()]
            if len(subdirs) == 1:
                resolved_plan_dir = subdirs[0]
                plan_slug = resolved_plan_dir.name

        # If we resolved a plan, verify its state is actually 'approved' before
        # stamping it 'decomposed'. control/approved-plan.md alone is not enough:
        # it may belong to a different plan artifact.
        approved_plan_text = approved_plan.read_text(encoding="utf-8")
        if resolved_plan_dir is not None:
            raw_plan_state = read_json_file(resolved_plan_dir / "plan-state.json")
            current_state = None
            if isinstance(raw_plan_state, dict):
                current_state = raw_plan_state.get("state")
            if current_state != "approved":
                return tool_error(
                    f"plan '{resolved_plan_dir.name}' is not in 'approved' state "
                    f"(current: {current_state!r}). Approve it first via workflow_approve_plan."
                )

            resolved_plan_artifact = _resolve_plan_artifact_path(resolved_plan_dir)
            if resolved_plan_artifact is None:
                return tool_error(
                    f"plan '{resolved_plan_dir.name}' has no plan artifact to decompose"
                )
            if approved_plan_text != resolved_plan_artifact.read_text(encoding="utf-8"):
                return tool_error(
                    f"control/approved-plan.md does not match plan '{resolved_plan_dir.name}'. "
                    "Re-run workflow_approve_plan for that plan before decomposing it."
                )

        staging_parent = _project_staging_root(root)
        staging_parent.mkdir(parents=True, exist_ok=True)
        staging_root = Path(
            tempfile.mkdtemp(
                prefix=f"decompose-{slug}-",
                dir=str(staging_parent),
            )
        )
        staged_ws_dir = staging_root / "workstreams"
        staged_ws_dir.mkdir(parents=True, exist_ok=True)

        try:
            manifest = {}
            created = []
            for s in norm:
                sdir = _contained_child(staged_ws_dir, s["name"])
                if sdir is None:
                    return tool_error(f"stream name '{s['name']}' escapes workstreams/")
                sdir.mkdir(parents=True, exist_ok=True)

                scope_text = s["scope"] if s["scope"] else _render_scope(s)
                write_text_file(sdir / "scope.md", scope_text)
                write_text_file(
                    sdir / "tasks.md",
                    f"# Tasks — {s['name']}\n\nNo tasks defined yet.\n",
                )
                write_text_file(
                    sdir / "progress.md",
                    f"# Progress — {s['name']}\n\n",
                )
                write_text_file(
                    sdir / "handoffs.md",
                    f"# Handoffs — {s['name']}\n\n",
                )
                write_json_file(
                    sdir / "task-state.json",
                    {"tasks": [], "currentTask": None, "lastReviewedBy": None},
                )

                manifest[s["name"]] = {
                    "owner": s["owner"],
                    "reviewer": s["reviewer"],
                    "completionMode": s["completionMode"],
                    "dependencies": s["dependencies"],
                    "acceptanceCriteria": s["acceptanceCriteria"],
                }
                created.append(s["name"])

            write_json_file(staged_ws_dir / "manifest.json", manifest)

            try:
                staged_ws_dir.replace(ws_dir)
            except OSError as exc:
                return tool_error(
                    f"failed to finalize decomposition into workstreams/: {exc}"
                )
        finally:
            _cleanup_directory_tree(staging_root)

        # Update plan-state.json → decomposed (if we located the plan dir).
        # The workstreams/ swap is already live; if the plan-state write
        # fails, the repo would show decomposed streams paired with an
        # 'approved' plan. Restore both to their pre-swap state on failure.
        plan_state_updated = False
        if resolved_plan_dir is not None:
            state_path = resolved_plan_dir / "plan-state.json"
            prior_state_text: str | None = (
                state_path.read_text(encoding="utf-8")
                if state_path.is_file()
                else None
            )
            raw = read_json_file(state_path)
            state = raw if isinstance(raw, dict) else {}
            state["state"] = "decomposed"
            state["decomposedAt"] = _now_iso()
            try:
                write_json_file(state_path, state)
            except Exception as exc:
                # Roll back the workstreams/ swap: remove the live tree and
                # restore an empty workstreams/ directory, then restore the
                # prior plan-state text (if any). Catch broadly so any
                # post-swap failure (OSError, TypeError from corrupted state,
                # etc.) leaves the repo in a consistent 'approved' shape.
                _cleanup_directory_tree(ws_dir)
                try:
                    ws_dir.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
                if prior_state_text is not None:
                    try:
                        write_text_file(state_path, prior_state_text)
                    except OSError:
                        pass
                return tool_error(
                    f"failed to update plan-state.json to 'decomposed'; "
                    f"rolled back workstreams/: {exc}"
                )
            plan_state_updated = True

        return json.dumps({
            "success": True,
            "project_slug": slug,
            "streams_created": created,
            "manifest_path": str(manifest_path),
            "plan_state_updated": plan_state_updated,
            "plan_slug": plan_slug,
        }, ensure_ascii=False)


# =============================================================================
# Tool: workflow_handoff
# =============================================================================

def workflow_handoff(
    project_name: str,
    from_stream: str = "",
    to_stream: str = "",
    description: str = "",
    status: str = "pending",
    artifact_path: str | None = None,
    task_id: str = None,
) -> str:
    """Record a handoff between two workstreams (writes to both streams)."""
    if not project_name or not project_name.strip():
        return tool_error("project_name is required")

    slug = slugify(project_name)
    if not slug:
        return tool_error(f"Could not derive a valid slug from '{project_name}'")

    with _project_lock(slug):
        root = project_path(slug)
        if not root.is_dir():
            return tool_error(f"Project '{slug}' does not exist at {root}")

        if not isinstance(description, str) or not description.strip():
            return tool_error("description is required")

        if not isinstance(status, str) or status not in VALID_HANDOFF_STATUSES:
            return tool_error(
                f"invalid status {status!r}. Allowed: {sorted(VALID_HANDOFF_STATUSES)}"
            )

        if from_stream == to_stream:
            return tool_error("from_stream and to_stream must differ")

        ws_dir = root / "workstreams"
        if not ws_dir.is_dir():
            return tool_error("workstreams/ does not exist — decompose first")

        from_dir = _contained_child(ws_dir, from_stream) if from_stream else None
        to_dir = _contained_child(ws_dir, to_stream) if to_stream else None
        if from_dir is None or not from_dir.is_dir():
            return tool_error(f"from_stream '{from_stream}' does not exist")
        if to_dir is None or not to_dir.is_dir():
            return tool_error(f"to_stream '{to_stream}' does not exist")

        ts = _now_iso()
        line = _render_handoff_entry(
            ts,
            from_stream=from_stream,
            to_stream=to_stream,
            status=status,
            description=description.strip(),
            artifact_path=artifact_path,
        )

        try:
            _atomic_write_many_text([
                (
                    from_dir / "handoffs.md",
                    _read_text_with_header(
                        from_dir / "handoffs.md",
                        f"# Handoffs — {from_dir.name}\n\n",
                    ) + line,
                ),
                (
                    to_dir / "handoffs.md",
                    _read_text_with_header(
                        to_dir / "handoffs.md",
                        f"# Handoffs — {to_dir.name}\n\n",
                    ) + line,
                ),
            ])
        except OSError as exc:
            return tool_error(f"failed to record handoff: {exc}")

        return json.dumps({
            "success": True,
            "project_slug": slug,
            "from_stream": from_stream,
            "to_stream": to_stream,
            "status": status,
            "recorded_at": ts,
        }, ensure_ascii=False)


# =============================================================================
# Tool: workflow_checkpoint
# =============================================================================

def workflow_checkpoint(
    project_name: str,
    stream: str = "",
    note: str = "",
    task_id=None,
    new_status: str | None = None,
    actor: str | None = None,
    evidence=None,
) -> str:
    """Record progress on a stream; optionally transition a task's status.

    On success, appends *note* to ``progress.md``. When *task_id* and
    *new_status* are both provided, validates the transition per
    ``TASK_TRANSITIONS`` and updates ``task-state.json`` (refusing owner
    self-approval on code streams).
    """
    if not project_name or not project_name.strip():
        return tool_error("project_name is required")
    if not isinstance(note, str) or not note.strip():
        return tool_error("note is required")
    if actor is not None and not isinstance(actor, str):
        return tool_error("actor must be a string")

    try:
        norm_evidence = _normalize_string_list(evidence, "evidence")
    except ValueError as exc:
        return tool_error(str(exc))

    actor = actor.strip() if isinstance(actor, str) and actor.strip() else None

    slug = slugify(project_name)
    if not slug:
        return tool_error(f"Could not derive a valid slug from '{project_name}'")

    with _project_lock(slug):
        root = project_path(slug)
        if not root.is_dir():
            return tool_error(f"Project '{slug}' does not exist at {root}")

        ws_dir = root / "workstreams"
        if not ws_dir.is_dir():
            return tool_error("workstreams/ does not exist — decompose first")

        sdir = _contained_child(ws_dir, stream) if stream else None
        if sdir is None or not sdir.is_dir():
            return tool_error(f"stream '{stream}' does not exist")

        ts = _now_iso()
        note = note.strip()
        progress_path = sdir / "progress.md"

        files_to_write: list[tuple[Path, str]] = []

        # Task state transition (only if caller asked for one).
        if task_id is not None or new_status is not None:
            if task_id is None or new_status is None:
                return tool_error("task_id and new_status must be provided together")

            task_id, task_id_error = _validate_task_id_argument(task_id)
            if task_id_error is not None:
                return task_id_error

            if not isinstance(new_status, str) or new_status not in VALID_TASK_STATES:
                return tool_error(
                    f"invalid new_status {new_status!r}. Allowed: {sorted(VALID_TASK_STATES)}"
                )

            state_path = sdir / "task-state.json"
            raw_state, _task, transition_error = _apply_task_checkpoint_state(
                ws_dir=ws_dir,
                stream=stream,
                state_path=state_path,
                task_id=task_id,
                new_status=new_status,
                actor=actor,
                evidence=norm_evidence,
                ts=ts,
            )
            if transition_error is not None:
                return transition_error

            files_to_write.append((state_path, _json_text(raw_state)))

        files_to_write.append((
            progress_path,
            _build_appended_markdown(
                progress_path,
                f"# Progress — {stream}\n\n",
                _render_progress_entry(
                    ts,
                    note,
                    actor=actor,
                    task_id=task_id,
                    new_status=new_status,
                ),
            ),
        ))

        try:
            _atomic_write_many_text(files_to_write)
        except OSError as exc:
            return tool_error(f"failed to record checkpoint: {exc}")

        return json.dumps({
            "success": True,
            "project_slug": slug,
            "stream": stream,
            "recorded_at": ts,
            "task_id": task_id,
            "new_status": new_status,
        }, ensure_ascii=False)


# =============================================================================
# Tool: workflow_sync_tasks
# =============================================================================

def workflow_sync_tasks(
    project_name: str,
    stream: str = "",
    overwrite: bool = False,
    task_id: str = None,
) -> str:
    """Seed or resync ``task-state.json`` from checkbox tasks in ``tasks.md``."""
    if not project_name or not project_name.strip():
        return tool_error("project_name is required")
    if not stream or not stream.strip():
        return tool_error("stream is required")
    if not isinstance(overwrite, bool):
        return tool_error("overwrite must be a boolean")

    slug = slugify(project_name)
    if not slug:
        return tool_error(f"Could not derive a valid slug from '{project_name}'")

    with _project_lock(slug):
        root = project_path(slug)
        if not root.is_dir():
            return tool_error(f"Project '{slug}' does not exist at {root}")

        ws_dir = root / "workstreams"
        if not ws_dir.is_dir():
            return tool_error("workstreams/ does not exist — decompose first")

        sdir = _contained_child(ws_dir, stream) if stream else None
        if sdir is None or not sdir.is_dir():
            return tool_error(f"stream '{stream}' does not exist")

        tasks_md_path = sdir / "tasks.md"
        if not tasks_md_path.is_file():
            return tool_error(f"tasks.md missing in stream '{stream}'")

        entry = _load_stream_manifest_entry(ws_dir, stream)
        if entry is None:
            return tool_error(
                f"cannot sync tasks for stream '{stream}': manifest entry is missing or unreadable"
            )

        completion_mode = entry.get("completionMode")
        owner = entry.get("owner")
        reviewer = entry.get("reviewer")
        if not isinstance(completion_mode, str) or completion_mode not in VALID_COMPLETION_MODES:
            return tool_error(
                f"cannot sync tasks for stream '{stream}': manifest entry is missing a valid completionMode"
            )
        if not isinstance(owner, str) or not owner:
            return tool_error(
                f"cannot sync tasks for stream '{stream}': manifest entry is missing an owner"
            )
        if completion_mode == "code" and (not isinstance(reviewer, str) or not reviewer):
            return tool_error(
                f"cannot sync tasks for code stream '{stream}': manifest entry is missing a reviewer"
            )

        parsed_tasks = _parse_checkbox_tasks_markdown(tasks_md_path.read_text(encoding="utf-8"))
        if not parsed_tasks:
            return tool_error(
                f"tasks.md in stream '{stream}' does not contain any checkbox tasks"
            )

        parsed_ok, parsed_error = _validate_parsed_checkbox_tasks(parsed_tasks)
        if not parsed_ok:
            return tool_error(parsed_error)

        state_path = sdir / "task-state.json"
        raw_state = read_json_file(state_path)
        if raw_state is _CORRUPT:
            return tool_error(
                f"task-state.json in stream '{stream}' is corrupted JSON. Fix or remove it before syncing tasks."
            )
        if raw_state is _MISSING:
            state = {}
            existing_tasks = []
        elif isinstance(raw_state, dict) and isinstance(raw_state.get("tasks"), list):
            state = raw_state
            existing_tasks = raw_state["tasks"]
        else:
            return tool_error(
                f"task-state.json in stream '{stream}' is not a valid task-state object"
            )

        if existing_tasks and not overwrite:
            return tool_error(
                f"task-state.json in stream '{stream}' already contains tasks; pass overwrite=true to replace it"
            )

        existing_by_title = {}  # review-metadata carryover disabled; see below
        existing_last_reviewer = None

        seeded_tasks = []
        preserved_review_history = False
        for idx, parsed in enumerate(parsed_tasks, start=1):
            seeded_task = {
                "id": idx,
                "title": parsed["title"],
                "status": parsed["status"],
                "owner": owner,
                "reviewer": reviewer if isinstance(reviewer, str) and reviewer else None,
                "dependsOn": [],
                "reviewRounds": 0,
            }

            # Conservative policy: do NOT carry review metadata across
            # overwrite. Title reuse is ambiguous — a deleted reviewed task
            # and a new task with the same title are different work items,
            # and there is no stable identity in tasks.md to tell them
            # apart. Durable review artifacts remain on disk in
            # review-report.md and evidence/*.json; task-state.json is
            # rebuilt fresh so reviewRounds / evidence / lastReviewedBy
            # cannot silently attach to the wrong task.

            seeded_tasks.append(seeded_task)

        state["tasks"] = seeded_tasks
        state["currentTask"] = next(
            (task["id"] for task in seeded_tasks if task["status"] == "pending"),
            None,
        )
        state["lastReviewedBy"] = existing_last_reviewer if preserved_review_history else None
        write_json_file(state_path, state)

        approved_count = sum(1 for task in seeded_tasks if task["status"] == "approved")
        pending_count = sum(1 for task in seeded_tasks if task["status"] == "pending")

        return json.dumps({
            "success": True,
            "project_slug": slug,
            "stream": stream,
            "tasks_synced": len(seeded_tasks),
            "approved_count": approved_count,
            "pending_count": pending_count,
            "current_task_id": state["currentTask"],
            "tasks_md_path": str(tasks_md_path),
            "task_state_path": str(state_path),
        }, ensure_ascii=False)


# =============================================================================
# Tool: workflow_review_task
# =============================================================================

def workflow_review_task(
    project_name: str,
    stream: str = "",
    task_id=None,
    verdict: str = "",
    reviewer: str = "",
    summary: str = "",
    issues=None,
    behavior_coverage=None,
    missing_evidence=None,
    evidence=None,
) -> str:
    """Persist a durable review report and update task state honestly."""
    if not project_name or not project_name.strip():
        return tool_error("project_name is required")
    if not stream or not stream.strip():
        return tool_error("stream is required")
    if task_id is None:
        return tool_error("task_id is required")
    if not isinstance(verdict, str) or verdict not in VALID_REVIEW_VERDICTS:
        return tool_error(
            f"invalid verdict {verdict!r}. Allowed: {sorted(VALID_REVIEW_VERDICTS)}"
        )
    if not isinstance(reviewer, str) or not reviewer.strip():
        return tool_error("reviewer is required")
    if not isinstance(summary, str) or not summary.strip():
        return tool_error("summary is required")

    try:
        norm_issues = _normalize_string_list(issues, "issues")
        norm_behavior_coverage = _normalize_string_list(
            behavior_coverage, "behavior_coverage"
        )
        norm_missing_evidence = _normalize_string_list(
            missing_evidence, "missing_evidence"
        )
        norm_evidence = _normalize_string_list(evidence, "evidence")
    except ValueError as exc:
        return tool_error(str(exc))

    reviewer = reviewer.strip()
    summary = summary.strip()

    task_id, task_id_error = _validate_task_id_argument(task_id)
    if task_id_error is not None:
        return task_id_error

    slug = slugify(project_name)
    if not slug:
        return tool_error(f"Could not derive a valid slug from '{project_name}'")

    with _project_lock(slug):
        root = project_path(slug)
        if not root.is_dir():
            return tool_error(f"Project '{slug}' does not exist at {root}")

        ws_dir = root / "workstreams"
        if not ws_dir.is_dir():
            return tool_error("workstreams/ does not exist — decompose first")

        sdir = _contained_child(ws_dir, stream) if stream else None
        if sdir is None or not sdir.is_dir():
            return tool_error(f"stream '{stream}' does not exist")

        state_path = sdir / "task-state.json"
        raw_state, state_error = _load_task_state_dict(state_path, stream)
        if state_error is not None:
            return state_error

        task = _find_task_entry(raw_state, task_id)
        if task is None:
            return tool_error(f"task_id {task_id} not found in stream '{stream}'")

        current = task.get("status", "pending")
        if not isinstance(current, str):
            return tool_error(
                f"task {task_id} in stream '{stream}' has a non-string "
                f"status ({type(current).__name__}); fix task-state.json first"
            )
        allowed = TASK_TRANSITIONS.get(current, set())
        if verdict not in allowed:
            return tool_error(
                f"invalid transition {current} -> {verdict} "
                f"(allowed from '{current}': {sorted(allowed) or 'none'})"
            )

        entry = _load_stream_manifest_entry(ws_dir, stream)
        actor_error = _validate_review_actor(
            entry,
            stream=stream,
            task_id=task_id,
            reviewer=reviewer,
        )
        if actor_error is not None:
            return actor_error

        review_ts = _now_iso()
        raw_state, task, transition_error = _apply_task_checkpoint_state(
            ws_dir=ws_dir,
            stream=stream,
            state_path=state_path,
            task_id=task_id,
            new_status=verdict,
            actor=reviewer,
            evidence=norm_evidence,
            ts=review_ts,
        )
        if transition_error is not None:
            return transition_error
        if task is None:
            return tool_error(f"task_id {task_id} not found in stream '{stream}' after review")

        existing_rounds = task.get("reviewRounds")
        if not isinstance(existing_rounds, int) or existing_rounds < 0:
            existing_rounds = 0
        review_round = existing_rounds + 1
        task["reviewRounds"] = review_round
        raw_state["lastReviewedBy"] = reviewer

        evidence_dir = sdir / "evidence"
        safe_tid = _safe_task_id_for_filename(task.get("id"))
        review_json_name = f"review-task-{safe_tid}-round-{review_round}.json"
        review_json_path = evidence_dir / review_json_name
        review_json_relpath = f"workstreams/{stream}/evidence/{review_json_name}"
        review_md_path = sdir / "review-report.md"

        report = {
            "taskId": task.get("id"),
            "taskTitle": task.get("title"),
            "streamType": stream,
            "reviewer": reviewer,
            "verdict": verdict,
            "round": review_round,
            "timestamp": review_ts,
            "summary": summary,
            "behaviorCoverage": norm_behavior_coverage,
            "issues": norm_issues,
            "missingEvidence": norm_missing_evidence,
            "artifacts": norm_evidence,
            "nextAction": (
                "Task approved — ready for completion or next work item"
                if verdict == "approved"
                else "Changes requested — return to implementation"
            ),
        }

        task_evidence = task.get("evidence") or []
        if not isinstance(task_evidence, list):
            task_evidence = []
        for artifact in [review_json_relpath, *norm_evidence]:
            if artifact not in task_evidence:
                task_evidence.append(artifact)
        task["evidence"] = task_evidence
        progress_path = sdir / "progress.md"

        try:
            _atomic_write_many_text([
                (review_json_path, _json_text(report)),
                (state_path, _json_text(raw_state)),
                (
                    review_md_path,
                    _build_appended_markdown(
                        review_md_path,
                        f"# Review Report — {stream}\n\n",
                        _render_review_report_markdown(stream, report, task),
                    ),
                ),
                (
                    progress_path,
                    _build_appended_markdown(
                        progress_path,
                        f"# Progress — {stream}\n\n",
                        _render_progress_entry(
                            review_ts,
                            summary,
                            actor=reviewer,
                            task_id=task.get("id"),
                            new_status=verdict,
                        ),
                    ),
                ),
            ])
        except OSError as exc:
            return tool_error(f"failed to record review: {exc}")

        return json.dumps({
            "success": True,
            "project_slug": slug,
            "stream": stream,
            "task_id": task.get("id"),
            "verdict": verdict,
            "reviewer": reviewer,
            "review_round": review_round,
            "review_report_path": str(review_md_path),
            "review_json_path": str(review_json_path),
        }, ensure_ascii=False)


# =============================================================================
# Registry
# =============================================================================

registry.register(
    name="workflow_save_plan",
    toolset="workflow-mutating",
    schema=WORKFLOW_SAVE_PLAN_SCHEMA,
    handler=lambda args, **kw: workflow_save_plan(
        project_name=args.get("project_name", ""),
        plan_slug=args.get("plan_slug", ""),
        plan_markdown=args.get("plan_markdown", ""),
        artifact=args.get("artifact", "plan"),
        state=args.get("state"),
        questions_markdown=args.get("questions_markdown"),
        decision_log_markdown=args.get("decision_log_markdown"),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_workflow_requirements,
    emoji="🗂️",
)

registry.register(
    name="workflow_create_project",
    toolset="workflow-mutating",
    schema=WORKFLOW_CREATE_PROJECT_SCHEMA,
    handler=lambda args, **kw: workflow_create_project(
        project_name=args.get("project_name", ""),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_workflow_requirements,
    emoji="📁",
)

registry.register(
    name="workflow_approve_plan",
    toolset="workflow-mutating",
    schema=WORKFLOW_APPROVE_PLAN_SCHEMA,
    handler=lambda args, **kw: workflow_approve_plan(
        project_name=args.get("project_name", ""),
        plan_slug=args.get("plan_slug"),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_workflow_requirements,
    emoji="✅",
)

registry.register(
    name="workflow_status",
    toolset="workflow-readonly",
    schema=WORKFLOW_STATUS_SCHEMA,
    handler=lambda args, **kw: workflow_status(
        project_name=args.get("project_name", ""),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_workflow_requirements,
    emoji="📊",
)

registry.register(
    name="workflow_decompose",
    toolset="workflow-mutating",
    schema=WORKFLOW_DECOMPOSE_SCHEMA,
    handler=lambda args, **kw: workflow_decompose(
        project_name=args.get("project_name", ""),
        streams=args.get("streams"),
        plan_slug=args.get("plan_slug"),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_workflow_requirements,
    emoji="🧩",
)

registry.register(
    name="workflow_handoff",
    toolset="workflow-mutating",
    schema=WORKFLOW_HANDOFF_SCHEMA,
    handler=lambda args, **kw: workflow_handoff(
        project_name=args.get("project_name", ""),
        from_stream=args.get("from_stream", ""),
        to_stream=args.get("to_stream", ""),
        description=args.get("description", ""),
        status=args.get("status", "pending"),
        artifact_path=args.get("artifact_path"),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_workflow_requirements,
    emoji="🤝",
)

registry.register(
    name="workflow_checkpoint",
    toolset="workflow-mutating",
    schema=WORKFLOW_CHECKPOINT_SCHEMA,
    handler=lambda args, **kw: workflow_checkpoint(
        project_name=args.get("project_name", ""),
        stream=args.get("stream", ""),
        note=args.get("note", ""),
        task_id=args.get("task_id"),
        new_status=args.get("new_status"),
        actor=args.get("actor"),
        evidence=args.get("evidence"),
    ),
    check_fn=check_workflow_requirements,
    emoji="📝",
)

registry.register(
    name="workflow_sync_tasks",
    toolset="workflow-mutating",
    schema=WORKFLOW_SYNC_TASKS_SCHEMA,
    handler=lambda args, **kw: workflow_sync_tasks(
        project_name=args.get("project_name", ""),
        stream=args.get("stream", ""),
        overwrite=args.get("overwrite", False),
        task_id=kw.get("task_id"),
    ),
    check_fn=check_workflow_requirements,
    emoji="☑️",
)

registry.register(
    name="workflow_review_task",
    toolset="workflow-mutating",
    schema=WORKFLOW_REVIEW_TASK_SCHEMA,
    handler=lambda args, **kw: workflow_review_task(
        project_name=args.get("project_name", ""),
        stream=args.get("stream", ""),
        task_id=args.get("task_id"),
        verdict=args.get("verdict", ""),
        reviewer=args.get("reviewer", ""),
        summary=args.get("summary", ""),
        issues=args.get("issues"),
        behavior_coverage=args.get("behavior_coverage"),
        missing_evidence=args.get("missing_evidence"),
        evidence=args.get("evidence"),
    ),
    check_fn=check_workflow_requirements,
    emoji="🔍",
)
