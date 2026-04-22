#!/usr/bin/env python3
"""NanoClaw -> Hermes migration helper.

Imports the durable, repo-first workflow artifacts that map cleanly from a
NanoClaw workspace into Hermes, converts role instructions into reference
skills, and archives NanoClaw-specific runtime metadata for manual review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


SKILL_CATEGORY_DIRNAME = "nanoclaw-imports"
SKILL_CATEGORY_DESCRIPTION = (
    "Skills imported from a NanoClaw multi-agent workspace."
)

MIGRATION_OPTION_METADATA: Dict[str, Dict[str, str]] = {
    "workspace-instructions": {
        "label": "Workspace instructions",
        "description": "Import top-level AGENTS.md and CLAUDE.md for reference.",
    },
    "workflow-docs": {
        "label": "Workflow docs",
        "description": "Import NanoClaw workflow specs and principles that map directly to Hermes repo-first workflows.",
    },
    "planning-library": {
        "label": "Planning library",
        "description": "Import reusable plan/decision/review docs and architecture references.",
    },
    "role-skills": {
        "label": "Role skills",
        "description": "Convert NanoClaw group role CLAUDE.md files into Hermes reference skills.",
    },
    "archive-runtime": {
        "label": "Archive runtime",
        "description": "Archive NanoClaw-only runtime and container metadata for manual review.",
    },
    "archive-discord-metadata": {
        "label": "Archive Discord metadata",
        "description": "Archive NanoClaw Discord command/reference docs that do not map directly to Hermes command surfaces.",
    },
}

MIGRATION_PRESETS: Dict[str, set[str]] = {
    "portable": {
        "workspace-instructions",
        "workflow-docs",
        "planning-library",
        "role-skills",
    },
    "full": set(MIGRATION_OPTION_METADATA),
}

WORKFLOW_DOC_FILES = (
    "docs/MULTI-AGENT-WORKFLOW-INSTRUCTIONS.md",
    "docs/MULTI-AGENT-PROJECT-DEVELOPMENT-SPEC.md",
    "docs/MULTI-AGENT-FAILURE-PATTERNS.md",
    "docs/AGENT-FIRST-PRINCIPLES.md",
    "docs/DISCORD-WORKFLOW-VS-PRINCIPLES.md",
)

PLANNING_LIBRARY_FILES = (
    "ARCHITECTURE.md",
    "OPTIMIZATION.md",
)

PLANNING_LIBRARY_DIRS = (
    "docs/decisions",
    "docs/plans",
    "docs/reviews",
)

RUNTIME_ARCHIVE_PATHS = (
    ".claude/settings.json",
    ".mcp.json",
    "agents/config.json",
    "container/agent-runner",
    "repo-tokens",
)

DISCORD_METADATA_ARCHIVE_PATHS = (
    "docs/discord-bot-summary.md",
    "docs/skills-as-branches.md",
)


@dataclass
class ItemResult:
    kind: str
    source: Optional[str]
    destination: Optional[str]
    status: str
    reason: str = ""
    details: Dict[str, Any] = field(default_factory=dict)


def parse_selection_values(values: Optional[Sequence[str]]) -> List[str]:
    parsed: List[str] = []
    for value in values or ():
        for part in str(value).split(","):
            part = part.strip().lower()
            if part:
                parsed.append(part)
    return parsed


def resolve_selected_options(
    include: Optional[Sequence[str]] = None,
    exclude: Optional[Sequence[str]] = None,
    preset: Optional[str] = None,
) -> set[str]:
    include_values = parse_selection_values(include)
    exclude_values = parse_selection_values(exclude)
    valid = set(MIGRATION_OPTION_METADATA)
    preset_name = (preset or "").strip().lower()

    if preset_name and preset_name not in MIGRATION_PRESETS:
        raise ValueError(
            "Unknown migration preset: "
            + preset_name
            + ". Valid presets: "
            + ", ".join(sorted(MIGRATION_PRESETS))
        )

    selected = set(MIGRATION_PRESETS[preset_name]) if preset_name else set(valid)

    unknown_includes = sorted(set(include_values) - valid)
    if unknown_includes:
        raise ValueError(
            "Unknown migration option(s): " + ", ".join(unknown_includes)
        )
    if include_values:
        selected = set(include_values)

    unknown_excludes = sorted(set(exclude_values) - valid)
    if unknown_excludes:
        raise ValueError(
            "Unknown migration option(s): " + ", ".join(unknown_excludes)
        )
    selected.difference_update(exclude_values)

    return selected


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative_label(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        return path.name


def write_report(output_dir: Path, report: Dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for item in report["items"]:
        grouped.setdefault(item["status"], []).append(item)

    lines = [
        "# NanoClaw -> Hermes Migration Summary",
        "",
        f"- Timestamp: {report['timestamp']}",
        f"- Mode: {report['mode']}",
        f"- Source: `{report['source_root']}`",
        f"- Target: `{report['target_root']}`",
        f"- Preset: `{report.get('preset') or 'custom'}`",
        "",
        "## Summary",
        "",
    ]
    for key, value in report["summary"].items():
        lines.append(f"- {key}: {value}")

    lines.extend(["", "## Items Needing Manual Review", ""])
    pending = grouped.get("skipped", []) + grouped.get("conflict", []) + grouped.get("error", [])
    if not pending:
        lines.append("- Nothing. All discovered items were either migrated or archived.")
    else:
        for item in pending:
            source = item["source"] or "(n/a)"
            destination = item["destination"] or "(n/a)"
            reason = item["reason"] or item["status"]
            lines.append(f"- `{source}` -> `{destination}`: {reason}")

    (output_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _group_display_name(group_name: str) -> str:
    name = group_name
    if name.startswith("dc_"):
        name = name[3:]
    return name.replace("_", " ").replace("-", " ").title()


def _group_skill_slug(group_name: str) -> str:
    name = group_name
    if name.startswith("dc_"):
        name = name[3:]
    name = name.lower().replace("_", "-")
    name = re.sub(r"[^a-z0-9-]+", "-", name).strip("-")
    return f"nanoclaw-role-{name or 'group'}"


def _render_role_skill(group_name: str, source_rel: str, body: str) -> str:
    display_name = _group_display_name(group_name)
    skill_name = _group_skill_slug(group_name)
    content = body.strip()
    return f"""---
