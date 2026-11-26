// The 3D critical strip: an imperative three.js engine with a thin React wrapper, mirroring
// mapEngine.ts. It owns its canvas (safe under React StrictMode and hot reload), the renderer,
// the camera and the frame loop; React only calls idempotent setters.
//
// World: x = (sigma - 1/2) * SX, z = -t, y = height. One terrain mesh carries two surfaces per
// vertex, zeta (A) and the proof wave (B), and `uMorph` blends between them.

import {
  BackSide, BufferAttribute, BufferGeometry, ClampToEdgeWrapping, Color, CubicBezierCurve3, CylinderGeometry, DataTexture, DoubleSide,
  DynamicDrawUsage, InstancedMesh, LinearFilter, LinearMipmapLinearFilter, Matrix4, Mesh, MeshBasicMaterial, PerspectiveCamera, PlaneGeometry, Points,
  Quaternion, RGBAFormat, Scene, ShaderMaterial, TubeGeometry, UnsignedByteType, Vector2, Vector3, Vector4, WebGLRenderer,
} from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { mergeGeometries } from "three/addons/utils/BufferGeometryUtils.js";
import { easeInOutCubic } from "../camera";
import { COLORS, RAMP_WAVE, RAMP_ZETA, TIER_FILL, srgb, type RampStop } from "../theme";
import { SX, SY, criticalLine, emptySurface, heightAt, waveSurface, zetaSurface, type Surface } from "./field";
import type { Landscape, ProofZero } from "./landscape";
import { bakeRamp } from "./ramp";
import {
  CUES, SHOTS, auditTour, compileTour, emptyFrame, sampleTour, sceneAt, shotPose, type CompiledTour, type SceneState, type ShotId,
  type Vec3,
} from "./tour";
import {
  APRON_FRAG, APRON_VERT, FLOW_FRAG, FLOW_VERT, GHOST_FRAG, GHOST_VERT, GROUND_FRAG, GROUND_VERT, MARK_FRAG, MARK_VERT, TERRAIN_FRAG,
  TERRAIN_VERT, TUBE_FRAG, TUBE_VERT,
} from "./shaders";

export type Population = "live" | "dead" | "compare";
export type Preset = "overview" | "dive" | "finale";

export interface Probe {
  x: number;
  y: number;
  title: string;
  lines: string[];
}

/** The tour reports only when something changes for the controls: about ten times in forty seconds. */
export interface TourEvent {
  phase: "start" | "cue" | "end" | "cancel";
  /** false once the tour has ended, been cancelled, or is held on one frame */
  playing: boolean;
  shot: ShotId;
  cue: number;
  /** what the controls should show from now on */
  scene: SceneState;
  /** the tour's own clock, in seconds (what the narration follows) */
  t: number;
}

export interface StripEvents {
  onPick(slug: string): void;
  onHover(chapter: number | null): void;
  onProbe(probe: Probe | null): void;
  onTour(event: TourEvent): void;
  onContext(state: "lost" | "restored"): void;
  /** the viewer moved the camera by hand (touring or not): anything playing along should stop */
  onInteract?(): void;
}

interface Pose {
  target: Vector3;
  radius: number;
  azimuth: number;
  polar: number;
}

const FOV = 32;
const H_MIN = -2; // the soft clamp in field.ts keeps every height inside (H_MIN, H_MAX)
const H_MAX = 1;
const FLOOR = -5.2; // the table: just under the deepest well (H_MIN * SY)
const LIGHT = new Vector3(-0.45, 0.8, 0.4).normalize(); // front-left: the relief runs along t
const DEAD_DEEP = "#6B7480"; // dead code at its lowest; it lightens to chalk with height
const GHOST = "#4B5563"; // the dead wave's grid in Compare
const SHADOW = "#8C7B62"; // umber, not black: a shadow on warm paper
const vec = (v: [number, number, number]) => new Vector3(v[0], v[1], v[2]);
const tint = (hex: string) => vec(srgb(hex));

