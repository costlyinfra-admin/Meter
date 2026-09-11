/**
 * Build-cost data actions: every way to get per-developer coding-tool spend in,
 * organized into one collapsible card per method so the panel stays uncluttered.
 *
 * Methods, most precise first:
 *  1. Usage-based tools (Claude Code, Cursor) — admin APIs report actual spend.
 *  2. GitHub Copilot — seat roster from the GitHub org × seat price.
 *  3. Other tools via SSO seats — count assigned users in Okta/Entra × price book.
 *  4. CSV import — universal fallback.
 *  5. Fine-tune / training cost — one-time, added manually.
 */
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { api, ApiError, type ManualBuildEntry, type SeatSource } from "../api";
import { money } from "../format";

export interface FeatureOption {
  feature_id: string;
  name: string;
}

/** One collapsible method card: a clickable header + an expanding panel. */
function MethodCard({
  id,
  title,
  tagline,
  openId,
  setOpenId,
  children,
}: {
  id: string;
  title: string;
  tagline: string;
  openId: string | null;
  setOpenId: (v: string | null) => void;
  children: ReactNode;
}) {
  const open = openId === id;
  return (
    <li className={`method-card${open ? " open" : ""}`}>
      <button
        type="button"
        className="method-head"
        onClick={() => setOpenId(open ? null : id)}
        aria-expanded={open}
      >
        <span className="method-text">
          <span className="method-title">{title}</span>
          <span className="method-tagline">{tagline}</span>
        </span>
        <span className="method-toggle">{open ? "Close" : "Set up"}</span>
      </button>
      {open && <div className="method-panel">{children}</div>}
    </li>
  );
}

