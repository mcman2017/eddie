// Deep links live in the URL hash as key=value pairs. Every writer merges, so opening a
// declaration never drops the view, the chapter or the camera preset.
//
//   #view=strip|wheel  #mode=strip|wave  #chapter=<slug>  #cam=overview|dive|finale
//   #decl=<id>  #node=<id>  #freeze=<seconds> (deterministic screenshots)

export type RouteKey = "view" | "mode" | "chapter" | "cam" | "decl" | "node" | "freeze" | "pop" | "bands";

export function readRoute(): URLSearchParams {
  return new URLSearchParams(window.location.hash.slice(1));
}

export function routeNumber(key: RouteKey): number | null {
  const params = readRoute();
  if (!params.has(key)) return null;
  const v = Number(params.get(key));
  return Number.isFinite(v) && v >= 0 ? v : null;
}

export function routeText(key: RouteKey): string | null {
  return readRoute().get(key);
}

/** Merge a patch into the hash without adding a history entry; null removes a key. */
export function writeRoute(patch: Partial<Record<RouteKey, string | number | null>>): void {
  const params = readRoute();
  for (const [key, value] of Object.entries(patch)) {
    if (value == null || value === "") params.delete(key);
    else params.set(key, String(value));
  }
  const next = params.toString();
  if (next !== window.location.hash.slice(1)) window.history.replaceState(null, "", next ? `#${next}` : window.location.pathname);
}
