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
    label: "Read-only first",
    path: "M8 1.4 13.4 3.4v4.3c0 3.3-2.2 5.6-5.4 6.9-3.2-1.3-5.4-3.6-5.4-6.9V3.4L8 1.4Z M5.8 8l1.6 1.6L10.5 6",
  },
  { label: "No prompt storage required", path: "M3 8.4 6.3 11.7 13 5" },
  {
    label: "Bill reconciliation",
    path: "M2.6 3.2h10.8v9.6H2.6z M8 5.4v5.2 M6.4 6.6h2.4a1.1 1.1 0 0 1 0 2.2H7.2a1.1 1.1 0 0 0 0 2.2h2.4",
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
          <h1 className="auth-headline">Stop guessing the margin on your AI product.</h1>
          <p className="auth-sub">
            See AI cost by customer and feature, find the highest-value optimizations, and verify
            what you actually saved — without routing production traffic through another gateway.
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
