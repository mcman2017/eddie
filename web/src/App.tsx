import { Suspense, lazy, useCallback, useEffect, useState } from "react";
import { api, type ChapterSummary, type Overview } from "./api";
import { ChapterGuide } from "./ChapterGuide";
import { Inspector } from "./Inspector";
import { ProofMap } from "./ProofMap";
import { routeNumber, routeText, writeRoute } from "./route";
import { TopBar } from "./TopBar";

const StripView = lazy(() => import("./strip/StripView"));

type View = "strip" | "wheel";
type Panel = "guide" | "inspector";

/**
 * Two views of one proof share the stage: the 3D critical strip (the default) and the
 * dependency wheel. The right panel is either the chapter guide or the declaration inspector.
 * Deep links are documented in route.ts.
 */
export function App() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [chapters, setChapters] = useState<ChapterSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<View>(() => (routeText("view") === "wheel" || routeNumber("node") != null ? "wheel" : "strip"));
  const [visited, setVisited] = useState<Record<View, boolean>>({ strip: view === "strip", wheel: view === "wheel" });
  const [panel, setPanel] = useState<Panel>(() => (routeNumber("decl") != null && !routeText("chapter") ? "inspector" : "guide"));
  const [chapter, setChapter] = useState<string | null>(() => routeText("chapter"));
  const [selected, setSelected] = useState<number | null>(null);
  const [reveal, setReveal] = useState<{ declId: number; nonce: number; instant?: boolean } | null>(null);
  const [initialNode, setInitialNode] = useState<number | null>(null);

  useEffect(() => {
    api
      .overview()
      .then((o) => {
        setOverview(o);
        const decl = routeNumber("decl");
        const node = routeNumber("node");
        if (decl != null) {
          setSelected(decl);
          if (node == null) setReveal({ declId: decl, nonce: 1, instant: true });
        } else if (o.roots.length) setSelected(o.roots[0].id);
        if (node != null) setInitialNode(node);
      })
      .catch((e: Error) => setError(e.message));
    api
      .chapters()
      .then((c) => setChapters(c.chapters))
      .catch(() => setChapters([]));
  }, []);

  const show = useCallback((next: View) => {
    setView(next);
    setVisited((v) => ({ ...v, [next]: true }));
    writeRoute({ view: next === "strip" ? null : next });
  }, []);

  const select = useCallback((declId: number, fly: boolean) => {
    setSelected(declId);
    setPanel("inspector");
    writeRoute({ decl: declId });
    if (fly) setReveal({ declId, nonce: performance.now() });
  }, []);

  const openChapter = useCallback((slug: string | null) => {
    setChapter(slug);
    setPanel("guide");
    writeRoute({ chapter: slug });
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (panel !== "guide" || !chapter || e.metaKey || e.ctrlKey || e.altKey) return;
      if (el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return;
      const here = chapters.find((c) => c.slug === chapter);
      const to = e.key === "ArrowRight" ? here?.next : e.key === "ArrowLeft" ? here?.prev : undefined;
      if (to) {
        openChapter(to);
        e.preventDefault();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [panel, chapter, chapters, openChapter]);

  return (
    <div className="app">
      <TopBar overview={overview} />
      <main className="stage">
        <div className="views">
          <div className="view-tabs" role="tablist" aria-label="View">
            <button type="button" role="tab" aria-selected={view === "strip"} onClick={() => show("strip")}>
              3D Critical Strip &amp; Wave
            </button>
            <button type="button" role="tab" aria-selected={view === "wheel"} onClick={() => show("wheel")}>
              Dependency Wheel
            </button>
          </div>
          <div className={`view ${view === "strip" ? "" : "view-off"}`} inert={view !== "strip"}>
            {visited.strip && (
              <Suspense fallback={<section className="map map-empty"><p>Loading the 3D view…</p></section>}>
                <StripView active={view === "strip"} chapter={chapter} onChapter={openChapter} />
              </Suspense>
            )}
          </div>
          <div className={`view ${view === "wheel" ? "" : "view-off"}`} inert={view !== "wheel"}>
            {visited.wheel &&
              (overview ? (
                <ProofMap overview={overview} selected={selected} reveal={reveal} initialNode={initialNode} onSelect={select} />
              ) : (
                <section className="map map-empty">
                  <p>
                    {error
                      ? error.includes("503")
                        ? "Eddie has no proof map yet. Build the index and the cone (make cone), then reload."
                        : `The API did not answer: ${error}. Start it with make api.`
                      : "Loading the proof map…"}
                  </p>
                </section>
              ))}
          </div>
        </div>
        <aside className="side" aria-label={panel === "guide" ? "Chapter guide" : "Theorem inspector"}>
          <div className="side-tabs" role="tablist" aria-label="Panel">
            <button type="button" role="tab" aria-selected={panel === "guide"} onClick={() => setPanel("guide")}>
              Chapter guide
            </button>
            <button type="button" role="tab" aria-selected={panel === "inspector"} onClick={() => setPanel("inspector")}>
              Declaration
            </button>
          </div>
          {panel === "guide" ? (
            <div className="side-body">
              <ChapterGuide slug={chapter} chapters={chapters} onChapter={openChapter} onSelectDecl={(id) => select(id, view === "wheel")} />
            </div>
          ) : (
            <Inspector declId={selected} onSelect={(id, fly) => select(id, fly && view === "wheel")} />
          )}
        </aside>
      </main>
    </div>
  );
}
