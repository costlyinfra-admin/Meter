/**
 * App routes. Public: /login, /signup. Everything else requires a session and
 * renders inside the AppShell (sidebar + content). Onboarding is no longer a
 * separate wizard — it's a setup checklist on the Overview.
 */
import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth/AuthContext";
import { AdminShell } from "./components/AdminShell";
import { AppShell } from "./components/AppShell";
import { AlertDetailPage } from "./pages/AlertDetailPage";
import { AlertFormPage } from "./pages/AlertFormPage";
import { AlertsPage } from "./pages/AlertsPage";
import { CopilotPage } from "./pages/CopilotPage";
import { ProductsPage } from "./pages/ProductsPage";
import { PromptDetail, PromptsPage } from "./pages/PromptsPage";
import { CostSourcesPage } from "./pages/CostSourcesPage";
import { ReconciliationPage } from "./pages/ReconciliationPage";
import { Dashboard } from "./pages/Dashboard";
import { FeatureDetail } from "./pages/FeatureDetail";
import { TracesPage } from "./pages/TracesPage";
import { TraceDetail } from "./pages/TraceDetail";
import { ApplicationsPage, ApplicationDetail } from "./pages/ApplicationsPage";
import { FeaturesPage } from "./pages/FeaturesPage";
import { InstallSdkPage } from "./pages/InstallSdkPage";
import { Login } from "./pages/Login";
import { HelpPage } from "./pages/HelpPage";
import { SettingsPage } from "./pages/SettingsPage";
import { Signup } from "./pages/Signup";
import { AdminConnectors } from "./pages/admin/AdminConnectors";
import { AdminCustomerDetail } from "./pages/admin/AdminCustomerDetail";
import { AdminCustomers } from "./pages/admin/AdminCustomers";
import { AdminDashboard } from "./pages/admin/AdminDashboard";
import { AdminErrors } from "./pages/admin/AdminErrors";
import { AdminSyncHistory } from "./pages/admin/AdminSyncHistory";

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) return <div className="page-center muted">Loading…</div>;
  if (!user) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

function RequireAdmin({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) return <div className="page-center muted">Loading…</div>;
  if (!user) return <Navigate to="/login" replace />;
  if (!user.is_admin) return <Navigate to="/" replace />;
  return <>{children}</>;
}

function PublicOnly({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) return <div className="page-center muted">Loading…</div>;
  if (user) return <Navigate to="/" replace />;
  return <>{children}</>;
}

export function App() {
  return (
    <Routes>
      <Route
        path="/login"
        element={
          <PublicOnly>
            <Login />
          </PublicOnly>
        }
      />
      <Route
        path="/signup"
        element={
          <PublicOnly>
            <Signup />
          </PublicOnly>
        }
      />
      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route path="/" element={<Dashboard />} />
        <Route path="/products" element={<ProductsPage />} />
        <Route path="/optimize" element={<CopilotPage />} />
        <Route path="/optimize/prompts" element={<PromptsPage />} />
        <Route path="/optimize/prompts/:id" element={<PromptDetail />} />
        <Route path="/cost-sources" element={<CostSourcesPage />} />
        {/* Provider invoice reconciliation — an opt-in module. The routes exist
            always; every request they make is refused unless the organization
            has enabled it, and the nav entry only appears when it has. */}
        <Route path="/reconciliation" element={<ReconciliationPage />} />
        <Route path="/reconciliation/:view" element={<ReconciliationPage />} />
        <Route path="/reconciliation/runs/:runId" element={<ReconciliationPage />} />
        <Route path="/applications" element={<ApplicationsPage />} />
        <Route path="/applications/:id" element={<ApplicationDetail />} />
        <Route path="/traces" element={<TracesPage />} />
        <Route path="/traces/:id" element={<TraceDetail />} />
        <Route path="/features" element={<FeaturesPage />} />
        <Route path="/features/:id" element={<FeatureDetail />} />
        <Route path="/install-sdk" element={<InstallSdkPage />} />
        <Route path="/alerts" element={<AlertsPage />} />
        <Route path="/alerts/new" element={<AlertFormPage />} />
        <Route path="/alerts/:id" element={<AlertDetailPage />} />
        <Route path="/alerts/:id/edit" element={<AlertFormPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/help" element={<HelpPage />} />
        <Route path="/help/:category/:topic" element={<HelpPage />} />
      </Route>
      <Route
        path="/admin"
        element={
          <RequireAdmin>
            <AdminShell />
          </RequireAdmin>
        }
      >
        <Route index element={<AdminDashboard />} />
        <Route path="customers" element={<AdminCustomers />} />
        <Route path="customers/:id" element={<AdminCustomerDetail />} />
        <Route path="connectors" element={<AdminConnectors />} />
        <Route path="sync-history" element={<AdminSyncHistory />} />
        <Route path="errors" element={<AdminErrors />} />
      </Route>
      <Route path="/dashboard" element={<Navigate to="/" replace />} />
      <Route path="/onboarding" element={<Navigate to="/" replace />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
