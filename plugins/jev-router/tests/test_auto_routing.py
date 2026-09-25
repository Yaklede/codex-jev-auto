import asyncio
import json

import pytest

from jev_router.auto_routing import AutoRouter, CoordinatorUnavailable
from jev_router.routing import Candidate, Decision, Profile


def test_astra_decision_uses_non_astra_main_model_and_records_both(tmp_path, monkeypatch):
    router = AutoRouter(tmp_path)
    candidates = [Candidate("gpt-6-astra", "high"), Candidate("gpt-6-sol", "medium")]
    decision = Decision(candidates[0], "open_jev_policy_ranked", 0.8, 2, Profile("backend", "broad", 0, (), False))

    async def choose(*args, **kwargs):
        return decision

    async def available():
        return candidates

    monkeypatch.setattr(router, "decide", choose)
    monkeypatch.setattr(router, "candidates", available)
    payload = {"model": "jev-auto", "input": [{"role": "user", "content": [{"type": "input_text", "text": "Plan a difficult migration"}]}]}
    routed = asyncio.run(router.route(payload))
    assert routed.decision.candidate.model == "gpt-6-astra"
    assert routed.payload["model"] == "gpt-6-sol"
    assert routed.payload["reasoning"]["effort"] == "medium"
    assert asyncio.run(router.route(payload)).payload == routed.payload
    entries = [json.loads(line) for line in (tmp_path / "auto-decisions.jsonl").read_text().splitlines()]
    assert len(entries) == 1
    assert entries[0]["model"] == "gpt-6-astra"
    assert entries[0]["coordinator_model"] == "gpt-6-sol"


def test_astra_with_no_safe_main_model_fails_closed(tmp_path, monkeypatch):
    router = AutoRouter(tmp_path)
    candidate = Candidate("gpt-6-astra", "high")
    decision = Decision(candidate, "open_jev_policy_ranked", 0.8, 1, Profile("backend", "broad", 0, (), False))

    async def choose(*args, **kwargs):
        return decision

    async def available():
        return [candidate]

    monkeypatch.setattr(router, "decide", choose)
    monkeypatch.setattr(router, "candidates", available)
    with pytest.raises(CoordinatorUnavailable):
        asyncio.run(router.route({"model": "jev-auto", "input": []}))
