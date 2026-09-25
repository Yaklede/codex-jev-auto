import json
import subprocess

import tomlkit

from jev_router import doctor


def test_doctor_separates_cli_visibility_from_desktop_picker(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    catalog = data / "auto-models.json"
    catalog.write_text(json.dumps({"models": [{"slug": "jev-auto", "display_name": "Jev Auto"}]}))
    base = "http://127.0.0.1:18084/v1"
    (data / "auto-install.json").write_text(json.dumps({"managed": {"openai_base_url": base, "model_catalog_json": str(catalog)}}))
    config = tmp_path / "config.toml"
    config.write_text(tomlkit.dumps({"openai_base_url": base, "model_catalog_json": str(catalog)}))
    monkeypatch.setattr(doctor, "_health", lambda url, expected_model=None: True)
    monkeypatch.setattr(doctor, "_weights_ready", lambda path: True)
    monkeypatch.setattr(doctor.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, json.dumps({"models": [{"slug": "jev-auto"}]}), ""))

    result = doctor.diagnose(config, data)
    assert result["ready"]
    assert result["checks"]["cli_catalog"]["status"] == "ok"
    assert result["checks"]["desktop_picker"]["status"] == "unverified"

    config.write_text(tomlkit.dumps({"openai_base_url": "https://example.test/v1", "model_catalog_json": str(catalog)}))
    result = doctor.diagnose(config, data)
    assert not result["ready"]
    assert result["checks"]["installation"]["status"] == "fail"
