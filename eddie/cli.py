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


# --- index ----------------------------------------------------------------------------------


def _index_path(args: argparse.Namespace) -> Path:
    return Path(args.index) if getattr(args, "index", None) else load_settings().index_path


def _open_index(args: argparse.Namespace):
    from eddie.ingest.index_db import connect_ro

    path = _index_path(args)
    if not path.exists():
        raise ConfigError(f"no index at {path}: run `python -m eddie.cli index build --src DIR`")
    return connect_ro(path)


def cmd_index_build(args: argparse.Namespace) -> int:
    from eddie.ingest.build import build_index

    archive_sha = None
    manifest = Path(args.src).parent / "FETCH_MANIFEST.json"
    if manifest.exists():
        archive_sha = json.loads(manifest.read_text()).get("joined_sha256")
    build_index(Path(args.src), _index_path(args), workers=args.workers, archive_sha256=archive_sha)
    return 0


def cmd_index_stats(args: argparse.Namespace) -> int:
    from eddie.ingest.index_db import stats

    with _open_index(args) as con:
        st = stats(con, output_tokens_per_decl=args.output_tokens)
    if args.json:
        _out(json.dumps(st, indent=2))
        return 0
    m = st["meta"]
    _out(f"index         {_index_path(args)}  (parser v{m['parser_version']}, built {m['built_at']}, {m['timing_total_s']} s)")
    _out(f"source        {st['modules']:,} modules, {st['source_lines']:,} lines, {st['source_bytes'] / 1e9:.3f} GB")
    _out(f"declarations  {st['decls']:,}  " + ", ".join(f"{k}={v:,}" for k, v in st["decls_by_kind"].items()))
    _out(f"sorry         {st['sorry_count']}   files with parser anomalies: {st['files_with_anomalies']}")
    _out(f"imports       {st['external_modules']:,} distinct external modules (" + ", ".join(f"{r}={c:,}" for r, c in st["external_roots"]) + ")")
    _out(f"graph         reachable from Main: {st['reachable_from_main']:,} / {st['modules']:,}   max depth {st['max_depth']}   max height {st['max_height']}   in cycles {st['modules_in_cycles']}")
    _out("top PageRank (foundations):")
    for name, pr, indeg in st["top_pagerank"]:
        _out(f"    {pr:.6f}  in={indeg:<6} {name}")
    f = st["forecast_r1_all_signatures"]
    _out("forecast: ONE Ember R1.triage pass over every signature")
    _out(f"    signatures      {f['signature_bytes'] / 1e6:,.1f} MB")
    _out(f"    input           ${f['input_usd_typical']:,.2f} typical (~3.5 B/token), ${f['input_usd_upper_bound']:,.2f} upper bound")
    _out(f"    output          ${f['output_usd_at_tokens_per_decl']:,.2f} at {f['output_tokens_per_decl']} tokens/decl")
    _out(f"    total           ${f['total_usd_typical']:,.2f} typical   vs lifetime cap $8.00: the $0 index must choose what Ember sees")
    return 0


def cmd_index_search(args: argparse.Namespace) -> int:
    from eddie.ingest.index_db import search

    with _open_index(args) as con:
        rows = search(con, args.query, limit=args.limit, kind=args.kind)
    for r in rows:
        _out(f"{r['kind']:<9} {r['full_name'] or '(anonymous)'}   [{r['module']}]")
        _out(f"          {r['signature'][:160]}")
    if not rows:
        _out("no matches")
    return 0


def cmd_index_show(args: argparse.Namespace) -> int:
    from eddie.ingest.index_db import load_body, lookup

    with _open_index(args) as con:
        rows = lookup(con, args.name)
        if not rows:
            _out("not found")
            return 1
        for r in rows[: args.max]:
            _out(f"{r['kind']} {r['full_name']}   [{r['module']}  {r['path']}:{r['start_line']}-{r['end_line']}]")
            if r["context"]:
                _out(f"  context: {r['context'][:300]}")
            _out(load_body(con, r) if not args.signature_only else r["signature"])
            _out("")
    return 0


def cmd_index_validate(args: argparse.Namespace) -> int:
    from eddie.ingest.index_db import validate_against_spine

    roots = None
    stats_json = Path(args.spine).with_name("stats.json")
    if stats_json.exists():
        roots = [r[0] for r in json.loads(stats_json.read_text()).get("project_namespaces", [])]
    with _open_index(args) as con:
        report = validate_against_spine(con, Path(args.spine), roots)
    _out(json.dumps(report, indent=2))
    return 0


