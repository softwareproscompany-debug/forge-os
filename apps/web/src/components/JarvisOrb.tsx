/**
 * JarvisOrb — the cinematic Draven orb, built on Three.js per the master spec.
 *
 * Three.js scene graph (no hand-rolled matrix math):
 *   1. Tendril core     — THREE.SphereGeometry + GLSL ShaderMaterial:
 *      domain-warped ridged-fbm smoke filaments (thin tendrils, never a
 *      solid ball), fragmented patchy limb, dark see-through center,
 *      faint amber ember pockets deep inside. Reference image is law.
 *   2. Plasma shells    — two fresnel shells broken into drifting wisps
 *      by noise (additive); never a smooth rim.
 *   3. Smoke coronas    — two larger wispy shells breaking the silhouette.
 *   4. Orbital geometry — three inclined arc Line loops at varied radii.
 *   5. Particle field   — THREE.Points with GPU-orbit shader.
 *   6. Atmospheric halo — radial-gradient plane behind everything (additive).
 *   7. Holo markers     — contextual arc ticks (processing/verification).
 *
 * All motion derives from the typed orb state machine (lib/orbState.ts) and
 * the real AudioAnalyzer signal (lib/audioAnalyzer.ts). Nothing is faked:
 * when no audio source is attached the orb falls back to state-driven
 * motion. WebGL is required — if the Three.js renderer cannot be created
 * the orb reports failure honestly (dataset.orbStatus = "failed") instead
 * of rendering simplified visuals.
 */

import { useEffect, useRef } from "react";
import * as THREE from "three";
import { AudioAnalyzer } from "../lib/audioAnalyzer";
import {
  AUTO_ADVANCE,
  STATE_VISUALS,
  canTransition,
  type OrbState,
  type OrbStateVisuals,
} from "../lib/orbState";

export interface JarvisOrbProps {
  state: OrbState;
  /** Live mic stream while listening (drives the halo). Null otherwise. */
  micStream: MediaStream | null;
  /** Real playback element while speaking (ElevenLabs). Null otherwise. */
  audioElement: HTMLAudioElement | null;
  onTap: () => void;
  disabled?: boolean;
  label: string;
  /** Visual theme. Same renderer/state machine/audio — only palette and
   *  motion character change. "jarvis" = cyan cinematic, "calcifer" = fire. */
  theme?: OrbTheme;
}

export type OrbTheme = "jarvis" | "calcifer";

interface OrbPalette {
  deep: [number, number, number];
  mid: [number, number, number];
  energy: [number, number, number];
  hot: [number, number, number];
  ember: [number, number, number];
  halo: [number, number, number];
  particle: [number, number, number];
  ring: [number, number, number];
  ringAlt: [number, number, number];
  /** 0 = jarvis drift, 1 = calcifer stronger upward flame draft */
  flame: number;
}

const PALETTES: Record<OrbTheme, OrbPalette> = {
  jarvis: {
    deep: [0.002, 0.008, 0.02],
    mid: [0.03, 0.22, 0.62],
    energy: [0.08, 0.75, 1.0],
    hot: [0.8, 0.95, 1.0],
    ember: [1.0, 0.45, 0.12],
    halo: [0.1, 0.62, 0.95],
    particle: [0.45, 0.85, 1.0],
    ring: [0.1, 0.45, 0.75],
    ringAlt: [0.35, 0.7, 0.9],
    flame: 0,
  },
  calcifer: {
    deep: [0.03, 0.008, 0.003],
    mid: [0.55, 0.13, 0.03],
    energy: [1.0, 0.46, 0.08],
    hot: [1.0, 0.93, 0.62],
    ember: [1.0, 0.74, 0.22],
    halo: [1.0, 0.42, 0.1],
    particle: [1.0, 0.6, 0.18],
    ring: [0.85, 0.36, 0.1],
    ringAlt: [1.0, 0.6, 0.22],
    flame: 1,
  },
};

/* ------------------------------------------------------------------ */
/* GLSL (shared noise + per-layer shaders)                             */
/* ------------------------------------------------------------------ */

