"""Workspace allocator for Mythos projects.

Each project gets a directory under ``workspace_root/<project_slug>__<id>/``
with three subdirs: ``frontend/``, ``backend/``, ``test/``. Specialist agents
are passed only their subdir's path so frontend cannot scribble in backend's
working directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict


WORKSPACE_SUBDIRS = ("frontend", "backend", "test", "docs", "shared")


class WorkspaceManager:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def project_root(self, project_slug: str, project_id: str) -> Path:
        # Slug + id keeps directories human-readable and unique.
        safe = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in project_slug.lower())
        return self.root / f"{safe}__{project_id}"

    def allocate(self, project_slug: str, project_id: str) -> Path:
        root = self.project_root(project_slug, project_id)
        root.mkdir(parents=True, exist_ok=True)
        for sub in WORKSPACE_SUBDIRS:
            (root / sub).mkdir(exist_ok=True)
        return root

    def subdir(self, project_root: Path, kind: str) -> Path:
        if kind not in WORKSPACE_SUBDIRS:
            raise ValueError(f"unknown workspace subdir: {kind}")
        return project_root / kind

    def workspaces_for_project(self, project_root: Path) -> Dict[str, Path]:
        return {kind: project_root / kind for kind in WORKSPACE_SUBDIRS}