name: {skill_name}
description: Imported NanoClaw role instructions for {display_name}.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [Migration, NanoClaw, Workflow, Role]
    related_skills: [workflow-plan, workflow-decompose, workflow-review-stream]
---

# Imported NanoClaw Role: {display_name}

This skill preserves the original NanoClaw role instructions from `{source_rel}` for migration and reference.

## Original Instructions

{content}
"""


class Migrator:
    def __init__(
        self,
        source_root: Path,
        target_root: Path,
        execute: bool,
        overwrite: bool,
        output_dir: Optional[Path],
        selected_options: Optional[set[str]] = None,
        preset_name: str = "",
    ):
        self.source_root = source_root
        self.target_root = target_root
        self.execute = execute
        self.overwrite = overwrite
        self.selected_options = set(selected_options or MIGRATION_OPTION_METADATA.keys())
        self.preset_name = preset_name.strip().lower()
        self.timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        self.output_dir = output_dir or (
            target_root / "migration" / "nanoclaw" / self.timestamp if execute else None
        )
        self.archive_dir = self.output_dir / "archive" if self.output_dir else None
        self.backup_dir = self.output_dir / "backups" if self.output_dir else None
        self.items: List[ItemResult] = []

    def is_selected(self, option_id: str) -> bool:
        return option_id in self.selected_options

    def record(
        self,
        kind: str,
        source: Optional[Path | str],
        destination: Optional[Path | str],
        status: str,
        reason: str = "",
        **details: Any,
    ) -> None:
        self.items.append(
            ItemResult(
                kind=kind,
                source=str(source) if source else None,
                destination=str(destination) if destination else None,
                status=status,
                reason=reason,
                details=details,
            )
        )

    def run_if_selected(self, option_id: str, fn) -> None:
        if self.is_selected(option_id):
            fn()
        else:
            self.record(option_id, None, None, "skipped", "Not selected for this run")

    def maybe_backup(self, destination: Path) -> Optional[Path]:
        if not self.execute or not destination.exists() or self.backup_dir is None:
            return None
        backup_path = self.backup_dir / relative_label(destination, self.target_root)
        ensure_parent(backup_path)
        if destination.is_dir():
            shutil.copytree(destination, backup_path, dirs_exist_ok=True)
        else:
            shutil.copy2(destination, backup_path)
        return backup_path

    def copy_file(self, source: Path, destination: Path, kind: str) -> None:
        if not source.exists():
            self.record(kind, source, destination, "skipped", "Source file not found")
            return
        if destination.exists():
            if destination.is_file() and sha256_file(source) == sha256_file(destination):
                self.record(kind, source, destination, "skipped", "Target already matches source")
                return
            if not self.overwrite:
                self.record(kind, source, destination, "conflict", "Target exists and overwrite is disabled")
                return

        if self.execute:
            backup_path = self.maybe_backup(destination)
            ensure_parent(destination)
            shutil.copy2(source, destination)
            self.record(kind, source, destination, "migrated", backup=str(backup_path) if backup_path else "")
        else:
            self.record(kind, source, destination, "migrated", "Would copy")

    def copy_tree_non_destructive(self, source_root: Path, destination_root: Path, kind: str) -> None:
        if not source_root.exists():
            self.record(kind, source_root, destination_root, "skipped", "Source directory not found")
            return

        files = [path for path in source_root.rglob("*") if path.is_file()]
        if not files:
            self.record(kind, source_root, destination_root, "skipped", "No files found")
            return

        copied = 0
        unchanged = 0
        conflicts = 0
        for source in files:
            rel = source.relative_to(source_root)
            destination = destination_root / rel
            if destination.exists():
                if destination.is_file() and sha256_file(source) == sha256_file(destination):
                    unchanged += 1
                    continue
                if not self.overwrite:
                    conflicts += 1
                    continue
            if self.execute:
                self.maybe_backup(destination)
                ensure_parent(destination)
                shutil.copy2(source, destination)
            copied += 1

        if copied == 0 and conflicts > 0:
            self.record(
                kind,
                source_root,
                destination_root,
                "conflict",
                "All candidate files conflicted with existing destination files",
                copied_files=copied,
                unchanged_files=unchanged,
                conflicts=conflicts,
            )
            return

        status = "migrated" if copied > 0 else "skipped"
        reason = "" if copied > 0 else "No new files to copy"
        if not self.execute and copied > 0:
            reason = "Would copy directory tree"
        self.record(
            kind,
            source_root,
            destination_root,
            status,
            reason,
            copied_files=copied,
            unchanged_files=unchanged,
            conflicts=conflicts,
        )

    def archive_path(self, source: Path, kind: str, reason: str) -> None:
        if not source.exists():
            self.record(kind, source, None, "skipped", "Source path not found")
            return
        destination = self.archive_dir / relative_label(source, self.source_root) if self.archive_dir else None
        if self.execute and destination is not None:
            ensure_parent(destination)
            if source.is_dir():
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(source, destination)
        self.record(kind, source, destination, "archived", reason)

    def migrate(self) -> Dict[str, Any]:
        if not self.source_root.exists():
            self.record("source", self.source_root, None, "error", "NanoClaw directory does not exist")
            return self.build_report()

        self.run_if_selected("workspace-instructions", self.migrate_workspace_instructions)
        self.run_if_selected("workflow-docs", self.migrate_workflow_docs)
        self.run_if_selected("planning-library", self.migrate_planning_library)
        self.run_if_selected("role-skills", self.migrate_role_skills)
        self.run_if_selected("archive-runtime", self.archive_runtime)
        self.run_if_selected("archive-discord-metadata", self.archive_discord_metadata)

        report = self.build_report()
        if self.output_dir:
            write_report(self.output_dir, report)
        return report

    def build_report(self) -> Dict[str, Any]:
        counts = {"migrated": 0, "archived": 0, "skipped": 0, "conflict": 0, "error": 0}
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        counts["total"] = len(self.items)
        return {
            "timestamp": self.timestamp,
            "mode": "execute" if self.execute else "dry-run",
            "source_root": str(self.source_root),
            "target_root": str(self.target_root),
            "output_dir": str(self.output_dir) if self.output_dir else None,
            "preset": self.preset_name or None,
            "selection": {
                "preset": self.preset_name or None,
                "selected": sorted(self.selected_options),
            },
            "summary": counts,
            "items": [asdict(item) for item in self.items],
        }

    def migrate_workspace_instructions(self) -> None:
        destination_root = self.target_root / "imports" / "nanoclaw" / "workspace"
        found_any = False
        for rel in ("AGENTS.md", "CLAUDE.md"):
            source = self.source_root / rel
            if source.exists():
                found_any = True
            self.copy_file(source, destination_root / rel, "workspace-instructions")
        if not found_any:
            self.record(
                "workspace-instructions",
                self.source_root,
                destination_root,
                "skipped",
                "No top-level AGENTS.md or CLAUDE.md found",
            )

    def migrate_workflow_docs(self) -> None:
        destination_root = self.target_root / "imports" / "nanoclaw" / "docs" / "workflow"
        found_any = False
        for rel in WORKFLOW_DOC_FILES:
            source = self.source_root / rel
            if source.exists():
                found_any = True
            self.copy_file(source, destination_root / Path(rel).name, "workflow-docs")
        if not found_any:
            self.record(
                "workflow-docs",
                self.source_root / "docs",
                destination_root,
                "skipped",
                "No workflow documentation files found",
            )

    def migrate_planning_library(self) -> None:
        found_any = False
        refs_root = self.target_root / "imports" / "nanoclaw" / "references"
        docs_root = self.target_root / "imports" / "nanoclaw" / "docs"

        for rel in PLANNING_LIBRARY_FILES:
            source = self.source_root / rel
            if source.exists():
                found_any = True
            self.copy_file(source, refs_root / Path(rel).name, "planning-library")

        for rel in PLANNING_LIBRARY_DIRS:
            source = self.source_root / rel
            if source.exists():
                found_any = True
            self.copy_tree_non_destructive(source, docs_root / Path(rel).name, "planning-library")

        if not found_any:
            self.record(
                "planning-library",
                self.source_root / "docs",
                docs_root,
                "skipped",
                "No planning-library files found",
            )

    def migrate_role_skills(self) -> None:
        groups_root = self.source_root / "groups"
        destination_root = self.target_root / "skills" / SKILL_CATEGORY_DIRNAME
        role_files = sorted(groups_root.glob("*/CLAUDE.md")) if groups_root.is_dir() else []
        if not role_files:
            self.record("role-skills", groups_root, destination_root, "skipped", "No group role CLAUDE.md files found")
            return

        for role_file in role_files:
            group_name = role_file.parent.name
            skill_slug = _group_skill_slug(group_name)
            destination = destination_root / skill_slug / "SKILL.md"
            body = role_file.read_text(encoding="utf-8")
            rendered = _render_role_skill(group_name, relative_label(role_file, self.source_root), body)

            if destination.exists() and not self.overwrite:
                self.record("role-skills", role_file, destination, "conflict", "Destination skill already exists")
                continue

            if self.execute:
                backup_path = self.maybe_backup(destination.parent)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(rendered, encoding="utf-8")
                self.record(
                    "role-skills",
                    role_file,
                    destination,
                    "migrated",
                    backup=str(backup_path) if backup_path else "",
                    group=group_name,
                )
            else:
                self.record(
                    "role-skills",
                    role_file,
                    destination,
                    "migrated",
                    "Would convert role instructions into a Hermes skill",
                    group=group_name,
                )

        description_path = destination_root / "DESCRIPTION.md"
        if self.execute:
            description_path.parent.mkdir(parents=True, exist_ok=True)
            if not description_path.exists():
                description_path.write_text(SKILL_CATEGORY_DESCRIPTION + "\n", encoding="utf-8")

    def archive_runtime(self) -> None:
        found_any = False
        for rel in RUNTIME_ARCHIVE_PATHS:
            source = self.source_root / rel
            if source.exists():
                found_any = True
            self.archive_path(source, "archive-runtime", "NanoClaw runtime/container metadata archived for manual review")
        if not found_any:
            self.record("archive-runtime", self.source_root, self.archive_dir, "skipped", "No NanoClaw runtime metadata found")

    def archive_discord_metadata(self) -> None:
        found_any = False
        for rel in DISCORD_METADATA_ARCHIVE_PATHS:
            source = self.source_root / rel
            if source.exists():
                found_any = True
            self.archive_path(source, "archive-discord-metadata", "NanoClaw Discord-specific command/reference metadata archived")
        if not found_any:
            self.record(
                "archive-discord-metadata",
                self.source_root / "docs",
                self.archive_dir,
                "skipped",
                "No Discord-specific metadata docs found",
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate NanoClaw workflow artifacts into Hermes Agent.")
    parser.add_argument(
        "--source",
        default=str(Path.home() / "code" / "analysis" / "harness" / "nanoclaw"),
        help="Path to the NanoClaw repository (default: ~/code/analysis/harness/nanoclaw)",
    )
    parser.add_argument(
        "--target",
        default=os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")),
        help="Path to Hermes home (default: HERMES_HOME or ~/.hermes)",
    )
    parser.add_argument(
        "--preset",
        default="portable",
        choices=sorted(MIGRATION_PRESETS),
        help="Migration preset to use (default: portable)",
    )
    parser.add_argument("--include", action="append", help="Comma-separated option ids to include")
    parser.add_argument("--exclude", action="append", help="Comma-separated option ids to exclude")
    parser.add_argument("--execute", action="store_true", help="Apply changes. Without this flag, the script runs in dry-run mode.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite conflicting destination files instead of recording conflicts")
    parser.add_argument("--output-dir", help="Custom directory for report output, archives, and backups")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        selected = resolve_selected_options(
            include=args.include,
            exclude=args.exclude,
            preset=args.preset,
        )
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2

    migrator = Migrator(
        source_root=Path(args.source).expanduser(),
        target_root=Path(args.target).expanduser(),
        execute=bool(args.execute),
        overwrite=bool(args.overwrite),
        output_dir=Path(args.output_dir).expanduser() if args.output_dir else None,
        selected_options=selected,
        preset_name=args.preset,
    )
    report = migrator.migrate()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["summary"].get("error", 0) == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