const NOISE_GLSL = `
float hash13(vec3 p) {
  p = fract(p * 0.1031);
  p += dot(p, p.zyx + 31.32);
  return fract((p.x + p.y) * p.z);
}
float vnoise(vec3 p) {
  vec3 i = floor(p);
  vec3 f = fract(p);
  f = f * f * (3.0 - 2.0 * f);
  float n000 = hash13(i);
  float n100 = hash13(i + vec3(1.0, 0.0, 0.0));
  float n010 = hash13(i + vec3(0.0, 1.0, 0.0));
  float n110 = hash13(i + vec3(1.0, 1.0, 0.0));
  float n001 = hash13(i + vec3(0.0, 0.0, 1.0));
  float n101 = hash13(i + vec3(1.0, 0.0, 1.0));
  float n011 = hash13(i + vec3(0.0, 0.0, 1.0));
  float n111 = hash13(i + vec3(1.0, 1.0, 1.0));
  return mix(
    mix(mix(n000, n100, f.x), mix(n010, n110, f.x), f.y),
    mix(mix(n001, n101, f.x), mix(n011, n111, f.x), f.y),
    f.z);
}
float fbm(vec3 p) {
  float v = 0.0;
  float a = 0.55;
  for (int i = 0; i < 4; i++) {
    v += a * vnoise(p);
    p = p * 2.03 + vec3(1.7, 9.2, 4.1);
    a *= 0.5;
  }
  return v;
}
`;

const CORE_VERT = `
uniform float uTime;
uniform float uDisplace;
uniform float uTurb;
varying vec3 vNormal;
varying vec3 vWorldPos;
varying vec3 vObjPos;
${NOISE_GLSL}
void main() {
  vec3 p = position;
  float n = vnoise(position * 2.6 + vec3(0.0, uTime * 0.35 * uTurb, 0.0)) * 0.65
          + vnoise(position * 7.5 + vec3(1.7, -uTime * 0.55 * uTurb, 0.0)) * 0.35;
  p += normal * (n - 0.5) * uDisplace;
  vObjPos = p;
  vec4 wp = modelMatrix * vec4(p, 1.0);
  vWorldPos = wp.xyz;
  vNormal = normalize(mat3(modelMatrix) * normal);
  gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
}
`;

const CORE_FRAG = `
precision mediump float;
varying vec3 vNormal;
varying vec3 vWorldPos;
varying vec3 vObjPos;
uniform float uTime;
uniform float uGlow;
uniform float uTurb;
uniform float uLevel;
uniform float uBass;
uniform float uHigh;
uniform float uError;
uniform vec3 uPalDeep;
uniform vec3 uPalMid;
uniform vec3 uPalEnergy;
uniform vec3 uPalHot;
uniform vec3 uPalEmber;
uniform float uFlame;
${NOISE_GLSL}
// Reference image is law: the orb is a sphere of WISPY SMOKE TENDRILS, not a
// solid object. The center stays dark near-black and see-through; energy
// lives in thin ridged filaments, a fragmented patchy limb, and faint amber
// embers deep inside. No glossy ball, no uniform ring.
void main() {
  vec3 N = normalize(vNormal);
  vec3 V = normalize(cameraPosition - vWorldPos);
  float facing = max(dot(N, V), 0.0);
  float fres = pow(1.0 - facing, 2.2);

  // Slow organic drift, quickened by voice energy.
  float t = uTime * (0.45 + 0.85 * uLevel) * (0.6 + 0.4 * uTurb);
  // Low base frequency: long sweeping tendrils, not marble veins.
  vec3 q = vObjPos * 1.35;
  vec3 warp = vec3(
    fbm(q * 1.25 + vec3(0.0, t * 0.33, 1.7)),
    fbm(q * 1.25 + vec3(4.7, 1.3, t * 0.26)),
    fbm(q * 1.25 + vec3(9.1, 2.8, 3.7)));
  vec3 p = q * 1.5 + warp * 3.4 + vec3(0.0, -t * 0.45, 0.0);

  // Ridged noise -> thin smoke filaments. High thresholds keep them sparse:
  // fbm clusters near 0.5, so only the very crest of each ridge survives.
  float n1 = fbm(p);
  float ridge1 = 1.0 - abs(2.0 * n1 - 1.0);
  float fil = pow(smoothstep(0.62, 0.995, ridge1), 2.5);

  // Finer second filament layer for detail.
  float n2 = fbm(p * 2.35 + vec3(0.0, t * 0.6, 3.1));
  float ridge2 = 1.0 - abs(2.0 * n2 - 1.0);
  float fil2 = pow(smoothstep(0.68, 0.995, ridge2), 2.5);

  // Bright knots where filaments fold.
  float knots = pow(smoothstep(0.80, 1.0, ridge1), 6.0);

  // Fragmented limb: patchy tendril clusters at the edge, never a ring.
  float limbNoise = fbm(vObjPos * 3.3 + vec3(0.0, t * 0.2, 7.3));
  float limbFrag = smoothstep(0.32, 0.85, limbNoise);
  float limb = pow(fres, 1.7) * (0.2 + 0.8 * limbFrag);

  vec3 cyan = uPalEnergy;
  vec3 ice = uPalHot;
  float energy = 0.7 + 0.9 * uLevel + 0.5 * uBass;

  // Filaments fade toward the disc center so the core stays dark; the limb
  // carries the energy, like the reference.
  float limbW = 0.18 + 0.82 * pow(fres, 1.6);
  vec3 filCol = mix(cyan, ice, clamp(fil2 * 0.65 + knots * 0.8, 0.0, 1.0))
              * (fil * 0.8 + fil2 * 0.5) * energy * limbW;
  filCol += ice * knots * 0.85 * energy;
  filCol += ice * uHigh * 0.28 * fil;

  vec3 limbCol = mix(cyan * 0.45, ice, limbFrag * 0.55)
               * limb * (0.45 + 0.75 * uGlow) * energy;

  // Dark see-through center: only the faintest deep smoke.
  float smoke = fbm(q * 1.15 + warp * 0.8);
  float bodyA = smoothstep(0.58, 0.96, smoke) * 0.04;
  vec3 col = uPalDeep * bodyA;

  // Amber embers deep inside: faint, offset, slow flicker.
  float e1 = 1.0 - smoothstep(0.10, 0.55, length(vObjPos - vec3(-0.34, 0.03, 0.26)));
  float e2 = 1.0 - smoothstep(0.08, 0.45, length(vObjPos - vec3(0.30, -0.20, -0.14)));
  float flick = 0.55 + 0.45 * sin(uTime * 1.25 + vObjPos.y * 6.0 + vObjPos.x * 4.0);
  float emberMask = smoothstep(0.30, 0.62, smoke);
  vec3 emberCol = uPalEmber * (e1 * 0.5 + e2 * 0.38) * flick * 0.30 * emberMask;

  vec3 add = filCol + limbCol + emberCol;
  add = mix(add,
            vec3(1.0, 0.42, 0.12) * (fil * 0.9 + limb * 0.6 + (e1 + e2) * 0.3),
            uError * 0.65);
  col += add;

  float alpha = clamp(
      dot(filCol, vec3(0.333)) * 1.15
    + dot(limbCol, vec3(0.333)) * 1.0
    + (e1 * 0.5 + e2 * 0.38) * 0.22
    + bodyA,
    0.0, 1.0);

  gl_FragColor = vec4(col * (0.4 + 0.6 * uGlow), alpha);
}
`;

