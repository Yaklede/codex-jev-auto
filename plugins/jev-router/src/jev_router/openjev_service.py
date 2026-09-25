"""Start a local Open Jev scorer on demand when its model is installed."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
from urllib.parse import urlparse

import httpx


async def _healthy(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(f"{url.rstrip('/')}/health")
            response.raise_for_status()
            data = response.json()
            return data.get("ok") is True and bool(data.get("model"))
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return False


def _weights_ready(model: Path) -> bool:
    index = model / "model.safetensors.index.json"
    if index.is_file():
        try:
            shards = set(json.loads(index.read_text(encoding="utf-8"))["weight_map"].values())
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False
        return bool(shards) and all((model / shard).is_file() for shard in shards)
    return (model / "model.safetensors").is_file()


async def ensure_openjev(url: str, data_directory: Path) -> bool:
    """Start only a local server; leave remote and unavailable setups untouched."""
    if await _healthy(url):
        return True
    if os.environ.get("OPENJEV_AUTOSTART", "1") == "0":
        return False
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.path not in ("", "/"):
        return False
    home = Path(os.environ.get("OPENJEV_HOME", Path.home() / ".local/share/jev-router/open-jev")).expanduser()
    executable = home / ".venv/bin/openjev"
    model = Path(os.environ.get("OPENJEV_MODEL", home / "models/gemma-3-4b-it")).expanduser()
    if not model.is_absolute():
        model = home / model
    if not executable.is_file() or not model.is_dir() or not _weights_ready(model):
        return False
    port = parsed.port or 80
    log_path = data_directory / "openjev.log"
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            [str(executable), "serve", "--host", "127.0.0.1", "--port", str(port), "--model", str(model)],
            cwd=home, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    for _ in range(60):
        if await _healthy(url):
            return True
        if process.poll() is not None:
            return False
        await asyncio.sleep(1)
    return False
