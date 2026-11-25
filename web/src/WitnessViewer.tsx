import { useState } from "react";
import type { DeclDetail, Witness } from "./api";
import { StatusMark } from "./StatusMark";
import { COLORS } from "./theme";

type Pair = { verified: Witness | null; rejected: Witness | null };
type Mode = "verified" | "rejected" | "both";

/** Pair each verified render with the render R2 rejected before it (via supersedes_id). */
export function pairWitnesses(witnesses: Witness[]): Pair[] {
  const byId = new Map(witnesses.map((w) => [w.id, w]));
  const used = new Set<number>();
  const pairs: Pair[] = [];
  for (const w of witnesses) {
    const before = w.supersedes_id != null ? byId.get(w.supersedes_id) : undefined;
    if (w.status === "verified" && before && before.status === "rejected") {
      pairs.push({ verified: w, rejected: before });
      used.add(w.id);
      used.add(before.id);
    }
  }
  for (const w of witnesses) {
    if (used.has(w.id)) continue;
    pairs.push(w.status === "rejected" ? { verified: null, rejected: w } : { verified: w, rejected: null });
  }
  return pairs;
}

export function WitnessViewer({ witnesses, reviews }: { witnesses: Witness[]; reviews: DeclDetail["reviews"] }) {
  return (
    <>
      {pairWitnesses(witnesses).map((pair) => (
        <WitnessPair key={`${pair.verified?.id}-${pair.rejected?.id}`} pair={pair} reviews={reviews} />
      ))}
    </>
  );
}

function WitnessPair({ pair, reviews }: { pair: Pair; reviews: DeclDetail["reviews"] }) {
  const both = !!(pair.verified && pair.rejected);
  const [mode, setMode] = useState<Mode>(pair.verified ? "verified" : "rejected");
  const shown = mode === "rejected" ? pair.rejected : pair.verified;
  const rejectedReview = pair.rejected ? reviews.find((r) => r.witness_id === pair.rejected?.id) : undefined;
  const modes: [Mode, string][] = [
    ["verified", "Verified"],
    ["rejected", "Rejected by R2"],
    ["both", "Side by side"],
  ];

  return (
    <div className="witness">
      {both && (
        <div className="segmented" role="tablist" aria-label="Which render to show">
          {modes.map(([m, label]) => (
            <button key={m} type="button" role="tab" aria-selected={mode === m} onClick={() => setMode(m)}>
              {label}
            </button>
          ))}
        </div>
      )}
      {mode === "both" && pair.verified && pair.rejected ? (
        <div className="witness-grid">
          <Figure w={pair.rejected} />
          <Figure w={pair.verified} />
        </div>
      ) : (
        shown && <Figure w={shown} />
      )}
      {mode !== "verified" && pair.rejected && (
        <div className="defects">
          <p className="defects-title">
            {pair.rejected.source === "seed" ? "Defects in this render (seed demo: planted, then measured)" : "What the R2 critic caught"}
          </p>
          <ul>
            {[...pair.rejected.defects, ...(rejectedReview?.reasons ?? [])].map((d) => (
              <li key={d}>{d}</li>
            ))}
          </ul>
        </div>
      )}
      {shown?.python_source && (
        <details className="script">
          <summary>Sandbox script that drew this</summary>
          <pre>
            <code>{shown.python_source}</code>
          </pre>
        </details>
      )}
    </div>
  );
}

function Figure({ w }: { w: Witness }) {
  const verdict = w.status === "verified" ? "Verified by the R2 critic" : w.status === "rejected" ? "Rejected by the R2 critic" : "Awaiting R2 review";
  return (
    <figure className={`figure figure-${w.status}`}>
      <img src={w.url} alt={w.title ?? verdict} loading="lazy" />
      <figcaption>
        {w.status === "rejected" ? <RejectMark /> : <StatusMark status={w.status === "verified" ? "verified" : "live"} />}
        <span>
          {verdict}
          {w.title ? `: ${w.title}` : ""}
          {w.source === "seed" ? " (seed render, local sandbox)" : ""}
        </span>
      </figcaption>
    </figure>
  );
}

function RejectMark() {
  return (
    <svg className="status-mark" width={14} height={14} viewBox="0 0 14 14" aria-hidden="true">
      <circle cx={7} cy={7} r={5.5} fill="none" stroke={COLORS.critical} strokeWidth={1.5} />
      <path d="M 4.9 4.9 L 9.1 9.1 M 9.1 4.9 L 4.9 9.1" stroke={COLORS.critical} strokeWidth={1.5} strokeLinecap="round" />
    </svg>
  );
}
