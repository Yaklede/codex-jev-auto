---
name: jev-auto
description: Use the existing Codex task as coordinator, score bounded coding work with local Open Jev, and delegate it to native subagents using the selected model and reasoning effort. Also applies when the user asks to invoke Jev Auto from their current task.
---

# Jev Auto

Keep the current task as the coordinator. Do not create a separate user task for delegated work. Decide whether there are genuinely independent bounded work streams before delegating. If two or more can run concurrently, start one native subagent per stream in parallel before doing stream-specific implementation or review in the main task; wait for their results and integrate them here. Parallel shell commands do not replace delegation. Keep one small or tightly coupled task in the main task. Assign disjoint files or isolate checkouts for parallel writers.

For each delegated bounded task, run `~/.local/share/jev-router/bin/jev-auto route '<task description>' --workspace '<absolute repository path>'`. Use the user's actual request and relevant repository context, without inventing complexity. Read the JSON `run_id`, `candidate.model`, `candidate.effort`, `reason`, and `candidate.requires_confirmation`. Keep the run ID associated with this bounded task.

If the route is Luna, Terra, or Sol, spawn a native subagent with exactly that model and reasoning effort. Pass a self-contained objective, the target repository, applicable project instructions, and completion criteria. Have the subagent report changed files and checks. Keep integration and final verification in this main task. Do not call Jev Auto recursively from a delegated agent.

If the route recommends Astra, explain the recommendation and cost, then request explicit user confirmation **before** an Astra call. Astra's assignment is planning only: inspect the repository, identify relevant files and risks, and return a concise implementation plan. Tell it not to edit files. After it returns, inspect the working tree for unexpected edits and hand the plan to a Sol implementation subagent. Use Sol high as the initial implementation setting unless the user directed otherwise. Do not send implementation work to Astra. If the user declines, use a Sol implementation subagent without Astra planning.

The catalog model `Jev Auto` is also available in the Codex model picker after installation and a supported app reload. Selecting it routes the **current task's turns** through the local Responses proxy and supplies the same coordination policy. Its main agent should decide whether native subagents are useful and spawn them without a separate skill invocation. The proxy alone cannot spawn agents; the main Codex agent must use its native collaboration tools. If Jev recommends Astra, the main turn uses a non-Astra model so it can ask for approval before an Astra planning subagent starts.

Local Open Jev scores option suitability, not task success probability. If it is unavailable, report the deterministic fallback. Treat model choice and actual code or design quality as separate questions; run project checks and inspect the result before completion.

After each routed task finishes, call `jev-auto finish <run_id> --status completed|failed|interrupted`, adding one `--check '<observed result>'` per check actually run and one `--agent <model> <effort> <agent-id>` per agent actually spawned. Record `completed` only after reviewing the subagent result; a passing model call alone does not prove a good implementation. Call `finish` even when the subagent fails or the user stops the work. Include the run ID in the final summary so later feedback can be attached. When the user gives feedback on an identifiable finished run, call `jev-auto feedback <run_id> --rating good|bad|mixed --note '<their feedback>'`. Keep their rating separate from observed tests and code review; do not treat it as a ground-truth quality label.