const SHELL_VERT = `
varying vec3 vNormal;
varying vec3 vWorldPos;
void main() {
  vec4 wp = modelMatrix * vec4(position, 1.0);
  vWorldPos = wp.xyz;
  vNormal = normalize(mat3(modelMatrix) * normal);
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}
`;

const SHELL_FRAG = `
precision mediump float;
varying vec3 vNormal;
varying vec3 vWorldPos;
uniform float uTime;
uniform float uAlpha;
uniform vec3 uTint;
${NOISE_GLSL}
void main() {
  vec3 N = normalize(vNormal);
  vec3 V = normalize(cameraPosition - vWorldPos);
  float fres = pow(1.0 - abs(dot(N, V)), 3.0);
  // Break the shell into drifting wisps — never a smooth billiard-ball rim.
  float brk = fbm(vWorldPos * 3.6 + vec3(0.0, uTime * 0.12, 4.2));
  float wisps = smoothstep(0.34, 0.8, brk);
  float a = fres * uAlpha * wisps;
  gl_FragColor = vec4(uTint * a, a);
}
`;

const SMOKE_FRAG = `
precision mediump float;
varying vec3 vNormal;
varying vec3 vWorldPos;
varying vec3 vObjPos;
uniform float uTime;
uniform float uTurb;
uniform float uLevel;
uniform float uAlpha;
uniform vec3 uPalSmokeA;
uniform vec3 uPalSmokeB;
uniform float uFlame;
${NOISE_GLSL}
void main() {
  vec3 N = normalize(vNormal);
  vec3 V = normalize(cameraPosition - vWorldPos);
  float facing = abs(dot(N, V));

  // Slightly stretched domain so tendrils elongate instead of blobbing.
  vec3 q = vObjPos * vec3(2.5, 3.2, 2.5);
  float rise = 0.30 + 0.35 * uFlame;
  float w = fbm(q * 1.4 + vec3(0.0, -uTime * (0.22 + 0.25 * uFlame) * uTurb, uTime * 0.05 * uTurb));
  float s = fbm(q + 1.8 * w + vec3(0.0, -uTime * rise * uTurb, 0.0));
  float ridge = 1.0 - abs(2.0 * s - 1.0);
  float wisps = pow(smoothstep(0.40, 0.95, ridge), 5.0);
  // Fine strands nested inside the wisps.
  float s2 = fbm(q * 2.1 + 1.2 * w + vec3(2.0, -uTime * rise * 1.4 * uTurb, 1.0));
  float ridge2 = 1.0 - abs(2.0 * s2 - 1.0);
  float strands = pow(smoothstep(0.45, 1.0, ridge2), 7.0);

  float a = (wisps * 0.8 + strands * 0.55) * uAlpha * (0.30 + 0.70 * facing) * (0.65 + 0.65 * uLevel);
  vec3 col = mix(uPalSmokeA, uPalSmokeB, clamp(wisps + strands * 0.5, 0.0, 1.0));
  gl_FragColor = vec4(col * a, a);
}
`;

