// The precomputed landscape: one JSON manifest and one little-endian float32 file, written by
// `python -m eddie.cli build-landscape`. Every 2D block is t-major: index = it * nSigma + iSigma.

import { getBuffer, getJson, type Tier } from "../api";

export interface Budget {
  within: number;
  between: number;
  residual: number;
  opposition: number;
  groups: { group: number; share: number; side: number }[];
}

export interface ProofZero {
  sigma: number;
  t: number;
  charge: number;
  refined: boolean;
  by_chapter?: Budget;
  by_band?: Budget;
}

export interface PopulationMeta {
  mass: number;
  stats: { present: boolean; classes?: number; n1?: number; n2?: number; r_inf_half?: number };
  zeros_by_band: ProofZero[][];
  band_mass: number[];
  minima?: { t: number; r: number; sharp: boolean }[];
  peaks?: { t: number; r: number }[];
  alignment?: { available: boolean; p?: number };
}

export interface StripChapter {
  id: number;
  slug: string;
  title: string;
  tier: Tier;
  chapter: number;
  sigma: number;
  t: number;
  side: number;
  live: number;
  dead: number;
}

export interface Finding {
  id: string;
  status: "exact" | "stable" | "encoding-specific" | "null";
  headline: string;
  detail: string;
  at?: { sigma: number; t: number };
}

export interface LandscapeMeta {
  schema: string;
  build_id: string;
  stale: boolean | null;
  grid: {
    sigma: { min: number; max: number; n: number };
    t: { min: number; max: number; n: number };
    sigma_ext: { min: number; max: number; n: number };
    omega: { min: number; max: number; n: number };
  };
  fields: Record<string, { offset: number; shape: number[] }>;
  zeta: { zeros: { t: number; slope: number }[]; pole: [number, number] | null; functional_equation_error: number | null };
  proof: {
    modules: number;
    classes: number;
    kappa: number;
    unit: number;
    expected_zeros: number;
    band_edges: number[];
    live: PopulationMeta;
    dead: PopulationMeta;
  };
  params: { bands: number; t_max: number };
  counts: { live: number; dead: number };
  chapters: StripChapter[];
  arcs: { from: number; to: number; net: number; final: boolean }[];
  findings: Finding[];
}

export interface Landscape {
  meta: LandscapeMeta;
  nS: number;
  nT: number;
  bands: number;
  tMax: number;
  zeta: { re: Float32Array; im: Float32Array };
  /** population 0 = live, 1 = dead; part 0 = re, 1 = im */
  proof(pop: number, band: number, part: number): Float32Array;
  apron: Float32Array;
  spectrum: [Float32Array, Float32Array];
}

let pending: Promise<Landscape> | null = null;

function size(shape: number[]): number {
  return shape.reduce((a, b) => a * b, 1);
}

async function fetchLandscape(): Promise<Landscape> {
  const meta = await getJson<LandscapeMeta>("/api/landscape");
  if (meta.schema !== "eddie-landscape/1") throw new Error(`unexpected landscape schema ${meta.schema}`);
  const data = new Float32Array(await getBuffer(`/api/landscape/fields?v=${meta.build_id}`));
  const total = Object.values(meta.fields).reduce((a, f) => a + size(f.shape), 0);
  if (data.length !== total) throw new Error("the landscape file does not match its manifest: run build-landscape again");
  for (let i = 0; i < data.length; i++) if (!Number.isFinite(data[i])) data[i] = 0;
  const nS = meta.grid.sigma.n;
  const nT = meta.grid.t.n;
  const cells = nS * nT;
  const block = (name: string) => data.subarray(meta.fields[name].offset, meta.fields[name].offset + size(meta.fields[name].shape));
  const zeta = block("zeta");
  const proof = block("proof");
  const bands = meta.fields.proof.shape[1];
  const spectrum = block("spectrum");
  const nW = meta.grid.omega.n;
  return {
    meta, nS, nT, bands, tMax: meta.grid.t.max,
    zeta: { re: zeta.subarray(0, cells), im: zeta.subarray(cells, 2 * cells) },
    proof: (pop, band, part) => {
      const at = ((pop * bands + band) * 2 + part) * cells;
      return proof.subarray(at, at + cells);
    },
    apron: block("apron"),
    spectrum: [spectrum.subarray(0, nW), spectrum.subarray(nW, 2 * nW)],
  };
}

/** One shared load per page; a failed load is forgotten so "Try again" works. */
export function loadLandscape(): Promise<Landscape> {
  if (!pending) {
    pending = fetchLandscape().catch((e) => {
      pending = null;
      throw e;
    });
  }
  return pending;
}
