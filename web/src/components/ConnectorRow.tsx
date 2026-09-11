/**
 * One expandable cost-source card. The header shows the source, its status, and
 * its actions; clicking Connect (not-connected) or Configure (connected) expands a
 * panel *directly underneath this row* — never at the bottom of the page. Expansion
 * is controlled by the parent so the list behaves as an accordion (one open at a
 * time). Not-connected rows expand to the setup guide + credential form; connected
 * rows expand to the form for replacing the stored credential, then the
 * provider's inline detail (`detail`).
 *
 * Replacing is deliberately the same field as connecting, never a pre-filled or
 * masked one. There is no route that returns a stored secret — not partially —
 * so there is nothing to pre-fill with, and a row of dots that cannot be edited
 * only invites someone to try. The one thing shown about the existing
 * credential is when it was set, which is what makes rotation checkable.
 */
import { type ReactNode, useCallback, useEffect, useState } from "react";
import { api, type ConnectorCredential, type ConnectorStatus } from "../api";
import { CONNECTOR_GUIDES } from "../connectorGuides";
import { ConnectorMark } from "./ConnectorMark";

export function ConnectorRow({
  connector,
  onConnected,
  hint,
  onSync,
  expanded,
  onToggle,
  detail,
}: {
  connector: ConnectorStatus;
  onConnected: () => void;
  hint?: string;
  /** When set, a connected row shows "Sync now"; returns a short result message. */
  onSync?: () => Promise<string>;
  /** Whether this card's inline panel is open (accordion, controlled by parent). */
  expanded: boolean;
  onToggle: () => void;
  /** Inline detail for a connected source (rendered under the row when expanded). */
  detail?: ReactNode;
}) {
  const [secret, setSecret] = useState("");
  const [label, setLabel] = useState("");
  const [saving, setSaving] = useState(false);
  // Which stored account is being replaced. null means "add another".
  const [replacing, setReplacing] = useState<string | null>(null);
  const [accounts, setAccounts] = useState<ConnectorCredential[] | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [syncNote, setSyncNote] = useState<string | null>(null);

  const guide = CONNECTOR_GUIDES[connector.type];
  // Most connectors take a token; the multiline ones take a service-account
  // blob. Derived rather than spelled out on twenty guides, and only ever used
  // as a word in a sentence.
  const noun = guide?.multiline ? "credentials" : "token";
  // Whether a second credential would actually be fetched with. The server
  // decides; offering "add another" where the sync reads one key would take a
  // key and never use it.
  const multi = connector.supports_multiple === true;

  const loadAccounts = useCallback(async () => {
    if (!connector.connected) return;
    try {
      setAccounts((await api.connectorCredentials(connector.type)).credentials);
    } catch {
      setAccounts([]); // the list is an aid; losing it must not block replacing
    }
  }, [connector.type, connector.connected]);

  useEffect(() => {
    if (expanded) void loadAccounts();
  }, [expanded, loadAccounts]);

  async function save() {
    if (!secret.trim()) return;
    setSaving(true);
    try {
      await api.saveCredential(
        connector.type,
        secret.trim(),
        label.trim() || undefined,
        replacing ?? undefined,
      );
      setSecret("");
      setLabel("");
      setReplacing(null);
      await loadAccounts();
      onConnected();
    } finally {
      setSaving(false);
    }
  }

  async function removeAccount(id: string) {
    await api.deleteCredential(connector.type, id);
    if (replacing === id) setReplacing(null);
    await loadAccounts();
    onConnected();
  }

  async function runSync() {
    if (!onSync) return;
    setSyncing(true);
    setSyncNote(null);
    try {
      setSyncNote(await onSync());
    } catch (err) {
      setSyncNote(err instanceof Error ? err.message : "Sync failed.");
    } finally {
      setSyncing(false);
    }
  }

  return (
    <li className="connector-row">
      <div className="connector-head">
        <button
          type="button"
          className="connector-info connector-info-toggle"
          onClick={onToggle}
          aria-expanded={expanded}
        >
          <ConnectorMark type={connector.type} name={connector.name} />
          <span className="connector-text">
            <span className="connector-name">{connector.name}</span>
            {/* The bare category is a raw enum value ("build_activity") and is
                capitalized by CSS to read as a label. A `hint` is authored
                prose — a sentence, or a date — and must be left alone. */}
            <span className={hint ? "connector-category" : "connector-category enum"}>
              {hint ?? connector.category.replace("_", " ")}
            </span>
          </span>
        </button>
        {connector.connected ? (
          <span className="connector-actions">
            <span className="badge connected">Connected</span>
            {onSync && (
              <button className="secondary" onClick={runSync} disabled={syncing}>
                {syncing ? "Syncing…" : "Sync now"}
              </button>
            )}
            <button className="secondary" onClick={onToggle} aria-expanded={expanded}>
              {expanded ? "Close ▴" : "Configure ▾"}
            </button>
          </span>
        ) : (
          <button className="secondary" onClick={onToggle} aria-expanded={expanded}>
            {expanded ? "Cancel" : "Connect"}
          </button>
        )}
      </div>

      {syncNote && <p className="muted connector-sync-note">{syncNote}</p>}

      {/* Connected -> replace the credential, then the inline detail; not
          connected -> inline setup. Always directly under this row.

          Replace comes FIRST. It is the reason someone opened Configure on a
          connector that is already working — the detail below is a provider's
          own list of workspaces, keys or accounts, and on a busy connector that
          list pushed the credential form off the bottom of the screen. */}
      {expanded && connector.connected && (
        <div className="connector-panel connector-rotate">
          <div className="connector-rotate-head">
            <h4>{replacing || !multi ? `Replace ${noun}` : `Accounts`}</h4>
            <span className="muted">
              {multi && accounts && accounts.length > 1
                ? `${accounts.length} accounts — every one is read, and their cost is summed`
                : connector.credential_set_at
                  ? `Current ${noun} set ${setAtLabel(connector.credential_set_at)}`
                  : `A ${noun} is stored`}
            </span>
          </div>

          {/* One row per stored account. A provider can be billed through more
              than one organisation, and telling them apart is the whole reason
              a label exists — the secret itself is never shown. */}
          {multi && accounts && accounts.length > 0 && (
            <ul className="credential-list">
              {accounts.map((acc, i) => (
                <li key={acc.id} className={replacing === acc.id ? "is-replacing" : undefined}>
                  <span className="credential-name">{acc.label || `Account ${i + 1}`}</span>
                  <span className="muted credential-meta">
                    set {acc.updated_at ? setAtLabel(acc.updated_at) : "previously"}
                  </span>
                  <button
                    type="button"
                    className="linklike"
                    onClick={() => {
                      setReplacing(replacing === acc.id ? null : acc.id);
                      setSecret("");
                    }}
                    aria-label={`Replace ${acc.label || `account ${i + 1}`}`}
                  >
                    {replacing === acc.id ? "Cancel" : "Replace"}
                  </button>
                  <button
                    type="button"
                    className="linklike danger"
                    onClick={() => void removeAccount(acc.id)}
                    aria-label={`Remove ${acc.label || `account ${i + 1}`}`}
                    // Removing the last one would disconnect the source, which
                    // is what Disconnect is for and discards more than a key.
                    disabled={accounts.length < 2}
                    title={
                      accounts.length < 2
                        ? "The only account — replace it, or disconnect the source"
                        : undefined
                    }
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          )}

          <p className="muted connector-rotate-note">
            {!multi
              ? `Paste a new ${noun} to replace the stored one. The old ${noun} is deleted, and syncs use the new one from then on.`
              : replacing
                ? `Paste a new ${noun} for this account. The old one is deleted, and syncs use the new one from then on.`
                : `Add another ${noun} if this provider bills you through more than one account. Every account is read and their cost is summed.`}{" "}
            Meter never shows a stored credential back to you.
          </p>
          {multi && !replacing && (
            <input
              className="credential-label-input"
              placeholder="Account name (e.g. Acme Labs)"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              aria-label="Account name"
            />
          )}
          <CredentialForm
            connector={connector}
            guide={guide}
            secret={secret}
            onSecret={setSecret}
            onSave={save}
            saving={saving}
            saveLabel={!multi || replacing ? "Replace" : `Add ${noun}`}
          />
        </div>
      )}

      {expanded && connector.connected && detail && <div className="connector-panel">{detail}</div>}

      {expanded && !connector.connected && (
        <div className="connector-panel">
          {guide && (
            <div className="connector-guide">
              <p className="muted connector-guide-blurb">{guide.blurb}</p>
              <ol className="connector-steps">
                {guide.steps.map((step, i) => (
                  <li key={i}>{step}</li>
                ))}
              </ol>
              {guide.docUrl && (
                <a
                  className="link connector-doc-link"
                  href={guide.docUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Open provider setup page ↗
                </a>
              )}
            </div>
          )}
          <CredentialForm
            connector={connector}
            guide={guide}
            secret={secret}
            onSecret={setSecret}
            onSave={save}
            saving={saving}
            saveLabel="Save"
          />
        </div>
      )}
    </li>
  );
}

/** How long ago the stored credential was set, in words. */
function setAtLabel(iso: string): string {
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86_400_000);
  if (!Number.isFinite(days)) return "previously";
  if (days <= 0) return "today";
  if (days === 1) return "yesterday";
  if (days < 60) return `${days} days ago`;
  const months = Math.round(days / 30);
  return months < 24 ? `${months} months ago` : `${Math.round(days / 365)} years ago`;
}

/**
 * The one credential field, shared by connecting and replacing.
 *
 * Shared on purpose: two fields would be two chances for one of them to stop
 * masking its input, or to keep a secret in state after a save. This one is
 * `type="password"`, cleared by its caller on success, and never rendered with
 * a value it did not just receive from the person typing.
 */
function CredentialForm({
  connector,
  guide,
  secret,
  onSecret,
  onSave,
  saving,
  saveLabel,
}: {
  connector: ConnectorStatus;
  guide?: (typeof CONNECTOR_GUIDES)[string];
  secret: string;
  onSecret: (value: string) => void;
  onSave: () => void;
  saving: boolean;
  saveLabel: string;
}) {
  return (
    <div className="connector-form">
      {guide?.multiline ? (
        <textarea
          placeholder={guide.placeholder}
          value={secret}
          onChange={(e) => onSecret(e.target.value)}
          aria-label={`${connector.name} credentials`}
          rows={3}
        />
      ) : (
        <input
          type="password"
          placeholder={guide?.placeholder ?? "Paste access token"}
          value={secret}
          onChange={(e) => onSecret(e.target.value)}
          aria-label={`${connector.name} token`}
          autoComplete="off"
          spellCheck={false}
        />
      )}
      <button onClick={onSave} disabled={saving || !secret.trim()}>
        {saving ? "…" : saveLabel}
      </button>
    </div>
  );
}
