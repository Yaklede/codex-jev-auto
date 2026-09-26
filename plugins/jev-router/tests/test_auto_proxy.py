"""Check the risky boundary between Codex, Jev routing, and Responses."""

import json
import asyncio

import httpx
import pytest
from starlette.testclient import TestClient
import zstandard

from jev_router import auto_proxy
from jev_router.auto_telemetry import SSEOutcomeParser, recent_usage
from jev_router import orchestration
from jev_router.auto_routing import RoutedRequest
from jev_router.routing import Candidate, Decision, Profile


@pytest.fixture(autouse=True)
def isolate_telemetry(monkeypatch, tmp_path):
    monkeypatch.setenv("JEV_ROUTER_DATA_DIR", str(tmp_path))


class FixedRouter:
    def __init__(self, model: str = "gpt-5.6-terra", coordinator: str | None = None) -> None:
        self.model = model
        self.coordinator = coordinator

    async def route(self, payload: dict, allowed_models: set[str] | None = None) -> RoutedRequest:
        candidate = Candidate(self.model, "medium")
        decision = Decision(candidate, "test", None, 1, Profile("general", "focused", 0, (), False))
        return RoutedRequest({**payload, "model": self.coordinator or self.model, "reasoning": {"effort": "medium"}}, decision, "route123")


class EventStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"data: ok\n\n"


class UsageStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"event: response.completed\r\ndata: {\"type\":\"response.com"
        yield b"pleted\",\"response\":{\"usage\":{\"input_tokens\":12,\"output_tokens\":5,"
        yield b"\"input_tokens_details\":{\"cached_tokens\":3},\"output_tokens_details\":{\"reasoning_tokens\":2}}}}\r\n\r\n"


def test_http_usage_records_routed_stream_only(monkeypatch, tmp_path):
    original_client = httpx.AsyncClient
    monkeypatch.setenv("JEV_ROUTER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JEV_API_UPSTREAM_URL", "https://example.test/v1")
    monkeypatch.setattr(auto_proxy.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=UsageStream())
    )))
    with TestClient(auto_proxy.create_app(FixedRouter())) as client:
        routed = client.post("/v1/responses", json={"model": "jev-auto", "input": []})
        concrete = client.post("/v1/responses", json={"model": "gpt-6-sol", "input": []})
    assert routed.content == concrete.content
    assert len(recent_usage(tmp_path)) == 1
    assert recent_usage(tmp_path, "route123")[0] == {
        "timestamp": recent_usage(tmp_path)[0]["timestamp"], "route_key": "route123", "status": "completed",
        "input_tokens": 12, "cached_tokens": 3, "output_tokens": 5, "reasoning_tokens": 2,
    }


def test_http_failed_event_keeps_unknown_usage_null(monkeypatch, tmp_path):
    class FailedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"type":"response.failed","response":{"error":{"message":"private"}}}\n\n'

    original_client = httpx.AsyncClient
    monkeypatch.setenv("JEV_ROUTER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JEV_API_UPSTREAM_URL", "https://example.test/v1")
    monkeypatch.setattr(auto_proxy.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=FailedStream())
    )))
    with TestClient(auto_proxy.create_app(FixedRouter())) as client:
        assert client.post("/v1/responses", json={"model": "jev-auto", "input": []}).status_code == 200
    assert recent_usage(tmp_path)[0]["status"] == "failed"
    assert recent_usage(tmp_path)[0]["input_tokens"] is None
    assert "private" not in (tmp_path / "auto-usage.jsonl").read_text()


def test_sse_parser_bounds_oversized_event_then_recovers():
    parser = SSEOutcomeParser()
    parser.feed(b"data: " + b"x" * (300 * 1024) + b"\n\n")
    assert parser.outcome is None
    parser.feed(b'data: {"type":"response.completed","response":{}}\n\n')
    assert parser.outcome == ("completed", {
        "input_tokens": None, "cached_tokens": None, "output_tokens": None, "reasoning_tokens": None,
    })


def test_zstd_request_routes_and_preserves_authorization(monkeypatch, tmp_path):
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
    assert recent_usage(tmp_path, "route123")[0]["status"] == "unverified"
    assert observed["url"] == "https://example.test/v1/responses"
    assert observed["authorization"] == "Bearer secret"
    assert observed["body"]["model"] == "gpt-5.6-terra"
    assert observed["body"]["reasoning"] == {"effort": "medium"}
    assert "native Codex subagent per stream in parallel" in observed["body"]["instructions"]
    assert observed["body"]["input"] == payload["input"]


def test_existing_instructions_are_preserved_and_concrete_model_is_untouched(monkeypatch):
    bodies = []
    original_client = httpx.AsyncClient

    def upstream(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=EventStream())

    monkeypatch.setattr(auto_proxy.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(upstream)))
    monkeypatch.setenv("JEV_API_UPSTREAM_URL", "https://example.test/v1")
    with TestClient(auto_proxy.create_app(FixedRouter())) as client:
        assert client.post("/v1/responses", json={"model": "jev-auto", "instructions": "Keep project conventions", "input": []}).status_code == 200
        assert client.post("/v1/responses", json={"model": "gpt-6-sol", "instructions": "Keep project conventions", "input": []}).status_code == 200
    assert bodies[0]["instructions"].startswith("Keep project conventions\n\n[Jev Auto orchestration]")
    assert bodies[1] == {"model": "gpt-6-sol", "instructions": "Keep project conventions", "input": []}


