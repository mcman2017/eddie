import { useCallback, useEffect, useRef, useState, type MouseEvent } from "react";
import type { Witness } from "./api";
import { STATUS_FILL } from "./theme";

const fmt = (t: number) => {
  const m = Math.floor(t / 60);
  const s = t - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, "0")}`;
};

interface Props {
  audio: Witness;
  spectrogram: Witness | null;
}

/** A sonified witness: waveform and spectrogram share one playhead; click either to seek. */
export function AudioWitness({ audio, spectrogram }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const headRef = useRef<HTMLDivElement>(null);
  const timeRef = useRef<HTMLSpanElement>(null);
  const bufferRef = useRef<AudioBuffer | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const srcRef = useRef<AudioBufferSourceNode | null>(null);
  const startRef = useRef(0);
  const offsetRef = useRef(0);
  const rafRef = useRef(0);
  const [duration, setDuration] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const position = useCallback(() => {
    const ctx = ctxRef.current;
    return srcRef.current && ctx ? ctx.currentTime - startRef.current : offsetRef.current;
  }, []);

  const paint = useCallback((t: number) => {
    const canvas = canvasRef.current;
    const buffer = bufferRef.current;
    if (!canvas || !buffer) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (canvas.width !== Math.round(w * dpr)) canvas.width = Math.round(w * dpr);
    if (canvas.height !== Math.round(h * dpr)) canvas.height = Math.round(h * dpr);
    const g = canvas.getContext("2d");
    if (!g) return;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);
    const data = buffer.getChannelData(0);
    const per = Math.max(1, Math.floor(data.length / w));
    const playedX = (t / buffer.duration) * w;
    const mid = h / 2;
    for (let x = 0; x < w; x++) {
      let lo = 1;
      let hi = -1;
      const start = x * per;
      for (let i = start; i < start + per && i < data.length; i++) {
        const v = data[i];
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
      if (hi < lo) continue;
      g.fillStyle = x <= playedX ? STATUS_FILL.verified : STATUS_FILL.triaged;
      g.fillRect(x, mid - hi * mid * 0.92, 1, Math.max(1, (hi - lo) * mid * 0.92));
    }
    const pct = `${Math.min(100, (t / buffer.duration) * 100)}%`;
    if (headRef.current) headRef.current.style.left = pct;
    if (timeRef.current) timeRef.current.textContent = `${fmt(t)} / ${fmt(buffer.duration)}`;
  }, []);

  useEffect(() => {
    let live = true;
    setError(null);
    fetch(audio.url)
      .then((r) => {
        if (!r.ok) throw new Error(`audio: HTTP ${r.status}`);
        return r.arrayBuffer();
      })
      .then((bytes) => new OfflineAudioContext(1, 2, 44_100).decodeAudioData(bytes))
      .then((buffer) => {
        if (!live) return;
        bufferRef.current = buffer;
        offsetRef.current = 0;
        setDuration(buffer.duration);
        paint(0);
      })
      .catch((e: Error) => live && setError(e.message));
    return () => {
      live = false;
    };
  }, [audio.url, paint]);

  const stopSource = useCallback(() => {
    const src = srcRef.current;
    srcRef.current = null;
    if (src) {
      src.onended = null;
      try {
        src.stop();
      } catch {
        /* already stopped */
      }
    }
    cancelAnimationFrame(rafRef.current);
  }, []);

  useEffect(() => {
    const onResize = () => paint(position());
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      stopSource();
      void ctxRef.current?.close();
      ctxRef.current = null;
    };
  }, [paint, position, stopSource]);

  const play = useCallback(
    (from: number) => {
      const buffer = bufferRef.current;
      if (!buffer) return;
      const ctx = ctxRef.current ?? (ctxRef.current = new AudioContext());
      void ctx.resume();
      stopSource();
      const src = ctx.createBufferSource();
      src.buffer = buffer;
      src.connect(ctx.destination);
      src.onended = () => {
        if (srcRef.current !== src) return;
        srcRef.current = null;
        offsetRef.current = 0;
        setPlaying(false);
        paint(0);
      };
      const at = Math.min(Math.max(from, 0), buffer.duration - 1e-3);
      src.start(0, at);
      srcRef.current = src;
      startRef.current = ctx.currentTime - at;
      setPlaying(true);
      const tick = () => {
        if (srcRef.current !== src) return;
        paint(ctx.currentTime - startRef.current);
        rafRef.current = requestAnimationFrame(tick);
      };
      tick();
    },
    [paint, stopSource],
  );

  const pause = () => {
    offsetRef.current = position();
    stopSource();
    setPlaying(false);
    paint(offsetRef.current);
  };

  const seek = (e: MouseEvent<HTMLElement>) => {
    const buffer = bufferRef.current;
    if (!buffer) return;
    const box = e.currentTarget.getBoundingClientRect();
    const t = ((e.clientX - box.left) / box.width) * buffer.duration;
    if (srcRef.current) play(t);
    else {
      offsetRef.current = t;
      paint(t);
    }
  };

  const fmax = Number(spectrogram?.qc?.["f_max_hz"] ?? 0);

  return (
    <div className="audio">
      <div className="audio-controls">
        <button type="button" className="play" onClick={() => (playing ? pause() : play(offsetRef.current))} disabled={!duration}>
          {playing ? "Pause" : "Play"}
        </button>
        <span className="audio-time" ref={timeRef}>
          {duration ? `${fmt(0)} / ${fmt(duration)}` : "loading…"}
        </span>
        <span className="audio-title">{audio.title}</span>
      </div>
      <div className="audio-track">
        <canvas ref={canvasRef} className="waveform" onClick={seek} aria-label="Waveform: click to seek" />
        {spectrogram && (
          <div className="spectrogram" onClick={seek}>
            <img src={spectrogram.url} alt={spectrogram.title ?? "Spectrogram of the witness audio"} draggable={false} />
            {fmax > 0 && (
              <>
                <span className="axis axis-top">{fmax.toLocaleString()} Hz</span>
                <span className="axis axis-bottom">0 Hz</span>
              </>
            )}
          </div>
        )}
        <div className="playhead" ref={headRef} aria-hidden="true" />
      </div>
      <div className="audio-axis">
        <span>0 s</span>
        <span>{duration ? `${duration.toFixed(1)} s` : ""}</span>
      </div>
      {error && <p className="note-error">{error}</p>}
    </div>
  );
}
