/**
 * Typed API client for the ForgeOS backend (see ../../CONTRACTS.md).
 *
 * - Base URL from `VITE_API_URL`, defaulting to http://localhost:8000.
 * - JWT Bearer token persisted in localStorage under TOKEN_KEY.
 * - All responses are typed; unknown payloads are narrowed, never `any`.
 */

const API_BASE = (
  import.meta.env.VITE_API_URL as string | undefined
)?.replace(/\/+$/, "") || "http://localhost:8000";

const API_PREFIX = "/api/v1";

export const TOKEN_KEY = "forge_token";

/* ------------------------------------------------------------------ */
/* Errors                                                              */
/* ------------------------------------------------------------------ */

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown) {
    super(ApiError.messageFor(status, detail));
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }

  private static messageFor(status: number, detail: unknown): string {
    if (typeof detail === "string" && detail.length > 0) return detail;
    if (detail !== null && typeof detail === "object") {
      const d = detail as Record<string, unknown>;
      if (typeof d["message"] === "string") return d["message"];
    }
    return `Request failed with status ${status}`;
  }
}

/* ------------------------------------------------------------------ */
/* Low-level fetch                                                     */
/* ------------------------------------------------------------------ */

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string | null): void {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  params?: Record<string, string | number | boolean | undefined | null>;
  /** Skip attaching the auth header (login/register). */
  unauthenticated?: boolean;
}

function buildUrl(path: string, params?: RequestOptions["params"]): string {
  const url = new URL(`${API_BASE}${API_PREFIX}${path}`);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== "") {
        url.searchParams.set(key, String(value));
      }
    }
  }
  return url.toString();
}

export async function apiFetch<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { method = "GET", body, params, unauthenticated = false } = options;
  const headers: Record<string, string> = {
    Accept: "application/json",
  };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (!unauthenticated) {
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }

  let res: Response;
  try {
    res = await fetch(buildUrl(path, params), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (err) {
    throw new ApiError(
      0,
      err instanceof Error
        ? `Network error: ${err.message}. Is the API reachable at ${API_BASE}?`
        : "Network error: unable to reach the API.",
    );
  }

  if (res.status === 401 && !unauthenticated) {
    // Token is invalid/expired — drop it so the app redirects to /login.
    setToken(null);
  }

  let payload: unknown = null;
  const contentType = res.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    try {
      payload = (await res.json()) as unknown;
    } catch {
      payload = null;
    }
  } else {
    const text = await res.text().catch(() => "");
    payload = text.length > 0 ? text : null;
  }

  if (!res.ok) {
    const detail =
      payload !== null && typeof payload === "object"
        ? ((payload as Record<string, unknown>)["detail"] ?? payload)
        : (payload ?? `HTTP ${res.status}`);
    throw new ApiError(res.status, detail);
  }

  return payload as T;
}

/** Normalize list responses: either a bare array or {items, total}. */
export function asItems<T>(data: unknown): T[] {
  return asPage<T>(data).items;
}

export interface Page<T> {
  items: T[];
  total: number;
}

/** Normalize paginated responses, preserving the server-reported total. */
export function asPage<T>(data: unknown): Page<T> {
  if (Array.isArray(data)) {
    const items = data as T[];
    return { items, total: items.length };
  }
  if (data !== null && typeof data === "object") {
    const record = data as Record<string, unknown>;
    const items = Array.isArray(record["items"]) ? (record["items"] as T[]) : [];
    const total =
      typeof record["total"] === "number" ? record["total"] : items.length;
    return { items, total };
  }
  return { items: [], total: 0 };
}

/* ------------------------------------------------------------------ */
/* Entity types (mirror CONTRACTS.md)                                  */
/* ------------------------------------------------------------------ */

export type Role = "owner" | "admin" | "member";

export interface User {
  id: string;
  email: string;
  full_name: string;
  business_id: string;
  role: Role;
  is_active: boolean;
}

export interface Business {
  id: string;
  name: string;
  slug: string;
  timezone: string;
}

