import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from jev_router import run_log
from jev_router.routing import Candidate, Decision, Profile


def test_route_outcome_and_feedback_survive_reloads(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "app.py").write_text("value = 1\n")
    subprocess.run(["git", "-C", str(workspace), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(workspace), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "initial"], check=True)
    decision = Decision(Candidate("gpt-6-sol", "medium"), "open_jev_policy_ranked", 0.3, 20, Profile("backend", "focused", 1, ("Python",), False))

    started = run_log.start(tmp_path / "data", decision, workspace)
    run_id = started["run_id"]
    assert started["decision"]["candidate"]["key"] == "gpt-6-sol/medium"
    assert started["git_before"]["status"] == []
    with pytest.raises(ValueError, match="Finish the run"):
        run_log.feedback(tmp_path / "data", run_id, "good", None)

    (workspace / "app.py").write_text("value = 2\n")
    finished = run_log.finish(tmp_path / "data", run_id, "completed", ["pytest: 3 passed"], [["gpt-6-sol", "medium", "/root/implementation"]])
    assert finished["outcome"]["git_after"]["status"] == [" M app.py"]
    assert finished["outcome"]["checks"] == ["pytest: 3 passed"]
    assert finished["outcome"]["agents"] == [{"model": "gpt-6-sol", "effort": "medium", "agent_id": "/root/implementation"}]
    with pytest.raises(ValueError, match="already finished"):
        run_log.finish(tmp_path / "data", run_id, "completed", [])

    rated = run_log.feedback(tmp_path / "data", run_id, "mixed", "The API works; naming is unclear")
    assert rated["feedback"]["rating"] == "mixed"
    assert run_log.recent(tmp_path / "data")[0] == rated


def test_run_id_cannot_escape_run_directory(tmp_path):
    with pytest.raises(ValueError, match="Invalid run ID"):
        run_log.finish(tmp_path, "../../config.toml", "completed", [])


def test_parallel_finish_cannot_overwrite_first_outcome(tmp_path):
    decision = Decision(Candidate("gpt-6-sol", "medium"), "test", None, 1, Profile("general", "focused", 0, (), False))
    run_id = run_log.start(tmp_path, decision, None)["run_id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda check: _finish_or_error(tmp_path, run_id, check), ("first", "second")))
    assert sorted(results) == ["ValueError", "completed"]
    assert run_log.recent(tmp_path)[0]["outcome"]["checks"] in (["first"], ["second"])


def test_quality_review_keeps_counts_without_source_or_request(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    decision = Decision(Candidate("gpt-6-sol", "medium"), "test", None, 1, Profile("backend", "focused", 0, (), False))
    run_id = run_log.start(tmp_path / "data", decision, workspace)["run_id"]
    report = {
        "workspace": str(workspace.resolve()),
        "request": "private requirement",
        "changed_paths": ["src/Secret.java"],
        "backend": {"status": "jpa_querydsl"},
        "frontend": {"review_required": False},
        "compose": {"review_required": True},
        "findings": [{"severity": "warning", "message": "Changed path is outside the planned scope", "evidence": "password=secret"}],
    }
    recorded = run_log.record_quality(tmp_path / "data", run_id, report)
    assert recorded["finding_counts"] == {"warning": 1}
    assert recorded["out_of_scope_files"] == 1
    assert recorded["compose_visual_review_required"] is True
    assert recorded["visual_review_required"] is True
    content = (tmp_path / "data" / "runs" / f"{run_id}.json").read_text()
    assert "private requirement" not in content
    assert "password=secret" not in content
    run_log.finish(tmp_path / "data", run_id, "completed", [])
    with pytest.raises(ValueError, match="before finishing"):
        run_log.record_quality(tmp_path / "data", run_id, report)


def _finish_or_error(directory, run_id, check):
    try:
        return run_log.finish(directory, run_id, "completed", [check])["outcome"]["status"]
    except ValueError:
        return "ValueError"
