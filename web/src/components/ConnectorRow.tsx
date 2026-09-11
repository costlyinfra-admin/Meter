/**
 * One expandable cost-source card. The header shows the source, its status, and
 * its actions; clicking Connect (not-connected) or Configure (connected) expands a
 * panel *directly underneath this row* — never at the bottom of the page. Expansion
 * is controlled by the parent so the list behaves as an accordion (one open at a
 * time). Not-connected rows expand to the setup guide + credential form; connected
 * rows expand to the provider's inline detail (`detail`) and the form for
 * replacing the stored credential.
 *
 * Replacing is deliberately the same field as connecting, never a pre-filled or
 * masked one. There is no route that returns a stored secret — not partially —
 * so there is nothing to pre-fill with, and a row of dots that cannot be edited
 * only invites someone to try. The one thing shown about the existing
 * credential is when it was set, which is what makes rotation checkable.
 */
import { type ReactNode, useState } from "react";
import { api, type ConnectorStatus } from "../api";
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
  const [saving, setSaving] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [syncNote, setSyncNote] = useState<string | null>(null);

  const guide = CONNECTOR_GUIDES[connector.type];
  // Most connectors take a token; the multiline ones take a service-account
  // blob. Derived rather than spelled out on twenty guides, and only ever used
  // as a word in a sentence.
  const noun = guide?.multiline ? "credentials" : "token";

  async function save() {
    if (!secret.trim()) return;
    setSaving(true);
    try {
      await api.saveCredential(connector.type, secret.trim());
      setSecret("");
      onToggle(); // collapse
      onConnected();
    } finally {
      setSaving(false);
    }
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

      {/* Connected -> inline detail; not connected -> inline setup. Always directly
          under this row. */}
      {expanded && connector.connected && detail && <div className="connector-panel">{detail}</div>}

      {expanded && connector.connected && (
        <div className="connector-panel connector-rotate">
          <div className="connector-rotate-head">
            <h4>Replace {noun}</h4>
            <span className="muted">
              {connector.credential_set_at
                ? `Current ${noun} set ${setAtLabel(connector.credential_set_at)}`
                : `A ${noun} is stored`}
            </span>
          </div>
          <p className="muted connector-rotate-note">
            Paste a new {noun} to replace the stored one. The old {noun} is deleted, and syncs use
            the new one from then on. Meter never shows a stored credential back to you.
          </p>
          <CredentialForm
            connector={connector}
            guide={guide}
            secret={secret}
            onSecret={setSecret}
            onSave={save}
            saving={saving}
            saveLabel="Replace"
          />
        </div>
      )}

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
