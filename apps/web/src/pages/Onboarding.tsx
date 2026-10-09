import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import {
  autopilotApi,
  brandKitApi,
  businessApi,
} from "../lib/api";
import type {
  AutopilotSettings,
  BrandKit,
  Business,
} from "../lib/api";
import { linesToList, listToLines } from "../lib/format";
import { errorMessage, Field, Spinner } from "../components/ui";

const STEPS = ["Business profile", "Brand kit", "Autopilot"] as const;

export default function OnboardingPage() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Step 1 — business
  const [business, setBusiness] = useState<Business | null>(null);
  const [bizName, setBizName] = useState("");
  const [bizTz, setBizTz] = useState("UTC");

  // Step 2 — brand kit
  const [brandKit, setBrandKit] = useState<BrandKit | null>(null);
  const [voice, setVoice] = useState("");
  const [tone, setTone] = useState("");
  const [primary, setPrimary] = useState("#7c5cff");
  const [secondary, setSecondary] = useState("#22d3ee");
  const [icp, setIcp] = useState("");
  const [doList, setDoList] = useState("");
  const [dontList, setDontList] = useState("");

  // Step 3 — autopilot
  const [auto, setAuto] = useState<AutopilotSettings | null>(null);
  const [autoApprove, setAutoApprove] = useState(false);
  const [approvalChannels, setApprovalChannels] = useState("");
  const [dailyCap, setDailyCap] = useState(500);
  const [quietStart, setQuietStart] = useState(22);
  const [quietEnd, setQuietEnd] = useState(8);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [b, kits, a] = await Promise.all([
          businessApi.getMe(),
          brandKitApi.list().catch(() => [] as BrandKit[]),
          autopilotApi.get().catch(() => null),
        ]);
        if (cancelled) return;
        setBusiness(b);
        setBizName(b.name);
        setBizTz(b.timezone || "UTC");
        const kit = kits[0] ?? null;
        setBrandKit(kit);
        if (kit) {
          setVoice(kit.voice_description ?? "");
          setTone((kit.tone_tags ?? []).join(", "));
          setPrimary(kit.primary_color ?? "#7c5cff");
          setSecondary(kit.secondary_color ?? "#22d3ee");
          setIcp(kit.icp_description ?? "");
          setDoList(listToLines(kit.do_list));
          setDontList(listToLines(kit.dont_list));
        }
        if (a) {
          setAuto(a);
          setAutoApprove(a.auto_approve);
          setApprovalChannels((a.require_approval_for_channels ?? []).join(", "));
          setDailyCap(a.daily_send_cap);
          setQuietStart(a.quiet_hours_start);
          setQuietEnd(a.quiet_hours_end);
        }
      } catch (err) {
        if (!cancelled) setError(errorMessage(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const saveStep = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setSaving(true);
    try {
      if (step === 0) {
        const updated = await businessApi.update({
          name: bizName.trim(),
          timezone: bizTz.trim() || "UTC",
        });
        setBusiness(updated);
      } else if (step === 1) {
        const payload = {
          name: `${bizName || business?.name || "Default"} brand kit`,
          voice_description: voice.trim() || null,
          tone_tags: tone.split(",").map((t) => t.trim()).filter(Boolean),
          primary_color: primary,
          secondary_color: secondary,
          icp_description: icp.trim() || null,
          do_list: linesToList(doList),
          dont_list: linesToList(dontList),
        };
        const saved = brandKit
          ? await brandKitApi.update(brandKit.id, payload)
          : await brandKitApi.create(payload);
        setBrandKit(saved);
      } else {
        const payload = {
          auto_approve: autoApprove,
          require_approval_for_channels: approvalChannels
            .split(",")
            .map((c) => c.trim().toLowerCase())
            .filter((c) => ["email", "sms", "social"].includes(c)),
          daily_send_cap: dailyCap,
          quiet_hours_start: quietStart,
          quiet_hours_end: quietEnd,
        };
        const saved = await autopilotApi.update(payload);
        setAuto(saved);
      }
      if (step < STEPS.length - 1) setStep(step + 1);
      else navigate("/", { replace: true });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <Spinner label="Loading workspace…" />;

  return (
    <div className="wizard">
      <h1>Set up ForgeOS</h1>
      <ol className="stepper" aria-label="Onboarding progress">
        {STEPS.map((label, i) => (
          <li
            key={label}
            className={`stepper-step${i === step ? " stepper-active" : ""}${
              i < step ? " stepper-done" : ""
            }`}
            aria-current={i === step ? "step" : undefined}
          >
            <span className="stepper-num">{i < step ? "✓" : i + 1}</span>
            {label}
          </li>
        ))}
      </ol>

      {error && (
        <div className="alert alert-error" role="alert" style={{ marginBottom: 16 }}>
          {error}
        </div>
      )}

      <form onSubmit={saveStep} className="card form">
        {step === 0 && (
          <>
            <Field label="Business name">
              <input
                required
                value={bizName}
                onChange={(e) => setBizName(e.target.value)}
              />
            </Field>
            <Field
              label="Timezone"
              hint="Used for scheduling campaign sends (IANA name, e.g. America/Chicago)."
            >
              <input
                value={bizTz}
                onChange={(e) => setBizTz(e.target.value)}
                placeholder="UTC"
              />
            </Field>
          </>
        )}

        {step === 1 && (
          <>
            <Field
              label="Brand voice"
              hint="How the brand sounds — the LLM uses this as its system prompt."
            >
              <textarea
                rows={3}
                value={voice}
                onChange={(e) => setVoice(e.target.value)}
                placeholder="Confident, plain-spoken, a little playful. Short sentences."
              />
            </Field>
            <Field label="Tone tags" hint="Comma-separated, e.g. friendly, direct, premium.">
              <input
                value={tone}
                onChange={(e) => setTone(e.target.value)}
                placeholder="friendly, direct"
              />
            </Field>
            <div className="grid-2">
              <Field label="Primary color">
                <div className="color-row">
                  <input
                    type="color"
                    value={primary}
                    onChange={(e) => setPrimary(e.target.value)}
                    aria-label="Primary color"
                  />
                  <input
                    value={primary}
                    onChange={(e) => setPrimary(e.target.value)}
                    spellCheck={false}
                  />
                </div>
              </Field>
              <Field label="Secondary color">
                <div className="color-row">
                  <input
                    type="color"
                    value={secondary}
                    onChange={(e) => setSecondary(e.target.value)}
                    aria-label="Secondary color"
                  />
                  <input
                    value={secondary}
                    onChange={(e) => setSecondary(e.target.value)}
                    spellCheck={false}
                  />
                </div>
              </Field>
            </div>
            <Field label="Ideal customer profile">
              <textarea
                rows={3}
                value={icp}
                onChange={(e) => setIcp(e.target.value)}
                placeholder="Who you're selling to and what they care about."
              />
            </Field>
            <div className="grid-2">
              <Field label="Do" hint="One rule per line.">
                <textarea
                  rows={4}
                  value={doList}
                  onChange={(e) => setDoList(e.target.value)}
                  placeholder={"Lead with the benefit\nUse the customer's words"}
                />
              </Field>
              <Field label="Don't" hint="One rule per line.">
                <textarea
                  rows={4}
                  value={dontList}
                  onChange={(e) => setDontList(e.target.value)}
                  placeholder={"No hype or superlatives\nNever mention competitors"}
                />
              </Field>
            </div>
          </>
        )}

        {step === 2 && (
          <>
            <label className="check-row">
              <input
                type="checkbox"
                checked={autoApprove}
                onChange={(e) => setAutoApprove(e.target.checked)}
              />
              <span>
                <strong>Autopilot auto-approve</strong>
                <span className="field-hint">
                  Generated assets skip the approvals inbox and go straight to
                  approved.
                </span>
              </span>
            </label>
            <Field
              label="Channels that always require approval"
              hint="Comma-separated: email, sms, social. Empty = none."
            >
              <input
                value={approvalChannels}
                onChange={(e) => setApprovalChannels(e.target.value)}
                placeholder="sms"
              />
            </Field>
            <div className="grid-3">
              <Field label="Daily send cap">
                <input
                  type="number"
                  min={1}
                  value={dailyCap}
                  onChange={(e) => setDailyCap(Number(e.target.value))}
                />
              </Field>
              <Field label="Quiet hours start (0–23)">
                <input
                  type="number"
                  min={0}
                  max={23}
                  value={quietStart}
                  onChange={(e) => setQuietStart(Number(e.target.value))}
                />
              </Field>
              <Field label="Quiet hours end (0–23)">
                <input
                  type="number"
                  min={0}
                  max={23}
                  value={quietEnd}
                  onChange={(e) => setQuietEnd(Number(e.target.value))}
                />
              </Field>
            </div>
            {auto && (
              <p className="muted">
                Current settings loaded — adjust and save, or finish to keep
                them.
              </p>
            )}
          </>
        )}

        <div className="form-actions">
          {step > 0 && (
            <button
              type="button"
              className="btn"
              onClick={() => setStep(step - 1)}
              disabled={saving}
            >
              Back
            </button>
          )}
          <span className="spacer" />
          {step === STEPS.length - 1 && (
            <button
              type="button"
              className="btn btn-ghost"
              onClick={() => navigate("/", { replace: true })}
            >
              Skip for now
            </button>
          )}
          <button type="submit" className="btn btn-primary" disabled={saving}>
            {saving
              ? "Saving…"
              : step === STEPS.length - 1
                ? "Finish setup"
                : "Save & continue"}
          </button>
        </div>
      </form>
    </div>
  );
}
