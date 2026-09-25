"""Background task lifecycle and durable local run records."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Iterator
import uuid

from .codex_runner import execute_task, list_candidates, plan_task
from .openjev_service import ensure_openjev
from .routing import Candidate, Decision, choose, fallback, inspect_repo


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True, text=True, check=True, timeout=30,
    ).stdout


def _workspace(path: str) -> Path:
    candidate = Path(path).expanduser().resolve(strict=True)
    if not candidate.is_dir():
        raise ValueError("workspace must be a directory")
    try:
        root = Path(_git(candidate, "rev-parse", "--show-toplevel").strip()).resolve(strict=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError("workspace must be inside a Git repository") from exc
    return root


@contextmanager
def _repository_lock(directory: Path, workspace: Path) -> Iterator[None]:
    digest = hashlib.sha256(str(workspace).encode()).hexdigest()[:24]
    lock_path = directory / f"repo-{digest}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another Jev Router task is already running in this repository") from exc
        yield
    finally:
        os.close(fd)


class JobManager:
    def __init__(self, directory: Path | None = None, jev_url: str | None = None) -> None:
        default_dir = os.environ.get("PLUGIN_DATA") or os.environ.get("JEV_ROUTER_DATA_DIR")
        self.directory = Path(directory or default_dir or Path.home() / ".local/share/jev-router").expanduser()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.jev_url = jev_url or os.environ.get("OPENJEV_URL", "http://127.0.0.1:8000")
        self._tasks: set[asyncio.Task[Any]] = set()

    def _file(self, run_id: str) -> Path:
        try:
            uuid.UUID(run_id)
        except ValueError as exc:
            raise ValueError("invalid run_id") from exc
        return self.directory / f"{run_id}.json"

    def _save(self, job: dict[str, Any]) -> None:
        job["updated_at"] = _now()
        destination = self._file(job["run_id"])
        temporary = destination.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, destination)

    def status(self, run_id: str) -> dict[str, Any]:
        try:
            job = json.loads(self._file(run_id).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError("unknown run_id") from exc
        if job["state"] in ("queued", "routing", "approved", "planning", "running"):
            pid = job.get("worker_pid")
            if pid and pid != os.getpid():
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    job["state"] = "interrupted"
                    job["error"] = "The MCP server stopped before this run completed"
                    self._save(job)
        return job

    def recent_runs(self, workspace: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        """Find past runs so feedback in a later conversation can be linked safely."""
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        root = str(_workspace(workspace)) if workspace else None
        runs: list[dict[str, Any]] = []
        for path in self.directory.glob("*.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                uuid.UUID(job["run_id"])
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
            if root and job.get("workspace") != root:
                continue
            decision = job.get("decision") or {}
            runs.append({
                "run_id": job["run_id"],
                "created_at": job.get("created_at"),
                "state": job.get("state"),
                "workspace": job.get("workspace"),
                "request": job.get("request", "")[:240],
                "candidate": (decision.get("candidate") or {}).get("key"),
            })
        runs.sort(key=lambda item: item["created_at"] or "", reverse=True)
        return runs[:limit]

    def record_feedback(self, run_id: str, feedback_text: str, aspect: str = "overall") -> dict[str, Any]:
        """Persist the user's words without treating them as a verified quality label."""
        job = self.status(run_id)
        if job["state"] not in {"completed", "failed", "interrupted", "awaiting_confirmation"}:
            raise ValueError("feedback requires a finished run or pending Astra recommendation")
        message = feedback_text.strip()
        if not message or len(message) > 10000:
            raise ValueError("feedback_text must contain 1 to 10000 characters")
        if aspect not in {"overall", "routing", "code", "design"}:
            raise ValueError("invalid feedback aspect")
        feedback_id = str(uuid.uuid4())
        directory = self.directory / "feedback"
        directory.mkdir(mode=0o700, exist_ok=True)
        entry = {
            "schema_version": 1,
            "feedback_id": feedback_id,
            "run_id": run_id,
            "created_at": _now(),
            "source": "user_explicit",
            "aspect": aspect,
            "feedback_text": message,
        }
        destination = directory / f"{feedback_id}.json"
        temporary = directory / f"{feedback_id}.tmp"
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(entry, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return {"run_id": run_id, "feedback_id": feedback_id, "aspect": aspect, "saved": True}

    def feedback_for_run(self, run_id: str) -> list[dict[str, Any]]:
        self.status(run_id)
        entries = []
        for path in (self.directory / "feedback").glob("*.json"):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if entry.get("run_id") == run_id:
                entries.append(entry)
        entries.sort(key=lambda item: item["created_at"])
        return entries

    def _launch(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def start(self, request: str, workspace: str) -> dict[str, Any]:
        if not request.strip():
            raise ValueError("request must not be empty")
        root = _workspace(workspace)
        job = {
            "run_id": str(uuid.uuid4()), "state": "queued", "request": request,
            "workspace": str(root), "created_at": _now(), "worker_pid": os.getpid(),
        }
        self._save(job)
        self._launch(self._route(job["run_id"]))
        return {"run_id": job["run_id"], "state": "queued", "workspace": str(root)}

    async def _route(self, run_id: str) -> None:
        job = self.status(run_id)
        job["state"] = "routing"
        self._save(job)
        try:
            workspace = Path(job["workspace"])
            profile = await asyncio.to_thread(inspect_repo, workspace, job["request"])
            candidates = await list_candidates()
            job["openjev_ready"] = await ensure_openjev(self.jev_url, self.directory)
            decision = await choose(job["request"], profile, candidates, self.jev_url)
            job["decision"] = decision.to_dict()
            job["available_pairs"] = len(candidates)
            if decision.candidate.requires_confirmation:
                executors = [candidate for candidate in candidates if candidate.model == "gpt-6-sol"]
                if not executors:
                    raise RuntimeError("Astra planning requires a Sol implementation model")
                executor = next((candidate for candidate in executors if candidate.effort == "high"), fallback(executors))
                job["executor_candidate"] = {"model": executor.model, "effort": executor.effort, "key": executor.key}
                job["state"] = "awaiting_confirmation"
                self._save(job)
                return
            self._save(job)
            await self._execute(job, decision)
        except Exception as exc:
            job["state"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"
            self._save(job)

    def launch_confirmed_astra(self, run_id: str) -> dict[str, Any]:
        job = self.status(run_id)
        decision_data = job.get("decision") or {}
        candidate_data = decision_data.get("candidate") or {}
        if job["state"] != "awaiting_confirmation" or candidate_data.get("model") != "gpt-6-astra":
            raise ValueError("run is not waiting for Astra confirmation")
        executor_data = job.get("executor_candidate") or {}
        if executor_data.get("model") != "gpt-6-sol":
            raise ValueError("Astra planning has no Sol executor; start a new run")
        # Mark before scheduling to prevent duplicate calls within this MCP server.
        job["state"] = "approved"
        job["confirmed_at"] = _now()
        job["worker_pid"] = os.getpid()
        self._save(job)
        planner = Candidate(candidate_data["model"], candidate_data["effort"])
        executor = Candidate(executor_data["model"], executor_data["effort"])
        self._launch(self._execute(job, executor, planner=planner))
        return {"run_id": run_id, "state": "approved", "planner": planner.key, "executor": executor.key}

    async def _execute(self, job: dict[str, Any], decision: Decision | Candidate, planner: Candidate | None = None) -> None:
        candidate = decision.candidate if isinstance(decision, Decision) else decision
        workspace = Path(job["workspace"])
        try:
            with _repository_lock(self.directory, workspace):
                job["before_status"] = _git(workspace, "status", "--short")
                before_diff_path = self.directory / f"{job['run_id']}.before.diff"
                before_diff_path.write_text(_git(workspace, "diff", "--binary", "HEAD"), encoding="utf-8")
                job["before_diff_path"] = str(before_diff_path)
                request = job["request"]
                if planner is not None:
                    job["state"] = "planning"
                    self._save(job)
                    plan = await plan_task(request, workspace, planner)
                    job["planner_result"] = plan
                    if plan["status"] != "completed" or not plan.get("final_response"):
                        raise RuntimeError("Astra planning did not complete")
                    if _git(workspace, "status", "--short") != job["before_status"] or _git(workspace, "diff", "--binary", "HEAD") != before_diff_path.read_text(encoding="utf-8"):
                        raise RuntimeError("Astra planning changed the repository")
                    request = (
                        f"Original user request:\n{request}\n\n"
                        "Astra's read-only plan follows. Treat it as advice and verify it against the repository before implementation:\n"
                        f"{plan['final_response']}"
                    )
                job["state"] = "running"
                self._save(job)
                result = await execute_task(request, workspace, candidate)
                job["result"] = result
                job["after_status"] = _git(workspace, "status", "--short")
                diff = _git(workspace, "diff", "--binary", "HEAD")
                diff_path = self.directory / f"{job['run_id']}.diff"
                diff_path.write_text(diff, encoding="utf-8")
                job["diff_path"] = str(diff_path)
                job["state"] = "completed" if result["status"] == "completed" else "failed"
                if job["state"] == "failed":
                    job["error"] = result.get("error") or f"Codex turn ended with {result['status']}"
                self._save(job)
        except Exception as exc:
            job["state"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"
            self._save(job)
