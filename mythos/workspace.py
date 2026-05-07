"""Per-project filesystem workspace manager.

Layout:
  <workspaces_root>/<project_id>/
    artifacts/
    source/
    tasks/{frontend,backend,test}/
    logs/agent-runs/
    scratch/
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


WORKSPACE_SUBDIRS = (
    "artifacts",
    "source",
    "tasks/frontend",
    "tasks/backend",
    "tasks/test",
    "logs/agent-runs",
    "scratch",
)


class WorkspaceManager:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def project_root(self, project_id: str) -> Path:
        return self.root / project_id

    def create(self, project_id: str) -> Path:
        base = self.project_root(project_id)
        for sub in WORKSPACE_SUBDIRS:
            (base / sub).mkdir(parents=True, exist_ok=True)
        return base

    def artifact_path(self, project_id: str, name: str) -> Path:
        d = self.project_root(project_id) / "artifacts"
        d.mkdir(parents=True, exist_ok=True)
        return d / name

    def task_dir(self, project_id: str, role: str) -> Path:
        d = self.project_root(project_id) / "tasks" / role
        d.mkdir(parents=True, exist_ok=True)
        return d

    def log_dir(self, project_id: str) -> Path:
        d = self.project_root(project_id) / "logs" / "agent-runs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def is_inside_workspace(self, project_id: str, path: str | Path) -> bool:
        """Path-policy guard: refuse to operate on paths outside the workspace."""
        try:
            resolved = Path(path).resolve()
            base = self.project_root(project_id).resolve()
            return base in resolved.parents or resolved == base
        except (OSError, ValueError):
            return False
