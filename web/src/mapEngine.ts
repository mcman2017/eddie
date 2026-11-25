// The proof map: a lazily loaded, zoomable circle tree drawn on one canvas.
//
// Level 0 is the dependency wheel: the top-level reduction (Main and its kernel) at the center
// and the proof pillars on a ring around it in chapter order (clockwise from the top, from the
// foundations to the chapters nearest Main). The heaviest net reference flows are drawn as
// directed curves, faint at the provider and solid with an arrowhead at the consumer. Every
// circle is a container: zooming into it packs its children inside (directories -> modules ->
// declarations), fetched from the read-only API on demand.
//
// Colour is the act a part belongs to, everywhere and at every depth. How far Eddie has worked on
// a declaration is how much of that colour it carries (a tint, then solid, then solid with a
// check), and code outside the proof is an uncoloured dashed ring.

import { packEnclose, packSiblings } from "d3-hierarchy";
import { api, type MapChild, type MapPage, type Overview, type PillarSummary } from "./api";
import { easeInOutCubic, flight, frameFor, toScreen, toWorld, type Flight, type Frame } from "./camera";
import { COLORS, TIER_FILL, actName, hexAlpha, type Status } from "./theme";

const WORLD_R = 1000;
const EXPAND_PX = 112; // draw an aggregate's children once its screen radius reaches this
const PREFETCH_PX = 72; // fetch an aggregate's children once its screen radius reaches this
const MAX_INFLIGHT = 3;
const MAX_SCALE = 40_000;
const SANS = 'system-ui, -apple-system, "Segoe UI", sans-serif';

export type NodeKind = "root" | "pillar" | "dir" | "group" | "module" | "decl" | "more";

