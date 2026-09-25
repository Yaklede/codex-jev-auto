import asyncio
import json

from jev_router import openjev_service


def test_autostart_uses_local_model_when_available(tmp_path, monkeypatch):
    home = tmp_path / "open-jev"
    executable = home / ".venv/bin/openjev"
    executable.parent.mkdir(parents=True)
    executable.write_text("stub")
    model = home / "models/gemma-3-4b-it"
    model.mkdir(parents=True)
    shards = ("model-00001.safetensors", "model-00002.safetensors")
    (model / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"a": shards[0], "b": shards[1]}}))
    for shard in shards:
        (model / shard).write_bytes(b"stub")
    monkeypatch.setenv("OPENJEV_HOME", str(home))
    states = iter((False, False, True))
    async def healthy(url):
        return next(states)
    monkeypatch.setattr(openjev_service, "_healthy", healthy)
    commands = []
    class Process:
        def poll(self):
            return None
    def popen(command, **kwargs):
        commands.append((command, kwargs))
        return Process()
    monkeypatch.setattr(openjev_service.subprocess, "Popen", popen)

    assert asyncio.run(openjev_service.ensure_openjev("http://127.0.0.1:8000", tmp_path))
    assert commands[0][0] == [str(executable), "serve", "--host", "127.0.0.1", "--port", "8000", "--model", str(model)]


def test_autostart_waits_for_every_model_shard(tmp_path):
    model = tmp_path / "model"
    model.mkdir()
    (model / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"a": "one.safetensors", "b": "two.safetensors"}}))
    (model / "one.safetensors").write_bytes(b"stub")
    assert not openjev_service._weights_ready(model)
    (model / "two.safetensors").write_bytes(b"stub")
    assert openjev_service._weights_ready(model)


def test_autostart_refuses_nonlocal_server(tmp_path, monkeypatch):
    async def unhealthy(url):
        return False
    monkeypatch.setattr(openjev_service, "_healthy", unhealthy)
    assert not asyncio.run(openjev_service.ensure_openjev("https://example.com", tmp_path))
