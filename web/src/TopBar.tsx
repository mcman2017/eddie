import { useEffect, useState } from "react";
import { api, type Activity, type BudgetSnapshot, type Overview } from "./api";

// The four brain regions keep their names; the line under each says what it does.
const REGIONS = [
  { key: "R1", label: "R1 Salience / Code", job: "reads and draws" },
  { key: "R2", label: "R2 Sensory Critic", job: "looks and listens" },
  { key: "R3", label: "R3 Workspace Arbiter", job: "checks the maths" },
  { key: "R4", label: "R4 Consolidator", job: "writes it up" },
];

function usePoll<T>(fn: () => Promise<T>, ms: number): T | null {
  const [value, setValue] = useState<T | null>(null);
  useEffect(() => {
    let live = true;
    const tick = () => fn().then((v) => live && setValue(v)).catch(() => undefined);
    tick();
    const id = window.setInterval(tick, ms);
    return () => {
      live = false;
      window.clearInterval(id);
    };
  }, [fn, ms]);
  return value;
}

const fetchBudget = () => api.budget();
const fetchActivity = () => api.activity();

export function TopBar({ overview }: { overview: Overview | null }) {
  const budget = usePoll<BudgetSnapshot>(fetchBudget, 4000);
  const activity = usePoll<Activity>(fetchActivity, 1500);
  const active = activity?.region?.slice(0, 2) ?? null;
  const a = overview?.archive;
  const cap = budget?.lifetime_cap_usd ?? 8;
  const spent = budget?.committed_usd ?? 0;
  const danger = budget?.breaker?.state === "open" || budget?.kill_switch;

  return (
    <header className="topbar">
      <div className="brand">
        Eddie
        <span className="brand-sub">Lean proof decompiler</span>
      </div>
      <div className="stats">
        <span className="stat">
          <b>{a ? a.files.toLocaleString() : "—"}</b> Lean files
        </span>
        <span className="sep">|</span>
        <span className="stat">
          <b>{a ? `${Math.round(a.dead_pct)}%` : "—"}</b> dead code
        </span>
        <span className="sep">|</span>
        <span className="stat" title="sorry is Lean's placeholder for a missing proof. None means no gaps.">
          <b>{a ? a.sorry : "—"}</b> proof gaps
        </span>
      </div>
      <ol className="regions" aria-label="Brain regions, in pipeline order">
        {REGIONS.map((r, i) => (
          <li key={r.key} className={`region ${active === r.key ? "region-active" : ""}`} aria-current={active === r.key ? "step" : undefined}>
            <span className="region-dot" aria-hidden="true" />
            <span className="region-name">
              {r.label}
              <small>{r.job}</small>
            </span>
            {i < REGIONS.length - 1 && (
              <span className="arrow" aria-hidden="true">
                →
              </span>
            )}
          </li>
        ))}
        {activity?.source === "seed" && active && <li className="region-note">demo data</li>}
      </ol>
      <div className={`spend ${danger ? "spend-danger" : ""} ${(budget?.inflight_worst_case_usd ?? 0) > 0 ? "spend-live" : ""}`}
        title="Ember spend so far, against the lifetime cap enforced by the spend governor">
        <span className="spend-amount">${spent.toFixed(4)}</span>
        <span className="spend-cap">/ ${cap.toFixed(2)} cap</span>
        {danger && <span className="spend-note">{budget?.kill_switch ? "kill switch on" : "breaker open"}</span>}
        {!budget?.available && <span className="spend-note">no ledger yet</span>}
      </div>
    </header>
  );
}
