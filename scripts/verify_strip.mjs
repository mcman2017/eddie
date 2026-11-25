#!/usr/bin/env node
// Drives headless Chrome over the DevTools protocol to check the 3D critical strip end to end:
// WebGL comes up, every mode and camera preset renders, a beacon click opens its chapter, the
// tour buttons move, and nothing logs an error. Loopback only; needs Node 22+ (built-in
// WebSocket and fetch) and a running app (make api after npm --prefix web run build, or make dev).
//
//   node scripts/verify_strip.mjs [--base http://127.0.0.1:8765] [--out data/verify] [--port 9333]
//
// Screenshots land in --out (git-ignored under data/). Frame times measured here come from
// software GL and say nothing about the recording machine.

import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, all) => (a.startsWith("--") ? [...acc, [a.slice(2), all[i + 1]]] : acc), []));
const base = args.base ?? "http://127.0.0.1:8765";
const out = resolve(args.out ?? "data/verify");
const port = Number(args.port ?? 9333);
const chrome = args.chrome ?? "google-chrome";
if (!/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(base)) throw new Error("--base must be a loopback address");
mkdirSync(out, { recursive: true });

const browser = spawn(chrome, [
  "--headless=new", `--user-data-dir=${out}/chrome-profile`, `--remote-debugging-port=${port}`, "--remote-debugging-address=127.0.0.1",
  "--window-size=1920,1080", "--force-device-scale-factor=1", "--hide-scrollbars", "--force-color-profile=srgb", "--disable-lcd-text",
  "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist",
  "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--disable-sync", "--disable-component-update",
  "--disable-background-networking", "--mute-audio", "--autoplay-policy=no-user-gesture-required",
  "--disable-background-timer-throttling", "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows", "about:blank",
], { stdio: "ignore" });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const failures = [];
const notes = [];
const check = (ok, what) => {
  (ok ? notes : failures).push(`${ok ? "ok  " : "FAIL"} ${what}`);
  console.log(`${ok ? "ok  " : "FAIL"} ${what}`);
};