export interface BrandKit {
  id: string;
  business_id: string;
  name: string;
  voice_description: string | null;
  tone_tags: string[];
  primary_color: string | null;
  secondary_color: string | null;
  fonts: Record<string, string>;
  icp_description: string | null;
  do_list: string[];
  dont_list: string[];
  version: number;
}

export interface Contact {
  id: string;
  email: string | null;
  phone: string | null;
  first_name: string;
  last_name: string;
  source: string | null;
  tags: string[];
  consent_email: boolean;
  consent_sms: boolean;
  unsubscribed: boolean;
}

export type AssetKind =
  | "email_copy"
  | "social_post"
  | "sms"
  | "blog"
  | "ad"
  | "image_prompt";
export type AssetStatus = "draft" | "in_review" | "approved" | "rejected";

export interface Asset {
  id: string;
  business_id: string;
  kind: AssetKind;
  title: string;
  body: string | null;
  variables: Record<string, unknown>;
  version: number;
  status: AssetStatus;
  brand_kit_version: number;
  created_by: string;
  approved_by: string | null;
  rejection_reason: string | null;
  parent_asset_id: string | null;
  cost_usd: number;
  tokens_in: number;
  tokens_out: number;
  llm_provider: string | null;
  llm_model: string | null;
  created_at: string;
}

export interface AssetVersion {
  id: string;
  version: number;
  status: AssetStatus;
  title: string;
  body: string | null;
  created_at: string;
}

export type Channel = "email" | "sms" | "social";

export interface Template {
  id: string;
  business_id: string;
  name: string;
  channel: Channel;
  subject_template: string | null;
  body_template: string;
  variables: string[];
}

export type CampaignStatus =
  | "draft"
  | "scheduled"
  | "running"
  | "paused"
  | "completed";

export interface Campaign {
  id: string;
  business_id: string;
  name: string;
  description: string | null;
  status: CampaignStatus;
  autopilot: boolean;
  created_by: string;
  starts_at: string | null;
  timezone: string;
  created_at: string;
}

export interface CampaignStep {
  id: string;
  campaign_id: string;
  position: number;
  channel: Channel;
  template_id: string | null;
  asset_id: string | null;
  delay_hours: number;
  trigger_event: string | null;
}

export type EnrollmentStatus = "active" | "paused" | "completed" | "unsubscribed";

export interface Enrollment {
  id: string;
  campaign_id: string;
  contact_id: string;
  current_step: number;
  status: EnrollmentStatus;
  next_run_at: string | null;
}

export interface AutopilotSettings {
  business_id: string;
  auto_approve: boolean;
  require_approval_for_channels: string[];
  daily_send_cap: number;
  quiet_hours_start: number;
  quiet_hours_end: number;
}

export interface DayStat {
  date: string;
  sent: number;
  delivered?: number;
  opened?: number;
  clicked?: number;
  converted?: number;
}

export interface AnalyticsOverview {
  sent: number;
  delivered: number;
  opened: number;
  clicked: number;
  converted: number;
  open_rate: number;
  ctr: number;
  conversion_rate: number;
  spend_usd: number;
  by_day: DayStat[];
}

export interface FunnelStep {
  step_id: string;
  position: number;
  sent: number;
  opened: number;
  clicked: number;
}

export interface OutboxMessage {
  id: string;
  business_id: string;
  channel: string;
  to_address: string;
  subject: string | null;
  body: string;
  provider: string;
  created_at: string;
}

export interface GenerateResult {
  asset_id: string;
  job_id: string;
}

/* ------------------------------------------------------------------ */
/* Endpoint functions                                                  */
/* ------------------------------------------------------------------ */

