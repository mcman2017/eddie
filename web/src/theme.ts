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
