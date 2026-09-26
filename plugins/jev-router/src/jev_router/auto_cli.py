"""Install, operate, and inspect the Jev Auto Responses router."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

import tomlkit
import uvicorn

from .auto_catalog import sync_catalog
from .auto_proxy import create_app, data_directory
from .auto_routing import AutoRouter
from .doctor import diagnose
from .quality import capture_baseline, inspect_quality
from .routing import ImplementationContract
from .intent_review import review_intent_sync
from . import auto_outcome, run_log


LABEL = "com.jevrouter.auto"
DEFAULT_PORT = 18084


def _config_path() -> Path:
    return Path.home() / ".codex/config.toml"


def _plist_path() -> Path:
    return Path.home() / f"Library/LaunchAgents/{LABEL}.plist"


def _base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/v1"


def _read_json_object(path: Path) -> dict:
    if path.stat().st_size > 128_000:
        raise ValueError(f"JSON input is too large: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _baseline_path(baseline_id: str) -> Path:
    if len(baseline_id) != 32 or any(character not in "0123456789abcdef" for character in baseline_id):
        raise ValueError("Invalid quality baseline ID")
    return data_directory() / "quality-baselines" / f"{baseline_id}.json"


def _save_baseline(workspace: Path) -> tuple[str, int]:
    baseline = capture_baseline(workspace)
    baseline_id = uuid4().hex
    path = _baseline_path(baseline_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(baseline, stream)
    return baseline_id, len(baseline["signatures"])


def _service_alive(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
            return json.load(response).get("model") == "jev-auto"
    except (URLError, TimeoutError, OSError, ValueError):
        return False


def _start_service(port: int, directory: Path) -> None:
    executable = directory / "runtime/bin/jev-auto"
    if not executable.is_file():
        raise RuntimeError(f"Install the package first; executable missing: {executable}")
    plist = _plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    configuration = {
        "Label": LABEL,
        "ProgramArguments": [str(executable), "serve", "--port", str(port)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(directory / "auto-service.log"),
        "StandardErrorPath": str(directory / "auto-service-error.log"),
    }
    plist.write_bytes(plistlib.dumps(configuration))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True, check=False)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True, text=True, check=True)
    subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{LABEL}"], capture_output=True, text=True, check=True)
    for _ in range(30):
        if _service_alive(port):
            return
        time.sleep(0.5)
    raise RuntimeError(f"Jev Auto service did not become healthy; see {directory / 'auto-service-error.log'}")


def _install_runtime(directory: Path) -> Path:
    """Run the LaunchAgent outside Desktop's macOS privacy-protected directory."""
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required to install Jev Auto")
    runtime = directory / "runtime"
    if not (runtime / "bin/python").exists():
        subprocess.run([uv, "venv", "--python", "3.11", str(runtime)], check=True, capture_output=True, text=True)
    project = Path(__file__).resolve().parents[2]
    subprocess.run(
        [uv, "pip", "install", "--python", str(runtime / "bin/python"), "--reinstall", str(project)],
        check=True, capture_output=True, text=True,
    )
    return runtime / "bin/jev-auto"


def _write_config(catalog: Path, port: int, directory: Path) -> Path:
    config_path = _config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    config = tomlkit.parse(original)
    before = {
        "openai_base_url": str(config["openai_base_url"]) if "openai_base_url" in config else None,
        "model_catalog_json": str(config["model_catalog_json"]) if "model_catalog_json" in config else None,
    }
    base_url = _base_url(port)
    config["openai_base_url"] = base_url
    config["model_catalog_json"] = str(catalog)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = directory / f"config.toml.before-jev-auto.{stamp}"
    backup.write_text(original, encoding="utf-8")
    backup.chmod(0o600)
    state = {"before": before, "managed": {"openai_base_url": base_url, "model_catalog_json": str(catalog)}, "backup": str(backup)}
    state_path = directory / "auto-install.json"
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    state_path.chmod(0o600)
    temporary = config_path.with_suffix(".jev-auto.tmp")
    temporary.write_text(tomlkit.dumps(config), encoding="utf-8")
    if config_path.exists():
        shutil.copymode(config_path, temporary)
    temporary.replace(config_path)
    return config_path


