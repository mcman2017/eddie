import { useEffect, useRef, useState } from "react";
import { ApiError } from "../api";
import { routeNumber, routeText, writeRoute } from "../route";
import { RAMP_WAVE, RAMP_ZETA, actName } from "../theme";
import { topPeaks } from "./field";
import { HarmonicSynth, pitchOf, type Partial as Tone } from "./HarmonicSynth";
import { loadLandscape, type Landscape } from "./landscape";
import { rampGradient } from "./ramp";
import { SpectrumPanel } from "./SpectrumPanel";
import { StripEngine, type Population, type Preset, type Probe } from "./StripEngine";
import "./strip.css";

interface Props {
  active: boolean;
  chapter: string | null;
  onChapter: (slug: string) => void;
}

type Status = "loading" | "ready" | "missing" | "error" | "lost";

const PRESETS: [Preset, string][] = [
  ["overview", "Overview"],
  ["dive", "Critical line dive"],
  ["finale", "Act 3 → Main"],
];

const TIERS = ["foundations", "machinery", "bottleneck", "main"] as const;
// each ramp from its first stop to its last: below the first stop the colour does not change
const ZETA_KEY = rampGradient(RAMP_ZETA, RAMP_ZETA[0][0], 1);
const WAVE_KEY = rampGradient(RAMP_WAVE, RAMP_WAVE[0][0], 1);

const short = (title: string) => title.split(" · ").pop() ?? title;