export const authApi = {
  async login(email: string, password: string): Promise<string> {
    // OAuth2 password form per contract.
    const form = new URLSearchParams({ username: email, password });
    const headers: Record<string, string> = {
      "Content-Type": "application/x-www-form-urlencoded",
      Accept: "application/json",
    };
    let res: Response;
    try {
      res = await fetch(`${API_BASE}${API_PREFIX}/auth/login`, {
        method: "POST",
        headers,
        body: form.toString(),
      });
    } catch (err) {
      throw new ApiError(
        0,
        err instanceof Error
          ? `Network error: ${err.message}. Is the API reachable at ${API_BASE}?`
          : "Network error: unable to reach the API.",
      );
    }
    const payload = (await res.json().catch(() => null)) as unknown;
    if (!res.ok) {
      const detail =
        payload !== null && typeof payload === "object"
          ? ((payload as Record<string, unknown>)["detail"] ?? "Login failed")
          : "Login failed";
      throw new ApiError(res.status, detail);
    }
    const token = (payload as Record<string, unknown>)["access_token"];
    if (typeof token !== "string" || token.length === 0) {
      throw new ApiError(res.status, "Login succeeded but no token was returned.");
    }
    setToken(token);
    return token;
  },

  async register(input: {
    email: string;
    password: string;
    full_name: string;
    business_name: string;
  }): Promise<{ token: string; user: User }> {
    const data = await apiFetch<{ token: string; user: User }>("/auth/register", {
      method: "POST",
      body: input,
      unauthenticated: true,
    });
    setToken(data.token);
    return data;
  },

  me(): Promise<User> {
    return apiFetch<User>("/auth/me");
  },

  logout(): void {
    setToken(null);
  },
};

export const businessApi = {
  getMe(): Promise<Business> {
    return apiFetch<Business>("/businesses/me");
  },
  update(data: Partial<Pick<Business, "name" | "timezone">>): Promise<Business> {
    return apiFetch<Business>("/businesses/me", { method: "PUT", body: data });
  },
};

export const brandKitApi = {
  list(): Promise<BrandKit[]> {
    return apiFetch<unknown>("/brand-kits").then(asItems<BrandKit>);
  },
  create(data: Partial<BrandKit>): Promise<BrandKit> {
    return apiFetch<BrandKit>("/brand-kits", { method: "POST", body: data });
  },
  update(id: string, data: Partial<BrandKit>): Promise<BrandKit> {
    return apiFetch<BrandKit>(`/brand-kits/${id}`, { method: "PUT", body: data });
  },
};

export const contactApi = {
  list(params?: { limit?: number; offset?: number }): Promise<Contact[]> {
    return apiFetch<unknown>("/contacts", { params }).then(asItems<Contact>);
  },
};

export const templateApi = {
  list(): Promise<Template[]> {
    return apiFetch<unknown>("/templates").then(asItems<Template>);
  },
};

export const assetApi = {
  list(params?: {
    status?: AssetStatus | "";
    kind?: AssetKind | "";
    limit?: number;
  }): Promise<Asset[]> {
    return apiFetch<unknown>("/assets", { params }).then(asItems<Asset>);
  },
  /** Same as list(), but preserves the server-reported total for counts. */
  listPage(params?: {
    status?: AssetStatus | "";
    kind?: AssetKind | "";
    limit?: number;
  }): Promise<Page<Asset>> {
    return apiFetch<unknown>("/assets", { params }).then(asPage<Asset>);
  },
  get(id: string): Promise<Asset> {
    return apiFetch<Asset>(`/assets/${id}`);
  },
  generate(input: {
    kind: AssetKind;
    title: string;
    template_id?: string;
    prompt?: string;
    variables?: Record<string, string>;
  }): Promise<GenerateResult> {
    return apiFetch<GenerateResult>("/assets/generate", {
      method: "POST",
      body: input,
    });
  },
  submit(id: string): Promise<Asset> {
    return apiFetch<Asset>(`/assets/${id}/submit`, { method: "POST" });
  },
  approve(id: string, note?: string): Promise<Asset> {
    return apiFetch<Asset>(`/assets/${id}/approve`, {
      method: "POST",
      body: { note: note ?? null },
    });
  },
  reject(id: string, reason: string): Promise<Asset> {
    return apiFetch<Asset>(`/assets/${id}/reject`, {
      method: "POST",
      body: { reason },
    });
  },
  versions(id: string): Promise<AssetVersion[]> {
    return apiFetch<unknown>(`/assets/${id}/versions`).then(asItems<AssetVersion>);
  },
};