# --- cone (declaration-level proof cone, pillars) ---------------------------------------------


def _cone_path(args: argparse.Namespace) -> Path:
    return Path(args.cone) if getattr(args, "cone", None) else load_settings().data_dir / "index" / "eddie_cone.sqlite"


def cmd_cone_build(args: argparse.Namespace) -> int:
    from eddie.ingest.cone import build_cone

    settings = load_settings()
    raw = settings.data_dir / "raw"
    spine = Path(args.spine) if args.spine else raw / "p0" / "spine.tsv.gz"
    pdf = Path(args.pdf) if args.pdf else raw / "pdfs" / "remark.pdf"
    build_cone(
        Path(args.index) if args.index else settings.index_path,
        _cone_path(args),
        spine_path=spine if spine.exists() else None,
        pdf_path=pdf if pdf.exists() else None,
        workers=args.workers,
    )
    return cmd_cone_stats(args)


def cmd_cone_stats(args: argparse.Namespace) -> int:
    from eddie.ingest.cone import cone_stats

    path = _cone_path(args)
    if not path.exists():
        raise ConfigError(f"no cone at {path}: run `python -m eddie.cli cone build`")
    st = cone_stats(path)
    m = st["meta"]
    if getattr(args, "json", False):
        _out(json.dumps(st, indent=2))
        return 0
    decls, live, dead = int(m["decls"]), int(m["live"]), int(m["dead"])
    _out(f"cone          {path}  (built {m['built_at']}, {m['timing_total_s']} s)")
    _out(f"liveness      {live:,} live / {decls:,} decls ({100 * live / max(decls, 1):.1f}%), {dead:,} dead pruned  [oracle: {m['liveness_source']}]")
    if m["agreement"]:
        a = m["agreement"]
        _out(f"graph check   our reference graph reaches {a['graph_live']:,}: precision {a['precision']:.3f}, recall {a['recall']:.3f} vs the spine")
    _out(f"edges         {int(m['edges']):,} reference edges ({int(m['weak_edges']):,} weak), {int(m['live_edges']):,} inside the live cone; max depth {m['max_depth']}")
    _out(f"roots         {', '.join(m['roots'])}")
    _out(f"pillars       {m['pillars']}  (PDF timeline: {m['timeline_matched']}/{m['timeline_steps']} steps matched)")
    for p in st["pillars"]:
        sub = f"  ~ {p['subtitle']}" if p["subtitle"] else ""
        _out(f"  {p['ord']:>2}. {p['title'][:52]:<52} live {p['live']:>8,}  dead {p['dead']:>7,}  depth~{(p['median_depth'] or 0):.0f}{sub[:90]}")
    return 0


# --- plan-budget ------------------------------------------------------------------------------


