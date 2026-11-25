"""Eddie command-line interface.

    python -m eddie.cli budget status|init|export-trace|adjust|trip|reset-breaker|set-lifetime-cap
    python -m eddie.cli llm ping --region R1.triage --effort minimal --max-output-tokens 16
    python -m eddie.cli index build|stats|search|show|validate-spine
    python -m eddie.cli sandbox run SCRIPT.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path

from eddie import paths
from eddie.config import (
    CONTEST_HARD_CEILING_USD,
    RUN_CAP_UNCONFIRMED_MAX_USD,
    ConfigError,
    Settings,
    check_dated_confirmation,
    format_usd,
    load_settings,
    nano_to_usd,
    parse_usd,
    usd_to_nano,
)
from eddie.llm.errors import EddieLLMError, GuardError, LedgerIntegrityError, UpstreamError

Handler = Callable[[argparse.Namespace], int]


def _out(text: str = "") -> None:
    print(text)


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _ask(prompt: str) -> str:
    if not sys.stdin.isatty():
        raise ConfigError(f"needs interactive confirmation (no TTY): {prompt.strip()}")
    return input(prompt).strip()


def _settings(args: argparse.Namespace) -> Settings:
    cli_value = parse_usd(args.run_cap_usd, name="--run-cap-usd") if args.run_cap_usd else None
    confirmed = False
    if cli_value is not None and cli_value > RUN_CAP_UNCONFIRMED_MAX_USD:
        if args.confirm_run_cap:
            confirmed = check_dated_confirmation(cli_value, args.confirm_run_cap)
            if not confirmed:
                raise ConfigError("--confirm-run-cap must be <amount>:<today's date>, matching --run-cap-usd")
        else:
            typed = _ask(f"Raise this run's spend cap to ${cli_value}? Type {cli_value} to confirm: ")
            confirmed = typed == str(cli_value)
            if not confirmed:
                raise ConfigError("run cap not confirmed")
    return load_settings(run_cap_usd=cli_value, run_cap_confirmed=confirmed)


# --- budget ----------------------------------------------------------------------------------


def _open_real_ledger(settings: Settings):
    from eddie.llm.ledger import Ledger

    if not settings.ledger_path.exists():
        return None
    return Ledger(settings.ledger_path, billable=True)


def _open_shadow_ledger(settings: Settings):
    from eddie.llm.ledger import Ledger

    if not settings.shadow_ledger_path.exists():
        return None
    return Ledger(settings.shadow_ledger_path, billable=False)


def cmd_budget_status(args: argparse.Namespace) -> int:
    from eddie.llm.governor import build_status, caps_from_settings

    settings = _settings(args)
    caps = caps_from_settings(settings)
    real = _open_real_ledger(settings)
    shadow = _open_shadow_ledger(settings)
    report: dict = {"ledger_path": str(settings.ledger_path), "initialized": real is not None}
    if real is not None:
        report.update(build_status(real, caps, settings))
        try:
            real.verify_anchor(settings.anchor_path)
            report["anchor"] = "ok"
        except LedgerIntegrityError as exc:
            report["anchor"] = f"PROBLEM: {exc}"
    if shadow is not None:
        totals = shadow.totals()
        report["shadow"] = {
            "would_have_cost_usd": float(nano_to_usd(totals["shadow_nano"])),
            "calls": sum(totals["calls_by_state"].values()),
        }
    if args.json:
        _out(json.dumps(report, indent=2, default=str))
        return 0

    _out("Eddie Metabolic Spend Governor  (muse-ember-1.0)")
    _out(f"  ledger        {settings.ledger_path}")
    if real is None:
        _out("                NOT INITIALIZED: run `python -m eddie.cli budget init` before any live call")
    else:
        _out(f"  anchor        {report['anchor']}")
        _out(
            f"  lifetime cap  ${report['lifetime_cap_usd']:.6f}   committed ${report['committed_usd']:.6f}"
            f"   (adjustments ${report['adjustments_usd']:.6f})"
        )
        _out(
            f"  in flight     reserved ${report['inflight_reserved_usd']:.6f}"
            f"   worst case ${report['inflight_worst_case_usd']:.6f}   headroom ${report['headroom_usd']:.6f}"
        )
        _out(f"  rolling 24 h  ${report['daily_committed_usd']:.6f} of ${report['daily_cap_usd']:.6f}")
        _out(f"  breaker       {report['breaker']['state']}" + (f"  ({report['breaker']['reason']})" if report["breaker"]["reason"] else ""))
        _out(f"  calls         {report['calls_by_state']}   anomalous: {report['anomalous_calls']}")
    _out(f"  run cap       {format_usd(settings.run_cap_nano)} per process")
    _out(f"  kill switch   {'PRESENT (all Ember calls refused)' if settings.kill_switch_path.exists() else 'absent'}")
    if "shadow" in report:
        _out(
            f"  shadow        {report['shadow']['calls']} mock/dgx calls would have cost "
            f"${report['shadow']['would_have_cost_usd']:.6f} on Ember"
        )
    if real is not None and report.get("recent"):
        _out("  recent calls:")
        for r in report["recent"][:10]:
            _out(
                f"    {r['created_at']}  {r['region']:<16} {r['effort']:<7} {r['state']:<14} "
                f"in={r['input_tokens']} out={r['output_tokens']} ${r['cost_usd']:.6f}"
            )
    return 0


def cmd_budget_init(args: argparse.Namespace) -> int:
    from eddie.llm.governor import caps_from_settings, write_status_snapshot
    from eddie.llm.ledger import initialize_canonical_ledger

    settings = _settings(args)
    _out("Creating THE canonical Ember spend ledger for this machine.")
    _out(f"  ledger: {settings.ledger_path}\n  anchor: {settings.anchor_path}")
    if args.dashboard_usd is not None:
        dashboard = parse_usd(args.dashboard_usd, name="--dashboard-usd")
    elif args.yes:
        raise ConfigError("--yes requires an explicit --dashboard-usd (the spend Meta already shows)")
    else:
        dashboard = parse_usd(
            _ask("Ember spend Meta's dashboard already shows since Nov 23 (USD, e.g. 0.00): ") or "0",
            name="dashboard spend",
        )
    lifetime = parse_usd(args.lifetime_cap_usd, name="--lifetime-cap-usd") if args.lifetime_cap_usd else nano_to_usd(settings.lifetime_cap_nano)
    if lifetime > CONTEST_HARD_CEILING_USD:
        raise ConfigError(f"lifetime cap may not exceed ${CONTEST_HARD_CEILING_USD}")
    if not args.yes and _ask('Type INIT to create the one canonical ledger: ') != "INIT":
        raise ConfigError("not confirmed")
    ledger = initialize_canonical_ledger(
        settings.ledger_path,
        settings.anchor_path,
        lifetime_cap_nano=usd_to_nano(lifetime),
        opening_balance_nano=usd_to_nano(dashboard),
        note=args.note or "dashboard spend at init",
        other_anchor_paths=(paths.canonical_anchor_path(),),
        recover=args.recover,
    )
    write_status_snapshot(ledger, caps_from_settings(settings), settings)
    _out(f"OK: ledger anchored. Lifetime cap ${lifetime}, opening balance ${dashboard}.")
    return 0


def cmd_budget_export(args: argparse.Namespace) -> int:
    from eddie.llm.governor import caps_from_settings, effective_lifetime_cap_nano
    from eddie.llm.telemetry import build_trace, write_json_atomic

    settings = _settings(args)
    real = _open_real_ledger(settings)
    if real is None:
        raise ConfigError("no canonical ledger yet (run `budget init`)")
    shadow = _open_shadow_ledger(settings) if args.include_shadow else None
    cap = effective_lifetime_cap_nano(caps_from_settings(settings), real.totals())
    trace = build_trace(real, lifetime_cap_nano=cap, shadow=shadow)
    out = Path(args.out) if args.out else paths.repo_root() / "exports" / "eddie_ember_telemetry.json"
    write_json_atomic(out, trace)
    write_json_atomic(settings.public_dir / "telemetry.json", trace)
    _out(f"wrote {out} ({trace['totals']['calls']} billable calls, total ${trace['totals']['total_spend_usd']:.6f})")
    return 0


def cmd_budget_adjust(args: argparse.Namespace) -> int:
    from eddie.llm.governor import caps_from_settings, write_status_snapshot

    settings = _settings(args)
    real = _open_real_ledger(settings)
    if real is None:
        raise ConfigError("no canonical ledger yet (run `budget init`)")
    nano = usd_to_nano(parse_usd(args.usd, name="--usd"))
    real.add_adjustment(nano, kind="manual", note=args.note)
    real.raise_high_water(settings.anchor_path)
    write_status_snapshot(real, caps_from_settings(settings), settings)
    _out(f"recorded {format_usd(nano)} of external spend ({args.note})")
    return 0


def cmd_budget_trip(args: argparse.Namespace) -> int:
    from eddie.llm.governor import caps_from_settings, write_status_snapshot

    settings = _settings(args)
    settings.kill_switch_path.parent.mkdir(parents=True, exist_ok=True)
    settings.kill_switch_path.write_text(args.reason + "\n")
    real = _open_real_ledger(settings)
    if real is not None:
        real.trip(f"manual: {args.reason}")
        write_status_snapshot(real, caps_from_settings(settings), settings)
    _out(f"TRIPPED: kill switch {settings.kill_switch_path} written; every Ember call is refused.")
    return 0


def cmd_budget_reset(args: argparse.Namespace) -> int:
    from eddie.llm.governor import caps_from_settings, write_status_snapshot

    settings = _settings(args)
    real = _open_real_ledger(settings)
    breaker = real.breaker() if real is not None else {"state": "n/a", "reason": None}
    _out(f"breaker: {breaker['state']}  reason: {breaker['reason']}")
    _out(f"kill switch: {'present' if settings.kill_switch_path.exists() else 'absent'}")
    if not args.yes and _ask("Reviewed the reason above? Type RESET to close the breaker: ") != "RESET":
        raise ConfigError("not confirmed")
    if real is not None:
        real.reset_breaker()
        write_status_snapshot(real, caps_from_settings(settings), settings)
    settings.kill_switch_path.unlink(missing_ok=True)
    _out("breaker closed, kill switch removed")
    return 0


def cmd_budget_set_cap(args: argparse.Namespace) -> int:
    settings = _settings(args)
    value = parse_usd(args.amount, name="amount")
    if value > CONTEST_HARD_CEILING_USD:
        raise ConfigError(f"lifetime cap may not exceed ${CONTEST_HARD_CEILING_USD}")
    if args.confirm is None or parse_usd(args.confirm, name="--confirm") != value:
        raise ConfigError("--confirm must repeat the exact amount")
    real = _open_real_ledger(settings)
    if real is None:
        raise ConfigError("no canonical ledger yet (run `budget init`)")
    real.set_lifetime_cap(usd_to_nano(value))
    _out(f"lifetime cap set to ${value} (env EDDIE_LIFETIME_CAP_USD can still only lower it)")
    return 0


# --- llm ------------------------------------------------------------------------------------


def cmd_llm_ping(args: argparse.Namespace) -> int:
    from eddie.llm.gateway import EddieLLMGateway
    from eddie.llm.types import EddieRequest

    settings = _settings(args)
    req = EddieRequest.simple(
        args.prompt,
        brain_region=args.region,
        effort=args.effort,
        max_output_tokens=args.max_output_tokens,
        reasoning_summary=args.summary,
        tag="ping",
    )

    async def run():
        async with EddieLLMGateway(settings) as gateway:
            return await gateway.respond(req)

    resp = asyncio.run(run())
    _out(f"backend       {resp.backend}  (response_id {resp.response_id})")
    _out(f"text          {resp.text[:400]}")
    u = resp.usage
    _out(f"usage         input={u.input_tokens} cached={u.cached_tokens} output={u.output_tokens} reasoning={u.reasoning_tokens}")
    _out(f"cost          {format_usd(resp.cost_nano)}   (Ember-equivalent {format_usd(resp.shadow_nano)})")
    for warning in resp.warnings:
        _out(f"warning       {warning}")
    return 0


# --- parser ---------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m eddie.cli", description="Eddie: Lean 4 proof decompiler")
    parser.add_argument("--run-cap-usd", help="raise this process's spend cap (needs confirmation above $0.10)")
    parser.add_argument("--confirm-run-cap", help="non-interactive confirmation: <amount>:<YYYY-MM-DD>")
    sub = parser.add_subparsers(dest="cmd", required=True)

    budget = sub.add_parser("budget", help="spend governor").add_subparsers(dest="action", required=True)
    p = budget.add_parser("status", help="caps, spend, breaker, anchor")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_budget_status)
    p = budget.add_parser("init", help="create THE canonical ledger + anchor (once)")
    p.add_argument("--dashboard-usd", help="spend Meta's dashboard already shows since Nov 23")
    p.add_argument("--lifetime-cap-usd", help=f"default from env (8.00), max {CONTEST_HARD_CEILING_USD}")
    p.add_argument("--recover", action="store_true", help="re-create a lost ledger (needs --dashboard-usd >= high-water)")
    p.add_argument("--note")
    p.add_argument("--yes", action="store_true", help="skip the typed confirmation (requires --dashboard-usd)")
    p.set_defaults(func=cmd_budget_init)
    p = budget.add_parser("export-trace", help="write eddie_ember_telemetry.json (proof of usage)")
    p.add_argument("--out")
    p.add_argument("--include-shadow", action="store_true")
    p.set_defaults(func=cmd_budget_export)
    p = budget.add_parser("adjust", help="record spend made outside Eddie (playground, curl)")
    p.add_argument("--usd", required=True)
    p.add_argument("--note", required=True)
    p.set_defaults(func=cmd_budget_adjust)
    p = budget.add_parser("trip", help="kill switch: refuse every Ember call")
    p.add_argument("--reason", default="manual trip")
    p.set_defaults(func=cmd_budget_trip)
    p = budget.add_parser("reset-breaker", help="close the breaker and remove the kill switch")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_budget_reset)
    p = budget.add_parser("set-lifetime-cap", help="change the persisted lifetime cap")
    p.add_argument("amount")
    p.add_argument("--confirm", required=True)
    p.set_defaults(func=cmd_budget_set_cap)

    llm = sub.add_parser("llm", help="gateway utilities").add_subparsers(dest="action", required=True)
    p = llm.add_parser("ping", help="one tiny call through the full governor pipeline")
    p.add_argument("--region", required=True)
    p.add_argument("--effort", required=True)
    p.add_argument("--max-output-tokens", type=int, required=True)
    p.add_argument("--summary", choices=["auto", "concise", "detailed"])
    p.add_argument("--prompt", default="Reply with the single word: ready")
    p.set_defaults(func=cmd_llm_ping)

    for register in _EXTRA_COMMANDS:
        register(sub)
    return parser


_EXTRA_COMMANDS: list[Callable[[argparse._SubParsersAction], None]] = []


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, GuardError) as exc:
        _err(f"refused: {exc}")
        return 2
    except UpstreamError as exc:
        _err(f"upstream error: {exc}")
        return 3
    except EddieLLMError as exc:
        _err(f"error: {exc}")
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
