import { useState } from "react";
import { useNavigate } from "react-router-dom";

const PLANS = [
  {
    key: "starter",
    name: "Starter",
    price: 49,
    tagline: "For solo founders getting started",
    features: ["1 business", "AI campaigns & assets", "Draven AI assistant", "Email support"],
    popular: false,
  },
  {
    key: "professional",
    name: "Professional",
    price: 149,
    tagline: "For growing agencies",
    features: [
      "5 businesses",
      "Everything in Starter",
      "12-agent AI swarm",
      "Travel agency workspace",
      "Priority support",
    ],
    popular: true,
  },
  {
    key: "enterprise",
    name: "Enterprise",
    price: 499,
    tagline: "For teams at scale",
    features: [
      "Unlimited businesses",
      "Everything in Professional",
      "White-label",
      "Dedicated support",
    ],
    popular: false,
  },
];

const API_BASE = import.meta.env.VITE_API_URL || "";

export default function Landing() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const choosePlan = async (plan: string) => {
    setLoading(plan);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/api/v1/billing/checkout`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ plan }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || "Checkout failed. Please try again.");
      }
      const data = await res.json();
      window.location.href = data.checkout_url;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Something went wrong.");
      setLoading(null);
    }
  };

  return (
    <div className="landing">
      <header className="landing-nav">
        <div className="landing-brand">
          <div className="brand-mark">F</div>
          <span>ForgeOS</span>
        </div>
        <button className="btn btn-ghost" onClick={() => navigate("/login")}>
          Log in
        </button>
      </header>

      <section className="landing-hero">
        <h1>Your business, run by AI.</h1>
        <p className="landing-sub">
          ForgeOS is the AI-powered business operating system — campaigns, content,
          leads, and operations on autopilot.
        </p>
        <button
          className="btn btn-primary btn-lg"
          onClick={() => choosePlan("professional")}
          disabled={loading !== null}
        >
          {loading ? "Loading…" : "Start Free Trial"}
        </button>
        {error && <p className="landing-error">{error}</p>}
      </section>

      <section className="landing-pricing">
        <h2>Simple pricing</h2>
        <p className="muted">Start free for 14 days. Cancel anytime.</p>
        <div className="pricing-grid">
          {PLANS.map((p) => (
            <div key={p.key} className={`pricing-card${p.popular ? " popular" : ""}`}>
              {p.popular && <div className="popular-badge">Most Popular</div>}
              <h3>{p.name}</h3>
              <p className="muted">{p.tagline}</p>
              <div className="pricing-price">
                <span className="price-amount">${p.price}</span>
                <span className="muted">/mo</span>
              </div>
              <ul className="pricing-features">
                {p.features.map((f) => (
                  <li key={f}>✓ {f}</li>
                ))}
              </ul>
              <button
                className={`btn ${p.popular ? "btn-primary" : "btn-secondary"} btn-block`}
                onClick={() => choosePlan(p.key)}
                disabled={loading !== null}
              >
                {loading === p.key ? "Loading…" : "Choose Plan"}
              </button>
            </div>
          ))}
        </div>
      </section>

      <footer className="landing-footer">
        <p className="muted">© 2026 ForgeOS. All rights reserved.</p>
      </footer>
    </div>
  );
}