def test_astra_recommendation_uses_safe_coordinator_and_asks_approval(monkeypatch):
    bodies = []
    original_client = httpx.AsyncClient

    def upstream(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=EventStream())

    monkeypatch.setattr(auto_proxy.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(upstream)))
    monkeypatch.setenv("JEV_API_UPSTREAM_URL", "https://example.test/v1")
    with TestClient(auto_proxy.create_app(FixedRouter("gpt-6-astra", coordinator="gpt-6-sol"))) as client:
        response = client.post("/v1/responses", json={"model": "jev-auto", "input": []})
    assert response.status_code == 200
    assert bodies[0]["model"] == "gpt-6-sol"
    assert "For this request, Jev recommended gpt-6-astra/medium" in bodies[0]["instructions"]
    assert "Ask for approval before spawning" in bodies[0]["instructions"]


def test_astra_is_blocked_before_upstream(monkeypatch):
    monkeypatch.setattr(auto_proxy.httpx, "AsyncClient", lambda **kwargs: (_ for _ in ()).throw(AssertionError("upstream called")))
    with TestClient(auto_proxy.create_app(FixedRouter("gpt-6-astra"))) as client:
        response = client.post("/v1/responses", json={"model": "jev-auto", "input": []})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "astra_approval_required"


def test_cli_compatibility_filter_does_not_reduce_desktop_candidates():
    assert auto_proxy._allowed_models("Codex Desktop/0.154.0 (codex_exec; 0.154.0)") == {"gpt-5.6-terra", "gpt-6-astra"}
    assert auto_proxy._allowed_models("Codex Desktop/0.157.0 (codex_exec; 0.157.0)") is None
    assert auto_proxy._allowed_models("Codex Desktop/0.154.0") is None


@pytest.mark.parametrize("binary", [False, True])
def test_websocket_jev_turn_adds_orchestration_policy(monkeypatch, binary):
    sent = []

    class Upstream:
        def __init__(self):
            self.events = asyncio.Queue()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, message):
            sent.append(json.loads(message))
            await self.events.put(json.dumps({"type": "response.completed"}))
            await self.events.put(None)

        def __aiter__(self):
            return self

        async def __anext__(self):
            item = await self.events.get()
            if item is None:
                raise StopAsyncIteration
            return item

    monkeypatch.setattr(auto_proxy.websockets, "connect", lambda *args, **kwargs: Upstream())
    with TestClient(auto_proxy.create_app(FixedRouter())) as client:
        with client.websocket_connect("/v1/responses") as ws:
            frame = {"response": {"model": "jev-auto", "input": [{"role": "user", "content": [{"type": "input_text", "text": "Implement two independent modules"}]}]}}
            if binary:
                ws.send_bytes(json.dumps(frame).encode())
            else:
                ws.send_json(frame)
            assert ws.receive_json()["type"] == "response.completed"
    assert sent[0]["response"]["model"] == "gpt-5.6-terra"
    assert "native Codex subagent per stream in parallel" in sent[0]["response"]["instructions"]


def test_websocket_tool_output_is_not_lost_when_user_text_is_reused(monkeypatch):
    sent = []

    class Upstream:
        def __init__(self):
            self.events = asyncio.Queue()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, message):
            sent.append(json.loads(message))
            if len(sent) == 2:
                await self.events.put(json.dumps({"type": "response.completed"}))
                await self.events.put(None)

        def __aiter__(self):
            return self

        async def __anext__(self):
            item = await self.events.get()
            if item is None:
                raise StopAsyncIteration
            return item

    monkeypatch.setattr(auto_proxy.websockets, "connect", lambda *args, **kwargs: Upstream())
    tool_result = {"type": "function_call_output", "call_id": "call_1", "output": "test passed"}
    with TestClient(auto_proxy.create_app(FixedRouter())) as client:
        with client.websocket_connect("/v1/responses") as ws:
            ws.send_json({"item": {"role": "user", "content": [{"type": "input_text", "text": "Fix the bug"}]}})
            ws.send_json({"response": {"model": "jev-auto", "input": [tool_result]}})
            assert ws.receive_json()["type"] == "response.completed"
    assert sent[1]["response"]["model"] == "gpt-5.6-terra"
    assert sent[1]["response"]["input"] == [tool_result]


