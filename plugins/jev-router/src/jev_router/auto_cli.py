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

import tomlkit
import uvicorn

from .auto_catalog import sync_catalog
from .auto_proxy import create_app, data_directory
from .auto_routing import AutoRouter


LABEL = "com.jevrouter.auto"
DEFAULT_PORT = 18084


def _config_path() -> Path:
    return Path.home() / ".codex/config.toml"


def _plist_path() -> Path:
    return Path.home() / f"Library/LaunchAgents/{LABEL}.plist"


def _base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/v1"


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
    route = sub.add_parser("route", help="Score a subagent task before spawning")
    route.add_argument("request")
    route.add_argument("--workspace", type=Path)
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
        else:
            directory = data_directory()
            directory.mkdir(parents=True, exist_ok=True)
            decision = asyncio.run(AutoRouter(directory).decide(args.request, args.workspace))
            result = decision.to_dict()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        print(f"jev-auto: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
