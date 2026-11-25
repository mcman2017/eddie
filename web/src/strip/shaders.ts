// GLSL for the critical strip. Colours are display-referred: uniforms carry sRGB values and the
// shaders write them unchanged (no <colorspace_fragment>), so a theme hex looks the same here
// as in the 2D map. three compiles these as GLSL ES 3.00 with its compatibility defines.

const COMMON = /* glsl */ `
  uniform vec3 uGround;
  vec3 fogged(vec3 col, vec3 world) {
    float d = length(world - cameraPosition);
    return mix(col, uGround, 1.0 - exp(-pow(0.0062 * d, 2.0)));
  }
  vec3 dithered(vec3 col) {
    // half a display step of noise: dark fog gradients band badly after video compression
    return col + (fract(sin(dot(gl_FragCoord.xy, vec2(12.9898, 78.233))) * 43758.5453) - 0.5) / 255.0;
  }
`;

export const TERRAIN_VERT = /* glsl */ `
  attribute vec3 aA;      // zeta:        height, slope in x, slope in z
  attribute vec3 aB;      // proof wave:  height, slope in x, slope in z
  attribute vec2 aPhA;    // unit phasors (cos, sin). A raw angle is never interpolated.
  attribute vec2 aPhB;
  attribute float aDiff;  // ln R(live) - ln R(dead), for Compare
  uniform float uMorph;
  uniform float uSweep;
  uniform float uSY;
  uniform float uSX;
  uniform float uTMax;
  varying float vH;
  varying float vM;
  varying float vDiff;
  varying vec2 vST;
  varying vec2 vPhA;
  varying vec2 vPhB;
  varying vec3 vN;
  varying vec3 vWorld;

  void main() {
    float t01 = -position.z / uTMax;
    // the morph runs up the strip as a front instead of fading everywhere at once
    float m = uSweep > 0.0 ? smoothstep(0.0, uSweep, uMorph * (1.0 + uSweep) - t01) : uMorph;
    float h = mix(aA.x, aB.x, m);
    vec2 g = mix(aA.yz, aB.yz, m);
    vH = h;
    vM = m;
    vDiff = aDiff;
    vST = vec2(position.x / uSX + 0.5, -position.z);
    vPhA = aPhA;
    vPhB = aPhB;
    vN = vec3(-g.x * uSY, 1.0, -g.y * uSY);
    vec4 world = modelMatrix * vec4(position.x, h * uSY, position.z, 1.0);
    vWorld = world.xyz;
    gl_Position = projectionMatrix * viewMatrix * world;
  }
`;

