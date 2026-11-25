// The visual tour: five shots joined into one continuous camera move, with the scene's state
// (zeta or proof wave, which declarations weight it) changing on the same clock.
//
// This file is pure data and arithmetic, with no three.js and no DOM, so the engine can sample
// it every frame without allocating and a check can audit the path without a GPU.
//
// The camera path. A pose is one point in six dimensions (eye, then look-at target), so the eye
// and the target always reach a key together. The path is a centripetal Catmull-Rom spline
// through the keys, evaluated as cubic Hermite segments, and time is mapped onto it by a
// monotone cubic whose slope is zero only at the two ends: the camera eases in once, eases out
// once, and never comes to rest in between. Orientation is always "look at the target", so no
// angle is ever interpolated and nothing can spin the long way round.

export type ShotId = "zeros" | "funnel" | "morph" | "compare" | "summit";
export type PopulationName = "live" | "dead" | "compare";
export type Vec3 = readonly [number, number, number];

/** What the controls show: which surface, and which declarations weight the wave. */
export interface SceneState {
  wave: boolean;
  population: PopulationName;
}

export interface Shot {
  id: ShotId;
  /** the label of its pill */
  label: string;
  /** when the tour passes through this shot's key frame, in seconds */
  at: number;
  /** the scene a pill leaves behind when it is used on its own */
  scene: SceneState;
}

export interface TourFacts {
  chapters: number;
  live: number;
  dead: number;
  /** the theorem every path to most of the proof runs through, when the landscape names it */
  gate: { name: string; share: number } | null;
}

export interface Cue {
  shot: ShotId;
  from: number;
  /** a frame inside the cue that shows it at rest, for reduced motion and for screenshots */
  still: number;
  text(f: TourFacts): string;
}

export interface TourFrame {
  eye: [number, number, number];
  target: [number, number, number];
  /** 0 = zeta, 1 = the proof wave */
  morph: number;
  /** true while a morph is under way, so it runs up the strip as a front */
  sweeping: boolean;
  /** 0 = live, 1 = dead */
  pop: number;
  /** opacity of the dead wave's grid over the live surface */
  ghost: number;
  shot: number;
  cue: number;
  scene: SceneState;
}

interface Key {
  at: number;
  eye: Vec3;
  target: Vec3;
}

export const TOUR_SECONDS = 40;

// Key frames for a strip 60 units long (tMax = 60); compileTour scales them to the landscape.
// Measured against the real landscape: the eye stays at least 3.5 above the highest the surface
// can reach, 8 from the nearest beacon and nearly 4 from the nearest flow arc, and it rises out
// of the valley through the one gap the arcs leave near t = 7.
const KEYS: Key[] = [
  { at: 0.0, eye: [-21.3, 69.4, 39.7], target: [1, -1, -32.4] }, // the overview, where the view opens
  { at: 4.2, eye: [-3, 10.5, 15], target: [0, 0, -22] }, // line up on the valley
  { at: 6.6, eye: [0, 4.6, -1.5], target: [0, 0.2, -30] }, // shot 1: down the critical line
  { at: 8.4, eye: [-1, 4.2, -6.8], target: [0, 0.6, -36] }, // lowest and deepest
  { at: 10.0, eye: [-1.2, 16, -4], target: [0, 0.8, -33] }, // boom up through the gap in the arcs
  { at: 13.6, eye: [5, 38, 44], target: [0, 1, -30] }, // shot 2: the whole funnel, head on
  { at: 20.8, eye: [32.3, 35.7, 47.3], target: [1, -1, -31] }, // shot 3: three-quarter view of the wave
  { at: 29.8, eye: [33.3, 27.3, 34.1], target: [1, -1, -31] }, // shot 4: a little lower, for the comparison
  { at: TOUR_SECONDS, eye: [7.5, 18, -27], target: [0.5, 3.6, -56] }, // shot 5: the summit
];

export const SHOTS: Shot[] = [
  { id: "zeros", label: "1. Critical Line Zeros", at: 6.6, scene: { wave: false, population: "live" } },
  { id: "funnel", label: "2. 20-Chapter Funnel", at: 13.6, scene: { wave: false, population: "live" } },
  { id: "morph", label: "3. Morph → Proof Wave", at: 20.8, scene: { wave: true, population: "live" } },
  { id: "compare", label: "4. Dead Code vs. Live Proof", at: 29.8, scene: { wave: true, population: "compare" } },
  { id: "summit", label: "5. Summit: Main.lean", at: TOUR_SECONDS, scene: { wave: false, population: "live" } },
];

// The scene's own timeline, in seconds.
const MORPH_IN: [number, number] = [14.8, 19.8]; // zeta to the proof wave, slower than the button's morph
const MORPH_OUT: [number, number] = [32.4, 36.4]; // and back, on the way to the summit
const DEAD: [number, number] = [23.6, 26.8]; // the surface shows the dead population
const COMPARE: [number, number] = [26.8, 31.4]; // live surface with the dead wave's grid over it
const FADE = 0.7; // seconds for a population or the grid to arrive or leave

