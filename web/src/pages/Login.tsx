import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError } from "../api";
import { useAuth } from "../auth/AuthContext";
import { DEMO_EMAIL, DEMO_PASSWORD } from "../demo";
import { AuthForm } from "./AuthForm";

export function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [demoError, setDemoError] = useState<string | null>(null);
  const [demoBusy, setDemoBusy] = useState(false);

  async function viewDemo() {
    setDemoError(null);
    setDemoBusy(true);
    try {
      await login(DEMO_EMAIL, DEMO_PASSWORD);
      navigate("/");
    } catch (err) {
      setDemoError(err instanceof ApiError ? err.message : "The demo isn't available right now.");
      setDemoBusy(false);
    }
  }

  const demoNote = (
    <div className="demo-callout">
      <p className="demo-title">Not ready to create an account?</p>
      <p className="muted">See a live demo dashboard — no signup needed.</p>
      {demoError && (
        <p className="error" role="alert">
          {demoError}
        </p>
      )}
      <button className="secondary" onClick={viewDemo} disabled={demoBusy}>
        {demoBusy ? "Opening…" : "View the demo"}
      </button>
    </div>
  );

  return (
    <AuthForm
      title="Sign in"
      submitLabel="Sign in"
      onSubmit={async (email, password) => {
        await login(email, password);
        navigate("/");
      }}
      footer={{ prompt: "New to Meter?", linkLabel: "Create an account", to: "/signup" }}
      note={demoNote}
    />
  );
}
