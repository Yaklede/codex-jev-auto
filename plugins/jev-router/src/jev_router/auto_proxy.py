"""Local Responses API proxy used by the Jev Auto catalog model."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import gzip
import asyncio
import re
from collections import deque
from uuid import uuid4

import httpx
import zstandard
import websockets
from websockets.exceptions import ConnectionClosed
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .auto_routing import AutoRouter, CoordinatorUnavailable, VIRTUAL_MODEL, latest_user_text
from .auto_telemetry import SSEOutcomeParser, outcome_from_event, record_usage
from .orchestration import apply as apply_orchestration


_HOP_HEADERS = {
    "connection", "content-length", "transfer-encoding", "host", "keep-alive",
    "proxy-authenticate", "proxy-authorization", "te", "trailer", "upgrade",
}
_RETURN_HEADERS = {"content-type", "content-encoding", "cache-control", "x-request-id", "openai-processing-ms", "openai-version"}


def data_directory() -> Path:
    return Path(os.environ.get("JEV_ROUTER_DATA_DIR", Path.home() / ".local/share/jev-router")).expanduser()


def _upstream(request: Request) -> str:
    if request.headers.get("chatgpt-account-id"):
        return os.environ.get("JEV_CHATGPT_UPSTREAM_URL", "https://chatgpt.com/backend-api/codex")
    return os.environ.get("JEV_API_UPSTREAM_URL", "https://api.openai.com/v1")


def _base(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Upstream must be a fixed HTTPS base URL")
    return url.rstrip("/")


def _allowed_models(user_agent: str | None) -> set[str] | None:
    """The tested 0.154 codex_exec client lists GPT-6 Sol/Luna but cannot call them."""
    if "codex_exec" not in (user_agent or ""):
        return None
    match = re.search(r"Codex Desktop/(\d+)\.(\d+)\.(\d+)", user_agent or "")
    if match and tuple(map(int, match.groups())) < (0, 156, 0):
        return {"gpt-5.6-terra", "gpt-6-astra"}
    return None


async def health(_: Request) -> Response:
    return JSONResponse({"ok": True, "model": VIRTUAL_MODEL})


async def models(_: Request) -> Response:
    return JSONResponse({"object": "list", "data": [{"id": VIRTUAL_MODEL, "object": "model", "owned_by": "jev-router"}]})


async def _close(upstream: httpx.Response, client: httpx.AsyncClient) -> None:
    await upstream.aclose()
    await client.aclose()


def _record_route(route_key: str, status: str, usage: dict | None = None) -> None:
    try:
        record_usage(data_directory(), route_key, status, usage)
    except OSError:
        # Observability must never break the Responses stream.
        pass


async def _observed_stream(upstream: httpx.Response, route_key: str):
    parser = SSEOutcomeParser() if upstream.headers.get("content-encoding", "identity").lower() == "identity" else None
    status = "unverified" if 200 <= upstream.status_code < 300 else "failed"
    try:
        async for chunk in upstream.aiter_raw():
            if parser is not None:
                parser.feed(chunk)
            yield chunk
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except GeneratorExit:
        status = "cancelled"
        raise
    except Exception:
        status = "failed"
        raise
    finally:
        outcome = parser.outcome if parser is not None else None
        _record_route(route_key, *(outcome if status == "unverified" and outcome is not None else (status, None)))


async def responses(request: Request) -> Response:
    routed = None
    try:
        body = await request.body()
        encoding = request.headers.get("content-encoding", "identity").lower()
        if encoding == "zstd":
            body = zstandard.ZstdDecompressor().decompress(body, max_output_size=32 * 1024 * 1024)
        elif encoding == "gzip":
            body = gzip.decompress(body)
        elif encoding != "identity":
            return JSONResponse({"error": {"code": "unsupported_encoding", "message": encoding}}, status_code=415)
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, zstandard.ZstdError, OSError):
        return JSONResponse({"error": {"code": "invalid_json", "message": "Expected a JSON Responses request"}}, status_code=400)
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
        return JSONResponse({"error": {"code": "invalid_request", "message": "Missing model"}}, status_code=400)
    if payload["model"] == VIRTUAL_MODEL:
        try:
            routed = await request.app.state.router.route(payload, _allowed_models(request.headers.get("user-agent")))
        except CoordinatorUnavailable as exc:
            return JSONResponse({"error": {"code": "coordinator_unavailable", "message": str(exc)}}, status_code=409)
        if routed.payload["model"] == "gpt-6-astra":
            return JSONResponse(
                {"error": {
                    "code": "astra_approval_required",
                    "message": "Jev Auto could not find a non-Astra coordinator model for this request.",
                    "route_key": routed.route_key,
                    "recommended_effort": routed.decision.candidate.effort,
                }},
                status_code=409,
            )
        payload = apply_orchestration(routed.payload, routed.decision, route_key=routed.route_key)
    try:
        upstream_base = _base(_upstream(request))
    except ValueError as exc:
        return JSONResponse({"error": {"code": "invalid_upstream", "message": str(exc)}}, status_code=500)
    headers = {name: value for name, value in request.headers.items() if name.lower() not in _HOP_HEADERS | {"content-encoding"}}
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=20, read=None, write=60, pool=20), follow_redirects=False)
    try:
        upstream_request = client.build_request(
            "POST", f"{upstream_base}/responses", headers=headers,
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )
        upstream = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        if routed is not None:
            _record_route(routed.route_key, "failed")
        return JSONResponse({"error": {"code": "upstream_unavailable", "message": type(exc).__name__}}, status_code=502)
    result_headers = {name: value for name, value in upstream.headers.items() if name.lower() in _RETURN_HEADERS}
    return StreamingResponse(
        _observed_stream(upstream, routed.route_key) if routed is not None else upstream.aiter_raw(), status_code=upstream.status_code,
        headers=result_headers, background=BackgroundTask(_close, upstream, client),
    )


async def websocket_responses(websocket: WebSocket) -> None:
    try:
        upstream_base = _base(_upstream(websocket))
        upstream_url = "wss://" + upstream_base.removeprefix("https://") + "/responses"
        if websocket.url.query:
            upstream_url += "?" + websocket.url.query
        headers = {
            name: value for name, value in websocket.headers.items()
            if name.lower() not in _HOP_HEADERS
            | {"origin", "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-protocol"}
        }
        async with websockets.connect(
            upstream_url, additional_headers=headers, max_size=None, ping_interval=None, open_timeout=20,
        ) as upstream:
            await websocket.accept()
            last_user_text = ""
            connection_route_id = "ws:" + uuid4().hex
            pending_routes: deque[str | None] = deque()

            async def client_to_upstream() -> None:
                nonlocal last_user_text
                while True:
                    event = await websocket.receive()
                    if event["type"] == "websocket.disconnect":
                        return
                    message = event.get("text") if event.get("text") is not None else event.get("bytes")
                    if message is None:
                        continue
                    if isinstance(message, (str, bytes)):
                        binary = isinstance(message, bytes)
                        try:
                            frame = json.loads(message)
                        except (ValueError, TypeError):
                            frame = None
                        if isinstance(frame, dict):
                            item = frame.get("item")
                            if isinstance(item, dict):
                                observed = latest_user_text({"input": [item]})
                                if observed:
                                    last_user_text = observed
                            response = frame.get("response") if isinstance(frame.get("response"), dict) else frame
                            if isinstance(response.get("model"), str) and response.get("model") != VIRTUAL_MODEL and response.get("generate") is not False:
                                pending_routes.append(None)
                            if response.get("model") == VIRTUAL_MODEL:
                                if not response.get("input") and response.get("generate") is False:
                                    # Codex opens a WebSocket session before sending the real user turn.
                                    response["model"] = "gpt-5.6-terra"
                                    await upstream.send(json.dumps(frame, ensure_ascii=False, separators=(",", ":")))
                                    continue
                                route_input = response
                                synthetic_input = not latest_user_text(response) and bool(last_user_text)
                                synthetic_session = not response.get("prompt_cache_key")
                                if synthetic_input or synthetic_session:
                                    route_input = dict(response)
                                if synthetic_input:
                                    route_input["input"] = [{"role": "user", "content": [{"type": "input_text", "text": last_user_text}]}]
                                if synthetic_session:
                                    route_input["prompt_cache_key"] = connection_route_id
                                try:
                                    routed = await websocket.app.state.router.route(route_input, _allowed_models(websocket.headers.get("user-agent")))
                                except CoordinatorUnavailable:
                                    await websocket.close(code=1008, reason="No non-Astra coordinator is available")
                                    return
                                if routed.payload["model"] == "gpt-6-astra":
                                    await websocket.close(code=1008, reason="Astra planning needs user confirmation")
                                    return
                                routed_payload = apply_orchestration(routed.payload, routed.decision, route_key=routed.route_key)
                                if synthetic_input:
                                    # The synthetic user text is only for routing. Preserve
                                    # any real tool output already present in this frame.
                                    if "input" in response:
                                        routed_payload["input"] = response["input"]
                                    else:
                                        routed_payload.pop("input", None)
                                if synthetic_session:
                                    routed_payload.pop("prompt_cache_key", None)
                                if response is frame:
                                    frame = routed_payload
                                else:
                                    frame["response"] = routed_payload
                                serialized = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
                                message = serialized.encode("utf-8") if binary else serialized
                                pending_routes.append(routed.route_key)
                    await upstream.send(message)

            async def upstream_to_client() -> None:
                async for message in upstream:
                    if pending_routes and len(message) <= 256 * 1024:
                        try:
                            event = json.loads(message)
                        except (ValueError, TypeError, UnicodeDecodeError):
                            event = None
                        outcome = outcome_from_event(event)
                        if outcome is not None:
                            route_key = pending_routes.popleft()
                            if route_key is not None:
                                _record_route(route_key, *outcome)
                    if isinstance(message, str):
                        await websocket.send_text(message)
                    else:
                        await websocket.send_bytes(message)

            tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            while pending_routes:
                route_key = pending_routes.popleft()
                if route_key is not None:
                    _record_route(route_key, "cancelled")
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc and not isinstance(exc, (WebSocketDisconnect, ConnectionClosed)):
                    raise exc
    except (OSError, ValueError, websockets.WebSocketException):
        try:
            await websocket.close(code=1011)
        except RuntimeError:
            pass


def create_app(router: AutoRouter | None = None) -> Starlette:
    app = Starlette(routes=[
        Route("/healthz", health, methods=["GET"]),
        Route("/v1/models", models, methods=["GET"]),
        Route("/v1/responses", responses, methods=["POST"]),
        WebSocketRoute("/v1/responses", websocket_responses),
    ])
    app.state.router = router or AutoRouter(data_directory())
    return app
