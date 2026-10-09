import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { errorMessage, Field } from "../components/ui";

export default function RegisterPage() {
  const { register } = useAuth();
  const navigate = useNavigate();
  const [fullName, setFullName] = useState("");
  const [businessName, setBusinessName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await register({
        email: email.trim(),
        password,
        full_name: fullName.trim(),
        business_name: businessName.trim(),
      });
      navigate("/onboarding", { replace: true });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="brand brand-center">
          <div className="brand-mark">F</div>
          <div className="brand-name">ForgeOS</div>
        </div>
        <h1>Create your workspace</h1>
        <p className="muted">
          One account provisions your user, business, and API access.
        </p>
        <form onSubmit={onSubmit} className="form">
          {error && (
            <div className="alert alert-error" role="alert">
              {error}
            </div>
          )}
          <Field label="Full name">
            <input
              required
              autoComplete="name"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              placeholder="Ada Lovelace"
            />
          </Field>
          <Field label="Business name">
            <input
              required
              value={businessName}
              onChange={(e) => setBusinessName(e.target.value)}
              placeholder="Acme Inc."
            />
          </Field>
          <Field label="Email">
            <input
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@company.com"
            />
          </Field>
          <Field label="Password">
            <input
              type="password"
              required
              minLength={8}
              autoComplete="new-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="At least 8 characters"
            />
          </Field>
          <button type="submit" className="btn btn-primary btn-block" disabled={busy}>
            {busy ? "Creating…" : "Create workspace"}
          </button>
        </form>
        <p className="auth-switch">
          Already have an account? <Link to="/login">Sign in</Link>
        </p>
      </div>
    </div>
  );
}