/** The hero view: the zeta terrain and the proof wave on one strip, with the chapters as beacons. */
export default function StripView({ active, chapter, onChapter }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const labelsRef = useRef<HTMLDivElement>(null);
  const engineRef = useRef<StripEngine | null>(null);
  const synthRef = useRef<HarmonicSynth | null>(null);
  const pick = useRef(onChapter);
  pick.current = onChapter;
  const flown = useRef(false);
  const [status, setStatus] = useState<Status>("loading");
  const [message, setMessage] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [land, setLand] = useState<Landscape | null>(null);
  const [blend, setBlend] = useState(() => (routeText("mode") === "wave" ? 1 : 0));
  const [bands, setBands] = useState<number | null>(() => routeNumber("bands"));
  const [population, setPopulation] = useState<Population>(() => {
    const p = routeText("pop");
    return p === "dead" || p === "compare" ? p : "live";
  });
  const [orbit, setOrbit] = useState(false);
  const [touring, setTouring] = useState(false);
  const [hover, setHover] = useState<number | null>(null);
  const [probe, setProbe] = useState<Probe | null>(null);
  const [card, setCard] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [hud, setHud] = useState<string | null>(null);
  const wave = blend >= 0.5;

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
            onTour: setTouring,
            onContext: (state) => setStatus(state === "lost" ? "lost" : "ready"),
          },
          { freeze: routeNumber("freeze") },
        );
        engineRef.current = engine;
        const box = host.getBoundingClientRect();
        engine.resize(box.width, box.height, window.devicePixelRatio || 1);
        engine.setMorph(routeText("mode") === "wave" ? 1 : 0, false);
        const cam = routeText("cam");
        if (cam === "dive" || cam === "finale") engine.flyToPreset(cam, true);
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
    const observer = new ResizeObserver(([entry]) => {
      engineRef.current?.resize(entry.contentRect.width, entry.contentRect.height, window.devicePixelRatio || 1);
    });
    observer.observe(host);
    return () => observer.disconnect();
  }, [ready]);

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
    if (chapter) engine.flyToChapter(chapter, !flown.current && routeText("chapter") === chapter);
    flown.current = true;
  }, [chapter, ready]);

  const goMode = (toWave: boolean) => {
    setBlend(toWave ? 1 : 0);
    engineRef.current?.setMorph(toWave ? 1 : 0, true);
    writeRoute({ mode: toWave ? "wave" : null });
  };

  const goPreset = (name: Preset) => {
    engineRef.current?.flyToPreset(name);
    writeRoute({ cam: name === "overview" ? null : name });
  };

  const toggleTour = () => {
    const engine = engineRef.current;
    if (!engine) return;
    if (touring) engine.stopTour();
    else engine.startTour();
  };

  const play = () => {
    const engine = engineRef.current;
    if (!engine || !land) return;
    const synth = synthRef.current ?? (synthRef.current = new HarmonicSynth());
    if (synth.playing) {
      synth.stop();
      setPlaying(false);
      setHud(null);
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
      const preset = PRESETS[Number(e.key) - 1];
      if (preset) goPreset(preset[0]);
      else if (e.key === "t" || e.key === "T") toggleTour();
      else if (e.key === "o" || e.key === "O") setOrbit((v) => !v);
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }); // eslint-disable-line react-hooks/exhaustive-deps

  const chapters = land?.meta.chapters ?? [];
  const zeros = land?.meta.zeta.zeros ?? [];
  const proof = land?.meta.proof;
  const count = bands ?? land?.bands ?? 8;
  const shown = population === "dead" ? proof?.dead : proof?.live;
  const waveZeros = shown?.zeros_by_band[Math.max(count, 1) - 1]?.length ?? 0;

  return (
    <section className="strip" aria-label="3D critical strip and proof wave">
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

          <p className="strip-caption">
            {wave ? (
              <>
                The proof's own wave: a Dirichlet series over {proof?.classes.toLocaleString()} structural classes of its{" "}
                {land.meta.counts.live.toLocaleString()} live and {land.meta.counts.dead.toLocaleString()} dead declarations. It has{" "}
                {waveZeros} {waveZeros === 1 ? "zero" : "zeros"} here, scattered across the strip.
              </>
            ) : (
              <>
                log|ζ(σ + it)| on the critical strip, computed with mpmath. Each well is a zero: all {zeros.length} up to t ={" "}
                {land.tMax} lie on σ = 1/2. The {chapters.length} beacons are the proof's chapters, laid out along its flow.
              </>
            )}
            {land.meta.stale && " The landscape is older than the proof cone: run make landscape."}
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

          <div className="strip-cams" role="group" aria-label="Camera">
            {PRESETS.map(([name, label], i) => (
              <button key={name} type="button" onClick={() => goPreset(name)} title={`Key ${i + 1}`}>
                {label}
              </button>
            ))}
            <button type="button" aria-pressed={touring} onClick={toggleTour} title="Key T">
              {touring ? "Stop tour" : "Tour"}
            </button>
            <button type="button" aria-pressed={orbit} onClick={() => setOrbit((v) => !v)} title="Key O">
              Auto-orbit
            </button>
          </div>

          <div className="strip-console">
            <div className="strip-mode" role="group" aria-label="Surface">
              <button type="button" data-testid="mode-strip" aria-pressed={!wave} onClick={() => goMode(false)}>
                Riemann critical strip
              </button>
              <input type="range" data-testid="blend" min={0} max={100} value={Math.round(blend * 100)} aria-label="Blend between the zeta strip and the proof wave"
                onChange={(e) => {
                  const v = Number(e.target.value) / 100;
                  setBlend(v);
                  engineRef.current?.setMorph(v, false);
                }} />
              <button type="button" data-testid="mode-wave" aria-pressed={wave} onClick={() => goMode(true)}>
                Gödel-Fourier wave
              </button>
            </div>
            {wave && (
              <div className="strip-wave">
                <label className="strip-bands">
                  <span>
                    Harmonics 1 to {count} of {land.bands}
                  </span>
                  <input type="range" data-testid="bands" min={1} max={land.bands} value={count} onChange={(e) => setBands(Number(e.target.value))}
                    aria-label="Number of harmonic bands summed, from the foundations up to Main" />
                </label>
                <div className="segmented" role="group" aria-label="Which declarations weight the wave">
                  {([["live", `Live cone (${Math.round(land.meta.counts.live / 1000)}K)`], ["dead", `Dead code (${Math.round(land.meta.counts.dead / 1000)}K)`], ["compare", "Compare"]] as const).map(
                    ([key, label]) => (
                      <button key={key} type="button" data-testid={`pop-${key}`} aria-pressed={population === key} aria-selected={population === key}
                        onClick={() => setPopulation(key)}>
                        {label}
                      </button>
                    ),
                  )}
                </div>
              </div>
            )}
            <div className="strip-actions">
              <button type="button" className="play" data-testid="harmonics-play" onClick={play} aria-pressed={playing}>
                {playing ? "Stop" : "Play spectral harmonics"}
              </button>
              <button type="button" className="strip-toggle" aria-expanded={card} onClick={() => setCard((v) => !v)}>
                Findings and spectrum
              </button>
            </div>
            <p className="strip-hud" role="status">
              {hud ??
                (population === "compare" && wave
                  ? "Compare: the shaded surface is the live cone, the grey wireframe is dead code, and the glow marks where they differ most."
                  : wave
                    ? "Sound: the 12 tallest peaks on σ = 1/2, mapped to 160 + 15·t Hz."
                    : "Sound: the first 12 zeros, mapped to 160 + 15·t Hz and weighted by 1/|ρ|.")}
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