def cmd_plan_budget(args: argparse.Namespace) -> int:
    from dataclasses import replace

    from eddie.llm.telemetry import write_json_atomic
    from eddie.planner.budget import PlanParams, build_plan
    from eddie.state.artifacts import ArtifactStore

    settings = load_settings()
    base = PlanParams()
    params = replace(
        base,
        target_usd=args.target_usd,
        tier2_usd=args.tier2_usd,
        reserve_demo_usd=args.reserve_demo_usd,
        lifetime_cap_usd=float(nano_to_usd(settings.lifetime_cap_nano)),
        daily_cap_usd=float(nano_to_usd(settings.daily_cap_nano)),
        concurrency=args.concurrency,
        fit=args.fit,
        start_date=args.start_date or "",
        worker=replace(base.worker, max_output=args.worker_max_output),
        critic=replace(base.critic, max_output=args.critic_max_output),
        arbiter=replace(base.arbiter, max_output=args.arbiter_max_output, effort=args.arbiter_effort),
        consolidator=replace(base.consolidator, max_output=args.consolidator_max_output),
        triage=replace(base.triage, max_output=args.triage_max_output),
    )
    cone = Path(args.cone) if args.cone else settings.data_dir / "index" / "eddie_cone.sqlite"
    if not cone.exists():
        raise ConfigError(f"no cone at {cone}: run `python -m eddie.cli cone build` first")
    plan = build_plan(settings.index_path, cone, params)
    out = Path(args.out) if args.out else settings.data_dir / "index" / "work_plan.json"
    write_json_atomic(out, plan)
    if not args.no_db:
        ArtifactStore(settings.data_dir / "state" / "eddie_artifacts.sqlite").replace_pending_plan(plan["plan_hash"], plan["work_items"])
    t, b = plan["totals"], plan["budget"]
    _out(f"plan {plan['plan_hash']}  fit={plan['fit']}  -> {out}")
    _out(f"  Tier 1  $0      {plan['tier1']['modules']:,} modules, {plan['tier1']['decls']:,} decls mapped; {plan['tier1']['dead']:,} dead pruned")
    _out(f"  Tier 2  R1.triage   {t['tier2']['decls']:,} live decls in {t['tier2']['batches']:,} batches      worst ${t['tier2']['worst_usd']:.4f}  expected ${t['tier2']['expected_usd']:.4f}  (budget ${b['tier2_usd']:.2f})")
    _out(f"  Tier 3  R1->R2->R3  {t['tier3']['milestones']:,} milestones ({t['tier3']['mandatory']} mandatory chain), {t['tier3']['calls']:,} calls  worst ${t['tier3']['worst_usd']:.4f}  expected ${t['tier3']['expected_usd']:.4f}  (budget ${b['tier3_usd']:.2f}, retry reserve ${t['tier3']['retry_reserve_usd']:.2f})")
    _out(f"  Tier 4  R4.consolidator  {t['tier4']['calls']} calls      worst ${t['tier4']['worst_usd']:.4f}  expected ${t['tier4']['expected_usd']:.4f}")
    _out(f"  planned ${plan['planned_usd']:.4f} (worst ${plan['planned_worst_usd']:.4f}, expected ${plan['planned_expected_usd']:.4f}) of target ${b['target_usd']:.2f}")
    c = plan["coverage"]
    _out(f"  coverage  pillars {c['pillars_with_milestones']}/{c['pillars_total']}, roots included: {c['roots_included']}, dominator chain closed: {c['dominator_chain_closed']}")
    _out(f"  lifetime  cap ${b['lifetime_cap_usd']:.2f}, W-tail ${b['w_tail_usd']:.3f} at concurrency {params.concurrency} -> usable ${b['usable_lifetime_usd']:.3f}; demo reserve ${b['reserve_demo_usd']:.2f}")
    _out("  schedule  " + ", ".join(f"{d['date']} ${d['reserve_usd']:.2f}" for d in plan["schedule"]))
    for w in plan["warnings"]:
        _out(f"  WARNING   {w}")
    return 0


# --- seed-demo --------------------------------------------------------------------------------


def cmd_seed_demo(args: argparse.Namespace) -> int:
    from eddie.demo.seed import SeedParams, seed_demo

    settings = load_settings()
    cone = Path(args.cone) if args.cone else settings.data_dir / "index" / "eddie_cone.sqlite"
    if not cone.exists():
        raise ConfigError(f"no cone at {cone}: run `python -m eddie.cli cone build` first")
    report = seed_demo(
        cone_path=cone,
        index_path=settings.index_path,
        db_path=settings.data_dir / "state" / "eddie_artifacts.sqlite",
        artifact_dir=settings.data_dir / "artifacts",
        params=SeedParams.quick() if args.quick else SeedParams(),
        activity=not args.no_activity,
        log=_out,
    )
    for w in report["witnesses"]:
        _out(f"witness  {w['decl']:<40} {w['kind']:<11} {w['status']:<9} {w['sha256'][:12]}.{w['ext']}")
    _out("seeded   all rows are source='seed' and labeled as not Ember output; re-running replaces them ($0)")
    return 0


# --- sandbox --------------------------------------------------------------------------------


