"""Slug + workspace tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from mythos.roles import Role
from mythos.slug import is_valid_slug, make_slug
from mythos.workspace import Workspace


def test_make_slug_strips_stopwords():
    s = make_slug("I want to build a translator extension", today=date(2026, 5, 7))
    assert s.startswith("proj-translator-extension-20260507-")
    assert is_valid_slug(s)


def test_make_slug_falls_back_to_project():
    s = make_slug("the the the", today=date(2026, 5, 7))
    assert s.startswith("proj-project-20260507-")


def test_make_slug_with_seed_is_stable():
    s1 = make_slug("translator", today=date(2026, 5, 7), seed="X")
    s2 = make_slug("translator", today=date(2026, 5, 7), seed="X")
    assert s1 == s2


def test_make_slug_distinct_inputs_distinct_slugs():
    a = make_slug("translator", today=date(2026, 5, 7))
    b = make_slug("notebook", today=date(2026, 5, 7))
    assert a != b


def test_workspace_create_layout(tmp_path: Path):
    ws = Workspace.create(tmp_path, "proj-x")
    assert ws.root == tmp_path / "proj-x"
    for sub in ("frontend", "backend", "tests", "spec", "logs"):
        assert (ws.root / sub).is_dir()


def test_workspace_role_dir_routing(tmp_path: Path):
    ws = Workspace.create(tmp_path, "proj-x")
    assert ws.role_dir(Role.APOLLO) == ws.root / "frontend"
    assert ws.role_dir(Role.ATLAS) == ws.root / "backend"
    assert ws.role_dir(Role.HEPHAESTUS) == ws.root / "tests"
    # The orchestrator-level roles operate at the project root.
    assert ws.role_dir(Role.HERMES) == ws.root
    assert ws.role_dir(Role.PROMETHEUS) == ws.root
    assert ws.role_dir(Role.ARGUS) == ws.root


def test_workspace_spec_versioning(tmp_path: Path):
    ws = Workspace.create(tmp_path, "p")
    assert ws.latest_spec_version() == 0
    p1 = ws.write_spec("v1 body")
    assert p1.name == "spec-v1.md"
    assert ws.latest_spec_version() == 1
    p2 = ws.write_spec("v2 body")
    assert p2.name == "spec-v2.md"
    assert ws.read_latest_spec() == "v2 body"


def test_workspace_log_append(tmp_path: Path):
    ws = Workspace.create(tmp_path, "p")
    ws.append_log(Role.PROMETHEUS, "first")
    ws.append_log(Role.PROMETHEUS, "second")
    text = ws.log_path(Role.PROMETHEUS).read_text()
    assert text == "first\nsecond\n"