def test_websocket_uses_connection_scoped_routing_identity_without_forwarding_it(monkeypatch):
    routed_keys = []
    sent = []

    class TrackingRouter(FixedRouter):
        async def route(self, payload, allowed_models=None):
            routed_keys.append(payload.get("prompt_cache_key"))
            return await super().route(payload, allowed_models)

    class Upstream:
        def __init__(self):
            self.events = asyncio.Queue()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, message):
            sent.append(json.loads(message))
            await self.events.put(json.dumps({"type": "response.completed"}))
            if len(sent) == 2:
                await self.events.put(None)

        def __aiter__(self):
            return self

        async def __anext__(self):
            item = await self.events.get()
            if item is None:
                raise StopAsyncIteration
            return item

    monkeypatch.setattr(auto_proxy.websockets, "connect", lambda *args, **kwargs: Upstream())
    with TestClient(auto_proxy.create_app(TrackingRouter())) as client:
        with client.websocket_connect("/v1/responses") as ws:
            ws.send_json({"response": {"model": "jev-auto", "input": [{"role": "user", "content": [{"type": "input_text", "text": "Fix bug"}]}]}})
            assert ws.receive_json()["type"] == "response.completed"
            ws.send_json({"response": {"model": "jev-auto", "input": [{"type": "function_call_output", "call_id": "1", "output": "ok"}]}})
            assert ws.receive_json()["type"] == "response.completed"

    assert len(routed_keys) == 2
    assert routed_keys[0] == routed_keys[1]
    assert routed_keys[0].startswith("ws:")
    assert all("prompt_cache_key" not in item["response"] for item in sent)
    assert sent[1]["response"]["input"] == [{"type": "function_call_output", "call_id": "1", "output": "ok"}]


@pytest.mark.parametrize("binary", [False, True])
def test_websocket_usage_is_recorded_from_terminal_event(monkeypatch, tmp_path, binary):
    class Upstream:
        def __init__(self):
            self.events = asyncio.Queue()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, message):
            event = {"type": "response.completed", "response": {"usage": {"input_tokens": 7, "output_tokens": 4}}}
            serialized = json.dumps(event)
            await self.events.put(serialized.encode() if binary else serialized)
            await self.events.put(None)

        def __aiter__(self):
            return self

        async def __anext__(self):
            item = await self.events.get()
            if item is None:
                raise StopAsyncIteration
            return item

    monkeypatch.setenv("JEV_ROUTER_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(auto_proxy.websockets, "connect", lambda *args, **kwargs: Upstream())
    with TestClient(auto_proxy.create_app(FixedRouter())) as client:
        with client.websocket_connect("/v1/responses") as ws:
            ws.send_json({"response": {"model": "jev-auto", "input": []}})
            event = ws.receive_bytes() if binary else ws.receive_text()
            assert json.loads(event)["type"] == "response.completed"
    assert recent_usage(tmp_path)[0]["input_tokens"] == 7
    assert recent_usage(tmp_path)[0]["cached_tokens"] is None


def test_orchestration_preserves_unknown_instruction_format():
    decision = Decision(Candidate("gpt-6-sol", "medium"), "test", None, 1, Profile("general", "focused", 0, (), False))
    payload = {"model": "gpt-6-sol", "instructions": ["unusual client format"], "input": []}
    assert orchestration.apply(payload, decision) == payload


def test_orchestration_upgrades_old_policy_in_existing_task_once():
    decision = Decision(Candidate("gpt-6-sol", "medium"), "test", None, 1, Profile("general", "focused", 0, (), False))
    payload = {"model": "gpt-6-sol", "instructions": "Existing\n\n[Jev Auto orchestration]\nOld policy", "input": []}
    updated = orchestration.apply(payload, decision)
    assert "user-visible behavior" in updated["instructions"]
    assert "Error-code changes alone are not recovery" in updated["instructions"]
    assert updated["instructions"].count(orchestration.BEHAVIOR_MARKER) == 1
    assert orchestration.apply(updated, decision) == updated


def test_orchestration_adds_replan_review_to_long_running_task():
    decision = Decision(Candidate("gpt-6-sol", "high"), "replan_review_open_jev_unavailable",
                        None, 3, Profile("backend", "focused", 0, (), False), "replan_review")
    existing = "Existing\n\n[Jev Auto orchestration]\nOld policy\n[Jev Auto behavior contract v1]\nOld behavior"
    payload = {"model": "gpt-6-sol", "instructions": existing, "input": []}
    updated = orchestration.apply(payload, decision, route_key="a" * 24)
    assert orchestration.REPLAN_MARKER in updated["instructions"]
    assert "Ask for approval before any Astra call" in updated["instructions"]
    assert orchestration.apply(updated, decision, route_key="a" * 24) == updated


def test_orchestration_delivers_astra_recommendation_to_existing_task():
    decision = Decision(Candidate("gpt-6-astra", "medium"), "open_jev_policy_ranked",
                        0.8, 20, Profile("backend", "broad", 0, (), False), "frontier_plan")
    payload = {"model": "gpt-6-sol", "instructions": "[Jev Auto orchestration]\nPrior policy\n"
               + orchestration.BEHAVIOR_MARKER + "\nOld behavior\n"
               + orchestration.REPLAN_MARKER + "\nCurrent review policy", "input": []}
    updated = orchestration.apply(payload, decision, route_key="b" * 24)
    assert "Jev recommended gpt-6-astra/medium" in updated["instructions"]
    assert "Ask for approval" in updated["instructions"]
    assert orchestration.apply(updated, decision, route_key="b" * 24) == updated
