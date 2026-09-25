"""Local MCP entry point for the Jev Router plugin."""

from __future__ import annotations

from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel
from typing import Literal

from .jobs import JobManager


mcp = FastMCP(
    "jev-router",
    instructions=(
        "Call route_and_run once for a user-authorized coding task, then poll run_status. "
        "Astra only plans in read-only mode, then Sol implements; Astra waits for explicit user confirmation through run_astra. "
        "Never claim that an awaiting_confirmation run has started. "
        "When the user gives feedback about a past Jev Router run, locate it with recent_runs and save their words with record_feedback."
    ),
)
manager = JobManager()


class AstraConfirmation(BaseModel):
    confirm_astra_execution: bool


@mcp.tool()
async def route_and_run(request: str, workspace: str) -> dict:
    """Start routing a coding request and run the selected Codex configuration. workspace is an absolute path inside a Git repository."""
    return manager.start(request, workspace)


@mcp.tool()
def run_status(run_id: str) -> dict:
    """Get the selected configuration, execution status, result, usage, and diff path for a run."""
    return manager.status(run_id)


@mcp.tool()
def recent_runs(workspace: str | None = None, limit: int = 5) -> list[dict]:
    """List recent Jev Router runs, optionally in one Git workspace, to identify a run for later feedback."""
    return manager.recent_runs(workspace, limit)


@mcp.tool()
def record_feedback(
    run_id: str,
    feedback_text: str,
    aspect: Literal["overall", "routing", "code", "design"] = "overall",
) -> dict:
    """Save explicit user feedback on a Jev Router run across Codex conversations. Preserves the user's words; does not retrain or change routing policy."""
    return manager.record_feedback(run_id, feedback_text, aspect)


@mcp.tool()
def run_feedback(run_id: str) -> list[dict]:
    """Read user feedback already saved for a Jev Router run."""
    return manager.feedback_for_run(run_id)


@mcp.tool()
async def run_astra(run_id: str, ctx: Context) -> dict:
    """Request direct user confirmation before Astra plans; Sol implements afterward. Fails closed if confirmation UI is unavailable."""
    job = manager.status(run_id)
    decision = job.get("decision") or {}
    candidate = decision.get("candidate") or {}
    if job["state"] != "awaiting_confirmation" or candidate.get("model") != "gpt-6-astra":
        raise ValueError("run is not waiting for Astra confirmation")
    executor = job.get("executor_candidate")
    if not executor or executor.get("model") != "gpt-6-sol":
        return {"run_id": run_id, "state": "awaiting_confirmation", "message": "This run predates Astra plan-only routing; start a new run."}
    prompt = (
        f"Approve GPT-6 Astra ({candidate['effort']}) to plan this task without editing code? "
        f"GPT-6 Sol ({executor['effort']}) will implement the plan afterward. "
        f"Run: {run_id}. Workspace: {job['workspace']}. "
        f"Request: {job['request'][:400]}. "
        "This may consume substantially more model usage."
    )
    try:
        response = await ctx.elicit(prompt, schema=AstraConfirmation)
    except Exception as exc:
        return {
            "run_id": run_id,
            "state": "awaiting_confirmation",
            "message": f"Direct user confirmation is unavailable ({type(exc).__name__}). Astra was not started.",
        }
    if response.action != "accept" or not response.data or not response.data.confirm_astra_execution:
        return {"run_id": run_id, "state": "awaiting_confirmation", "message": "Astra was not approved."}
    return manager.launch_confirmed_astra(run_id)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
