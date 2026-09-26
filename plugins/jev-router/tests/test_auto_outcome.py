import json

import pytest

from jev_router import auto_outcome
from jev_router.auto_telemetry import record_usage


KEY = "a" * 24


def _decision(directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "auto-decisions.jsonl").write_text(json.dumps({
        "time": 1, "route_key": KEY, "model": "gpt-6-sol", "effort": "medium",
        "reason": "open_jev_policy_ranked", "policy_band": "standard",
    }) + "\n")


def test_main_route_outcome_usage_quality_and_feedback_join_without_guessing_missing_tokens(tmp_path):
    _decision(tmp_path)
    record_usage(tmp_path, KEY, "completed", {"input_tokens": 100, "output_tokens": 20})
    record_usage(tmp_path, KEY, "completed", {"input_tokens": 50})
    auto_outcome.quality(tmp_path, KEY, {
        "changed_paths": ["src/a.py"], "backend": {"status": "not_applicable"},
        "frontend": {"review_required": False}, "findings": [],
    })
    auto_outcome.outcome(tmp_path, KEY, "completed", ["pytest: passed"], 1)
    auto_outcome.feedback(tmp_path, KEY, "mixed", "Worked but needed cleanup")

    item = auto_outcome.recent(tmp_path)[0]
    assert item["model"] == "gpt-6-sol"
    assert item["usage"] == {
        "input_tokens": 150, "cached_tokens": None,
        "output_tokens": 20, "reasoning_tokens": None,
    }
    assert item["observed_responses"] == 2
    assert item["outcome"]["revisions"] == 1
    assert item["quality_review"]["changed_files"] == 1
    assert item["feedback"]["rating"] == "mixed"


def test_unknown_or_invalid_route_does_not_create_feedback(tmp_path):
    with pytest.raises(ValueError, match="Invalid route key"):
        auto_outcome.feedback(tmp_path, "../../outside", "good", None)
    with pytest.raises(ValueError, match="Unknown route key"):
        auto_outcome.outcome(tmp_path, KEY, "completed", [], 0)


def test_compose_visual_review_is_retained_in_main_route_quality(tmp_path):
    _decision(tmp_path)
    record = auto_outcome.quality(tmp_path, KEY, {
        "changed_paths": ["shared/src/commonMain/kotlin/HomeScreen.kt"],
        "backend": {"status": "not_applicable"},
        "frontend": {"review_required": False},
        "compose": {"review_required": True},
        "findings": [],
    })
    assert record["quality_review"]["visual_review_required"] is True


def test_recent_exposes_lineage_and_guide_version_for_audit(tmp_path):
    _decision(tmp_path)
    path = tmp_path / "auto-decisions.jsonl"
    item = json.loads(path.read_text())
    item.update({"telemetry_schema": 2, "session_fingerprint": "b" * 24,
                 "previous_route_key": "c" * 24, "relationship": "possible_correction",
                 "score": 0.62, "compared": 4, "guide_source_sha256": "d" * 64})
    path.write_text(json.dumps(item) + "\n")
    record = auto_outcome.recent(tmp_path)[0]
    assert record["session_fingerprint"] == "b" * 24
    assert record["previous_route_key"] == "c" * 24
    assert record["relationship"] == "possible_correction"
    assert record["score"] == 0.62
    assert record["guide_source_sha256"] == "d" * 64
