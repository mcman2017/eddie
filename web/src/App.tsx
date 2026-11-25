import { useEffect, useState } from "react";
import { api, type BudgetSnapshot, type IndexStats, type SearchHit } from "./api";
import { BudgetGauge } from "./BudgetGauge";

const usd = (v: number | undefined, places = 4) => (v === undefined ? "—" : `$${v.toFixed(places)}`);
const n = (v: number | undefined) => (v === undefined ? "—" : v.toLocaleString());

function useBudget(pollMs = 5000) {
  const [snapshot, setSnapshot] = useState<BudgetSnapshot | null>(null);
  useEffect(() => {
    let live = true;
    const tick = () => api.budget().then((b) => live && setSnapshot(b)).catch(() => live && setSnapshot({ available: false }));
    tick();
    const id = window.setInterval(tick, pollMs);
    return () => {
      live = false;
      window.clearInterval(id);
    };
  }, [pollMs]);
  return snapshot;
}

function Governor() {
  const b = useBudget();
  const breakerOpen = b?.breaker?.state === "open" || b?.kill_switch;
  return (
    <section className="card">
      <header className="card-head">
        <h2>Metabolic Spend Governor</h2>
        <span className={`pill ${breakerOpen ? "pill-danger" : "pill-ok"}`}>{breakerOpen ? "breaker OPEN" : "armed"}</span>
      </header>
      <div className="gauge-row">
        <BudgetGauge snapshot={b} />
        <dl className="facts">
          <dt>committed</dt>
          <dd>{usd(b?.committed_usd)}</dd>
          <dt>in-flight worst case</dt>
          <dd>{usd(b?.inflight_worst_case_usd)}</dd>
          <dt>headroom</dt>
          <dd>{usd(b?.headroom_usd)}</dd>
          <dt>rolling 24 h</dt>
          <dd>
            {usd(b?.daily_committed_usd)} / {usd(b?.daily_cap_usd, 2)}
          </dd>
          <dt>per-run cap</dt>
          <dd>{usd(b?.run_cap_usd, 2)}</dd>
        </dl>
      </div>
      {b?.breaker?.reason && <p className="warn">breaker: {b.breaker.reason}</p>}
    </section>
  );
}

function IndexPanel() {
  const [stats, setStats] = useState<IndexStats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("riemannHypothesis");
  const [hits, setHits] = useState<SearchHit[]>([]);

  useEffect(() => {
    api.stats().then(setStats).catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    const term = q.trim();
    if (!term) return setHits([]);
    const id = window.setTimeout(() => {
      api.search(term).then((r) => setHits(r.results)).catch(() => setHits([]));
    }, 180);
    return () => window.clearTimeout(id);
  }, [q]);

  const f = stats?.forecast_r1_all_signatures;
  return (
    <section className="card">
      <header className="card-head">
        <h2>Structural Index</h2>
        <span className="pill">$0 · local</span>
      </header>
      {error && <p className="warn">{error}</p>}
      <div className="stat-grid">
        <div>
          <b>{n(stats?.modules)}</b>
          <span>modules</span>
        </div>
        <div>
          <b>{n(stats?.decls)}</b>
          <span>declarations</span>
        </div>
        <div>
          <b>{n(stats?.source_lines)}</b>
          <span>lines of Lean</span>
        </div>
        <div>
          <b>{stats ? stats.sorry_count : "—"}</b>
          <span>sorry</span>
        </div>
      </div>
      {f && (
        <p className="forecast">
          One Ember triage pass over every signature would cost <b>~{usd(f.total_usd_typical, 2)}</b>, so the
          index decides what Ember ever sees.
        </p>
      )}
      <input className="search" value={q} onChange={(e) => setQ(e.target.value)} placeholder="search theorems, lemmas, defs…" spellCheck={false} />
      <ol className="hits">
        {hits.map((h) => (
          <li key={h.id}>
            <div className="hit-head">
              <span className={`kind kind-${h.kind}`}>{h.kind}</span>
              <code className="name">{h.full_name ?? "(anonymous)"}</code>
              <span className="module">{h.module}</span>
            </div>
            <pre className="sig">{h.signature}</pre>
          </li>
        ))}
      </ol>
    </section>
  );
}

export function App() {
  return (
    <div className="shell">
      <header className="masthead">
        <h1>
          EDDIE<span className="dot">·</span>
        </h1>
        <p>a 4-region proof decompiler · Lean 4 → sight, sound &amp; intuition · on Meta Muse Ember</p>
      </header>
      <main className="grid">
        <Governor />
        <IndexPanel />
      </main>
    </div>
  );
}
