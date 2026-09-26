"""Conservative signals for reviewing an unresolved, repeatedly attempted task."""

from __future__ import annotations

import re
from typing import Any


_FAILURE = re.compile(
    r"실패|해결되지|미해결|동작하지|통과하지|검증되지|목표.{0,5}미달|손실|"
    r"failed|unresolved|still broken|did not pass|not verified|loss",
    re.IGNORECASE,
)
_CONTINUATION = re.compile(
    r"(?:이전|기존).{0,50}(?:보완|고쳐|해결|재검토|다시|계속|목표)|"
    r"계속|다시|아직|여전히|보완|원하는 결과|목표.{0,20}(?:달성|이뤄|못)|"
    r"previous.{0,50}(?:fix|solve|continue|review)|continue|again|still|same goal|until it works",
    re.IGNORECASE,
)
_CORRECTION = re.compile(
    r"내가 원한|아직|여전히|계속.{0,20}(?:안|못|실패)|다시.{0,20}(?:고쳐|해결)|"
    r"not what I asked|still (?:broken|failing)|didn't fix",
    re.IGNORECASE,
)
_REPEATED = re.compile(
    r"(?:여러 번|몇 번|수차례|반복(?:해서|된)?|계속).{0,45}"
    r"(?:실패|해결.{0,3}못|안 됨|안돼|동작.{0,3}않|목표.{0,5}미달)|"
    r"(?:multiple|repeated|several).{0,45}(?:failed|attempts|fixes)",
    re.IGNORECASE,
)


def _messages(payload: dict[str, Any]) -> list[tuple[str, str]]:
    messages: list[tuple[str, str]] = []
    for item in payload.get("input") or []:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            continue
        parts = [part.get("text", "") for part in item.get("content") or []
                 if isinstance(part, dict) and part.get("type") in {"input_text", "output_text", "text"}
                 and isinstance(part.get("text"), str)]
        if parts:
            messages.append((item["role"], "\n".join(parts)[:4000]))
    return messages[-12:]


def needs_replan_review(payload: dict[str, Any], latest: str) -> bool:
    """Request a Sol-led review; never use a negative experiment to select Astra."""
    if _REPEATED.search(latest):
        return True
    if not _CONTINUATION.search(latest):
        return False
    prior = _messages(payload)
    if prior and prior[-1] == ("user", latest[:4000]):
        prior.pop()
    # Only explicit reports and user corrections count; tool errors and hypothesis
    # failures on their own are not evidence that a stronger model is needed.
    failures = sum(role == "assistant" and bool(_FAILURE.search(text)) for role, text in prior)
    corrections = sum(role == "user" and bool(_CORRECTION.search(text)) for role, text in prior)
    return failures >= 2 or (failures >= 1 and corrections >= 1)
