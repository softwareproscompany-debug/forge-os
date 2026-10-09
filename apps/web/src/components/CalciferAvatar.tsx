import { useEffect, useRef } from "react";
import calciferImg from "../assets/calciferImage";
import { AudioAnalyzer } from "../lib/audioAnalyzer";
import type { OrbState } from "../lib/orbState";

export interface CalciferAvatarProps {
  state: OrbState;
  /** Live mic stream while listening. Null otherwise. */
  micStream: MediaStream | null;
  /** Real playback element while speaking (ElevenLabs). Null otherwise. */
  audioElement: HTMLAudioElement | null;
  onTap: () => void;
  disabled?: boolean;
  label: string;
}

/**
 * Calcifer — the living fire model, rendered from Michael's exact reference
 * image (Studio Ghibli character, personal build).
 *
 * - The image's black background melts into the near-black page via a
 *   feathered radial mask, with a faint warm glow behind it.
 * - The mouth overlay sits over the painted smile (measured at 50.8% / 67.6%
 *   of the asset) and opens/closes from the REAL speech audio through the
 *   shared AudioAnalyzer (TTS playback element while speaking). Fast attack /
 *   slow release envelope so it reads as talking, not jitter. Hidden at idle,
 *   so the original smile shows.
 * - Alive motion: gentle bob, flame flicker, and a glow that breathes with
 *   the orb state — restrained, never cartoonish-bouncy.
 * - Overlay positioning is percentage-based, so it stays aligned at any size.
 */
export function CalciferAvatar(props: CalciferAvatarProps) {
  const { state, micStream, audioElement, onTap, disabled, label } = props;
  const wrapRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);
  const mouthRef = useRef<HTMLDivElement>(null);
  const glowRef = useRef<HTMLDivElement>(null);
  const analyzerRef = useRef<AudioAnalyzer | null>(null);
  const stateRef = useRef<OrbState>(state);
  const reducedMotion = useRef(false);

  stateRef.current = state;

  // Attach real audio sources as they change (same contract as the orb).
  useEffect(() => {
    if (!analyzerRef.current) analyzerRef.current = new AudioAnalyzer();
    const az = analyzerRef.current;
    if (state === "listening" && micStream) az.attachMic(micStream);
    else if (state === "speaking" && audioElement) az.attachElement(audioElement);
    else if (state === "idle" || state === "sleep" || state === "interrupted")
      az.detach();
    // processing/error keep the last source bound (levels decay naturally)
  }, [state, micStream, audioElement]);

  // Animation loop: mouth envelope + bob + flicker + glow.
  useEffect(() => {
    if (!analyzerRef.current) analyzerRef.current = new AudioAnalyzer();
    reducedMotion.current =
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

    let raf = 0;
    let disposed = false;
    let mouth = 0; // smoothed 0..1
    let flash = 0; // success pulse, decays
    let last = performance.now();

    const frame = () => {
      if (disposed) return;
      const now = performance.now();
      const t = now / 1000;
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      const s = stateRef.current;
      const levels = analyzerRef.current?.read() ?? {
        level: 0,
        bass: 0,
        mid: 0,
        high: 0,
      };
      const lvl = Math.min(1, Math.max(0, levels.level));

      // Mouth: driven by real speech audio while speaking only.
      const target = s === "speaking" ? Math.min(1, lvl * 2.4) : 0;
      const rate = target > mouth ? 1 - Math.pow(0.0001, dt) : 1 - Math.pow(0.02, dt);
      mouth += (target - mouth) * rate;

      if (s === "success") flash = 1;
      flash = Math.max(0, flash - dt * 1.6);

      const inner = innerRef.current;
      const mouthEl = mouthRef.current;
      const glow = glowRef.current;
      if (inner && mouthEl && glow) {
        if (reducedMotion.current) {
          inner.style.transform = "";
          mouthEl.style.transform =
            "translate(-50%, -50%) scale(1, 0.06)";
          glow.style.opacity = s === "sleep" ? "0.12" : "0.35";
        } else {
          // Energy per state: idle < listening < processing < speaking.
          const energy =
            s === "speaking"
              ? 1.4
              : s === "processing"
                ? 1.6
                : s === "listening"
                  ? 1.15
                  : 1.0;
          // Gentle bob — slow, small, never bouncy.
          const bob = Math.sin(t * 1.35) * 3.2 * energy;
          const sway = Math.sin(t * 0.9 + 1.2) * 1.6 * energy;
          // Flame flicker: layered sines for an organic shimmer.
          const flicker =
            1 +
            0.012 * Math.sin(t * 9.3) +
            0.009 * Math.sin(t * 17.7 + 2.1) +
            0.05 * lvl;
          const dim = s === "sleep" ? 0.62 : 1;
          inner.style.transform = `translate(${sway.toFixed(2)}px, ${bob.toFixed(2)}px) scale(${(flicker * dim).toFixed(4)})`;
          inner.style.filter = `brightness(${(dim * (1 + flash * 0.35)).toFixed(3)})`;
          // Mouth: scaleY from the envelope; nearly closed at rest so the
          // painted smile shows through.
          const open = 0.06 + mouth * 1.15;
          mouthEl.style.transform = `translate(-50%, -50%) scale(1, ${open.toFixed(3)})`;
          mouthEl.style.opacity = mouth > 0.02 ? "1" : "0";
          // Warm glow breathes with state + voice.
          const baseGlow =
            s === "sleep" ? 0.12 : s === "error" ? 0.3 : 0.34;
          const glowPulse =
            baseGlow +
            0.1 * Math.sin(t * 2.2) +
            0.22 * lvl +
            0.3 * flash;
          glow.style.opacity = Math.max(0.08, Math.min(0.9, glowPulse)).toFixed(3);
        }
      }
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => {
      disposed = true;
      cancelAnimationFrame(raf);
      analyzerRef.current?.detach();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div ref={wrapRef} className="calcifer-wrap" data-state={state}>
      <div ref={glowRef} className="calcifer-glow" aria-hidden="true" />
      <button
        type="button"
        className="calcifer-hit"
        onClick={onTap}
        disabled={disabled}
        aria-label={label}
      >
        <div ref={innerRef} className="calcifer-inner">
          <img
            src={calciferImg}
            alt="Calcifer"
            className="calcifer-img"
            draggable={false}
          />
          <div ref={mouthRef} className="calcifer-mouth" aria-hidden="true" />
        </div>
      </button>
    </div>
  );
}
