/**
 * Where GitHub sends an admin back after "Connect with GitHub".
 *
 * GitHub returns a one-time `code` (and usually the `installation_id` just
 * made). The server proves with the code which installations this person can
 * actually see, and nothing is stored until they confirm one here. That click
 * is the point: a link someone else crafted would otherwise connect this
 * organization's Meter to the sender's repositories just by being opened.
 */
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, ApiError, type GitHubAppInstallation } from "../api";
import { GITHUB_RETURN_KEY } from "../components/GitHubAppOption";
import { toast } from "../toast";

type State =
  | { kind: "checking" }
  | { kind: "choose"; installations: GitHubAppInstallation[] }
  | { kind: "requested" }
  | { kind: "error"; message: string };

function returnPath(): string {
  try {
    const saved = sessionStorage.getItem(GITHUB_RETURN_KEY);
    sessionStorage.removeItem(GITHUB_RETURN_KEY);
    // Only a path inside Meter: never somewhere a stored value could point.
    if (saved && saved.startsWith("/") && !saved.startsWith("//")) return saved;
  } catch {
    // Storage blocked: the Features page is where GitHub is connected anyway.
  }
  return "/features";
}

export function GitHubCallbackPage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const [state, setState] = useState<State>({ kind: "checking" });
  const [connecting, setConnecting] = useState<number | null>(null);
  // The code works once. React runs effects twice in development, and a
  // second exchange would fail and replace a good answer with an error.
  const asked = useRef(false);

  const code = params.get("code");
  const installationParam = params.get("installation_id");
  const setupAction = params.get("setup_action");

  useEffect(() => {
    if (asked.current) return;
    asked.current = true;
    if (setupAction === "request") {
      // A member asked; an owner has to approve before there is anything to read.
      setState({ kind: "requested" });
      return;
    }
    if (!code) {
      setState({
        kind: "error",
        message: "GitHub didn't send a sign-in back to Meter. Try connecting again.",
      });
      return;
    }
    const installationId = installationParam ? Number(installationParam) : undefined;
    api
      .githubAppVerify(
        code,
        installationId && Number.isFinite(installationId) ? installationId : undefined,
      )
      .then(({ installations }) =>
        setState(
          installations.length === 0
            ? {
                kind: "error",
                message:
                  "Your GitHub account can't see an installation of Meter yet. Install it on an organization, then try again.",
              }
            : { kind: "choose", installations },
        ),
      )
      .catch((err) =>
        setState({
          kind: "error",
          message: err instanceof ApiError ? err.message : "Could not reach GitHub.",
        }),
      );
  }, [code, installationParam, setupAction]);

  async function connect(installation: GitHubAppInstallation) {
    setConnecting(installation.installation_id);
    try {
      await api.githubAppConnect(installation.claim);
      navigate(returnPath(), { replace: true });
      toast(`GitHub connected: ${installation.account}`);
    } catch (err) {
      setConnecting(null);
      setState({
        kind: "error",
        message: err instanceof ApiError ? err.message : "Could not connect GitHub.",
      });
    }
  }

  return (
    <div className="content github-callback">
      <div className="dash-head">
        <h1>Connect GitHub</h1>
      </div>
      <section className="panel">
        {state.kind === "checking" && (
          <p className="muted" aria-live="polite">
            Checking with GitHub…
          </p>
        )}

        {state.kind === "requested" && (
          <>
            <p>
              GitHub has sent your request to your organization's owners. Once one of them approves
              Meter, come back and choose <strong>Connect with GitHub</strong> again.
            </p>
            <Link to="/features">Back to Features</Link>
          </>
        )}

        {state.kind === "error" && (
          <>
            <p className="error" role="alert">
              {state.message}
            </p>
            <p className="muted">
              You can also connect by pasting a personal access token on the Features page.
            </p>
            <Link to="/features">Back to Features</Link>
          </>
        )}

        {state.kind === "choose" && (
          <>
            <p>
              {state.installations.length === 1
                ? "Connect this GitHub organization to Meter?"
                : "Which GitHub organization should Meter read?"}{" "}
              Meter will read pull requests in the repositories chosen on GitHub, and nothing else.
              This replaces any GitHub connection Meter already has.
            </p>
            <ul className="github-installations">
              {state.installations.map((inst) => (
                <li key={inst.installation_id}>
                  <span className="github-installation-name">
                    <strong>{inst.account}</strong>
                    <span className="muted">
                      {inst.account_type === "User" ? "Personal account" : "Organization"} ·{" "}
                      {inst.repository_selection === "all"
                        ? "all repositories"
                        : "selected repositories"}
                    </span>
                  </span>
                  <button
                    type="button"
                    onClick={() => void connect(inst)}
                    disabled={connecting !== null}
                  >
                    {connecting === inst.installation_id
                      ? "Connecting…"
                      : `Connect ${inst.account}`}
                  </button>
                </li>
              ))}
            </ul>
            <Link to="/features">Cancel</Link>
          </>
        )}
      </section>
    </div>
  );
}
