// Deep links live in the URL hash as key=value pairs. Every writer merges, so opening a
// declaration never drops the view, the chapter or the shot.
//
//   #view=strip|wheel  #mode=strip|wave  #pop=dead|compare  #bands=<n>  #chapter=<slug>
//   #shot=1..5 (a shot of the visual tour)  #cam=dive|finale (older camera presets)
//   #decl=<id>  #node=<id>
//   #cinema (a bare flag: the capture layout, with the drawer and the controls put away)
//   #freeze=<seconds>  #tour=<seconds> (deterministic screenshots: a frozen clock, a tour frame)

export type RouteKey = "view" | "mode" | "chapter" | "cam" | "shot" | "tour" | "cinema" | "decl" | "node" | "freeze" | "pop" | "bands";

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

/** True when a bare flag such as #cinema is present (with any value but 0). */
export function routeFlag(key: RouteKey): boolean {
  const params = readRoute();
  return params.has(key) && params.get(key) !== "0";
}

/** Merge a patch into the hash without adding a history entry; null removes a key. */
export function writeRoute(patch: Partial<Record<RouteKey, string | number | null>>): void {
  const params = readRoute();
  for (const [key, value] of Object.entries(patch)) {
    if (value == null || value === "") params.delete(key);
    else params.set(key, String(value));
  }
  // a bare flag stays bare: "cinema", not "cinema="
  const next = params.toString().replace(/(^|&)cinema=(?=&|$)/, "$1cinema");
  if (next !== window.location.hash.slice(1)) window.history.replaceState(null, "", next ? `#${next}` : window.location.pathname);
}