def cmd_sandbox_run(args: argparse.Namespace) -> int:
    from eddie.sandbox.runner import Limits, SandboxRunner

    script = Path(args.script).read_text(encoding="utf-8")
    runner = SandboxRunner(limits=Limits(wall_s=args.timeout, cpu_s=int(args.timeout)))
    res = asyncio.run(runner.run(script, postprocess=not args.no_postprocess))
    _out(f"status        {res.status}  (rc={res.returncode}, {res.duration_s}s)")
    if res.stdout.strip():
        _out("stdout        " + res.stdout.strip()[:2000].replace("\n", "\n              "))
    if res.stderr.strip():
        _out("stderr        " + res.stderr.strip()[-2000:].replace("\n", "\n              "))
    for v in res.violations:
        _out(f"VIOLATION     {v['event']}: {v['detail']}")
    for a in res.artifacts:
        defects = res.qc.get(a.name, {}).get("defects")
        _out(f"artifact      {a.name:<32} {a.kind:<6} {a.bytes:>9} B  {a.store_path}" + (f"  DEFECTS {defects}" if defects else ""))
    for r in res.rejected:
        _out(f"rejected      {r}")
    return 0 if res.status == "ok" else 5


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

    index = sub.add_parser("index", help="$0 structural index").add_subparsers(dest="action", required=True)
    p = index.add_parser("build", help="parse every .lean file into the SQLite FTS5 + graph index")
    p.add_argument("--src", required=True, help="extracted archive or sample tree")
    p.add_argument("--workers", type=int)
    p.add_argument("--index")
    p.set_defaults(func=cmd_index_build)
    p = index.add_parser("stats", help="counts, graph summary and the Ember cost forecast")
    p.add_argument("--index")
    p.add_argument("--json", action="store_true")
    p.add_argument("--output-tokens", type=int, default=16, help="assumed output tokens per decl in the forecast")
    p.set_defaults(func=cmd_index_stats)
    p = index.add_parser("search", help="full-text search over names, signatures and docstrings")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--kind")
    p.add_argument("--index")
    p.set_defaults(func=cmd_index_search)
    p = index.add_parser("show", help="print a declaration, loading its body by byte offsets")
    p.add_argument("name")
    p.add_argument("--max", type=int, default=3)
    p.add_argument("--signature-only", action="store_true")
    p.add_argument("--index")
    p.set_defaults(func=cmd_index_show)
    p = index.add_parser("validate-spine", help="recall of parsed names against p0/spine.tsv.gz")
    p.add_argument("spine")
    p.add_argument("--index")
    p.set_defaults(func=cmd_index_validate)

    cone = sub.add_parser("cone", help="declaration-level live proof cone + pillars").add_subparsers(dest="action", required=True)
    p = cone.add_parser("build", help="reference graph, liveness, dominators, scores, pillars, zoom tree")
    p.add_argument("--index")
    p.add_argument("--cone")
    p.add_argument("--spine", help="liveness oracle (default data/raw/p0/spine.tsv.gz when present)")
    p.add_argument("--pdf", help="companion PDF for pillar titles (default data/raw/pdfs/remark.pdf)")
    p.add_argument("--workers", type=int)
    p.set_defaults(func=cmd_cone_build)
    p = cone.add_parser("stats", help="liveness, graph agreement and the pillar list")
    p.add_argument("--cone")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_cone_stats)

    p = sub.add_parser("plan-budget", help="exact tiered Ember work plan fitted inside a target budget")
    p.add_argument("--target-usd", type=float, default=6.50)
    p.add_argument("--tier2-usd", type=float, default=2.00)
    p.add_argument("--reserve-demo-usd", type=float, default=1.50)
    p.add_argument("--fit", choices=["worst", "expected"], default="worst")
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--start-date", help="first day of the schedule (YYYY-MM-DD, default tomorrow)")
    p.add_argument("--triage-max-output", type=int, default=2048)
    p.add_argument("--worker-max-output", type=int, default=4096)
    p.add_argument("--critic-max-output", type=int, default=1024)
    p.add_argument("--arbiter-max-output", type=int, default=8192)
    p.add_argument("--arbiter-effort", default="xhigh", choices=["high", "xhigh", "max"])
    p.add_argument("--consolidator-max-output", type=int, default=16384)
    p.add_argument("--cone")
    p.add_argument("--out")
    p.add_argument("--no-db", action="store_true", help="do not load the queue into eddie_artifacts.sqlite")
    p.set_defaults(func=cmd_plan_budget)

    p = sub.add_parser("seed-demo", help="$0 sandbox witnesses for the explorer (labeled seed, never Ember)")
    p.add_argument("--cone")
    p.add_argument("--quick", action="store_true", help="small grids (seconds); for smoke tests")
    p.add_argument("--no-activity", action="store_true", help="do not mark a brain region as active")
    p.set_defaults(func=cmd_seed_demo)

    sandbox = sub.add_parser("sandbox", help="guarded script runner").add_subparsers(dest="action", required=True)
    p = sandbox.add_parser("run", help="run a generated script; collect plots/audio + spectrograms + QC")
    p.add_argument("script")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--no-postprocess", action="store_true")
    p.set_defaults(func=cmd_sandbox_run)

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
