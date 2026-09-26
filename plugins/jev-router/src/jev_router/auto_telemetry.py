"""Small, content-free outcome records for routed Responses requests."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any


_FIELDS = ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens")
_MAX_EVENT_BYTES = 256 * 1024


def _token(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def usage_from_response(response: Any) -> dict[str, int | None]:
    """Extract only numeric usage from a Responses response object."""
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        usage = {}
    input_details = usage.get("input_tokens_details")
    output_details = usage.get("output_tokens_details")
    return {
        "input_tokens": _token(usage.get("input_tokens")),
        "cached_tokens": _token(input_details.get("cached_tokens")) if isinstance(input_details, dict) else None,
        "output_tokens": _token(usage.get("output_tokens")),
        "reasoning_tokens": _token(output_details.get("reasoning_tokens")) if isinstance(output_details, dict) else None,
    }


def outcome_from_event(event: Any) -> tuple[str, dict[str, int | None]] | None:
    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    status = {
        "response.completed": "completed",
        "response.failed": "failed",
        "response.incomplete": "failed",
        "response.cancelled": "cancelled",
        "response.canceled": "cancelled",
    }.get(event_type)
    if status is None:
        return None
    return status, usage_from_response(event.get("response"))


def record_usage(directory: Path, route_key: str, status: str, usage: dict[str, int | None] | None = None) -> None:
    if status not in {"completed", "failed", "cancelled", "unverified"}:
        raise ValueError("Invalid route outcome")
    usage = usage or {}
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "route_key": route_key,
        "status": status,
        **{field: _token(usage.get(field)) for field in _FIELDS},
    }
    directory.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(directory / "auto-usage.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def recent_usage(directory: Path, route_key: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Read recent compact records; ignore malformed or unrelated lines."""
    path = directory / "auto-usage.jsonl"
    if not path.exists() or limit <= 0:
        return []
    from collections import deque

    found: deque[dict[str, Any]] = deque(maxlen=limit)
    with path.open("rb") as log:
        while line := log.readline(_MAX_EVENT_BYTES + 1):
            if len(line) > _MAX_EVENT_BYTES or not line.endswith(b"\n"):
                # Skip a truncated or oversized record without loading its tail.
                while line and not line.endswith(b"\n"):
                    line = log.readline(_MAX_EVENT_BYTES + 1)
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and isinstance(entry.get("route_key"), str) and (route_key is None or entry["route_key"] == route_key):
                found.append(entry)
    return list(found)


class SSEOutcomeParser:
    """Bounded parser that observes terminal SSE events without changing chunks."""

    def __init__(self) -> None:
        self._pending = bytearray()
        self._data = bytearray()
        self._dropping = False
        self.outcome: tuple[str, dict[str, int | None]] | None = None

    def feed(self, chunk: bytes) -> None:
        if self.outcome is not None:
            return
        for byte in chunk:
            if byte == 10:  # LF; CR is stripped below.
                line = bytes(self._pending).rstrip(b"\r")
                self._pending.clear()
                self._line(line)
            elif len(self._pending) < _MAX_EVENT_BYTES:
                self._pending.append(byte)
            else:
                self._dropping = True

    def _line(self, line: bytes) -> None:
        if not line:
            if not self._dropping and self._data:
                try:
                    event = json.loads(self._data)
                except (ValueError, UnicodeDecodeError):
                    event = None
                self.outcome = outcome_from_event(event) or self.outcome
            self._data.clear()
            self._dropping = False
        elif not self._dropping and line.startswith(b"data:"):
            value = line[5:].lstrip(b" ")
            if len(self._data) + len(value) + 1 <= _MAX_EVENT_BYTES:
                self._data.extend(value)
                self._data.extend(b"\n")
            else:
                self._dropping = True
