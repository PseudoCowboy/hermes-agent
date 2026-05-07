"""Per-project workspace management.

Layout::

    workspaces/
      <project_id>/
        source/
        artifacts/
          design/
          reviews/
          frontend/
          backend/
          tests/
          logs/
        runs/
          <run_id>/
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class WorkspaceLayout:
    root: Path
    source: Path
    artifacts: Path
    design: Path
    reviews: Path
    frontend: Path
    backend: Path
    tests: Path
    logs: Path
    runs: Path


class WorkspaceManager:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, project_id: str) -> WorkspaceLayout:
        proj = self.root / project_id
        proj.mkdir(parents=True, exist_ok=True)
        layout = WorkspaceLayout(
            root=proj,
            source=proj / "source",
            artifacts=proj / "artifacts",
            design=proj / "artifacts" / "design",
            reviews=proj / "artifacts" / "reviews",
            frontend=proj / "artifacts" / "frontend",
            backend=proj / "artifacts" / "backend",
            tests=proj / "artifacts" / "tests",
            logs=proj / "artifacts" / "logs",
            runs=proj / "runs",
        )
        for p in (layout.source, layout.design, layout.reviews,
                  layout.frontend, layout.backend, layout.tests,
                  layout.logs, layout.runs):
            p.mkdir(parents=True, exist_ok=True)
        return layout

    def get(self, project_id: str) -> WorkspaceLayout:
        # Idempotent — re-creating dirs is safe.
        return self.create(project_id)

    def remove(self, project_id: str) -> None:
        path = self.root / project_id
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    def run_dir(self, project_id: str, run_id: str) -> Path:
        d = self.root / project_id / "runs" / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d