const n = (v: number) => v.toLocaleString("en-US");

export const CUES: Cue[] = [
  { shot: "zeros", from: 0, still: 6.6,
    text: () => "Riemann's 1859 bet: EVERY non-trivial zero valley sits dead-center on σ = 1/2." },
  { shot: "funnel", from: 8.8, still: 13.6,
    text: (f) => `The proof in ${f.chapters} chapters and four acts. Each arc is one chapter feeding another, up to Main.` },
  { shot: "morph", from: 14.2, still: 20.8,
    text: () => "Now the proof's own shape: a million declarations added up as one wave. It pictures the code, not ζ." },
  { shot: "compare", from: 21.6, still: 22.8,
    text: (f) => `Colour is the live proof: ${n(f.live)} declarations the final theorem depends on.` },
  { shot: "compare", from: 23.6, still: 25.6,
    text: (f) => `Grey is dead code: ${n(f.dead)} declarations it never uses. Same archive, a different wave.` },
  { shot: "compare", from: 26.8, still: 29.8,
    text: () => "Both at once: the grey grid is dead code, and outlines mark where the two differ most." },
  { shot: "summit", from: 31.4, still: TOUR_SECONDS,
    text: (f) => f.gate
      ? `The summit: Main.lean. One theorem here, ${f.gate.name}, gates ${f.gate.share.toFixed(1)}% of the live proof.`
      : `The summit: Main.lean, where all ${f.chapters} chapters meet and the final theorem is proved.` },
];

export interface CompiledTour {
  duration: number;
  /** key times, spline knots, poses (6 numbers each), pose tangents and the slopes of the time map */
  at: number[];
  knot: number[];
  pose: number[][];
  tangent: number[][];
  rate: number[];
}

/** cubic-bezier(x1, y1, x2, y2) as a function of x, the same curve CSS draws */
export function cubicBezier(x1: number, y1: number, x2: number, y2: number): (x: number) => number {
  const curve = (a: number, b: number, t: number) => 3 * a * t * (1 - t) * (1 - t) + 3 * b * t * t * (1 - t) + t * t * t;
  return (x) => {
    if (x <= 0) return 0;
    if (x >= 1) return 1;
    let lo = 0;
    let hi = 1;
    for (let i = 0; i < 28; i++) {
      const mid = (lo + hi) / 2;
      if (curve(x1, x2, mid) < x) lo = mid;
      else hi = mid;
    }
    return curve(y1, y2, (lo + hi) / 2);
  };
}

const glide = cubicBezier(0.3, 0, 0.7, 1);

/** 0 before `from`, 1 after `to`, eased between */
function ramp(t: number, from: number, to: number): number {
  return glide((t - from) / (to - from));
}

/** 0 outside [from, to], 1 inside, with a linear fade of FADE seconds on the way in and out */
function gate(t: number, from: number, to: number): number {
  return Math.max(0, Math.min(1, (t - from) / FADE, (to + FADE - t) / FADE));
}

export function compileTour(tMax: number): CompiledTour {
  const s = tMax / 60;
  const pose = KEYS.map((k) => [k.eye[0], k.eye[1], k.eye[2] * s, k.target[0], k.target[1], k.target[2] * s]);
  const at = KEYS.map((k) => k.at);
  const last = pose.length - 1;
  // centripetal knots: the square root of the distance between neighbouring poses
  const knot = [0];
  for (let k = 0; k < last; k++) {
    const d = Math.hypot(...pose[k].map((v, i) => pose[k + 1][i] - v));
    knot.push(knot[k] + Math.sqrt(Math.max(d, 1e-6)));
  }
  const du = (k: number) => knot[k + 1] - knot[k];
  const secant = (k: number) => pose[k].map((v, i) => (pose[k + 1][i] - v) / du(k));
  const tangent = pose.map((_, k) => {
    if (k === 0) return secant(0);
    if (k === last) return secant(last - 1);
    const a = secant(k - 1);
    const b = secant(k);
    return a.map((v, i) => (v * du(k) + b[i] * du(k - 1)) / (du(k - 1) + du(k)));
  });
  // The time map knot(t): monotone cubic (Fritsch-Butland slopes), at rest only at the two ends.
  const slope = (k: number) => du(k) / (at[k + 1] - at[k]);
  const rate = pose.map((_, k) => {
    if (k === 0 || k === last) return 0;
    const h0 = at[k] - at[k - 1];
    const h1 = at[k + 1] - at[k];
    return (3 * (h0 + h1)) / ((2 * h1 + h0) / slope(k - 1) + (h1 + 2 * h0) / slope(k));
  });
  return { duration: TOUR_SECONDS, at, knot, pose, tangent, rate };
}

