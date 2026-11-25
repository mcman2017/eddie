// Typed client for Eddie's read-only API. Nothing here can trigger a paid model call.

export interface BudgetSnapshot {
  available: boolean;
  lifetime_cap_usd?: number;
  committed_usd?: number;
  inflight_worst_case_usd?: number;
  headroom_usd?: number;
  daily_cap_usd?: number;
  daily_committed_usd?: number;
  breaker?: { state: string; reason: string | null };
  kill_switch?: boolean;
}

export type Tier = "foundations" | "machinery" | "bottleneck" | "main";

/** Net reference flow between two pillars: `to` builds on `from`. */
export interface PillarFlow {
  from: number;
  to: number;
  net: number;
  forward: number;
  backward: number;
}

export interface PillarSummary {
  id: number;
  slug: string;
  title: string;
  subtitle: string | null;
  category: string;
  ord: number;
  /** Position in the proof's flow, 0 = most foundational; `chapter` is flow_ord + 1. */
  flow_ord: number;
  chapter: number;
  tier: Tier;
  ref_in: number;
  ref_out: number;
  live: number;
  dead: number;
  decls: number;
  modules: number;
  node_id: number;
  head: { id: number | null; full_name: string | null };
  timeline: { number: number; phrase: string }[];
}

export interface Overview {
  archive: { files: number; decls: number; live: number; dead: number; named: number; dead_named: number; dead_pct: number; sorry: number };
  cone: { edges: number; live_edges: number; max_depth: number; agreement: { precision?: number; recall?: number } };
  roots: { id: number; full_name: string }[];
  pillars: PillarSummary[];
  cross_pillar_edges: { source: number; target: number; weight: number }[];
  pillar_flows: PillarFlow[];
  flow: { forward_pct: number; method: string | null };
  featured: { decl_id: number; full_name: string; kinds: string[]; statuses: string[] }[];
  artifact_sources?: Record<string, number>;
  plan: null | {
    plan_hash: string;
    planned_usd: number;
    totals: { tier2: { decls: number }; tier3: { milestones: number } };
  };
}

export interface MapChild {
  id: number | string;
  kind: "root" | "pillar" | "dir" | "group" | "module" | "decl";
  pillar_id?: number | null;
  label: string;
  path?: string;
  decls: number;
  live: number;
  dead: number;
  modules?: number;
  verified?: number;
  triaged?: number;
  top_decl_id?: number | null;
  // decl leaves only
  decl_id?: number;
  decl_kind?: string;
  full_name?: string | null;
  score?: number;
  status?: "verified" | "triaged" | "live" | "dead";
  is_root?: number;
}

export interface MapPage {
  node: MapChild & { id: number };
  breadcrumb: { id: number; label: string; kind: string }[];
  children: MapChild[];
  more: null | { count: number; live: number; decls: number };
}

export interface Witness {
  id: number;
  kind: "plot" | "audio" | "spectrogram" | "animation" | "data";
  title: string | null;
  status: "pending" | "verified" | "rejected";
  url: string;
  defects: string[];
  supersedes_id: number | null;
  pair_id: number | null;
  source: string;
  critic_verdict: Record<string, unknown> | null;
  python_source: string | null;
  qc: Record<string, unknown> | null;
  bytes: number | null;
}

export interface DeclDetail {
  id: number;
  kind: string;
  full_name: string | null;
  signature: string;
  docstring: string | null;
  context: string | null;
  citation: { module: string; path: string; start_line: number; end_line: number };
  metrics: {
    live: boolean;
    depth: number;
    dom_mass: number;
    dominator: string | null;
    dominator_id: number | null;
    score: number;
    fan_in: number;
    fan_out: number;
    pillar_rank: number | null;
    is_root: boolean;
  };
  pillar: { id: number; title: string; slug: string };
  node_id: number | null;
  status: "verified" | "triaged" | "live" | "dead";
  triage: { gloss: string; source: string } | null;
  intuition: { intuition_md: string; verdict: string; source: string } | null;
  witnesses: Witness[];
  reviews: { region: string; verdict: string; reasons: string[]; source: string; witness_id: number | null }[];
  planned: { tier: number; kind: string; scheduled_day: string | null; status: string } | null;
}

