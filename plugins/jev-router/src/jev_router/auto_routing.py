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
from uuid import uuid4

from .codex_runner import list_candidates
from .openjev_service import ensure_openjev
from .replanning import needs_replan_review
from .routing import Candidate, Decision, ImplementationContract, Profile, _eligible, _policy_band, choose, fallback, inspect_repo


VIRTUAL_MODEL = "jev-auto"


class CoordinatorUnavailable(RuntimeError):
    """An Astra recommendation cannot be handled without a non-Astra main model."""


def latest_user_text(payload: dict[str, Any]) -> str:
    """Codex resends the turn history after tools; keep the latest user prompt."""
    return next(iter(reversed(_user_texts(payload))), "")


def _user_texts(payload: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for item in payload.get("input") or []:
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        parts: list[str] = []
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") in ("input_text", "text"):
                value = content.get("text")
                if isinstance(value, str):
                    parts.append(value)
        if parts:
            texts.append("\n".join(parts).strip())
    return [value for value in texts if value]


def _is_followup(request: str) -> bool:
    """Only short, referential requests need earlier task text for routing."""
    text = request.strip().lower()
    return len(text) <= 80 and bool(re.search(
        r"^(?:이어서|계속|진행|수정\s*진행|그대로\s*진행|해\s*줘|해주세요|그거|그렇게|위(?:의)?\s*(?:내용|작업|대로))",
        text,
    ))


def _routing_request(request: str, previous: str) -> str:
    if not _is_followup(request) or not previous or previous == request:
        return request
    # Keep the routing input small and exclude any intervening assistant/tool text.
    return f"Previous user task: {previous[:1600]}\nCurrent user request: {request}"


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
        self._sessions: dict[tuple[str, str], tuple[str, str]] = {}
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

    async def decide(
        self, request: str, workspace: Path | None = None,
        allowed_models: set[str] | None = None, review_replan: bool = False,
        implementation_contract: ImplementationContract | None = None,
    ) -> Decision:
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
        review_candidates = [candidate for candidate in candidates if _eligible(candidate, "replan_review")]
        review_replan = review_replan and bool(review_candidates)
        ready = await ensure_openjev(self.jev_url, self.data_directory)
        if not ready:
            if review_replan:
                candidate = next((item for item in review_candidates if item.effort == "high"), review_candidates[0])
                return Decision(candidate, "replan_review_open_jev_unavailable", None, len(candidates), profile, "replan_review")
            return Decision(fallback(candidates), "open_jev_not_ready_fallback", None, len(candidates), profile)
        return await choose(request, profile, candidates, self.jev_url,
                            policy_band="replan_review" if review_replan else None,
                            implementation_contract=implementation_contract)

    async def route(self, payload: dict[str, Any], allowed_models: set[str] | None = None) -> RoutedRequest:
        user_texts = _user_texts(payload)
        request = user_texts[-1] if user_texts else ""
        allowed_key = ",".join(sorted(allowed_models)) if allowed_models is not None else "all"
        session_id = payload.get("prompt_cache_key")
        # A cache key is useful only when the client supplies a stable identity.
        session = (allowed_key, session_id) if isinstance(session_id, str) and session_id else None
        async with self._lock:
            previous = self._sessions.get(session) if session else None
            if previous:
                prior_route = self._routes.get(previous[1])
                if not prior_route or time.monotonic() - prior_route[0] >= 7200:
                    self._sessions.pop(session, None)
                    previous = None
            if request:
                prior_text = user_texts[-2] if len(user_texts) > 1 else (previous[0] if previous else "")
                scoring_request = _routing_request(request, prior_text)
                review_replan = (_policy_band(request, _generic_profile(request)) != "explicit_astra"
                                 and needs_replan_review(payload, request))
            else:
                scoring_request = ""
                review_replan = False
            if not request and previous and previous[1] in self._routes:
                route_key = previous[1]
            else:
                # Without a session identity, each call is independent. Its random
                # key prevents one task from inheriting another task's decision.
                identity = session_id if session else uuid4().hex
                cache_input = f"{allowed_key}\0{identity}\0{scoring_request}\0{int(review_replan)}".encode("utf-8")
                route_key = hashlib.sha256(cache_input).hexdigest()[:24]
            cached = self._routes.get(route_key) if session else None
            if cached and time.monotonic() - cached[0] < 7200:
                decision = cached[1]
                coordinator = cached[2]
            else:
                decision = await self.decide(scoring_request, allowed_models=allowed_models,
                                             review_replan=review_replan)
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
                if session:
                    self._routes[route_key] = (time.monotonic(), decision, coordinator)
                if len(self._routes) > 512:
                    oldest = sorted(self._routes, key=lambda key: self._routes[key][0])[:128]
                    for key in oldest:
                        self._routes.pop(key, None)
                    self._sessions = {
                        key: value for key, value in self._sessions.items()
                        if value[1] in self._routes
                    }
                self._record(route_key, decision, coordinator)
            if session and request:
                self._sessions[session] = (request, route_key)
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
