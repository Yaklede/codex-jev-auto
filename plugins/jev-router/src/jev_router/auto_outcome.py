"""Join Jev Auto main-turn routing, usage, checks, and explicit feedback."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .run_log import _locked_runs, _write


def _key(route_key: str) -> str:
    if len(route_key) != 24 or any(character not in "0123456789abcdef" for character in route_key):
        raise ValueError("Invalid route key")
    return route_key


def _path(directory: Path, route_key: str) -> Path:
    return directory / "auto-outcomes" / f"{_key(route_key)}.json"


def _decision(directory: Path, route_key: str) -> dict[str, Any]:
    path = directory / "auto-decisions.jsonl"
    if not path.is_file():
        raise ValueError("Unknown route key")
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get("route_key") == route_key:
            return item
    raise ValueError("Unknown route key")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _update(directory: Path, route_key: str, field: str, value: dict[str, Any]) -> dict[str, Any]:
    route_key = _key(route_key)
    _decision(directory, route_key)
    path = _path(directory, route_key)
    with _locked_runs(directory):
        record = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"route_key": route_key}
        record[field] = value
        _write(path, record)
    return record


def outcome(directory: Path, route_key: str, status: str, checks: list[str], revisions: int | None) -> dict[str, Any]:
    if status not in {"completed", "failed", "interrupted"}:
        raise ValueError("Invalid status")
    if revisions is not None and (revisions < 0 or revisions > 1000):
        raise ValueError("Revisions must be between 0 and 1000")
    if len(checks) > 30 or any(len(check) > 300 for check in checks):
        raise ValueError("Too many or too long checks")
    return _update(directory, route_key, "outcome", {
        "status": status, "checks": checks, "revisions": revisions, "recorded_at": _now(),
    })


def feedback(directory: Path, route_key: str, rating: str, note: str | None) -> dict[str, Any]:
    if rating not in {"good", "bad", "mixed"}:
        raise ValueError("Invalid rating")
    if note is not None and len(note) > 5000:
        raise ValueError("Feedback note is too long")
    return _update(directory, route_key, "feedback", {
        "rating": rating, "note": note, "source": "user_explicit", "recorded_at": _now(),
    })


def quality(directory: Path, route_key: str, report: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for finding in report.get("findings", []):
        severity = finding.get("severity", "review")
        counts[severity] = counts.get(severity, 0) + 1
    return _update(directory, route_key, "quality_review", {
        "changed_files": len(report.get("changed_paths", [])),
        "out_of_scope_files": sum(
            finding.get("message") == "Changed path is outside the planned scope"
            for finding in report.get("findings", [])
        ),
        "backend_profile": report.get("backend", {}).get("status"),
        "visual_review_required": bool(
            report.get("frontend", {}).get("review_required")
            or report.get("compose", {}).get("review_required")
        ),
        "finding_counts": counts,
        "recorded_at": _now(),
    })


def recent(directory: Path, limit: int = 10) -> list[dict[str, Any]]:
    """Return compact records; absent usage remains unknown rather than zero."""
    if not 1 <= limit <= 100:
        raise ValueError("Limit must be between 1 and 100")
    path = directory / "auto-decisions.jsonl"
    if not path.exists():
        return []
    from .auto_telemetry import recent_usage

    decisions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        try:
            item = json.loads(line)
        except ValueError:
            continue
        key = item.get("route_key")
        if not isinstance(key, str) or key in seen:
            continue
        seen.add(key)
        decisions.append(item)
        if len(decisions) >= limit:
            break
    results = []
    for decision in decisions:
        key = decision["route_key"]
        path = _path(directory, key)
        attached = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        observations = recent_usage(directory, route_key=key, limit=10_000)
        usage = {}
        for field in ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens"):
            known = [row[field] for row in observations if isinstance(row.get(field), int)]
            usage[field] = sum(known) if known else None
        statuses: dict[str, int] = {}
        for observation in observations:
            status = observation.get("status", "unknown")
            statuses[status] = statuses.get(status, 0) + 1
        results.append({
            "route_key": key,
            "time": decision.get("time"),
            "model": decision.get("model"),
            "effort": decision.get("effort"),
            "reason": decision.get("reason"),
            "policy_band": decision.get("policy_band"),
            "usage": usage,
            "observed_responses": len(observations),
            "response_status_counts": statuses,
            "usage_history_truncated": len(observations) >= 10_000,
            "outcome": attached.get("outcome"),
            "quality_review": attached.get("quality_review"),
            "feedback": attached.get("feedback"),
        })
    return results
