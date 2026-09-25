import asyncio
from pathlib import Path
import subprocess
from types import SimpleNamespace
import uuid

from jev_router import jobs, mcp_server
from jev_router.routing import Candidate, Decision, Profile


def _repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("example\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run([
        "git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "-qm", "initial",
    ], check=True)


def test_astra_never_starts_before_direct_accepted_confirmation(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo(repo)
    manager = jobs.JobManager(tmp_path / "runs")
    monkeypatch.setattr(mcp_server, "manager", manager)
    calls = []

    async def fake_candidates():
        return [Candidate("gpt-6-astra", "high"), Candidate("gpt-6-sol", "medium")]

    async def fake_choose(request, profile, candidates, url):
        return Decision(Candidate("gpt-6-astra", "high"), "open_jev_ranked", 0.7, 2, profile)

    async def fake_plan(request, workspace, candidate):
        calls.append(("plan", candidate.key))
        return {"thread_id": "planner", "turn_id": "plan-turn", "status": "completed", "final_response": "Inspect the payment API, then add a focused test.", "usage": None}

    async def fake_execute(request, workspace, candidate):
        assert "Astra's read-only plan" in request
        calls.append(("implement", candidate.key))
        return {"thread_id": "test", "turn_id": "turn", "status": "completed", "usage": None}

    monkeypatch.setattr(jobs, "list_candidates", fake_candidates)
    monkeypatch.setattr(jobs, "choose", fake_choose)
    monkeypatch.setattr(jobs, "plan_task", fake_plan)
    monkeypatch.setattr(jobs, "execute_task", fake_execute)
    async def no_server(url, directory):
        return False
    monkeypatch.setattr(jobs, "ensure_openjev", no_server)

    class Context:
        def __init__(self, answer):
            self.answer = answer
            self.prompts = []

        async def elicit(self, prompt, schema):
            self.prompts.append(prompt)
            return SimpleNamespace(
                action="accept",
                data=SimpleNamespace(confirm_astra_execution=self.answer),
            )

    class UnsupportedContext:
        async def elicit(self, prompt, schema):
            raise RuntimeError("client has no elicitation UI")

    async def run():
        started = manager.start("Fix the API", str(repo))
        await asyncio.gather(*manager._tasks)
        run_id = started["run_id"]
        assert manager.status(run_id)["state"] == "awaiting_confirmation"
        assert calls == []
        unsupported = await mcp_server.run_astra(run_id, UnsupportedContext())
        assert unsupported["state"] == "awaiting_confirmation"
        assert calls == []
        declined = await mcp_server.run_astra(run_id, Context(False))
        assert declined["state"] == "awaiting_confirmation"
        assert calls == []
        context = Context(True)
        approved = await mcp_server.run_astra(run_id, context)
        assert approved["state"] == "approved"
        assert "GPT-6 Astra" in context.prompts[0]
        await asyncio.gather(*manager._tasks)
        assert calls == [("plan", "gpt-6-astra/high"), ("implement", "gpt-6-sol/medium")]
        assert manager.status(run_id)["state"] == "completed"
        assert manager.status(run_id)["planner_result"]["thread_id"] == "planner"
        try:
            await mcp_server.run_astra(run_id, Context(True))
        except ValueError:
            pass
        else:
            raise AssertionError("Astra confirmation was reusable")

    asyncio.run(run())


def test_non_astra_selection_executes_selected_effort(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo(repo)
    manager = jobs.JobManager(tmp_path / "runs")
    calls = []

    async def fake_candidates():
        return [Candidate("gpt-6-sol", "high")]

    async def fake_execute(request, workspace, candidate):
        calls.append((request, workspace, candidate))
        return {"thread_id": "thread-123", "turn_id": "turn-123", "status": "completed", "usage": None}

    monkeypatch.setattr(jobs, "list_candidates", fake_candidates)
    monkeypatch.setattr(jobs, "execute_task", fake_execute)
    async def no_server(url, directory):
        return False
    monkeypatch.setattr(jobs, "ensure_openjev", no_server)

    async def run():
        started = manager.start("Fix the API", str(repo))
        await asyncio.gather(*manager._tasks)
        result = manager.status(started["run_id"])
        assert result["state"] == "completed"
        assert result["decision"]["candidate"]["key"] == "gpt-6-sol/high"
        assert result["result"]["thread_id"] == "thread-123"
        assert Path(result["before_diff_path"]).exists()
        assert Path(result["diff_path"]).exists()

    asyncio.run(run())
    assert calls == [("Fix the API", repo.resolve(), Candidate("gpt-6-sol", "high"))]


def test_astra_plan_that_changes_files_never_reaches_executor(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _repo(repo)
    manager = jobs.JobManager(tmp_path / "runs")
    job = {
        "run_id": str(uuid.uuid4()), "state": "approved", "request": "Plan the API change",
        "workspace": str(repo), "worker_pid": __import__("os").getpid(),
    }
    manager._save(job)
    calls = []

    async def fake_plan(request, workspace, candidate):
        (workspace / "README.md").write_text("unexpected edit\n")
        return {"status": "completed", "final_response": "Plan", "usage": None}

    async def fake_execute(request, workspace, candidate):
        calls.append(candidate.key)

    monkeypatch.setattr(jobs, "plan_task", fake_plan)
    monkeypatch.setattr(jobs, "execute_task", fake_execute)
    asyncio.run(manager._execute(job, Candidate("gpt-6-sol", "high"), planner=Candidate("gpt-6-astra", "low")))
    result = manager.status(job["run_id"])
    assert result["state"] == "failed"
    assert "changed the repository" in result["error"]
    assert calls == []
