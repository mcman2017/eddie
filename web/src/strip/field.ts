// Turning complex fields into a surface: height, slope and a unit phasor per grid node.
//
// Height is a soft-clamped log magnitude, so zeros are wells of bounded depth and the pole is
// a bounded peak. Phase is carried as (cos, sin), never as an angle, so it can be interpolated
// across a triangle without tearing at the +-pi branch cut.

import type { Landscape } from "./landscape";

export const SX = 24; // world width of sigma in [0, 1]
export const SY = 2.4; // world height of one unit of H

export interface Surface {
  /** soft-clamped height, slope in world x and world z, and the unit phasor, per node */
  h: Float32Array;
  gx: Float32Array;
  gz: Float32Array;
  cos: Float32Array;
  sin: Float32Array;
  /** ln of the plotted magnitude (before clamping), for comparisons */
  log: Float32Array;
}

export function emptySurface(n: number): Surface {
  return { h: new Float32Array(n), gx: new Float32Array(n), gz: new Float32Array(n), cos: new Float32Array(n),
           sin: new Float32Array(n), log: new Float32Array(n) };
}

const softClamp = (u: number) => (u >= 0 ? Math.tanh(u) : -2 * Math.tanh(-u / 2));

function slopes(s: Surface, nS: number, nT: number, tMax: number): void {
  const dx = SX / (nS - 1);
  const dz = tMax / (nT - 1);
  const { h, gx, gz } = s;
  for (let j = 0; j < nT; j++) {
    const jm = Math.max(j - 1, 0);
    const jp = Math.min(j + 1, nT - 1);
    for (let i = 0; i < nS; i++) {
      const im = Math.max(i - 1, 0);
      const ip = Math.min(i + 1, nS - 1);
      const k = j * nS + i;
      gx[k] = (h[j * nS + ip] - h[j * nS + im]) / ((ip - im) * dx);
      gz[k] = -(h[jp * nS + i] - h[jm * nS + i]) / ((jp - jm) * dz); // world z = -t
    }
  }
}

/** log|zeta|: 0 is |zeta| = 1 (sea level), wells are zeros, the pole at s = 1 is a peak. */
export function zetaSurface(land: Landscape): Surface {
  const n = land.nS * land.nT;
  const s = emptySurface(n);
  const { re, im } = land.zeta;
  const pole = land.meta.zeta.pole;
  for (let k = 0; k < n; k++) {
    const mag = Math.hypot(re[k], im[k]);
    const l = mag > 0 ? Math.log(mag) : -12;
    s.log[k] = l;
    s.h[k] = softClamp(l / 1.4);
    s.cos[k] = mag > 0 ? re[k] / mag : 1;
    s.sin[k] = mag > 0 ? im[k] / mag : 0;
  }
  if (pole) {
    const k = pole[1] * land.nS + pole[0];
    s.log[k] = 6;
    s.h[k] = softClamp(6 / 1.4);
  }
  slopes(s, land.nS, land.nT, land.tMax);
  return s;
}

export interface WaveMix {
  /** weight of each harmonic band, 0..1 */
  bands: Float32Array;
  /** 0 = live, 1 = dead; values between are only the transition */
  pop: number;
}

/**
 * The proof wave as R = |Z(sigma + it)| / Z(sigma): each sigma column is divided by its own
 * level at t = 0, which removes the tilt across the strip without moving a zero or changing
 * the phase. Sea level (H = 0) is the population's exact long-run level on the line.
 */
export function waveSurface(land: Landscape, mix: WaveMix, out: Surface, scratch: { re: Float32Array; im: Float32Array }): Surface {
  const { nS, nT, bands } = land;
  const n = nS * nT;
  const { re, im } = scratch;
  re.fill(0);
  im.fill(0);
  for (let pop = 0; pop < 2; pop++) {
    const share = pop === 0 ? 1 - mix.pop : mix.pop;
    if (share <= 0) continue;
    // the population's level at t = 0, per sigma column, over all of its bands
    const level = new Float64Array(nS);
    for (let b = 0; b < bands; b++) {
      const r0 = land.proof(pop, b, 0);
      for (let i = 0; i < nS; i++) level[i] += r0[i];
    }
    for (let b = 0; b < bands; b++) {
      const w = mix.bands[b] * share;
      if (w <= 0) continue;
      const pr = land.proof(pop, b, 0);
      const pi = land.proof(pop, b, 1);
      for (let j = 0; j < nT; j++) {
        const row = j * nS;
        for (let i = 0; i < nS; i++) {
          const g = w / (level[i] || 1);
          re[row + i] += pr[row + i] * g;
          im[row + i] += pi[row + i] * g;
        }
      }
    }
  }
  const live = land.meta.proof.live.stats.r_inf_half ?? 0.05;
  const center = Math.log(live);
  for (let k = 0; k < n; k++) {
    const mag = Math.hypot(re[k], im[k]);
    const l = Math.log(Math.max(mag, 1e-6));
    out.log[k] = l;
    out.h[k] = softClamp((l - center) / 1.25);
    out.cos[k] = mag > 0 ? re[k] / mag : 1;
    out.sin[k] = mag > 0 ? im[k] / mag : 0;
  }
  slopes(out, nS, nT, land.tMax);
  return out;
}

/** R on the critical line for the current mix, straight from the surface's log column. */
export function criticalLine(land: Landscape, s: Surface): Float32Array {
  const half = Math.round((land.nS - 1) / 2);
  const out = new Float32Array(land.nT);
  for (let j = 0; j < land.nT; j++) out[j] = Math.exp(s.log[j * land.nS + half]);
  return out;
}

/** The tallest local maxima of a profile beyond tMin, in order of t. */
export function topPeaks(profile: Float32Array, tMax: number, count: number, tMin = 2): { t: number; r: number }[] {
  const dt = tMax / (profile.length - 1);
  const peaks: { t: number; r: number }[] = [];
  for (let j = 1; j < profile.length - 1; j++) {
    if (j * dt >= tMin && profile[j] > profile[j - 1] && profile[j] >= profile[j + 1]) peaks.push({ t: j * dt, r: profile[j] });
  }
  return peaks.sort((a, b) => b.r - a.r).slice(0, count).sort((a, b) => a.t - b.t);
}

export function heightAt(land: Landscape, s: Surface, sigma: number, t: number): number {
  const i = Math.min(Math.max(Math.round(sigma * (land.nS - 1)), 0), land.nS - 1);
  const j = Math.min(Math.max(Math.round((t / land.tMax) * (land.nT - 1)), 0), land.nT - 1);
  return s.h[j * land.nS + i];
}
