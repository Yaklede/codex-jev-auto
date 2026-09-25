"""Compare live Open Jev routing with provisional workload labels.

Run from the plugin environment:
    uv run python ../../scripts/evaluate_router.py --output ../../evals/baseline.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from jev_router.codex_runner import list_candidates
from jev_router.routing import Profile, choose


ROOT = Path(__file__).resolve().parents[1]


async def evaluate(case_path: Path) -> list[dict]:
    cases = json.loads(case_path.read_text(encoding="utf-8"))
    candidates = await list_candidates()
    results = []
    for case in cases:
        profile = Profile(**{**case["profile"], "languages": tuple(case["profile"]["languages"])})
        decision = await choose(case["request"], profile, candidates, "http://127.0.0.1:8000")
        candidate = decision.candidate
        acceptable = candidate.key in case["acceptable_pairs"] if "acceptable_pairs" in case else (
            candidate.model in case["acceptable_models"]
            and candidate.effort in case["acceptable_efforts"]
        )
        results.append({
            "id": case["id"],
            "split": case["split"],
            "selected": candidate.key,
            "workflow": "astra_plan_then_sol" if candidate.requires_confirmation else "direct_execution",
            "acceptable": acceptable,
            "reason": decision.reason,
            "score": decision.score,
            "compared": decision.compared,
        })
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cases", type=Path, default=ROOT / "evals/router_cases.json")
    args = parser.parse_args()
    results = asyncio.run(evaluate(args.cases))
    if any(row["reason"].endswith("fallback") for row in results):
        raise SystemExit("Open Jev fallback occurred; evaluation is invalid")
    for row in results:
        print(f"{row['split']:11} {row['id']:20} {row['selected']:24} {'OK' if row['acceptable'] else 'MISS'}")
    for split in ("development", "holdout"):
        group = [row for row in results if row["split"] == split]
        print(f"{split}: {sum(row['acceptable'] for row in group)}/{len(group)} provisional labels")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
