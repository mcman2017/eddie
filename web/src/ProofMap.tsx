import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { api, type MapPage, type Overview } from "./api";
import { MapEngine, type VNode } from "./mapEngine";
import { StatusMark } from "./StatusMark";
import { STATUS_LABEL, type Status } from "./theme";

interface Props {
  overview: Overview;
  selected: number | null;
  reveal: { declId: number; nonce: number; instant?: boolean } | null;
  initialNode: number | null;
  onSelect: (declId: number, fly: boolean) => void;
}

const shortLabel = (label: string) => label.split(" · ").pop() ?? label;

export function ProofMap({ overview, selected, reveal, initialNode, onSelect }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const engineRef = useRef<MapEngine | null>(null);
  const drag = useRef<{ x: number; y: number; moved: boolean; id: number } | null>(null);
  const [ready, setReady] = useState(false);
  const [path, setPath] = useState<VNode[]>([]);
  const [hover, setHover] = useState<{ node: VNode; x: number; y: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [root, setRoot] = useState<MapPage["node"] | null>(null);

  useEffect(() => {
    let engine: MapEngine | null = null;
    let cancelled = false;
    api
      .node(0)
      .then((page) => {
        const canvas = canvasRef.current;
        const wrap = wrapRef.current;
        if (cancelled || !canvas || !wrap) return;
        engine = new MapEngine(canvas, overview, page, { onFocus: setPath, onError: setError });
        engineRef.current = engine;
        setRoot(page.node);
        const box = wrap.getBoundingClientRect();
        engine.resize(box.width, box.height, window.devicePixelRatio || 1);
        setReady(true);
      })
      .catch((e: Error) => setError(e.message));
    return () => {
      cancelled = true;
      engine?.dispose();
      engineRef.current = null;
      setReady(false);
    };
  }, [overview]);

  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap || !ready) return;
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      engineRef.current?.resize(width, height, window.devicePixelRatio || 1);
    });
    observer.observe(wrap);
    return () => observer.disconnect();
  }, [ready]);

  useEffect(() => {
    if (ready) engineRef.current?.setSelected(selected);
  }, [selected, ready]);

  useEffect(() => {
    const engine = engineRef.current;
    if (!engine || !ready || !reveal) return;
    let live = true;
    api
      .decl(reveal.declId)
      .then((d) => (live && d.node_id != null ? engine.revealDecl(reveal.declId, d.node_id, reveal.instant) : undefined))
      .catch((e: Error) => setError(e.message));
    return () => {
      live = false;
    };
  }, [reveal, ready]);

  useEffect(() => {
    const engine = engineRef.current;
    if (!engine || !ready || initialNode == null) return;
    engine
      .revealNode(initialNode)
      .then((node) => node && engine.flyTo(node, true))
      .catch((e: Error) => setError(e.message));
  }, [initialNode, ready]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !ready) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const box = canvas.getBoundingClientRect();
      const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
      engineRef.current?.zoomAt(e.clientX - box.left, e.clientY - box.top, Math.exp(-dy * 0.0016));
    };
    canvas.addEventListener("wheel", onWheel, { passive: false });
    return () => canvas.removeEventListener("wheel", onWheel);
  }, [ready]);

  const local = (e: PointerEvent<HTMLCanvasElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    return [e.clientX - box.left, e.clientY - box.top] as const;
  };

  const onPointerDown = (e: PointerEvent<HTMLCanvasElement>) => {
    if (e.button === 0) drag.current = { x: e.clientX, y: e.clientY, moved: false, id: e.pointerId };
  };

  const onPointerMove = (e: PointerEvent<HTMLCanvasElement>) => {
    const engine = engineRef.current;
    if (!engine) return;
    const d = drag.current;
    if (d && e.buttons & 1) {
      const dx = e.clientX - d.x;
      const dy = e.clientY - d.y;
      if (!d.moved && Math.hypot(dx, dy) > 4) {
        d.moved = true;
        e.currentTarget.setPointerCapture(d.id);
      }
      if (d.moved) {
        engine.panBy(dx, dy);
        d.x = e.clientX;
        d.y = e.clientY;
        engine.setHover(null);
        setHover(null);
        return;
      }
    }
    const [sx, sy] = local(e);
    const node = engine.pick(sx, sy);
    engine.setHover(node);
    setHover(node ? { node, x: sx, y: sy } : null);
  };

  const onPointerUp = (e: PointerEvent<HTMLCanvasElement>) => {
    const d = drag.current;
    drag.current = null;
    const engine = engineRef.current;
    if (!engine || !d || d.moved) return;
    const [sx, sy] = local(e);
    const node = engine.pick(sx, sy);
    if (!node) engine.stepOut();
    else if (node.kind === "decl" && node.declId != null) onSelect(node.declId, false);
    else if (node.kind === "more") return;
    else if (engine.isFocusOrAncestor(node)) engine.stepOut();
    else engine.flyTo(node);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLCanvasElement>) => {
    const engine = engineRef.current;
    const wrap = wrapRef.current;
    if (!engine || !wrap) return;
    const { width, height } = wrap.getBoundingClientRect();
    if (e.key === "Escape" || e.key === "Backspace") engine.stepOut();
    else if (e.key === "+" || e.key === "=") engine.zoomAt(width / 2, height / 2, 1.5);
    else if (e.key === "-" || e.key === "_") engine.zoomAt(width / 2, height / 2, 1 / 1.5);
    else return;
    e.preventDefault();
  };

  const counts: Record<Status, number> = {
    verified: root?.verified ?? 0,
    triaged: root?.triaged ?? 0,
    live: Math.max(0, (root?.live ?? 0) - (root?.verified ?? 0) - (root?.triaged ?? 0)),
    dead: root?.dead ?? 0,
  };
  const seedOnly = (overview.artifact_sources?.seed ?? 0) > 0 && !(overview.artifact_sources?.ember ?? 0);
  const wrapWidth = wrapRef.current?.clientWidth ?? 0;

  return (
    <section className="map" ref={wrapRef} aria-label="Proof map">
      <canvas
        ref={canvasRef}
        tabIndex={0}
        role="img"
        aria-label="Zoomable map of the proof: Main at the center, proof pillars around it, declarations inside"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerLeave={() => {
          engineRef.current?.setHover(null);
          setHover(null);
        }}
        onKeyDown={onKeyDown}
      />
      <nav className="crumbs" aria-label="Where you are">
        {path.map((node, i) => (
          <span key={node.key} className="crumb">
            {i > 0 && <span className="crumb-sep" aria-hidden="true">/</span>}
            <button
              type="button"
              aria-current={i === path.length - 1 ? "location" : undefined}
              onClick={() => engineRef.current?.flyTo(node)}
            >
              {node.kind === "root" ? "Main" : shortLabel(node.label)}
            </button>
          </span>
        ))}
      </nav>
      <SearchBox onPick={(id) => onSelect(id, true)} />
      <div className="legend" aria-label="Legend">
        <div className="legend-items">
          {(["verified", "triaged", "live", "dead"] as Status[]).map((s) => (
            <span className="legend-item" key={s}>
              <StatusMark status={s} size={16} />
              {STATUS_LABEL[s]}
              <span className="legend-count">{counts[s].toLocaleString()}</span>
            </span>
          ))}
        </div>
        <p className="legend-note">
          Circle area shows how many declarations a part holds.
          {seedOnly && " Witnesses shown so far are local seed data, not Ember output."}
        </p>
      </div>
      <p className="map-hint">Scroll to zoom, drag to pan, click a part to enter it, Esc to step out</p>
      {hover && <Tooltip node={hover.node} x={hover.x} y={hover.y} flip={hover.x > wrapWidth - 360} />}
      {error && (
        <p className="map-error" role="alert">
          {error.includes("cone not built")
            ? "The proof map needs the cone. Run python -m eddie.cli cone build, then reload."
            : `The map could not load: ${error}`}
        </p>
      )}
    </section>
  );
}

