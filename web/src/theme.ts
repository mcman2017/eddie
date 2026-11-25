// Design tokens shared by canvas, WebGL and CSS. The values live in palette.json; styles.css
// repeats the chrome ones as custom properties and eddie/sandbox/style.py mirrors them for the
// Python plots (tests/test_palette_parity.py keeps the two equal).
//
// The explorer is ink on gallery paper. Chrome is neutral, so hue is spent only on data: the
// four acts, the height of the two surfaces and the critical line. A status is a shape (check
// badge, dot, ring, dashed ring), never a hue. Ember orange marks energy (spend and the active
// brain region) and is the colour of the last act, where the proof ends.

import palette from "./palette.json";

export type Status = "verified" | "triaged" | "live" | "dead";
export type RampStop = [number, string];

export const COLORS = {
  /** the page, and the sky of the 3D view */
  ground: palette.surface.page,
  /** recessed areas: code blocks, tracks, the 3D ground plane */
  well: palette.surface.well,
  raised: palette.surface.card,
  line: palette.surface.line,
  lineStrong: palette.surface.lineStrong,
  ink: palette.ink.primary,
  ink2: palette.ink.secondary,
  ink3: palette.ink.muted,
  ember: palette.act.main,
  critical: palette.status.critical,
  gold: palette.criticalLine.core,
  crimson: palette.criticalLine.casing,
  zeroRing: palette.zero.ring,
  zeroCentre: palette.zero.centre,
  chalk: palette.dead.chalk,
  slate: palette.dead.slate,
} as const;

// Status marks are neutral: how far Eddie has worked on a declaration reads as how much ink it
// carries, so it never competes with the act colours.
export const STATUS_FILL: Record<Status, string> = {
  verified: palette.ink.primary,
  triaged: palette.ink.muted,
  live: palette.surface.lineStrong,
  dead: palette.dead.slate,
};

export const STATUS_LABEL: Record<Status, string> = {
  verified: "Seen and verified",
  triaged: "Summarised",
  live: "Live, not yet visited",
  dead: "Dead code",
};

// The four acts are identities, so each has its own hue. The set is validated for colour-blind
// separation on every surface it is drawn on; cobalt and magenta-violet only stay apart because
// they differ in lightness, so do not re-step one of them by eye.
export const TIER_FILL = {
  foundations: palette.act.foundations,
  machinery: palette.act.machinery,
  bottleneck: palette.act.bottleneck,
  main: palette.act.main,
} as const;

export const TIER_LABEL = {
  foundations: "Foundations",
  machinery: "Machinery",
  bottleneck: "Bottleneck",
  main: "Summit (Main.lean)",
} as const;

export const ACT_NUMERAL = {
  foundations: "Act I",
  machinery: "Act II",
  bottleneck: "Act III",
  main: "Act IV",
} as const;

/** "Act II: Machinery" */
export function actName(tier: keyof typeof TIER_LABEL): string {
  return `${ACT_NUMERAL[tier]}: ${TIER_LABEL[tier]}`;
}

// Height ramps of the two surfaces, as (height, colour) stops. Height 0 is "sea level": |zeta| = 1
// on the strip, the long-run level on the wave. Both run dark at the extremes and light at sea
// level, so wells and peaks stand off the paper and the contour lines stay readable everywhere.
export const RAMP_ZETA = palette.rampZeta as RampStop[];
export const RAMP_WAVE = palette.rampWave as RampStop[];

// Interim stops for the 3D terrain until it samples the ramps above as a texture.
export const RAMP = ["#101E6E", "#1C77C3", "#6CC5A0", "#F1E7A6", "#F28C38", "#C72C73"] as const;
export const DEAD_RAMP = ["#C9C5BC", "#D9D5CC", "#E6E2DA", "#F0EDE6"] as const;

/** "#rrggbb" as display-referred [r, g, b] in 0..1 (what a shader should output unchanged). */
export function srgb(hex: string): [number, number, number] {
  const n = parseInt(hex.slice(1), 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}

export function hexAlpha(hex: string, alpha: number): string {
  const [r, g, b] = srgb(hex);
  return `rgba(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)}, ${alpha.toFixed(3)})`;
}
