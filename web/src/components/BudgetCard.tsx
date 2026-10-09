/**
 * Settings → Budgets: the organization's one AI budget.
 *
 * The budget is a single stored row, not a set of independently editable
 * preferences, so this saves it whole and offers a plain way to remove it. Every
 * value is validated again on the server; the checks here exist to say what is
 * wrong before a round trip, not instead of one.
 */
import { useEffect, useState } from "react";
import { api, ApiError, type Budget } from "../api";
import { money } from "../format";
import { Skeleton } from "./Skeleton";

const CADENCES: { value: Budget["cadence"]; label: string }[] = [
  { value: "monthly", label: "Per month" },
  { value: "annual", label: "Per year" },
];

/** Today in YYYY-MM-DD, for the default effective date on a new budget. */
function todayISO(): string {
  const now = new Date();
  return new Date(now.getTime() - now.getTimezoneOffset() * 60_000).toISOString().slice(0, 10);
}

export function BudgetCard({ currency }: { currency: string }) {
  const [budget, setBudget] = useState<Budget | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [amount, setAmount] = useState("");
  const [cadence, setCadence] = useState<Budget["cadence"]>("monthly");
  const [effectiveFrom, setEffectiveFrom] = useState(todayISO);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .getBudget()
      .then(({ budget: b }) => {
        setBudget(b);
        if (b) {
          setAmount(String(b.amount));
          setCadence(b.cadence);
          setEffectiveFrom(b.effective_from);
        }
      })
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Could not load the budget."),
      )
      .finally(() => setLoaded(true));
  }, []);

  async function save() {
    setError(null);
    setSaved(false);
    const value = Number(amount);
    if (!Number.isFinite(value) || value <= 0) {
      setError("Enter a budget amount greater than 0.");
      return;
    }
    setSaving(true);
    try {
      const { budget: next } = await api.setBudget({
        amount: value,
        cadence,
        effective_from: effectiveFrom,
      });
      setBudget(next);
      setSaved(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save the budget.");
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    setError(null);
    setSaved(false);
    setSaving(true);
    try {
      await api.removeBudget();
      setBudget(null);
      setAmount("");
      setCadence("monthly");
      setEffectiveFrom(todayISO());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not remove the budget.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="settings-card" id="budgets">
      <h2>Budgets</h2>
      <p className="muted settings-lead">
        One AI budget for the organization. The Overview tracks each period against it and
        forecasts where the period will land; alerts that measure spend against budget need it
        too. Longer or shorter windows are prorated by calendar day.
      </p>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {!loaded ? (
        <Skeleton />
      ) : (
        <>
          <div className="settings-field">
            <label htmlFor="budget-amount">Budget amount ({currency})</label>
            <input
              id="budget-amount"
              type="number"
              min="0"
              step="100"
              value={amount}
              placeholder="50000"
              onChange={(e) => {
                setAmount(e.target.value);
                setSaved(false);
              }}
            />
          </div>

          <div className="settings-field">
            <label htmlFor="budget-cadence">Applies</label>
            <select
              id="budget-cadence"
              value={cadence}
              onChange={(e) => {
                setCadence(e.target.value as Budget["cadence"]);
                setSaved(false);
              }}
            >
              {CADENCES.map((c) => (
                <option key={c.value} value={c.value}>
                  {c.label}
                </option>
              ))}
            </select>
            <span className="settings-hint muted">
              A yearly budget is spread across the days of the year being reported on — leap
              years included.
            </span>
          </div>

          <div className="settings-field">
            <label htmlFor="budget-from">Effective from</label>
            <input
              id="budget-from"
              type="date"
              value={effectiveFrom}
              onChange={(e) => {
                setEffectiveFrom(e.target.value);
                setSaved(false);
              }}
            />
            <span className="settings-hint muted">
              Days before this date are not budgeted, so a budget set today does not
              retroactively cover last quarter.
            </span>
          </div>

          <div className="settings-actions">
            <button type="button" onClick={save} disabled={saving}>
              {saving ? "Saving…" : budget ? "Update budget" : "Set budget"}
            </button>
            {budget && (
              <button type="button" className="secondary" onClick={remove} disabled={saving}>
                Remove budget
              </button>
            )}
            {saved && <span className="settings-saved">Saved ✓</span>}
          </div>

          <p className="muted settings-hint">
            {budget
              ? `Current budget: ${money(budget.amount)} ${
                  budget.cadence === "monthly" ? "per month" : "per year"
                }, effective ${budget.effective_from}.`
              : "No budget is set. Cards and alerts that need one will say so rather than assume a figure."}
          </p>
        </>
      )}
    </section>
  );
}
