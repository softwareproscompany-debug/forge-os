/**
 * AudioAnalyzer — real audio analysis for the cinematic orb.
 *
 * Feeds the orb's animation from ACTUAL signals:
 *  - microphone input while listening
 *  - the real playback element while speaking (ElevenLabs audio or TTS)
 *
 * Never invents a waveform. When no source is attached, levels decay to a
 * quiet floor and the orb falls back to its synthetic idle motion.
 *
 * Attack/release smoothing keeps the visuals cinematic instead of jittery:
 * fast attack so the orb reacts instantly, slow release so it settles
 * gracefully between phrases.
 */

export interface AudioLevels {
  /** Overall smoothed amplitude 0..1 */
  level: number;
  /** Bass band energy 0..1 */
  bass: number;
  /** Mid band energy 0..1 */
  mid: number;
  /** High band energy 0..1 */
  high: number;
}

const FFT_SIZE = 256;
const ATTACK = 0.55; // fast reaction on onset
const RELEASE = 0.08; // graceful settle

export class AudioAnalyzer {
  private ctx: AudioContext | null = null;
  private analyser: AnalyserNode | null = null;
  private micSource: MediaStreamAudioSourceNode | null = null;
  private elementSource: MediaElementAudioSourceNode | null = null;
  private freqData: Uint8Array | null = null;
  private levels: AudioLevels = { level: 0, bass: 0, mid: 0, high: 0 };
  private lastElement: HTMLAudioElement | null = null;

  private ensureContext(): AudioContext | null {
    if (typeof window === "undefined") return null;
    try {
      if (!this.ctx) {
        const AC = window.AudioContext || (window as any).webkitAudioContext;
        if (!AC) return null;
        this.ctx = new AC();
      }
      if (this.ctx.state === "suspended") void this.ctx.resume();
      return this.ctx;
    } catch {
      return null;
    }
  }

  /** Attach live microphone input (call while listening). */
  attachMic(stream: MediaStream): void {
    const ctx = this.ensureContext();
    if (!ctx) return;
    this.detachElement();
    try {
      this.micSource?.disconnect();
    } catch { /* noop */ }
    try {
      this.micSource = ctx.createMediaStreamSource(stream);
      this.bindAnalyser(this.micSource);
    } catch {
      this.micSource = null;
    }
  }

  /**
   * Attach an <audio> element's real playback signal (call while speaking).
   * A MediaElementSource can only be created once per element — reuse it.
   */
  attachElement(el: HTMLAudioElement): void {
    const ctx = this.ensureContext();
    if (!ctx) return;
    if (this.lastElement === el && this.elementSource) {
      this.bindAnalyser(this.elementSource);
      return;
    }
    this.detachMic();
    try {
      this.elementSource = ctx.createMediaElementSource(el);
      // Route through the analyser to the destination so audio still plays.
      this.elementSource.connect(ctx.destination);
      this.lastElement = el;
      this.bindAnalyser(this.elementSource);
    } catch {
      // Element already has a source node elsewhere — analyze without it.
      this.elementSource = null;
    }
  }

  private bindAnalyser(source: AudioNode): void {
    const ctx = this.ctx;
    if (!ctx) return;
    try {
      this.analyser?.disconnect();
    } catch { /* noop */ }
    this.analyser = ctx.createAnalyser();
    this.analyser.fftSize = FFT_SIZE;
    this.analyser.smoothingTimeConstant = 0.72;
    try {
      source.connect(this.analyser);
    } catch { /* noop */ }
    this.freqData = new Uint8Array(this.analyser.frequencyBinCount);
  }

  private detachMic(): void {
    try {
      this.micSource?.disconnect();
    } catch { /* noop */ }
    this.micSource = null;
  }

  private detachElement(): void {
    // Keep the element source alive (it feeds the speakers); just unbind.
    this.elementSource = null;
    this.lastElement = null;
  }

  /** Release everything (call on unmount / interrupt). */
  detach(): void {
    this.detachMic();
    this.detachElement();
    try {
      this.analyser?.disconnect();
    } catch { /* noop */ }
    this.analyser = null;
    this.freqData = null;
  }

  /** Read smoothed levels. Safe to call every frame. */
  read(): AudioLevels {
    if (!this.analyser || !this.freqData) {
      // No source: decay toward the quiet floor.
      const l = this.levels;
      l.level += (0 - l.level) * RELEASE;
      l.bass += (0 - l.bass) * RELEASE;
      l.mid += (0 - l.mid) * RELEASE;
      l.high += (0 - l.high) * RELEASE;
      return l;
    }
    this.analyser.getByteFrequencyData(this.freqData);
    const d = this.freqData;
    const n = d.length;
    // Bands across the spectrum (voice lives mostly in low/mid).
    const bass = avg(d, 1, Math.floor(n * 0.12)) / 255;
    const mid = avg(d, Math.floor(n * 0.12), Math.floor(n * 0.45)) / 255;
    const high = avg(d, Math.floor(n * 0.45), n - 1) / 255;
    const level = Math.min(1, bass * 0.55 + mid * 0.35 + high * 0.10);
    const l = this.levels;
    l.level = smooth(l.level, level);
    l.bass = smooth(l.bass, bass);
    l.mid = smooth(l.mid, mid);
    l.high = smooth(l.high, high);
    return l;
  }
}

function avg(d: Uint8Array, from: number, to: number): number {
  let s = 0;
  const count = Math.max(1, to - from);
  for (let i = from; i < to; i++) s += d[i];
  return s / count;
}

function smooth(prev: number, next: number): number {
  const k = next > prev ? ATTACK : RELEASE;
  return prev + (next - prev) * k;
}
