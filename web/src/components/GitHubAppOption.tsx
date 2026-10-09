/**
 * "Connect with GitHub": the sign-in option on the GitHub card, beside pasting
 * a personal access token.
 *
 * The admin is sent to GitHub's own screen, signs in there, and picks the
 * organization and the repositories Meter may read. Nothing is typed into
 * Meter. They come back to /github/callback (GitHubCallbackPage), which proves
 * the installation is theirs before anything is stored.
 *
 * Renders nothing when this deployment has not set the option up, so the card
 * is exactly the token form it always was.
 */
import { useEffect, useState } from "react";
import { api, type GitHubAppStatus } from "../api";

/** Where the callback page returns to: the page the admin started from. */
export const GITHUB_RETURN_KEY = "meter.github.return";

export function GitHubAppOption({ connected }: { connected: boolean }) {
  const [status, setStatus] = useState<GitHubAppStatus | null>(null);

  useEffect(() => {
    let live = true;
    api
      .githubApp()
      .then((s) => live && setStatus(s))
      // The token form below still works; losing this option must not block it.
      .catch(() => live && setStatus(null));
    return () => {
      live = false;
    };
  }, [connected]);

  if (!status?.configured || !status.install_url) return null;
  const installUrl = status.install_url;

  function start() {
    try {
      sessionStorage.setItem(GITHUB_RETURN_KEY, window.location.pathname);
    } catch {
      // Private mode: the callback falls back to the Features page.
    }
    window.location.assign(installUrl);
  }

  const app = status.connection;
  if (app) {
    return (
      <div className="github-app-option connected">
        <p>
          Connected by signing in with GitHub
          {app.account ? (
            <>
              {" "}
              to <strong>{app.account}</strong>
            </>
          ) : null}
          . Meter reads only the repositories chosen there, and only pull requests.
        </p>
        <div className="github-app-actions">
          {app.manage_url && (
            <a className="link" href={app.manage_url} target="_blank" rel="noopener noreferrer">
              Change repositories on GitHub ↗
            </a>
          )}
          <button type="button" className="secondary" onClick={start}>
            Connect a different organization
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="github-app-option">
      <h4>Sign in with GitHub (recommended)</h4>
      <p className="muted">
        Sign in on GitHub, pick your organization and the repositories Meter may read. Meter can
        only read pull requests, and nothing is copied or pasted. You need to be an owner of the
        organization, or GitHub will ask one to approve it.
      </p>
      <button type="button" onClick={start}>
        Connect with GitHub
      </button>
      <p className="github-app-or muted">
        <span>
          {connected ? "or replace the stored token" : "or paste a personal access token"}
        </span>
      </p>
    </div>
  );
}
