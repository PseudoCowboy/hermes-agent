"""Unit tests for the JSON store."""

import json

from mythos.models import (
    AgentRun,
    AgentRole,
    Approval,
    Artifact,
    ArtifactType,
    AuditEvent,
    ChannelRole,
    Project,
    ProjectChannel,
    ProjectPhase,
    Task,
    TaskStatus,
)
from mythos.store import JsonStore


def test_save_and_load_project_roundtrip(tmp_path):
    store = JsonStore(tmp_path)
    p = Project(title="hello", request_text="build hello world")
    store.save_project(p)
    loaded = store.get_project(p.id)
    assert loaded is not None
    assert loaded.title == "hello"
    assert loaded.phase == ProjectPhase.INTAKE


def test_list_projects_returns_all(tmp_path):
    store = JsonStore(tmp_path)
    a = Project(title="a")
    b = Project(title="b")
    store.save_project(a)
    store.save_project(b)
    titles = sorted(p.title for p in store.list_projects())
    assert titles == ["a", "b"]


def test_channel_index_finds_project(tmp_path):
    store = JsonStore(tmp_path)
    p = Project(title="x", project_channel_id="c1")
    store.save_project(p)
    store.save_channel(ProjectChannel(
        project_id=p.id, discord_channel_id="c1",
        channel_role=ChannelRole.PROJECT,
    ))
    found = store.find_project_by_channel("c1")
    assert found is not None and found.id == p.id


def test_audit_jsonl_is_appended(tmp_path):
    store = JsonStore(tmp_path)
    p = Project(title="a")
    store.save_project(p)
    store.append_audit(AuditEvent(
        project_id=p.id, event_type="x", actor_type="system", actor_id="o",
        payload={"k": 1},
    ))
    store.append_audit(AuditEvent(
        project_id=p.id, event_type="y", actor_type="system", actor_id="o",
        payload={"k": 2},
    ))
    events = list(store.iter_audit(p.id))
    assert [e.event_type for e in events] == ["x", "y"]


def test_artifact_versioning(tmp_path):
    store = JsonStore(tmp_path)
    pid = "prj_1"
    store.save_artifact(Artifact(
        project_id=pid, type=ArtifactType.DESIGN_SPEC, version=1, path="/a",
    ))
    store.save_artifact(Artifact(
        project_id=pid, type=ArtifactType.DESIGN_SPEC, version=2, path="/b",
    ))
    arts = store.list_artifacts(pid)
    assert len(arts) == 2
    assert {a.version for a in arts} == {1, 2}


def test_recovery_after_restart(tmp_path):
    store1 = JsonStore(tmp_path)
    p = Project(title="resilient", phase=ProjectPhase.AWAITING_APPROVAL)
    store1.save_project(p)
    store1.save_task(Task(
        project_id=p.id, role=AgentRole.BACKEND, title="t",
        status=TaskStatus.PENDING,
    ))

    # Simulate restart
    store2 = JsonStore(tmp_path)
    loaded = store2.get_project(p.id)
    assert loaded is not None
    assert loaded.phase == ProjectPhase.AWAITING_APPROVAL
    tasks = store2.list_tasks(p.id)
    assert len(tasks) == 1
    assert tasks[0].role == AgentRole.BACKEND
