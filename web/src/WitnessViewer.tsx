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
  // a pair opens as before and after: the contrast between the two renders is the point
  const [mode, setMode] = useState<Mode>(both ? "both" : pair.verified ? "verified" : "rejected");
  const shown = mode === "rejected" ? pair.rejected : pair.verified;
  const rejectedReview = pair.rejected ? reviews.find((r) => r.witness_id === pair.rejected?.id) : undefined;
  const modes: [Mode, string][] = [
    ["both", "Before and after"],
    ["rejected", "Rejected by R2"],
    ["verified", "Verified"],
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
        <div className="witness-pair">
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
  // the first defect listed is the measured one, so it can sit on the image itself
  const lead = w.status === "rejected" ? w.defects[0] : undefined;
  return (
    <figure className={`figure figure-${w.status}`}>
      <div className="figure-frame">
        {w.status === "rejected" && (
          <span className="verdict verdict-rejected">
            <RejectMark color={COLORS.raised} />
            Rejected{lead ? `: ${lead}` : ""}
          </span>
        )}
        {w.status === "verified" && (
          <span className="verdict verdict-verified">
            <CheckMark />
            Verified
          </span>
        )}
        <img src={w.url} alt={w.title ?? verdict} loading="lazy" />
      </div>
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

function CheckMark() {
  return (
    <svg className="status-mark" width={14} height={14} viewBox="0 0 14 14" aria-hidden="true">
      <path d="M 3.2 7.4 L 6 10 L 10.9 4.3" fill="none" stroke={COLORS.raised} strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function RejectMark({ color = COLORS.critical }: { color?: string }) {
  return (
    <svg className="status-mark" width={14} height={14} viewBox="0 0 14 14" aria-hidden="true">
      <circle cx={7} cy={7} r={5.5} fill="none" stroke={color} strokeWidth={1.5} />
      <path d="M 4.9 4.9 L 9.1 9.1 M 9.1 4.9 L 4.9 9.1" stroke={color} strokeWidth={1.5} strokeLinecap="round" />
    </svg>
  );
}
