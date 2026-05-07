"""Pytest fixtures shared across mythos integration tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Make the repo root importable so `import mythos` works without installation.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Mythos uses asyncio.Lock at construction time, so any fixture creating an
# Orchestrator must be inside a running loop. We rely on pytest-asyncio's
# default event_loop fixture; nothing extra needed here.