export const TERRAIN_FRAG = /* glsl */ `
  uniform vec3 uRamp[6];
  uniform vec3 uInk;
  uniform vec3 uLight;
  uniform float uPhase;     // strength of the phase lines
  uniform float uCompare;   // 1 while Live and Dead are compared
  uniform vec2 uZetaWells[16];   // (sigma, t) of the zeros of zeta in the box
  uniform int uZetaCount;
  uniform vec2 uWaveWells[64];   // zeros of the proof wave for the bands and population shown
  uniform int uWaveCount;
  varying float vH;
  varying float vM;
  varying float vDiff;
  varying vec2 vST;
  varying vec2 vPhA;
  varying vec2 vPhB;
  varying vec3 vN;
  varying vec3 vWorld;
  ${COMMON}

  vec3 ramp(float c) {
    float x = clamp(c, 0.0, 1.0) * 5.0;
    int i = int(min(floor(x), 4.0));
    return mix(uRamp[i], uRamp[i + 1], x - float(i));
  }

  // n lines of constant phase from an interpolated phasor. fract(n * theta / 2 pi) is continuous
  // across the +-pi cut, and the line width comes from the phasor's own gradient rather than
  // from fwidth(theta), which would spike there. Lines fade where they crowd (near a zero the
  // interpolated phasor shrinks and the lines converge), so they dissolve instead of aliasing.
  float phaseLines(vec2 u, float n) {
    float k = n / 6.2831853;
    float f = atan(u.y, u.x) * k;
    float inv = k / max(dot(u, u), 1e-6);
    float fw = abs((u.x * dFdx(u.y) - u.y * dFdx(u.x)) * inv) + abs((u.x * dFdy(u.y) - u.y * dFdy(u.x)) * inv);
    float d = abs(fract(f - 0.5) - 0.5) / max(fw, 1e-6);   // distance to the nearest line, in pixels
    return (1.0 - smoothstep(0.5, 1.5, d)) * (1.0 - smoothstep(0.22, 0.5, fw)) * smoothstep(0.2, 0.7, length(u));
  }

  void main() {
    // wells glow: lightness follows -H, as in the seeded strip plot
    vec3 col = ramp(0.42 - 0.25 * vH);
    vec3 n = normalize(vN);
    float lambert = dot(n, normalize(uLight)) * 0.5 + 0.5;
    col *= mix(0.6, 1.14, lambert * lambert);
    col += uInk * 0.05 * pow(1.0 - max(dot(n, normalize(cameraPosition - vWorld)), 0.0), 3.0);

    float f = vH * 4.0;
    float iso = 1.0 - smoothstep(0.6, 1.6, abs(fract(f - 0.5) - 0.5) / max(fwidth(f), 1e-5));
    col = mix(col, col * 1.32 + 0.012, iso * 0.4 * (1.0 - smoothstep(0.25, 0.6, fwidth(f))));

    float lines = phaseLines(vPhA, 8.0) * (1.0 - vM) + phaseLines(vPhB, 8.0) * vM;
    col = mix(col, uInk, lines * 0.2 * uPhase);

    float ds = abs(vST.x - 0.5);
    float core = 1.0 - smoothstep(0.0, max(0.0035, 0.9 * fwidth(vST.x)), ds);
    col += uInk * (core * 0.6 + exp(-pow(ds / 0.045, 2.0)) * 0.1);

    // zeros glow from inside their wells. The falloff is measured in world units, so the glow is
    // round on screen although sigma is stretched.
    float wells = 0.0;
    if (vM < 0.99) {
      float a = 0.0;
      for (int i = 0; i < 16; i++) {
        if (i >= uZetaCount) break;
        vec2 d = (vST - uZetaWells[i]) * vec2(24.0, 1.0);
        a += exp(-dot(d, d) / 0.5);
      }
      wells += a * (1.0 - vM);
    }
    if (vM > 0.01) {
      float b = 0.0;
      for (int i = 0; i < 64; i++) {
        if (i >= uWaveCount) break;
        vec2 d = (vST - uWaveWells[i]) * vec2(24.0, 1.0);
        b += exp(-dot(d, d) / 0.4);
      }
      wells += b * vM;
    }
    col += uInk * min(wells, 1.0) * 0.8;

    // Compare: where the live and dead waves differ most, the live surface lights up
    col += uInk * uCompare * vM * 0.42 * smoothstep(0.7, 2.4, abs(vDiff));

    float edge = smoothstep(0.0, 0.012, min(vST.x, 1.0 - vST.x));
    col = mix(col * 0.55 + uInk * 0.1, col, edge);
    gl_FragColor = vec4(dithered(fogged(col, vWorld)), 1.0);
  }
`;

export const APRON_VERT = /* glsl */ `
  attribute float aH;
  uniform float uBase;
  uniform float uScale;
  uniform float uSX;
  varying float vH;
  varying vec2 vST;
  varying vec3 vWorld;
  void main() {
    vH = aH;
    vST = vec2(position.x / uSX + 0.5, -position.z);
    vec4 world = modelMatrix * vec4(position.x, uBase + aH * uScale, position.z, 1.0);
    vWorld = world.xyz;
    gl_Position = projectionMatrix * viewMatrix * world;
  }
`;

export const APRON_FRAG = /* glsl */ `
  uniform vec3 uDead[4];
  uniform vec3 uInk;
  varying float vH;
  varying vec2 vST;
  varying vec3 vWorld;
  ${COMMON}
  void main() {
    if (vST.x > 0.0 && vST.x < 1.0) discard;   // the strip itself belongs to the function
    float x = clamp(vH * 1.15, 0.0, 1.0) * 3.0;
    int i = int(min(floor(x), 2.0));
    vec3 col = mix(uDead[i], uDead[i + 1], x - float(i));
    // dashed contours: dead code keeps its dashed, unstained look
    float f = vH * 6.0;
    float line = 1.0 - smoothstep(0.6, 1.6, abs(fract(f - 0.5) - 0.5) / max(fwidth(f), 1e-5));
    float dash = step(0.5, fract((vST.y + vST.x * 9.0) * 1.3));
    col += uInk * 0.1 * line * dash * step(0.02, vH);
    gl_FragColor = vec4(dithered(fogged(col, vWorld)), 1.0);
  }
`;

// Round additive sprites: beacon lamps and zero markers.
export const GLOW_VERT = /* glsl */ `
  attribute float aSize;
  attribute float aPulse;
  attribute float aAlpha;
  uniform float uPx;
  varying float vAlpha;
  varying float vPulse;
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_Position = projectionMatrix * mv;
    gl_PointSize = clamp(aSize * (1.0 + 1.5 * aPulse) * uPx / -mv.z, 2.0, 120.0);
    vAlpha = aAlpha;
    vPulse = aPulse;
  }
`;

export const GLOW_FRAG = /* glsl */ `
  uniform vec3 uColor;
  uniform float uRing;
  varying float vAlpha;
  varying float vPulse;
  void main() {
    float r = length(gl_PointCoord * 2.0 - 1.0);
    if (r > 1.0) discard;
    float core = smoothstep(0.42, 0.0, r);
    float halo = (1.0 - r) * (1.0 - r);
    float ring = (1.0 - smoothstep(0.0, 0.1, abs(r - 0.7))) * uRing;
    gl_FragColor = vec4(uColor, (core * mix(0.95, 0.25, uRing) + halo * 0.4 + ring * 0.9) * vAlpha * (1.0 + 0.8 * vPulse));
  }
`;

// Particles riding the flow arcs: the cubic Bezier is evaluated on the GPU from attributes.
export const FLOW_VERT = /* glsl */ `
  attribute vec3 aP1;
  attribute vec3 aP2;
  attribute vec3 aP3;
  attribute vec4 aSeed;   // phase, speed, size, lag behind the head of its comet
  uniform float uTime;
  uniform float uPx;
  uniform float uFlow;
  varying float vAlpha;
  void main() {
    float u = fract(aSeed.x + uTime * aSeed.y - aSeed.w);
    float e = u * u * (3.0 - 2.0 * u);   // leave slowly, arrive slowly
    float a = 1.0 - e;
    vec3 p = a * a * a * position + 3.0 * a * a * e * aP1 + 3.0 * a * e * e * aP2 + e * e * e * aP3;
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    gl_Position = projectionMatrix * mv;
    gl_PointSize = clamp(aSeed.z * uPx / -mv.z, 1.5, 16.0);
    vAlpha = sin(3.14159265 * u) * uFlow * (1.0 - aSeed.w * 18.0);
  }
`;

export const FLOW_FRAG = /* glsl */ `
  uniform vec3 uColor;
  varying float vAlpha;
  void main() {
    float r = length(gl_PointCoord * 2.0 - 1.0);
    if (r > 1.0) discard;
    gl_FragColor = vec4(uColor, (1.0 - r) * vAlpha);
  }
`;

// Arc bodies: dim at the provider, bright at the consumer, so direction reads in a still frame.
export const TUBE_VERT = /* glsl */ `
  attribute float aW;
  varying float vU;
  varying float vW;
  void main() {
    vU = uv.x;
    vW = aW;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;

export const TUBE_FRAG = /* glsl */ `
  uniform vec3 uColor;
  uniform float uFlow;
  varying float vU;
  varying float vW;
  void main() {
    gl_FragColor = vec4(uColor, mix(0.04, 0.5, vU * vU) * (0.35 + 0.65 * vW) * uFlow);
  }
`;