/** Small deterministic generator, so particle phases (and screenshots) repeat exactly. */
function mulberry(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export class StripEngine {
  private readonly renderer: WebGLRenderer;
  private readonly scene = new Scene();
  private readonly camera = new PerspectiveCamera(FOV, 1, 0.5, 600);
  private readonly controls: OrbitControls;
  private readonly canvas: HTMLCanvasElement;
  private readonly zeta: Surface;
  private readonly wave: Surface;
  private readonly ghost: Surface;
  private readonly scratch: { re: Float32Array; im: Float32Array };
  private readonly terrain: Mesh<BufferGeometry, ShaderMaterial>;
  private readonly ghostMesh: Mesh<BufferGeometry, ShaderMaterial>;
  private readonly lamps: Points<BufferGeometry, ShaderMaterial>;
  private readonly zetaMarks: Points<BufferGeometry, ShaderMaterial>;
  private readonly waveMarks: Points<BufferGeometry, ShaderMaterial>;
  private readonly flow: Points<BufferGeometry, ShaderMaterial>;
  private readonly tubes: Mesh<BufferGeometry, ShaderMaterial>[];
  private readonly lampPos: Vector3[] = [];
  private readonly disposables: { dispose(): void }[] = [];
  private skirt = new Uint32Array(0);
  private surfaceIndex = new Uint32Array(0);
  private labels: { el: HTMLElement; pos: Vector3; kind: "beacon" | "zero"; index: number }[] = [];
  private width = 2;
  private height = 2;
  private raf = 0;
  private active = false;
  private disposed = false;
  private contextLost = false;
  private last = 0;
  private clock = 0;
  private readonly freeze: number | null;
  private readonly still: boolean;

  private morph = 0;
  private morphFrom = 0;
  private morphTo = 0;
  private morphStart = -1;
  private sweep = 0;
  private bandCount: number;
  private readonly bands: Float32Array;
  private readonly bandTarget: Float32Array;
  private pop = 0;
  private popTarget = 0;
  private compare = false;
  private waveDirty = true;
  private marksDirty = true;
  private selected: number | null = null;
  private hovered: number | null = null;
  private flight: { from: Pose; to: Pose; start: number; ms: number } | null = null;
  private readonly course: CompiledTour;
  private readonly frame = emptyFrame();
  /** The tour's clock. While it is set the tour owns the camera, the morph and the population. */
  private cine: { t: number; playing: boolean; armed: boolean; cue: number } | null = null;
  private ghostLevel = 0;
  private pulses = { zeta: new Float32Array(64), wave: new Float32Array(64), lamp: new Float32Array(64) };
  private labelKey = "";
  private pointer: { x: number; y: number; at: number } | null = null;
  private frames = 0;

  constructor(
    private readonly host: HTMLElement,
    private readonly land: Landscape,
    private readonly events: StripEvents,
    opts: { freeze: number | null },
  ) {
    this.freeze = opts.freeze;
    this.still = opts.freeze != null || (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false);
    this.course = compileTour(land.tMax);
    this.canvas = document.createElement("canvas");
    this.canvas.setAttribute("aria-label", "3D critical strip: the zeta terrain and the proof wave, with one beacon per chapter");
    this.canvas.setAttribute("role", "img");
    host.prepend(this.canvas);
    this.renderer = new WebGLRenderer({ canvas: this.canvas, antialias: true, alpha: false, powerPreference: "high-performance" });
    this.renderer.setClearColor(new Color(COLORS.ground), 1);

    const n = land.nS * land.nT;
    this.zeta = zetaSurface(land);
    this.wave = emptySurface(n);
    this.ghost = emptySurface(n);
    this.scratch = { re: new Float32Array(n), im: new Float32Array(n) };
    this.bandCount = land.bands;
    this.bands = new Float32Array(land.bands).fill(1);
    this.bandTarget = new Float32Array(land.bands).fill(1);

    this.buildGround();
    this.terrain = this.buildTerrain();
    this.ghostMesh = this.buildGhost();
    this.buildApron();
    this.lamps = this.buildBeacons();
    this.zetaMarks = this.buildMarks(land.meta.zeta.zeros.length, COLORS.zeroRing, 0.5);
    this.waveMarks = this.buildMarks(64, COLORS.ink2, 0.6);
    const { tubes, flow } = this.buildArcs();
    this.tubes = tubes;
    this.flow = flow;
    this.placeZetaMarks();

    this.controls = new OrbitControls(this.camera, this.canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.minDistance = 5;
    this.controls.maxDistance = 190;
    this.controls.maxPolarAngle = 1.5;
    this.controls.autoRotateSpeed = 0.55;
    this.controls.addEventListener("start", this.onUserMove);
    this.applyPose(this.preset("overview"));

    this.canvas.addEventListener("pointermove", this.onPointerMove);
    this.canvas.addEventListener("pointerdown", this.onPointerDown);
    this.canvas.addEventListener("pointerup", this.onPointerUp);
    this.canvas.addEventListener("pointerleave", this.onPointerLeave);
    this.canvas.addEventListener("webglcontextlost", this.onLost);
    this.canvas.addEventListener("webglcontextrestored", this.onRestored);
    document.addEventListener("visibilitychange", this.onVisibility);
  }

  // --- construction ---------------------------------------------------------------------------

  private wx(sigma: number): number {
    return (sigma - 0.5) * SX;
  }

  /**
   * The block: the surface lattice, then a skirt of walls down to the table. Each border vertex
   * is repeated twice in the skirt (top and foot) with the wall's outward normal, so the walls
   * shade flat, follow the morph exactly and cannot crack away from the surface.
   */
  private buildTerrain(): Mesh<BufferGeometry, ShaderMaterial> {
    const { nS, nT, tMax } = this.land;
    const n = nS * nT;
    const border: number[] = [];
    const normals: [number, number][] = [];
    const quads: number[] = [];
    const wall = (ids: number[], nx: number, nz: number) => {
      const first = border.length;
      for (const id of ids) {
        border.push(id);
        normals.push([nx, nz]);
      }
      for (let s = first; s + 1 < border.length; s++) {
        const top = n + 2 * s;
        quads.push(top, top + 1, top + 2, top + 1, top + 3, top + 2);
      }
    };
    wall(Array.from({ length: nT }, (_, j) => j * nS), -1, 0);
    wall(Array.from({ length: nT }, (_, j) => j * nS + nS - 1), 1, 0);
    wall(Array.from({ length: nS }, (_, i) => i), 0, 1);
    wall(Array.from({ length: nS }, (_, i) => (nT - 1) * nS + i), 0, -1);
    this.skirt = new Uint32Array(border);
    const total = n + 2 * border.length;

    const pos = new Float32Array(total * 3);
    for (let j = 0; j < nT; j++) {
      for (let i = 0; i < nS; i++) {
        const k = (j * nS + i) * 3;
        pos[k] = this.wx(i / (nS - 1));
        pos[k + 2] = -(tMax * j) / (nT - 1);
      }
    }
    const aSkirt = new Float32Array(total * 3);
    border.forEach((id, s) => {
      for (const foot of [0, 1]) {
        const k = n + 2 * s + foot;
        pos[k * 3] = pos[id * 3];
        pos[k * 3 + 2] = pos[id * 3 + 2];
        aSkirt.set([foot, normals[s][0], normals[s][1]], k * 3);
      }
    });
    const surface = new Uint32Array((nS - 1) * (nT - 1) * 6);
    let q = 0;
    for (let j = 0; j < nT - 1; j++) {
      for (let i = 0; i < nS - 1; i++) {
        const a = j * nS + i;
        const b = a + 1;
        const c = a + nS;
        const d = c + 1;
        // counter-clockwise seen from above (+y); alternate the diagonal so no direction is favoured
        if ((i + j) % 2 === 0) surface.set([a, b, c, b, d, c], q);
        else surface.set([a, d, c, a, b, d], q);
        q += 6;
      }
    }
    this.surfaceIndex = surface;
    const index = new Uint32Array(surface.length + quads.length);
    index.set(surface);
    index.set(quads, surface.length);

    const geo = new BufferGeometry();
    geo.setAttribute("position", new BufferAttribute(pos, 3));
    geo.setAttribute("aSkirt", new BufferAttribute(aSkirt, 3));
    geo.setIndex(new BufferAttribute(index, 1));
    const aA = new Float32Array(total * 3);
    const aPhA = new Float32Array(total * 2);
    for (let k = 0; k < n; k++) {
      aA[k * 3] = this.zeta.h[k];
      aA[k * 3 + 1] = this.zeta.gx[k];
      aA[k * 3 + 2] = this.zeta.gz[k];
      aPhA[k * 2] = this.zeta.cos[k];
      aPhA[k * 2 + 1] = this.zeta.sin[k];
    }
    this.copyToSkirt(aA, 3);
    geo.setAttribute("aA", new BufferAttribute(aA, 3));
    geo.setAttribute("aPhA", new BufferAttribute(aPhA, 2));
    for (const [name, size] of [["aB", 3], ["aPhB", 2], ["aDiff", 1]] as const) {
      const attr = new BufferAttribute(new Float32Array(total * size), size);
      attr.setUsage(DynamicDrawUsage);
      geo.setAttribute(name, attr);
    }
    const mat = new ShaderMaterial({
      vertexShader: TERRAIN_VERT,
      fragmentShader: TERRAIN_FRAG,
      uniforms: {
        uMorph: { value: 0 }, uSweep: { value: 0 }, uSY: { value: SY }, uSX: { value: SX }, uTMax: { value: tMax },
        uFloor: { value: FLOOR }, uRampA: { value: this.rampTexture(RAMP_ZETA) }, uRampB: { value: this.rampTexture(RAMP_WAVE) },
        uInk: { value: tint(COLORS.ink) }, uPaper: { value: tint(COLORS.raised) }, uSky: { value: tint(COLORS.ground) },
        uGold: { value: tint(COLORS.gold) }, uCrimson: { value: tint(COLORS.crimson) },
        uRing: { value: tint(COLORS.zeroRing) }, uCentre: { value: tint(COLORS.zeroCentre) },
        uChalk: { value: tint(COLORS.chalk) }, uSlate: { value: tint(DEAD_DEEP) },
        uLight: { value: LIGHT.clone() }, uPhase: { value: 1 }, uCompare: { value: 0 }, uDead: { value: 0 },
        uTime: { value: 0 }, uPxRatio: { value: 1 },
        uZetaWells: { value: Array.from({ length: 16 }, (_, i) => new Vector2(0.5, this.land.meta.zeta.zeros[i]?.t ?? -99)) },
        uZetaPulse: { value: new Float32Array(16) },
        uZetaCount: { value: Math.min(16, this.land.meta.zeta.zeros.length) },
      },
      side: DoubleSide,
    });
    const mesh = new Mesh(geo, mat);
    mesh.frustumCulled = false; // heights live in the shader
    this.scene.add(mesh);
    this.disposables.push(geo, mat);
    return mesh;
  }

  /** Repeat each border vertex's values on its two skirt vertices. */
  private copyToSkirt(values: Float32Array, size: number): void {
    const n = this.land.nS * this.land.nT;
    for (let s = 0; s < this.skirt.length; s++) {
      const from = this.skirt[s] * size;
      for (let c = 0; c < size; c++) {
        values[(n + 2 * s) * size + c] = values[from + c];
        values[(n + 2 * s + 1) * size + c] = values[from + c];
      }
    }
  }

  /**
   * One row of texels per ramp. The bytes are display sRGB and the shader writes them unchanged
   * (no colour space on the texture). Mipmaps prefilter the ramp where a far, steep wall crosses
   * several stops inside one pixel, which would otherwise shimmer.
   */
  private rampTexture(stops: RampStop[]): DataTexture {
    const tex = new DataTexture(bakeRamp(stops, H_MIN, H_MAX), 256, 1, RGBAFormat, UnsignedByteType);
    tex.magFilter = LinearFilter;
    tex.minFilter = LinearMipmapLinearFilter;
    tex.generateMipmaps = true;
    tex.wrapS = tex.wrapT = ClampToEdgeWrapping;
    tex.needsUpdate = true;
    this.disposables.push(tex);
    return tex;
  }

  /** The table under the block, with a contact shadow around its footprint. */
  private buildGround(): void {
    const { tMax } = this.land;
    const plane = new PlaneGeometry(900, 900);
    const mat = new ShaderMaterial({
      vertexShader: GROUND_VERT, fragmentShader: GROUND_FRAG,
      uniforms: {
        uColor: { value: tint(COLORS.well) }, uShadow: { value: tint(SHADOW) }, uSky: { value: tint(COLORS.ground) },
        uFoot: { value: new Vector4(0, -tMax / 2, SX / 2, tMax / 2) },
      },
    });
    const mesh = new Mesh(plane, mat);
    mesh.rotation.x = -Math.PI / 2;
    mesh.position.set(0, FLOOR - 0.02, -tMax / 2);
    mesh.frustumCulled = false;
    this.scene.add(mesh);
    this.disposables.push(plane, mat);
  }

  /** The dead wave as a slate grid on the surface lattice, shown only in Compare. */
  private buildGhost(): Mesh<BufferGeometry, ShaderMaterial> {
    const src = this.terrain.geometry;
    const geo = new BufferGeometry();
    geo.setAttribute("position", src.getAttribute("position"));
    geo.setAttribute("aB", src.getAttribute("aB"));
    geo.setIndex(new BufferAttribute(this.surfaceIndex, 1));
    const aH = new BufferAttribute(new Float32Array(src.getAttribute("position").count), 1);
    aH.setUsage(DynamicDrawUsage);
    geo.setAttribute("aH", aH);
    const mat = new ShaderMaterial({
      vertexShader: GHOST_VERT, fragmentShader: GHOST_FRAG, transparent: true, depthWrite: false, side: DoubleSide,
      polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -2,
      uniforms: {
        uSY: { value: SY }, uSX: { value: SX }, uColor: { value: tint(GHOST) }, uVeil: { value: tint(COLORS.chalk) },
        uSky: { value: tint(COLORS.ground) },
        uOpacity: { value: 0 }, uPxRatio: { value: 1 },
      },
    });
    const mesh = new Mesh(geo, mat);
    mesh.frustumCulled = false;
    mesh.visible = false;
    mesh.renderOrder = 2;
    this.scene.add(mesh);
    this.disposables.push(geo, mat);
    return mesh;
  }

  /** Dead declarations as unpainted chalk mounds on the table, either side of the block. */
  private buildApron(): void {
    const { nT, tMax, meta } = this.land;
    const ext = meta.grid.sigma_ext;
    const nE = ext.n;
    const pos = new Float32Array(nE * nT * 3);
    for (let j = 0; j < nT; j++) {
      for (let i = 0; i < nE; i++) {
        const k = (j * nE + i) * 3;
        pos[k] = this.wx(ext.min + ((ext.max - ext.min) * i) / (nE - 1));
        pos[k + 2] = -(tMax * j) / (nT - 1);
      }
    }
    const index = new Uint32Array((nE - 1) * (nT - 1) * 6);
    let q = 0;
    for (let j = 0; j < nT - 1; j++) {
      for (let i = 0; i < nE - 1; i++) {
        const a = j * nE + i;
        index.set([a, a + 1, a + nE, a + 1, a + nE + 1, a + nE], q);
        q += 6;
      }
    }
    const geo = new BufferGeometry();
    geo.setAttribute("position", new BufferAttribute(pos, 3));
    geo.setAttribute("aH", new BufferAttribute(this.land.apron.slice(), 1));
    geo.setIndex(new BufferAttribute(index, 1));
    const mat = new ShaderMaterial({
      vertexShader: APRON_VERT,
      fragmentShader: APRON_FRAG,
      uniforms: {
        uBase: { value: FLOOR + 0.03 }, uScale: { value: 2.7 }, uSX: { value: SX }, uTMax: { value: tMax },
        uExt: { value: new Vector2(ext.min, ext.max) }, uChalk: { value: tint(COLORS.chalk) }, uSlate: { value: tint(COLORS.slate) },
        uTable: { value: tint(COLORS.well) }, uShadow: { value: tint(SHADOW) }, uSky: { value: tint(COLORS.ground) },
        uLight: { value: LIGHT.clone() },
      },
      side: DoubleSide,
    });
    const mesh = new Mesh(geo, mat);
    mesh.frustumCulled = false;
    this.scene.add(mesh);
    this.disposables.push(geo, mat);
  }

  private markMaterial(ring: number): ShaderMaterial {
    const mat = new ShaderMaterial({
      vertexShader: MARK_VERT, fragmentShader: MARK_FRAG, transparent: true,
      uniforms: { uInk: { value: tint(COLORS.ink) }, uPaper: { value: tint(COLORS.raised) }, uPx: { value: 1 }, uRing: { value: ring } },
    });
    this.disposables.push(mat);
    return mat;
  }

  private pointCloud(count: number, mat: ShaderMaterial, order: number): Points<BufferGeometry, ShaderMaterial> {
    const geo = new BufferGeometry();
    for (const [name, size] of [["position", 3], ["aSize", 1], ["aPulse", 1], ["aAlpha", 1], ["aColor", 3]] as const) {
      const attr = new BufferAttribute(new Float32Array(count * size), size);
      attr.setUsage(DynamicDrawUsage);
      geo.setAttribute(name, attr);
    }
    const points = new Points(geo, mat);
    points.frustumCulled = false;
    points.renderOrder = order;
    this.scene.add(points);
    this.disposables.push(geo);
    return points;
  }

  /** One column and one head per chapter, in its act's colour. Positions are fixed, so nothing moves in the morph. */
  private buildBeacons(): Points<BufferGeometry, ShaderMaterial> {
    const chapters = this.land.meta.chapters;
    const maxLive = Math.max(...chapters.map((c) => c.live), 1);
    const column = new CylinderGeometry(1, 1, 1, 12, 1, true);
    // opaque columns; a slightly wider white hull drawn inside-out behind each gives it a casing
    const body = new MeshBasicMaterial({ side: DoubleSide });
    const hull = new MeshBasicMaterial({ color: new Color(COLORS.raised), side: BackSide });
    const beams = new InstancedMesh(column, body, chapters.length);
    const hulls = new InstancedMesh(column, hull, chapters.length);
    const m = new Matrix4();
    const lamps = this.pointCloud(chapters.length, this.markMaterial(0), 5);
    const pos = lamps.geometry.getAttribute("position") as BufferAttribute;
    const size = lamps.geometry.getAttribute("aSize") as BufferAttribute;
    const alpha = lamps.geometry.getAttribute("aAlpha") as BufferAttribute;
    const color = lamps.geometry.getAttribute("aColor") as BufferAttribute;
    chapters.forEach((c, i) => {
      const share = Math.sqrt(c.live / maxLive);
      const top = c.tier === "main" ? 7.6 : 3.3 + 2.9 * share;
      // the column rises out of the zeta surface where the chapter stands
      const bottom = Math.min(heightAt(this.land, this.zeta, c.sigma, c.t) * SY, 0) - 0.4;
      const r = c.tier === "main" ? 0.15 : 0.06 + 0.06 * share;
      const x = this.wx(c.sigma);
      const z = -c.t;
      m.compose(new Vector3(x, (top + bottom) / 2, z), new Quaternion(), new Vector3(r, top - bottom, r));
      beams.setMatrixAt(i, m);
      beams.setColorAt(i, new Color(TIER_FILL[c.tier]));
      m.compose(new Vector3(x, (top + bottom) / 2, z), new Quaternion(), new Vector3(r + 0.05, top - bottom, r + 0.05));
      hulls.setMatrixAt(i, m);
      this.lampPos.push(new Vector3(x, top, z));
      pos.setXYZ(i, x, top, z);
      size.setX(i, c.tier === "main" ? 1.5 : 0.75 + 0.5 * share);
      alpha.setX(i, 1);
      color.setXYZ(i, ...srgb(TIER_FILL[c.tier]));
    });
    for (const mesh of [hulls, beams]) {
      mesh.instanceMatrix.needsUpdate = true;
      mesh.frustumCulled = false;
      this.scene.add(mesh);
    }
    if (beams.instanceColor) beams.instanceColor.needsUpdate = true;
    this.disposables.push(column, body, hull);
    return lamps;
  }

  /** Ring markers over zeros; `hole` is the radius of the open centre (a wider hole is a lighter ring). */
  private buildMarks(count: number, hex: string, hole: number): Points<BufferGeometry, ShaderMaterial> {
    const marks = this.pointCloud(count, this.markMaterial(hole), 4);
    const color = marks.geometry.getAttribute("aColor") as BufferAttribute;
    for (let i = 0; i < count; i++) color.setXYZ(i, ...srgb(hex));
    return marks;
  }

  /**
   * Net flows as opaque arcs with particles riding them, in the act colour of the chapter the
   * flow leaves. An arc is thin and pale at its provider and full at its consumer.
   */
  private buildArcs(): { tubes: Mesh<BufferGeometry, ShaderMaterial>[]; flow: Points<BufferGeometry, ShaderMaterial> } {
    const chapters = this.land.meta.chapters;
    const at = new Map(chapters.map((c, i) => [c.id, i]));
    const arcs = this.land.meta.arcs.filter((a) => at.has(a.from) && at.has(a.to));
    const top = Math.log1p(Math.max(...arcs.map((a) => a.net), 1));
    const parts: BufferGeometry[] = [];
    const p0: number[] = [];
    const p1: number[] = [];
    const p2: number[] = [];
    const p3: number[] = [];
    const seed: number[] = [];
    const tone: number[] = [];
    const rand = mulberry(20261125);
    for (const arc of arcs) {
      const a = this.lampPos[at.get(arc.from) as number];
      const b = this.lampPos[at.get(arc.to) as number];
      const rgb = srgb(TIER_FILL[chapters[at.get(arc.from) as number].tier]);
      const w = arc.final ? 1 : Math.log1p(arc.net) / top;
      const lift = 1.6 + 0.13 * a.distanceTo(b);
      const c1 = a.clone().lerp(b, 0.25).add(new Vector3(0, lift, 0));
      const c2 = a.clone().lerp(b, 0.75).add(new Vector3(0, lift, 0));
      const radius = 0.035 + 0.065 * w;
      const tube = new TubeGeometry(new CubicBezierCurve3(a, c1, c2, b), 28, radius, 6, false);
      const count = tube.getAttribute("position").count;
      tube.setAttribute("aW", new BufferAttribute(new Float32Array(count).fill(w), 1));
      tube.setAttribute("aR", new BufferAttribute(new Float32Array(count).fill(radius), 1));
      const colors = new Float32Array(count * 3);
      for (let k = 0; k < count; k++) colors.set(rgb, k * 3);
      tube.setAttribute("aColor", new BufferAttribute(colors, 3));
      parts.push(tube);
      const comets = 2 + Math.round(5 * w);
      for (let k = 0; k < comets; k++) {
        const phase = rand();
        const speed = 0.045 + 0.05 * rand();
        const size = 0.34 + 0.26 * w;
        for (let trail = 0; trail < 3; trail++) {
          p0.push(a.x, a.y, a.z);
          p1.push(c1.x, c1.y, c1.z);
          p2.push(c2.x, c2.y, c2.z);
          p3.push(b.x, b.y, b.z);
          seed.push(phase, speed, size * (1 - 0.22 * trail), trail * 0.014);
          tone.push(...rgb);
        }
      }
    }
    const merged = parts.length ? mergeGeometries(parts) : new BufferGeometry();
    parts.forEach((g) => g.dispose());
    const tubes = [0.9, 0].map((hullPx) => {
      const mat = new ShaderMaterial({
        vertexShader: TUBE_VERT, fragmentShader: TUBE_FRAG, side: hullPx ? BackSide : DoubleSide,
        uniforms: { uPaper: { value: tint(COLORS.raised) }, uPx: { value: 1 }, uPxRatio: { value: 1 }, uHull: { value: hullPx }, uFlow: { value: 1 } },
      });
      const mesh = new Mesh(merged, mat);
      mesh.frustumCulled = false;
      this.scene.add(mesh);
      this.disposables.push(mat);
      return mesh;
    });
    const geo = new BufferGeometry();
    geo.setAttribute("position", new BufferAttribute(new Float32Array(p0), 3));
    geo.setAttribute("aP1", new BufferAttribute(new Float32Array(p1), 3));
    geo.setAttribute("aP2", new BufferAttribute(new Float32Array(p2), 3));
    geo.setAttribute("aP3", new BufferAttribute(new Float32Array(p3), 3));
    geo.setAttribute("aSeed", new BufferAttribute(new Float32Array(seed), 4));
    geo.setAttribute("aColor", new BufferAttribute(new Float32Array(tone), 3));
    const flowMat = new ShaderMaterial({
      vertexShader: FLOW_VERT, fragmentShader: FLOW_FRAG, transparent: true,
      uniforms: { uPaper: { value: tint(COLORS.raised) }, uTime: { value: 0 }, uPx: { value: 1 }, uFlow: { value: 1 } },
    });
    const flow = new Points(geo, flowMat);
    flow.frustumCulled = false;
    flow.renderOrder = 3;
    this.scene.add(flow);
    this.disposables.push(merged, geo, flowMat);
    return { tubes, flow };
  }

  private placeZetaMarks(): void {
    const zeros = this.land.meta.zeta.zeros;
    const pos = this.zetaMarks.geometry.getAttribute("position") as BufferAttribute;
    const size = this.zetaMarks.geometry.getAttribute("aSize") as BufferAttribute;
    zeros.forEach((z, i) => {
      pos.setXYZ(i, 0, 0.45, -z.t); // a ring floating over the well, visible from any angle
      size.setX(i, 1.9);
    });
    pos.needsUpdate = true;
    size.needsUpdate = true;
  }

  // --- the wave ---------------------------------------------------------------------------------

  private currentZeros(): ProofZero[] {
    const pop = this.popTarget >= 0.5 ? this.land.meta.proof.dead : this.land.meta.proof.live;
    return pop.zeros_by_band[Math.max(this.bandCount, 1) - 1] ?? [];
  }

  private updateWave(): void {
    const { land } = this;
    const n = land.nS * land.nT;
    waveSurface(land, { bands: this.bands, pop: this.pop }, this.wave, this.scratch);
    const geo = this.terrain.geometry;
    const aB = geo.getAttribute("aB") as BufferAttribute;
    const aPhB = geo.getAttribute("aPhB") as BufferAttribute;
    const aDiff = geo.getAttribute("aDiff") as BufferAttribute;
    const b = aB.array as Float32Array;
    const ph = aPhB.array as Float32Array;
    for (let k = 0; k < n; k++) {
      b[k * 3] = this.wave.h[k];
      b[k * 3 + 1] = this.wave.gx[k];
      b[k * 3 + 2] = this.wave.gz[k];
      ph[k * 2] = this.wave.cos[k];
      ph[k * 2 + 1] = this.wave.sin[k];
    }
    this.copyToSkirt(b, 3);
    aB.needsUpdate = true;
    aPhB.needsUpdate = true;
    if (this.compare) {
      waveSurface(land, { bands: this.bands, pop: 1 }, this.ghost, this.scratch);
      const d = aDiff.array as Float32Array;
      for (let k = 0; k < n; k++) d[k] = this.wave.log[k] - this.ghost.log[k];
      aDiff.needsUpdate = true;
      const aH = this.ghostMesh.geometry.getAttribute("aH") as BufferAttribute;
      (aH.array as Float32Array).set(this.ghost.h);
      aH.needsUpdate = true;
    }
    this.waveDirty = false;
  }

  private placeWaveMarks(): void {
    const zeros = this.currentZeros();
    const pos = this.waveMarks.geometry.getAttribute("position") as BufferAttribute;
    const size = this.waveMarks.geometry.getAttribute("aSize") as BufferAttribute;
    const count = Math.min(zeros.length, pos.count);
    for (let i = 0; i < pos.count; i++) {
      if (i < count) {
        const z = zeros[i];
        pos.setXYZ(i, this.wx(z.sigma), 0.45, -z.t);
        size.setX(i, 1.3);
      } else size.setX(i, 0);
    }
    pos.needsUpdate = true;
    size.needsUpdate = true;
    this.waveMarks.geometry.setDrawRange(0, count);
    this.marksDirty = false;
  }

  // --- public setters ---------------------------------------------------------------------------

  setActive(on: boolean): void {
    this.active = on;
    if (on) this.kick();
  }

  /**
   * `crisp` renders at the display's own pixel ratio (whole pixels, so ink lines stay sharp)
   * instead of a ratio fitted to a pixel budget: for recording, where sharpness matters more.
   */
  resize(width: number, height: number, dpr: number, crisp = false): void {
    if (width < 2 || height < 2) return;
    this.width = width;
    this.height = height;
    const ratio = crisp ? Math.min(Math.max(1, Math.round(dpr)), 2) : Math.min(dpr, 2, Math.sqrt(2_600_000 / (width * height)));
    this.renderer.setPixelRatio(ratio);
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    // the console covers the bottom of the stage: frame the scene a little above the centre
    this.camera.setViewOffset(width, height, 0, Math.round(height * 0.07), width, height);
    this.camera.updateProjectionMatrix();
    const px = (height * ratio) / (2 * Math.tan((FOV * Math.PI) / 360));
    for (const p of [this.lamps, this.zetaMarks, this.waveMarks, this.flow, ...this.tubes]) p.material.uniforms.uPx.value = px;
    for (const p of [this.terrain, this.ghostMesh, ...this.tubes]) p.material.uniforms.uPxRatio.value = ratio;
    this.labelKey = "";
    this.kick();
  }

  /** 0 = the zeta strip, 1 = the proof wave. Animated changes run up the strip as a front. */
  setMorph(target: number, animate: boolean): void {
    if (this.cine) return; // the tour owns the scene; the controls catch up when it ends
    const to = Math.min(1, Math.max(0, target));
    if (!animate || this.still) {
      this.morph = this.morphTo = to;
      this.morphStart = -1;
      this.sweep = 0;
    } else if (to !== this.morphTo || this.morphStart < 0) {
      this.morphFrom = this.morph;
      this.morphTo = to;
      this.morphStart = this.clock;
      this.sweep = 0.35;
    }
    this.kick();
  }

  /** Harmonics 1..count are summed (low bands are the foundations, high bands the chapters near Main). */
  setBandCount(count: number): void {
    if (this.cine) return;
    this.bandCount = Math.min(this.land.bands, Math.max(1, Math.round(count)));
    for (let b = 0; b < this.land.bands; b++) this.bandTarget[b] = b < this.bandCount ? 1 : 0;
    if (this.still) this.bands.set(this.bandTarget);
    this.waveDirty = this.marksDirty = true;
    this.kick();
  }

  setPopulation(pop: Population): void {
    if (this.cine) return;
    this.compare = pop === "compare";
    this.popTarget = pop === "dead" ? 1 : 0;
    if (this.still) this.pop = this.popTarget;
    this.ghostLevel = this.compare ? 1 : 0;
    this.ghostMesh.visible = this.compare;
    this.waveDirty = this.marksDirty = true;
    this.kick();
  }

  setSelected(slug: string | null): void {
    const i = this.land.meta.chapters.findIndex((c) => c.slug === slug);
    this.selected = i >= 0 ? i : null;
    this.labelKey = "";
    this.kick();
  }

  setOrbit(on: boolean): void {
    this.controls.autoRotate = on && !this.still && !this.cine;
    this.kick();
  }

  /** React renders the label nodes; the engine only moves them. */
  attachLabels(root: HTMLElement): void {
    const zeros = this.land.meta.zeta.zeros;
    this.labels = [];
    root.querySelectorAll<HTMLElement>("[data-beacon]").forEach((el) => {
      const index = Number(el.dataset.beacon);
      if (this.lampPos[index]) this.labels.push({ el, kind: "beacon", index, pos: this.lampPos[index] });
    });
    root.querySelectorAll<HTMLElement>("[data-zero]").forEach((el) => {
      const index = Number(el.dataset.zero);
      if (zeros[index]) this.labels.push({ el, kind: "zero", index, pos: new Vector3(this.wx(0.5), 0.9, -zeros[index].t) });
    });
    this.labelKey = "";
    this.kick();
  }

  pulse(kind: "zeta" | "wave", index: number): void {
    if (index >= 0 && index < 64) this.pulses[kind][index] = 1;
    this.kick();
  }

  /** R on the critical line for the bands and population now shown (the synth plays its peaks). */
  profile(): Float32Array {
    if (this.waveDirty) this.updateWave();
    return criticalLine(this.land, this.wave);
  }

  zeros(): ProofZero[] {
    return this.currentZeros();
  }

  // --- camera -----------------------------------------------------------------------------------

  private preset(name: Preset): Pose {
    const t = this.land.tMax;
    if (name === "dive") return { target: new Vector3(0, -1.5, -0.5 * t), radius: 0.52 * t, azimuth: 0, polar: 1.3 };
    if (name === "finale") return { target: new Vector3(0, 0.4, -0.88 * t), radius: 0.5 * t, azimuth: 0.5, polar: 1.0 };
    return { target: new Vector3(1, -1, -0.54 * t), radius: 1.72 * t, azimuth: -0.3, polar: 0.82 };
  }

  private chapterPose(index: number): Pose {
    const c = this.land.meta.chapters[index];
    // Main is the tour's last frame: opening its chapter when the tour ends does not move the camera
    if (c.tier === "main") return this.shotPoseOf("summit");
    const lamp = this.lampPos[index];
    return { target: new Vector3(lamp.x * 0.6, 0.2, lamp.z), radius: 27, azimuth: c.side < 0 ? -0.5 : c.side > 0 ? 0.5 : 0.3, polar: 0.98 };
  }

  private poseOf(eye: Vec3, target: Vec3): Pose {
    const d = new Vector3(eye[0] - target[0], eye[1] - target[1], eye[2] - target[2]);
    const radius = d.length();
    return { target: new Vector3(target[0], target[1], target[2]), radius, azimuth: Math.atan2(d.x, d.z),
             polar: Math.acos(Math.min(1, Math.max(-1, d.y / radius))) };
  }

  private shotPoseOf(id: ShotId): Pose {
    const p = shotPose(this.course, id);
    return this.poseOf(p.eye, p.target);
  }

  private currentPose(): Pose {
    const d = this.camera.position.clone().sub(this.controls.target);
    const radius = d.length();
    return { target: this.controls.target.clone(), radius, azimuth: Math.atan2(d.x, d.z), polar: Math.acos(Math.min(1, Math.max(-1, d.y / radius))) };
  }

  private applyPose(p: Pose): void {
    const s = Math.sin(p.polar);
    this.camera.position.set(p.target.x + p.radius * s * Math.sin(p.azimuth), p.target.y + p.radius * Math.cos(p.polar),
                             p.target.z + p.radius * s * Math.cos(p.azimuth));
    if (this.controls) this.controls.target.copy(p.target);
    this.camera.lookAt(p.target);
  }

  private fly(to: Pose, instant = false): void {
    if (instant || this.still) {
      this.flight = null;
      this.applyPose(to);
    } else {
      // drop leftover inertia so the flight starts from rest
      this.controls.enableDamping = false;
      this.controls.update();
      this.controls.enableDamping = true;
      const from = this.currentPose();
      const d = from.target.distanceTo(to.target) + Math.abs(from.radius - to.radius);
      const turn = Math.abs(from.azimuth - to.azimuth) + Math.abs(from.polar - to.polar);
      // already there: no flight, so nothing waits on one
      this.flight = d < 0.02 && turn < 0.002 ? null : { from, to, start: this.clock, ms: Math.min(2200, Math.max(1200, 700 + d * 22)) };
    }
    this.labelKey = "";
    this.kick();
  }

  flyToPreset(name: Preset, instant = false): void {
    this.stopTour();
    this.fly(this.preset(name), instant);
  }

  flyToChapter(slug: string, instant = false): void {
    const i = this.land.meta.chapters.findIndex((c) => c.slug === slug);
    if (i < 0) return;
    this.stopTour();
    this.fly(this.chapterPose(i), instant);
  }

  flyToPoint(sigma: number, t: number): void {
    this.stopTour();
    this.fly({ target: new Vector3(this.wx(sigma), -0.6, -t), radius: 15, azimuth: 0.35, polar: 0.95 });
  }

  /** Go to a shot's key frame on its own. The scene (wave, population) is the caller's to set. */
  flyToShot(id: ShotId, instant = false): void {
    this.stopTour();
    this.fly(this.shotPoseOf(id), instant);
  }

  // --- the visual tour --------------------------------------------------------------------------

  /**
   * Play the tour: one continuous move through the five shots, the same on every take. A take
   * from the start first returns to the opening frame (the overview of the zeta strip) and only
   * then starts the clock; on a freshly loaded view that takes no time at all.
   */
  playTour(from = 0): void {
    this.cine = null;
    this.flight = null;
    this.controls.autoRotate = false;
    const start = Math.min(this.course.duration, Math.max(0, from));
    if (start === 0 && !this.still) {
      this.fly(this.preset("overview"));
      this.setMorph(0, true);
      this.setPopulation("live");
      this.setBandCount(this.land.bands);
    }
    this.cine = { t: start, playing: true, armed: false, cue: -1 };
    this.report("start");
    this.kick();
  }

  /** Show the tour's exact frame at `seconds` and hold it: for deep links and screenshots. */
  seekTour(seconds: number): void {
    this.flight = null;
    this.cine = { t: Math.min(this.course.duration, Math.max(0, seconds)), playing: false, armed: true, cue: -1 };
    this.applyTour(this.cine.t);
    this.kick();
  }

  /** Leave the tour where it is. The normal animators finish whatever the scene was doing. */
  stopTour(): void {
    const c = this.cine;
    if (!c) return;
    const scene = sceneAt(c.t);
    this.cine = null;
    this.sweep = 0;
    if (this.morph !== (scene.wave ? 1 : 0)) this.setMorph(scene.wave ? 1 : 0, true);
    this.setPopulation(scene.population);
    this.events.onTour({ phase: "cancel", playing: false, shot: SHOTS[this.frame.shot].id, cue: this.frame.cue, scene, t: c.t });
    this.kick();
  }

  private report(phase: TourEvent["phase"]): void {
    const c = this.cine;
    if (!c) return;
    const cue = Math.max(0, c.cue);
    this.events.onTour({ phase, playing: c.playing && phase !== "end", shot: CUES[cue].shot, cue, scene: sceneAt(c.t), t: c.t });
  }

  /** Put the camera and the scene on the tour's frame at time t. */
  private applyTour(t: number): void {
    const c = this.cine;
    if (!c) return;
    // without motion the tour is a slideshow: each caption's frame at rest, cut to on the same clock
    const f = sampleTour(this.course, this.still && c.playing ? CUES[this.cueIndex(t)].still : t, this.frame);
    this.camera.position.set(f.eye[0], f.eye[1], f.eye[2]);
    this.controls.target.set(f.target[0], f.target[1], f.target[2]);
    this.camera.lookAt(this.controls.target);
    this.morph = this.morphTo = f.morph;
    this.morphStart = -1;
    this.sweep = f.sweeping ? 0.35 : 0;
    const comparing = f.ghost > 0;
    if (f.pop !== this.pop || comparing !== this.compare) {
      this.pop = f.pop;
      this.popTarget = f.pop >= 0.5 ? 1 : 0;
      this.compare = comparing;
      this.ghostMesh.visible = comparing;
      this.waveDirty = this.marksDirty = true;
    }
    this.ghostLevel = f.ghost;
    if (f.cue !== c.cue) {
      c.cue = f.cue;
      this.report("cue");
    }
  }

  private cueIndex(t: number): number {
    let cue = 0;
    for (let i = 0; i < CUES.length; i++) if (t >= CUES[i].from) cue = i;
    return cue;
  }

  private finishTour(): void {
    const c = this.cine;
    if (!c) return;
    this.report("end");
    this.cine = null;
    this.sweep = 0;
    const main = this.land.meta.chapters.find((ch) => ch.tier === "main");
    if (main) this.events.onPick(main.slug);
  }

  // --- input ------------------------------------------------------------------------------------

  private onUserMove = (): void => {
    this.flight = null;
    this.stopTour();
    this.events.onInteract?.();
  };

  private project(p: Vector3): [number, number, boolean] {
    const v = p.clone().project(this.camera);
    return [(v.x * 0.5 + 0.5) * this.width, (-v.y * 0.5 + 0.5) * this.height, v.z > -1 && v.z < 1];
  }

  private nearest(points: Vector3[], x: number, y: number, reach: number): number {
    let best = -1;
    let bestD = reach;
    points.forEach((p, i) => {
      const [sx, sy, ok] = this.project(p);
      const d = Math.hypot(sx - x, sy - y);
      if (ok && d < bestD) {
        best = i;
        bestD = d;
      }
    });
    return best;
  }

  private onPointerMove = (e: PointerEvent): void => {
    const box = this.canvas.getBoundingClientRect();
    const x = e.clientX - box.left;
    const y = e.clientY - box.top;
    if (e.buttons || this.cine?.playing) {
      this.events.onProbe(null);
      return;
    }
    const lamp = this.nearest(this.lampPos, x, y, 22);
    if (lamp !== this.hovered) {
      this.hovered = lamp >= 0 ? lamp : null;
      this.events.onHover(this.hovered);
      this.labelKey = "";
    }
    this.canvas.style.cursor = lamp >= 0 ? "pointer" : "";
    let probe: Probe | null = null;
    if (lamp < 0) {
      const zetaZeros = this.land.meta.zeta.zeros;
      const proofZeros = this.currentZeros();
      const zi = this.morph < 0.5 ? this.nearest(zetaZeros.map((z) => new Vector3(0, 0, -z.t)), x, y, 16) : -1;
      const wi = this.morph >= 0.5 ? this.nearest(proofZeros.map((z) => new Vector3(this.wx(z.sigma), 0, -z.t)), x, y, 16) : -1;
      if (zi >= 0) {
        probe = { x, y, title: `Zero ${zi + 1} of ζ`, lines: [`t = ${zetaZeros[zi].t.toFixed(6)}`, "on the line σ = 1/2, as every one of them is"] };
      } else if (wi >= 0) {
        const z = proofZeros[wi];
        const lines = [`σ = ${z.sigma.toFixed(3)}, t = ${z.t.toFixed(3)}`];
        const groups = z.by_chapter?.groups ?? [];
        const a = groups.find((g) => g.side > 0);
        const b = groups.find((g) => g.side < 0);
        const name = (g: number) => this.land.meta.chapters[g]?.title.split(" · ").pop() ?? "modules outside every chapter";
        if (a && b && z.by_chapter) {
          lines.push(`Largest opposing parts: ${name(a.group)} against ${name(b.group)}`);
          lines.push(`${Math.round(100 * z.by_chapter.within)}% of the amplitude is lost inside chapters, ${Math.round(100 * z.by_chapter.between)}% between them`);
        }
        lines.push("A landmark of this numbering, not a bottleneck");
        probe = { x, y, title: "A zero of the proof wave", lines };
      }
    }
    this.events.onProbe(probe);
  };

  private onPointerDown = (e: PointerEvent): void => {
    this.pointer = { x: e.clientX, y: e.clientY, at: performance.now() };
  };

  private onPointerUp = (e: PointerEvent): void => {
    const p = this.pointer;
    this.pointer = null;
    if (!p || Math.hypot(e.clientX - p.x, e.clientY - p.y) > 5 || performance.now() - p.at > 500) return; // an orbit drag
    const box = this.canvas.getBoundingClientRect();
    const lamp = this.nearest(this.lampPos, e.clientX - box.left, e.clientY - box.top, 22);
    if (lamp >= 0) this.events.onPick(this.land.meta.chapters[lamp].slug);
  };

  private onPointerLeave = (): void => {
    if (this.hovered != null) {
      this.hovered = null;
      this.events.onHover(null);
      this.labelKey = "";
    }
    this.events.onProbe(null);
  };

  private onLost = (e: Event): void => {
    e.preventDefault();
    this.contextLost = true;
    this.events.onContext("lost");
  };

  private onRestored = (): void => {
    this.contextLost = false;
    this.events.onContext("restored");
    this.kick();
  };

  private onVisibility = (): void => {
    if (!document.hidden) this.kick();
  };

  // --- frame loop -------------------------------------------------------------------------------

  private kick(): void {
    if (!this.raf && this.active && !this.disposed && !this.contextLost && !document.hidden) {
      this.last = 0;
      this.raf = requestAnimationFrame(this.tick);
    }
  }

  private approach(current: number, target: number, rate: number, dt: number): number {
    const step = rate * dt;
    return Math.abs(target - current) <= step ? target : current + Math.sign(target - current) * step;
  }

  private tick = (now: number): void => {
    this.raf = 0;
    if (!this.active || this.disposed || this.contextLost) return;
    const raw = this.last ? (now - this.last) / 1000 : 0;
    const dt = Math.min(0.1, raw);
    this.last = now;
    this.clock = now;
    const time = this.freeze ?? now / 1000;

    const cine = this.cine;
    if (cine && !cine.armed && !this.flight && this.morphStart < 0 && this.pop === this.popTarget) cine.armed = true;
    const touring = !!cine && cine.armed;
    if (cine && touring) {
      // absolute time, so the tour is the same at 8 frames a second as at 60; a long hitch slows it
      if (cine.playing) cine.t = Math.min(this.course.duration, cine.t + Math.min(0.25, raw));
      this.applyTour(cine.t);
    }

    if (this.morphStart >= 0) {
      const e = Math.min(1, (now - this.morphStart) / 1700);
      this.morph = this.morphFrom + (this.morphTo - this.morphFrom) * easeInOutCubic(e);
      if (e >= 1) {
        this.morphStart = -1;
        this.sweep = 0;
      }
    }
    let mixing = false;
    for (let b = 0; b < this.bands.length; b++) {
      const next = this.approach(this.bands[b], this.bandTarget[b], 3.6, dt);
      if (next !== this.bands[b]) {
        this.bands[b] = next;
        mixing = true;
      }
    }
    const pop = touring ? this.pop : this.approach(this.pop, this.popTarget, 1.8, dt);
    if (pop !== this.pop) {
      this.pop = pop;
      mixing = true;
    }
    if (mixing || this.waveDirty) this.updateWave();
    if (!mixing && this.marksDirty) this.placeWaveMarks();

    if (touring) {
      // the tour placed the camera above
    } else if (this.flight) {
      const f = this.flight;
      const e = easeInOutCubic(Math.min(1, (now - f.start) / f.ms));
      let turn = f.to.azimuth - f.from.azimuth;
      turn = Math.atan2(Math.sin(turn), Math.cos(turn)); // the short way round
      const far = f.from.target.distanceTo(f.to.target);
      this.applyPose({
        target: f.from.target.clone().lerp(f.to.target, e),
        radius: f.from.radius + (f.to.radius - f.from.radius) * e + 0.3 * far * Math.sin(Math.PI * e),
        azimuth: f.from.azimuth + turn * e,
        polar: f.from.polar + (f.to.polar - f.from.polar) * e,
      });
      if (e >= 1) this.flight = null;
    } else this.controls.update(dt || null);

    const u = this.terrain.material.uniforms;
    u.uMorph.value = this.morph;
    u.uSweep.value = this.sweep;
    u.uCompare.value = this.ghostLevel;
    u.uDead.value = this.pop;
    u.uTime.value = time;
    (u.uZetaPulse.value as Float32Array).set(this.pulses.zeta.subarray(0, 16));
    this.ghostMesh.material.uniforms.uOpacity.value = this.ghostLevel * this.morph;
    const quiet = 1 - 0.72 * this.morph; // the layout steps back while the wave is on stage
    for (const tube of this.tubes) tube.material.uniforms.uFlow.value = quiet;
    this.flow.material.uniforms.uFlow.value = quiet;
    this.flow.material.uniforms.uTime.value = time;
    this.fade(this.zetaMarks, this.land.meta.zeta.zeros.length, 1 - this.morph, this.pulses.zeta, dt);
    this.fade(this.waveMarks, this.currentZeros().length, this.morph, this.pulses.wave, dt);
    const lampPulse = this.lamps.geometry.getAttribute("aPulse") as BufferAttribute;
    for (let i = 0; i < this.lampPos.length; i++) lampPulse.setX(i, i === this.selected ? 0.9 : i === this.hovered ? 0.5 : 0);
    lampPulse.needsUpdate = true;

    this.renderer.render(this.scene, this.camera);
    this.layoutLabels();
    if (this.frames++ === 0) {
      document.documentElement.dataset.strip = "ready";
      (window as unknown as { __eddieStrip?: unknown }).__eddieStrip = this.debug();
    }
    if (cine && cine === this.cine && cine.playing && touring && cine.t >= this.course.duration) this.finishTour();
    // Keep going while anything can still move; a frozen or reduced-motion view draws on demand.
    // The next frame is requested directly: kick() restarts the clock for a view waking from idle,
    // and going through it here would make every frame's dt zero.
    const moving = this.flight || this.cine?.playing || mixing || this.morphStart >= 0 || this.waveDirty || this.marksDirty;
    if ((!this.still || moving) && !document.hidden) this.raf = requestAnimationFrame(this.tick);
  };

  private fade(points: Points<BufferGeometry, ShaderMaterial>, count: number, level: number, pulses: Float32Array, dt: number): void {
    const alpha = points.geometry.getAttribute("aAlpha") as BufferAttribute;
    const pulse = points.geometry.getAttribute("aPulse") as BufferAttribute;
    for (let i = 0; i < Math.min(count, alpha.count); i++) {
      pulses[i] = Math.max(0, pulses[i] - dt / 0.9);
      alpha.setX(i, level);
      pulse.setX(i, pulses[i]);
    }
    alpha.needsUpdate = true;
    pulse.needsUpdate = true;
    points.visible = level > 0.01;
  }

  private layoutLabels(): void {
    const c = this.camera;
    const key = `${c.position.x.toFixed(3)},${c.position.y.toFixed(3)},${c.position.z.toFixed(3)},${this.controls.target.x.toFixed(3)},${this.controls.target.z.toFixed(3)},${this.width},${this.height},${this.morph.toFixed(2)},${this.selected},${this.hovered}`;
    if (key === this.labelKey) return;
    this.labelKey = key;
    // zero badges are wide: place the nearest first and drop any that would touch one already placed
    const drawn: [number, number][] = [];
    const order = this.labels
      .map((label) => ({ label, far: label.kind === "zero" ? label.pos.distanceToSquared(c.position) : -1 }))
      .sort((a, b) => a.far - b.far);
    for (const { label } of order) {
      const [x, y, ok] = this.project(label.pos);
      let show = ok && x > 8 && x < this.width - 8 && y > 44 && y < this.height - 8;
      if (label.kind === "zero") {
        show = show && this.morph < 0.4 && x < this.width - 170 && !drawn.some(([dx, dy]) => Math.abs(dx - x) < 170 && Math.abs(dy - y) < 25);
        if (show) drawn.push([x, y]);
      }
      label.el.style.display = show ? "" : "none";
      if (!show) continue;
      // whole pixels at rest; during the tour's slow drifts whole pixels would step visibly
      label.el.style.transform = this.cine?.playing ? `translate(${x.toFixed(2)}px, ${y.toFixed(2)}px)` : `translate(${Math.round(x)}px, ${Math.round(y)}px)`;
      if (label.kind === "beacon") {
        label.el.dataset.state = label.index === this.selected ? "selected" : label.index === this.hovered ? "hover" : "";
      }
    }
  }

  private debug() {
    return {
      settled: () => !this.flight && !this.waveDirty && !this.marksDirty && (this.cine
        ? !this.cine.playing
        : this.morphStart < 0 && this.pop === this.popTarget && this.bands.every((b, i) => b === this.bandTarget[i])),
      state: () => ({
        morph: this.morph, bands: this.bandCount, pop: this.popTarget, popLevel: this.pop, compare: this.compare, selected: this.selected,
        zeros: this.currentZeros().length, frames: this.frames, camera: this.camera.position.toArray().map((v) => +v.toFixed(3)),
        renderer: this.renderer.info.render, pixelRatio: this.renderer.getPixelRatio(),
      }),
      lose: () => this.renderer.forceContextLoss(),
      tour: () => ({ duration: this.course.duration, shots: SHOTS.map((s) => ({ id: s.id, at: s.at })), t: this.cine?.t ?? null,
                     playing: !!this.cine?.playing, cue: this.cine?.cue ?? null,
                     cues: CUES.map((c) => ({ shot: c.shot, from: c.from })) }),
      tourSeek: (seconds: number) => this.seekTour(seconds),
      tourPlay: (from = 0) => this.playTour(from),
      tourAudit: () => auditTour(this.course),
    };
  }

  dispose(): void {
    this.disposed = true;
    if (this.raf) cancelAnimationFrame(this.raf);
    this.canvas.removeEventListener("pointermove", this.onPointerMove);
    this.canvas.removeEventListener("pointerdown", this.onPointerDown);
    this.canvas.removeEventListener("pointerup", this.onPointerUp);
    this.canvas.removeEventListener("pointerleave", this.onPointerLeave);
    this.canvas.removeEventListener("webglcontextlost", this.onLost);
    this.canvas.removeEventListener("webglcontextrestored", this.onRestored);
    document.removeEventListener("visibilitychange", this.onVisibility);
    this.controls.removeEventListener("start", this.onUserMove);
    this.controls.dispose();
    this.disposables.forEach((d) => d.dispose());
    this.renderer.dispose();
    // browsers cap live WebGL contexts per page; StrictMode and hot reload would leak them
    this.renderer.getContext().getExtension("WEBGL_lose_context")?.loseContext();
    this.canvas.remove();
    void this.host;
  }
}
