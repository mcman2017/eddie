#!/usr/bin/env node
// Drives headless Chrome over the DevTools protocol to check the 3D critical strip end to end:
// WebGL comes up on a light sky, every mode and camera preset renders, a beacon click opens its
// chapter, the visual tour and its five shots land on their frames, the capture layout puts the
// drawer away, and nothing logs an error. Loopback only; needs Node 22+ (built-in
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
  // nothing here may leave the machine, a YouTube embed included
  "--host-resolver-rules=MAP *.youtube-nocookie.com ~NOTFOUND, MAP *.youtube.com ~NOTFOUND",
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
  const press = async (key, code) => {
    for (const type of ["keyDown", "keyUp"]) await send("Input.dispatchKeyEvent", { type, key, code: key, windowsVirtualKeyCode: code });
  };
  // mean luminance (0..1) of a small square of the page as it is actually drawn
  const luminance = async (x, y, size = 24) => {
    const { data } = await send("Page.captureScreenshot", { format: "png", clip: { x, y, width: size, height: size, scale: 1 } });
    return evaluate(`new Promise((done) => { const img = new Image(); img.onload = () => { const c = document.createElement('canvas'); c.width = img.width; c.height = img.height; const g = c.getContext('2d'); g.drawImage(img, 0, 0); const d = g.getImageData(0, 0, c.width, c.height).data; let s = 0; for (let i = 0; i < d.length; i += 4) s += 0.2126 * d[i] + 0.7152 * d[i + 1] + 0.0722 * d[i + 2]; done(s / (d.length / 4) / 255); }; img.src = 'data:image/png;base64,${data}'; })`);
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
  // light theme: the sky of the 3D view (a corner the model never reaches) and the page are paper
  const sky = await luminance(40, 300);
  check(sky > 0.9, `the 3D sky is light (luminance ${sky.toFixed(2)})`);
  check((await evaluate("getComputedStyle(document.documentElement).colorScheme")) === "light", "the page declares a light colour scheme");
  check(await evaluate("!!document.querySelector('[data-testid=strip-legend] .legend-ramp') && document.querySelectorAll('[data-testid=strip-legend] .act-chip').length === 4"), "the legend shows the height ramp and the four acts");
  const gate = await evaluate("fetch('/api/landscape').then(r => r.json()).then(j => j.gate)");
  check(!!gate && (await evaluate("document.querySelector('[data-testid=gate-mark]')?.textContent ?? ''")).includes(`gates ${gate.share.toFixed(1)}%`), `the gate theorem is marked on its chapter (${gate?.full_name}, ${gate?.share.toFixed(1)}%)`);
  check(await click("[data-testid=findings-toggle]"), "opened the findings card");
  const takeaway = await evaluate("fetch('/api/landscape').then(r => r.json()).then(j => j.takeaway)");
  check(!!takeaway && (await evaluate("document.querySelector('[data-testid=takeaway]')?.textContent")) === takeaway, "the findings card leads with the takeaway built from the numbers");
  await sleep(150);
  await shot("21-findings");
  check(await click("[data-testid=findings-toggle]"), "closed the findings card");
  await sleep(100);
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
  await evaluate("(() => { const el = document.querySelector('[data-testid=blend]'); const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set; set.call(el, '50'); el.dispatchEvent(new Event('input', { bubbles: true })); })()");
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
  const lastSource = await evaluate(`fetch('/api/chapters/${chapters[chapters.length - 1][0]}').then((r) => r.json()).then((c) => c.source)`);
  if (lastSource === "seed") {
    check((await evaluate("document.querySelector('[data-testid=stub-badge]')?.textContent")) === chapters[chapters.length - 1][1], "the stub badge is shown verbatim");
  } else {
    check(await evaluate("!document.querySelector('[data-testid=stub-badge]') && !!document.querySelector('[data-testid=source-badge]')"),
      `a chapter written by ${lastSource} carries its source badge, not the stub badge`);
  }
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
  check(await click("[data-testid=harmonics-play]"), "clicked Play spectral harmonics");
  await until("/Zero 1 of 12: t = 14\\.1347, sounding at 372 Hz/.test(document.querySelector('.strip-hud')?.textContent ?? '')", "the first zero to sound at its mapped pitch");
  await until("/Zero 2 of 12/.test(document.querySelector('.strip-hud')?.textContent ?? '')", "the second partial to enter");
  check(await click("[data-testid=harmonics-play]"), "stopped the chord");

  // 5b) the visual tour, frame by frame. A frozen page can be put on any frame of the tour, so
  //     each shot is captured exactly, and each pill must land on its shot's key frame.
  await open("freeze=12.5");
  await ready();
  const tour = await evaluate("window.__eddieStrip.tour()");
  check(tour.duration >= 30 && tour.duration <= 45 && tour.shots.length === 5, `the tour has five shots and lasts ${tour.duration} s`);
  const audit = await evaluate("window.__eddieStrip.tourAudit()");
  check(audit.minSpeed > 0.5 && audit.jerk < 0.2 && audit.minHeight > 3,
    `the camera never stops or jolts mid-tour (slowest ${audit.minSpeed.toFixed(2)} units/s, largest step in velocity ${(100 * audit.jerk).toFixed(1)}%, lowest eye ${audit.minHeight.toFixed(2)})`);
  check((await evaluate("document.querySelectorAll('[data-testid=shot-pill]').length")) === 5, "five shot pills");
  const caption = "document.querySelector('[data-testid=strip-caption]')";
  for (const [i, s] of tour.shots.entries()) {
    await evaluate(`window.__eddieStrip.tourSeek(${s.at})`);
    await until(`${caption}?.dataset.shot === '${s.id}' && window.__eddieStrip.settled()`, `the tour frame of shot ${i + 1}`);
    const seek = await evaluate("window.__eddieStrip.state()");
    await sleep(150);
    await shot(`${13 + i}-tour-${i + 1}-${s.id}`);
    check(await click(`[data-testid=shot-pill][data-shot="${s.id}"]`), `clicked pill ${i + 1}`);
    await until(`${caption}?.dataset.shot === '${s.id}' && window.__eddieStrip.tour().t === null && window.__eddieStrip.settled()`, `pill ${i + 1} to settle`);
    const pill = await evaluate("window.__eddieStrip.state()");
    check(Math.hypot(...seek.camera.map((v, k) => v - pill.camera[k])) < 0.05 && seek.morph === pill.morph && seek.pop === pill.pop && seek.compare === pill.compare,
      `pill ${i + 1} (${s.id}) lands on the tour's key frame`);
  }
  await evaluate("window.__eddieStrip.tourSeek(25.6)");
  await until("window.__eddieStrip.settled() && window.__eddieStrip.state().popLevel === 1", "the tour's dead-code frame");
  await sleep(150);
  await shot("18-tour-dead-code");

  // 5c) the tour on a running clock: it advances, Escape hands the view back, and it ends by
  //     opening the last chapter on the zeta strip.
  await open("");
  await ready();
  check(await click("[data-testid=tour-play]"), "started the visual tour");
  await until("window.__eddieStrip.tour().playing && window.__eddieStrip.tour().t > 0.3", "the tour's clock to start");
  const t0 = [Date.now(), (await evaluate("window.__eddieStrip.tour().t"))];
  await sleep(2500);
  const t1 = [Date.now(), (await evaluate("window.__eddieStrip.tour().t"))];
  const pace = (t1[1] - t0[1]) / ((t1[0] - t0[0]) / 1000);
  check(pace > 0.5 && pace < 1.1, `the tour keeps wall-clock time whatever the frame rate (${pace.toFixed(2)} s per second)`);
  await press("Escape", 27);
  await until(`window.__eddieStrip.tour().t === null && ${caption}?.dataset.cue === ''`, "Escape to stop the tour and drop its caption");
  await evaluate("window.__eddieStrip.tourPlay(38.6)");
  await until(`document.querySelector('[data-testid=chapter-guide]')?.dataset.slug === ${JSON.stringify(last)} && window.__eddieStrip.tour().t === null`, "the tour to end by opening the last chapter", 30000);
  const ended = await evaluate("window.__eddieStrip.state()");
  check(ended.morph === 0 && (await evaluate("location.hash")).includes("shot=5"), "the tour ends on the zeta strip at the summit");

  // 5d) the capture layout: top bar, canvas, legend and caption; the drawer comes in with the last chapter
  await open("cinema&freeze=12.5");
  await ready();
  const hidden = "(() => { const r = document.querySelector('.side').getBoundingClientRect(); return r.left >= innerWidth - 1; })()";
  check(await evaluate(hidden), "cinema: the drawer is away");
  check(await evaluate("document.querySelector('.strip-host').getBoundingClientRect().width === innerWidth"), "cinema: the canvas fills the frame");
  check(await evaluate("getComputedStyle(document.querySelector('.strip-console')).display === 'none' && getComputedStyle(document.querySelector('.view-tabs')).display === 'none'"), "cinema: the console and the view tabs are put away");
  check(await evaluate("['.topbar', '[data-testid=strip-legend]', '[data-testid=strip-caption]'].every((s) => { const r = document.querySelector(s)?.getBoundingClientRect(); return r && r.width > 0 && r.height > 0; })"), "cinema: the top bar, the legend and the caption stay");
  await evaluate("window.__eddieStrip.tourSeek(20.8)");
  await until(`${caption}?.dataset.shot === 'morph' && window.__eddieStrip.settled()`, "a tour frame in the capture layout");
  await sleep(150);
  await shot("19-cinema-wave");
  await open("cinema");
  await ready();
  await evaluate("window.__eddieStrip.tourPlay(38.8)");
  await until(`document.querySelector('.app').classList.contains('drawer-open') && document.querySelector('[data-testid=chapter-guide]')?.dataset.slug === ${JSON.stringify(last)}`, "cinema: the drawer to open on the last chapter when the tour ends", 30000);
  await sleep(900);
  check(!(await evaluate(hidden)), "cinema: the drawer slid in over the canvas");
  check(await evaluate("document.querySelector('.strip-host').getBoundingClientRect().width === innerWidth"), "cinema: the canvas kept its size");
  await shot("20-cinema-summit");

  // 6) live cross-fades. Every screenshot above is a frozen frame, where state is assigned
  //    directly; on a running clock the surface has to travel to the dead population by itself.
  await open("mode=wave");
  await ready();
  check(await click("[data-testid=pop-dead]"), "clicked Dead code on a running page");
  await until("window.__eddieStrip.state().popLevel === 1 && window.__eddieStrip.settled()", "the surface to reach the dead population");
  check((await evaluate("window.__eddieStrip.state().popLevel")) === 1, "the live cross-fade to the dead wave completes");

  // 8) the voice-over, on a running clock (a frozen page never starts it): one live case per control
  await open("");
  await ready();
  const nar = "window.__eddieNarration?.state()";
  check(await evaluate("!!document.querySelector('[data-testid=tour-sound]')"), "the voice switch sits beside Play Visual Tour");
  check(await click("[data-testid=tour-play]"), "started the tour with the voice on");
  await until(`${nar}?.playing === 0 && ${nar}?.source === 'file'`, "the first shot's clip to play from its file", 30000);
  check(await click("[data-testid=tour-sound]"), "muted the voice mid-shot");
  await until(`${nar}?.muted === true && ${nar}?.playing === 0`, "the muted clip to keep running in silence");
  check(await click("[data-testid=tour-sound]"), "turned the voice back on");
  await until(`${nar}?.muted === false && ${nar}?.playing === 0`, "the voice to rejoin the same clip in place");
  await press("Escape", 27);
  await until(`${nar}?.playing === null`, "Escape to stop the narration");
  check(await click('[data-testid=shot-pill][data-shot="morph"]'), "clicked pill 3");
  await until(`${nar}?.playing === 2`, "pill 3 to tell its own shot");
  await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: 900, y: 620, button: "none" });
  await send("Input.dispatchMouseEvent", { type: "mousePressed", x: 900, y: 620, button: "left", clickCount: 1 });
  for (let k = 1; k <= 6; k++) await send("Input.dispatchMouseEvent", { type: "mouseMoved", x: 900 + 25 * k, y: 620 + 6 * k, button: "left", buttons: 1 });
  await send("Input.dispatchMouseEvent", { type: "mouseReleased", x: 1050, y: 656, button: "left", clickCount: 1 });
  await until(`${nar}?.playing === null`, "a camera drag to stop the narration");
  const manifest = await evaluate("fetch('/audio/tour/narration.json').then((r) => r.json())");
  const cueList = (await evaluate("window.__eddieStrip.tour()")).cues;
  const windows = manifest.shots.map((clip) => {
    const first = cueList.findIndex((c) => c.shot === clip.id);
    const nextShot = cueList.findIndex((c, i) => i > first && c.shot !== clip.id);
    return (nextShot < 0 ? tour.duration : cueList[nextShot].from) - cueList[first].from;
  });
  check(manifest.shots.every((clip, i) => clip.duration_s <= windows[i] - manifest.margin_s),
    `every clip ends inside its shot (${manifest.shots.map((c, i) => `${c.duration_s.toFixed(1)}/${windows[i].toFixed(1)} s`).join(", ")}; voice ${manifest.voice})`);
  await open("cinema");
  await ready();
  check(await evaluate("!document.querySelector('[data-testid=tour-sound]')"), "cinema: no voice switch in the frame");
  check(await click("[data-testid=tour-play]"), "cinema: started the tour");
  await until("window.__eddieStrip.tour().playing && window.__eddieStrip.tour().t > 1.5", "cinema: the tour's clock to run");
  check(await evaluate(`(${nar}?.playing ?? null) === null`), "cinema: the tour plays in silence (capture scripts hear nothing)");
  await press("Escape", 27);

  // 9) the film card and its lightbox
  const film = await evaluate("fetch('/api/demo-video/meta').then((r) => r.json())");
  await open("");
  await ready();
  if (film.available) {
    await until("!!document.querySelector('[data-testid=film-card], [data-testid=film-pill]')", "the film card");
    if (await evaluate("!!document.querySelector('[data-testid=film-pill] .film-pill-expand')")) await click("[data-testid=film-pill] .film-pill-expand");
    const overlap = await evaluate(`(() => { const c = document.querySelector('[data-testid=film-card]')?.getBoundingClientRect(); if (!c) return 'no card'; return ['.strip-shots', '.strip-legend', '.strip-caption', '.strip-console'].filter((s) => { const r = document.querySelector(s)?.getBoundingClientRect(); return r && r.width > 0 && !(c.right <= r.left || r.right <= c.left || c.bottom <= r.top || r.bottom <= c.top); }).join(', '); })()`);
    check(overlap === "", `the film card collides with nothing${overlap ? ` (it overlaps ${overlap})` : ""}`);
    await shot("21-film-card");
    check(await click("[data-testid=tour-play]"), "started the tour, then opened the film over it");
    await until(`window.__eddieStrip.tour().playing && ${nar}?.playing === 0`, "the tour and its voice to run");
    check(await click("[data-testid=film-card] .film-open"), "opened the film");
    await until("!!document.querySelector('dialog[open][data-testid=film-modal]') && !!document.querySelector('[data-testid=film-video], [data-testid=film-iframe]')", "the lightbox and its player");
    check(await evaluate(`window.__eddieStrip.tour().t === null && (${nar}?.playing ?? null) === null`), "opening the film stops the tour and the voice");
    if (film.mode === "local") {
      await until("(() => { const v = document.querySelector('[data-testid=film-video]'); return !!v && v.readyState >= 1 && v.duration > 60; })()", "the film's metadata (served with byte ranges)");
    }
    await sleep(400);
    await shot("22-film-lightbox");
    await press("Escape", 27);
    await until("!document.querySelector('dialog[open]') && !document.querySelector('[data-testid=film-video], [data-testid=film-iframe]')", "Escape to close the film and stop its sound");
    check(await click("[data-testid=film-card] .film-tuck"), "tucked the card away");
    await until("!!document.querySelector('[data-testid=film-pill]')", "the pill");
    check(await click("[data-testid=film-pill] .film-pill-play"), "the pill opens the film");
    await until("!!document.querySelector('dialog[open]')", "the lightbox from the pill");
    check(await click(".film-close"), "closed it with the x");
    await until("!document.querySelector('dialog[open]')", "the x to close the film");
    check(await click("[data-testid=film-pill] .film-pill-expand"), "brought the card back");
    await open("cinema");
    await ready();
    await sleep(500);
    check(await evaluate("!document.querySelector('[data-testid=film-card], [data-testid=film-pill], [data-testid=film-modal]')"), "cinema: no film card, pill or lightbox in the frame");
  } else {
    await sleep(600);
    check(await evaluate("!document.querySelector('[data-testid=film-card], [data-testid=film-pill]')"), "no film yet: the card stays hidden");
  }

  // 7) frame pacing (software GL here: an upper bound on trouble, not a prediction)
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
