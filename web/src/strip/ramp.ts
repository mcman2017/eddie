// Height ramps as data: a row of sRGB bytes for the terrain shader, and the same ramp as a CSS
// gradient for the legend. Stops are interpolated in OKLab, so the steps between two hues are
// even to the eye and a blue-to-gold passage does not go muddy in the middle.

import { srgb, type RampStop } from "../theme";

const toLinear = (c: number) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
const toDisplay = (c: number) => (c <= 0.0031308 ? 12.92 * c : 1.055 * c ** (1 / 2.4) - 0.055);

function toOklab(hex: string): [number, number, number] {
  const [r, g, b] = srgb(hex).map(toLinear);
  const l = Math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b);
  const m = Math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b);
  const s = Math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b);
  return [
    0.2104542553 * l + 0.793617785 * m - 0.0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + 0.4505937099 * s,
    0.0259040371 * l + 0.7827717662 * m - 0.808675766 * s,
  ];
}

function toBytes([L, a, b]: [number, number, number]): [number, number, number] {
  const l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (L - 0.0894841775 * a - 1.291485548 * b) ** 3;
  const rgb = [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ];
  return rgb.map((c) => Math.round(255 * Math.min(1, Math.max(0, toDisplay(Math.min(1, Math.max(0, c))))))) as [number, number, number];
}

function at(stops: RampStop[], h: number): [number, number, number] {
  if (h <= stops[0][0]) return toBytes(toOklab(stops[0][1]));
  for (let i = 1; i < stops.length; i++) {
    if (h <= stops[i][0]) {
      const k = (h - stops[i - 1][0]) / (stops[i][0] - stops[i - 1][0]);
      const a = toOklab(stops[i - 1][1]);
      const b = toOklab(stops[i][1]);
      return toBytes([a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k]);
    }
  }
  return toBytes(toOklab(stops[stops.length - 1][1]));
}

/** `n` RGBA texels covering heights lo..hi, left to right. */
export function bakeRamp(stops: RampStop[], lo: number, hi: number, n = 256): Uint8Array {
  const out = new Uint8Array(n * 4);
  for (let i = 0; i < n; i++) {
    const [r, g, b] = at(stops, lo + ((hi - lo) * i) / (n - 1));
    out.set([r, g, b, 255], i * 4);
  }
  return out;
}

/** The ramp as a CSS gradient, sampled finely enough to match the texture. */
export function rampGradient(stops: RampStop[], lo: number, hi: number, samples = 24): string {
  const parts = Array.from({ length: samples + 1 }, (_, i) => {
    const [r, g, b] = at(stops, lo + ((hi - lo) * i) / samples);
    return `rgb(${r} ${g} ${b}) ${((100 * i) / samples).toFixed(1)}%`;
  });
  return `linear-gradient(to right, ${parts.join(", ")})`;
}
