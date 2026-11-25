// Typed client for Eddie's read-only API. Nothing here can trigger a paid model call.

export interface BudgetSnapshot {
  available: boolean;
  generated_at?: string;
  lifetime_cap_usd?: number;
  committed_usd?: number;
  inflight_worst_case_usd?: number;
  inflight_reserved_usd?: number;
  headroom_usd?: number;
  daily_cap_usd?: number;
  daily_committed_usd?: number;
  run_cap_usd?: number;
  breaker?: { state: string; reason: string | null };
  kill_switch?: boolean;
  calls_by_state?: Record<string, number>;
}

export interface Forecast {
  signature_bytes: number;
  input_usd_typical: number;
  input_usd_upper_bound: number;
  output_usd_at_tokens_per_decl: number;
  output_tokens_per_decl: number;
  total_usd_typical: number;
}

export interface IndexStats {
  modules: number;
  source_lines: number;
  decls: number;
  decls_by_kind: Record<string, number>;
  sorry_count: number;
  reachable_from_main: number;
  max_depth: number;
  top_pagerank: [string, number, number][];
  forecast_r1_all_signatures: Forecast;
}

export interface SearchHit {
  id: number;
  kind: string;
  full_name: string | null;
  signature: string;
  module: string;
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
  return (await res.json()) as T;
}

export const api = {
  health: () => getJson<{ ok: boolean; version: string; index: boolean }>("/healthz"),
  budget: () => getJson<BudgetSnapshot>("/api/budget"),
  stats: () => getJson<IndexStats>("/api/index/stats"),
  search: (q: string, limit = 25) =>
    getJson<{ query: string; results: SearchHit[] }>(`/api/search?q=${encodeURIComponent(q)}&limit=${limit}`),
};
