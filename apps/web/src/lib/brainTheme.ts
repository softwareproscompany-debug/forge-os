/**
 * Shared brain graph theme — warm cluster colors on the dark command
 * center. One color per FORGE layer; link/timeline colors for edges.
 */

export const LAYER_COLORS: Record<string, string> = {
  foundation: "#f5a524",
  origination: "#5aa2ff",
  reach: "#f472b6",
  growth: "#a78bfa",
  evidence: "#4ade80",
};

export const LINK_COLORS: Record<string, string> = {
  brand: "#f5a524",
  asset: "#5aa2ff",
  campaign: "#f472b6",
  engagement: "#4ade80",
};

export const TIMELINE_COLORS: Record<string, string> = {
  sent: "#f5a524",
  opened: "#5aa2ff",
  clicked: "#f472b6",
  converted: "#4ade80",
  event: "#9c978b",
};

export const BRAIN_VIEWS = [
  "rings",
  "circle",
  "areas",
  "links",
  "timeline",
  "orbit",
] as const;

export type BrainView = (typeof BRAIN_VIEWS)[number];

export const BRAIN_VIEW_LABELS: Record<BrainView, string> = {
  rings: "RINGS",
  circle: "CIRCLE",
  areas: "AREAS",
  links: "LINKS",
  timeline: "TIMELINE",
  orbit: "3D ORBIT",
};
