import json
from pathlib import Path
import stat
import subprocess
import uuid

import pytest

from jev_router import jobs, mcp_server


def _repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def _job(manager: jobs.JobManager, workspace: Path, request: str, state: str = "completed") -> str:
    run_id = str(uuid.uuid4())
    manager._save({
        "run_id": run_id,
        "state": state,
        "workspace": str(workspace.resolve()),
        "request": request,
        "created_at": jobs._now(),
        "decision": {"candidate": {"key": "gpt-6-luna/medium"}},
    })
    return run_id


def test_feedback_survives_new_manager_and_keeps_users_words(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _repo(repo)
    manager = jobs.JobManager(tmp_path / "data")
    run_id = _job(manager, repo, "Improve login screen")
    monkeypatch.setattr(mcp_server, "manager", manager)

    matches = mcp_server.recent_runs(str(repo))
    assert [item["run_id"] for item in matches] == [run_id]
    assert matches[0]["candidate"] == "gpt-6-luna/medium"

    result = mcp_server.record_feedback(run_id, "동작은 맞지만 간격이 어색해", "design")
    assert result["saved"] is True
    fresh_manager = jobs.JobManager(tmp_path / "data")
    feedback = fresh_manager.feedback_for_run(run_id)
    assert len(feedback) == 1
    assert feedback[0]["feedback_text"] == "동작은 맞지만 간격이 어색해"
    assert feedback[0]["aspect"] == "design"
    assert feedback[0]["source"] == "user_explicit"
    assert fresh_manager.status(run_id)["state"] == "completed"
    saved_file = next((tmp_path / "data" / "feedback").glob("*.json"))
    assert stat.S_IMODE(saved_file.stat().st_mode) == 0o600
    assert json.loads(saved_file.read_text())["run_id"] == run_id


def test_feedback_rejects_ambiguous_or_unfinished_target(tmp_path):
    repo = tmp_path / "repo"
    _repo(repo)
    manager = jobs.JobManager(tmp_path / "data")
    running = _job(manager, repo, "Running task", "running")
    finished = _job(manager, repo, "Finished task")

    with pytest.raises(ValueError, match="finished run"):
        manager.record_feedback(running, "좋았어")
    with pytest.raises(ValueError, match="1 to 10000"):
        manager.record_feedback(finished, " ")
    with pytest.raises(ValueError, match="invalid feedback aspect"):
        manager.record_feedback(finished, "좋았어", "made_up")
    with pytest.raises(ValueError, match="unknown run_id"):
        manager.record_feedback(str(uuid.uuid4()), "좋았어")
    assert not (tmp_path / "data" / "feedback").exists()


def test_recent_runs_filters_workspace(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    _repo(first)
    _repo(second)
    manager = jobs.JobManager(tmp_path / "data")
    first_id = _job(manager, first, "First task")
    _job(manager, second, "Second task")
    assert [run["run_id"] for run in manager.recent_runs(str(first))] == [first_id]
