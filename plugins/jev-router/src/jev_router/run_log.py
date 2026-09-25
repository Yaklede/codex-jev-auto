"""Keep each skill-routed task's decision, outcome, and optional feedback together."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Iterator
from uuid import uuid4

from .routing import Decision, EFFORTS, MODELS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_state(workspace: Path | None) -> dict[str, Any] | None:
    if workspace is None or not workspace.is_dir():
        return None
    try:
        head = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return {"head": head, "status": status}


def _path(directory: Path, run_id: str) -> Path:
    if len(run_id) != 32 or any(character not in "0123456789abcdef" for character in run_id):
        raise ValueError("Invalid run ID")
    return directory / "runs" / f"{run_id}.json"


def _write(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor, name = tempfile.mkstemp(prefix=".run-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def _locked_runs(directory: Path) -> Iterator[None]:
    runs = directory / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    runs.chmod(0o700)
    descriptor = os.open(runs / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def start(directory: Path, decision: Decision, workspace: Path | None) -> dict[str, Any]:
    run_id = uuid4().hex
    resolved = workspace.resolve() if workspace else None
    record = {
        "run_id": run_id,
        "started_at": _now(),
        "state": "routed",
        "decision": decision.to_dict(),
        "workspace": str(resolved) if resolved else None,
        "git_before": _git_state(resolved),
    }
    _write(_path(directory, run_id), record)
    return record


def finish(directory: Path, run_id: str, status: str, checks: list[str], agents: list[list[str]] | None = None) -> dict[str, Any]:
    if status not in {"completed", "failed", "interrupted"}:
        raise ValueError("Invalid run status")
    path = _path(directory, run_id)
    with _locked_runs(directory):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["state"] != "routed":
            raise ValueError("Run already finished")
        executions = []
        for model, effort, agent_id in agents or []:
            if model not in MODELS or effort not in EFFORTS or not agent_id.strip():
                raise ValueError("Invalid executed agent")
            executions.append({"model": model, "effort": effort, "agent_id": agent_id})
        workspace = Path(record["workspace"]) if record["workspace"] else None
        record.update({
            "state": "finished",
            "finished_at": _now(),
            "outcome": {"status": status, "checks": checks, "agents": executions, "git_after": _git_state(workspace)},
        })
        _write(path, record)
    return record


def feedback(directory: Path, run_id: str, rating: str, note: str | None) -> dict[str, Any]:
    if rating not in {"good", "bad", "mixed"}:
        raise ValueError("Invalid feedback rating")
    path = _path(directory, run_id)
    with _locked_runs(directory):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["state"] != "finished":
            raise ValueError("Finish the run before adding feedback")
        record["feedback"] = {"rating": rating, "note": note, "recorded_at": _now()}
        _write(path, record)
    return record


def recent(directory: Path, limit: int = 10) -> list[dict[str, Any]]:
    paths = sorted((directory / "runs").glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths[:limit]]