export function BuildCostActions({
  period,
  features,
  onChanged,
}: {
  period?: string;
  features: FeatureOption[];
  onChanged: () => Promise<void>;
}) {
  const [openId, setOpenId] = useState<string | null>(null);
  const [csv, setCsv] = useState("");
  // The manual-entry form. Kept as strings so a half-typed amount does not
  // become NaN under the cursor.
  const [mDev, setMDev] = useState("");
  const [mHandle, setMHandle] = useState("");
  const [mTool, setMTool] = useState("cursor");
  const [mAmount, setMAmount] = useState("");
  const [mMonths, setMMonths] = useState("1");
  const [manual, setManual] = useState<ManualBuildEntry[] | null>(null);
  const [tool, setTool] = useState("cursor");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [claudeKey, setClaudeKey] = useState("");
  const [copilotOwner, setCopilotOwner] = useState("");
  const [cursorKey, setCursorKey] = useState("");
  const [idp, setIdp] = useState("okta");
  const [oktaDomain, setOktaDomain] = useState("");
  const [oktaToken, setOktaToken] = useState("");
  const [entraTenant, setEntraTenant] = useState("");
  const [entraClientId, setEntraClientId] = useState("");
  const [entraSecret, setEntraSecret] = useState("");
  const [seatAppId, setSeatAppId] = useState("");
  const [seatTool, setSeatTool] = useState("cursor");
  const [seatPlan, setSeatPlan] = useState("business");
  const [seatSources, setSeatSources] = useState<SeatSource[]>([]);
  const [ftFeature, setFtFeature] = useState("");
  const [ftAmount, setFtAmount] = useState("");
  const [ftLabel, setFtLabel] = useState("");
  const monthParam = period?.slice(0, 7);

  useEffect(() => {
    api
      .listSeatSources()
      .then(setSeatSources)
      .catch(() => undefined);
  }, []);

  async function syncClaudeCode() {
    setBusy(true);
    setNote(null);
    try {
      if (claudeKey.trim()) {
        // A freshly-entered admin key updates the shared Anthropic credential.
        await api.saveCredential("anthropic", claudeKey.trim());
        setClaudeKey("");
      }
      const r = await api.syncClaudeCodeSpend(monthParam);
      setNote(
        `Synced Claude Code spend: ${r.spending_members} of ${r.members} developers → ${money(r.total)} build cost.`,
      );
      await onChanged();
    } catch (err) {
      setNote(err instanceof ApiError ? err.message : "Claude Code sync failed.");
    } finally {
      setBusy(false);
    }
  }

  async function syncCopilot() {
    if (!copilotOwner.trim()) {
      setNote("Enter the GitHub organization to sync Copilot seats.");
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      const r = await api.syncCopilotSeats(copilotOwner.trim(), monthParam);
      setNote(
        `Synced ${r.seats} Copilot ${r.plan} seats (${money(r.seat_price)}/seat) → ${money(r.total)} build cost.`,
      );
      await onChanged();
    } catch (err) {
      setNote(err instanceof ApiError ? err.message : "Copilot sync failed.");
    } finally {
      setBusy(false);
    }
  }

  async function syncCursor() {
    setBusy(true);
    setNote(null);
    try {
      if (cursorKey.trim()) {
        // A freshly-entered admin key replaces the stored credential first.
        await api.saveCredential("cursor", cursorKey.trim());
        setCursorKey("");
      }
      const r = await api.syncCursorSpend(monthParam);
      setNote(
        `Synced Cursor spend: ${r.spending_members} of ${r.members} members with usage → ${money(r.total)} build cost.`,
      );
      await onChanged();
    } catch (err) {
      setNote(err instanceof ApiError ? err.message : "Cursor sync failed.");
    } finally {
      setBusy(false);
    }
  }

  async function connectIdp() {
    let secret: string;
    if (idp === "okta") {
      if (!oktaDomain.trim() || !oktaToken.trim()) {
        setNote("Enter the Okta domain and an API token.");
        return;
      }
      secret = JSON.stringify({ domain: oktaDomain.trim(), token: oktaToken.trim() });
    } else {
      if (!entraTenant.trim() || !entraClientId.trim() || !entraSecret.trim()) {
        setNote("Enter the Entra tenant id, client id, and client secret.");
        return;
      }
      secret = JSON.stringify({
        tenant_id: entraTenant.trim(),
        client_id: entraClientId.trim(),
        client_secret: entraSecret.trim(),
      });
    }
    setBusy(true);
    setNote(null);
    try {
      await api.saveCredential(idp, secret);
      setOktaToken("");
      setEntraSecret("");
      setNote(`Connected ${idp}. Add app→tool mappings below, then sync.`);
    } catch (err) {
      setNote(err instanceof ApiError ? err.message : "Could not connect identity provider.");
    } finally {
      setBusy(false);
    }
  }

  async function addSeatSource() {
    if (!seatAppId.trim()) {
      setNote("Enter the IdP application id to map.");
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      await api.registerSeatSource(idp, seatAppId.trim(), seatTool, seatTool, seatPlan.trim());
      setSeatAppId("");
      setSeatSources(await api.listSeatSources());
      setNote("Added seat mapping.");
    } catch (err) {
      setNote(err instanceof ApiError ? err.message : "Could not add mapping.");
    } finally {
      setBusy(false);
    }
  }

  async function syncSeats() {
    setBusy(true);
    setNote(null);
    try {
      const r = await api.syncIdpSeats(monthParam);
      setNote(`Synced ${r.total_seats} SSO seats → ${money(r.total)} build cost.`);
      await onChanged();
    } catch (err) {
      setNote(err instanceof ApiError ? err.message : "Seat sync failed.");
    } finally {
      setBusy(false);
    }
  }

  async function importBuild() {
    setBusy(true);
    setNote(null);
    try {
      const r = await api.importBuildCost(csv, tool, monthParam);
      setCsv("");
      const months = r.months_imported ?? 1;
      setNote(
        months > 1
          ? `Imported build cost across ${months} months (current month total ${money(r.total)}).`
          : `Imported build cost (total ${money(r.total)}).`,
      );
      await onChanged();
    } catch (err) {
      setNote(err instanceof ApiError ? err.message : "Import failed.");
    } finally {
      setBusy(false);
    }
  }

  const loadManual = useCallback(async () => {
    try {
      setManual((await api.manualBuildCost(monthParam)).entries);
    } catch {
      setManual([]); // the list is an aid; losing it must not block adding
    }
  }, [monthParam]);

  useEffect(() => {
    if (openId === "manual") void loadManual();
  }, [openId, loadManual]);

  async function addManual() {
    const amount = parseFloat(mAmount);
    if (!mDev.trim() || !Number.isFinite(amount) || amount <= 0) return;
    setBusy(true);
    setNote(null);
    try {
      const r = await api.addManualBuildCost({
        developer: mDev.trim(),
        github_handle: mHandle.trim() || undefined,
        tool: mTool,
        amount,
        period: monthParam,
        months: Math.max(1, parseInt(mMonths, 10) || 1),
      });
      setMDev("");
      setMHandle("");
      setMAmount("");
      setMMonths("1");
      setNote(
        `Added ${money(amount)} of ${mTool.replace("_", " ")} cost (total ${money(r.total)}).`,
      );
      await loadManual();
      await onChanged();
    } catch (err) {
      setNote(err instanceof ApiError ? err.message : "Could not add that cost.");
    } finally {
      setBusy(false);
    }
  }

  async function removeManual(id: string) {
    setBusy(true);
    try {
      await api.deleteManualBuildCost(id);
      await loadManual();
      await onChanged();
    } finally {
      setBusy(false);
    }
  }

  async function recordTraining() {
    const amount = parseFloat(ftAmount);
    if (!ftFeature || !ftLabel.trim() || Number.isNaN(amount)) {
      setNote("Pick a feature, and enter a run label and amount.");
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      await api.recordTrainingCost(ftFeature, amount, ftLabel.trim(), monthParam);
      setFtAmount("");
      setFtLabel("");
      setNote("Recorded fine-tuning run as build cost.");
      await onChanged();
    } catch (err) {
      setNote(err instanceof ApiError ? err.message : "Could not record training cost.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <ul className="method-list">
        {/* 1 — Usage-based tools: Claude Code + Cursor admin APIs (most precise). */}
        <MethodCard
          id="usage"
          title="Usage-based tools — Claude Code &amp; Cursor"
          tagline="Most precise. Pulls each developer's actual spend from the tool's admin API."
          openId={openId}
          setOpenId={setOpenId}
        >
          <p className="method-help">
            Claude Code and Cursor each expose an admin API that reports real per-developer spend.
            We pull it and split it across features by who authored which pull requests — the most
            accurate build-cost option.
          </p>

          <div className="method-block">
            <span className="method-block-title">Claude Code</span>
            <span className="inline">
              <input
                placeholder="Anthropic Admin key (sk-ant-admin…)"
                type="password"
                value={claudeKey}
                onChange={(e) => setClaudeKey(e.target.value)}
                autoComplete="off"
              />
              <button onClick={syncClaudeCode} disabled={busy}>
                Sync Claude Code
              </button>
            </span>
            <span className="muted">
              Anthropic Console → Settings → Admin keys. Reuses your Anthropic connection — leave
              blank if already connected. (Per-developer Claude Code spend needs an Enterprise
              plan.)
            </span>
          </div>

          <div className="method-block">
            <span className="method-block-title">Cursor</span>
            <span className="inline">
              <input
                placeholder="Cursor Admin API key (kept from last sync if blank)"
                type="password"
                value={cursorKey}
                onChange={(e) => setCursorKey(e.target.value)}
                autoComplete="off"
              />
              <button onClick={syncCursor} disabled={busy}>
                Sync Cursor
              </button>
            </span>
            <span className="muted">
              cursor.com/dashboard → Settings → Cursor Admin API Keys (team admins only). Pulls each
              member's metered spend.
            </span>
          </div>
        </MethodCard>

        {/* 2 — GitHub Copilot seats. */}
        <MethodCard
          id="copilot"
          title="GitHub Copilot"
          tagline="Reads who has a Copilot seat in your GitHub org × the seat price."
          openId={openId}
          setOpenId={setOpenId}
        >
          <p className="method-help">
            Copilot is licensed per seat. We read the seat assignments from your GitHub organization
            and multiply by the plan's seat price, then allocate to features by PR authorship.
          </p>
          <span className="inline">
            <input
              placeholder="GitHub organization"
              value={copilotOwner}
              onChange={(e) => setCopilotOwner(e.target.value)}
            />
            <button onClick={syncCopilot} disabled={busy || !copilotOwner.trim()}>
              Sync Copilot seats
            </button>
          </span>
          <span className="muted">
            Needs a GitHub token with the <code>manage_billing:copilot</code> or{" "}
            <code>admin:org</code> scope — the same GitHub connection from Features works if it has
            billing access.
          </span>
        </MethodCard>

        {/* 3 — SSO seat estimate for tools without a usage API. */}
        <MethodCard
          id="sso"
          title="Other tools via SSO seats"
          tagline="Tabnine, Amazon Q, Gemini Code Assist, Codex, Cursor — counted from your SSO provider."
          openId={openId}
          setOpenId={setOpenId}
        >
          <p className="method-help">
            Some tools don't report per-developer cost. If your team signs in to them through{" "}
            <strong>Okta</strong> or <strong>Microsoft Entra ID</strong> (your single sign-on
            provider), we can count how many people are assigned to each tool and multiply by its
            per-seat price.
          </p>
          <ol className="connector-steps">
            <li>Connect your SSO provider below (read-only).</li>
            <li>
              <strong>Map</strong> each SSO app to the tool and plan it stands for — e.g. “the
              Tabnine app = Tabnine, Enterprise”. That tells us which seat price to apply.
            </li>
            <li>Sync — we count assigned users × seat price for the month.</li>
          </ol>

          <div className="method-block">
            <span className="method-block-title">1. Connect SSO provider</span>
            <span className="inline">
              <select value={idp} onChange={(e) => setIdp(e.target.value)}>
                <option value="okta">Okta</option>
                <option value="entra">Microsoft Entra ID</option>
              </select>
              {idp === "okta" ? (
                <>
                  <input
                    placeholder="Okta domain (acme.okta.com)"
                    value={oktaDomain}
                    onChange={(e) => setOktaDomain(e.target.value)}
                  />
                  <input
                    placeholder="API token"
                    type="password"
                    value={oktaToken}
                    onChange={(e) => setOktaToken(e.target.value)}
                  />
                </>
              ) : (
                <>
                  <input
                    placeholder="Tenant id"
                    value={entraTenant}
                    onChange={(e) => setEntraTenant(e.target.value)}
                  />
                  <input
                    placeholder="Client id"
                    value={entraClientId}
                    onChange={(e) => setEntraClientId(e.target.value)}
                  />
                  <input
                    placeholder="Client secret"
                    type="password"
                    value={entraSecret}
                    onChange={(e) => setEntraSecret(e.target.value)}
                  />
                </>
              )}
              <button onClick={connectIdp} disabled={busy}>
                Connect
              </button>
            </span>
          </div>

          <div className="method-block">
            <span className="method-block-title">2. Map an app to a tool</span>
            <span className="inline">
              <input
                placeholder={idp === "okta" ? "Okta app id" : "Entra app (service principal) id"}
                value={seatAppId}
                onChange={(e) => setSeatAppId(e.target.value)}
              />
              <select value={seatTool} onChange={(e) => setSeatTool(e.target.value)}>
                <option value="cursor">Cursor</option>
                <option value="tabnine">Tabnine</option>
                <option value="amazon_q">Amazon Q Developer</option>
                <option value="gemini_code_assist">Gemini Code Assist</option>
                <option value="codex">OpenAI Codex (ChatGPT)</option>
              </select>
              <input
                placeholder="plan (e.g. business)"
                value={seatPlan}
                onChange={(e) => setSeatPlan(e.target.value)}
              />
              <button onClick={addSeatSource} disabled={busy}>
                Add mapping
              </button>
            </span>
          </div>

          {seatSources.length > 0 && (
            <div className="method-block">
              <span className="method-block-title">3. Mapped apps</span>
              <span className="inline pools-line">
                <span className="muted">
                  {seatSources
                    .map((s) => `${s.provider}: ${s.app_label || s.app_id} → ${s.tool}/${s.plan}`)
                    .join("  ")}
                </span>
                <button onClick={syncSeats} disabled={busy}>
                  Sync seats
                </button>
              </span>
            </div>
          )}

          <span className="muted">
            Seat-licensed tools like Gemini Code Assist, Amazon Q, and Tabnine have no per-developer
            usage API, so this seat estimate is how we capture them. Cursor is more precise via its
            own API above.
          </span>
        </MethodCard>

        {/* 4 — Add one figure by hand. */}
        <MethodCard
          id="manual"
          title="Add a cost manually"
          tagline="One developer, one tool, one amount. For spend no connector can see."
          openId={openId}
          setOpenId={setOpenId}
        >
          <p className="method-help">
            For spend Meter cannot reach — a tool without an API, an invoice you were sent, a seat
            billed outside your org. Unlike a sync or a CSV import, this is <strong>added</strong>{" "}
            to the month rather than replacing it, and a later sync will not remove it. The GitHub
            handle is what attributes the cost to features through PR authorship; without one it
            lands in Unattributed.
          </p>
          <div className="manual-cost-form">
            <label>
              Developer
              <input
                value={mDev}
                onChange={(e) => setMDev(e.target.value)}
                placeholder="Dana Patel"
                aria-label="Developer name"
              />
            </label>
            <label>
              GitHub handle
              <input
                value={mHandle}
                onChange={(e) => setMHandle(e.target.value)}
                placeholder="dpatel"
                aria-label="GitHub handle"
              />
            </label>
            <label>
              Tool
              <select value={mTool} onChange={(e) => setMTool(e.target.value)} aria-label="Tool">
                <option value="cursor">Cursor</option>
                <option value="claude_code">Claude Code</option>
                <option value="copilot">Copilot</option>
                <option value="codex">Codex</option>
              </select>
            </label>
            <label>
              Amount ($)
              <input
                type="number"
                min="0"
                step="0.01"
                value={mAmount}
                onChange={(e) => setMAmount(e.target.value)}
                placeholder="120.00"
                aria-label="Amount"
              />
            </label>
            <label>
              Months
              <input
                type="number"
                min="1"
                max="24"
                value={mMonths}
                onChange={(e) => setMMonths(e.target.value)}
                aria-label="Months"
              />
            </label>
            <button onClick={addManual} disabled={busy || !mDev.trim() || !mAmount.trim()}>
              Add cost
            </button>
          </div>
          {manual && manual.length > 0 && (
            <>
              <span className="chart-title">Added by hand this month</span>
              <ul className="manual-cost-list">
                {manual.map((e) => (
                  <li key={e.id}>
                    <span className="manual-cost-dev">{e.developer}</span>
                    <span className="muted">{e.tool.replace("_", " ")}</span>
                    <span className="manual-cost-amount">{money(e.amount)}</span>
                    <button
                      type="button"
                      className="linklike danger"
                      onClick={() => void removeManual(e.id)}
                      disabled={busy}
                      aria-label={`Remove ${e.developer}`}
                    >
                      Remove
                    </button>
                  </li>
                ))}
              </ul>
            </>
          )}
        </MethodCard>

        {/* 5 — CSV import (universal fallback). */}
        <MethodCard
          id="csv"
          title="Import a CSV"
          tagline="Works for any tool. Paste developer, github handle, tool, amount, months."
          openId={openId}
          setOpenId={setOpenId}
        >
          <p className="method-help">
            A universal fallback. Assemble a simple sheet with a header row of{" "}
            <code>developer,github_handle,tool,amount,months</code> — one row per developer — and
            paste it here. <code>developer</code> is the display name; <code>github_handle</code> is
            their GitHub login, used to attribute PRs to features (matched case-insensitively). The
            tool column is optional if you pick a tool below. <code>months</code> is optional too
            (default 1) — set it to backfill history: <code>…,50.00,12</code> records $50 for each
            of the last 12 months, ending with the current month.
          </p>
          <textarea
            value={csv}
            onChange={(e) => setCsv(e.target.value)}
            rows={3}
            placeholder={
              "developer,github_handle,tool,amount,months\nJohn,John-ni,claude_code,50.00,12"
            }
          />
          <span className="inline">
            <select value={tool} onChange={(e) => setTool(e.target.value)}>
              <option value="cursor">Cursor</option>
              <option value="claude_code">Claude Code</option>
              <option value="copilot">Copilot</option>
              <option value="codex">Codex</option>
            </select>
            <button onClick={importBuild} disabled={busy || !csv.trim()}>
              Import
            </button>
          </span>
        </MethodCard>

        {/* 6 — Fine-tune / training cost (one-time, manual). */}
        <MethodCard
          id="training"
          title="Fine-tuning / training runs"
          tagline="One-time model-training cost, added manually per feature."
          openId={openId}
          setOpenId={setOpenId}
        >
          <p className="method-help">
            The one-time cost of fine-tuning or training a model. It counts as build cost (never
            blended with inference). Pick the feature it was for and enter the amount.
          </p>
          <span className="inline">
            <select value={ftFeature} onChange={(e) => setFtFeature(e.target.value)}>
              <option value="">Select feature…</option>
              {features.map((f) => (
                <option key={f.feature_id} value={f.feature_id}>
                  {f.name}
                </option>
              ))}
            </select>
            <input
              placeholder="Run label (e.g. Llama-3.1-70B tuning)"
              value={ftLabel}
              onChange={(e) => setFtLabel(e.target.value)}
            />
            <input
              placeholder="$ amount"
              inputMode="decimal"
              value={ftAmount}
              onChange={(e) => setFtAmount(e.target.value)}
              autoComplete="off"
            />
            <button onClick={recordTraining} disabled={busy}>
              Add training cost
            </button>
          </span>
        </MethodCard>
      </ul>
      {note && <p className="muted build-note">{note}</p>}
    </>
  );
}
