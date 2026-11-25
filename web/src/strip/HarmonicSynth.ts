// "Play spectral harmonics": a chord built in the browser from ordinates on the t axis, either
// the first zeros of zeta or the tallest peaks of the proof wave on the critical line. $0.
//
// The ordinate is not used as the raw pitch: t * 16 Hz puts the low partials near 30 to 130 Hz,
// which laptop and phone speakers cannot reproduce. Pitch is the affine map below, which keeps
// every partial between about 160 and 1,060 Hz and preserves the spacing of the ordinates. The
// interface always shows the exact t next to the mapped pitch.

export interface Partial {
  /** ordinate on the t axis (the mathematical value) */
  t: number;
  /** relative loudness, e.g. 1 / |rho| for a zero or the peak height for the wave */
  weight: number;
  /** which marker to pulse when this partial enters */
  index: number;
}

export const pitchOf = (t: number): number => 160 + 15 * t;

const ENTER_S = 0.3;
const ATTACK_S = 0.04;
const HOLD_S = 2.4;
const RELEASE_S = 0.7;

export class HarmonicSynth {
  private ctx: AudioContext | null = null;
  private master: GainNode | null = null;
  private timers: number[] = [];
  private sources: OscillatorNode[] = [];

  get playing(): boolean {
    return this.sources.length > 0;
  }

  /** Must be called from a user gesture (it creates or resumes the audio context). */
  play(partials: Partial[], onEnter: (p: Partial) => void, onEnd: () => void): number {
    this.stop();
    if (!partials.length) return 0;
    const ctx = this.ctx ?? (this.ctx = new AudioContext());
    void ctx.resume();
    const total = partials.reduce((a, p) => a + p.weight, 0) || 1;
    const duration = ENTER_S * partials.length + HOLD_S + RELEASE_S;
    const t0 = ctx.currentTime + 0.05;
    const master = ctx.createGain();
    master.gain.setValueAtTime(0.85, t0);
    master.gain.setValueAtTime(0.85, t0 + duration - RELEASE_S);
    master.gain.linearRampToValueAtTime(0, t0 + duration);
    const limiter = ctx.createDynamicsCompressor(); // headroom, so stacked partials never clip
    master.connect(limiter).connect(ctx.destination);
    this.master = master;
    partials.forEach((p, k) => {
      const start = t0 + k * ENTER_S;
      const level = Math.min(0.5, (0.9 * p.weight) / total + 0.03);
      const env = ctx.createGain();
      env.gain.setValueAtTime(0, start);
      env.gain.linearRampToValueAtTime(level, start + ATTACK_S);
      const pan = ctx.createStereoPanner();
      pan.pan.value = partials.length > 1 ? -0.55 + (1.1 * k) / (partials.length - 1) : 0;
      env.connect(pan).connect(master);
      const f = pitchOf(p.t);
      // the fundamental plus a soft second harmonic: warmer, and audible on small speakers
      for (const [mult, gain] of [[1, 1], [2, 0.16]] as const) {
        const osc = ctx.createOscillator();
        osc.type = "sine";
        osc.frequency.value = f * mult;
        const g = ctx.createGain();
        g.gain.value = gain;
        osc.connect(g).connect(env);
        osc.start(start);
        osc.stop(t0 + duration + 0.05);
        this.sources.push(osc);
      }
      this.timers.push(window.setTimeout(() => onEnter(p), Math.max(0, (start - ctx.currentTime) * 1000)));
    });
    this.timers.push(window.setTimeout(() => {
      this.sources = [];
      onEnd();
    }, (duration + 0.1) * 1000));
    return duration;
  }

  stop(): void {
    this.timers.forEach((id) => window.clearTimeout(id));
    this.timers = [];
    const ctx = this.ctx;
    if (ctx && this.master) {
      this.master.gain.cancelScheduledValues(ctx.currentTime);
      this.master.gain.setTargetAtTime(0, ctx.currentTime, 0.03);
    }
    this.sources.forEach((s) => {
      try {
        s.stop((ctx?.currentTime ?? 0) + 0.15);
      } catch {
        /* already stopped */
      }
    });
    this.sources = [];
    this.master = null;
  }

  dispose(): void {
    this.stop();
    void this.ctx?.close();
    this.ctx = null;
  }
}
