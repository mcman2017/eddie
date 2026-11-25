import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api";
import { routeNumber, routeText, writeRoute } from "../route";
import { RAMP_WAVE, RAMP_ZETA, actName } from "../theme";
import { topPeaks } from "./field";
import { HarmonicSynth, pitchOf, type Partial as Tone } from "./HarmonicSynth";
import { loadLandscape, type Landscape } from "./landscape";
import { rampGradient } from "./ramp";
import { SpectrumPanel } from "./SpectrumPanel";
import { StripEngine, type Population, type Probe, type TourEvent } from "./StripEngine";
import { CUES, SHOTS, type SceneState, type ShotId, type TourFacts } from "./tour";
import "./strip.css";

interface Props {
  active: boolean;
  chapter: string | null;
  onChapter: (slug: string) => void;
  /** the capture layout: controls put away, and the canvas drawn at the display's own resolution */
  cinema: boolean;
  /** told when the tour starts and stops, so the app can put the drawer away for the take */
  onTouring: (running: boolean) => void;
}

type Status = "loading" | "ready" | "missing" | "error" | "lost";

const TIERS = ["foundations", "machinery", "bottleneck", "main"] as const;
// each ramp from its first stop to its last: below the first stop the colour does not change
const ZETA_KEY = rampGradient(RAMP_ZETA, RAMP_ZETA[0][0], 1);
const WAVE_KEY = rampGradient(RAMP_WAVE, RAMP_WAVE[0][0], 1);

const short = (title: string) => title.split(" · ").pop() ?? title;

/** The caption a shot carries when its pill is used on its own: the last one the tour shows there. */
function captionOf(id: ShotId): number {
  let cue = 0;
  CUES.forEach((c, i) => {
    if (c.shot === id) cue = i;
  });
  return cue;
}

/** #shot=3 or #shot=morph */
function shotFromRoute(): (typeof SHOTS)[number] | null {
  const raw = routeText("shot");
  if (!raw) return null;
  return SHOTS[Number(raw) - 1] ?? SHOTS.find((s) => s.id === raw) ?? null;
}