const SMOKE_VERT = `
varying vec3 vNormal;
varying vec3 vWorldPos;
varying vec3 vObjPos;
void main() {
  vObjPos = position;
  vec4 wp = modelMatrix * vec4(position, 1.0);
  vWorldPos = wp.xyz;
  vNormal = normalize(mat3(modelMatrix) * normal);
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}
`;

const HALO_VERT = `
varying vec2 vUv;
void main() {
  vUv = uv;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}
`;

const HALO_FRAG = `
precision mediump float;
varying vec2 vUv;
uniform float uIntensity;
uniform float uPulse;
uniform vec3 uTint;
uniform float uError;
void main() {
  vec2 c = vUv - 0.5;
  float d = length(c) * 2.0;
  float g = exp(-d * d * 4.6) * uIntensity * (0.85 + 0.30 * uPulse);
  float ring = exp(-pow((d - 0.62) * 6.0, 2.0)) * 0.28 * uIntensity;
  vec3 tint = mix(uTint, vec3(1.0, 0.45, 0.15), uError * 0.7);
  gl_FragColor = vec4(tint * (g + ring), (g + ring));
}
`;

const POINTS_VERT = `
attribute vec4 aSeed; // orbitRadius, angleOffset, speed, size
uniform float uTime;
uniform float uEnergy;
uniform float uPixelRatio;
uniform float uRise;
varying float vTwinkle;
void main() {
  float ang = aSeed.y + uTime * aSeed.z * (0.25 + 0.85 * uEnergy);
  float r = aSeed.x * (1.0 + 0.10 * sin(uTime * 0.8 + aSeed.y * 4.0) * uEnergy);
  vec3 p = vec3(cos(ang) * r, sin(ang * 0.85 + aSeed.y * 1.7) * r * 0.5, sin(ang) * r * 0.8);
  p.y += uRise * (0.22 * sin(uTime * (0.9 + aSeed.z) + aSeed.y * 2.0) + 0.10 * sin(ang * 2.0));
  vec4 mv = modelViewMatrix * vec4(p, 1.0);
  gl_Position = projectionMatrix * mv;
  float dist = max(0.4, -mv.z);
  gl_PointSize = aSeed.w * uPixelRatio * 26.0 / dist;
  vTwinkle = 0.55 + 0.45 * sin(uTime * (1.5 + aSeed.z * 2.0) + aSeed.y * 9.0);
}
`;

const POINTS_FRAG = `
precision mediump float;
varying float vTwinkle;
uniform float uAlpha;
uniform vec3 uTint;
void main() {
  vec2 c = gl_PointCoord - 0.5;
  float d = length(c) * 2.0;
  float disc = smoothstep(1.0, 0.15, d);
  gl_FragColor = vec4(uTint * disc * vTwinkle, disc * vTwinkle * uAlpha);
}
`;

/* ------------------------------------------------------------------ */
/* Quality tiers                                                       */
/* ------------------------------------------------------------------ */

type Quality = "high" | "balanced" | "low";

function detectQuality(): Quality {
  if (typeof window === "undefined") return "balanced";
  const coarse = window.matchMedia?.("(pointer: coarse)").matches ?? false;
  const small = Math.min(window.innerWidth, window.innerHeight) < 500;
  const lowMem =
    (navigator as unknown as { deviceMemory?: number }).deviceMemory != null &&
    (navigator as unknown as { deviceMemory?: number }).deviceMemory! <= 4;
  if (coarse && (small || lowMem)) return "low";
  if (coarse || small) return "balanced";
  return "high";
}

const QUALITY_CFG: Record<
  Quality,
  { pixelRatio: number; sphereSeg: number; particles: number; shells: number }
> = {
  high: { pixelRatio: 2, sphereSeg: 64, particles: 380, shells: 2 },
  balanced: { pixelRatio: 1.5, sphereSeg: 48, particles: 220, shells: 2 },
  low: { pixelRatio: 1, sphereSeg: 32, particles: 110, shells: 1 },
};

/* ------------------------------------------------------------------ */
/* Component                                                           */
/* ------------------------------------------------------------------ */

