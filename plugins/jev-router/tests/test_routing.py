import asyncio
from types import SimpleNamespace

import httpx

from jev_router.routing import Candidate, Profile, _eligible, _policy_band, available_candidates, choose


def _catalog_model(name, efforts, hidden=False):
    return SimpleNamespace(
        model=name,
        hidden=hidden,
        supported_reasoning_efforts=[SimpleNamespace(reasoning_effort=SimpleNamespace(value=e)) for e in efforts],
    )


def test_live_catalog_limits_the_twenty_target_pairs():
    entries = [
        _catalog_model(name, ("low", "medium", "high", "xhigh", "max", "ultra"))
        for name in ("gpt-6-luna", "gpt-5.6-terra", "gpt-6-sol", "gpt-6-astra")
    ]
    pairs = available_candidates(entries)
    assert len(pairs) == 20
    assert Candidate("gpt-6-astra", "max") in pairs
    assert all(pair.effort != "ultra" for pair in pairs)
    entries[0].hidden = True
    assert len(available_candidates(entries)) == 15


def test_jev_stable_ranking_selects_the_same_candidate_after_reorder():
    candidates = [Candidate("gpt-6-sol", "medium"), Candidate("gpt-6-sol", "high")]
    profile = Profile("backend", "broad", 12, ("Python",), False)

    def handle(request):
        options = __import__("json").loads(request.content)["options"]
        winner = next(i for i, item in enumerate(options) if "gpt-6-sol/high" in item)
        return httpx.Response(200, json={
            "best_index": winner,
            "options": [{"option": item, "score": 2.0 if i == winner else 0.0, "probability": 0.8 if i == winner else 0.2} for i, item in enumerate(options)],
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await choose("Refactor payment API", profile, candidates, "http://localhost", client)

    decision = asyncio.run(run())
    assert decision.candidate == Candidate("gpt-6-sol", "high")
    assert decision.reason == "open_jev_policy_ranked"


def test_jev_position_bias_falls_back_without_selecting_astra():
    candidates = [Candidate("gpt-5.6-terra", "medium"), Candidate("gpt-6-sol", "medium")]
    profile = Profile("frontend", "focused", 20, ("React",), False)

    def handle(request):
        options = __import__("json").loads(request.content)["options"]
        return httpx.Response(200, json={
            "best_index": 0,
            "options": [{"option": item, "score": 2.0 if i == 0 else 0.0, "probability": 0.9 if i == 0 else 0.1} for i, item in enumerate(options)],
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await choose("Improve UI", profile, candidates, "http://localhost", client)

    decision = asyncio.run(run())
    assert decision.candidate == Candidate("gpt-6-sol", "medium")
    assert decision.reason == "jev_order_disagreement_fallback"


def test_workload_bounds_keep_small_work_cheap_and_complex_work_capable():
    small = _policy_band("Fix one spelling mistake in README.md", Profile("general", "focused", 12, (), False))
    frontier = _policy_band("Fix a payment settlement race across services", Profile("backend", "broad", 1800, (), True))
    assert small == "small"
    assert frontier == "frontier"
    assert _eligible(Candidate("gpt-6-luna", "medium"), small)
    assert not _eligible(Candidate("gpt-6-luna", "high"), small)
    assert _eligible(Candidate("gpt-6-sol", "high"), frontier)
    assert _eligible(Candidate("gpt-6-astra", "high"), frontier)
    assert not _eligible(Candidate("gpt-6-luna", "high"), frontier)


def test_one_file_setting_change_is_small_but_cross_service_change_is_not():
    profile = Profile("backend", "focused", 200, ("Python",), False)
    one_file = "Change the default retry count from 3 to 4 in one configuration file, leaving behavior elsewhere unchanged."
    spread = "Change one configuration setting across multiple services with migration tests."
    assert _policy_band(one_file, profile) == "small"
    assert _policy_band(spread, profile) != "small"


def test_explicit_astra_waits_for_confirmation_even_without_scorer():
    candidates = [Candidate("gpt-6-sol", "medium"), Candidate("gpt-6-astra", "medium")]
    profile = Profile("mixed", "broad", 2000, (), True)
    decision = asyncio.run(choose("Use GPT-6 Astra for this architecture task", profile, candidates, "http://offline"))
    assert decision.candidate.requires_confirmation
    assert decision.reason == "explicit_astra_request"


def test_extreme_work_recommends_astra_planning_and_negation_does_not_force_it():
    profile = Profile("mixed", "broad", 8000, ("Kotlin", "React"), True)
    request = "Very difficult task: redesign payment architecture across services with a migration, settlement race, and rollback."
    assert _policy_band(request, profile) == "frontier_plan"
    assert _eligible(Candidate("gpt-6-astra", "medium"), "frontier_plan")
    assert not _eligible(Candidate("gpt-6-sol", "high"), "frontier_plan")
    assert _policy_band("Do not use GPT-6 Astra for this task", profile) != "explicit_astra"


def test_replan_review_keeps_astra_out_of_automatic_model_choice():
    candidates = [Candidate("gpt-6-astra", "high"), Candidate("gpt-6-sol", "high")]
    profile = Profile("backend", "broad", 300, ("Python",), True)

    def handle(request):
        options = __import__("json").loads(request.content)["options"]
        scores = [10.0 if "gpt-6-astra" in option else 1.0 for option in options]
        return httpx.Response(200, json={
            "best_index": scores.index(max(scores)),
            "options": [{"option": option, "score": score, "probability": 0.9}
                        for option, score in zip(options, scores)],
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await choose("Repeated failed fixes need review", profile, candidates,
                                "http://localhost", client, policy_band="replan_review")

    decision = asyncio.run(run())
    assert decision.candidate == Candidate("gpt-6-sol", "high")
    assert decision.policy_band == "replan_review"


def test_jev_network_failure_uses_non_astra_fallback():
    candidates = [Candidate("gpt-6-astra", "max"), Candidate("gpt-6-sol", "medium")]
    profile = Profile("general", "focused", 1, (), False)

    def handle(request):
        raise httpx.ConnectError("offline", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            return await choose("Update README", profile, candidates, "http://localhost", client)

    decision = asyncio.run(run())
    assert decision.candidate == Candidate("gpt-6-sol", "medium")
    assert decision.reason == "jev_unavailable_or_invalid_fallback"
