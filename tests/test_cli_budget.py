"""CLI: budget init/status/adjust/trip/reset/export and llm ping (mock), all offline."""

from __future__ import annotations

import json
import os

from eddie.cli import main
from eddie.config import load_settings


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def status_json(capsys):
    code, out, _ = run(capsys, "budget", "status", "--json")
    assert code == 0
    return json.loads(out)


def test_status_before_init(capsys):
    assert status_json(capsys)["initialized"] is False


def test_init_requires_explicit_dashboard_figure_without_tty(capsys):
    code, _, err = run(capsys, "budget", "init", "--yes")
    assert code == 2 and "dashboard" in err
    code, _, err = run(capsys, "budget", "init")  # no TTY -> cannot prompt
    assert code == 2


def test_full_budget_lifecycle(capsys, tmp_path):
    code, out, _ = run(capsys, "budget", "init", "--yes", "--dashboard-usd", "0.37")
    assert code == 0, out
    s = status_json(capsys)
    assert s["initialized"] and s["anchor"] == "ok"
    assert s["committed_usd"] == 0.37
    assert s["lifetime_cap_usd"] == 8.0

    code, _, err = run(capsys, "budget", "init", "--yes", "--dashboard-usd", "0")
    assert code == 2 and "already exists" in err

    assert run(capsys, "budget", "adjust", "--usd", "0.05", "--note", "playground test")[0] == 0
    assert status_json(capsys)["committed_usd"] == 0.42

    assert run(capsys, "budget", "trip", "--reason", "drill")[0] == 0
    settings = load_settings(dict(os.environ))
    assert settings.kill_switch_path.exists()
    assert status_json(capsys)["breaker"]["state"] == "open"
    assert run(capsys, "budget", "reset-breaker", "--yes")[0] == 0
    assert not settings.kill_switch_path.exists()
    assert status_json(capsys)["breaker"]["state"] == "closed"

    out_file = tmp_path / "trace.json"
    assert run(capsys, "budget", "export-trace", "--out", str(out_file))[0] == 0
    trace = json.loads(out_file.read_text())
    assert trace["totals"]["total_spend_usd"] == 0.42
    assert (settings.public_dir / "telemetry.json").exists()

    code, _, err = run(capsys, "budget", "set-lifetime-cap", "9.50", "--confirm", "9.50")
    assert code == 2
    assert run(capsys, "budget", "set-lifetime-cap", "5.00", "--confirm", "5.00")[0] == 0
    assert status_json(capsys)["lifetime_cap_usd"] == 5.0


def test_llm_ping_mock_never_touches_the_real_ledger(capsys):
    code, out, _ = run(capsys, "llm", "ping", "--region", "R1.triage", "--effort", "minimal", "--max-output-tokens", "16")
    assert code == 0, out
    assert "backend       mock" in out
    settings = load_settings(dict(os.environ))
    assert not settings.ledger_path.exists()
    assert settings.shadow_ledger_path.exists()


def test_llm_ping_refuses_bad_parameters(capsys):
    code, _, err = run(capsys, "llm", "ping", "--region", "R1.triage", "--effort", "none", "--max-output-tokens", "16")
    assert code == 2 and "none" in err
    code, _, err = run(capsys, "llm", "ping", "--region", "R1.triage", "--effort", "high", "--max-output-tokens", "16")
    assert code == 2


def test_raising_run_cap_needs_confirmation(capsys):
    code, _, err = run(capsys, "--run-cap-usd", "0.50", "budget", "status")
    assert code == 2 and "confirm" in err.lower()
    code, _, err = run(capsys, "--run-cap-usd", "0.50", "--confirm-run-cap", "0.50:1999-01-01", "budget", "status")
    assert code == 2


def test_ember_backend_without_init_is_refused(capsys, monkeypatch):
    monkeypatch.setenv("EDDIE_BACKEND", "ember")
    monkeypatch.setenv("EDDIE_ALLOW_LIVE_EMBER", "true")
    code, _, err = run(capsys, "llm", "ping", "--region", "R1.triage", "--effort", "minimal", "--max-output-tokens", "16")
    assert code == 2
    assert "pytest" in err or "anchor" in err
