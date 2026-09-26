"""Instructions attached only to turns that enter through the Jev Auto catalog model."""

from __future__ import annotations

from typing import Any

from .routing import Decision


MARKER = "[Jev Auto orchestration]"
BEHAVIOR_MARKER = "[Jev Auto behavior contract v1]"
BEHAVIOR_POLICY = (
    "For a broken mode or regression, define the user-visible behavior first. "
    "Trace UI, saved settings, environment wiring, runtime path, and prior working behavior. "
    "Determine mode semantics from product evidence, then verify the behavior where offered. "
    "Error-code changes alone are not recovery. Ask the user to choose an implementation "
    "only if the product contract remains ambiguous."
)
REPLAN_MARKER = "[Jev Auto replanning contract v1]"
REPLAN_POLICY = (
    "When the same user goal remains unmet after at least two materially different attempts or "
    "repeated user corrections, review the actual failures, acceptance criteria, and assumptions. "
    "Distinguish valid negative experiment results from an agent reasoning failure. If a new "
    "high-level plan could change the approach, explain the evidence and ask for approval for an "
    "Astra planning subagent. Never call Astra before approval; Astra plans only and Sol implements. "
    "If the available evidence or constraints cannot support the requested result, report that "
    "limit instead of promising success or repeatedly tuning against the same data."
)
QUALITY_FLOW_MARKER = "[Jev Auto staged quality flow v1]"
QUALITY_FLOW_POLICY = (
    "When Sol has resolved behavior and design decisions, write a short implementation contract "
    "with requirement, 1-4 allowed_paths, acceptance_checks, existing_patterns, "
    "decisions_resolved, risk, and ui_baseline for UI. Route that bounded implementation "
    "with jev-auto route '<subtask>' --workspace '<repo>' --contract-file '<json>' before "
    "delegating; Luna medium is eligible only for low-risk, fully specified work. "
    "After implementation, collect observed tests, acceptance results, quality-check output, "
    "and rendered-screen review for UI. Run jev-auto intent-review --contract-file '<json>' "
    "--evidence-file '<json>' to choose completion candidate, local fix, Sol review, or "
    "missing evidence. Jev's choice is advisory; the main agent verifies the actual result "
    "and must not complete with failed or missing required evidence. Keep tiny coupled edits local."
)
BASELINE_MARKER = "[Jev Auto quality baseline v1]"
BASELINE_POLICY = (
    "For repository edits, run quality-context with --save-baseline before editing and retain its "
    "baseline_id. Pass --baseline-id to quality-check after editing so unchanged pre-task files "
    "are excluded. If a pre-task edited file changes again, inspect it manually against the "
    "baseline; HEAD diffs can include earlier changes. If HEAD changes, capture a fresh baseline "
    "after reviewing the intervening commit. Do not treat missing baseline evidence as a clean review."
)


def apply(payload: dict[str, Any], decision: Decision, route_key: str | None = None) -> dict[str, Any]:
    """Keep existing Codex instructions and add a bounded delegation policy."""
    instruction = (
        f"{MARKER}\n"
        "Keep this task as coordinator. For independent bounded streams, run "
        "~/.local/share/jev-router/bin/jev-auto route '<subtask>' --workspace '<absolute repo>' "
        "before spawning one native Codex subagent per stream in parallel with the chosen concrete "
        "model and effort. Give agents disjoint files; wait, integrate, review, and record each "
        "agent/check with jev-auto finish. Work here for small or coupled tasks. Do not create "
        "user-facing tasks or recursively delegate. For code/UI edits, run jev-auto quality-context "
        "'<request>' --workspace '<repo>' before editing; note acceptance, existing patterns, "
        "and planned paths. After editing run jev-auto quality-check '<request>' --workspace "
        "'<repo>' with --allowed-path for planned paths. Review findings, diff, tests, and rendered "
        "screen states. For React or KMP/CMP UI, use quality-context with --focus-path to "
        "identify nearby components, theme, layout, and state patterns before implementation. "
        "After implementation, inspect the rendered screen at relevant sizes and states, "
        "compare its visual hierarchy with adjacent screens, and review framework-specific "
        "performance evidence; static findings alone do not prove design or speed. "
        "Direct JDBC in a JPA+QueryDSL repo needs an explicit reason and "
        "--allow-jdbc --jdbc-reason. Keep small edits small. Astra requires user approval; "
        "Astra plans only, then Sol implements. Follow higher-priority instructions."
        f"\n{BEHAVIOR_MARKER}\n{BEHAVIOR_POLICY}"
        f"\n{REPLAN_MARKER}\n{REPLAN_POLICY}"
        f"\n{QUALITY_FLOW_MARKER}\n{QUALITY_FLOW_POLICY}"
        f"\n{BASELINE_MARKER}\n{BASELINE_POLICY}"
    )
    if decision.candidate.requires_confirmation:
        instruction += (
            f"\nFor this request, Jev recommended {decision.candidate.model}/"
            f"{decision.candidate.effort} for planning. The current main turn uses a "
            "non-Astra coordinator model. Ask for approval before spawning the Astra "
            "planning subagent."
        )
    elif decision.policy_band == "replan_review":
        instruction += (
            "\nThis request has repeated-failure signals. Review the prior attempts before "
            "continuing. Decide from evidence whether Astra planning would help; if so, ask "
            "for user approval before any Astra call."
        )
    if route_key:
        instruction += (
            f"\nMain route key: {route_key}. For completed repository work, record observed checks "
            "with jev-auto auto-outcome ROUTE_KEY --status completed|failed|interrupted --check; "
            "pass --route-key to quality-check. For later feedback, find the earlier target via "
            "auto-runs before auto-feedback; "
            "unknown usage stays unknown."
        )
    updated = dict(payload)
    existing = updated.get("instructions")
    if isinstance(existing, str):
        if MARKER not in existing:
            updated["instructions"] = existing + "\n\n" + instruction
        else:
            # Long-running tasks may carry older policies. Add each revision once.
            additions = []
            if BEHAVIOR_MARKER not in existing:
                additions.append(f"{BEHAVIOR_MARKER}\n{BEHAVIOR_POLICY}")
            if REPLAN_MARKER not in existing:
                additions.append(f"{REPLAN_MARKER}\n{REPLAN_POLICY}")
            if QUALITY_FLOW_MARKER not in existing:
                additions.append(f"{QUALITY_FLOW_MARKER}\n{QUALITY_FLOW_POLICY}")
            if BASELINE_MARKER not in existing:
                additions.append(f"{BASELINE_MARKER}\n{BASELINE_POLICY}")
            if decision.candidate.requires_confirmation:
                approval_marker = f"[Jev Auto Astra recommendation {route_key or 'current'}]"
                if approval_marker not in existing:
                    additions.append(approval_marker + f"\nJev recommended {decision.candidate.model}/"
                                     f"{decision.candidate.effort} for planning. Ask for approval "
                                     "before spawning an Astra planning subagent.")
            elif decision.policy_band == "replan_review":
                review_marker = f"[Jev Auto replan review {route_key or 'current'}]"
                if review_marker not in existing:
                    additions.append(review_marker + "\nReview prior failures and decide whether "
                                     "Astra planning would help. Ask for approval before any Astra call.")
            if additions:
                updated["instructions"] = existing + "\n\n" + "\n".join(additions)
    elif existing is None:
        updated["instructions"] = instruction
    # The Responses request contract defines instructions as a string. Preserve
    # unexpected client formats unchanged rather than manufacturing an invalid list.
    return updated
