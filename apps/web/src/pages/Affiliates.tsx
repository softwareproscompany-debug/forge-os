import { useCallback, useEffect, useState } from "react";
import { affiliatesApi } from "../lib/api";
import type {
  AffiliateEarnings,
  AffiliateLink,
  AffiliateProgram,
} from "../lib/api";
import { formatMoney, formatNumber, formatPercent } from "../lib/format";
import {
  Badge,
  EmptyState,
  ErrorBanner,
  Field,
  Modal,
  PageHeader,
  Spinner,
  StatCard,
  errorMessage,
} from "../components/ui";

const DAY_OPTIONS = [7, 14, 30, 90];
const SLUG_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

function slugify(label: string): string {
  return label
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export default function AffiliatesPage() {
  const [days, setDays] = useState(30);
  const [programs, setPrograms] = useState<AffiliateProgram[]>([]);
  const [links, setLinks] = useState<AffiliateLink[]>([]);
  const [earnings, setEarnings] = useState<AffiliateEarnings | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [showProgramForm, setShowProgramForm] = useState(false);
  const [showLinkForm, setShowLinkForm] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [progs, lks, earn] = await Promise.all([
        affiliatesApi.listPrograms(),
        affiliatesApi.listLinks(),
        affiliatesApi.earnings(days),
      ]);
      setPrograms(progs);
      setLinks(lks);
      setEarnings(earn);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [days]);

  useEffect(() => {
    void load();
  }, [load]);

  const copyShortUrl = useCallback(async (slug: string) => {
    const url = affiliatesApi.shortUrl(slug);
    try {
      await navigator.clipboard.writeText(url);
    } catch {
      // Clipboard API unavailable (permissions); fall back silently.
    }
    setCopied(slug);
    window.setTimeout(() => setCopied((cur) => (cur === slug ? null : cur)), 1500);
  }, []);

  const toggleProgram = useCallback(
    async (program: AffiliateProgram) => {
      try {
        await affiliatesApi.updateProgram(program.id, {
          status: program.status === "active" ? "paused" : "active",
        });
        await load();
      } catch (err) {
        setError(err);
      }
    },
    [load],
  );

  const deleteProgram = useCallback(
    async (program: AffiliateProgram) => {
      if (
        !window.confirm(
          `Delete program "${program.name}" and all its links? This cannot be undone.`,
        )
      )
        return;
      try {
        await affiliatesApi.deleteProgram(program.id);
        await load();
      } catch (err) {
        setError(err);
      }
    },
    [load],
  );

  const toggleLink = useCallback(
    async (link: AffiliateLink) => {
      try {
        await affiliatesApi.updateLink(link.id, { is_active: !link.is_active });
        await load();
      } catch (err) {
        setError(err);
      }
    },
    [load],
  );

  const deleteLink = useCallback(
    async (link: AffiliateLink) => {
      if (!window.confirm(`Delete link "${link.label}"? This cannot be undone.`))
        return;
      try {
        await affiliatesApi.deleteLink(link.id);
        await load();
      } catch (err) {
        setError(err);
      }
    },
    [load],
  );

  if (loading) return <Spinner label="Loading affiliates…" />;

  const totals = earnings?.totals;
  const stats: { label: string; value: number; format: (n: number) => string }[] =
    totals
      ? [
          {
            label: "Earnings",
            value: totals.earnings_usd,
            format: (n) => formatMoney(n),
          },
          {
            label: "Clicks",
            value: totals.clicks,
            format: (n) => formatNumber(n),
          },
          {
            label: "Conversions",
            value: totals.conversions,
            format: (n) => formatNumber(n),
          },
          {
            label: "Conversion rate",
            value: totals.conversion_rate,
            format: (n) => formatPercent(n),
          },
        ]
      : [];

  return (
    <div>
      <PageHeader
        title="Affiliates"
        subtitle="Promote third-party offers, track clicks and commissions. Short links are served at /r/{slug}."
        actions={
          <>
            <button
              type="button"
              className="btn"
              onClick={() => setShowProgramForm(true)}
            >
              + Program
            </button>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => setShowLinkForm(true)}
              disabled={programs.length === 0}
              title={
                programs.length === 0
                  ? "Create a program first"
                  : "Create a trackable link"
              }
            >
              + Link
            </button>
          </>
        }
      />

      {error ? <ErrorBanner error={error} /> : null}

      <div className="stat-grid" role="region" aria-label="Affiliate totals">
        <div className="stat-grid-controls">
          {DAY_OPTIONS.map((d) => (
            <button
              key={d}
              type="button"
              className={`btn btn-sm${d === days ? " btn-active" : ""}`}
              onClick={() => setDays(d)}
            >
              {d}d
            </button>
          ))}
        </div>
        {stats.map((s) => (
          <StatCard
            key={s.label}
            label={s.label}
            value={s.value}
            format={s.format}
          />
        ))}
      </div>

      <section aria-label="Programs">
        <h2 className="section-title">Programs</h2>
        {programs.length === 0 ? (
          <EmptyState
            title="No affiliate programs yet"
            hint="Add the programs whose offers you promote — Amazon Associates, ShareASale, or a direct partnership."
            action={
              <button
                type="button"
                className="btn btn-primary"
                onClick={() => setShowProgramForm(true)}
              >
                Add your first program
              </button>
            }
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Network</th>
                  <th>Commission</th>
                  <th>Cookie</th>
                  <th>Status</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {programs.map((p) => (
                  <tr key={p.id}>
                    <td>{p.name}</td>
                    <td>{p.network}</td>
                    <td>{p.default_commission_pct}%</td>
                    <td>{p.cookie_days != null ? `${p.cookie_days}d` : "—"}</td>
                    <td>
                      <Badge value={p.status} />
                    </td>
                    <td className="row-actions">
                      <button
                        type="button"
                        className="btn btn-sm"
                        onClick={() => void toggleProgram(p)}
                      >
                        {p.status === "active" ? "Pause" : "Resume"}
                      </button>
                      <button
                        type="button"
                        className="btn btn-sm btn-danger"
                        onClick={() => void deleteProgram(p)}
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section aria-label="Links">
        <h2 className="section-title">Trackable links</h2>
        {links.length === 0 ? (
          <EmptyState
            title="No links yet"
            hint="Create a short link for an offer. Share the /r/{slug} URL — clicks are tracked automatically."
          />
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Label</th>
                  <th>Short link</th>
                  <th>Clicks</th>
                  <th>Conv.</th>
                  <th>Earnings</th>
                  <th>Status</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {links.map((link) => {
                  const stat = earnings?.per_link.find(
                    (s) => s.link_id === link.id,
                  );
                  return (
                    <tr key={link.id}>
                      <td>{link.label}</td>
                      <td>
                        <code className="mono">/r/{link.slug}</code>{" "}
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={() => void copyShortUrl(link.slug)}
                          title="Copy the full short-link URL"
                        >
                          {copied === link.slug ? "Copied ✓" : "Copy"}
                        </button>
                      </td>
                      <td>{formatNumber(stat?.clicks ?? 0)}</td>
                      <td>{formatNumber(stat?.conversions ?? 0)}</td>
                      <td>{formatMoney(stat?.earnings_usd ?? 0)}</td>
                      <td>
                        <Badge value={link.is_active ? "active" : "paused"} />
                      </td>
                      <td className="row-actions">
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={() => void toggleLink(link)}
                        >
                          {link.is_active ? "Deactivate" : "Activate"}
                        </button>
                        <button
                          type="button"
                          className="btn btn-sm btn-danger"
                          onClick={() => void deleteLink(link)}
                        >
                          Delete
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {showProgramForm && (
        <ProgramForm
          onClose={() => setShowProgramForm(false)}
          onSaved={() => {
            setShowProgramForm(false);
            void load();
          }}
        />
      )}
      {showLinkForm && (
        <LinkForm
          programs={programs}
          onClose={() => setShowLinkForm(false)}
          onSaved={() => {
            setShowLinkForm(false);
            void load();
          }}
        />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */

function ProgramForm({
  onClose,
  onSaved,
}: {
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState("");
  const [network, setNetwork] = useState("amazon");
  const [websiteUrl, setWebsiteUrl] = useState("");
  const [commission, setCommission] = useState("4");
  const [cookieDays, setCookieDays] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const pct = Number(commission);
      if (!Number.isFinite(pct) || pct < 0 || pct > 100) {
        throw new Error("Commission must be a number between 0 and 100.");
      }
      const cookies = cookieDays.trim() === "" ? null : Number(cookieDays);
      if (cookies !== null && (!Number.isInteger(cookies) || cookies < 0)) {
        throw new Error("Cookie days must be a whole number, 0 or more.");
      }
      await affiliatesApi.createProgram({
        name: name.trim(),
        network: network.trim() || "other",
        website_url: websiteUrl.trim() || null,
        default_commission_pct: pct,
        cookie_days: cookies,
      });
      onSaved();
    } catch (err) {
      setError(err);
      setSaving(false);
    }
  };

  return (
    <Modal title="New affiliate program" onClose={onClose}>
      <form onSubmit={(e) => void save(e)}>
        {error ? <ErrorBanner error={error} /> : null}
        <Field label="Program name">
          <input
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Demo Gear Picks"
            required
            maxLength={255}
            autoFocus
          />
        </Field>
        <Field label="Network">
          <select
            className="input"
            value={network}
            onChange={(e) => setNetwork(e.target.value)}
          >
            <option value="amazon">Amazon Associates</option>
            <option value="shareasale">ShareASale</option>
            <option value="cj">CJ Affiliate</option>
            <option value="impact">Impact</option>
            <option value="direct">Direct partnership</option>
            <option value="other">Other</option>
          </select>
        </Field>
        <Field label="Website URL" hint="The merchant's site, for reference.">
          <input
            className="input"
            value={websiteUrl}
            onChange={(e) => setWebsiteUrl(e.target.value)}
            placeholder="https://example.com"
            inputMode="url"
          />
        </Field>
        <Field
          label="Default commission %"
          hint="Used when a conversion postback doesn't carry its own amount."
        >
          <input
            className="input"
            value={commission}
            onChange={(e) => setCommission(e.target.value)}
            inputMode="decimal"
            required
          />
        </Field>
        <Field label="Cookie days" hint="Optional.">
          <input
            className="input"
            value={cookieDays}
            onChange={(e) => setCookieDays(e.target.value)}
            inputMode="numeric"
            placeholder="30"
          />
        </Field>
        <div className="modal-actions">
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn-primary" disabled={saving}>
            {saving ? "Saving…" : "Create program"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

/* ------------------------------------------------------------------ */

function LinkForm({
  programs,
  onClose,
  onSaved,
}: {
  programs: AffiliateProgram[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [programId, setProgramId] = useState(programs[0]?.id ?? "");
  const [label, setLabel] = useState("");
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [destinationUrl, setDestinationUrl] = useState("");
  const [utmCampaign, setUtmCampaign] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const onLabelChange = (value: string) => {
    setLabel(value);
    if (!slugTouched) setSlug(slugify(value));
  };

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const cleanSlug = slug.trim().toLowerCase();
      if (!SLUG_RE.test(cleanSlug)) {
        throw new Error(
          "Slug must be lowercase letters, numbers and hyphens (e.g. espresso-maker).",
        );
      }
      const url = destinationUrl.trim();
      if (!/^https?:\/\//i.test(url)) {
        throw new Error("Destination URL must start with http:// or https://.");
      }
      await affiliatesApi.createLink({
        program_id: programId,
        label: label.trim(),
        slug: cleanSlug,
        destination_url: url,
        utm_source: "forgeos",
        utm_medium: "affiliate",
        utm_campaign: utmCampaign.trim() || null,
      });
      onSaved();
    } catch (err) {
      setError(errorMessage(err));
      setSaving(false);
    }
  };

  return (
    <Modal title="New trackable link" onClose={onClose}>
      <form onSubmit={(e) => void save(e)}>
        {error ? <ErrorBanner error={error} /> : null}
        <Field label="Program">
          <select
            className="input"
            value={programId}
            onChange={(e) => setProgramId(e.target.value)}
            required
          >
            {programs.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Label" hint="What this offer is, for your reports.">
          <input
            className="input"
            value={label}
            onChange={(e) => onLabelChange(e.target.value)}
            placeholder="e.g. Espresso maker — spring deal"
            required
            maxLength={255}
            autoFocus
          />
        </Field>
        <Field
          label="Slug"
          hint="Lowercase letters, numbers and hyphens. Your short link becomes /r/{slug}."
        >
          <input
            className="input mono"
            value={slug}
            onChange={(e) => {
              setSlug(e.target.value);
              setSlugTouched(true);
            }}
            placeholder="espresso-maker"
            required
            maxLength={128}
          />
        </Field>
        <Field
          label="Destination URL"
          hint="The merchant URL with your affiliate ID/tag already in it."
        >
          <input
            className="input"
            value={destinationUrl}
            onChange={(e) => setDestinationUrl(e.target.value)}
            placeholder="https://www.amazon.com/dp/…?tag=your-tag-20"
            required
            inputMode="url"
          />
        </Field>
        <Field label="UTM campaign" hint="Optional. Source/medium default to forgeos/affiliate.">
          <input
            className="input"
            value={utmCampaign}
            onChange={(e) => setUtmCampaign(e.target.value)}
            placeholder="spring-deals"
            maxLength={128}
          />
        </Field>
        <div className="modal-actions">
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn-primary" disabled={saving}>
            {saving ? "Saving…" : "Create link"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