def install(port: int) -> dict:
    directory = data_directory()
    if (directory / "auto-install.json").exists():
        raise RuntimeError("Jev Auto is already installed; use sync-catalog or restore first")
    catalog = sync_catalog(directory)
    runtime_command = _install_runtime(directory)
    _start_service(port, directory)
    bin_directory = directory / "bin"
    bin_directory.mkdir(exist_ok=True)
    command = bin_directory / "jev-auto"
    command.unlink(missing_ok=True)
    command.symlink_to(runtime_command)
    config = _write_config(catalog, port, directory)
    return {"service": _base_url(port), "catalog": str(catalog), "config": str(config), "command": str(command), "desktop_restart_needed": True}


def restore() -> dict:
    directory = data_directory()
    state_path = directory / "auto-install.json"
    if not state_path.exists():
        raise RuntimeError("No Jev Auto install state found")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    config_path = _config_path()
    config = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    for key, managed in state["managed"].items():
        if key in config and str(config[key]) == managed:
            previous = state["before"].get(key)
            if previous is None:
                del config[key]
            else:
                config[key] = previous
    temporary = config_path.with_suffix(".jev-auto.tmp")
    temporary.write_text(tomlkit.dumps(config), encoding="utf-8")
    shutil.copymode(config_path, temporary)
    temporary.replace(config_path)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True, check=False)
    _plist_path().unlink(missing_ok=True)
    (directory / "bin/jev-auto").unlink(missing_ok=True)
    shutil.rmtree(directory / "runtime", ignore_errors=True)
    state_path.unlink()
    return {"restored": str(config_path), "desktop_restart_needed": True}


