/**
 * Typed state machine for the Draven cinematic orb.
 *
 * Every visual state derives from a real application event — never from
 * arbitrary timers. Invalid transitions are rejected loudly in dev and
 * ignored safely in production.
 */

export type OrbState =
  | "idle"
  | "wake"
  | "listening"
  | "processing"
  | "speaking"
  | "interrupted"
  | "success"
  | "error"
  | "sleep";

/** Allowed transitions. Anything not listed is invalid. */
const TRANSITIONS: Record<OrbState, ReadonlySet<OrbState>> = {
  idle: new Set(["wake", "listening", "processing", "error", "sleep"]),
  wake: new Set(["listening", "processing", "idle", "error"]),
  listening: new Set(["processing", "idle", "interrupted", "error", "sleep"]),
  processing: new Set(["speaking", "listening", "idle", "success", "error", "interrupted"]),
  speaking: new Set(["interrupted", "listening", "idle", "success", "error", "processing"]),
  interrupted: new Set(["listening", "idle", "processing", "error"]),
  success: new Set(["idle", "listening", "speaking", "processing"]),
  error: new Set(["idle", "wake", "listening", "processing"]),
  sleep: new Set(["idle", "wake"]),
};

export function canTransition(from: OrbState, to: OrbState): boolean {
  return TRANSITIONS[from]?.has(to) ?? false;
}

export interface OrbStateVisuals {
  /** Core brightness multiplier */
  coreGlow: number;
  /** Surface turbulence speed multiplier */
  turbulence: number;
  /** Orbital ring spin multiplier */
  ringSpin: number;
  /** Particle energy multiplier */
  particleEnergy: number;
  /** Halo expansion multiplier */
  halo: number;
  /** Overall scale pulse */
  scalePulse: number;
  /** Holographic marker visibility 0..1 */
  holoMarkers: number;
  /** Error tint 0..1 (amber/red wash) */
  errorTint: number;
}

/** Per-state visual parameters the renderer interpolates toward. */
export const STATE_VISUALS: Record<OrbState, OrbStateVisuals> = {
  idle:        { coreGlow: 0.55, turbulence: 0.35, ringSpin: 0.25, particleEnergy: 0.30, halo: 0.70, scalePulse: 1.00, holoMarkers: 0, errorTint: 0 },
  wake:        { coreGlow: 1.40, turbulence: 1.20, ringSpin: 1.60, particleEnergy: 1.40, halo: 1.25, scalePulse: 1.12, holoMarkers: 0.6, errorTint: 0 },
  listening:   { coreGlow: 1.00, turbulence: 0.90, ringSpin: 0.80, particleEnergy: 0.85, halo: 1.00, scalePulse: 1.04, holoMarkers: 0.25, errorTint: 0 },
  processing:  { coreGlow: 0.95, turbulence: 1.60, ringSpin: 2.40, particleEnergy: 1.30, halo: 0.95, scalePulse: 1.02, holoMarkers: 1, errorTint: 0 },
  speaking:    { coreGlow: 1.15, turbulence: 1.10, ringSpin: 1.00, particleEnergy: 1.00, halo: 1.10, scalePulse: 1.06, holoMarkers: 0.15, errorTint: 0 },
  interrupted: { coreGlow: 0.45, turbulence: 0.50, ringSpin: 0.40, particleEnergy: 0.40, halo: 0.60, scalePulse: 0.97, holoMarkers: 0, errorTint: 0 },
  success:     { coreGlow: 1.50, turbulence: 0.60, ringSpin: 0.60, particleEnergy: 0.90, halo: 1.30, scalePulse: 1.08, holoMarkers: 0.4, errorTint: 0 },
  error:       { coreGlow: 0.70, turbulence: 0.45, ringSpin: 0.20, particleEnergy: 0.35, halo: 0.75, scalePulse: 1.00, holoMarkers: 0.5, errorTint: 1 },
  sleep:       { coreGlow: 0.18, turbulence: 0.10, ringSpin: 0.05, particleEnergy: 0.08, halo: 0.35, scalePulse: 0.99, holoMarkers: 0, errorTint: 0 },
};

/** Auto-advance map: transient states resolve on their own after a beat. */
export const AUTO_ADVANCE: Partial<Record<OrbState, { to: OrbState; afterMs: number }>> = {
  wake: { to: "listening", afterMs: 650 },
  success: { to: "idle", afterMs: 1100 },
  interrupted: { to: "idle", afterMs: 500 },
};
