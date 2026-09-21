/**
 * Sign in / sign up, laid out as the costlyinfra.com hero: the site's own
 * positioning on the left, and — where the product screenshot sits on the
 * marketing page — the form.
 *
 * Same words, same pill badge, same lime glow, same trust row. Someone arriving
 * from the website should not be able to tell they crossed a boundary; the only
 * difference is that the panel on the right does something.
 */
import { useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "../api";
import { BrandMark } from "../components/BrandMark";
import { DotField } from "../components/DotField";

interface AuthFormProps {
  title: string;
  submitLabel: string;
  onSubmit: (email: string, password: string) => Promise<void>;
  footer: { prompt: string; linkLabel: string; to: string };
  note?: ReactNode;
}

function SparkIcon() {
  return (
    <svg viewBox="0 0 16 16" width="13" height="13" aria-hidden fill="currentColor">
      <path d="M8 0.8l1.1 3.6 3.6 1.1-3.6 1.1L8 10.2 6.9 6.6 3.3 5.5l3.6-1.1L8 .8Z" />
      <path d="M12.9 9.4l.6 1.9 1.9.6-1.9.6-.6 1.9-.6-1.9-1.9-.6 1.9-.6.6-1.9Z" />
    </svg>
  );
}

/** The three promises the website leads with, in the same order it makes them. */
const TRUST = [
  {
    label: "Read-only access",
    path: "M8 1.4 13.4 3.4v4.3c0 3.3-2.2 5.6-5.4 6.9-3.2-1.3-5.4-3.6-5.4-6.9V3.4L8 1.4Z M5.8 8l1.6 1.6L10.5 6",
  },
  { label: "Privacy by design", path: "M3 8.4 6.3 11.7 13 5" },
  {
    label: "No gateway required",
    // A straight line, not the dollar-in-a-box that used to sit here: that icon
    // was drawn for "Bill reconciliation" and reads as nonsense beside this.
    // Traffic goes direct — that is the whole claim.
    path: "M2.5 8h11 M10 4.5 13.5 8 10 11.5",
  },
];

export function AuthForm({ title, submitLabel, onSubmit, footer, note }: AuthFormProps) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await onSubmit(email, password);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="auth-hero">
      {/* The site's lime bloom, off the top-right corner, and the dot grid that
          scatters away from the cursor. */}
      <div className="auth-glow" aria-hidden />
      <DotField />

      <header className="auth-top">
        <div className="auth-top-inner">
          <span className="brand">
            <BrandMark />
            Meter
          </span>
          <a className="auth-home-link" href="https://costlyinfra.com">
            costlyinfra.com ↗
          </a>
        </div>
      </header>

      <div className="auth-grid">
        <div className="auth-pitch">
          <span className="auth-badge">
            <SparkIcon />
            AI economics for product companies
          </span>
          <h1 className="auth-headline">Understand and optimize every dollar of AI spend.</h1>
          <p className="auth-sub">
            Track costs across products, customers, and features—understand the economics, identify
            spend drivers, and prioritize optimizations that improve margin.
          </p>
          <ul className="auth-trust">
            {TRUST.map((item) => (
              <li key={item.label}>
                <svg
                  viewBox="0 0 16 16"
                  width="15"
                  height="15"
                  aria-hidden
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.3"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d={item.path} />
                </svg>
                {item.label}
              </li>
            ))}
          </ul>
        </div>

        <div className="auth-card">
          <h2>{title}</h2>
          <form onSubmit={handleSubmit}>
            <label>
              Work email
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="email"
                required
              />
            </label>
            <label>
              Password
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                minLength={8}
                required
              />
            </label>
            {error && (
              <p className="error" role="alert">
                {error}
              </p>
            )}
            <button type="submit" disabled={submitting}>
              {submitting ? "…" : submitLabel}
            </button>
          </form>
          <p className="muted">
            {footer.prompt} <Link to={footer.to}>{footer.linkLabel}</Link>
          </p>
          {note}
        </div>
      </div>
    </div>
  );
}
