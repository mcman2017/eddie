import { useState } from "react";
import type { Attempt, DeclDetail, Receipt, Witness } from "./api";
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

const RELATION: Record<string, string> = {
  special_case_check: "Special-case check",
  numerical_illustration: "Illustration",
  analogy: "Analogy",
};

const OUTCOME: Record<string, string> = {
  verified: "verified by R2",
  rejected: "rejected by R2",
  qc_failed: "failed Eddie's automatic image checks",
  sandbox_error: "did not run in the sandbox",
  rejected_static: "refused before running (static check)",
  invalid_output: "the reply could not be used",
  critic_unusable: "R2 gave no usable verdict",
};

/** One model call, in a line: which region, how hard it thought, what it cost (no vendor names). */
export function ReceiptLine({ receipt }: { receipt: Receipt | null | undefined }) {
  if (!receipt) return null;
  const tokens = receipt.input_tokens + receipt.output_tokens;
  return (
    <p className="receipt" title={receipt.response_id ?? undefined}>
      {receipt.region} · {receipt.effort} · {tokens.toLocaleString()} tokens ·{" "}
      {receipt.backend === "ember" ? `$${receipt.cost_usd.toFixed(4)}` : `${receipt.backend}, $0`}
      {receipt.response_id ? ` · ${receipt.response_id.slice(0, 16)}…` : ""}
    </p>
  );
}

export function WitnessViewer({ witnesses, reviews, receipts, attempts }: {
  witnesses: Witness[];
  reviews: DeclDetail["reviews"];
  receipts?: Record<string, Receipt>;
  attempts?: Attempt[];
}) {
  return (
    <>
      {pairWitnesses(witnesses).map((pair) => (
        <WitnessPair key={`${pair.verified?.id}-${pair.rejected?.id}`} pair={pair} reviews={reviews} receipts={receipts} />
      ))}
      {attempts && attempts.length > 1 && (
        <details className="attempts">
          <summary>All {attempts.length} attempts</summary>
          <ol>
            {attempts.map((a) => (
              <li key={`${a.attempt}-${a.origin}`}>
                <b>Attempt {a.attempt + 1}</b>
                {a.origin === "calibration_draft" ? " (calibration draft, written without the plot checklist)" : ""}
                {a.origin === "repair" ? " (repair pass)" : ""}:{" "}
                {OUTCOME[a.outcome] ?? a.outcome}
                {a.effort ? `, ${a.effort} effort` : ""}
                {a.critique && a.critique.length > 0 && (
                  <ul>
                    {a.critique.slice(0, 4).map((c) => (
                      <li key={c}>{c}</li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ol>
        </details>
      )}
    </>
  );
}

function WitnessPair({ pair, reviews, receipts }: { pair: Pair; reviews: DeclDetail["reviews"]; receipts?: Record<string, Receipt> }) {
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
      {shown?.caption && <p className="witness-caption">{shown.caption}</p>}
      {shown?.call_id && <ReceiptLine receipt={receipts?.[shown.call_id]} />}
      {mode !== "verified" && pair.rejected && (
        <div className="defects">
          <p className="defects-title">
            {pair.rejected.source === "seed"
              ? "Defects in this render (seed demo: planted, then measured)"
              : (pair.rejected.critic_verdict as { reviewer?: string } | null)?.reviewer === "sandbox-qc"
                ? "What Eddie's automatic image checks caught"
                : "What the R2 critic caught"}
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
          {w.source === "mock" ? " (mock rehearsal, not Ember)" : ""}
        </span>
        {w.relation && <span className="chip">{RELATION[w.relation] ?? w.relation}</span>}
        {w.origin === "calibration_draft" && <span className="chip chip-dashed">Calibration draft: written without the plot checklist</span>}
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
