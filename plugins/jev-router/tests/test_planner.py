import asyncio
from types import SimpleNamespace

from openai_codex import ApprovalMode, Sandbox

from jev_router import codex_runner
from jev_router.routing import Candidate


def test_astra_planner_uses_read_only_sandbox(tmp_path, monkeypatch):
    calls = {}

    class Thread:
        id = "planner-thread"

        async def run(self, request, **kwargs):
            calls["request"] = request
            calls["run_kwargs"] = kwargs
            return SimpleNamespace(
                status=SimpleNamespace(value="completed"), id="planner-turn",
                final_response="Plan only", error=None, usage=None, duration_ms=10,
            )

    class Codex:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def thread_start(self, **kwargs):
            calls["start_kwargs"] = kwargs
            return Thread()

    monkeypatch.setattr(codex_runner, "AsyncCodex", Codex)
    result = asyncio.run(codex_runner.plan_task("Plan the migration", tmp_path, Candidate("gpt-6-astra", "high")))
    assert result["status"] == "completed"
    assert calls["start_kwargs"]["sandbox"] is Sandbox.read_only
    assert calls["start_kwargs"]["approval_mode"] is ApprovalMode.deny_all
    assert "Do not modify files" in calls["start_kwargs"]["developer_instructions"]
    assert calls["run_kwargs"]["model"] == "gpt-6-astra"
