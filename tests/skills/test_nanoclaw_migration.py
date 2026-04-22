from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "migration"
    / "nanoclaw-migration"
    / "scripts"
    / "nanoclaw_to_hermes.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("nanoclaw_to_hermes", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_resolve_selected_options_supports_presets():
    mod = load_module()
    portable = mod.resolve_selected_options(preset="portable")
    full = mod.resolve_selected_options(preset="full")

    assert "role-skills" in portable
    assert "archive-runtime" not in portable
    assert "archive-runtime" in full
    assert portable < full


def test_migrator_imports_workspace_instructions_and_workflow_docs(tmp_path: Path):
    mod = load_module()
    source = tmp_path / "nanoclaw"
    target = tmp_path / ".hermes"
    target.mkdir()

    (source / "docs").mkdir(parents=True)
    (source / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    (source / "CLAUDE.md").write_text("# NanoClaw\n", encoding="utf-8")
    (source / "docs" / "MULTI-AGENT-WORKFLOW-INSTRUCTIONS.md").write_text("# Workflow\n", encoding="utf-8")
    (source / "docs" / "MULTI-AGENT-PROJECT-DEVELOPMENT-SPEC.md").write_text("# Spec\n", encoding="utf-8")
    (source / "docs" / "MULTI-AGENT-FAILURE-PATTERNS.md").write_text("# Failures\n", encoding="utf-8")

    migrator = mod.Migrator(
        source_root=source,
        target_root=target,
        execute=True,
        overwrite=False,
        output_dir=target / "migration-report",
        selected_options={"workspace-instructions", "workflow-docs"},
        preset_name="portable",
    )
    report = migrator.migrate()

    assert (target / "imports" / "nanoclaw" / "workspace" / "AGENTS.md").exists()
    assert (target / "imports" / "nanoclaw" / "workspace" / "CLAUDE.md").exists()
    assert (target / "imports" / "nanoclaw" / "docs" / "workflow" / "MULTI-AGENT-WORKFLOW-INSTRUCTIONS.md").exists()
    assert (target / "imports" / "nanoclaw" / "docs" / "workflow" / "MULTI-AGENT-PROJECT-DEVELOPMENT-SPEC.md").exists()
    assert report["summary"]["migrated"] >= 4
    assert report["selection"]["selected"] == ["workflow-docs", "workspace-instructions"]


def test_migrator_converts_group_roles_into_skills(tmp_path: Path):
    mod = load_module()
    source = tmp_path / "nanoclaw"
    target = tmp_path / ".hermes"
    target.mkdir()

    (source / "groups" / "dc_hermes").mkdir(parents=True)
    (source / "groups" / "dc_atlas").mkdir(parents=True)
    (source / "groups" / "dc_hermes" / "CLAUDE.md").write_text(
        "# Hermes Role\n\nReview plans.\n",
        encoding="utf-8",
    )
    (source / "groups" / "dc_atlas" / "CLAUDE.md").write_text(
        "# Atlas Role\n\nImplement backend work.\n",
        encoding="utf-8",
    )

    migrator = mod.Migrator(
        source_root=source,
        target_root=target,
        execute=True,
        overwrite=False,
        output_dir=target / "migration-report",
        selected_options={"role-skills"},
        preset_name="portable",
    )
    report = migrator.migrate()

    hermes_skill = target / "skills" / mod.SKILL_CATEGORY_DIRNAME / "nanoclaw-role-hermes" / "SKILL.md"
    atlas_skill = target / "skills" / mod.SKILL_CATEGORY_DIRNAME / "nanoclaw-role-atlas" / "SKILL.md"
    assert hermes_skill.exists()
    assert atlas_skill.exists()

    text = hermes_skill.read_text(encoding="utf-8")
    assert "name: nanoclaw-role-hermes" in text
    assert "Imported NanoClaw Role: Hermes" in text
    assert "# Hermes Role" in text
    assert report["summary"]["migrated"] >= 2


def test_migrator_archives_runtime_and_discord_metadata(tmp_path: Path):
    mod = load_module()
    source = tmp_path / "nanoclaw"
    target = tmp_path / ".hermes"
    target.mkdir()

    (source / ".claude").mkdir(parents=True)
    (source / ".claude" / "settings.json").write_text("{}\n", encoding="utf-8")
    (source / "container" / "agent-runner").mkdir(parents=True)
    (source / "container" / "agent-runner" / "package.json").write_text("{}\n", encoding="utf-8")
    (source / "docs").mkdir(parents=True)
    (source / "docs" / "discord-bot-summary.md").write_text("# Discord Commands\n", encoding="utf-8")

    migrator = mod.Migrator(
        source_root=source,
        target_root=target,
        execute=True,
        overwrite=False,
        output_dir=target / "migration-report",
        selected_options={"archive-runtime", "archive-discord-metadata"},
        preset_name="full",
    )
    report = migrator.migrate()

    output_dir = Path(report["output_dir"])
    assert (output_dir / "archive" / ".claude" / "settings.json").exists()
    assert (output_dir / "archive" / "container" / "agent-runner" / "package.json").exists()
    assert (output_dir / "archive" / "docs" / "discord-bot-summary.md").exists()
    assert report["summary"]["archived"] >= 3


def test_role_skill_conflict_without_overwrite_preserves_existing_file(tmp_path: Path):
    mod = load_module()
    source = tmp_path / "nanoclaw"
    target = tmp_path / ".hermes"
    target.mkdir()

    (source / "groups" / "dc_hermes").mkdir(parents=True)
    (source / "groups" / "dc_hermes" / "CLAUDE.md").write_text(
        "# Hermes Role\n\nNew imported instructions.\n",
        encoding="utf-8",
    )

    existing_skill = target / "skills" / mod.SKILL_CATEGORY_DIRNAME / "nanoclaw-role-hermes" / "SKILL.md"
    existing_skill.parent.mkdir(parents=True)
    existing_skill.write_text("existing\n", encoding="utf-8")

    migrator = mod.Migrator(
        source_root=source,
        target_root=target,
        execute=True,
        overwrite=False,
        output_dir=target / "migration-report",
        selected_options={"role-skills"},
        preset_name="portable",
    )
    report = migrator.migrate()

    assert existing_skill.read_text(encoding="utf-8") == "existing\n"
    conflicts = [item for item in report["items"] if item["status"] == "conflict"]
    assert any(item["kind"] == "role-skills" for item in conflicts)
