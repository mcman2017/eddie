import { useEffect, useState } from "react";
import { api, type ChapterDetail, type ChapterSummary, type ChapterTheorem, type Tier } from "./api";
import { AudioWitness } from "./AudioWitness";
import { Prose } from "./Prose";
import { StatusMark } from "./StatusMark";
import { actName } from "./theme";
import { WitnessViewer } from "./WitnessViewer";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const TIERS: Tier[] = ["foundations", "machinery", "bottleneck", "main"];
const short = (title: string) => title.split(" · ").pop() ?? title;

function day(iso: string | null): string {
  if (!iso) return "a later day";
  const [, m, d] = iso.split("-").map(Number);
  return `${MONTHS[(m ?? 1) - 1]} ${d}`;
}

/** The opening paragraphs of a stub: the structured sections below repeat the rest with links. */
function synopsis(markdown: string, badge: string | null): string {
  const blocks = markdown.split(/\n{2,}/).map((b) => b.trim()).filter(Boolean);
  const kept = [];
  for (const b of blocks) {
    if (badge && b === badge) continue;
    if (/^\*\*(Builds on|Feeds|Final theorems|Chokepoints|Bottleneck theorems|Authors' summary PDF|Companion remark)/.test(b) || b.startsWith("The R4 consolidator")) break;
    kept.push(b);
  }
  return kept.join("\n\n");
}

interface Props {
  slug: string | null;
  chapters: ChapterSummary[];
  onChapter: (slug: string | null) => void;
  onSelectDecl: (id: number) => void;
}

export function ChapterGuide({ slug, chapters, onChapter, onSelectDecl }: Props) {
  const [detail, setDetail] = useState<ChapterDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!slug) return;
    let live = true;
    setError(null);
    api
      .chapter(slug)
      .then((d) => live && setDetail(d))
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [slug]);

  if (!slug) return <Contents chapters={chapters} onChapter={onChapter} />;
  if (error) return <p className="note-error">{error}</p>;
  if (!detail) return <p className="muted">Opening the chapter…</p>;

  const d = detail;
  const stale = d.slug !== slug;
  const visuals = d.witnesses.filter((w) => w.kind === "plot" || w.kind === "animation");
  const audio = d.witnesses.filter((w) => w.kind === "audio");
  const spectrograms = d.witnesses.filter((w) => w.kind === "spectrogram");
  const text = d.synthesis_md ? (d.source === "seed" ? synopsis(d.synthesis_md, d.badge) : d.synthesis_md) : "";

  return (
    <article className="guide" data-testid="chapter-guide" data-slug={d.slug} aria-busy={stale}>
      <header className="guide-head">
        <div className="guide-nav">
          <button type="button" className="link" onClick={() => onChapter(null)}>
            All chapters
          </button>
          <span className="guide-count">
            Chapter {d.chapter} of {d.chapters}
          </span>
          <span className="guide-steps">
            <button type="button" disabled={!d.prev} onClick={() => d.prev && onChapter(d.prev)} data-testid="tour-prev" title="Left arrow">
              Previous
            </button>
            <button type="button" disabled={!d.next} onClick={() => d.next && onChapter(d.next)} data-testid="tour-next" title="Right arrow">
              Next
            </button>
          </span>
        </div>
        <p className={`guide-tier tier-${d.tier}`}>{actName(d.tier, true)}</p>
        <h1>{short(d.title)}</h1>
        {d.title !== short(d.title) && <p className="guide-path">{d.title}</p>}
        {d.badge ? (
          <p className="stub-badge" data-testid="stub-badge">
            {d.badge}
          </p>
        ) : d.source ? (
          <p className="source-badge">Written by the R4 consolidator ({d.source})</p>
        ) : (
          <p className="stub-badge">No text yet: run make seed for the $0 structural stub</p>
        )}
      </header>

      {text && (
        <div className="prose">
          <Prose text={text} />
        </div>
      )}

      {(d.builds_on.length > 0 || d.feeds.length > 0) && (
        <section className="insp-section">
          <h2>Where it sits in the proof</h2>
          <div className="guide-flow">
            <FlowList title="Builds on" items={d.builds_on} onChapter={onChapter} empty="nothing upstream" />
            <span className="guide-arrow" aria-hidden="true">
              →
            </span>
            <span className="guide-here">Chapter {d.chapter}</span>
            <span className="guide-arrow" aria-hidden="true">
              →
            </span>
            <FlowList title="Feeds" items={d.feeds} onChapter={onChapter} empty={d.tier === "main" ? "the proof ends here" : "nothing downstream"} />
          </div>
          <p className="guide-note">A → B means B builds on A. Counts are net references between the two chapters.</p>
        </section>
      )}

      {d.final_theorems.length > 0 && (
        <section className="insp-section">
          <h2>Final theorems</h2>
          <TheoremList items={d.final_theorems} onSelectDecl={onSelectDecl} total={d.live_total} gates={false} />
        </section>
      )}

      {d.bottlenecks.length > 0 && (
        <section className="insp-section">
          <h2>Chokepoints</h2>
          <p className="guide-note">Every route from the final theorem to the stated share of the live proof runs through each of these.</p>
          <TheoremList items={d.bottlenecks} onSelectDecl={onSelectDecl} total={d.live_total} gates />
        </section>
      )}

      <section className="insp-section">
        <h2>See it and hear it</h2>
        {visuals.length > 0 && <WitnessViewer witnesses={visuals} reviews={d.reviews} />}
        {audio.map((a) => (
          <AudioWitness key={a.id} audio={a}
            spectrogram={spectrograms.find((s) => s.pair_id === a.id) ?? spectrograms.find((s) => s.id === a.pair_id) ?? null} />
        ))}
        {d.witnesses.length === 0 && (
          <p className="muted">
            Nothing has been drawn or sounded for this chapter yet.{" "}
            {d.planned
              ? `The budget plan schedules ${d.planned.milestones} of its milestones from ${day(d.planned.first_day)} (R1 translation, R2 critique, R3 arbitration), then one R4 consolidation on ${day(d.planned.consolidation_day)}.`
              : "It is outside the current Ember budget plan."}
          </p>
        )}
      </section>

      {d.remark_steps.length > 0 && (
        <p className="guide-note">
          Matches the authors' summary PDF by keyword: {d.remark_steps.map((s) => `step ${s.number}, ${s.phrase}`).join("; ")}.
        </p>
      )}
    </article>
  );
}

