// The visual tour's voice-over: one pre-rendered clip per shot (web/public/audio/tour/, built
// by scripts/build_tour_narration.py), played on the tour's own clock.
//
// Every clip runs through its own gain (for clean fades) into one master gain (the mute). While
// a tour plays with narration allowed, clips always run, silently when muted, so unmuting
// rejoins mid-sentence and stays in sync. When the clips are missing, the same lines are spoken
// with the browser's speechSynthesis; there, mute cancels and unmute resumes at the next shot.
// The page decides when narration is allowed at all: never in #cinema (unless #narrate=1) or
// with ?mute=1, so capture scripts stay silent.

import source from "./narration.json";

interface Line {
  at: number;
  text: string;
}

export interface NarrationShot {
  id: string;
  file?: string;
  start: number;
  end: number;
  duration_s?: number;
  lines: Line[];
}

interface Manifest {
  voice?: string;
  shots: NarrationShot[];
}

export type NarrationSource = "file" | "speech" | null;

export interface NarrationState {
  /** index of the shot whose clip is playing (audible or not), or null */
  playing: number | null;
  muted: boolean;
  source: NarrationSource;
  /** seconds into the current clip */
  position: number | null;
  ready: boolean;
}

const RAMP_S = 0.03;
const FADE_S = 0.08;

export class TourNarrator {
  private ctx: AudioContext | null = null;
  private master: GainNode | null = null;
  private manifest: Manifest | null = null;
  private buffers: (AudioBuffer | null)[] = [];
  private loading: Promise<void> | null = null;
  private clip: { src: AudioBufferSourceNode; gain: GainNode; shot: number; startedAt: number } | null = null;
  private speech: { shot: number; timers: number[] } | null = null;
  private muted = false;
  private token = 0;

  constructor(private readonly base = "/audio/tour/") {}

  /** Call from a click or key handler: creates or resumes the audio context and starts loading. */
  prime(): void {
    const ctx = this.context();
    void ctx.resume();
    void this.load();
  }

  private context(): AudioContext {
    if (!this.ctx) {
      this.ctx = new AudioContext();
      this.master = this.ctx.createGain();
      this.master.gain.value = this.muted ? 0 : 1;
      this.master.connect(this.ctx.destination);
    }
    return this.ctx;
  }

  load(): Promise<void> {
    if (!this.loading) this.loading = this.fetchAll();
    return this.loading;
  }

  private async fetchAll(): Promise<void> {
    try {
      const res = await fetch(`${this.base}narration.json`, { cache: "no-cache" });
      if (!res.ok) throw new Error(`narration manifest: HTTP ${res.status}`);
      this.manifest = (await res.json()) as Manifest;
      const ctx = this.context();
      this.buffers = await Promise.all(
        this.manifest.shots.map(async (s) => {
          if (!s.file) return null;
          try {
            const r = await fetch(s.file);
            return r.ok ? await ctx.decodeAudioData(await r.arrayBuffer()) : null;
          } catch {
            return null;
          }
        }),
      );
    } catch {
      this.manifest = null; // no clips: the lines are spoken by the browser instead
      this.buffers = [];
    }
  }

  private lines(index: number): Line[] {
    return (this.manifest?.shots[index] ?? (source.shots as NarrationShot[])[index])?.lines ?? [];
  }

  /** Play shot `index` from `offset` seconds into it (a late start keeps the voice on the tour's clock). */
  async play(index: number, offset = 0): Promise<void> {
    this.stop(0);
    const token = ++this.token;
    await this.load();
    if (token !== this.token) return; // something newer was asked for while loading
    const buffer = this.buffers[index];
    if (buffer && this.ctx && this.master) {
      if (offset >= buffer.duration) return;
      const gain = this.ctx.createGain();
      gain.gain.value = 1;
      gain.connect(this.master);
      const src = this.ctx.createBufferSource();
      src.buffer = buffer;
      src.connect(gain);
      const startedAt = this.ctx.currentTime - Math.max(0, offset);
      src.start(0, Math.max(0, offset));
      src.onended = () => {
        if (this.clip?.src === src) this.clip = null;
      };
      this.clip = { src, gain, shot: index, startedAt };
      return;
    }
    this.speak(index, offset);
  }

  private speak(index: number, offset: number): void {
    const synth = typeof window !== "undefined" ? window.speechSynthesis : undefined;
    if (!synth) return;
    const timers = this.lines(index)
      .filter((l) => l.at + 0.05 >= offset)
      .map((l) =>
        window.setTimeout(() => {
          if (this.muted) return;
          const u = new SpeechSynthesisUtterance(l.text);
          u.lang = "en-US";
          u.rate = 1.0;
          synth.speak(u);
        }, Math.max(0, (l.at - offset) * 1000)),
      );
    this.speech = { shot: index, timers };
  }

  /** Stop whatever is playing, with a short fade so nothing clicks. */
  stop(fadeMs = FADE_S * 1000): void {
    this.token++;
    const c = this.clip;
    if (c && this.ctx) {
      const now = this.ctx.currentTime;
      c.gain.gain.cancelScheduledValues(now);
      c.gain.gain.setValueAtTime(c.gain.gain.value, now);
      c.gain.gain.linearRampToValueAtTime(0, now + Math.max(0.001, fadeMs / 1000));
      try {
        c.src.stop(now + Math.max(0.001, fadeMs / 1000) + 0.01);
      } catch {
        /* already stopped */
      }
    }
    this.clip = null;
    if (this.speech) {
      this.speech.timers.forEach((t) => window.clearTimeout(t));
      window.speechSynthesis?.cancel();
      this.speech = null;
    }
  }

  /** Mute or unmute in place: the clip keeps running, so the voice comes back in sync. */
  setMuted(muted: boolean): void {
    this.muted = muted;
    if (this.master && this.ctx) {
      const now = this.ctx.currentTime;
      this.master.gain.cancelScheduledValues(now);
      this.master.gain.setValueAtTime(this.master.gain.value, now);
      this.master.gain.linearRampToValueAtTime(muted ? 0 : 1, now + RAMP_S);
    }
    if (muted && this.speech) window.speechSynthesis?.cancel();
  }

  /** The tab was hidden: the tour's clock stops, so the voice waits with it. */
  pause(): void {
    void this.ctx?.suspend();
    window.speechSynthesis?.pause();
  }

  resume(): void {
    void this.ctx?.resume();
    window.speechSynthesis?.resume();
  }

  get state(): NarrationState {
    const c = this.clip;
    return {
      playing: c ? c.shot : this.speech ? this.speech.shot : null,
      muted: this.muted,
      source: c ? "file" : this.speech ? "speech" : null,
      position: c && this.ctx ? +(this.ctx.currentTime - c.startedAt).toFixed(3) : null,
      ready: this.buffers.some((b) => b != null),
    };
  }

  dispose(): void {
    this.stop(0);
    void this.ctx?.close();
    this.ctx = null;
    this.master = null;
  }
}

/** Where each shot's narration starts on the tour clock: the first cue of that shot. */
export function shotStarts(cues: readonly { shot: string; from: number }[], shots: readonly { id: string }[]): number[] {
  return shots.map((s) => cues.find((c) => c.shot === s.id)?.from ?? 0);
}
