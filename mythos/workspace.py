"""Per-project workspace layout on disk.

Each project gets ``<workspace_root>/<slug>/`` with subdirs:

  * ``frontend/``   – Apollo's working tree
  * ``backend/``    – Atlas's working tree
  * ``tests/``      – Hephaestus's working tree
  * ``spec/``       – Prometheus drafts go here; Argus reads them
  * ``logs/``       – per-role .log files
  * ``state.json``  – serialized project state (for crash recovery)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from mythos.roles import Role


@dataclass
class Workspace:
    """Filesystem handles for a single project's working tree."""

    slug: str
    root: Path

    @classmethod
    def create(cls, root: Path, slug: str) -> "Workspace":
        ws = cls(slug=slug, root=root / slug)
        ws.ensure()
        return ws

    def ensure(self) -> None:
        for sub in ("frontend", "backend", "tests", "spec", "logs"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # --- Per-role working dirs ---
    def role_dir(self, role: Role) -> Path:
        if role == Role.APOLLO:
            return self.root / "frontend"
        if role == Role.ATLAS:
            return self.root / "backend"
        if role == Role.HEPHAESTUS:
            return self.root / "tests"
        # Athena, Prometheus, Argus all operate on the project root
        return self.root

    # --- Spec management ---
    @property
    def spec_dir(self) -> Path:
        return self.root / "spec"

    def spec_path(self, version: int = 1) -> Path:
        return self.spec_dir / f"spec-v{version}.md"

    def latest_spec_version(self) -> int:
        versions = []
        for p in self.spec_dir.glob("spec-v*.md"):
            try:
                versions.append(int(p.stem.split("v")[-1]))
            except ValueError:
                pass
        return max(versions) if versions else 0

    def write_spec(self, content: str, version: Optional[int] = None) -> Path:
        if version is None:
            version = self.latest_spec_version() + 1
        path = self.spec_path(version)
        path.write_text(content, encoding="utf-8")
        return path

    def read_latest_spec(self) -> Optional[str]:
        v = self.latest_spec_version()
        if v == 0:
            return None
        return self.spec_path(v).read_text(encoding="utf-8")

    # --- Log files ---
    def log_path(self, role: Role) -> Path:
        return self.root / "logs" / f"{role.value}.log"

    def append_log(self, role: Role, line: str) -> None:
        path = self.log_path(role)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")

    # --- State persistence (Discord is system-of-record; this is recovery hint) ---
    @property
    def state_path(self) -> Path:
        return self.root / "state.json"

    def write_state(self, payload: Dict) -> None:
        self.state_path.write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )

    def read_state(self) -> Optional[Dict]:
        if not self.state_path.exists():
            return None
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
