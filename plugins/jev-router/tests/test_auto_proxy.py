"""Check the risky boundary between Codex, Jev routing, and Responses."""

import json

import httpx
from starlette.testclient import TestClient
import zstandard

from jev_router import auto_proxy
from jev_router.auto_routing import RoutedRequest
from jev_router.routing import Candidate, Decision, Profile


class FixedRouter:
    def __init__(self, model: str = "gpt-5.6-terra") -> None:
        self.model = model

    async def route(self, payload: dict, allowed_models: set[str] | None = None) -> RoutedRequest:
        candidate = Candidate(self.model, "medium")
        decision = Decision(candidate, "test", None, 1, Profile("general", "focused", 0, (), False))
        return RoutedRequest({**payload, "model": self.model, "reasoning": {"effort": "medium"}}, decision, "route123")


class EventStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"data: ok\n\n"


def test_zstd_request_routes_and_preserves_authorization(monkeypatch):
    observed = {}
    original_client = httpx.AsyncClient

    def upstream(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["authorization"] = request.headers.get("authorization")
        observed["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=EventStream())

    monkeypatch.setattr(auto_proxy.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(upstream)))
    monkeypatch.setenv("JEV_API_UPSTREAM_URL", "https://example.test/v1")
    payload = {"model": "jev-auto", "input": [{"role": "user", "content": [{"type": "input_text", "text": "fix button"}]}]}
    compressed = zstandard.ZstdCompressor().compress(json.dumps(payload).encode())
    with TestClient(auto_proxy.create_app(FixedRouter())) as client:
        response = client.post("/v1/responses", content=compressed, headers={"content-encoding": "zstd", "authorization": "Bearer secret"})
    assert response.status_code == 200
    assert response.content == b"data: ok\n\n"
    assert observed == {
        "url": "https://example.test/v1/responses",
        "authorization": "Bearer secret",
        "body": {**payload, "model": "gpt-5.6-terra", "reasoning": {"effort": "medium"}},
    }


def test_astra_is_blocked_before_upstream(monkeypatch):
    monkeypatch.setattr(auto_proxy.httpx, "AsyncClient", lambda **kwargs: (_ for _ in ()).throw(AssertionError("upstream called")))
    with TestClient(auto_proxy.create_app(FixedRouter("gpt-6-astra"))) as client:
        response = client.post("/v1/responses", json={"model": "jev-auto", "input": []})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "astra_approval_required"


def test_cli_compatibility_filter_does_not_reduce_desktop_candidates():
    assert auto_proxy._allowed_models("Codex Desktop/0.154.0 (codex_exec; 0.154.0)") == {"gpt-5.6-terra", "gpt-6-astra"}
    assert auto_proxy._allowed_models("Codex Desktop/0.154.0") is None
