"""Evidence-first final review; Open Jev recommends an action, not acceptance."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

from .quality import _within_scope
from .routing import _score


_OPTIONS = {
    "complete_candidate": " completion candidate: all supplied checks pass; coordinator still verifies intent",
    "luna_fix": " a bounded implementation correction following an already settled plan",
    "sol_review": " Sol reviews the user's intent, design decisions, and implementation before proceeding",
}
_LUNA_RISKS = {"low", "medium"}


def _rows(value: Any, key: str) -> dict[str, str] | None:
    if not isinstance(value, list):
        return None
    rows: dict[str, str] = {}
    for item in value:
        if not isinstance(item, Mapping) or not isinstance(item.get(key), str):
            return None
        name, status = item[key].strip(), item.get("status")
        if not name or name in rows or status not in {"passed", "failed", "missing"}:
            return None
        rows[name] = status
    return rows


def _strings(value: Any) -> list[str] | None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        return None
    return [item.strip() for item in value]


def _result(decision: str, reason: str, blockers: list[str], *,
            jev_used: bool = False, jev_score: float | None = None) -> dict[str, Any]:
    return {
        "decision": decision,
        "reason": reason,
        "blockers": blockers,
        "jev_used": jev_used,
        "jev_score": jev_score,
        "intent_verified": False,
    }


def _gates(contract: Mapping[str, Any], evidence: Mapping[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """Return missing, failed, and local-fix signals; never infer a pass."""
    missing: list[str] = []
    failed: list[str] = []
    local: list[str] = []

    if not isinstance(contract.get("requirement"), str) or not contract["requirement"].strip():
        missing.append("requirement")
    checks = _strings(contract.get("acceptance_checks"))
    if not checks or len(checks) != len(set(checks)):
        missing.append("acceptance_checks")
        checks = []
    acceptance = _rows(evidence.get("acceptance_results"), "check")
    if acceptance is None:
        missing.append("acceptance_results")
        acceptance = {}
    for check, status in acceptance.items():
        if check not in checks and status == "failed":
            failed.append(f"acceptance_failed:{check}")
    for check in checks:
        status = acceptance.get(check, "missing")
        if status == "failed":
            failed.append(f"acceptance_failed:{check}")
        elif status == "missing":
            missing.append(f"acceptance_missing:{check}")

    required_tests = _strings(contract.get("required_tests", []))
    if required_tests is None or len(required_tests) != len(set(required_tests)):
        missing.append("required_tests")
        required_tests = []
    tests_raw = evidence.get("tests")
    tests = _rows(tests_raw, "name")
    if tests is None:
        missing.append("tests")
        tests = {}
    for name, status in tests.items():
        if status == "failed":
            failed.append(f"test_failed:{name}")
            local.append("test_failure")
    for item in tests_raw if isinstance(tests_raw, list) else []:
        if isinstance(item, Mapping) and item.get("required") is True and isinstance(item.get("name"), str):
            required_tests.append(item["name"].strip())
    for name in dict.fromkeys(required_tests):
        status = tests.get(name, "missing")
        if status == "missing":
            missing.append(f"test_missing:{name}")

    quality = evidence.get("quality")
    if not isinstance(quality, Mapping) or quality.get("phase") != "review":
        missing.append("quality_review")
        quality = {}
    findings = quality.get("findings", [])
    if not isinstance(findings, list):
        missing.append("quality_findings")
        findings = []
    for index, finding in enumerate(findings):
        if not isinstance(finding, Mapping):
            missing.append(f"quality_finding_invalid:{index}")
        elif finding.get("resolved") is not True or not isinstance(finding.get("resolution_reason"), str) or not finding["resolution_reason"].strip():
            failed.append(f"static_finding:{index}")
            if finding.get("severity") == "review":
                local.append("static_finding")

    changed_paths = quality.get("changed_paths")
    claimed_paths = evidence.get("changed_paths")
    allowed_paths = _strings(contract.get("allowed_paths"))
    if allowed_paths is None or not allowed_paths:
        missing.append("allowed_paths")
        allowed_paths = []
    if _strings(changed_paths) is None:
        missing.append("changed_paths")
    else:
        if claimed_paths is not None and _strings(claimed_paths) != _strings(changed_paths):
            failed.append("changed_paths_disagree")
        for path in changed_paths:
            if allowed_paths and not _within_scope(path, allowed_paths):
                failed.append(f"out_of_scope:{path}")

    frontend = quality.get("frontend", {})
    compose = quality.get("compose", {})
    visual = evidence.get("visual_review")
    visual_required = bool(contract.get("visual_review_required") is True or contract.get("ui_baseline")
                           or (isinstance(visual, Mapping) and visual.get("required") is True))
    visual_required = visual_required or any(
        isinstance(section, Mapping) and section.get("review_required") is True
        for section in (frontend, compose)
    )
    if visual_required:
        if not isinstance(visual, Mapping) or visual.get("status") not in {"passed", "failed"}:
            missing.append("visual_review")
        elif visual["status"] == "failed":
            failed.append("visual_review_failed")
            local.append("visual_mismatch")
        elif any(not isinstance(visual.get(key), str) or not visual[key].strip()
                 for key in ("baseline_ref", "rendered_ref", "comparison_notes")):
            missing.append("visual_comparison_evidence")

    if contract.get("decisions_resolved") is not True:
        failed.append("decisions_unresolved")
    return missing, failed, local


async def review_intent(
    contract: dict[str, Any], evidence: dict[str, Any], *, jev_url: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Choose a next action from explicit evidence; completion remains a human/agent review."""
    if not isinstance(contract, dict) or not isinstance(evidence, dict):
        raise TypeError("contract and evidence must be JSON objects")
    missing, failed, local = _gates(contract, evidence)
    if missing:
        return _result("evidence_missing", "Required review evidence is missing", missing + failed)
    if any(item.startswith("acceptance_failed:") or item.startswith("out_of_scope:")
           or item in {"decisions_unresolved", "changed_paths_disagree"} for item in failed):
        return _result("sol_review", "Intent, scope, or design decisions require Sol review", failed)

    can_luna_fix = bool(failed and local and str(contract.get("risk", "")).lower() in _LUNA_RISKS)
    choices = ["luna_fix", "sol_review"] if can_luna_fix else (
        ["sol_review"] if failed else ["complete_candidate", "sol_review"]
    )
    if len(choices) == 1:
        return _result(choices[0], "Deterministic completion gate", failed)

    parsed = urlparse(jev_url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}:
        return _result("sol_review" if failed else "complete_candidate",
                       "Local Open Jev URL unavailable; deterministic fallback", failed)
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=60)
    assert client is not None
    try:
        # No raw diff enters the prompt or any persistent log. Jev's ranking is
        # advisory, so disagreements and service failures use a conservative action.
        options = [_OPTIONS[item] for item in choices]
        visual = evidence.get("visual_review")
        context = (
            "Recommend the next action for a coding task after deterministic checks. "
            "A completion candidate is not proof of user intent.\n"
            f"Requirement: {contract['requirement'][:2000]}\n"
            f"Existing patterns: {str(contract.get('existing_patterns', []))[:1000]}\n"
            f"Risk: {str(contract.get('risk', 'unknown'))[:100]}\n"
            f"Acceptance results: {str(evidence.get('acceptance_results', []))[:1000]}\n"
            f"Test results: {str(evidence.get('tests', []))[:700]}\n"
            f"Visual comparison: {str(visual.get('comparison_notes', ''))[:500] if isinstance(visual, Mapping) else 'not applicable'}\n"
            f"remaining issues: {', '.join(failed) or 'none'}.\n"
            "The next action is"
        )
        first = await _score(client, jev_url, context, options)
        second = list(reversed(await _score(client, jev_url, context, list(reversed(options)))))
        first_choice = max(range(len(choices)), key=lambda index: first[index][0])
        second_choice = max(range(len(choices)), key=lambda index: second[index][0])
        if first_choice != second_choice:
            return _result("sol_review" if failed else "complete_candidate",
                           "Open Jev option order disagreement; deterministic fallback", failed)
        chosen = choices[first_choice]
        return _result(chosen, "Open Jev advisory ranking", failed,
                       jev_used=True, jev_score=first[first_choice][1])
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, asyncio.TimeoutError):
        return _result("sol_review" if failed else "complete_candidate",
                       "Open Jev unavailable or invalid; deterministic fallback", failed)
    finally:
        if own_client:
            await client.aclose()


def review_intent_sync(contract: dict[str, Any], evidence: dict[str, Any], *,
                       jev_url: str) -> dict[str, Any]:
    """Synchronous CLI adapter; call the async API from an existing event loop."""
    return asyncio.run(review_intent(contract, evidence, jev_url=jev_url))
