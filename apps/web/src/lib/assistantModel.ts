import type { OrbTheme } from "../components/JarvisOrb";

/**
 * Assistant model identity — single source of truth.
 *
 * - `forgeos.assistantModel` in localStorage: "draven" | "calcifer" (default "draven").
 * - The orb theme derives from it (draven -> jarvis cyan, calcifer -> fire).
 * - The frontend sends it as `persona` on every POST /draven/chat; the backend
 *   uses it for the identity reply and the LLM system prompt only.
 * - Tool behavior, safety rules, approvals, and response substance are
 *   identical for both models.
 */

export type AssistantModel = "draven" | "calcifer";

export const ASSISTANT_MODEL_KEY = "forgeos.assistantModel";

export function readAssistantModel(): AssistantModel {
  try {
    return window.localStorage.getItem(ASSISTANT_MODEL_KEY) === "calcifer"
      ? "calcifer"
      : "draven";
  } catch {
    return "draven";
  }
}

export function writeAssistantModel(model: AssistantModel): void {
  try {
    window.localStorage.setItem(ASSISTANT_MODEL_KEY, model);
  } catch {
    // Storage unavailable (private mode) — the choice simply won't persist.
  }
}

/**
 * Time-aware salutation from the browser's local time.
 * Business manners: never "good night" — the assistant isn't signing off,
 * so late night stays "Good evening".
 */
export function timeSalutation(d: Date = new Date()): string {
  const h = d.getHours();
  if (h >= 5 && h < 12) return "Good morning";
  if (h >= 12 && h < 17) return "Good afternoon";
  return "Good evening";
}

/** Opening message for the Draven screen: salutation + persona identity. */
export function greetingFor(model: AssistantModel, d: Date = new Date()): string {
  const s = timeSalutation(d);
  return model === "calcifer"
    ? `${s}! Calcifer here — a living fire model at your service. Tap the mic and talk to me: approvals, campaigns, performance, whatever you need. I'll keep the flames steady and show my work as I go.`
    : `${s} — I'm Draven, your voice-first executive assistant inside ForgeOS. Tap the mic and talk to me — ask about approvals, campaigns, performance, or tell me what to prepare. I'll show my work as I go.`;
}

/** Client IANA timezone for the backend's time-aware identity reply. */
export function clientTimeZone(): string | undefined {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch {
    return undefined;
  }
}

export interface AssistantModelMeta {
  id: AssistantModel;
  name: string;
  theme: OrbTheme;
  tagline: string;
  /** One-line personality description shown in the picker. */
  description: string;
  /** CSS for the small orb preview dot in the picker. */
  orbCss: string;
}

export const ASSISTANT_MODELS: Record<AssistantModel, AssistantModelMeta> = {
  draven: {
    id: "draven",
    name: "Draven",
    theme: "jarvis",
    tagline: "Executive assistant",
    description: "The calm, precise executive — cyan orb, straight answers.",
    orbCss:
      "radial-gradient(circle at 35% 35%, #a8ecff, #12a3e8 55%, #05283f)",
  },
  calcifer: {
    id: "calcifer",
    name: "Calcifer",
    theme: "calcifer",
    tagline: "Living fire model",
    description:
      "A living fire spirit — warm, loyal, energetic. Same tools, same rules.",
    orbCss:
      "radial-gradient(circle at 35% 35%, #ffe9a8, #ff7a1a 55%, #6e1a04)",
  },
};