function FlowList({ title, items, onChapter, empty }: { title: string; items: ChapterDetail["builds_on"]; onChapter: (slug: string) => void; empty: string }) {
  return (
    <div className="guide-flowlist">
      <h3>{title}</h3>
      {items.length === 0 ? (
        <p className="muted">{empty}</p>
      ) : (
        <ul>
          {items.map((n) => (
            <li key={n.slug}>
              <button type="button" onClick={() => onChapter(n.slug)}>
                <b>{n.chapter}</b> {short(n.title)}
                <span className="guide-net">{n.net.toLocaleString()}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function TheoremList({ items, onSelectDecl, total, gates }: { items: ChapterTheorem[]; onSelectDecl: (id: number) => void; total: number; gates: boolean }) {
  return (
    <ul className="theorems">
      {items.map((t) => (
        <li key={t.id}>
          <button type="button" className="theorem-name" onClick={() => onSelectDecl(t.id)}>
            <StatusMark status={t.status} />
            <span className="code">{t.full_name}</span>
          </button>
          {gates && (
            <span className="theorem-gate">
              gates {Math.max(t.dom_mass - 1, 0).toLocaleString()} declarations ({(100 * (t.dom_mass / Math.max(total, 1))).toFixed(1)}% of the live proof)
            </span>
          )}
          <pre className="lean">
            <code>{t.signature}</code>
          </pre>
        </li>
      ))}
    </ul>
  );
}

function Contents({ chapters, onChapter }: { chapters: ChapterSummary[]; onChapter: (slug: string) => void }) {
  if (!chapters.length) return <p className="muted">The atlas needs the proof cone: run make cone, then make seed.</p>;
  const stubs = chapters.filter((c) => c.source === "seed").length;
  return (
    <div className="guide guide-contents" data-testid="atlas-contents">
      <header className="guide-head">
        <h1>Proof Atlas</h1>
        <p className="guide-path">
          {chapters.length} chapters in the order the proof is built, from the foundations to Main. Select a chapter here or a
          beacon on the strip.
        </p>
        {stubs > 0 && (
          <p className="stub-badge">
            {stubs === chapters.length ? `All ${stubs}` : `${stubs} of ${chapters.length}`} chapters are drafts built from structure alone ($0). No
            model has written them yet.
          </p>
        )}
      </header>
      {TIERS.map((tier) => {
        const rows = chapters.filter((c) => c.tier === tier);
        if (!rows.length) return null;
        return (
          <section key={tier} className="insp-section">
            <h2 className={`contents-act tier-${tier}`}>
              <i className="act-chip" />
              {actName(tier, true)}
            </h2>
            <ol className="contents">
              {rows.map((c) => (
                <li key={c.slug}>
                  <button type="button" onClick={() => onChapter(c.slug)}>
                    <b>{c.chapter}</b>
                    <span className="contents-title">{short(c.title)}</span>
                    <span className="contents-count">{c.live.toLocaleString()} live</span>
                    {c.verified_witnesses > 0 && <StatusMark status="verified" />}
                  </button>
                </li>
              ))}
            </ol>
          </section>
        );
      })}
    </div>
  );
}
