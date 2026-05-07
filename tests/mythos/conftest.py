"""Pytest fixtures shared by mythos tests."""
import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_state_dir():
    d = Path(tempfile.mkdtemp(prefix="mythos-test-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
