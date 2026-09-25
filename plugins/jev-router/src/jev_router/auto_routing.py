"""Route one Codex Responses turn to a concrete model and reasoning effort."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any

from .codex_runner import list_candidates
from .openjev_service import ensure_openjev
from .routing import Candidate, Decision, Profile, choose, fallback, inspect_repo


VIRTUAL_MODEL = "jev-auto"


class CoordinatorUnavailable(RuntimeError):
    """An Astra recommendation cannot be handled without a non-Astra main model."""


def latest_user_text(payload: dict[str, Any]) -> str:
    """Codex resends the turn history after tools; keep the latest user prompt."""
    for item in reversed(payload.get("input") or []):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        parts: list[str] = []
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") in ("input_text", "text"):
                value = content.get("text")
                if isinstance(value, str):
                    parts.append(value)
        if parts:
            return "\n".join(parts)
    return ""


def _generic_profile(request: str) -> Profile:
    lower = request.lower()
    front = any(term in lower for term in ("ui", "ux", "frontend", "design", "화면", "디자인", "프론트"))
    back = any(term in lower for term in ("api", "backend", "database", "service", "서버", "백엔드", "정산"))
    kind = "mixed" if front and back else "frontend" if front else "backend" if back else "general"
    broad = bool(re.search(r"architecture|migration|redesign|across services|아키텍처|마이그레이션|전면|전체|설계", lower))
    return Profile(kind, "broad" if broad else "focused", 0, (), False)


@dataclass(frozen=True)
class RoutedRequest:
    payload: dict[str, Any]
    decision: Decision
    route_key: str


class AutoRouter:
    def __init__(self, data_directory: Path, jev_url: str | None = None) -> None:
        self.data_directory = data_directory
        self.jev_url = jev_url or os.environ.get("OPENJEV_URL", "http://127.0.0.1:8000")
        self._routes: dict[str, tuple[float, Decision, Candidate]] = {}
        self._candidates: tuple[float, list[Candidate]] | None = None
        self._lock = asyncio.Lock()

    async def candidates(self) -> list[Candidate]:
        now = time.monotonic()
        if self._candidates and now - self._candidates[0] < 300:
            return self._candidates[1]
        try:
            candidates = await list_candidates()
        except Exception:
            candidates = [Candidate("gpt-5.6-terra", "medium")]
        if not candidates:
            candidates = [Candidate("gpt-5.6-terra", "medium")]
        self._candidates = (now, candidates)
        return candidates

    async def decide(self, request: str, workspace: Path | None = None, allowed_models: set[str] | None = None) -> Decision:
        candidates = await self.candidates()
        if allowed_models is not None:
            candidates = [candidate for candidate in candidates if candidate.model in allowed_models]
            if not candidates:
                raise RuntimeError("No supported Jev Auto model is available for this client")
        if workspace and workspace.is_dir():
            try:
                profile = inspect_repo(workspace, request)
            except Exception:
                profile = _generic_profile(request)
        else:
            profile = _generic_profile(request)
        if not request.strip():
            candidate = fallback(candidates)
            return Decision(candidate, "no_user_text_fallback", None, len(candidates), profile)
        ready = await ensure_openjev(self.jev_url, self.data_directory)
        if not ready:
            candidate = fallback(candidates)
            return Decision(candidate, "open_jev_not_ready_fallback", None, len(candidates), profile)
        return await choose(request, profile, candidates, self.jev_url)

    async def route(self, payload: dict[str, Any], allowed_models: set[str] | None = None) -> RoutedRequest:
        request = latest_user_text(payload)
        allowed_key = ",".join(sorted(allowed_models)) if allowed_models is not None else "all"
        cache_input = f"{allowed_key}\0{payload.get('prompt_cache_key', '')}\0{request}".encode("utf-8")
        route_key = hashlib.sha256(cache_input).hexdigest()[:24]
        async with self._lock:
            cached = self._routes.get(route_key)
            if cached and time.monotonic() - cached[0] < 7200:
                decision = cached[1]
                coordinator = cached[2]
            else:
                decision = await self.decide(request, allowed_models=allowed_models)
                if decision.candidate.requires_confirmation:
                    available = await self.candidates()
                    if allowed_models is not None:
                        available = [candidate for candidate in available if candidate.model in allowed_models]
                    try:
                        coordinator = fallback(available)
                    except RuntimeError as exc:
                        raise CoordinatorUnavailable("No non-Astra coordinator model is available") from exc
                else:
                    coordinator = decision.candidate
                self._routes[route_key] = (time.monotonic(), decision, coordinator)
                if len(self._routes) > 512:
                    oldest = sorted(self._routes, key=lambda key: self._routes[key][0])[:128]
                    for key in oldest:
                        self._routes.pop(key, None)
                self._record(route_key, decision, coordinator)
        concrete = dict(payload)
        concrete["model"] = coordinator.model
        reasoning = dict(concrete.get("reasoning") or {})
        reasoning["effort"] = coordinator.effort
        concrete["reasoning"] = reasoning
        return RoutedRequest(concrete, decision, route_key)

    def _record(self, route_key: str, decision: Decision, coordinator: Candidate) -> None:
        self.data_directory.mkdir(parents=True, exist_ok=True)
        record = {
            "time": time.time(),
            "route_key": route_key,
            "model": decision.candidate.model,
            "effort": decision.candidate.effort,
            "coordinator_model": coordinator.model,
            "coordinator_effort": coordinator.effort,
            "reason": decision.reason,
            "policy_band": decision.policy_band,
            "astra_confirmation_required": decision.candidate.requires_confirmation,
        }
        with (self.data_directory / "auto-decisions.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(record, ensure_ascii=False) + "\n")
