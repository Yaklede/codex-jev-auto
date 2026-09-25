"""Generate a Codex model catalog that preserves native models and adds Jev Auto."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
from urllib.request import urlopen


OFFICIAL_CATALOG_URL = "https://raw.githubusercontent.com/openai/codex/main/codex-rs/models-manager/models.json"
NEEDED_NEW_MODELS = {"gpt-6-sol", "gpt-6-luna"}
VIRTUAL_MODEL = "jev-auto"


def _models(document: object) -> list[dict]:
    if not isinstance(document, dict) or not isinstance(document.get("models"), list):
        raise ValueError("Invalid Codex model catalog")
    models = document["models"]
    if any(not isinstance(item, dict) or not isinstance(item.get("slug"), str) for item in models):
        raise ValueError("Invalid Codex model entry")
    return models


def build_catalog(native: dict, official: dict) -> dict:
    """Retain the installed client's catalog and fill missing GPT-6 rows from OpenAI."""
    result = deepcopy(_models(native))
    by_slug = {item["slug"]: item for item in result if item["slug"] != VIRTUAL_MODEL}
    result = [item for item in result if item["slug"] != VIRTUAL_MODEL]
    upstream = {item["slug"]: item for item in _models(official)}
    for slug in NEEDED_NEW_MODELS:
        if slug not in by_slug:
            if slug not in upstream:
                raise ValueError(f"Official catalog has no {slug} entry")
            item = deepcopy(upstream[slug])
            result.append(item)
            by_slug[slug] = item
    if "gpt-6-sol" not in by_slug:
        raise ValueError("A GPT-6 Sol catalog template is required")
    auto = deepcopy(by_slug["gpt-6-sol"])
    auto.update({
        "slug": VIRTUAL_MODEL,
        "display_name": "Jev Auto",
        "description": "Use local Open Jev to select a Codex model and reasoning effort for this turn.",
        "visibility": "list",
        "supported_in_api": True,
        "default_reasoning_level": "medium",
        "priority": 0,
    })
    auto["supported_reasoning_levels"] = [
        level for level in auto.get("supported_reasoning_levels", [])
        if level.get("effort") in {"low", "medium", "high", "xhigh", "max"}
    ]
    auto["prefer_websockets"] = False
    auto["supports_experimental_context"] = False
    auto["use_responses_lite"] = False
    auto.pop("availability_nux", None)
    auto.pop("upgrade", None)
    result.append(auto)
    return {"models": result}


def _native_catalog(codex_home: Path) -> dict:
    cache = codex_home / "models_cache.json"
    if cache.is_file():
        return json.loads(cache.read_text(encoding="utf-8"))
    process = subprocess.run(["codex", "debug", "models"], check=True, capture_output=True, text=True, timeout=30)
    return json.loads(process.stdout)


def sync_catalog(data_directory: Path, codex_home: Path | None = None) -> Path:
    codex_home = codex_home or Path.home() / ".codex"
    native = _native_catalog(codex_home)
    with urlopen(OFFICIAL_CATALOG_URL, timeout=15) as response:
        official = json.load(response)
    catalog = build_catalog(native, official)
    data_directory.mkdir(parents=True, exist_ok=True)
    destination = data_directory / "auto-models.json"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=data_directory, delete=False, suffix=".json") as temporary:
        json.dump(catalog, temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    try:
        process = subprocess.run(
            ["codex", "debug", "models", "-c", f'model_catalog_json="{temporary_path}"'],
            check=True, capture_output=True, text=True, timeout=30,
        )
        parsed = json.loads(process.stdout)
        slugs = {entry["slug"] for entry in _models(parsed)}
        expected = {entry["slug"] for entry in _models(native)} | NEEDED_NEW_MODELS | {VIRTUAL_MODEL}
        if not expected.issubset(slugs):
            raise ValueError(f"Catalog validation lost models: {sorted(expected - slugs)}")
        temporary_path.replace(destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination
