// GLSL for the critical strip, drawn as a painted relief model on paper. Colours are
// display-referred: uniforms and the ramp textures carry sRGB values and the shaders write them
// unchanged (no <colorspace_fragment>), so a palette hex looks the same here as in CSS. three
// compiles these as GLSL ES 3.00 with its compatibility defines.
//
// Nothing here is additive. On a light ground a glow adds up to white and disappears, so lines
// are mixed toward ink, and every mark carries its own contrast: a white ring and an ink hairline
// around a coloured body, because no single colour stands off every colour of the two ramps.
// Opaque passes write alpha 1 (the drawing buffer always has an alpha channel).

const COMMON = /* glsl */ `
  uniform vec3 uSky;
  vec3 fogged(vec3 col, vec3 world) {
    float d = length(world - cameraPosition);
    return mix(col, uSky, 1.0 - exp(-pow(0.0036 * d, 2.0)));
  }
  vec3 dithered(vec3 col) {
    // half a display step of interleaved gradient noise: smooth gradients band after video compression
    return col + (fract(52.9829189 * fract(dot(gl_FragCoord.xy, vec2(0.06711056, 0.00583715)))) - 0.5) / 255.0;
  }
  // Screen-space gradient length. fwidth() is the L1 norm, which makes a line breathe as the
  // camera orbits; this is the L2 norm.
  float grad(float f) {
    return length(vec2(dFdx(f), dFdy(f)));
  }
  // Coverage of a line of half-width w pixels at distance d (in the units of gradient g).
  float lineCov(float d, float g, float w) {
    return 1.0 - smoothstep(w - 0.5, w + 0.5, d / max(g, 1e-6));
  }
  // 1 inside a disc of radius r0, with an edge one pixel (px, in the units of r) wide
  float disc(float r, float r0, float px) {
    return 1.0 - smoothstep(r0 - 0.5 * px, r0 + 0.5 * px, r);
  }
`;

export const TERRAIN_VERT = /* glsl */ `
  attribute vec3 aA;      // zeta:        height, slope in x, slope in z
  attribute vec3 aB;      // proof wave:  height, slope in x, slope in z
  attribute vec2 aPhA;    // unit phasors (cos, sin). A raw angle is never interpolated.
  attribute vec2 aPhB;
  attribute float aDiff;  // ln R(live) - ln R(dead), for Compare
  attribute vec3 aSkirt;  // side walls: (1 at the foot of the wall, outward normal x, z); 0 on the surface
  uniform float uMorph;
  uniform float uSweep;
  uniform float uSY;
  uniform float uSX;
  uniform float uTMax;
  uniform float uFloor;
  varying float vH;
  varying float vM;
  varying float vDiff;
  varying float vWall;
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
    float wall = step(0.5, dot(aSkirt.yz, aSkirt.yz));
    // a wall drops from the surface to the table; its colour is the height it passes through
    float y = mix(h * uSY, uFloor, aSkirt.x);
    vH = y / uSY;
    vM = m;
    vDiff = aDiff;
    vWall = wall;
    vST = vec2(position.x / uSX + 0.5, -position.z);
    vPhA = aPhA;
    vPhB = aPhB;
    vN = mix(vec3(-g.x * uSY, 1.0, -g.y * uSY), vec3(aSkirt.y, 0.0, aSkirt.z), wall);
    vec4 world = modelMatrix * vec4(position.x, y, position.z, 1.0);
    vWorld = world.xyz;
    gl_Position = projectionMatrix * viewMatrix * world;
  }
`;

export const TERRAIN_FRAG = /* glsl */ `
  uniform sampler2D uRampA;      // zeta: colour of each height, -2..1 across the texture
  uniform sampler2D uRampB;      // the proof wave
  uniform vec3 uInk;
  uniform vec3 uPaper;
  uniform vec3 uGold;            // the critical line: gold core in a crimson casing
  uniform vec3 uCrimson;
  uniform vec3 uRing;            // a zero of zeta: emerald ring, gold centre
  uniform vec3 uCentre;
  uniform vec3 uChalk;           // dead code: chalk at the top, slate at the bottom
  uniform vec3 uSlate;
  uniform vec3 uLight;           // unit vector, fixed in the world
  uniform float uPhase;          // strength of the phase lines
  uniform float uCompare;        // 1 while Live and Dead are compared
  uniform float uDead;           // 0 = live, 1 = dead: the wave keeps its shape and loses its colour
  uniform float uTime;
  uniform float uPxRatio;        // device pixels per CSS pixel, so line widths are in CSS pixels
  uniform float uSX;
  uniform float uTMax;
  uniform vec2 uZetaWells[16];   // (sigma, t) of the zeros of zeta in the box
  uniform float uZetaPulse[16];  // 1 when the synth sounds that zero, decaying to 0
  uniform int uZetaCount;
  varying float vH;
  varying float vM;
  varying float vDiff;
  varying float vWall;
  varying vec2 vST;
  varying vec2 vPhA;
  varying vec2 vPhB;
  varying vec3 vN;
  varying vec3 vWorld;
  ${COMMON}

  // n lines of constant phase from an interpolated phasor. fract(n * theta / 2 pi) is continuous
  // across the +-pi cut, and the line width comes from the phasor's own gradient rather than
  // from fwidth(theta), which would spike there. Lines fade where they crowd (near a zero the
  // interpolated phasor shrinks and the lines converge), so they dissolve instead of aliasing.
  float phaseLines(vec2 u, float n) {
    float k = n / 6.2831853;
    float f = atan(u.y, u.x) * k;
    float inv = k / max(dot(u, u), 1e-6);
    float fw = length(vec2((u.x * dFdx(u.y) - u.y * dFdx(u.x)) * inv, (u.x * dFdy(u.y) - u.y * dFdy(u.x)) * inv));
    float d = abs(fract(f - 0.5) - 0.5) / max(fw, 1e-6);   // distance to the nearest line, in pixels
    return (1.0 - smoothstep(0.4 * uPxRatio, 1.3 * uPxRatio, d)) * (1.0 - smoothstep(0.22, 0.5, fw)) * smoothstep(0.2, 0.7, length(u));
  }

  void main() {
    float top = 1.0 - vWall;   // 1 on the surface, 0 on the side walls
    // Hypsometric tint: the colour is the height. Both ramps are light at sea level (h = 0) and
    // deepen toward the wells and the peaks, so both ends stand off the paper. The colour flips
    // in the middle of the morph rather than cross-fading through mud for its whole length.
    float u = (0.5 + 255.0 * clamp((vH + 2.0) / 3.0, 0.0, 1.0)) / 256.0;
    float cm = smoothstep(0.3, 0.7, vM);
    vec3 col = mix(texture2D(uRampA, vec2(u, 0.5)).rgb, texture2D(uRampB, vec2(u, 0.5)).rgb, cm);
    // dead code keeps its shape and loses its colour: one grey, darker with depth
    col = mix(col, mix(uSlate, uChalk, smoothstep(0.1, 0.95, u)), uDead * cm);

    // hillshade: light multiplies the paint, as on a relief map. No highlights: they crawl.
    vec3 n = normalize(vN);
    float ndl = dot(n, uLight);
    col *= mix(0.68, 1.0, smoothstep(-0.2, 0.9, ndl));
    col = mix(col, uPaper, 0.07 * smoothstep(0.85, 1.0, ndl) * top);
    if (!gl_FrontFacing && vWall < 0.5) col *= 0.74;
    // the walls are a section through the block: the same strata, a shade deeper
    col = mix(col, col * 0.86, vWall);

    // contours every quarter unit of height, a heavier one at sea level
    float gh = grad(vH);
    float crowd = 1.0 - smoothstep(0.045, 0.085, gh / uPxRatio);
    float iso = lineCov(abs(fract(vH * 4.0 - 0.5) - 0.5) * 0.25, gh, 0.55 * uPxRatio) * crowd;
    float sea = lineCov(abs(vH), gh, 0.85 * uPxRatio) * crowd;
    col = mix(col, uInk, max(iso * mix(0.2, 0.36, cm), sea * 0.5));

    // lines of constant phase: the interference pattern, strongest on the wave
    float lines = phaseLines(vPhA, 8.0) * (1.0 - vM) + phaseLines(vPhB, 8.0) * vM;
    col = mix(col, uInk, lines * mix(0.12, 0.4, cm) * uPhase * top * (1.0 - 0.55 * uCompare));

    // Compare: patches where one wave is at least four times the other, outlined. Lifted toward
    // paper where the live wave is the louder one, chalked where the dead one is.
    float ad = abs(vDiff);
    float gd = grad(ad);
    float on = uCompare * cm * top;
    col = mix(col, vDiff > 0.0 ? uPaper : uChalk, smoothstep(-0.5, 0.5, (ad - 1.386) / max(gd, 1e-6)) * 0.26 * on);
    col = mix(col, uPaper, lineCov(abs(ad - 1.386), gd, 2.2 * uPxRatio) * on);
    col = mix(col, uInk, lineCov(abs(ad - 1.386), gd, 1.0 * uPxRatio) * 0.9 * on);

    // Plan position in world units (sigma is stretched uSX times against t) and one pixel there.
    vec2 w = vST * vec2(uSX, 1.0);
    float px = max(length(vec2(grad(w.x), grad(w.y))), 1e-5);

    // Each zero of zeta is a target painted where it sits: an emerald ring around a gold centre,
    // round in the plan although sigma is stretched.
    float rz = 1e3;
    float pulse = 0.0;
    for (int i = 0; i < 16; i++) {
      if (i >= uZetaCount) break;
      float r = length(w - uZetaWells[i] * vec2(uSX, 1.0));
      if (r < rz) {
        rz = r;
        pulse = uZetaPulse[i];
      }
    }
    float zl = (1.0 - cm) * top;
    float rr = rz / (1.0 + 0.35 * pulse);
    col = mix(col, uRing, zl * 0.9 * (1.0 - smoothstep(0.1, 0.1 + 2.0 * px, abs(rr - 0.62))));
    // the synth sends a ripple out of the zero it sounds
    float ripple = 0.8 + 2.6 * (1.0 - pulse);
    col = mix(col, uRing, zl * pulse * (1.0 - smoothstep(0.06, 0.06 + px, abs(rz - ripple))));

    // The critical line: a gold core in a crimson casing. The pair carries its own contrast
    // (4.4:1), so the line reads over every colour of the ramp, and its width has a floor in
    // pixels so it never thins out with distance. On the proof wave nothing is special about
    // sigma = 1/2, so there the line is only an ink hairline that shares zeta's frame.
    float sg = max(grad(vST.x), 0.0009);
    float dpx = abs(vST.x - 0.5) / sg;
    float heat = pow(0.5 + 0.5 * sin(6.2831853 * (vST.y / 11.0 - uTime * 0.2)), 8.0);
    float core = (1.25 + 0.55 * heat) * uPxRatio;
    col = mix(col, uInk, lineCov(dpx, 1.0, core + 2.5 * uPxRatio) * 0.8 * zl);
    col = mix(col, uCrimson, lineCov(dpx, 1.0, core + 1.9 * uPxRatio) * zl);
    col = mix(col, mix(uGold, vec3(1.0, 0.96, 0.78), heat), lineCov(dpx, 1.0, core) * zl);
    col = mix(col, uInk, lineCov(dpx, 1.0, 0.6 * uPxRatio) * 0.55 * cm * top);

    // the gold centre of each zero sits on the line like a bead on a wire
    col = mix(col, uInk, zl * disc(rr, 0.3, px) * 0.85);
    col = mix(col, uCentre, zl * disc(rr, 0.25, px));

    // an ink keyline where the surface meets its walls
    float edge = min(min(vST.x, 1.0 - vST.x) * uSX, min(vST.y, uTMax - vST.y));
    col = mix(col, uInk, (1.0 - smoothstep(0.0, max(0.05, 1.4 * px), edge)) * 0.6 * top);
    gl_FragColor = vec4(dithered(fogged(col, vWorld)), 1.0);
  }
`;

export const APRON_VERT = /* glsl */ `
  attribute float aH;
  uniform float uBase;
  uniform float uScale;
  uniform float uSX;
  uniform float uTMax;
  uniform vec2 uExt;      // sigma at the outer rims
  varying float vH;
  varying vec2 vST;
  varying vec3 vWorld;
  void main() {
    vST = vec2(position.x / uSX + 0.5, -position.z);
    // the mounds sink back into the table at the rims and at both ends, so no edge is cut open
    float rim = smoothstep(uExt.x, uExt.x + 0.07, vST.x) * (1.0 - smoothstep(uExt.y - 0.07, uExt.y, vST.x))
              * smoothstep(0.0, 1.6, vST.y) * (1.0 - smoothstep(uTMax - 1.6, uTMax, vST.y));
    vH = aH * rim;
    vec4 world = modelMatrix * vec4(position.x, uBase + vH * uScale, position.z, 1.0);
    vWorld = world.xyz;
    gl_Position = projectionMatrix * viewMatrix * world;
  }
`;

// Dead code outside the strip: unpainted chalk relief with dashed slate contours.
export const APRON_FRAG = /* glsl */ `
  uniform vec3 uChalk;
  uniform vec3 uSlate;
  uniform vec3 uTable;
  uniform vec3 uShadow;
  uniform vec3 uLight;
  uniform float uSX;
  varying float vH;
  varying vec2 vST;
  varying vec3 vWorld;
  ${COMMON}
  void main() {
    if (vST.x > 0.0 && vST.x < 1.0) discard;   // the strip itself belongs to the function
    // facet normals from the surface itself: the relief reads as chipped chalk
    vec3 n = normalize(cross(dFdx(vWorld), dFdy(vWorld)));
    if (n.y < 0.0) n = -n;
    float ndl = dot(n, uLight);
    // the mounds rise out of the table: its colour at their foot, chalk above
    vec3 col = mix(uTable, uChalk, smoothstep(0.0, 0.25, vH)) * mix(0.8, 1.02, smoothstep(-0.2, 0.9, ndl));
    float f = vH * 6.0;
    float line = lineCov(abs(fract(f - 0.5) - 0.5), grad(f), 0.6);
    float q = (vST.y + vST.x * 9.0) * 1.3;
    float dash = smoothstep(0.0, grad(q), abs(fract(q) - 0.5) - 0.25);
    col = mix(col, uSlate, 0.75 * line * dash * step(0.02, vH));
    // the block's shadow falls across the mounds next to it
    float d = max(-vST.x, vST.x - 1.0) * uSX;
    col = mix(col, uShadow, 0.26 * exp(-d / 1.1) + 0.12 * exp(-d / 4.5));
    gl_FragColor = vec4(dithered(fogged(col, vWorld)), 1.0);
  }
`;

// The table the model stands on, with a soft shadow around its footprint. It fades into the
// sky with distance, so there is no horizon line.
export const GROUND_VERT = /* glsl */ `
  varying vec3 vWorld;
  void main() {
    vec4 world = modelMatrix * vec4(position, 1.0);
    vWorld = world.xyz;
    gl_Position = projectionMatrix * viewMatrix * world;
  }
`;

export const GROUND_FRAG = /* glsl */ `
  uniform vec3 uColor;
  uniform vec3 uShadow;
  uniform vec4 uFoot;   // centre x, centre z, half width, half length of the block
  varying vec3 vWorld;
  ${COMMON}
  float outside(vec2 p, vec2 b) {
    return length(max(abs(p) - b, 0.0));
  }
  void main() {
    vec2 p = vWorld.xz - uFoot.xy;
    float sh = 0.3 * exp(-outside(p, uFoot.zw) / 1.1)                                  // contact
             + 0.16 * exp(-outside(p - vec2(1.6, -1.4), uFoot.zw + 0.6) / 4.5);        // cast, away from the light
    vec3 col = mix(mix(uColor, uShadow, sh), uSky, smoothstep(70.0, 190.0, length(p)));
    gl_FragColor = vec4(dithered(col), 1.0);
  }
`;

// The dead wave in Compare: a slate grid at the dead heights, floating through the live surface.
export const GHOST_VERT = /* glsl */ `
  attribute float aH;
  attribute vec3 aB;      // the live wave, shared with the terrain
  uniform float uSY;
  uniform float uSX;
  varying float vGap;
  varying vec2 vST;
  varying vec3 vWorld;
  void main() {
    vST = vec2(position.x / uSX + 0.5, -position.z);
    vGap = aH - aB.x;
    vec4 world = modelMatrix * vec4(position.x, aH * uSY, position.z, 1.0);
    vWorld = world.xyz;
    gl_Position = projectionMatrix * viewMatrix * world;
  }
`;

export const GHOST_FRAG = /* glsl */ `
  uniform vec3 uColor;
  uniform vec3 uVeil;
  uniform float uOpacity;
  uniform float uPxRatio;
  varying float vGap;
  varying vec2 vST;
  varying vec3 vWorld;
  ${COMMON}
  void main() {
    vec2 q = vec2(vST.x * 10.0, vST.y / 1.5);
    vec2 g = vec2(grad(q.x), grad(q.y));
    vec2 c = (1.0 - smoothstep(0.4 * uPxRatio, 1.4 * uPxRatio, abs(fract(q - 0.5) - 0.5) / max(g, vec2(1e-6)))) * (1.0 - smoothstep(0.2, 0.45, g));
    // where the two waves coincide the grid would fight the surface for depth: let it fade there
    float line = max(c.x, c.y);
    float a = mix(0.3, 0.95, line) * smoothstep(0.0, 0.06, vGap) * uOpacity;
    if (a < 0.004) discard;
    gl_FragColor = vec4(fogged(mix(uVeil, uColor, line), vWorld), a);
  }
`;

// Round cased marks: beacon heads (a disc) and zero markers (a ring you can see through).
// Interiors are opaque and write depth, so draw order only matters in the one-pixel edge.
export const MARK_VERT = /* glsl */ `
  attribute float aSize;
  attribute float aPulse;
  attribute float aAlpha;
  attribute vec3 aColor;
  uniform float uPx;
  varying float vAlpha;
  varying float vSize;
  varying vec3 vColor;
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    gl_Position = projectionMatrix * mv;
    vSize = clamp(aSize * (1.0 + 0.7 * aPulse) * uPx / -mv.z, 9.0, 54.0) * step(0.02, aAlpha);
    gl_PointSize = vSize;
    vAlpha = aAlpha;
    vColor = aColor;
  }
`;

export const MARK_FRAG = /* glsl */ `
  uniform vec3 uInk;
  uniform vec3 uPaper;
  uniform float uRing;   // 0 = disc, else a ring whose hole has this radius
  varying float vAlpha;
  varying float vSize;
  varying vec3 vColor;
  ${COMMON}
  void main() {
    float r = length(gl_PointCoord * 2.0 - 1.0);
    float px = 2.0 / max(vSize, 1.0);
    float alpha = disc(r, 1.0 - 0.5 * px, px);
    vec3 col = uInk;                                   // hairline
    col = mix(col, uPaper, disc(r, 1.0 - 1.6 * px, px));   // white casing
    col = mix(col, vColor, disc(r, 0.74, px));        // body
    if (uRing > 0.0) {
      col = mix(col, uPaper, disc(r, uRing, px));
      col = mix(col, uInk, disc(r, uRing - 1.6 * px, px));
      alpha *= 1.0 - disc(r, uRing - 2.8 * px, px);    // open centre
    }
    alpha *= vAlpha;
    if (alpha < 0.02) discard;
    gl_FragColor = vec4(col, alpha);
  }
`;

// Particles riding the flow arcs: the cubic Bezier is evaluated on the GPU from attributes.
// A particle is born and dies by growing and shrinking, not by fading, so it stays opaque.
export const FLOW_VERT = /* glsl */ `
  attribute vec3 aP1;
  attribute vec3 aP2;
  attribute vec3 aP3;
  attribute vec4 aSeed;   // phase, speed, size, lag behind the head of its comet
  attribute vec3 aColor;  // the act of the chapter the flow leaves
  uniform float uTime;
  uniform float uPx;
  uniform float uFlow;
  varying float vSize;
  varying vec3 vColor;
  void main() {
    float u = fract(aSeed.x + uTime * aSeed.y - aSeed.w);
    float e = u * u * (3.0 - 2.0 * u);   // leave slowly, arrive slowly
    float a = 1.0 - e;
    vec3 p = a * a * a * position + 3.0 * a * a * e * aP1 + 3.0 * a * e * e * aP2 + e * e * e * aP3;
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    gl_Position = projectionMatrix * mv;
    float life = sin(3.14159265 * u) * uFlow * (1.0 - aSeed.w * 18.0);
    vSize = clamp(aSeed.z * uPx / -mv.z, 3.5, 11.0) * smoothstep(0.0, 0.35, life);
    gl_PointSize = vSize;
    vColor = aColor;
  }
`;

export const FLOW_FRAG = /* glsl */ `
  uniform vec3 uPaper;
  varying float vSize;
  varying vec3 vColor;
  ${COMMON}
  void main() {
    float r = length(gl_PointCoord * 2.0 - 1.0);
    float px = 2.0 / max(vSize, 1.0);
    float alpha = disc(r, 1.0 - 0.5 * px, px) * step(2.5, vSize);
    if (alpha < 0.02) discard;
    // a white rim once the dot is large enough to carry one
    float body = mix(1.0, 1.0 - 2.4 * px, smoothstep(5.0, 8.0, vSize));
    gl_FragColor = vec4(mix(uPaper, vColor, disc(r, body, px)), alpha);
  }
`;

// Arc bodies are opaque, so crossing arcs sort by depth. Direction reads in a still frame from
// the taper and the colour: thin and pale at the provider, full and saturated at the consumer.
// Each arc is drawn twice: a slightly wider white hull behind it (back faces), then the body.
export const TUBE_VERT = /* glsl */ `
  attribute float aW;      // weight of the flow, 0..1
  attribute float aR;      // radius the geometry was built with
  attribute vec3 aColor;   // the act of the chapter the flow leaves
  uniform float uPx;
  uniform float uPxRatio;
  uniform float uHull;     // extra radius in pixels (the white pass), else 0
  uniform float uFlow;
  varying float vU;
  varying vec3 vColor;
  varying vec3 vNormal;
  void main() {
    vU = uv.x;
    vColor = aColor;
    vNormal = normalMatrix * normal;
    vec3 centre = position - normal * aR;
    vec4 mv = modelViewMatrix * vec4(centre, 1.0);
    float pixel = -mv.z / uPx;                                  // world units per device pixel here
    float want = aR * mix(0.45, 1.0, vU * vU) * mix(0.55, 1.0, uFlow);
    float radius = max(want, 0.75 * uPxRatio * pixel) + uHull * uPxRatio * pixel;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(centre + normal * radius, 1.0);
  }
`;

export const TUBE_FRAG = /* glsl */ `
  uniform vec3 uPaper;
  uniform float uHull;
  uniform float uFlow;
  varying float vU;
  varying vec3 vColor;
  varying vec3 vNormal;
  void main() {
    vec3 body = mix(uPaper, vColor, mix(0.3, 0.95, vU * vU) * mix(0.6, 1.0, uFlow));
    body *= mix(0.82, 1.0, abs(normalize(vNormal).z));           // a little roundness
    gl_FragColor = vec4(mix(body, uPaper, step(0.5, uHull)), 1.0);
  }
`;