function hermite(p0: number, m0: number, p1: number, m1: number, h: number, x: number): number {
  const x2 = x * x;
  const x3 = x2 * x;
  return (2 * x3 - 3 * x2 + 1) * p0 + (x3 - 2 * x2 + x) * h * m0 + (-2 * x3 + 3 * x2) * p1 + (x3 - x2) * h * m1;
}

export function sceneAt(t: number): SceneState {
  const wave = t >= (MORPH_IN[0] + MORPH_IN[1]) / 2 && t < (MORPH_OUT[0] + MORPH_OUT[1]) / 2;
  const population: PopulationName = t >= DEAD[0] && t < DEAD[1] ? "dead" : t >= COMPARE[0] && t < COMPARE[1] ? "compare" : "live";
  return { wave, population };
}

export function cueAt(t: number): number {
  let cue = 0;
  for (let i = 0; i < CUES.length; i++) if (t >= CUES[i].from) cue = i;
  return cue;
}

/** The whole state of the tour at time t (seconds), written into `out`. Pure and allocation-free. */
export function sampleTour(c: CompiledTour, time: number, out: TourFrame): TourFrame {
  const t = Math.min(c.duration, Math.max(0, time));
  let k = 0;
  while (k < c.at.length - 2 && t >= c.at[k + 1]) k++;
  const span = c.at[k + 1] - c.at[k];
  const u = hermite(c.knot[k], c.rate[k], c.knot[k + 1], c.rate[k + 1], span, (t - c.at[k]) / span);
  const h = c.knot[k + 1] - c.knot[k];
  const x = Math.min(1, Math.max(0, (u - c.knot[k]) / h));
  for (let i = 0; i < 3; i++) {
    out.eye[i] = hermite(c.pose[k][i], c.tangent[k][i], c.pose[k + 1][i], c.tangent[k + 1][i], h, x);
    out.target[i] = hermite(c.pose[k][i + 3], c.tangent[k][i + 3], c.pose[k + 1][i + 3], c.tangent[k + 1][i + 3], h, x);
  }
  out.morph = ramp(t, MORPH_IN[0], MORPH_IN[1]) - ramp(t, MORPH_OUT[0], MORPH_OUT[1]);
  out.sweeping = (t > MORPH_IN[0] && t < MORPH_IN[1]) || (t > MORPH_OUT[0] && t < MORPH_OUT[1]);
  out.pop = gate(t, DEAD[0], DEAD[1]);
  out.ghost = gate(t, COMPARE[0], COMPARE[1]);
  out.cue = cueAt(t);
  out.shot = SHOTS.findIndex((s) => s.id === CUES[out.cue].shot);
  out.scene = sceneAt(t);
  return out;
}

export function emptyFrame(): TourFrame {
  return { eye: [0, 0, 0], target: [0, 0, 0], morph: 0, sweeping: false, pop: 0, ghost: 0, shot: 0, cue: 0,
           scene: { wave: false, population: "live" } };
}

/** The camera pose of a shot's key frame, for the pill that jumps straight to it. */
export function shotPose(c: CompiledTour, id: ShotId): { eye: Vec3; target: Vec3 } {
  const shot = SHOTS.find((s) => s.id === id) as Shot;
  const f = sampleTour(c, shot.at, emptyFrame());
  return { eye: [f.eye[0], f.eye[1], f.eye[2]], target: [f.target[0], f.target[1], f.target[2]] };
}

export interface TourAudit {
  /** slowest the eye moves between the first and last second, in units per second */
  minSpeed: number;
  /** largest jump in eye velocity from one sample to the next, relative to the speed there */
  jerk: number;
  /** lowest the eye goes */
  minHeight: number;
}

/** Sample the path finely and report what a smooth, clear move has to satisfy. */
export function auditTour(c: CompiledTour, hz = 120): TourAudit {
  const f = emptyFrame();
  const dt = 1 / hz;
  let minSpeed = Infinity;
  let jerk = 0;
  let minHeight = Infinity;
  let prev: number[] | null = null;
  let prevV: number[] | null = null;
  for (let t = 0; t <= c.duration + 1e-9; t += dt) {
    sampleTour(c, t, f);
    minHeight = Math.min(minHeight, f.eye[1]);
    if (prev) {
      const v = f.eye.map((x, i) => (x - (prev as number[])[i]) / dt);
      const speed = Math.hypot(...v);
      const inside = t > 1 && t < c.duration - 1;
      if (inside) minSpeed = Math.min(minSpeed, speed);
      if (inside && prevV) jerk = Math.max(jerk, Math.hypot(...v.map((x, i) => x - (prevV as number[])[i])) / speed);
      prevV = v;
    }
    prev = [...f.eye];
  }
  return { minSpeed, jerk, minHeight };
}