const CAMEL = /[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+|[^A-Za-z0-9]+/g;

export interface VNode {
  key: string;
  treeId: number | null; // tree node id for aggregates
  declId: number | null; // declaration id for leaves
  pillarId: number | null;
  kind: NodeKind;
  label: string;
  context: string | null; // secondary line: a pillar's timeline step, or a declaration's full name
  decls: number;
  live: number;
  dead: number;
  verified: number;
  triaged: number;
  status: Status | null; // leaves only
  isRoot: boolean;
  weight: number;
  parent: VNode | null;
  children: VNode[] | null; // null until loaded
  loading: boolean;
  // layout relative to the parent, in units of the parent's radius, and the resolved world circle
  rx: number;
  ry: number;
  rr: number;
  angle: number; // ring position at level 0
  x: number;
  y: number;
  r: number;
  // last drawn screen circle
  sx: number;
  sy: number;
  sr: number;
  drawnAt: number;
  expandedAt: number;
}

export interface MapEvents {
  onFocus(path: VNode[]): void;
  onError(message: string): void;
}

function blank(partial: Partial<VNode> & Pick<VNode, "key" | "kind" | "label">, parent: VNode | null): VNode {
  return {
    treeId: null, declId: null, pillarId: null, context: null, decls: 0, live: 0, dead: 0, verified: 0, triaged: 0,
    status: null, isRoot: false, weight: 1, parent, children: null, loading: false, rx: 0, ry: 0, rr: 0, angle: 0,
    x: 0, y: 0, r: 0, sx: 0, sy: 0, sr: 0, drawnAt: -1, expandedAt: -1, ...partial,
  };
}

/** Second line of a ring label: the act, plus a step of the authors' PDF only when the match was confident. */
function stepLabel(p: PillarSummary | undefined): string | null {
  if (!p) return null;
  const step = p.timeline?.[0];
  const act = actName(p.tier);
  return step ? `${act}, PDF step ${step.number}: ${step.phrase}` : act;
}

function fromChild(c: MapChild, parent: VNode, pillars: Map<number, PillarSummary>): VNode {
  if (c.kind === "decl") {
    const status: Status = c.status ?? (c.live ? "live" : "dead");
    const score = c.score ?? 0;
    return blank(
      {
        key: `d${c.decl_id}`, kind: "decl", label: c.label, declId: c.decl_id ?? null, context: c.full_name ?? null,
        decls: 1, live: c.live, dead: c.dead, verified: status === "verified" ? 1 : 0,
        triaged: status === "triaged" ? 1 : 0, status, isRoot: !!c.is_root,
        weight: 1 + 8 * score * score + (c.is_root ? 6 : 0),
      },
      parent,
    );
  }
  const pillar = c.pillar_id != null && c.pillar_id >= 0 ? pillars.get(c.pillar_id) : undefined;
  return blank(
    {
      key: `n${c.id}`, kind: c.kind as NodeKind, label: c.label, treeId: Number(c.id), pillarId: c.pillar_id ?? null,
      context: c.kind === "pillar" ? stepLabel(pillar) : null, decls: c.decls, live: c.live, dead: c.dead,
      verified: c.verified ?? 0, triaged: c.triaged ?? 0, weight: Math.max(c.decls, 1),
    },
    parent,
  );
}

const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

export class MapEngine {
  readonly root: VNode;
  frame: Frame = { x: 0, y: 0, scale: 1 };
  hover: VNode | null = null;
  selectedDecl: number | null = null;
  /** Screen bands kept clear of ring labels (breadcrumb and search on top, legend below). */
  insets = { top: 108, bottom: 88 };

  private readonly ctx: CanvasRenderingContext2D;
  private width = 1;
  private height = 1;
  private dpr = 1;
  private sized = false;
  private anim: { flight: Flight; start: number } | null = null;
  private dirty = true;
  private raf = 0;
  private frameNo = 0;
  private inflight = 0;
  private queue: VNode[] = [];
  private focusKey = "";
  private focusPath: VNode[] = [];
  private readonly pillars = new Map<number, PillarSummary>();
  private readonly flows: Overview["pillar_flows"];
  private readonly reduceMotion: boolean;
  private disposed = false;
  private fadeNow = 1;

  constructor(
    private readonly canvas: HTMLCanvasElement,
    overview: Overview,
    rootPage: MapPage,
    private readonly events: MapEvents,
  ) {
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("canvas 2D is unavailable");
    this.ctx = ctx;
    for (const p of overview.pillars) this.pillars.set(p.id, p);
    this.flows = overview.pillar_flows ?? [];
    this.reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    const n = rootPage.node;
    this.root = blank(
      { key: `n${n.id}`, kind: "root", label: n.label, treeId: Number(n.id), decls: n.decls, live: n.live, dead: n.dead,
        verified: n.verified ?? 0, triaged: n.triaged ?? 0, x: 0, y: 0, r: WORLD_R, rr: 1 },
      null,
    );
    this.applyPage(this.root, rootPage);
  }

  dispose(): void {
    this.disposed = true;
    if (this.raf) cancelAnimationFrame(this.raf);
  }

  // --- geometry -------------------------------------------------------------------------------

  resize(width: number, height: number, dpr: number): void {
    this.width = Math.max(1, width);
    this.height = Math.max(1, height);
    this.dpr = dpr;
    this.canvas.width = Math.round(this.width * dpr);
    this.canvas.height = Math.round(this.height * dpr);
    if (!this.sized) {
      this.sized = true;
      this.frame = this.fitFrame(this.root);
    }
    this.invalidate();
  }

  private fitFrame(node: VNode): Frame {
    if (node.kind === "root") return frameFor({ x: 0, y: 0, r: WORLD_R }, this.width, this.height, 0.9);
    if (node.kind === "pillar" && node.parent?.kind === "root") {
      // a ring pillar at level 0 is framed with room for nothing else: its contents are the view
      return frameFor(node, this.width, this.height, 0.94);
    }
    return frameFor(node, this.width, this.height, node.kind === "decl" ? 0.45 : 0.93);
  }

  private minScale(): number {
    return this.fitFrame(this.root).scale * 0.55;
  }

  private layoutChildren(node: VNode): void {
    const kids = node.children ?? [];
    if (!kids.length) return;
    if (node.kind === "root") {
      this.layoutSpine(node);
      return;
    }
    const circles = kids.map((k) => ({ k, r: Math.sqrt(Math.max(k.weight, 1e-6)), x: 0, y: 0 }));
    circles.sort((a, b) => b.r - a.r || (a.k.key < b.k.key ? -1 : 1));
    packSiblings(circles);
    const enc = packEnclose(circles);
    // leave the top band of the container free for its own label
    const fit = 0.84 / enc.r;
    for (const c of circles) {
      c.k.rx = (c.x - enc.x) * fit;
      c.k.ry = (c.y - enc.y) * fit + 0.1;
      c.k.rr = c.r * fit * 0.94;
    }
  }

  private layoutSpine(root: VNode): void {
    const kids = root.children ?? [];
    const hub = kids.find((k) => k.pillarId === 0) ?? null;
    const chapter = (k: VNode) => this.pillars.get(k.pillarId ?? -1)?.flow_ord ?? 999;
    const ring = kids.filter((k) => k !== hub).sort((a, b) => chapter(a) - chapter(b));
    if (hub) {
      hub.rx = 0;
      hub.ry = 0;
      hub.rr = 0.15;
    }
    const rho = 0.64;
    const sizes = ring.map((k) => Math.sqrt(Math.max(k.weight, 1)));
    const total = sizes.reduce((a, b) => a + b, 0) || 1;
    const k = Math.min((2 * Math.PI * rho * 0.86) / (2 * total), 0.3 / Math.max(...sizes, 1));
    const radii = sizes.map((v) => v * k);
    const used = 2 * radii.reduce((a, b) => a + b, 0);
    const gap = (2 * Math.PI * rho - used) / Math.max(ring.length, 1);
    let arc = 0;
    ring.forEach((node, i) => {
      const theta = -Math.PI / 2 + (arc + radii[i] + gap / 2) / rho;
      node.angle = theta;
      node.rx = Math.cos(theta) * rho;
      node.ry = Math.sin(theta) * rho;
      node.rr = radii[i];
      arc += 2 * radii[i] + gap;
    });
  }

  private resolve(node: VNode): void {
    for (const c of node.children ?? []) {
      c.x = node.x + c.rx * node.r;
      c.y = node.y + c.ry * node.r;
      c.r = c.rr * node.r;
      this.resolve(c);
    }
  }

  // --- data -----------------------------------------------------------------------------------

  private applyPage(node: VNode, page: MapPage): void {
    const existing = new Map((node.children ?? []).map((c) => [c.key, c]));
    const kids: VNode[] = [];
    for (const c of page.children) {
      if (node.kind === "root" && c.decls === 0) continue; // empty re-export modules
      const fresh = fromChild(c, node, this.pillars);
      const old = existing.get(fresh.key);
      if (old) {
        old.verified = fresh.verified;
        old.triaged = fresh.triaged;
        old.status = fresh.status;
        kids.push(old);
      } else kids.push(fresh);
    }
    if (page.more && page.more.count > 0) {
      kids.push(
        blank(
          { key: `m${node.key}`, kind: "more", label: `+${page.more.count.toLocaleString()} more`,
            decls: page.more.decls, live: page.more.live, weight: Math.max(page.more.decls, 1) },
          node,
        ),
      );
    }
    node.children = kids;
    this.layoutChildren(node);
    this.resolve(node);
    this.invalidate();
  }

  private request(node: VNode): void {
    if (node.children || node.loading || node.treeId == null) return;
    node.loading = true;
    this.queue.push(node);
    this.pump();
  }

  private pump(): void {
    while (this.inflight < MAX_INFLIGHT && this.queue.length) {
      const node = this.queue.shift() as VNode;
      this.inflight++;
      api
        .node(node.treeId as number)
        .then((page) => !this.disposed && this.applyPage(node, page))
        .catch((e: Error) => this.events.onError(e.message))
        .finally(() => {
          this.inflight--;
          node.loading = false;
          this.invalidate();
          this.pump();
        });
    }
  }

  private async loadNow(node: VNode, include?: number): Promise<void> {
    if (node.treeId == null) return;
    const page = await api.node(node.treeId, include);
    if (!this.disposed) this.applyPage(node, page);
  }

  /** Load the path from the root to a tree node and return it. */
  async revealNode(treeId: number): Promise<VNode | null> {
    const page = await api.node(treeId);
    let node = this.root;
    for (const crumb of page.breadcrumb.slice(1)) {
      let next = node.children?.find((c) => c.treeId === crumb.id);
      if (!next) {
        await this.loadNow(node, crumb.id);
        next = node.children?.find((c) => c.treeId === crumb.id);
      }
      if (!next) return null;
      node = next;
    }
    return node;
  }

  async revealDecl(declId: number, moduleNodeId: number, instant = false): Promise<void> {
    const mod = await this.revealNode(moduleNodeId);
    if (!mod) return;
    if (!mod.children?.some((c) => c.declId === declId)) await this.loadNow(mod, declId);
    this.selectedDecl = declId;
    this.flyTo(mod, instant);
  }

  // --- camera ---------------------------------------------------------------------------------

  flyTo(node: VNode, instant = false): void {
    const target = this.fitFrame(node);
    if (node.kind !== "decl" && node.kind !== "more") this.request(node);
    if (instant || this.reduceMotion) {
      this.anim = null;
      this.frame = target;
    } else {
      this.anim = { flight: flight(this.frame, target, Math.min(this.width, this.height)), start: performance.now() };
    }
    this.invalidate();
  }

  stepOut(): void {
    const path = this.focusPath;
    this.flyTo(path.length > 1 ? path[path.length - 2] : this.root);
  }

  zoomAt(sx: number, sy: number, factor: number): void {
    this.anim = null;
    const [wx, wy] = toWorld(this.frame, this.width, this.height, sx, sy);
    const scale = clamp(this.frame.scale * factor, this.minScale(), MAX_SCALE);
    this.frame = { scale, x: wx - (sx - this.width / 2) / scale, y: wy - (sy - this.height / 2) / scale };
    this.invalidate();
  }

  panBy(dx: number, dy: number): void {
    this.anim = null;
    this.frame = { ...this.frame, x: this.frame.x - dx / this.frame.scale, y: this.frame.y - dy / this.frame.scale };
    this.invalidate();
  }

  setHover(node: VNode | null): void {
    if (node !== this.hover) {
      this.hover = node;
      this.invalidate();
    }
  }

  setSelected(declId: number | null): void {
    this.selectedDecl = declId;
    this.invalidate();
  }

  /** The deepest circle drawn under a screen point. */
  pick(sx: number, sy: number): VNode | null {
    const [wx, wy] = toWorld(this.frame, this.width, this.height, sx, sy);
    const slack = 4 / this.frame.scale;
    let node = this.root;
    let hit: VNode | null = null;
    for (;;) {
      if (node.kind !== "root" && node.expandedAt !== this.frameNo) break;
      const next = (node.children ?? []).find(
        (c) => c.drawnAt === this.frameNo && (c.x - wx) ** 2 + (c.y - wy) ** 2 <= (c.r + slack) ** 2,
      );
      if (!next) break;
      hit = next;
      node = next;
    }
    return hit;
  }

  isFocusOrAncestor(node: VNode): boolean {
    return this.focusPath.includes(node);
  }

  // --- frame loop -----------------------------------------------------------------------------

  invalidate(): void {
    this.dirty = true;
    if (!this.raf && !this.disposed) this.raf = requestAnimationFrame(this.tick);
  }

  private tick = (now: number): void => {
    this.raf = 0;
    let moving = false;
    if (this.anim) {
      const t = Math.min(1, (now - this.anim.start) / this.anim.flight.duration);
      this.frame = this.anim.flight.at(easeInOutCubic(t));
      if (t >= 1) this.anim = null;
      else moving = true;
      this.dirty = true;
    }
    if (this.dirty) {
      this.dirty = false;
      this.draw();
      this.updateFocus();
    }
    if (moving && !this.disposed) this.raf = requestAnimationFrame(this.tick);
  };

  private updateFocus(): void {
    const minDim = Math.min(this.width, this.height);
    const { x: cx, y: cy, scale } = this.frame;
    const path = [this.root];
    let node = this.root;
    for (;;) {
      const next = (node.children ?? []).find(
        (c) => c.kind !== "decl" && c.kind !== "more" && (c.x - cx) ** 2 + (c.y - cy) ** 2 <= c.r * c.r && c.r * scale >= 0.32 * minDim,
      );
      if (!next) break;
      path.push(next);
      node = next;
    }
    const key = path[path.length - 1].key;
    if (key !== this.focusKey) {
      this.focusKey = key;
      this.focusPath = path;
      this.events.onFocus(path);
    }
  }

  // --- drawing --------------------------------------------------------------------------------

  private draw(): void {
    const { ctx } = this;
    this.frameNo++;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.clearRect(0, 0, this.width, this.height);
    const fade = this.spineFade();
    this.fadeNow = fade;
    if (fade > 0) this.drawSpineEdges(fade);
    this.drawNode(this.root);
    if (fade > 0) this.drawRingLabels(fade);
  }

  private spineFade(): number {
    const zoom = this.frame.scale / this.fitFrame(this.root).scale;
    return clamp(1 - (Math.log2(Math.max(zoom, 1e-9)) - 0.6) / 1.6, 0, 1);
  }

  private screen(node: VNode): [number, number, number] {
    const [sx, sy] = toScreen(this.frame, this.width, this.height, node.x, node.y);
    return [sx, sy, node.r * this.frame.scale];
  }

  private drawNode(node: VNode): void {
    const [sx, sy, sr] = this.screen(node);
    if (sx + sr < 0 || sx - sr > this.width || sy + sr < 0 || sy - sr > this.height || sr < 0.5) return;
    node.drawnAt = this.frameNo;
    node.sx = sx;
    node.sy = sy;
    node.sr = sr;
    if (node.kind === "root") {
      for (const c of node.children ?? []) this.drawNode(c);
      return;
    }
    const aggregate = node.kind !== "decl" && node.kind !== "more";
    if (aggregate && sr >= PREFETCH_PX && !node.children) this.request(node);
    const expanded = aggregate && sr >= EXPAND_PX && !!node.children?.length;
    node.expandedAt = expanded ? this.frameNo : -1;
    if (node.kind === "decl") this.drawDecl(node, sx, sy, sr);
    else if (node.kind === "more") this.drawMore(node, sx, sy, sr);
    else if (expanded) this.drawContainer(node, sx, sy, sr);
    else this.drawAggregate(node, sx, sy, sr);
    if (expanded) for (const c of node.children ?? []) this.drawNode(c);
    if (node === this.hover) this.ring(sx, sy, sr + 2, hexAlpha(COLORS.ink, 0.85), 2);
  }

  /** The act colour of the chapter a node sits in; muted ink for anything outside every chapter. */
  private act(node: VNode): string {
    for (let n: VNode | null = node; n; n = n.parent) {
      const tier = n.pillarId != null ? this.pillars.get(n.pillarId)?.tier : undefined;
      if (tier) return TIER_FILL[tier];
    }
    return COLORS.ink3;
  }

  private disc(sx: number, sy: number, sr: number, fill: string): void {
    const { ctx } = this;
    ctx.beginPath();
    ctx.arc(sx, sy, sr, 0, Math.PI * 2);
    ctx.fillStyle = fill;
    ctx.fill();
  }

  private ring(sx: number, sy: number, sr: number, stroke: string, width: number, dash: number[] = []): void {
    const { ctx } = this;
    ctx.beginPath();
    ctx.arc(sx, sy, Math.max(sr, 0.5), 0, Math.PI * 2);
    ctx.setLineDash(dash);
    ctx.strokeStyle = stroke;
    ctx.lineWidth = width;
    ctx.stroke();
    ctx.setLineDash([]);
  }

  private drawAggregate(node: VNode, sx: number, sy: number, sr: number): void {
    const { ctx } = this;
    const act = this.act(node);
    const atSpine = node.parent?.kind === "root";
    if (node.live === 0) {
      this.disc(sx, sy, sr, hexAlpha(COLORS.chalk, 0.45));
      this.ring(sx, sy, sr, COLORS.slate, 1, [3, 3]);
    } else {
      const liveFrac = node.decls ? node.live / node.decls : 1;
      this.disc(sx, sy, sr, COLORS.raised); // opaque paper base: edges and siblings never show through
      // On the wheel the chapters are solid, fuller the more of them is live; deeper down a part
      // is a tint with a solid rim, so its ink label stays readable.
      this.disc(sx, sy, sr, hexAlpha(act, atSpine ? 0.55 + 0.45 * liveFrac : 0.16 + 0.26 * liveFrac));
      this.ring(sx, sy, sr, atSpine ? hexAlpha(COLORS.ink, 0.55) : act, atSpine ? 1 : 1.5);
      if (node.verified || node.triaged) {
        // honest progress, drawn in ink just outside the rim: arc length is the share of this
        // node's live declarations
        const lw = clamp(sr * 0.07, 2, 5);
        let a0 = -Math.PI / 2;
        for (const [count, color] of [[node.verified, COLORS.ink], [node.triaged, COLORS.ink3]] as const) {
          if (!count) continue;
          const a1 = a0 + (2 * Math.PI * count) / node.live;
          ctx.beginPath();
          ctx.arc(sx, sy, sr + lw / 2 + 2, a0, a1);
          ctx.strokeStyle = color;
          ctx.lineWidth = lw;
          ctx.stroke();
          a0 = a1;
        }
      }
      if (node.verified > 0) this.beacon(sx, sy - sr, clamp(sr * 0.1, 4, 8));
    }
    if (atSpine && node.pillarId === 0) {
      this.label("Main", sx, sy, clamp(sr * 0.34, 14, 26), sr * 1.8, COLORS.raised, 700);
    } else if (sr >= 26 && !(atSpine && this.fadeNow > 0.05)) {
      const size = clamp(sr * 0.2, 11, 18);
      this.wrapped(this.shortLabel(node), sx, sy, size, sr * 1.62, atSpine && node.live ? COLORS.raised : COLORS.ink, 600, sr >= 56 ? 2 : 1);
    }
    if (node.loading && sr >= PREFETCH_PX) this.ring(sx, sy, sr + 4, hexAlpha(COLORS.ink3, 0.6), 1.5, [2, 6]);
  }

  private drawContainer(node: VNode, sx: number, sy: number, sr: number): void {
    const act = this.act(node);
    this.disc(sx, sy, sr, node.live ? hexAlpha(act, 0.06) : hexAlpha(COLORS.chalk, 0.25));
    this.ring(sx, sy, sr, node.live ? hexAlpha(act, 0.7) : COLORS.slate, 1.25, node.live ? [] : [3, 3]);
    const size = clamp(sr * 0.055, 12, 22);
    this.label(this.shortLabel(node), sx, sy - sr * 0.87, size, sr * 1.1, COLORS.ink2, 600);
  }

  private drawDecl(node: VNode, sx: number, sy: number, sr: number): void {
    const status = node.status ?? "live";
    const selected = node.declId != null && node.declId === this.selectedDecl;
    const act = this.act(node);
    if (status === "dead") {
      this.disc(sx, sy, sr, hexAlpha(COLORS.chalk, 0.45));
      this.ring(sx, sy, sr, COLORS.slate, 1, [3, 3]);
    } else {
      // the more Eddie has done with a declaration, the more of its act's colour it carries
      this.disc(sx, sy, sr, COLORS.raised);
      this.disc(sx, sy, sr, hexAlpha(act, status === "verified" ? 1 : status === "triaged" ? 0.62 : 0.24));
      this.ring(sx, sy, sr, status === "verified" ? COLORS.ink : act, status === "verified" ? 2 : 1.25);
      if (status === "verified" && sr >= 12) this.check(sx, sy, sr);
    }
    if (node.isRoot) this.ring(sx, sy, sr + 3.5, COLORS.ink, 1.5);
    if (selected) {
      this.ring(sx, sy, sr + (node.isRoot ? 8 : 4.5), COLORS.ink, 2.5);
    }
    if (sr >= 24) {
      const solid = status === "verified";
      const size = clamp(sr * 0.22, 11, 16);
      const y = solid && sr >= 12 ? sy + sr * 0.45 : sy;
      this.wrapped(node.label, sx, y, size, sr * 1.6, solid ? COLORS.raised : COLORS.ink, 600, sr >= 60 ? 2 : 1);
    }
  }

  private drawMore(node: VNode, sx: number, sy: number, sr: number): void {
    this.ring(sx, sy, sr, hexAlpha(COLORS.ink3, 0.6), 1, [4, 4]);
    if (sr >= 22) this.label(node.label, sx, sy, clamp(sr * 0.2, 11, 15), sr * 1.7, COLORS.ink3, 500);
  }

  /** A small ink badge on the rim of a part that holds a verified witness. */
  private beacon(sx: number, sy: number, r: number): void {
    this.disc(sx, sy, r + 1.5, COLORS.raised);
    this.disc(sx, sy, r, COLORS.ink);
  }

  private check(sx: number, sy: number, sr: number): void {
    const { ctx } = this;
    const s = clamp(sr * 0.32, 4, 18);
    const cy = sr >= 24 ? sy - sr * 0.18 : sy;
    ctx.beginPath();
    ctx.moveTo(sx - s * 0.55, cy);
    ctx.lineTo(sx - s * 0.12, cy + s * 0.45);
    ctx.lineTo(sx + s * 0.62, cy - s * 0.45);
    ctx.strokeStyle = COLORS.raised;
    ctx.lineWidth = clamp(s * 0.22, 1.5, 4);
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.stroke();
  }

  private shortLabel(node: VNode): string {
    const parts = node.label.split(" · ");
    return parts[parts.length - 1];
  }

  private fit(text: string, maxWidth: number): string {
    const { ctx } = this;
    if (ctx.measureText(text).width <= maxWidth) return text;
    let lo = 0;
    let hi = text.length;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (ctx.measureText(text.slice(0, mid) + "…").width <= maxWidth) lo = mid;
      else hi = mid - 1;
    }
    return lo >= 3 ? text.slice(0, lo) + "…" : "";
  }

  private label(text: string, x: number, y: number, size: number, maxWidth: number, color: string, weight: number,
                align: CanvasTextAlign = "center", baseline: CanvasTextBaseline = "middle"): void {
    const { ctx } = this;
    ctx.font = `${weight} ${size.toFixed(1)}px ${SANS}`;
    const shown = this.fit(text, maxWidth);
    if (!shown) return;
    ctx.textAlign = align;
    ctx.textBaseline = baseline;
    ctx.fillStyle = color;
    ctx.fillText(shown, x, y);
  }

  /** A centered label over up to `maxLines` lines, broken at spaces or CamelCase boundaries. */
  private wrapped(text: string, x: number, y: number, size: number, maxWidth: number, color: string, weight: number,
                  maxLines: number): void {
    const { ctx } = this;
    ctx.font = `${weight} ${size.toFixed(1)}px ${SANS}`;
    if (maxLines <= 1 || ctx.measureText(text).width <= maxWidth) {
      this.label(text, x, y, size, maxWidth, color, weight);
      return;
    }
    const tokens = text.includes(" ") ? text.split(/(?<= )/) : (text.match(CAMEL) ?? [text]);
    const lines: string[] = [];
    let line = "";
    for (const t of tokens) {
      if (lines.length === maxLines - 1) {
        line += t;
        continue;
      }
      if (line && ctx.measureText(line + t).width > maxWidth) {
        lines.push(line.trimEnd());
        line = t.trimStart();
      } else line += t;
    }
    lines.push(line.trimEnd());
    const lh = size * 1.15;
    const top = y - ((lines.length - 1) * lh) / 2;
    lines.forEach((l, i) => this.label(l, x, top + i * lh, size, maxWidth, color, weight));
  }

  private ringNodes(): { hub: VNode | null; ring: VNode[] } {
    const kids = this.root.children ?? [];
    const hub = kids.find((k) => k.pillarId === 0) ?? null;
    return { hub, ring: kids.filter((k) => k !== hub) };
  }

  private drawSpineEdges(fade: number): void {
    const { ctx } = this;
    const { hub, ring } = this.ringNodes();
    if (!hub) return;
    const [hx, hy, hr] = this.screen(hub);
    // the spine: Main's reduction reaches every pillar
    ctx.lineCap = "round";
    for (const node of ring) {
      const [nx, ny, nr] = this.screen(node);
      const d = Math.hypot(nx - hx, ny - hy) || 1;
      const ux = (nx - hx) / d;
      const uy = (ny - hy) / d;
      ctx.beginPath();
      ctx.moveTo(hx + ux * (hr + 4), hy + uy * (hr + 4));
      ctx.lineTo(nx - ux * (nr + 4), ny - uy * (nr + 4));
      ctx.strokeStyle = hexAlpha(COLORS.ink3, 0.22 * fade);
      ctx.lineWidth = 1;
      ctx.stroke();
    }
    // net reference flows, provider -> consumer, bundled through the hub
    const byPillar = new Map<number | null, VNode>(ring.map((n) => [n.pillarId, n]));
    byPillar.set(hub.pillarId, hub);
    const maxW = Math.log1p(Math.max(...this.flows.map((e) => e.net), 1));
    for (const e of this.flows.slice(0, 44)) {
      const a = byPillar.get(e.from);
      const b = byPillar.get(e.to);
      if (!a || !b) continue;
      const toHub = b === hub;
      const norm = toHub ? 1 : Math.log1p(e.net) / maxW;
      const [ax, ay] = this.screen(a);
      const [bx, by, br] = this.screen(b);
      const [cx, cy] = toHub
        ? [(ax + bx) / 2, (ay + by) / 2]
        : toScreen(this.frame, this.width, this.height, ((a.x + b.x) / 2) * 0.18, ((a.y + b.y) / 2) * 0.18);
      // stop at the consumer's rim, where the arrowhead sits
      const dx = bx - cx;
      const dy = by - cy;
      const len = Math.hypot(dx, dy) || 1;
      const ex = bx - (dx / len) * (br + 3);
      const ey = by - (dy / len) * (br + 3);
      // a flow takes the act colour of the chapter it leaves
      const act = this.act(a);
      const strong = (0.3 + 0.6 * norm * norm) * fade;
      const stroke = ctx.createLinearGradient(ax, ay, ex, ey);
      stroke.addColorStop(0, hexAlpha(act, strong * 0.12));
      stroke.addColorStop(1, hexAlpha(act, strong));
      ctx.beginPath();
      ctx.moveTo(ax, ay);
      ctx.quadraticCurveTo(cx, cy, ex, ey);
      ctx.strokeStyle = stroke;
      ctx.lineWidth = 1 + 2.5 * norm;
      ctx.stroke();
      const head = 4 + 5 * norm;
      const ux = dx / len;
      const uy = dy / len;
      ctx.beginPath();
      ctx.moveTo(ex, ey);
      ctx.lineTo(ex - ux * head * 1.7 - uy * head * 0.7, ey - uy * head * 1.7 + ux * head * 0.7);
      ctx.lineTo(ex - ux * head * 1.7 + uy * head * 0.7, ey - uy * head * 1.7 - ux * head * 0.7);
      ctx.closePath();
      ctx.fillStyle = hexAlpha(act, Math.min(1, strong * 1.4));
      ctx.fill();
    }
  }

  private drawRingLabels(fade: number): void {
    const { ctx } = this;
    const { hub, ring } = this.ringNodes();
    ctx.globalAlpha = fade;
    const [cx] = toScreen(this.frame, this.width, this.height, 0, 0);
    const outer = Math.max(0, ...ring.map((n) => Math.abs(n.sx - cx) + n.sr));
    const lh = 19;
    const gapY = 9;
    for (const side of [1, -1]) {
      // pie-chart labels: two columns beside the ring, pushed apart, joined to their circle by a leader
      const items = ring
        .filter((n) => n.drawnAt === this.frameNo && n.expandedAt !== this.frameNo && (Math.cos(n.angle) >= 0 ? 1 : -1) === side)
        .map((n) => {
          const h = (n.context ? 2 : 1) * lh;
          return { n, h, y: n.sy - h / 2, ex: n.sx + Math.cos(n.angle) * n.sr, ey: n.sy + Math.sin(n.angle) * n.sr };
        })
        .sort((a, b) => a.y - b.y);
      for (let i = 1; i < items.length; i++) items[i].y = Math.max(items[i].y, items[i - 1].y + items[i - 1].h + gapY);
      if (items[0] && items[0].y < this.insets.top) {
        items[0].y = this.insets.top;
        for (let i = 1; i < items.length; i++) items[i].y = Math.max(items[i].y, items[i - 1].y + items[i - 1].h + gapY);
      }
      const last = items[items.length - 1];
      const floor = this.height - this.insets.bottom;
      if (last && last.y + last.h > floor) {
        last.y = floor - last.h;
        for (let i = items.length - 2; i >= 0; i--) items[i].y = Math.min(items[i].y, items[i + 1].y - items[i].h - gapY);
      }
      const colX = cx + side * (outer + 34);
      const room = side > 0 ? this.width - colX - 16 : colX - 16;
      const align: CanvasTextAlign = side > 0 ? "left" : "right";
      for (const it of items) {
        const midY = it.y + lh / 2;
        ctx.beginPath();
        ctx.moveTo(it.ex, it.ey);
        ctx.lineTo(colX - side * 8, midY);
        ctx.strokeStyle = hexAlpha(COLORS.ink3, 0.5);
        ctx.lineWidth = 1;
        ctx.stroke();
        // the leader ends in a chip of the chapter's act colour, beside its name
        this.disc(colX - side * 8, midY, 6, COLORS.raised);
        this.disc(colX - side * 8, midY, 4.5, this.act(it.n));
        const chapter = this.pillars.get(it.n.pillarId ?? -1)?.chapter;
        this.label(`${chapter != null ? `${chapter}. ` : ""}${this.shortLabel(it.n)}`, colX, it.y, 16, room, COLORS.ink, 600, align, "top");
        if (it.n.context) this.label(it.n.context, colX, it.y + lh, 13.5, room, COLORS.ink3, 400, align, "top");
      }
    }
    if (hub && hub.drawnAt === this.frameNo && hub.expandedAt !== this.frameNo) {
      this.label("riemannHypothesis", hub.sx, hub.sy + hub.sr + 20, 15, 260, COLORS.ink2, 500);
    }
    ctx.globalAlpha = 1;
  }

}
