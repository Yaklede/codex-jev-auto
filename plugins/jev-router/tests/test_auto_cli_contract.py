import json
import sys

from jev_router import auto_cli
from jev_router.routing import Candidate, Decision, Profile


CONTRACT = {
    "requirement": "Keep the existing rounded primary button and submit the form",
    "allowed_paths": ["src/Form.tsx"],
    "acceptance_checks": ["submit_works", "button_matches_existing"],
    "existing_patterns": ["Use the shared PrimaryButton"],
    "decisions_resolved": True,
    "risk": "low",
    "ui_baseline": ["src/components/PrimaryButton.tsx"],
}


def test_route_accepts_validated_implementation_contract(tmp_path, monkeypatch, capsys):
    contract_file = tmp_path / "contract.json"
    contract_file.write_text(json.dumps(CONTRACT), encoding="utf-8")
    monkeypatch.setenv("JEV_ROUTER_DATA_DIR", str(tmp_path / "data"))
    seen = {}

    class FakeRouter:
        def __init__(self, directory):
            pass

        async def decide(self, request, workspace=None, implementation_contract=None):
            seen["contract"] = implementation_contract
            return Decision(Candidate("gpt-6-luna", "medium"), "bounded_contract", None, 20,
                            Profile("frontend", "focused", 1, ("React",), False), "bounded_implementation")

    monkeypatch.setattr(auto_cli, "AutoRouter", FakeRouter)
    monkeypatch.setattr(sys, "argv", ["jev-auto", "route", "Implement form submit", "--contract-file", str(contract_file)])
    auto_cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["candidate"]["model"] == "gpt-6-luna"
    assert seen["contract"].decisions_resolved is True
    assert seen["contract"].ui_baseline == ("src/components/PrimaryButton.tsx",)


def test_intent_review_cli_passes_observed_evidence(tmp_path, monkeypatch, capsys):
    contract_file = tmp_path / "contract.json"
    evidence_file = tmp_path / "evidence.json"
    contract_file.write_text(json.dumps(CONTRACT), encoding="utf-8")
    evidence_file.write_text(json.dumps({"tests": [{"name": "submit_works", "required": True, "status": "passed"}]}), encoding="utf-8")
    seen = {}

    def fake_review(contract, evidence, *, jev_url):
        seen.update({"contract": contract, "evidence": evidence, "jev_url": jev_url})
        return {"decision": "evidence_missing", "reason": "visual_review_missing", "blockers": ["visual review"],
                "jev_used": False, "jev_score": None}

    monkeypatch.setattr(auto_cli, "review_intent_sync", fake_review)
    monkeypatch.setattr(sys, "argv", ["jev-auto", "intent-review", "--contract-file", str(contract_file),
                                   "--evidence-file", str(evidence_file), "--jev-url", "http://127.0.0.1:8000"])
    auto_cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["decision"] == "evidence_missing"
    assert seen["contract"]["requirement"] == CONTRACT["requirement"]
    assert seen["evidence"]["tests"][0]["status"] == "passed"
