import { useEffect, useState, type ReactNode } from "react";
import { api, type DeclDetail, type Neighbors } from "./api";
import { AudioWitness } from "./AudioWitness";
import { StatusChip } from "./StatusMark";
import { ReceiptLine, WitnessViewer } from "./WitnessViewer";

const SEED_PREFIX = "SEED: not an Ember translation.";
const unseed = (text: string) => (text.startsWith(SEED_PREFIX) ? text.slice(SEED_PREFIX.length).trimStart() : text);

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function day(iso: string | null): string {
  if (!iso) return "a later day";
  const [, m, d] = iso.split("-").map(Number);
  return `${MONTHS[(m ?? 1) - 1]} ${d}`;
}

/** Inline `code` spans inside otherwise plain prose. */
function Prose({ text }: { text: string }) {
  return (
    <>
      {text.split(/\n{2,}/).map((para, i) => (
        <p key={i}>
          {para.split(/(`[^`]+`)/).map((part, j) =>
            part.startsWith("`") && part.endsWith("`") ? <code key={j}>{part.slice(1, -1)}</code> : part,
          )}
        </p>
      ))}
    </>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="insp-section">
      <h2>{title}</h2>
      {children}
    </section>
  );
}

export function Inspector({ declId, onSelect }: { declId: number | null; onSelect: (id: number, fly: boolean) => void }) {
  const [detail, setDetail] = useState<DeclDetail | null>(null);
  const [neighbors, setNeighbors] = useState<Neighbors | null>(null);
  const [body, setBody] = useState<string | null>(null);
  const [bodyNote, setBodyNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (declId == null) return;
    let live = true;
    setError(null);
    setBody(null);
    setBodyNote(null);
    Promise.all([api.decl(declId), api.neighbors(declId)])
      .then(([d, n]) => {
        if (!live) return;
        setDetail(d);
        setNeighbors(n);
      })
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [declId]);

  if (declId == null || !detail) {
    return (
      <div className="inspector inspector-empty">
        {error ? <p className="note-error">{error}</p> : <p>Select a declaration on the map, or find one by name.</p>}
      </div>
    );
  }

  const d = detail;
  const m = d.metrics;
  const audio = d.witnesses.filter((w) => w.kind === "audio");
  const spectrograms = d.witnesses.filter((w) => w.kind === "spectrogram");
  const visuals = d.witnesses.filter((w) => w.kind === "plot" || w.kind === "animation");
  const lines =
    d.citation.start_line === d.citation.end_line
      ? `line ${d.citation.start_line}`
      : `lines ${d.citation.start_line}–${d.citation.end_line}`;

  return (
    <div className="inspector" aria-busy={detail.id !== declId}>
      <header className="insp-head">
        <div className="insp-tags">
          <span className="kind">{d.kind}</span>
          <StatusChip status={d.status} />
        </div>
        <h1 className="decl-name">{d.full_name ?? `(anonymous ${d.kind})`}</h1>
        <p className="insp-where">
          {d.pillar.title ? <>Part of {d.pillar.title.split(" · ").pop()}</> : "Outside every pillar"}
          {m.is_root && <span className="root-flag">One of Main's final theorems</span>}
        </p>
      </header>

      <Section title="Intuition">
        {d.intuition ? (
          <div className="prose">
            {d.intuition.source === "seed" && <p className="seed-note">Seed text written for the demo, not an Ember translation.</p>}
            {d.intuition.arbiter_notes && (
              <p className="audit" data-testid="r3-audit">
                <span className={`chip${d.intuition.arbiter_notes.verdict === "unfaithful" ? " chip-dashed" : ""}`}>
                  Audited by R3:{" "}
                  {d.intuition.arbiter_notes.verdict === "faithful"
                    ? "faithful to the Lean statement"
                    : d.intuition.arbiter_notes.verdict === "needs_fix"
                      ? "corrected against the Lean statement"
                      : "the first reading was misleading"}
                </span>{" "}
                confidence {d.intuition.arbiter_notes.confidence}
              </p>
            )}
            <Prose text={d.intuition.source === "seed" ? unseed(d.intuition.intuition_md) : d.intuition.intuition_md} />
            {d.intuition.arbiter_notes && d.intuition.arbiter_notes.hypotheses.length > 0 && (
              <>
                <h3>Hypotheses, in plain English</h3>
                <ul>
                  {d.intuition.arbiter_notes.hypotheses.map((h) => (
                    <li key={h.name + h.plain_english}>
                      <code>{h.name}</code>: {h.plain_english}
                    </li>
                  ))}
                </ul>
              </>
            )}
            {d.intuition.arbiter_notes && d.intuition.arbiter_notes.caveats.length > 0 && (
              <>
                <h3>What it does not say</h3>
                <ul>
                  {d.intuition.arbiter_notes.caveats.map((c) => (
                    <li key={c}>{c}</li>
                  ))}
                </ul>
              </>
            )}
            {d.intuition.citation && d.intuition.citation.length > 0 && (
              <p className="cite">
                Grounded in:{" "}
                {d.intuition.citation.map((c, k) => (
                  <span key={c.decl}>
                    {k > 0 ? "; " : ""}
                    <code>{c.decl}</code> ({c.path}:{c.start_line})
                  </span>
                ))}
              </p>
            )}
            {d.intuition.call_id && <ReceiptLine receipt={d.receipts?.[d.intuition.call_id]} />}
          </div>
        ) : d.triage ? (
          <div className="prose">
            <Prose text={d.triage.gloss} />
            {d.triage.source === "seed" && <p className="seed-note">Seed gloss, not an Ember triage.</p>}
            {(d.triage.role || d.triage.area) && (
              <p className="triage-tags">
                {d.triage.role && <span className="chip">{d.triage.role.replace("_", " ")}</span>}
                {d.triage.area && <span className="chip">{d.triage.area}</span>}
                {d.triage.visualizability != null && <span className="chip">drawable {d.triage.visualizability}/3</span>}
              </p>
            )}
            {d.triage.call_id && <ReceiptLine receipt={d.receipts?.[d.triage.call_id]} />}
          </div>
        ) : (
          <p className="muted">
            No plain-language reading yet.{" "}
            {d.planned
              ? d.planned.kind === "milestone"
                ? `Planned for ${day(d.planned.scheduled_day)}: the full R1 to R4 pass (translation, witness, critique, arbitration).`
                : `Planned for ${day(d.planned.scheduled_day)}: a one-line R1 triage gloss.`
              : m.live
                ? "It is outside the current Ember budget plan."
                : "It is outside the proof, so Ember will skip it."}
          </p>
        )}
      </Section>

      {visuals.length > 0 && (
        <Section title="What it looks like">
          <WitnessViewer witnesses={visuals} reviews={d.reviews} receipts={d.receipts} attempts={d.attempts} />
        </Section>
      )}

      {audio.length > 0 && (
        <Section title="What it sounds like">
          {audio.map((a) => (
            <AudioWitness
              key={a.id}
              audio={a}
              spectrogram={spectrograms.find((s) => s.pair_id === a.id) ?? spectrograms.find((s) => s.id === a.pair_id) ?? null}
            />
          ))}
        </Section>
      )}

      <Section title="Formal statement">
        <pre className="lean">
          <code>{d.signature}</code>
        </pre>
        <p className="cite">
          <span className="cite-path">{d.citation.path}</span>, {lines}
        </p>
        {d.source_available === false ? (
          <p className="muted">The proof itself is not served from this deployment: Eddie shows statements only.</p>
        ) : body == null ? (
          <button type="button" className="link" onClick={() => api.declBody(d.id).then((r) => {
            setBody(r.body ?? "");
            setBodyNote(r.body_note ?? null);
          }).catch(() => setBody(""))}>
            Show the Lean source
          </button>
        ) : (
          <pre className="lean lean-body">
            <code>{body || bodyNote || "Source not available on this host."}</code>
          </pre>
        )}
      </Section>

      <Section title="Place in the proof">
        <dl className="facts">
          <div>
            <dt>Steps from Main</dt>
            <dd>{m.live && m.depth >= 0 ? m.depth.toLocaleString() : "outside the proof"}</dd>
          </div>
          <div>
            <dt>Importance score</dt>
            <dd>
              {m.score.toFixed(3)}
              {m.pillar_rank ? `, rank ${m.pillar_rank.toLocaleString()} in its pillar` : ""}
            </dd>
          </div>
          <div>
            <dt>Reached only through it</dt>
            <dd>{Math.max(0, m.dom_mass - 1).toLocaleString()} declarations</dd>
          </div>
          <div>
            <dt>Uses, used by</dt>
            <dd>
              {m.fan_out.toLocaleString()}, {m.fan_in.toLocaleString()}
            </dd>
          </div>
          {m.dominator && m.dominator_id != null && (
            <div>
              <dt>Every path from Main passes</dt>
              <dd>
                <button type="button" className="link code" onClick={() => onSelect(m.dominator_id as number, true)}>
                  {m.dominator}
                </button>
              </dd>
            </div>
          )}
        </dl>
      </Section>

      {neighbors && (neighbors.uses.length > 0 || neighbors.used_by.length > 0) && (
        <Section title="Connections">
          <NeighborList title="Depends on" items={neighbors.uses} onPick={(id) => onSelect(id, true)} />
          <NeighborList title="Used by" items={neighbors.used_by} onPick={(id) => onSelect(id, true)} />
        </Section>
      )}
      {error && <p className="note-error">{error}</p>}
    </div>
  );
}

function NeighborList({ title, items, onPick }: { title: string; items: Neighbors["uses"]; onPick: (id: number) => void }) {
  if (!items.length) return null;
  return (
    <div className="neighbors">
      <h3>{title}</h3>
      <ul>
        {items.map((n) => (
          <li key={n.id}>
            <button type="button" onClick={() => onPick(n.id)} className={n.live ? "" : "is-dead"}>
              <span className="kind">{n.kind}</span>
              <span className="code">{n.full_name}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
