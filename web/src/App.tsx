import { useCallback, useEffect, useState } from "react";
import { api, type Overview } from "./api";
import { Inspector } from "./Inspector";
import { ProofMap } from "./ProofMap";
import { TopBar } from "./TopBar";

/** Deep links: #decl=<id> opens a declaration, #node=<id> frames a part of the map. */
function readHash(): { decl: number | null; node: number | null } {
  const params = new URLSearchParams(window.location.hash.slice(1));
  const num = (k: string) => {
    const v = Number(params.get(k));
    return params.has(k) && Number.isInteger(v) && v >= 0 ? v : null;
  };
  return { decl: num("decl"), node: num("node") };
}

export function App() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<number | null>(null);
  const [reveal, setReveal] = useState<{ declId: number; nonce: number; instant?: boolean } | null>(null);
  const [initialNode, setInitialNode] = useState<number | null>(null);

  useEffect(() => {
    api
      .overview()
      .then((o) => {
        setOverview(o);
        const hash = readHash();
        if (hash.decl != null) {
          setSelected(hash.decl);
          if (hash.node == null) setReveal({ declId: hash.decl, nonce: 1, instant: true });
        } else if (o.roots.length) setSelected(o.roots[0].id);
        if (hash.node != null) setInitialNode(hash.node);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  const select = useCallback((declId: number, fly: boolean) => {
    setSelected(declId);
    if (fly) setReveal({ declId, nonce: performance.now() });
  }, []);

  useEffect(() => {
    if (selected != null) window.history.replaceState(null, "", `#decl=${selected}`);
  }, [selected]);

  return (
    <div className="app">
      <TopBar overview={overview} />
      <main className="stage">
        {overview ? (
          <ProofMap overview={overview} selected={selected} reveal={reveal} initialNode={initialNode} onSelect={select} />
        ) : (
          <section className="map map-empty">
            <p>
              {error
                ? error.includes("503")
                  ? "Eddie has no proof map yet. Build the index and the cone (python -m eddie.cli cone build), then reload."
                  : `The API did not answer: ${error}. Start it with make api.`
                : "Loading the proof map…"}
            </p>
          </section>
        )}
        <Inspector declId={selected} onSelect={select} />
      </main>
    </div>
  );
}
