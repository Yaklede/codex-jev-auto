import json
import sys

from jev_router.auto_cli import main


def test_cli_joins_main_route_outcome_and_feedback(tmp_path, monkeypatch, capsys):
    route_key = "b" * 24
    (tmp_path / "auto-decisions.jsonl").write_text(json.dumps({
        "time": 1, "route_key": route_key, "model": "gpt-6-luna", "effort": "low",
        "reason": "open_jev_policy_ranked", "policy_band": "small",
    }) + "\n")
    monkeypatch.setenv("JEV_ROUTER_DATA_DIR", str(tmp_path))

    monkeypatch.setattr(sys, "argv", ["jev-auto", "auto-outcome", route_key, "--status", "completed", "--check", "pytest passed", "--revisions", "0"])
    main()
    assert json.loads(capsys.readouterr().out)["outcome"]["status"] == "completed"

    monkeypatch.setattr(sys, "argv", ["jev-auto", "auto-feedback", route_key, "--rating", "good", "--note", "No rework"])
    main()
    assert json.loads(capsys.readouterr().out)["feedback"]["source"] == "user_explicit"

    monkeypatch.setattr(sys, "argv", ["jev-auto", "auto-runs", "--limit", "1"])
    main()
    record = json.loads(capsys.readouterr().out)[0]
    assert record["route_key"] == route_key
    assert record["usage"]["input_tokens"] is None
    assert record["outcome"]["revisions"] == 0
