// Design tokens shared by canvas and CSS (styles.css mirrors these as custom properties).
//
// The proof is drawn like tissue under a fluorescence microscope: live declarations are a
// deep blue stain that brightens as Eddie works on them (one hue, monotone lightness, checked
// with the ordinal-ramp validator against the map surface), and code outside the proof is
// unstained, drawn only as a dashed outline. Ember orange is reserved for energy: spend and
// the brain region that is currently active.

export type Status = "verified" | "triaged" | "live" | "dead";

export const COLORS = {
  ground: "#0b1220",
  raised: "#111a2c",
  ink: "#f0efec",
  ink2: "#c3c2b7",
  ink3: "#898781",
  ember: "#eb6834",
  critical: "#d03b3b",
} as const;

export const STATUS_FILL: Record<Status, string> = {
  verified: "#cde2fb",
  triaged: "#3987e5",
  live: "#184f95",
  dead: "#898781",
};

export const STATUS_LABEL: Record<Status, string> = {
  verified: "Verified witness",
  triaged: "Triaged",
  live: "Unvisited live",
  dead: "Pruned dead",
};

// The one-hue ramp of the witness plots (dark ground to the verified light), used by the 3D
// terrain: evenly spaced in perceived lightness, so height reads as stain intensity.
export const RAMP = ["#0b1220", "#0d366b", "#1c5cab", "#3987e5", "#86b6ef", "#cde2fb"] as const;

// Dead code in 3D: the same idea in a cold, unstained indigo.
export const DEAD_RAMP = ["#0b0e1c", "#151a36", "#232a58", "#3a4288"] as const;

// The four acts are an order, so they share the hue and step in lightness (ordinal ramp).
export const TIER_FILL = {
  foundations: "#1c5cab",
  machinery: "#3987e5",
  bottleneck: "#86b6ef",
  main: "#cde2fb",
} as const;

export const TIER_LABEL = {
  foundations: "Foundations",
  machinery: "Intermediate machinery",
  bottleneck: "Bottleneck",
  main: "Main",
} as const;

/** "#rrggbb" as display-referred [r, g, b] in 0..1 (what a shader should output unchanged). */
export function srgb(hex: string): [number, number, number] {
  const n = parseInt(hex.slice(1), 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}
