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
    payload = {"model": "jev-auto", "prompt_cache_key": "thread-1", "input": [{"role": "user", "content": [{"type": "input_text", "text": "Plan a difficult migration"}]}]}
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


def _payload(text=None, session=None, history=()):
    messages = [{"role": "user", "content": [{"type": "input_text", "text": value}]} for value in history]
    if text is not None:
        messages.append({"role": "user", "content": [{"type": "input_text", "text": text}]})
    result = {"model": "jev-auto", "input": messages}
    if session is not None:
        result["prompt_cache_key"] = session
    return result


def test_tool_only_continuation_reuses_session_decision(tmp_path, monkeypatch):
    router = AutoRouter(tmp_path)
    seen = []

    async def decide(request, **kwargs):
        seen.append(request)
        return Decision(Candidate("gpt-6-luna", "low"), "test", None, 1, Profile("general", "focused", 0, (), False))

    monkeypatch.setattr(router, "decide", decide)
    first = asyncio.run(router.route(_payload("Fix the pagination bug", "session-a")))
    continued = asyncio.run(router.route({"model": "jev-auto", "prompt_cache_key": "session-a", "input": [{"type": "function_call_output", "output": "done"}]}))
    assert continued.route_key == first.route_key
    assert continued.payload["model"] == "gpt-6-luna"
    assert seen == ["Fix the pagination bug"]


def test_ambiguous_followup_includes_bounded_prior_user_task(tmp_path, monkeypatch):
    router = AutoRouter(tmp_path)
    seen = []

    async def decide(request, **kwargs):
        seen.append(request)
        return Decision(Candidate("gpt-6-sol", "medium"), "test", None, 1, Profile("general", "focused", 0, (), False))

    monkeypatch.setattr(router, "decide", decide)
    original = "Implement backend migration " + "x" * 3000
    asyncio.run(router.route(_payload(original, "session-b")))
    asyncio.run(router.route(_payload("진행해", "session-b")))
    assert len(seen) == 2
    assert seen[1].startswith("Previous user task: Implement backend migration ")
    assert seen[1].endswith("Current user request: 진행해")
    assert len(seen[1]) < 1700
    assert "x" * 2000 not in seen[1]


def test_identical_request_in_distinct_sessions_routes_independently(tmp_path, monkeypatch):
    router = AutoRouter(tmp_path)
    calls = []

    async def decide(request, **kwargs):
        calls.append(request)
        model = "gpt-6-luna" if len(calls) == 1 else "gpt-6-sol"
        return Decision(Candidate(model, "medium"), "test", None, 1, Profile("general", "focused", 0, (), False))

    monkeypatch.setattr(router, "decide", decide)
    one = asyncio.run(router.route(_payload("Fix typo", "session-one")))
    two = asyncio.run(router.route(_payload("Fix typo", "session-two")))
    assert len(calls) == 2
    assert one.route_key != two.route_key
    assert one.payload["model"] != two.payload["model"]


def test_no_session_does_not_reuse_or_leak_prior_task(tmp_path, monkeypatch):
    router = AutoRouter(tmp_path)
    seen = []

    async def decide(request, **kwargs):
        seen.append(request)
        return Decision(Candidate("gpt-6-sol", "medium"), "test", None, 1, Profile("general", "focused", 0, (), False))

    monkeypatch.setattr(router, "decide", decide)
    first = asyncio.run(router.route(_payload("Sensitive private migration")))
    second = asyncio.run(router.route(_payload("Fix typo")))
    third = asyncio.run(router.route(_payload("Fix typo")))
    empty = asyncio.run(router.route(_payload()))
    assert seen == ["Sensitive private migration", "Fix typo", "Fix typo", ""]
    assert len({first.route_key, second.route_key, third.route_key, empty.route_key}) == 4
    assert all(len(key) == 24 for key in (first.route_key, second.route_key, third.route_key, empty.route_key))


def test_followup_context_stays_with_its_session(tmp_path, monkeypatch):
    router = AutoRouter(tmp_path)
    seen = []

    async def decide(request, **kwargs):
        seen.append(request)
        return Decision(Candidate("gpt-6-sol", "medium"), "test", None, 1, Profile("general", "focused", 0, (), False))

    monkeypatch.setattr(router, "decide", decide)
    asyncio.run(router.route(_payload("Private backend migration", "first")))
    asyncio.run(router.route(_payload("Public UI change", "second")))
    asyncio.run(router.route(_payload("진행해", "second")))
    assert "Public UI change" in seen[-1]
    assert "Private backend migration" not in seen[-1]


def test_repeated_failure_routes_to_sol_review_without_calling_astra(tmp_path, monkeypatch):
    router = AutoRouter(tmp_path)

    async def available():
        return [Candidate("gpt-6-sol", "medium"), Candidate("gpt-6-sol", "high"),
                Candidate("gpt-6-astra", "medium")]

    async def not_ready(*args):
        return False

    monkeypatch.setattr(router, "candidates", available)
    monkeypatch.setattr("jev_router.auto_routing.ensure_openjev", not_ready)
    request = "여러 번 고쳤지만 여전히 동작하지 않아. 원인을 재검토해줘"
    result = asyncio.run(router.route(_payload(request, "stalled-task")))
    assert result.decision.policy_band == "replan_review"
    assert result.decision.candidate == Candidate("gpt-6-sol", "high")
    assert not result.decision.candidate.requires_confirmation
    assert result.payload["model"] == "gpt-6-sol"


def test_explicit_astra_request_precedes_replan_review(tmp_path, monkeypatch):
    router = AutoRouter(tmp_path)
    seen = []

    async def decide(request, **kwargs):
        seen.append(kwargs["review_replan"])
        return Decision(Candidate("gpt-6-astra", "medium"), "explicit_astra_request", None,
                        2, Profile("general", "focused", 0, (), False), "explicit_astra")

    async def available():
        return [Candidate("gpt-6-sol", "medium"), Candidate("gpt-6-astra", "medium")]

    monkeypatch.setattr(router, "decide", decide)
    monkeypatch.setattr(router, "candidates", available)
    result = asyncio.run(router.route(_payload("Astra로 분석해. 여러 번 수정해도 실패했어", "astra-explicit")))
    assert seen == [False]
    assert result.decision.candidate.requires_confirmation


def test_transcript_failures_trigger_review_without_latest_failure_words(tmp_path, monkeypatch):
    router = AutoRouter(tmp_path)

    async def available():
        return [Candidate("gpt-6-sol", "medium"), Candidate("gpt-6-sol", "high")]

    async def not_ready(*args):
        return False

    monkeypatch.setattr(router, "candidates", available)
    monkeypatch.setattr("jev_router.auto_routing.ensure_openjev", not_ready)
    payload = _payload("이전 작업을 보완해서 목표를 달성해줘", "history-session",
                       history=("첫 수정 요청", "아직 안 되니 다른 접근으로 고쳐줘"))
    payload["input"].insert(1, {"role": "assistant", "content": [
        {"type": "output_text", "text": "첫 번째 구현은 검증에 실패했습니다."}]})
    payload["input"].insert(3, {"role": "assistant", "content": [
        {"type": "output_text", "text": "두 번째 구현도 문제가 해결되지 않았습니다."}]})
    result = asyncio.run(router.route(payload))
    assert result.decision.policy_band == "replan_review"
    assert result.payload["reasoning"]["effort"] == "high"
