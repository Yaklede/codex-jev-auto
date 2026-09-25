"""Read-only checks for the installed catalog, proxy, and scorer."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from urllib.error import URLError
from urllib.request import urlopen

import tomlkit

from .auto_proxy import data_directory
from .openjev_service import _weights_ready


def _health(url: str, expected_model: str | None = None) -> bool:
    try:
        with urlopen(url, timeout=2) as response:
            body = json.load(response)
        return body.get("ok") is True and (expected_model is None or body.get("model") == expected_model)
    except (URLError, TimeoutError, OSError, ValueError, TypeError):
        return False


def diagnose(config_path: Path | None = None, directory: Path | None = None) -> dict:
    config_path = config_path or Path.home() / ".codex/config.toml"
    directory = directory or data_directory()
    checks: dict[str, dict] = {}
    state_path = directory / "auto-install.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else None
    if not state:
        checks["installation"] = {"status": "fail", "detail": "Jev Auto install state is missing"}
    elif not config_path.is_file():
        checks["installation"] = {"status": "fail", "detail": "Codex config.toml is missing"}
    else:
        config = tomlkit.parse(config_path.read_text(encoding="utf-8"))
        managed = state["managed"]
        current = {key: str(config[key]) if key in config else None for key in managed}
        matches = current == managed
        checks["installation"] = {
            "status": "ok" if matches else "fail",
            "config": str(config_path),
            "detail": "Codex config matches Jev Auto installation" if matches else "Codex config differs from Jev Auto installation",
        }

    catalog_path = Path(state["managed"]["model_catalog_json"]) if state else directory / "auto-models.json"
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        auto = next(row for row in catalog["models"] if row["slug"] == "jev-auto")
        checks["catalog"] = {"status": "ok", "path": str(catalog_path), "display_name": auto["display_name"]}
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        checks["catalog"] = {"status": "fail", "path": str(catalog_path), "detail": "Jev Auto model entry is missing"}

    base_url = state["managed"]["openai_base_url"] if state else "http://127.0.0.1:18084/v1"
    proxy_health = base_url.removesuffix("/v1") + "/healthz"
    checks["proxy"] = {"status": "ok" if _health(proxy_health, "jev-auto") else "fail", "url": proxy_health}

    jev_url = os.environ.get("OPENJEV_URL", "http://127.0.0.1:8000")
    scorer_ready = _health(jev_url.rstrip("/") + "/health")
    home = Path(os.environ.get("OPENJEV_HOME", Path.home() / ".local/share/jev-router/open-jev")).expanduser()
    model = Path(os.environ.get("OPENJEV_MODEL", home / "models/gemma-3-4b-it")).expanduser()
    if not model.is_absolute():
        model = home / model
    checks["open_jev"] = {
        "status": "ok" if scorer_ready else "warn",
        "url": jev_url,
        "weights_ready": _weights_ready(model),
        "detail": "Running" if scorer_ready else "Unavailable; routing will use a deterministic fallback",
    }

    try:
        result = subprocess.run(["codex", "debug", "models"], capture_output=True, text=True, check=True, timeout=30)
        rows = json.loads(result.stdout)["models"]
        visible = any(row.get("slug") == "jev-auto" for row in rows)
        checks["cli_catalog"] = {"status": "ok" if visible else "fail", "detail": "jev-auto is listed by Codex CLI" if visible else "Codex CLI does not list jev-auto"}
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        checks["cli_catalog"] = {"status": "warn", "detail": "Could not inspect Codex CLI models"}

    checks["desktop_picker"] = {
        "status": "unverified",
        "detail": "CLI catalog visibility does not prove Desktop picker visibility; inspect the picker after restarting Desktop",
    }
    return {"ready": not any(check["status"] == "fail" for check in checks.values()), "checks": checks}
