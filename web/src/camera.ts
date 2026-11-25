// A small 2D camera: world -> screen transform plus smooth zoom-and-pan flights.
//
// Flights follow the optimal zoom/pan path of van Wijk & Nuij, "Smooth and efficient zooming
// and panning" (InfoVis 2003): for long jumps the camera rises (zooms out), travels and
// descends, instead of sliding across a huge distance at high magnification.

export interface Frame {
  x: number; // world point at the viewport center
  y: number;
  scale: number; // screen px per world unit
}

export interface Circle {
  x: number;
  y: number;
  r: number;
}

export interface Flight {
  duration: number; // ms
  at: (t: number) => Frame; // t in [0, 1]
}

const RHO = Math.SQRT2; // the paper's recommended curvature

export const easeInOutCubic = (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

export function frameFor(c: Circle, width: number, height: number, fill = 0.9): Frame {
  return { x: c.x, y: c.y, scale: (Math.min(width, height) * fill) / (2 * Math.max(c.r, 1e-9)) };
}

export function toScreen(f: Frame, width: number, height: number, x: number, y: number): [number, number] {
  return [(x - f.x) * f.scale + width / 2, (y - f.y) * f.scale + height / 2];
}

export function toWorld(f: Frame, width: number, height: number, sx: number, sy: number): [number, number] {
  return [(sx - width / 2) / f.scale + f.x, (sy - height / 2) / f.scale + f.y];
}

/** The smooth path between two frames; `viewport` is the viewport size in px that `w` measures. */
export function flight(a: Frame, b: Frame, viewport: number, msPerUnit = 520): Flight {
  const w0 = viewport / a.scale;
  const w1 = viewport / b.scale;
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const d = Math.hypot(dx, dy);
  const rho2 = RHO * RHO;
  let S: number;
  let at: (s: number) => [number, number];
  if (d < 1e-9 * Math.max(w0, w1)) {
    // pure zoom
    S = Math.log(w1 / w0) / RHO;
    at = (s) => [0, w0 * Math.exp(RHO * s)];
  } else {
    const b0 = (w1 * w1 - w0 * w0 + rho2 * rho2 * d * d) / (2 * w0 * rho2 * d);
    const b1 = (w1 * w1 - w0 * w0 - rho2 * rho2 * d * d) / (2 * w1 * rho2 * d);
    const r0 = Math.log(Math.sqrt(b0 * b0 + 1) - b0);
    const r1 = Math.log(Math.sqrt(b1 * b1 + 1) - b1);
    S = (r1 - r0) / RHO;
    const c0 = Math.cosh(r0);
    const s0 = Math.sinh(r0);
    at = (s) => [(w0 / rho2) * (c0 * Math.tanh(RHO * s + r0) - s0), (w0 * c0) / Math.cosh(RHO * s + r0)];
  }
  const span = Number.isFinite(S) ? S : 0;
  const duration = Math.min(2200, Math.max(420, Math.abs(span) * msPerUnit));
  return {
    duration,
    at: (t: number) => {
      if (t >= 1) return { ...b };
      const [u, w] = at(span * t);
      const k = d > 0 ? u / d : 0;
      return { x: a.x + dx * k, y: a.y + dy * k, scale: viewport / w };
    },
  };
}
