"""Instructions attached only to turns that enter through the Jev Auto catalog model."""

from __future__ import annotations

from typing import Any

from .routing import Decision


MARKER = "[Jev Auto orchestration]"


def apply(payload: dict[str, Any], decision: Decision) -> dict[str, Any]:
    """Keep existing Codex instructions and add a bounded delegation policy."""
    instruction = (
        f"{MARKER}\n"
        "This is the user's existing main Codex task. Keep it as coordinator; do not create "
        "separate user-facing tasks. For each new work request, decide whether it contains "
        "genuinely independent, bounded work streams such as separate features, modules, "
        "or code reviews. If two or more such streams can run concurrently, spawn one "
        "native Codex subagent per stream in parallel before doing the stream-specific "
        "implementation or review in the main task. Wait for their results. Parallel "
        "shell commands by the main task do not replace subagent delegation. Do not "
        "require the user to invoke a plugin or repeat a delegation request. For one "
        "small or tightly coupled task, work directly in the main task. If native "
        "subagents are unavailable, continue in the main task and report that limit. "
        "Keep requirements, task division, integration, final code review, and verification "
        "in the main task. Avoid parallel agents editing the same files; assign disjoint "
        "ownership or isolate their checkouts. "
        "Before spawning each delegated subtask, run "
        "~/.local/share/jev-router/bin/jev-auto route '<subtask>' --workspace "
        "'<absolute repository path>'. Spawn with the "
        "returned concrete model and reasoning effort, never with the virtual jev-auto model. "
        "After reviewing the result, record its actual agent ID and checks with the same "
        "CLI's finish command. Do not recursively delegate from a subagent. "
        "If Astra is recommended, ask for explicit user approval before any Astra call; "
        "Astra only plans, then Sol implements. User instructions and higher-priority "
        "Codex policies take precedence."
    )
    if decision.candidate.requires_confirmation:
        instruction += (
            f"\nFor this request, Jev recommended {decision.candidate.model}/"
            f"{decision.candidate.effort} for planning. The current main turn uses a "
            "non-Astra coordinator model. Ask for approval before spawning the Astra "
            "planning subagent."
        )
    updated = dict(payload)
    existing = updated.get("instructions")
    if isinstance(existing, str):
        if MARKER not in existing:
            updated["instructions"] = existing + "\n\n" + instruction
    elif existing is None:
        updated["instructions"] = instruction
    # The Responses request contract defines instructions as a string. Preserve
    # unexpected client formats unchanged rather than manufacturing an invalid list.
    return updated
