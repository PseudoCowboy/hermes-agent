"""Shared fixtures for the mythos test suite."""

from __future__ import annotations

import sys
from pathlib import Path

# Add repo root so `import mythos` works without installing the package.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
