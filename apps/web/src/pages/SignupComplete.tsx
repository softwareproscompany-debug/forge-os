import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { setToken } from "../lib/api";
import { useAuth } from "../lib/auth";

const API_BASE = import.meta.env.VITE_API_URL || "";

export default function SignupComplete() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { refreshUser } = useAuth();
  const sessionId = params.get("session_id") || "";

  const [businessName, setBusinessName] = useState("");
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/v1/billing/complete-signup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: sessionId,
          business_name: businessName,
          full_name: fullName,
          password,
        }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || "Signup failed. Please try again.");
      }
      const data = await res.json();
      setToken(data.token);
      await refreshUser();
      navigate("/dashboard", { replace: true });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong.");
      setLoading(false);
    }
  };

  if (!sessionId) {
    return (
      <div className="auth-page">
        <div className="auth-card">
          <h2>Invalid signup link</h2>
          <p className="muted">
            This link is missing its session. Please start from the{" "}
            <a href="/">pricing page</a>.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="brand" style={{ marginBottom: 16 }}>
          <div className="brand-mark">F</div>
          <div className="brand-name">ForgeOS</div>
        </div>
        <h2>Payment successful 🎉</h2>
        <p className="muted" style={{ marginBottom: 20 }}>
          One last step — set up your account.
        </p>
        <form onSubmit={submit}>
          <label className="field">
            <span>Business name</span>
            <input
              className="input"
              value={businessName}
              onChange={(e) => setBusinessName(e.target.value)}
              placeholder="Acme Inc."
              required
              autoComplete="organization"
            />
          </label>
          <label className="field">
            <span>Your name</span>
            <input
              className="input"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              placeholder="Jordan Smith"
              required
              autoComplete="name"
            />
          </label>
          <label className="field">
            <span>Password</span>
            <input
              className="input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="Min. 8 characters"
              required
              minLength={8}
              autoComplete="new-password"
            />
          </label>
          {error && <p className="form-error">{error}</p>}
          <button type="submit" className="btn btn-primary btn-block" disabled={loading}>
            {loading ? "Creating account…" : "Create my account"}
          </button>
        </form>
      </div>
    </div>
  );
}