def main() -> None:
    parser = argparse.ArgumentParser(prog="jev-auto")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Run the local Responses API router")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    setup = sub.add_parser("install", help="Install the service and Codex catalog")
    setup.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("restore", help="Restore previous Codex configuration")
    sub.add_parser("sync-catalog", help="Refresh the merged Codex catalog")
    sub.add_parser("doctor", help="Check the local Codex catalog and Jev services")
    route = sub.add_parser("route", help="Score a subagent task before spawning")
    route.add_argument("request")
    route.add_argument("--workspace", type=Path)
    route.add_argument("--contract-file", type=Path, help="Validated implementation contract JSON for post-plan routing")
    finish = sub.add_parser("finish", help="Record a routed task's verified outcome")
    finish.add_argument("run_id")
    finish.add_argument("--status", choices=("completed", "failed", "interrupted"), required=True)
    finish.add_argument("--check", action="append", default=[], help="Observed check result; repeat as needed")
    finish.add_argument("--agent", action="append", nargs=3, metavar=("MODEL", "EFFORT", "ID"), default=[], help="Actually spawned agent; repeat for Astra plan and Sol implementation")
    feedback = sub.add_parser("feedback", help="Attach user feedback to a finished run")
    feedback.add_argument("run_id")
    feedback.add_argument("--rating", choices=("good", "bad", "mixed"), required=True)
    feedback.add_argument("--note")
    recent = sub.add_parser("runs", help="Show recent routing decisions and outcomes")
    recent.add_argument("--limit", type=int, default=10)
    auto_recent = sub.add_parser("auto-runs", help="Show Jev Auto main-turn choices, observed usage, and outcomes")
    auto_recent.add_argument("--limit", type=int, default=10)
    auto_finish = sub.add_parser("auto-outcome", help="Record verified outcome for a main Jev Auto route")
    auto_finish.add_argument("route_key")
    auto_finish.add_argument("--status", choices=("completed", "failed", "interrupted"), required=True)
    auto_finish.add_argument("--check", action="append", default=[])
    auto_finish.add_argument("--revisions", type=int, help="Observed correction rounds, if known")
    auto_feedback = sub.add_parser("auto-feedback", help="Attach explicit user feedback to a main Jev Auto route")
    auto_feedback.add_argument("route_key")
    auto_feedback.add_argument("--rating", choices=("good", "bad", "mixed"), required=True)
    auto_feedback.add_argument("--note")
    context = sub.add_parser("quality-context", help="Inspect repository conventions before implementation")
    context.add_argument("request")
    context.add_argument("--workspace", type=Path, required=True)
    context.add_argument("--focus-path", action="append", default=[], help="Likely affected file or directory; repeat as needed")
    context.add_argument("--save-baseline", action="store_true", help="Record pre-task changed-path hashes for later scope review")
    review = sub.add_parser("quality-check", help="Review changed paths and repository conventions")
    review.add_argument("request")
    review.add_argument("--workspace", type=Path, required=True)
    review.add_argument("--allowed-path", action="append", default=[], help="Planned path or glob; repeat for each scope")
    review.add_argument("--allow-jdbc", action="store_true", help="Explicitly permit direct JDBC for this task")
    review.add_argument("--jdbc-reason", help="Required explanation when --allow-jdbc is used")
    review.add_argument("--run-id", help="Attach redacted quality check counts to this routed run")
    review.add_argument("--route-key", help="Attach redacted quality check counts to a main Jev Auto route")
    review.add_argument("--baseline-id", help="Review only changes after a saved quality-context baseline")
    intent = sub.add_parser("intent-review", help="Choose the next action from a task contract and observed evidence")
    intent.add_argument("--contract-file", type=Path, required=True)
    intent.add_argument("--evidence-file", type=Path, required=True)
    intent.add_argument("--jev-url", default=os.environ.get("OPENJEV_URL", "http://127.0.0.1:8000"))
    args = parser.parse_args()
    if args.command == "serve":
        uvicorn.run(create_app(), host="127.0.0.1", port=args.port, access_log=False, log_level="warning")
        return
    try:
        if args.command == "install":
            result = install(args.port)
        elif args.command == "restore":
            result = restore()
        elif args.command == "sync-catalog":
            result = {"catalog": str(sync_catalog(data_directory()))}
        elif args.command == "doctor":
            result = diagnose()
        elif args.command == "route":
            directory = data_directory()
            directory.mkdir(parents=True, exist_ok=True)
            contract = ImplementationContract.from_dict(_read_json_object(args.contract_file)) if args.contract_file else None
            decision = asyncio.run(AutoRouter(directory).decide(args.request, args.workspace,
                                                               implementation_contract=contract))
            result = decision.to_dict()
            result["run_id"] = run_log.start(directory, decision, args.workspace)["run_id"]
        elif args.command == "finish":
            result = run_log.finish(data_directory(), args.run_id, args.status, args.check, args.agent)
        elif args.command == "feedback":
            result = run_log.feedback(data_directory(), args.run_id, args.rating, args.note)
        elif args.command == "auto-runs":
            result = auto_outcome.recent(data_directory(), args.limit)
        elif args.command == "auto-outcome":
            result = auto_outcome.outcome(data_directory(), args.route_key, args.status, args.check, args.revisions)
        elif args.command == "auto-feedback":
            result = auto_outcome.feedback(data_directory(), args.route_key, args.rating, args.note)
        elif args.command == "quality-context":
            if args.save_baseline:
                baseline_id, count = _save_baseline(args.workspace)
            result = inspect_quality(args.workspace, request=args.request, focus_paths=args.focus_path)
            if args.save_baseline:
                result["baseline_id"] = baseline_id
                result["preexisting_changed_paths"] = count
        elif args.command == "quality-check":
            if args.allow_jdbc and not (args.jdbc_reason and args.jdbc_reason.strip()):
                raise ValueError("--allow-jdbc requires --jdbc-reason")
            result = inspect_quality(
                args.workspace, request=args.request, review=True,
                allowed_paths=args.allowed_path, allow_jdbc=args.allow_jdbc,
                jdbc_reason=args.jdbc_reason if args.allow_jdbc else None,
                baseline=_read_json_object(_baseline_path(args.baseline_id)) if args.baseline_id else None,
            )
            if args.run_id:
                result["recorded_quality"] = run_log.record_quality(data_directory(), args.run_id, result)
            if args.route_key:
                result["recorded_auto_quality"] = auto_outcome.quality(data_directory(), args.route_key, result)["quality_review"]
        elif args.command == "intent-review":
            contract = _read_json_object(args.contract_file)
            evidence = _read_json_object(args.evidence_file)
            result = review_intent_sync(contract, evidence, jev_url=args.jev_url)
        else:
            if not 1 <= args.limit <= 100:
                raise ValueError("--limit must be between 1 and 100")
            result = run_log.recent(data_directory(), args.limit)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"jev-auto: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
