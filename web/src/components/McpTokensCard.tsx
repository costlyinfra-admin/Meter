/**
 * Settings → Coding agents.
 *
 * Where someone gives a coding agent read access to their Meter data, and takes
 * it back. Three things it has to get right:
 *
 * The token is shown ONCE. The server stores only a hash, so a panel that
 * pretended to show it again would be lying. It stays on screen until dismissed,
 * and says plainly that this is the only time.
 *
 * Revoking has to be obvious and safe. Each token carries when it was last used
 * and how many calls it made this week, because the question before turning one
 * off is always "is anything still using this?".
 *
 * And it says where the data goes. An agent reading these numbers sends them to
 * whatever model that agent runs on — Meter's own privacy promise covers Meter,
 * not the tool at the other end, and someone deciding whether to connect one
 * deserves to read that before they do rather than after.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type McpActivity, type McpToken } from "../api";
import { Snippet } from "./Snippet";

/** Tool calls listed under "Recent activity". Enough to see a session's shape. */
const SHOWN_ACTIVITY = 15;

const OUTCOME_LABELS: Record<McpActivity["outcome"], string> = {
  ok: "",
  error: "failed",
  rate_limited: "rate limited",
};

function when(iso: string | null): string {
  if (!iso) return "never";
  const at = new Date(iso);
  const minutes = Math.round((Date.now() - at.getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)}h ago`;
  return at.toLocaleDateString();
}

export function McpTokensCard({ appOrigin }: { appOrigin?: string }) {
  const [tokens, setTokens] = useState<McpToken[] | null>(null);
  const [activity, setActivity] = useState<McpActivity[]>([]);
  const [label, setLabel] = useState("");
  const [created, setCreated] = useState<{ label: string; token: string } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const origin = appOrigin ?? window.location.origin;

  async function reload() {
    const [list, recent] = await Promise.all([api.mcpTokens(), api.mcpActivity()]);
    setTokens(list);
    setActivity(recent.slice(0, SHOWN_ACTIVITY));
  }

  useEffect(() => {
    let live = true;
    Promise.all([api.mcpTokens(), api.mcpActivity()])
      .then(([list, recent]) => {
        if (!live) return;
        setTokens(list);
        setActivity(recent.slice(0, SHOWN_ACTIVITY));
      })
      .catch(() => live && setTokens([]));
    return () => {
      live = false;
    };
  }, []);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    if (!label.trim()) return;
    setBusy("create");
    setError(null);
    try {
      const made = await api.createMcpToken(label.trim());
      setCreated({ label: made.label, token: made.token });
      setLabel("");
      await reload();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "Could not create the token.");
    } finally {
      setBusy(null);
    }
  }

  async function revoke(token: McpToken) {
    setBusy(token.id);
    setError(null);
    try {
      await api.revokeMcpToken(token.id);
      await reload();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : "Could not revoke the token.");
    } finally {
      setBusy(null);
    }
  }

  const active = (tokens ?? []).filter((t) => t.active);

  return (
    <section className="settings-card">
      <h2>Coding agents</h2>
      <p className="muted">
        Let a coding agent — Claude Code, say — read this organization&rsquo;s cost and optimization
        data from inside the repository that produced the spend.
      </p>

      <p className="hint">
        Access is <strong>read-only</strong>: an agent can read what a screen shows and cannot
        change a feature, a setting or an optimization. It never receives prompts or responses,
        because Meter does not store any. What it does receive — your feature names, spend and
        customer identifiers — is sent to whichever model that agent runs on, which is outside
        Meter. Connect one only where that is acceptable.{" "}
        <Link to="/help/trust/coding-agents">More about coding agents</Link>.
      </p>

      {created && (
        <div className="settings-field mcp-new-token">
          <label>Token for {created.label}</label>
          <Snippet>{created.token}</Snippet>
          <span className="settings-hint muted">
            This is the only time it is shown — Meter stores only a hash of it. Give it to your
            agent as <code>METER_MCP_TOKEN</code>, then dismiss this.
          </span>
          <button type="button" className="secondary" onClick={() => setCreated(null)}>
            Done
          </button>
        </div>
      )}

      <form className="settings-field" onSubmit={create}>
        <label htmlFor="mcp-label">Add a token</label>
        <div className="mcp-add">
          <input
            id="mcp-label"
            value={label}
            maxLength={100}
            placeholder="What it is for, e.g. Bipin's laptop"
            onChange={(e) => setLabel(e.target.value)}
          />
          <button type="submit" disabled={!label.trim() || busy === "create"}>
            {busy === "create" ? "Creating…" : "Create token"}
          </button>
        </div>
        <span className="settings-hint muted">
          One per machine, so you can turn off a laptop without disturbing anything else.
        </span>
      </form>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {tokens === null ? (
        <p className="muted">Loading…</p>
      ) : tokens.length === 0 ? (
        <p className="muted">No tokens yet.</p>
      ) : (
        <div className="kb-table-wrap">
          <table className="features-table">
            <thead>
              <tr>
                <th>Label</th>
                <th>Last used</th>
                <th>Calls (7d)</th>
                <th>State</th>
                <th aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {tokens.map((token) => (
                <tr key={token.id}>
                  <td>{token.label}</td>
                  <td>{when(token.last_used_at)}</td>
                  <td>{token.recent_calls}</td>
                  <td>{token.active ? "Active" : `Revoked ${when(token.revoked_at)}`}</td>
                  <td>
                    {token.active && (
                      <button
                        type="button"
                        className="secondary"
                        disabled={busy === token.id}
                        onClick={() => revoke(token)}
                      >
                        {busy === token.id ? "Revoking…" : "Revoke"}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {active.length > 0 && (
        <div className="settings-field">
          <label>Connect Claude Code</label>
          <Snippet>{`claude mcp add --transport http meter ${origin}/api/mcp \\
  --scope user --header "Authorization: Bearer <your token>"`}</Snippet>
        </div>
      )}

      <div className="settings-field">
        <label>Recent activity</label>
        {activity.length === 0 ? (
          <span className="settings-hint muted">
            Nothing yet. Every tool call an agent makes is recorded here — what it asked for, never
            what it was told.
          </span>
        ) : (
          <ul className="kb-list mcp-activity">
            {activity.map((entry, i) => (
              <li key={i}>
                <span className="muted">{when(entry.at)}</span> {entry.tool}
                {entry.arguments ? ` ${entry.arguments}` : ""}
                {OUTCOME_LABELS[entry.outcome] && (
                  <strong> — {OUTCOME_LABELS[entry.outcome]}</strong>
                )}
                {entry.token_label ? <span className="muted"> · {entry.token_label}</span> : null}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