/** The hero view: the zeta terrain and the proof wave on one strip, with the chapters as beacons. */
export default function StripView({ active, chapter, onChapter, cinema, onTouring }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const labelsRef = useRef<HTMLDivElement>(null);
  const engineRef = useRef<StripEngine | null>(null);
  const synthRef = useRef<HarmonicSynth | null>(null);
  const pick = useRef(onChapter);
  pick.current = onChapter;
  const flown = useRef(false);
  const crisp = useRef(cinema);
  crisp.current = cinema;
  const [status, setStatus] = useState<Status>("loading");
  const [message, setMessage] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [land, setLand] = useState<Landscape | null>(null);
  const [blend, setBlend] = useState(() => {
    const s = shotFromRoute();
    return (s ? s.scene.wave : routeText("mode") === "wave") ? 1 : 0;
  });
  const [bands, setBands] = useState<number | null>(() => routeNumber("bands"));
  const [population, setPopulation] = useState<Population>(() => {
    const p = shotFromRoute()?.scene.population ?? routeText("pop");
    return p === "dead" || p === "compare" ? p : "live";
  });
  const [orbit, setOrbit] = useState(false);
  const [touring, setTouring] = useState(false);
  const [shot, setShot] = useState<ShotId | null>(() => shotFromRoute()?.id ?? null);
  const [cue, setCue] = useState<number | null>(() => {
    const s = shotFromRoute();
    return s ? captionOf(s.id) : null;
  });
  const [hover, setHover] = useState<number | null>(null);
  const [probe, setProbe] = useState<Probe | null>(null);
  const [card, setCard] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [hud, setHud] = useState<string | null>(null);
  const wave = blend >= 0.5;

  const stopSound = () => {
    synthRef.current?.stop();
    setPlaying(false);
    setHud(null);
  };

  // The tour reports about ten times in forty seconds; the controls follow it from here.
  const onTour = useRef<(e: TourEvent) => void>(() => undefined);
  onTour.current = (e) => {
    setBlend(e.scene.wave ? 1 : 0);
    setPopulation(e.scene.population);
    if (e.phase === "cancel") {
      setTouring(false);
      setShot(null);
      setCue(null);
    } else {
      setTouring(e.playing);
      setShot(e.shot);
      setCue(e.cue);
    }
    if (e.phase === "start") {
      stopSound();
      setOrbit(false);
      setCard(false);
      onTouring(true);
    }
    if (e.phase === "end" || e.phase === "cancel") {
      writeRoute({
        mode: e.scene.wave ? "wave" : null, pop: e.scene.population === "live" ? null : e.scene.population,
        shot: e.phase === "end" ? SHOTS.length : null, cam: null, tour: null,
      });
      onTouring(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    let engine: StripEngine | null = null;
    setStatus("loading");
    loadLandscape()
      .then((l) => {
        const host = hostRef.current;
        if (cancelled || !host) return;
        engine = new StripEngine(
          host,
          l,
          {
            onPick: (slug) => pick.current(slug),
            onHover: setHover,
            onProbe: setProbe,
            onTour: (e) => onTour.current(e),
            onContext: (state) => setStatus(state === "lost" ? "lost" : "ready"),
          },
          { freeze: routeNumber("freeze") },
        );
        engineRef.current = engine;
        const box = host.getBoundingClientRect();
        engine.resize(box.width, box.height, window.devicePixelRatio || 1, crisp.current);
        // where the link points: a frame of the tour, one of its shots, or a mode and an older preset
        const tourAt = routeNumber("tour");
        const first = shotFromRoute();
        if (tourAt != null) engine.seekTour(tourAt);
        else if (first) {
          engine.setMorph(first.scene.wave ? 1 : 0, false);
          engine.flyToShot(first.id, true);
        } else {
          engine.setMorph(routeText("mode") === "wave" ? 1 : 0, false);
          const cam = routeText("cam");
          if (cam === "dive" || cam === "finale") engine.flyToPreset(cam, true);
        }
        setLand(l);
        setStatus("ready");
      })
      .catch((e: Error) => {
        if (cancelled) return;
        setMessage(e.message);
        setStatus(e instanceof ApiError && e.status === 503 ? "missing" : "error");
      });
    return () => {
      cancelled = true;
      engine?.dispose();
      engineRef.current = null;
      synthRef.current?.dispose();
      synthRef.current = null;
    };
  }, [attempt]);

  const ready = status === "ready" && land != null;

  useEffect(() => {
    const host = hostRef.current;
    if (!host || !ready) return;
    const fit = () => {
      const box = host.getBoundingClientRect();
      engineRef.current?.resize(box.width, box.height, window.devicePixelRatio || 1, cinema);
    };
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(host);
    return () => observer.disconnect();
  }, [ready, cinema]);

  useEffect(() => {
    if (ready && labelsRef.current) engineRef.current?.attachLabels(labelsRef.current);
  }, [ready, land]);

  useEffect(() => {
    if (ready) engineRef.current?.setActive(active);
  }, [active, ready]);

  useEffect(() => {
    if (ready && land) engineRef.current?.setBandCount(bands ?? land.bands);
  }, [bands, ready, land]);

  useEffect(() => {
    if (ready) engineRef.current?.setPopulation(population);
  }, [population, ready]);

  useEffect(() => {
    if (ready) engineRef.current?.setOrbit(orbit);
  }, [orbit, ready]);

  useEffect(() => {
    const engine = engineRef.current;
    if (!ready || !engine) return;
    engine.setSelected(chapter);
    // a link that names a shot or a tour frame keeps that framing; otherwise go to the chapter
    const framed = !flown.current && (routeNumber("tour") != null || shotFromRoute() != null);
    if (chapter && !framed) engine.flyToChapter(chapter, !flown.current && routeText("chapter") === chapter);
    flown.current = true;
  }, [chapter, ready]);

  const chapters = land?.meta.chapters ?? [];
  const zeros = land?.meta.zeta.zeros ?? [];
  const proof = land?.meta.proof;
  const mainSlug = chapters.find((c) => c.tier === "main")?.slug ?? null;

  /** A change the user makes by hand ends the tour and drops the shot's caption. */
  const byHand = () => {
    engineRef.current?.stopTour();
    setShot(null);
    setCue(null);
    writeRoute({ shot: null, tour: null });
  };

  const applyScene = (scene: SceneState, animate: boolean) => {
    setBlend(scene.wave ? 1 : 0);
    engineRef.current?.setMorph(scene.wave ? 1 : 0, animate);
    setPopulation(scene.population);
    writeRoute({ mode: scene.wave ? "wave" : null, pop: scene.population === "live" ? null : scene.population });
  };

  const goMode = (toWave: boolean) => {
    byHand();
    applyScene({ wave: toWave, population }, true);
  };

  const goPopulation = (next: Population) => {
    byHand();
    setPopulation(next);
    writeRoute({ pop: next === "live" ? null : next });
  };

  const goShot = (id: ShotId) => {
    const engine = engineRef.current;
    const at = SHOTS.findIndex((s) => s.id === id);
    if (!engine || at < 0) return;
    // the summit is chapter Main's own framing: opening that chapter flies there
    if (id === "summit" && mainSlug && chapter !== mainSlug) {
      engine.stopTour();
      onChapter(mainSlug);
    } else engine.flyToShot(id);
    applyScene(SHOTS[at].scene, true);
    setShot(id);
    setCue(captionOf(id));
    writeRoute({ shot: at + 1, cam: null, tour: null });
  };

  const goOverview = () => {
    engineRef.current?.flyToPreset("overview");
    setShot(null);
    setCue(null);
    writeRoute({ shot: null, cam: null, tour: null });
  };

  const toggleTour = () => {
    const engine = engineRef.current;
    if (!engine) return;
    if (touring) engine.stopTour();
    else engine.playTour();
  };

  const play = () => {
    const engine = engineRef.current;
    if (!engine || !land) return;
    const synth = synthRef.current ?? (synthRef.current = new HarmonicSynth());
    if (synth.playing) {
      stopSound();
      return;
    }
    const tones: Tone[] = wave
      ? topPeaks(engine.profile(), land.tMax, 12).map((p, index) => ({ t: p.t, weight: p.r, index }))
      : land.meta.zeta.zeros.slice(0, 12).map((z, index) => ({ t: z.t, weight: 1 / Math.hypot(0.5, z.t), index }));
    synth.play(
      tones,
      (p) => {
        if (!wave) engine.pulse("zeta", p.index);
        setHud(`${wave ? "Peak" : "Zero"} ${p.index + 1} of ${tones.length}: t = ${p.t.toFixed(4)}, sounding at ${Math.round(pitchOf(p.t))} Hz`);
      },
      () => {
        setPlaying(false);
        setHud(null);
      },
    );
    setPlaying(true);
  };

  useEffect(() => {
    if (!active || !ready) return;
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (e.metaKey || e.ctrlKey || e.altKey || (el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;
      const pill = SHOTS[Number(e.key) - 1];
      if (pill) goShot(pill.id);
      else if (e.key === "0") goOverview();
      else if (e.key === "t" || e.key === "T") toggleTour();
      else if (e.key === "o" || e.key === "O") setOrbit((v) => !v);
      else if (e.key === "Escape" && touring) engineRef.current?.stopTour();
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }); // eslint-disable-line react-hooks/exhaustive-deps

  const count = bands ?? land?.bands ?? 8;
  const shown = population === "dead" ? proof?.dead : proof?.live;
  const waveZeros = shown?.zeros_by_band[Math.max(count, 1) - 1]?.length ?? 0;
  const gate = land?.meta.gate;
  const facts: TourFacts = {
    chapters: chapters.length, live: land?.meta.counts.live ?? 0, dead: land?.meta.counts.dead ?? 0,
    gate: gate ? { name: gate.full_name.split(".").pop() ?? gate.full_name, share: gate.share } : null,
  };

  return (
    <section className={`strip${touring ? " is-touring" : ""}${cinema ? " is-cinema" : ""}`} aria-label="3D critical strip and proof wave">
      <div className="strip-host" ref={hostRef} />
      {ready && land && (
        <>
          <div className="strip-labels" ref={labelsRef}>
            {chapters.map((c, i) => (
              <button key={c.slug} type="button" className={`beacon-label tier-${c.tier}`} data-beacon={i} data-testid="beacon"
                data-chapter={c.chapter} onClick={() => onChapter(c.slug)}
                aria-label={`Chapter ${c.chapter}: ${short(c.title)} (${actName(c.tier)})`}>
                <b>{c.chapter}</b>
                <span className="beacon-title">{short(c.title)}</span>
              </button>
            ))}
            {zeros.map((z, i) => (
              <span key={z.t} className="zero-label" data-zero={i} aria-hidden="true">
                Zero #{i + 1}: t = {z.t.toFixed(2)}
              </span>
            ))}
          </div>

          {/* one line that says what is on screen: a shot's caption, or else the surface's own */}
          <p className={`strip-caption${cue != null ? " is-shot" : ""}`} data-testid="strip-caption" data-shot={cue != null ? CUES[cue].shot : ""}
            data-cue={cue ?? ""} role="status">
            {cue != null ? (
              <strong>{CUES[cue].text(facts)}</strong>
            ) : wave ? (
              <>
                <strong>The proof's own shape: a million declarations added up as one wave.</strong>
                <span>
                  {proof?.classes.toLocaleString()} structural classes of its {land.meta.counts.live.toLocaleString()} live and{" "}
                  {land.meta.counts.dead.toLocaleString()} dead declarations, summed as a Dirichlet series (the Gödel-Fourier wave). It
                  pictures the code, not ζ: its {waveZeros} {waveZeros === 1 ? "zero moves" : "zeros move"} with the numbering.
                </span>
              </>
            ) : (
              <>
                <strong>Riemann's 1859 bet: EVERY non-trivial zero valley sits dead-center on σ = 1/2.</strong>
                <span>
                  Height is the size of ζ(σ + it) on a log scale, computed with mpmath. The {zeros.length} zeros below t = {land.tMax} are
                  ringed, and the numbered pins are the proof's {chapters.length} chapters.
                </span>
              </>
            )}
            {land.meta.stale && cue == null && <span> The landscape is older than the proof cone: run make landscape.</span>}
          </p>

          <div className="strip-legend" data-testid="strip-legend">
            <div className="legend-ramp" style={{ background: wave ? WAVE_KEY : ZETA_KEY }} />
            <div className="legend-ends">
              <span>{wave ? "quiet" : "ζ near 0"}</span>
              <span>{wave ? "the wave's height" : "the size of ζ"}</span>
              <span>{wave ? "loud" : "large"}</span>
            </div>
            <ul className="legend-acts">
              {TIERS.map((tier) => (
                <li key={tier} className={`tier-${tier}`}>
                  <i className="act-chip" />
                  {actName(tier)}
                </li>
              ))}
            </ul>
            <ul className="legend-keys">
              {!wave && (
                <li>
                  <i className="key-line" />
                  the critical line, σ = 1/2
                </li>
              )}
              <li>
                <i className="key-dead" />
                dead code: never used by the proof
              </li>
            </ul>
          </div>

          <div className="strip-shots" role="group" aria-label="Shots and tour">
            <button type="button" className="tour-play" data-testid="tour-play" aria-pressed={touring} onClick={toggleTour} title="Key T">
              {touring ? "Stop tour" : "Play Visual Tour"}
            </button>
            <ol className="shot-list">
              {SHOTS.map((s, i) => (
                <li key={s.id}>
                  <button type="button" data-testid="shot-pill" data-shot={s.id} aria-pressed={shot === s.id} onClick={() => goShot(s.id)}
                    title={`Key ${i + 1}`}>
                    {s.label}
                  </button>
                </li>
              ))}
            </ol>
            <div className="shot-extras">
              <button type="button" data-testid="cam-overview" onClick={goOverview} title="Key 0">
                Overview
              </button>
              <button type="button" data-testid="cam-orbit" aria-pressed={orbit} onClick={() => setOrbit((v) => !v)} title="Key O">
                Auto-orbit
              </button>
            </div>
          </div>

          <div className="strip-console">
            <div className="strip-mode" role="group" aria-label="Surface">
              <button type="button" data-testid="mode-strip" aria-pressed={!wave} onClick={() => goMode(false)}
                title="The landscape of the Riemann zeta function on its critical strip">
                Riemann critical strip
              </button>
              <input type="range" data-testid="blend" min={0} max={100} value={Math.round(blend * 100)} aria-label="Blend between the zeta strip and the proof wave"
                onChange={(e) => {
                  const v = Number(e.target.value) / 100;
                  byHand();
                  setBlend(v);
                  engineRef.current?.setMorph(v, false);
                  writeRoute({ mode: v >= 0.5 ? "wave" : null });
                }} />
              <button type="button" data-testid="mode-wave" aria-pressed={wave} onClick={() => goMode(true)}
                title="The Gödel-Fourier wave: the proof's own structure, summed as a wave">
                Proof wave
              </button>
            </div>
            {/* kept mounted through the tour, so the console does not change height mid-take */}
            {(wave || touring) && (
              <div className="strip-wave">
                <label className="strip-bands">
                  <span>
                    Build-up: layers 1 to {count} of {land.bands}
                  </span>
                  <input type="range" data-testid="bands" min={1} max={land.bands} value={count}
                    onChange={(e) => {
                      byHand();
                      setBands(Number(e.target.value));
                      writeRoute({ bands: Number(e.target.value) === land.bands ? null : Number(e.target.value) });
                    }}
                    aria-label="How many layers of the proof are summed into the wave, from the foundations up to Main" />
                </label>
                <div className="segmented" role="group" aria-label="Which declarations make the wave">
                  {([["live", `Live proof (${Math.round(land.meta.counts.live / 1000)}K)`], ["dead", `Dead code (${Math.round(land.meta.counts.dead / 1000)}K)`], ["compare", "Compare"]] as const).map(
                    ([key, label]) => (
                      <button key={key} type="button" data-testid={`pop-${key}`} aria-pressed={population === key} aria-selected={population === key}
                        onClick={() => goPopulation(key)}>
                        {label}
                      </button>
                    ),
                  )}
                </div>
              </div>
            )}
            <div className="strip-actions">
              <button type="button" className="play" data-testid="harmonics-play" onClick={play} aria-pressed={playing}>
                {playing ? "Stop" : "Play it as sound"}
              </button>
              <button type="button" className="strip-toggle" data-testid="findings-toggle" aria-expanded={card} onClick={() => setCard((v) => !v)}>
                What we found
              </button>
            </div>
            <p className="strip-hud" role="status">
              {hud ??
                (population === "compare" && wave
                  ? "Colour is the live proof. The grey grid is dead code. Outlines mark where the two differ most."
                  : population === "dead" && wave
                    ? "Grey is dead code: declarations the final theorem never uses. Its wave is drawn at its true height."
                    : wave
                      ? "Sound: the 12 tallest peaks on the line. Higher up the strip is higher in pitch (160 + 15·t Hz)."
                      : "Sound: one tone per zero. Higher up the line is higher in pitch (160 + 15·t Hz).")}
            </p>
          </div>

          {card && <SpectrumPanel land={land} onFly={(sigma, t) => engineRef.current?.flyToPoint(sigma, t)} />}

          {hover != null && chapters[hover] && (
            <div className="strip-hover" role="status">
              <strong>
                Chapter {chapters[hover].chapter}: {short(chapters[hover].title)}
              </strong>
              <span>
                {actName(chapters[hover].tier)}, {chapters[hover].live.toLocaleString()} live and {chapters[hover].dead.toLocaleString()} dead
                declarations
              </span>
            </div>
          )}
          {probe && (
            <div className="tooltip" style={{ left: probe.x, top: probe.y, transform: "translate(16px, 16px)" }} role="status">
              <strong>{probe.title}</strong>
              {probe.lines.map((l) => (
                <span key={l}>{l}</span>
              ))}
            </div>
          )}
        </>
      )}
      {status !== "ready" && (
        <div className="strip-empty">
          {status === "loading" && <p>Loading the landscape…</p>}
          {status === "missing" && (
            <p>
              The 3D strip has no landscape yet. Run <code>make landscape</code> (about 30 seconds, no model calls), then reload. The
              Dependency Wheel works without it.
            </p>
          )}
          {status === "error" && (
            <>
              <p>The 3D strip could not start: {message}</p>
              <button type="button" className="link" onClick={() => setAttempt((n) => n + 1)}>
                Try again
              </button>
            </>
          )}
          {status === "lost" && (
            <>
              <p>The graphics context was lost.</p>
              <button type="button" className="link" onClick={() => setAttempt((n) => n + 1)}>
                Reload the 3D view
              </button>
            </>
          )}
        </div>
      )}
    </section>
  );
}