export interface ChapterNeighbour {
  slug: string;
  title: string;
  chapter: number;
  net: number;
}

export interface ChapterTheorem {
  id: number;
  kind: string;
  full_name: string;
  signature: string;
  dom_mass: number;
  dom_share: number;
  status: "verified" | "triaged" | "live" | "dead";
}

export interface ChapterSummary {
  pillar_id: number;
  slug: string;
  title: string;
  tier: Tier;
  tier_label: string;
  chapter: number;
  chapters: number;
  modules: number;
  live: number;
  dead: number;
  net: number;
  ref_in: number;
  ref_out: number;
  remark_steps: { number: number; phrase: string }[];
  prev: string | null;
  next: string | null;
  /** Who wrote the chapter text: "seed" is a $0 structural stub, never model output. */
  source: string | null;
  badge: string | null;
  node_id: number | null;
  verified_witnesses: number;
}

export interface ChapterDetail extends ChapterSummary {
  live_total: number;
  synthesis_md: string | null;
  builds_on: ChapterNeighbour[];
  feeds: ChapterNeighbour[];
  bottlenecks: ChapterTheorem[];
  final_theorems: ChapterTheorem[];
  witnesses: (Witness & { decl_id: number; decl_full_name: string })[];
  reviews: DeclDetail["reviews"];
  planned: { milestones: number; first_day: string | null; consolidation_day: string | null } | null;
}

export interface Neighbors {
  uses: { id: number; full_name: string; kind: string; live: number }[];
  used_by: { id: number; full_name: string; kind: string; live: number }[];
}

export interface Activity {
  region: string | null;
  decl_full_name?: string | null;
  message?: string | null;
  source?: string | null;
}

/** A failed request. The message keeps the old `url: HTTP status` prefix and adds the API's own hint. */
export class ApiError extends Error {
  constructor(
    readonly url: string,
    readonly status: number,
    readonly detail: string | null,
  ) {
    super(`${url}: HTTP ${status}${detail ? ` (${detail})` : ""}`);
  }
}

async function fail(url: string, res: Response): Promise<never> {
  let detail: string | null = null;
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") detail = body.detail;
  } catch {
    /* not JSON */
  }
  throw new ApiError(url, res.status, detail);
}

export async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  if (!res.ok) return fail(url, res);
  return (await res.json()) as T;
}

export async function getBuffer(url: string): Promise<ArrayBuffer> {
  const res = await fetch(url);
  if (!res.ok) return fail(url, res);
  return res.arrayBuffer();
}

export const api = {
  overview: () => getJson<Overview>("/api/overview"),
  budget: () => getJson<BudgetSnapshot>("/api/budget"),
  activity: () => getJson<Activity>("/api/activity"),
  node: (id: number, include?: number) =>
    getJson<MapPage>(`/api/map/node/${id}${include != null ? `?include=${include}` : ""}`),
  decl: (id: number) => getJson<DeclDetail>(`/api/decl/${id}`),
  neighbors: (id: number) => getJson<Neighbors>(`/api/decl/${id}/neighbors?limit=8`),
  declBody: (id: number) => getJson<{ body?: string }>(`/api/decl/${id}?body=1`),
  chapters: () => getJson<{ chapters: ChapterSummary[]; stub_badge: string; flow: { forward_pct: number } }>("/api/chapters"),
  chapter: (slug: string) => getJson<ChapterDetail>(`/api/chapters/${encodeURIComponent(slug)}`),
  search: (q: string) =>
    getJson<{ results: { id: number; kind: string; full_name: string | null; module: string; signature: string }[] }>(
      `/api/search?q=${encodeURIComponent(q)}&limit=12`,
    ),
};