export interface StepInput {
  channel: Channel;
  template_id?: string | null;
  asset_id?: string | null;
  delay_hours: number;
  trigger_event?: string | null;
  position?: number;
}

export const campaignApi = {
  list(): Promise<Campaign[]> {
    return apiFetch<unknown>("/campaigns").then(asItems<Campaign>);
  },
  create(data: {
    name: string;
    description?: string;
    timezone?: string;
    starts_at?: string | null;
    autopilot?: boolean;
  }): Promise<Campaign> {
    return apiFetch<Campaign>("/campaigns", { method: "POST", body: data });
  },
  get(id: string): Promise<Campaign & { steps?: CampaignStep[] }> {
    return apiFetch<Campaign & { steps?: CampaignStep[] }>(`/campaigns/${id}`);
  },
  update(id: string, data: Partial<Campaign>): Promise<Campaign> {
    return apiFetch<Campaign>(`/campaigns/${id}`, { method: "PUT", body: data });
  },
  /** Replace the full step list (positions are derived from array order). */
  saveSteps(id: string, steps: StepInput[]): Promise<unknown> {
    const payload = steps.map((s, i) => ({ ...s, position: i }));
    return apiFetch<unknown>(`/campaigns/${id}/steps`, {
      method: "POST",
      body: { steps: payload },
    });
  },
  updateStep(
    id: string,
    stepId: string,
    data: Partial<StepInput>,
  ): Promise<CampaignStep> {
    return apiFetch<CampaignStep>(`/campaigns/${id}/steps/${stepId}`, {
      method: "PUT",
      body: data,
    });
  },
  launch(id: string): Promise<Campaign> {
    return apiFetch<Campaign>(`/campaigns/${id}/launch`, { method: "POST" });
  },
  pause(id: string): Promise<Campaign> {
    return apiFetch<Campaign>(`/campaigns/${id}/pause`, { method: "POST" });
  },
  enrollments(id: string): Promise<Enrollment[]> {
    return apiFetch<unknown>(`/campaigns/${id}/enrollments`).then(
      asItems<Enrollment>,
    );
  },
};

export const autopilotApi = {
  get(): Promise<AutopilotSettings> {
    return apiFetch<AutopilotSettings>("/autopilot");
  },
  update(data: Partial<AutopilotSettings>): Promise<AutopilotSettings> {
    return apiFetch<AutopilotSettings>("/autopilot", {
      method: "PUT",
      body: data,
    });
  },
};

export const analyticsApi = {
  overview(params?: { days?: number; campaign_id?: string }): Promise<AnalyticsOverview> {
    return apiFetch<AnalyticsOverview>("/analytics/overview", { params });
  },
  funnel(campaignId: string): Promise<FunnelStep[]> {
    return apiFetch<unknown>(`/analytics/campaigns/${campaignId}/funnel`).then(
      asItems<FunnelStep>,
    );
  },
};

export const outboxApi = {
  list(limit = 50): Promise<OutboxMessage[]> {
    return apiFetch<unknown>("/dev/outbox", { params: { limit } }).then(
      asItems<OutboxMessage>,
    );
  },
};

/* ------------------------------------------------------------------ */
/* Ops / mission control (read-only aggregate)                         */
/* ------------------------------------------------------------------ */

export type OpsActivityKind = "generation" | "send" | "event";

export interface OpsActivityItem {
  id: string;
  kind: OpsActivityKind;
  title: string;
  detail: string | null;
  status: string | null;
  at: string;
}

export interface OpsCounters {
  sends_today: number;
  generations_today: number;
  in_flight: number;
}

export interface OpsActivityResponse {
  as_of: string;
  stages: Record<string, Record<string, number>>;
  counters: OpsCounters;
  activity: OpsActivityItem[];
}

export const opsApi = {
  activity(limit = 30): Promise<OpsActivityResponse> {
    return apiFetch<OpsActivityResponse>("/ops/activity", {
      params: { limit },
    });
  },
};
