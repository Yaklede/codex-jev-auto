"""Small adapter around the public Codex Python SDK."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openai_codex import ApprovalMode, AsyncCodex, Sandbox
from openai_codex.types import ReasoningEffort

from .routing import Candidate, available_candidates


async def list_candidates() -> list[Candidate]:
    async with AsyncCodex() as codex:
        catalog = await codex.models()
    return available_candidates(catalog.data)


def _result(thread: Any, result: Any) -> dict[str, Any]:
    status = getattr(result.status, "value", str(result.status))
    return {
        "thread_id": thread.id,
        "turn_id": result.id,
        "status": status,
        "final_response": result.final_response,
        "error": result.error.model_dump(mode="json") if result.error else None,
        "usage": result.usage.model_dump(mode="json") if result.usage else None,
        "duration_ms": result.duration_ms,
    }


async def plan_task(request: str, workspace: Path, candidate: Candidate) -> dict[str, Any]:
    """Use Astra for read-only direction finding after explicit confirmation."""
    if candidate.model != "gpt-6-astra":
        raise ValueError("planning model must be Astra")
    async with AsyncCodex() as codex:
        thread = await codex.thread_start(
            model=candidate.model,
            cwd=str(workspace),
            config={"model_reasoning_effort": candidate.effort},
            sandbox=Sandbox.read_only,
            approval_mode=ApprovalMode.deny_all,
            developer_instructions=(
                "Analyze this repository and write a concise implementation plan only. "
                "Do not modify files or invoke the jev-router plugin. "
                "Identify relevant files, design decisions, risks, and focused verification. "
                "The plan will be handed to another Codex model for implementation."
            ),
        )
        result = await thread.run(request, model=candidate.model, effort=ReasoningEffort(candidate.effort), cwd=str(workspace))
        return _result(thread, result)


async def execute_task(request: str, workspace: Path, candidate: Candidate) -> dict[str, Any]:
    async with AsyncCodex() as codex:
        thread = await codex.thread_start(
            model=candidate.model,
            cwd=str(workspace),
            config={"model_reasoning_effort": candidate.effort},
            sandbox=Sandbox.workspace_write,
            approval_mode=ApprovalMode.auto_review,
            developer_instructions=(
                "Complete the user's requested coding task in this workspace. "
                "Do not invoke the jev-router plugin from this delegated task. "
                "Respect existing files and project instructions. Report changes and tests."
            ),
        )
        result = await thread.run(
            request,
            model=candidate.model,
            effort=ReasoningEffort(candidate.effort),
            cwd=str(workspace),
        )
        return _result(thread, result)
