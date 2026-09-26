import asyncio
import json

import httpx

from jev_router.intent_review import review_intent


def contract(**changes):
    return {
        "requirement": "Keep the rounded button and make submit work",
        "allowed_paths": ["src/Button.tsx"],
        "acceptance_checks": ["submit_works"],
        "required_tests": ["button_test"],
        "existing_patterns": ["Use the shared rounded Button"],
        "decisions_resolved": True,
        "risk": "low",
        "ui_baseline": "Existing button is rounded",
        **changes,
    }


def evidence(**changes):
    return {
        "acceptance_results": [{"check": "submit_works", "status": "passed"}],
        "tests": [{"name": "button_test", "status": "passed"}],
        "quality": {
            "phase": "review", "findings": [], "changed_paths": ["src/Button.tsx"],
            "frontend": {"review_required": True}, "compose": {"review_required": False},
        },
        "visual_review": {
            "required": True,
            "status": "passed",
            "baseline_ref": "src/components/SharedButton.tsx",
            "rendered_ref": "artifacts/profile-mobile.png",
            "comparison_notes": "Primary button shape and spacing match the adjacent screen.",
        },
        **changes,
    }


def run(contract_value=None, evidence_value=None, *, handler=None, url="http://localhost:8000"):
    async def execute():
        if handler is None:
            return await review_intent(contract_value or contract(), evidence_value or evidence(), jev_url=url)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await review_intent(contract_value or contract(), evidence_value or evidence(),
                                       jev_url=url, client=client)
    return asyncio.run(execute())


def scorer(winner):
    def handle(request):
        body = json.loads(request.content)
        assert "diff --git" not in body["context"]
        options = body["options"]
        selected = next(index for index, option in enumerate(options) if winner in option)
        return httpx.Response(200, json={
            "best_index": selected,
            "options": [{"option": option, "score": 2.0 if index == selected else 0.0,
                         "probability": 0.8 if index == selected else 0.2}
                        for index, option in enumerate(options)],
        })
    return handle


def test_clean_evidence_can_be_a_candidate_but_never_claims_verified_intent():
    result = run(handler=scorer("completion candidate"))
    assert result["decision"] == "complete_candidate"
    assert result["jev_used"] is True
    assert result["intent_verified"] is False


def test_jev_can_request_sol_review_even_when_checks_pass():
    result = run(handler=scorer("Sol reviews"))
    assert result["decision"] == "sol_review"


def test_missing_required_test_or_acceptance_never_completes():
    missing_test = run(evidence_value=evidence(tests=[]), handler=scorer("completion candidate"))
    missing_acceptance = run(evidence_value=evidence(acceptance_results=[]), handler=scorer("completion candidate"))
    assert missing_test["decision"] == missing_acceptance["decision"] == "evidence_missing"
    assert "test_missing:button_test" in missing_test["blockers"]
    assert "acceptance_missing:submit_works" in missing_acceptance["blockers"]


def test_required_visual_review_must_be_performed():
    result = run(evidence_value=evidence(visual_review={"required": True, "status": "missing"}))
    assert result["decision"] == "evidence_missing"
    assert "visual_review" in result["blockers"]


def test_claimed_visual_pass_needs_comparison_evidence():
    result = run(evidence_value=evidence(visual_review={"required": True, "status": "passed"}))
    assert result["decision"] == "evidence_missing"
    assert "visual_comparison_evidence" in result["blockers"]


def test_static_finding_and_failed_visual_review_can_route_local_fix():
    quality = evidence()["quality"] | {"findings": [{"severity": "review", "message": "button shape mismatch"}]}
    result = run(evidence_value=evidence(quality=quality,
                                         visual_review={"required": True, "status": "failed"}),
                 handler=scorer("bounded implementation correction"))
    assert result["decision"] == "luna_fix"
    assert "static_finding:0" in result["blockers"]
    assert "visual_review_failed" in result["blockers"]


def test_jev_never_receives_completion_option_when_test_failed():
    def handle(request):
        body = json.loads(request.content)
        assert all("completion candidate" not in option for option in body["options"])
        options = body["options"]
        selected = next(index for index, option in enumerate(options) if "Sol reviews" in option)
        return httpx.Response(200, json={
            "best_index": selected,
            "options": [{"option": option, "score": 1.0 if index == selected else 0.0,
                         "probability": 0.7 if index == selected else 0.3}
                        for index, option in enumerate(options)],
        })

    result = run(evidence_value=evidence(tests=[{"name": "button_test", "status": "failed"}]),
                 handler=handle)
    assert result["decision"] == "sol_review"


def test_out_of_scope_and_failed_acceptance_require_sol():
    result = run(evidence_value=evidence(acceptance_results=[{"check": "submit_works", "status": "failed"}],
                                         quality=evidence()["quality"] | {"changed_paths": ["src/Other.tsx"]}))
    assert result["decision"] == "sol_review"
    assert "acceptance_failed:submit_works" in result["blockers"]
    assert "out_of_scope:src/Other.tsx" in result["blockers"]


def test_high_risk_failure_never_routes_luna():
    result = run(contract_value=contract(risk="high"),
                 evidence_value=evidence(tests=[{"name": "button_test", "status": "failed"}]),
                 handler=scorer("bounded implementation correction"))
    assert result["decision"] == "sol_review"


def test_unresolved_static_finding_requires_reason_to_clear():
    original = evidence()["quality"]
    unresolved = original | {"findings": [{"severity": "review", "resolved": True}]}
    assert run(evidence_value=evidence(quality=unresolved))["decision"] != "complete_candidate"
    cleared = original | {"findings": [{"severity": "review", "resolved": True,
                                         "resolution_reason": "Reviewed rendered controls and confirmed this is an existing button."}]}
    assert run(evidence_value=evidence(quality=cleared),
               handler=scorer("completion candidate"))["decision"] == "complete_candidate"


def test_unclassified_risk_and_backend_warning_do_not_route_luna():
    failed = evidence(tests=[{"name": "button_test", "status": "failed"}])
    assert run(contract_value=contract(risk="unknown"), evidence_value=failed,
               handler=scorer("bounded implementation correction"))["decision"] == "sol_review"
    warning = evidence()["quality"] | {"findings": [{"severity": "warning", "message": "Direct JDBC"}]}
    assert run(evidence_value=evidence(quality=warning),
               handler=scorer("bounded implementation correction"))["decision"] == "sol_review"


def test_unresolved_decision_and_missing_quality_are_not_completion():
    assert run(contract_value=contract(decisions_resolved=False))["decision"] == "sol_review"
    assert run(evidence_value=evidence(quality={}))["decision"] == "evidence_missing"


def test_offline_scorer_does_not_claim_jev_approval():
    def offline(request):
        raise httpx.ConnectError("offline", request=request)

    result = run(handler=offline)
    assert result["decision"] == "complete_candidate"
    assert result["jev_used"] is False
    assert result["intent_verified"] is False


def test_remote_scorer_is_not_used_for_private_requirement():
    result = run(url="https://example.com", handler=lambda request: (_ for _ in ()).throw(AssertionError()))
    assert result["jev_used"] is False
    assert result["decision"] == "complete_candidate"