export function JarvisOrb(props: JarvisOrbProps) {
  const { state, micStream, audioElement, onTap, disabled, label, theme = "jarvis" } = props;
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const analyzerRef = useRef<AudioAnalyzer | null>(null);
  const stateRef = useRef<OrbState>(state);
  const themeRef = useRef<OrbTheme>(theme);
  const reducedMotion = useRef(false);
  const failedRef = useRef(false);

  stateRef.current = state;
  themeRef.current = theme;

  // Attach real audio sources as they change.
  useEffect(() => {
    if (!analyzerRef.current) analyzerRef.current = new AudioAnalyzer();
    const az = analyzerRef.current;
    if (state === "listening" && micStream) az.attachMic(micStream);
    else if (state === "speaking" && audioElement) az.attachElement(audioElement);
    else if (state === "idle" || state === "sleep" || state === "interrupted") az.detach();
    // processing/error keep the last source bound (levels decay naturally)
  }, [state, micStream, audioElement]);

  useEffect(() => {
    const wrap = wrapRef.current;
    const canvas = canvasRef.current;
    if (!wrap || !canvas) return;
    if (!analyzerRef.current) analyzerRef.current = new AudioAnalyzer();

    reducedMotion.current =
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

    const quality: Quality = detectQuality();
    const cfg = QUALITY_CFG[quality];
    const pal = PALETTES[themeRef.current] ?? PALETTES.jarvis;
    const v3 = (p: [number, number, number]) => new THREE.Vector3(...p);

    let renderer: THREE.WebGLRenderer | null = null;
    let rafId = 0;
    let running = true;
    let disposed = false;

    const failOrb = (err: unknown) => {
      // No simplified fallback — report honestly instead of fake visuals.
      failedRef.current = true;
      running = false;
      cancelAnimationFrame(rafId);
      wrap.dataset.orbStatus = "failed";
      console.warn("[JarvisOrb] Three.js renderer unavailable — orb disabled:", err);
      try { renderer?.dispose(); } catch { /* already torn down */ }
      renderer = null;
    };

    /* ---------------- scene ---------------- */
    let scene: THREE.Scene;
    let camera: THREE.PerspectiveCamera;
    try {
      renderer = new THREE.WebGLRenderer({
        canvas,
        alpha: true,
        antialias: true,
        powerPreference: "high-performance",
      });
    } catch (err) {
      failOrb(err);
      return;
    }
    if (!renderer) { failOrb(new Error("renderer creation returned null")); return; }
    const pixelRatio = Math.min(window.devicePixelRatio || 1, cfg.pixelRatio);
    renderer.setPixelRatio(pixelRatio);
    renderer.setClearColor(0x000000, 0);
    renderer.autoClear = true;

    scene = new THREE.Scene();
    camera = new THREE.PerspectiveCamera(42, 1, 0.1, 20);
    camera.position.set(0, 0, 2.6);

    const resize = () => {
      const rect = wrap.getBoundingClientRect();
      const w = Math.max(2, Math.floor(rect.width));
      const h = Math.max(2, Math.floor(rect.height));
      renderer!.setSize(w, h, false);
      camera.aspect = w / h;
      camera.updateProjectionMatrix();
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(wrap);

    // Pointer parallax (desktop only).
    let px = 0, py = 0, tx = 0, ty = 0;
    const finePointer = window.matchMedia?.("(pointer: fine)").matches ?? false;
    const onMove = (e: PointerEvent) => {
      const r = wrap.getBoundingClientRect();
      tx = ((e.clientX - r.left) / r.width - 0.5) * 0.16;
      ty = ((e.clientY - r.top) / r.height - 0.5) * -0.12;
    };
    if (finePointer) window.addEventListener("pointermove", onMove);

    /* ---------------- layers ---------------- */

    // Layer 1: energy core (smoke volume — black shows through).
    const coreUniforms = {
      uTime: { value: 0 },
      uDisplace: { value: 0.1 },
      uTurb: { value: 0.35 },
      uGlow: { value: 0.55 },
      uLevel: { value: 0 },
      uBass: { value: 0 },
      uHigh: { value: 0 },
      uError: { value: 0 },
      uPalDeep: { value: v3(pal.deep) },
      uPalMid: { value: v3(pal.mid) },
      uPalEnergy: { value: v3(pal.energy) },
      uPalHot: { value: v3(pal.hot) },
      uPalEmber: { value: v3(pal.ember) },
      uFlame: { value: pal.flame },
    };
    const core = new THREE.Mesh(
      new THREE.SphereGeometry(0.98, cfg.sphereSeg, Math.floor(cfg.sphereSeg * 0.75)),
      new THREE.ShaderMaterial({
        vertexShader: CORE_VERT,
        fragmentShader: CORE_FRAG,
        uniforms: coreUniforms,
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
        side: THREE.FrontSide,
      })
    );
    scene.add(core);

    // Layer 2: plasma shells (fresnel, additive).
    const shells: THREE.Mesh[] = [];
    const shellRadii = cfg.shells === 2 ? [1.12, 1.24] : [1.12];
    shellRadii.forEach((radius, si) => {
      const mat = new THREE.ShaderMaterial({
        vertexShader: SHELL_VERT,
        fragmentShader: SHELL_FRAG,
        uniforms: {
          uTime: { value: 0 },
          uAlpha: { value: si === 0 ? 0.28 : 0.12 },
          uTint: { value: v3(pal.energy) },
        },
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      });
      const m = new THREE.Mesh(
        new THREE.SphereGeometry(radius, Math.floor(cfg.sphereSeg * 0.75), Math.floor(cfg.sphereSeg * 0.5)),
        mat
      );
      m.userData.spin = si === 0 ? 0.6 : 0.36;
      scene.add(m);
      shells.push(m);
    });

    // Layer 2b: smoke coronas (wispy tendrils breaking the silhouette).
    const coronas: { mesh: THREE.Mesh; spin: number }[] = [];
    [
      { scale: 1.18, alpha: 0.6, spin: -0.35 },
      { scale: 1.42, alpha: 0.3, spin: 0.22 },
    ].forEach((c) => {
      const mat = new THREE.ShaderMaterial({
        vertexShader: SMOKE_VERT,
        fragmentShader: SMOKE_FRAG,
        uniforms: {
          uTime: { value: 0 },
          uTurb: { value: 0.35 },
          uLevel: { value: 0 },
          uAlpha: { value: c.alpha },
          uPalSmokeA: { value: v3(pal.mid) },
          uPalSmokeB: { value: v3(pal.energy) },
          uFlame: { value: pal.flame },
        },
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      });
      const mesh = new THREE.Mesh(
        new THREE.SphereGeometry(c.scale, Math.floor(cfg.sphereSeg * 0.6), Math.floor(cfg.sphereSeg * 0.45)),
        mat
      );
      mesh.userData.spin = c.spin;
      mesh.userData.baseAlpha = c.alpha;
      scene.add(mesh);
      coronas.push({ mesh, spin: c.spin });
    });

    // Layer 6: atmospheric halo (behind everything).
    const haloUniforms = {
      uIntensity: { value: 0.3 },
      uPulse: { value: 0 },
      uTint: { value: v3(pal.halo) },
      uError: { value: 0 },
    };
    const halo = new THREE.Mesh(
      new THREE.PlaneGeometry(5.2, 5.2),
      new THREE.ShaderMaterial({
        vertexShader: HALO_VERT,
        fragmentShader: HALO_FRAG,
        uniforms: haloUniforms,
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      })
    );
    halo.position.z = -1.4;
    scene.add(halo);

    // Layer 4: orbital arcs (partial sweeps, depth-layered).
    const arcDefs = [
      { radius: 0.7, start: 0.4, sweep: 3.9, incl: [0.52, 0.18, 0.1] as const, speed: 0.3 },
      { radius: 0.79, start: 2.5, sweep: 2.7, incl: [-0.38, 0.62, -0.18] as const, speed: -0.2 },
      { radius: 0.62, start: 4.3, sweep: 4.6, incl: [0.22, -0.48, 0.34] as const, speed: 0.42 },
    ];
    const ringGroups: THREE.Group[] = [];
    const ringMats: THREE.LineBasicMaterial[] = [];
    arcDefs.forEach((def, i) => {
      const pts: THREE.Vector3[] = [];
      const segs = 120;
      for (let s = 0; s <= segs; s++) {
        const a = def.start + (s / segs) * def.sweep;
        pts.push(new THREE.Vector3(Math.cos(a) * def.radius, Math.sin(a) * def.radius, 0));
      }
      const geo = new THREE.BufferGeometry().setFromPoints(pts);
      const mat = new THREE.LineBasicMaterial({
        color: new THREE.Color(...(i === 1 ? pal.ringAlt : pal.ring)),
        transparent: true,
        opacity: 0.05,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      });
      const line = new THREE.Line(geo, mat);
      const g = new THREE.Group();
      g.add(line);
      g.rotation.set(def.incl[0], def.incl[1], def.incl[2]);
      g.userData.speed = def.speed;
      g.userData.dir = i % 2 === 0 ? 1 : -1;
      scene.add(g);
      ringGroups.push(g);
      ringMats.push(mat);
    });

    // Layer 5: particle field (GPU orbits).
    const seedArr = new Float32Array(cfg.particles * 4);
    for (let i = 0; i < cfg.particles; i++) {
      seedArr[i * 4] = 0.62 + Math.random() * 0.75;
      seedArr[i * 4 + 1] = Math.random() * Math.PI * 2;
      seedArr[i * 4 + 2] = 0.25 + Math.random() * 0.9;
      seedArr[i * 4 + 3] = 1.6 + Math.random() * 3.4;
    }
    const pointsGeo = new THREE.BufferGeometry();
    pointsGeo.setAttribute("position", new THREE.BufferAttribute(new Float32Array(cfg.particles * 3), 3));
    pointsGeo.setAttribute("aSeed", new THREE.BufferAttribute(seedArr, 4));
    const pointsUniforms = {
      uTime: { value: 0 },
      uEnergy: { value: 0.3 },
      uPixelRatio: { value: pixelRatio },
      uAlpha: { value: 0.85 },
      uTint: { value: v3(pal.particle) },
      uRise: { value: pal.flame },
    };
    const points = new THREE.Points(
      pointsGeo,
      new THREE.ShaderMaterial({
        vertexShader: POINTS_VERT,
        fragmentShader: POINTS_FRAG,
        uniforms: pointsUniforms,
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
      })
    );
    scene.add(points);

    // Layer 7: holo markers (contextual HUD arcs).
    const holoMats: THREE.LineBasicMaterial[] = [];
    const holoGroup = new THREE.Group();
    for (let k = 0; k < 4; k++) {
      const pts: THREE.Vector3[] = [];
      const start = (k * Math.PI) / 2 + 0.25;
      for (let s = 0; s <= 40; s++) {
        const a = start + (s / 40) * 0.9;
        pts.push(new THREE.Vector3(Math.cos(a) * 0.9, Math.sin(a) * 0.9, 0));
      }
      const mat = new THREE.LineBasicMaterial({
        color: new THREE.Color(0.6, 0.95, 1.0),
        transparent: true,
        opacity: 0,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      });
      const line = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints(pts),
        mat
      );
      line.userData.dir = k % 2 === 0 ? 1 : -1;
      line.userData.phase = (k * Math.PI) / 2;
      holoGroup.add(line);
      holoMats.push(mat);
    }
    scene.add(holoGroup);

    /* ---------------- state machine visuals (interpolated) ---------------- */
    const cur: OrbStateVisuals = { ...STATE_VISUALS.idle };
    let autoTimer: ReturnType<typeof setTimeout> | null = null;
    let lastState: OrbState = stateRef.current;
    const applyAuto = (s: OrbState) => {
      if (autoTimer) clearTimeout(autoTimer);
      const adv = AUTO_ADVANCE[s];
      if (adv) {
        autoTimer = setTimeout(() => {
          if (canTransition(stateRef.current, adv.to)) stateRef.current = adv.to;
        }, adv.afterMs);
      }
    };
    applyAuto(lastState);

    const onContextLost = (e: Event) => {
      e.preventDefault();
      failOrb(new Error("webglcontextlost"));
    };
    canvas.addEventListener("webglcontextlost", onContextLost);

    /* ---------------- frame loop ---------------- */
    let startT = performance.now();
    let lastT = startT;

    const frame = (now: number) => {
      if (!running || disposed || failedRef.current) return;
      rafId = requestAnimationFrame(frame);
      const dt = Math.min(0.05, (now - lastT) / 1000);
      lastT = now;
      const t = (now - startT) / 1000;

      const s = stateRef.current;
      if (s !== lastState) {
        if (!canTransition(lastState, s)) {
          stateRef.current = "idle";
        } else {
          applyAuto(s);
        }
        lastState = stateRef.current;
      }
      const target = STATE_VISUALS[stateRef.current];
      const k = 1 - Math.exp(-dt * 5);
      (Object.keys(cur) as (keyof OrbStateVisuals)[]).forEach((key) => {
        cur[key] += (target[key] - cur[key]) * k;
      });

      const levels = analyzerRef.current?.read() ?? { level: 0, bass: 0, mid: 0, high: 0 };
      const shimmer = 0.5 + 0.5 * Math.sin(t * 1.7);
      const lvl = Math.max(levels.level, stateRef.current === "idle" ? 0.12 * shimmer : 0);

      px += (tx - px) * (1 - Math.exp(-dt * 3));
      py += (ty - py) * (1 - Math.exp(-dt * 3));
      camera.position.x = px;
      camera.position.y = py;
      camera.lookAt(0, 0, 0);

      const spin = t * 0.12 * cur.ringSpin;

      // Core
      const breathe = 1 + 0.028 * Math.sin(t * 2.1) * cur.scalePulse + lvl * 0.06;
      core.scale.setScalar(0.98 * breathe);
      core.rotation.y = t * 0.1 * cur.turbulence;
      Object.assign(coreUniforms.uTime, { value: t });
      coreUniforms.uDisplace.value = 0.06 + 0.12 * cur.turbulence * (0.4 + 0.6 * lvl) + levels.bass * 0.06;
      coreUniforms.uTurb.value = cur.turbulence;
      coreUniforms.uGlow.value = cur.coreGlow * (0.85 + 0.45 * lvl);
      coreUniforms.uLevel.value = lvl;
      coreUniforms.uBass.value = levels.bass;
      coreUniforms.uHigh.value = levels.high;
      coreUniforms.uError.value = cur.errorTint;

      // Shells + coronas
      shells.forEach((m, si) => {
        m.rotation.y = spin * (si + 1) * 0.6;
        const su = (m.material as THREE.ShaderMaterial).uniforms;
        su.uTime.value = t;
        su.uAlpha.value =
          (si === 0 ? 0.28 : 0.12) * (0.5 + 0.5 * cur.coreGlow);
      });
      coronas.forEach(({ mesh }, ci) => {
        mesh.rotation.y = spin * mesh.userData.spin * 4.0 + t * 0.02 * mesh.userData.spin;
        const u = (mesh.material as THREE.ShaderMaterial).uniforms;
        u.uTime.value = t * (ci === 1 ? 1.35 : 1.0);
        u.uTurb.value = cur.turbulence;
        u.uLevel.value = lvl;
        u.uAlpha.value = (mesh.userData.baseAlpha as number) * (0.5 + 0.5 * cur.coreGlow);
      });

      // Halo — restrained; the reference has almost no outer glow.
      haloUniforms.uIntensity.value = 0.22 * cur.halo * (0.8 + 0.5 * lvl);
      haloUniforms.uPulse.value = lvl;
      haloUniforms.uError.value = cur.errorTint;

      // Rings are contextual processing indicators only (reference image is
      // law): invisible at rest, rising faintly while thinking.
      const ringVis = Math.min(1, cur.ringSpin / 2.0);
      ringGroups.forEach((g, i) => {
        g.rotation.z += dt * g.userData.speed * cur.ringSpin * (g.userData.dir as number);
        ringMats[i].opacity = Math.min(0.5, 0.36 * ringVis * ringVis + lvl * 0.08);
      });

      // Particles
      pointsUniforms.uTime.value = t;
      pointsUniforms.uEnergy.value = cur.particleEnergy * (0.6 + 0.8 * lvl);

      // Holo markers
      holoGroup.children.forEach((line) => {
        line.rotation.z = t * (0.6 + cur.ringSpin * 0.5) * (line.userData.dir as number) + (line.userData.phase as number);
      });
      const holoA = cur.holoMarkers * 0.8;
      holoMats.forEach((m) => { m.opacity = holoA; });
      if (cur.errorTint > 0.5) holoMats.forEach((m) => m.color.setRGB(1.0, 0.55, 0.2));
      else holoMats.forEach((m) => m.color.setRGB(0.6, 0.95, 1.0));

      renderer!.render(scene, camera);
    };

    const onVis = () => {
      if (document.hidden) {
        running = false;
        cancelAnimationFrame(rafId);
      } else if (!disposed && !failedRef.current && !reducedMotion.current) {
        running = true;
        lastT = performance.now();
        rafId = requestAnimationFrame(frame);
      }
    };
    document.addEventListener("visibilitychange", onVis);

    wrap.dataset.orbStatus = "webgl-three";
    if (reducedMotion.current) {
      // One elegant static frame — no animation loop.
      frame(performance.now() + 4000);
    } else {
      rafId = requestAnimationFrame(frame);
    }

    return () => {
      disposed = true;
      running = false;
      cancelAnimationFrame(rafId);
      if (autoTimer) clearTimeout(autoTimer);
      ro.disconnect();
      if (finePointer) window.removeEventListener("pointermove", onMove);
      document.removeEventListener("visibilitychange", onVis);
      canvas.removeEventListener("webglcontextlost", onContextLost);
      analyzerRef.current?.detach();
      scene.traverse((obj) => {
        const mesh = obj as THREE.Mesh;
        if (mesh.geometry) mesh.geometry.dispose();
        const mat = mesh.material as THREE.Material | THREE.Material[] | undefined;
        if (Array.isArray(mat)) mat.forEach((m) => m.dispose());
        else if (mat) mat.dispose();
      });
      try { renderer?.dispose(); } catch { /* already torn down */ }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div ref={wrapRef} className="jarvis-orb-wrap">
      <button
        type="button"
        className={`jarvis-orb orb-${stateRef.current}`}
        onClick={onTap}
        aria-label={label}
        disabled={disabled}
      >
        <canvas ref={canvasRef} className="jarvis-orb-canvas" aria-hidden="true" />
      </button>
    </div>
  );
}