function Tooltip({ node, x, y, flip }: { node: VNode; x: number; y: number; flip: boolean }) {
  const lines: string[] = [];
  let title = node.label;
  if (node.kind === "decl") {
    title = node.context ?? node.label;
    lines.push(STATUS_LABEL[node.status ?? "live"]);
    lines.push("Click to inspect");
  } else if (node.kind === "more") {
    lines.push(`${node.decls.toLocaleString()} declarations in smaller parts not drawn`);
  } else {
    if (node.context) lines.push(node.context);
    lines.push(`${node.decls.toLocaleString()} declarations, ${node.live.toLocaleString()} live`);
    if (node.verified) lines.push(`${node.verified.toLocaleString()} with a verified witness`);
    if (node.triaged) lines.push(`${node.triaged.toLocaleString()} triaged`);
  }
  return (
    <div className={`tooltip ${flip ? "tooltip-flip" : ""}`} style={{ left: x, top: y }} role="status">
      <strong>{title}</strong>
      {lines.map((l) => (
        <span key={l}>{l}</span>
      ))}
    </div>
  );
}

type Hit = { id: number; kind: string; full_name: string | null; module: string; signature: string };

function SearchBox({ onPick }: { onPick: (id: number) => void }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);

  useEffect(() => {
    const term = q.trim();
    if (term.length < 2) {
      setHits([]);
      return;
    }
    const t = window.setTimeout(() => {
      api
        .search(term)
        .then((r) => {
          setHits(r.results);
          setActive(0);
          setOpen(true);
        })
        .catch(() => setHits([]));
    }, 160);
    return () => window.clearTimeout(t);
  }, [q]);

  const pick = (h: Hit | undefined) => {
    if (!h) return;
    onPick(h.id);
    setOpen(false);
  };

  return (
    <div className="search">
      <input
        type="search"
        value={q}
        placeholder="Find a theorem, lemma or definition"
        aria-label="Find a declaration by name"
        spellCheck={false}
        onChange={(e) => setQ(e.target.value)}
        onFocus={() => hits.length && setOpen(true)}
        onBlur={() => window.setTimeout(() => setOpen(false), 120)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") setActive((a) => Math.min(a + 1, hits.length - 1));
          else if (e.key === "ArrowUp") setActive((a) => Math.max(a - 1, 0));
          else if (e.key === "Enter") pick(hits[active]);
          else if (e.key === "Escape") setOpen(false);
          else return;
          e.preventDefault();
        }}
      />
      {open && hits.length > 0 && (
        <ul className="search-hits" role="listbox" aria-label="Matching declarations">
          {hits.map((h, i) => (
            <li key={h.id} role="option" aria-selected={i === active}>
              <button type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => pick(h)}>
                <span className="hit-kind">{h.kind}</span>
                <span className="hit-name">{h.full_name ?? "(anonymous)"}</span>
                <span className="hit-module">{h.module}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
