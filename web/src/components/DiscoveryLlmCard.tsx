/**
 * Settings → "Feature discovery model" (BYOK).
 *
 * Optional per tenant. Configured, discovery clusters PR metadata through the
 * tenant's own OpenAI-compatible endpoint; left alone, it uses Meter's, and
 * nothing about discovery changes.
 *
 * The API key is write-only by design: the server never returns it, so this card
 * only ever knows *whether* one is stored (`has_key`). Editing the model or the
 * endpoint therefore does not require re-entering it — an empty key field on
 * save means "keep the one you have".
 */
import { useEffect, useState } from "react";
import { api, ApiError, type DiscoveryLlm, type DiscoveryLlmProviders } from "../api";

type Draft = { provider: string; base_url: string; model: string; api_key: string };

const EMPTY: Draft = { provider: "groq", base_url: "", model: "", api_key: "" };

export function DiscoveryLlmCard() {
  const [config, setConfig] = useState<DiscoveryLlm | null>(null);
  const [meta, setMeta] = useState<DiscoveryLlmProviders | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState<null | "save" | "test" | "toggle" | "remove">(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.discoveryLlm(), api.discoveryLlmProviders()])
      .then(([c, m]) => {
        setConfig(c);
        setMeta(m);
        setDraft({
          provider: c.provider ?? "groq",
          base_url: c.base_url ?? "",
          model: c.model ?? m.default_model,
          api_key: "",
        });
      })
      .catch(() => setError("Could not load the discovery model settings."));
  }, []);

  function pickProvider(value: string) {
    const known = meta?.providers.find((p) => p.value === value);
    // Prefill the endpoint, but leave it editable: a provider can change its
    // path, and a private deployment has its own.
    setDraft((d) => ({ ...d, provider: value, base_url: known?.base_url ?? "" }));
    setNote(null);
  }

  async function run<T>(kind: NonNullable<typeof busy>, fn: () => Promise<T>) {
    setBusy(kind);
    setError(null);
    setNote(null);
    try {
      return await fn();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong.");
      return null;
    } finally {
      setBusy(null);
    }
  }

  async function save() {
    const saved = await run("save", () =>
      api.saveDiscoveryLlm({
        provider: draft.provider,
        base_url: draft.base_url,
        model: draft.model,
        // Omitted when blank, so editing keeps the stored key.
        ...(draft.api_key.trim() ? { api_key: draft.api_key.trim() } : {}),
        enabled: true,
      }),
    );
    if (saved) {
      setConfig(saved);
      setDraft((d) => ({ ...d, api_key: "" })); // never keep the secret around
      setEditing(false);
      setNote("Saved. Discovery will use your model from now on.");
    }
  }

  async function test() {
    const result = await run("test", () =>
      api.testDiscoveryLlm({
        provider: draft.provider,
        base_url: draft.base_url,
        model: draft.model,
        ...(draft.api_key.trim() ? { api_key: draft.api_key.trim() } : {}),
      }),
    );
    if (result) {
      if (result.ok) setNote(`Connected — ${result.model} responded.`);
      else setError(result.error || "The provider rejected the request.");
    }
  }

  if (!config || !meta) {
    return (
      <section className="settings-card">
        <h2>Feature discovery model</h2>
        <p className="muted">{error ?? "Loading…"}</p>
      </section>
    );
  }

  const active = config.configured && config.enabled;

  return (
    <section className="settings-card">
      <h2>Feature discovery model</h2>
      <p className="muted settings-hint">
        Feature discovery groups your merged pull requests into features using an LLM. By default it
        runs on Meter's own model at no cost to you. Point it at your own OpenAI-compatible
        endpoint to keep that traffic and spend on your account. Only PR titles, branches and labels
        are ever sent — never source code.
      </p>

      <p className="detail-meta">
        <span className={active ? "badge conf-high" : "badge"}>
          {active ? "Using your model" : "Using Meter's model"}
        </span>
        {config.configured && !editing && (
          <span className="muted">
            {config.provider} · {config.model}
          </span>
        )}
      </p>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {note && <p className="hint">{note}</p>}

      {config.configured && !editing ? (
        <div className="settings-actions">
          <button className="secondary" onClick={() => setEditing(true)}>
            Edit
          </button>
          <button className="secondary" disabled={busy !== null} onClick={test}>
            {busy === "test" ? "Testing…" : "Test connection"}
          </button>
          <button
            className="secondary"
            disabled={busy !== null}
            onClick={async () => {
              const next = await run("toggle", () => api.setDiscoveryLlmEnabled(!config.enabled));
              if (next) setConfig(next);
            }}
          >
            {config.enabled ? "Use Meter's model" : "Use my model"}
          </button>
          <button
            className="link danger"
            disabled={busy !== null}
            onClick={async () => {
              const next = await run("remove", () => api.removeDiscoveryLlm());
              if (next) {
                setConfig(next);
                setDraft({ ...EMPTY, model: meta.default_model });
              }
            }}
          >
            Remove
          </button>
        </div>
      ) : (
        <>
          <div className="settings-field">
            <label htmlFor="byok-provider">Provider</label>
            <select
              id="byok-provider"
              value={draft.provider}
              onChange={(e) => pickProvider(e.target.value)}
            >
              {meta.providers.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.value === "custom" ? "Custom (OpenAI-compatible)" : p.value}
                </option>
              ))}
            </select>
          </div>

          <div className="settings-field">
            <label htmlFor="byok-base-url">Base URL</label>
            <input
              id="byok-base-url"
              value={draft.base_url}
              placeholder="https://api.groq.com/openai/v1"
              onChange={(e) => setDraft((d) => ({ ...d, base_url: e.target.value }))}
            />
          </div>

          <div className="settings-field">
            <label htmlFor="byok-model">Model</label>
            <input
              id="byok-model"
              value={draft.model}
              placeholder={meta.default_model}
              onChange={(e) => setDraft((d) => ({ ...d, model: e.target.value }))}
            />
          </div>

          <div className="settings-field">
            <label htmlFor="byok-key">API key</label>
            <input
              id="byok-key"
              type="password"
              autoComplete="off"
              value={draft.api_key}
              placeholder={config.has_key ? "Stored — leave blank to keep it" : "Your provider key"}
              onChange={(e) => setDraft((d) => ({ ...d, api_key: e.target.value }))}
            />
            <span className="muted settings-hint">
              Stored encrypted and never shown again — not here, not in the API, not in logs.
            </span>
          </div>

          <div className="settings-actions">
            <button disabled={busy !== null} onClick={save}>
              {busy === "save" ? "Saving…" : "Save"}
            </button>
            <button className="secondary" disabled={busy !== null} onClick={test}>
              {busy === "test" ? "Testing…" : "Test connection"}
            </button>
            {config.configured && (
              <button className="secondary" onClick={() => setEditing(false)}>
                Cancel
              </button>
            )}
          </div>
        </>
      )}
    </section>
  );
}
