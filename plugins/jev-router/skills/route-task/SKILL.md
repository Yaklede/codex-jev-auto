---
name: route-task
description: Route a coding task through Jev Router, or save the user's feedback about a previous Jev Router task, including feedback given in a later Codex conversation.
---

# Route a coding task

1. Use the user's coding request verbatim and the absolute path of the target Git workspace. If the workspace is ambiguous, identify it from the current task context before calling the tool.
2. Call `jev-router.route_and_run` once. It returns a run ID while routing continues in the background.
3. Poll `jev-router.run_status` using that ID, with reasonable delays. Explain the selected model, effort, and routing reason when available. Do not perform the coding task again in this coordinating conversation.
4. If the state is `awaiting_confirmation`, explain that Astra will write a read-only plan and Sol will implement afterward, then call `jev-router.run_astra`. Its MCP elicitation asks the user directly and the tool cannot start Astra without an accepted response. If elicitation is unsupported or declined, leave the run pending and tell the user neither planning nor implementation started.
5. When complete, report the planner and executor task IDs when present, selected models and efforts, final result, usage when available, and the diff path. If it failed, report the actual error. The diff may include changes already present before this run; compare `before_status` and `after_status`.

## Feedback after a run

The runner automatically saves the model choice, execution state, usage when available, and Git state in a local run record. When the user later comments on a Jev Router result or model choice, record that explicit feedback without asking them to fill out a form.

1. If the matching run ID is in this conversation, use it. Otherwise call `jev-router.recent_runs`, filtering by the current Git workspace when known, and identify the matching task from its request and time. If several runs fit and the user did not identify one, ask which run they mean before recording.
2. Call `jev-router.record_feedback` once with the run ID and the user's feedback in their own words. Use `aspect=\"routing\"` for model/cost judgments, `code` for implementation quality, `design` for screen quality, and `overall` when the comment spans categories or is unclear. Do not manufacture a positive or negative verdict from silence.
3. Confirm that the feedback was saved. Explain that it is evidence for later calibration, not an immediate model-weight update or proof that a different model would have done better.

Do not record ordinary questions about how the router works as feedback. A new Codex conversation can reach the same local run records on the same computer when the plugin is available there.

Do not invoke this plugin recursively from a task started by the plugin. Do not claim that Open Jev's option probability is a success rate. Its ranking is advisory; the runner applies task-scope bounds and uses a conservative option within them when scoring fails or reverses under option reordering. Astra must never edit code in this workflow.
