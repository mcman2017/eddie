import { useEffect, useRef, useState, type PointerEvent } from "react";
import { COLORS } from "../theme";
import type { Landscape } from "./landscape";

const STATUS_NOTE: Record<string, string> = {
  exact: "Exact",
  stable: "Holds across encodings",
  "encoding-specific": "This numbering only",
  null: "Checked, nothing there",
};

/**
 * Findings, and the power spectrum |F(ω)|² of the live and dead declarations on one log axis.
 * Two series: live is a solid ink line, dead a dashed slate line (its shape, not only its
 * colour, tells it apart); both are listed in the legend.
 */
export function SpectrumPanel({ land, onFly }: { land: Landscape; onFly: (sigma: number, t: number) => void }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const [hover, setHover] = useState<{ x: number; omega: number; live: number; dead: number } | null>(null);
  const { min, max, n } = land.meta.grid.omega;
  const [live, dead] = land.spectrum;
  const from = Math.max(1, Math.round((2 / (max - min)) * (n - 1))); // skip the peak at ω = 0, which is 1 by normalisation
  const pad = { l: 46, r: 12, t: 10, b: 24 };
  const W = 396;
  const H = 150;
  let lo = Infinity;
  let hi = -Infinity;
  for (let i = from; i < n; i++) {
    for (const v of [live[i], dead[i]]) {
      if (v > 0) {
        lo = Math.min(lo, v);
        hi = Math.max(hi, v);
      }
    }
  }
  lo = Math.max(lo, hi * 1e-5);
  const xOf = (i: number) => pad.l + ((i - from) / (n - 1 - from)) * (W - pad.l - pad.r);
  const yOf = (v: number) => pad.t + (1 - (Math.log(Math.max(v, lo)) - Math.log(lo)) / (Math.log(hi) - Math.log(lo))) * (H - pad.t - pad.b);

  useEffect(() => {
    const canvas = ref.current;
    const g = canvas?.getContext("2d");
    if (!canvas || !g) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    g.font = "12px system-ui, sans-serif";
    g.fillStyle = COLORS.ink3;
    g.strokeStyle = COLORS.line;
    g.lineWidth = 1;
    for (let p = Math.ceil(Math.log10(lo)); p <= Math.floor(Math.log10(hi)); p++) {
      const y = Math.round(yOf(10 ** p)) + 0.5;
      g.beginPath();
      g.moveTo(pad.l, y);
      g.lineTo(W - pad.r, y);
      g.stroke();
      g.textAlign = "right";
      g.textBaseline = "middle";
      g.fillText(`1e${p}`, pad.l - 6, y);
    }
    g.textAlign = "center";
    g.textBaseline = "top";
    for (const w of [0.25, 0.5, 0.75, 1]) g.fillText((min + (max - min) * w).toFixed(0), xOf(Math.round(w * (n - 1))), H - pad.b + 6);
    const draw = (data: Float32Array, color: string, dash: number[], width: number) => {
      g.beginPath();
      for (let i = from; i < n; i++) {
        const x = xOf(i);
        const y = yOf(data[i]);
        if (i === from) g.moveTo(x, y);
        else g.lineTo(x, y);
      }
      g.setLineDash(dash);
      g.strokeStyle = color;
      g.lineWidth = width;
      g.stroke();
      g.setLineDash([]);
    };
    draw(dead, COLORS.slate, [5, 4], 1.5);
    draw(live, COLORS.ink, [], 2);
  }, [land]); // eslint-disable-line react-hooks/exhaustive-deps

  const onMove = (e: PointerEvent<HTMLCanvasElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - box.left;
    const i = Math.round(from + ((x - pad.l) / (W - pad.l - pad.r)) * (n - 1 - from));
    if (i < from || i >= n) setHover(null);
    else setHover({ x: xOf(i), omega: min + ((max - min) * i) / (n - 1), live: live[i], dead: dead[i] });
  };

  const stats = land.meta.proof;
  return (
    <div className="strip-card" role="region" aria-label="Findings and spectrum">
      <h2>What the numbers say</h2>
      <ul className="findings">
        {land.meta.findings.map((f) => (
          <li key={f.id}>
            <p className="finding-head">
              {f.at ? (
                <button type="button" className="link" onClick={() => f.at && onFly(f.at.sigma, f.at.t)}>
                  {f.headline}
                </button>
              ) : (
                f.headline
              )}
              <span className={`finding-status finding-${f.status}`}>{STATUS_NOTE[f.status] ?? f.status}</span>
            </p>
            <p className="finding-detail">{f.detail}</p>
          </li>
        ))}
      </ul>
      <h2>Power spectrum |F(ω)|², live and dead declarations</h2>
      <div className="spectrum">
        <canvas ref={ref} style={{ width: W, height: H }} onPointerMove={onMove} onPointerLeave={() => setHover(null)}
          aria-label="Power spectrum of the live and dead declarations against ω, log scale" />
        {hover && (
          <>
            <span className="spectrum-cross" style={{ left: hover.x }} aria-hidden="true" />
            <span className="spectrum-tip" style={{ left: Math.min(hover.x + 8, W - 150) }} role="status">
              ω = {hover.omega.toFixed(1)}: live {hover.live.toExponential(2)}, dead {hover.dead.toExponential(2)}
            </span>
          </>
        )}
      </div>
      <p className="spectrum-legend">
        <span><i className="swatch swatch-live" /> Live, {stats.live.stats.n1?.toLocaleString(undefined, { maximumFractionDigits: 0 })} effective classes</span>
        <span><i className="swatch swatch-dead" /> Dead, {stats.dead.stats.n1?.toLocaleString(undefined, { maximumFractionDigits: 0 })} effective classes</span>
      </p>
      <p className="spectrum-note">
        Peaks are recurrences of the prime coding and move with it. What does not move is the floor: fewer effective classes, a
        higher floor.
      </p>
    </div>
  );
}
