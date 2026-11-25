import { useEffect, useRef } from "react";
import type { BudgetSnapshot } from "./api";

// A Canvas ring spanning the whole contest budget ($0 → $10 disqualification line).
// Ember arc = committed spend, amber arc = worst case of calls in flight, tick at the
// lifetime cap ($8) and a red zone for the last $2 that Eddie is never allowed to touch.

const CONTEST_USD = 10;
const START = Math.PI * 0.75;
const SWEEP = Math.PI * 1.5;

function arc(ctx: CanvasRenderingContext2D, cx: number, cy: number, r: number, from: number, to: number, color: string, width: number) {
  if (to <= from) return;
  ctx.beginPath();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.lineCap = "butt";
  ctx.arc(cx, cy, r, START + SWEEP * from, START + SWEEP * to);
  ctx.stroke();
}

export function BudgetGauge({ snapshot, size = 280 }: { snapshot: BudgetSnapshot | null; size?: number }) {
  const ref = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size, size);

    const cx = size / 2;
    const cy = size / 2 + 8;
    const r = size * 0.38;
    const w = size * 0.075;
    const cap = snapshot?.lifetime_cap_usd ?? 8;
    const committed = Math.min(snapshot?.committed_usd ?? 0, CONTEST_USD);
    const inflight = Math.min(snapshot?.inflight_worst_case_usd ?? 0, CONTEST_USD - committed);

    arc(ctx, cx, cy, r, 0, 1, "#2a2421", w); // track
    arc(ctx, cx, cy, r, cap / CONTEST_USD, 1, "#5c1a14", w); // forbidden buffer above the cap
    arc(ctx, cx, cy, r, 0, committed / CONTEST_USD, "#ff6a2a", w);
    arc(ctx, cx, cy, r, committed / CONTEST_USD, (committed + inflight) / CONTEST_USD, "#ffb547", w);

    // dollar ticks
    for (let d = 0; d <= CONTEST_USD; d++) {
      const a = START + (SWEEP * d) / CONTEST_USD;
      const inner = r + w / 2 + 4;
      const outer = inner + (d === cap ? 14 : 6);
      ctx.beginPath();
      ctx.strokeStyle = d === cap ? "#ff3b30" : "#6b5f58";
      ctx.lineWidth = d === cap ? 2.5 : 1;
      ctx.moveTo(cx + Math.cos(a) * inner, cy + Math.sin(a) * inner);
      ctx.lineTo(cx + Math.cos(a) * outer, cy + Math.sin(a) * outer);
      ctx.stroke();
    }

    ctx.textAlign = "center";
    ctx.fillStyle = "#f4ece6";
    ctx.font = `600 ${Math.round(size * 0.12)}px "JetBrains Mono", ui-monospace, monospace`;
    ctx.fillText(`$${committed.toFixed(committed < 1 ? 4 : 2)}`, cx, cy + 4);
    ctx.fillStyle = "#a8988d";
    ctx.font = `500 ${Math.round(size * 0.048)}px ui-sans-serif, system-ui, sans-serif`;
    ctx.fillText(snapshot?.available ? `of $${cap.toFixed(2)} lifetime cap` : "ledger not initialized", cx, cy + size * 0.1);
    ctx.fillText("$10 = disqualified", cx, cy + size * 0.36);
  }, [snapshot, size]);

  return <canvas ref={ref} style={{ width: size, height: size }} aria-label="Ember spend gauge" role="img" />;
}