async function connect() {
  for (let i = 0; i < 60; i++) {
    try {
      const target = await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`, { method: "PUT" })).json();
      return target.webSocketDebuggerUrl;
    } catch {
      await sleep(250);
    }
  }
  throw new Error("Chrome did not open its debugging port");
}

async function main() {
  const ws = new WebSocket(await connect());
  await new Promise((r, j) => {
    ws.onopen = r;
    ws.onerror = j;
  });
  let next = 0;
  const waiting = new Map();
  const errors = [];
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.id && waiting.has(msg.id)) {
      const { ok, bad } = waiting.get(msg.id);
      waiting.delete(msg.id);
      if (msg.error) bad(new Error(msg.error.message));
      else ok(msg.result);
    } else if (msg.method === "Runtime.exceptionThrown") errors.push(msg.params.exceptionDetails.exception?.description ?? msg.params.exceptionDetails.text);
    else if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") errors.push(msg.params.args.map((a) => a.value ?? a.description).join(" "));
    else if (msg.method === "Log.entryAdded" && msg.params.entry.level === "error") errors.push(`${msg.params.entry.text} ${msg.params.entry.url ?? ""}`);
  };
  const send = (method, params = {}) => new Promise((ok, bad) => {
    const id = ++next;
    waiting.set(id, { ok, bad });
    ws.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async (expression) => {
    const r = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description ?? r.exceptionDetails.text);
    return r.result.value;
  };
  const until = async (expression, what, ms = 20000) => {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
      if (await evaluate(expression).catch(() => false)) return true;
      await sleep(100);
    }
    check(false, `timed out waiting for ${what}`);
    return false;
  };
  const open = async (hash) => {
    await send("Page.navigate", { url: "about:blank" });
    await sleep(120);
    await send("Page.navigate", { url: `${base}/${hash ? `#${hash}` : ""}` });
  };
  const ready = () => until("document.documentElement.dataset.strip === 'ready' && window.__eddieStrip.settled()", "the strip to settle");
  const shot = async (name) => {
    const { data } = await send("Page.captureScreenshot", { format: "png" });
    writeFileSync(`${out}/${name}.png`, Buffer.from(data, "base64"));
  };
  const click = async (selector) => {
    const box = await evaluate(`(() => { const r = document.querySelector(${JSON.stringify(selector)})?.getBoundingClientRect(); return r ? [r.left + r.width / 2, r.top + r.height / 2] : null; })()`);
    if (!box) return false;
    for (const type of ["mouseMoved", "mousePressed", "mouseReleased"]) {
      await send("Input.dispatchMouseEvent", { type, x: box[0], y: box[1], button: type === "mouseMoved" ? "none" : "left", clickCount: type === "mouseMoved" ? 0 : 1 });
    }
    return true;
  };

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Log.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: 1920, height: 1080, deviceScaleFactor: 1, mobile: false });

  // 1) WebGL and the first frame
  await open("freeze=12.5");
  if (!(await ready())) throw new Error(`the strip never became ready (errors: ${errors.join(" | ") || "none"})`);
  const gl = await evaluate("(() => { const c = document.createElement('canvas').getContext('webgl2'); const e = c && c.getExtension('WEBGL_debug_renderer_info'); return c ? (e ? c.getParameter(e.UNMASKED_RENDERER_WEBGL) : 'webgl2') : null; })()");
  check(!!gl, `WebGL 2 context (${gl})`);
  check((await evaluate("[innerWidth, innerHeight].join('x')")) === "1920x1080", "viewport is 1920x1080");
  const state = await evaluate("window.__eddieStrip.state()");
  check(state.renderer.triangles > 50000 && state.renderer.calls >= 6, `scene drawn: ${state.renderer.triangles} triangles in ${state.renderer.calls} calls`);
  check((await evaluate("document.querySelectorAll('[data-testid=beacon]').length")) === (await evaluate("fetch('/api/chapters').then(r => r.json()).then(j => j.chapters.length)")), "one beacon label per chapter");
  await shot("01-strip-overview");

  // 2) every deterministic state
  const shots = [
    ["02-strip-dive", "freeze=12.5&cam=dive"], ["03-strip-finale", "freeze=12.5&cam=finale"],
    ["04-wave-live", "freeze=12.5&mode=wave"], ["05-wave-dead", "freeze=12.5&mode=wave&pop=dead"],
    ["06-wave-compare", "freeze=12.5&mode=wave&pop=compare"], ["07-wave-3-harmonics", "freeze=12.5&mode=wave&bands=3"],
    ["08-wave-dive", "freeze=12.5&mode=wave&cam=dive"], ["09-wheel", "view=wheel"],
  ];
  for (const [name, hash] of shots) {
    await open(hash);
    if (name === "09-wheel") await until("!!document.querySelector('.view:not(.view-off) .legend')", "the wheel");
    else await ready();
    await sleep(name === "09-wheel" ? 900 : 150);
    await shot(name);
  }
  // half-way through the morph: both sets of phase lines are on screen, cross-faded
  await open("freeze=12.5&cam=dive");
  await ready();
  await evaluate("(() => { const el = document.querySelector('.strip-mode input'); const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set; set.call(el, '50'); el.dispatchEvent(new Event('input', { bubbles: true })); })()");
  await until("Math.abs(window.__eddieStrip.state().morph - 0.5) < 0.02 && window.__eddieStrip.settled()", "the blend slider to reach the middle");
  await sleep(150);
  await shot("12-mid-morph");
  await open("freeze=12.5&mode=wave");
  await ready();
  const wave = await evaluate("window.__eddieStrip.state()");
  check(wave.morph === 1 && wave.zeros > 0, `wave mode shows ${wave.zeros} zero markers`);

  // 3) chapters: deep link, badge, tour buttons
  const chapters = await evaluate("fetch('/api/chapters').then(r => r.json()).then(j => j.chapters.map(c => [c.slug, c.badge]))");
  const last = chapters[chapters.length - 1][0];
  await open(`freeze=12.5&chapter=${last}`);
  await ready();
  await until(`document.querySelector('[data-testid=chapter-guide]')?.dataset.slug === ${JSON.stringify(last)}`, "the Main chapter guide");
  check((await evaluate("document.querySelector('[data-testid=stub-badge]')?.textContent")) === chapters[chapters.length - 1][1], "the stub badge is shown verbatim");
  check((await evaluate("document.querySelectorAll('.guide .figure').length")) >= 1, "the Main chapter shows its witnesses");
  check((await evaluate("!!document.querySelector('.guide .segmented')")), "the rejected-vs-verified toggle is present");
  await shot("10-chapter-main");

  // 4) live interaction: click a beacon, the drawer opens and the camera flies; Next moves on
  await open("");
  await ready();
  const before = (await evaluate("window.__eddieStrip.state()")).camera;
  const pickIndex = Math.min(6, chapters.length - 2);
  check(await click(`[data-testid="beacon"][data-chapter="${pickIndex + 1}"]`), `clicked beacon ${pickIndex + 1}`);
  await until(`document.querySelector('[data-testid=chapter-guide]')?.dataset.slug === ${JSON.stringify(chapters[pickIndex][0])}`, "the clicked chapter to open");
  await until("window.__eddieStrip.settled()", "the flight to finish");
  const after = (await evaluate("window.__eddieStrip.state()")).camera;
  check(Math.hypot(after[0] - before[0], after[1] - before[1], after[2] - before[2]) > 3, "the camera flew to the beacon");
  check((await evaluate("location.hash")).includes(`chapter=${chapters[pickIndex][0]}`), "the chapter is in the URL");
  check(await click("[data-testid=tour-next]"), "clicked Next");
  await until(`document.querySelector('[data-testid=chapter-guide]')?.dataset.slug === ${JSON.stringify(chapters[pickIndex + 1][0])}`, "Next to open the following chapter");
  await until("window.__eddieStrip.settled()", "the second flight to finish");
  const third = (await evaluate("window.__eddieStrip.state()")).camera;
  check(Math.hypot(third[0] - after[0], third[1] - after[1], third[2] - after[2]) > 0.5, "Next moved the camera");
  await shot("11-chapter-after-click");

  // 5) the synth and the tour run from their buttons
  check(await click(".strip-actions .play"), "clicked Play spectral harmonics");
  await until("/Zero 1 of 12: t = 14\\.1347, sounding at 372 Hz/.test(document.querySelector('.strip-hud')?.textContent ?? '')", "the first zero to sound at its mapped pitch");
  await until("/Zero 2 of 12/.test(document.querySelector('.strip-hud')?.textContent ?? '')", "the second partial to enter");
  check(await click(".strip-actions .play"), "stopped the chord");
  check(await click(".strip-cams button:nth-child(4)"), "started the tour");
  await until(`document.querySelector('[data-testid=chapter-guide]')?.dataset.slug === ${JSON.stringify(chapters[0][0])}`, "the tour to open chapter 1");
  await until(`document.querySelector('[data-testid=chapter-guide]')?.dataset.slug === ${JSON.stringify(chapters[1][0])}`, "the tour to move on to chapter 2");
  check(await click(".strip-cams button:nth-child(4)"), "stopped the tour");

  // 6) frame pacing (software GL here: an upper bound on trouble, not a prediction)
  const frames = await evaluate("new Promise((done) => { const t = []; const step = (now) => { t.push(now); if (t.length < 90) requestAnimationFrame(step); else { const d = t.slice(1).map((v, i) => v - t[i]).sort((a, b) => a - b); done({ median: d[Math.floor(d.length / 2)], p95: d[Math.floor(d.length * 0.95)] }); } }; requestAnimationFrame(step); })");
  console.log(`note frame time on ${gl}: median ${frames.median.toFixed(1)} ms, p95 ${frames.p95.toFixed(1)} ms`);

  check(errors.length === 0, `no console errors${errors.length ? `: ${errors.slice(0, 5).join(" | ")}` : ""}`);
  ws.close();
}

main()
  .catch((e) => {
    failures.push(`FAIL ${e.message}`);
    console.log(`FAIL ${e.message}`);
  })
  .finally(() => {
    browser.kill();
    console.log(`${failures.length ? "FAILED" : "PASSED"}: ${notes.length} checks ok, ${failures.length} failed; screenshots in ${out}`);
    process.exit(failures.length ? 1 : 0);
  });
