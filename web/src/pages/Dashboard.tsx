/**
 * Features dashboard — the money screen (design §9.1).
 *
 * Build cost and inference cost live in SEPARATE columns and are never blended.
 * Each cost number links to the feature's drill-down, where its evidence trail
 * lives. An Unattributed row carries spend not yet mapped to a feature.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  api,
  ApiError,
  type BudgetForecast,
  type Dashboard as DashboardData,
  type RangeKind,
  type ReviewRange,
} from "../api";
import { CategoryBadge, ConfidenceBadge, ProductBadge, WorthBadge } from "../components/badges";
import { CustomerBreakdown } from "../components/CustomerBreakdown";
import { DeveloperBreakdown } from "../components/DeveloperBreakdown";
import { OnboardingChecklist } from "../components/OnboardingChecklist";
import { PeriodSelector } from "../components/PeriodSelector";
import { ProductBreakdown } from "../components/ProductBreakdown";
import {
  BudgetForecastPanel,
  KeyInsights,
  KpiRow,
  OpenActions,
  ProviderSpendPanel,
  SpendTrend,
  type SavingsSummary,
} from "../components/OverviewPanels";

interface SavingsState extends SavingsSummary {
  /** feature id -> potential monthly savings, for the table's column. */
  byFeature: Record<string, number>;
}
import { ProviderBreakdown, type SpendSource } from "../components/ProviderBreakdown";
import { compact, money, num } from "../format";

type OverviewTab = "products" | "features" | "providers" | "developers" | "customers";

/** Notify the app shell (which owns the alerts badge) to re-poll alert state. */
export const REFRESH_ALERTS_EVENT = "meter:refresh-alerts";

/** What the month-over-month delta is compared against, per selected range. */
const DELTA_LABEL: Record<RangeKind, string> = {
  this_month: "vs last month",
  last_month: "vs the month before",
  last_3_months: "vs prev 3 months",
  last_6_months: "vs prev 6 months",
  last_12_months: "vs prev 12 months",
  custom: "vs prior period",
};

/** "Aug 22, 4:07 PM" — short, local, no seconds. */
function shortStamp(d: Date): string {
  return d.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/** Per-source detail for the freshness tooltip. */
function freshnessDetail(inferenceAt: string | null, buildAt: string | null): string {
  const one = (label: string, iso: string | null) =>
    `${label}: ${iso ? shortStamp(new Date(iso)) : "never synced"}`;
  return `When cost was last ingested — ${one("Inference", inferenceAt)} · ${one("Build", buildAt)}`;
}

export function Dashboard() {
  const navigate = useNavigate();
  const [tab, setTab] = useState<OverviewTab>("features");
  // Which half of the By Provider tab is showing. Lifted here so the headline
  // card's "build" and "run" can open the matching one directly.
  const [providerSource, setProviderSource] = useState<SpendSource>("inference");
  const tabsRef = useRef<HTMLDivElement>(null);
  // Three months rather than one: a single month has no shape to it — the trend
  // is a lone bar and a period-over-period delta is the only comparison there
  // is. Three is the shortest window where the charts say something.
  const [range, setRange] = useState<ReviewRange>({ kind: "last_3_months" });
  const [data, setData] = useState<DashboardData | null>(null);
  // Loaded by its own request; see the effect below.
  const [savings, setSavings] = useState<SavingsState | null>(null);
  const [savingsFailed, setSavingsFailed] = useState(false);
  // Also its own request: the forecast reads the Optimize figure server-side,
  // so it inherits that subsystem's latency and must not gate the Overview.
  const [forecast, setForecast] = useState<BudgetForecast | null>(null);
  const [forecastFailed, setForecastFailed] = useState(false);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshNote, setRefreshNote] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setData(null);
    setError(null);
    api
      .dashboard(range)
      .then((d) => active && setData(d))
      .catch(
        (err) =>
          active &&
          setError(err instanceof ApiError ? err.message : "Could not load the dashboard."),
      );
    return () => {
      active = false;
    };
  }, [range, refreshKey]);

  // Refresh = pull fresh cost from every connected inference provider (current
  // month), then re-read everything the Overview shows plus the alerts badge
  // (owned by the app shell, signalled via a window event). A provider that fails
  // is reported rather than silently leaving stale numbers on screen.
  const refreshAll = async () => {
    setRefreshing(true);
    setRefreshNote(null);
    try {
      const r = await api.refreshInference();
      if (r.errors.length > 0) {
        setRefreshNote(
          `Could not refresh ${r.errors.map((e) => `${e.provider} (${e.error})`).join("; ")}`,
        );
      }
    } catch (err) {
      setRefreshNote(
        err instanceof ApiError ? err.message : "Could not pull fresh cost from providers.",
      );
    } finally {
      setRefreshing(false);
      setRefreshKey((k) => k + 1); // re-read regardless, so alerts/stored cost update
      window.dispatchEvent(new Event(REFRESH_ALERTS_EVENT));
    }
  };

  // Savings come from the Optimize engine, which computes per feature. It is
  // fetched on its own so a slow or failing calculation there can never delay
  // or break the Overview — the cards say "calculating" and the page is fine.
  useEffect(() => {
    let live = true;
    setSavings(null);
    setSavingsFailed(false);
    (async () => {
      try {
        // The endpoint takes a month (YYYY-MM); data.end is a full date.
        const overview = await api.copilotOverview(data?.end?.slice(0, 7));
        if (!live) return;
        setSavings({
          // Measured and modelled are both real opportunities; directional ones
          // are excluded because they carry no defensible number.
          potentialMonthly: overview.totals.measured + overview.totals.modeled_ceiling,
          realizedMonthly: overview.verified_monthly_savings,
          realizedAnnual: overview.verified_annual_savings,
          byFeature: Object.fromEntries(
            overview.by_feature.map((f) => [f.feature_id, f.measured + f.modeled_ceiling]),
          ),
        });
      } catch {
        // Wrapped rather than only .catch()'d: a synchronous throw would take
        // the Overview down with it, and the savings cards are the one part of
        // this page that depends on another subsystem.
        if (live) setSavingsFailed(true);
      }
    })();
    return () => {
      live = false;
    };
  }, [data?.end]);

  // The budget forecast is computed entirely on the server — the stored budget,
  // the observed daily spend, the org's timezone — and asked for on the same
  // window the page is showing.
  useEffect(() => {
    let live = true;
    setForecast(null);
    setForecastFailed(false);
    api
      .budgetForecast(range)
      .then((f) => live && setForecast(f))
      .catch(() => live && setForecastFailed(true));
    return () => {
      live = false;
    };
  }, [range, refreshKey]);

  // Filtering is on the name alone: it is what someone types a search box for,
  // and matching on numbers would make a stray digit hide rows without saying so.
  const visibleFeatures = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const rows = data?.features ?? [];
    return needle ? rows.filter((f) => f.name.toLowerCase().includes(needle)) : rows;
  }, [data, query]);

  const hasFeatures = !!data && data.features.length > 0;
  const hasBuild = !!data && data.totals.build_cost > 0;
  const hasInference = !!data && data.totals.inference_cost > 0;
  const setupComplete = hasFeatures && hasBuild && hasInference;
  const deltaLabel = DELTA_LABEL[range.kind];

  /** Open the By Provider tab on one half of the split, and bring it into view —
   *  the tabs sit well below the headline figures that link to them. */
  function showSource(source: SpendSource) {
    setTab("providers");
    setProviderSource(source);
    tabsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  return (
    <div className="content">
      <div className="dash-head">
        <div>
          <h1>Overview</h1>
          <p className="muted dash-sub">AI cost observability and optimization</p>
        </div>
        <div className="last-updated">
          {/* The period governs everything below it, so it sits with the title
              rather than between the summary and the breakdown. */}
          <PeriodSelector
            value={range}
            onChange={setRange}
            // What is actually on screen, which is the server's answer and not
            // always the selection's: a range can run past the months with data.
            resolved={
              data ? { start: data.start.slice(0, 7), end: data.end.slice(0, 7) } : undefined
            }
          />
          {data?.data_updated_at && (
            <span
              className="muted last-updated-text"
              title={freshnessDetail(data.inference_updated_at, data.build_updated_at)}
            >
              Updated {shortStamp(new Date(data.data_updated_at))}
            </span>
          )}
          <button
            type="button"
            className={refreshing ? "icon-button spinning" : "icon-button"}
            onClick={refreshAll}
            disabled={data === null || refreshing}
            aria-label="Refresh data and alerts"
            title="Pull fresh cost from connected providers, then reload"
          >
            <RefreshIcon />
          </button>
        </div>
      </div>

      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}

      {refreshNote && (
        <p className="error" role="alert">
          {refreshNote}
        </p>
      )}

      {data && !setupComplete && (
        <OnboardingChecklist
          hasFeatures={hasFeatures}
          hasBuild={hasBuild}
          hasInference={hasInference}
        />
      )}

      {data === null && !error && <p className="muted">Loading…</p>}

      {data && (
        <KpiRow
          data={data}
          savings={savings}
          savingsFailed={savingsFailed}
          deltaLabel={deltaLabel}
          onShowSource={showSource}
        />
      )}

      {data && (
        <div className="overview-grid">
          <KeyInsights insights={data.insights} />
          {/* The middle column: what it cost, then where that is heading. */}
          <div className="overview-middle">
            <SpendTrend trend={data.trend} />
            <BudgetForecastPanel trend={data.trend} forecast={forecast} failed={forecastFailed} />
          </div>
          <div className="overview-side">
            <ProviderSpendPanel providers={data.providers} />
            <OpenActions actions={data.actions} />
          </div>
        </div>
      )}

      {/* Tabs switch only the detailed breakdown below; the summary, insights,
          and totals above stay put no matter which tab is active. */}
      {data && (
        <div className="tabs" role="tablist" aria-label="Cost breakdown" ref={tabsRef}>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "products"}
            className={tab === "products" ? "tab active" : "tab"}
            onClick={() => setTab("products")}
          >
            By Product
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "features"}
            className={tab === "features" ? "tab active" : "tab"}
            onClick={() => setTab("features")}
          >
            By Feature
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "providers"}
            className={tab === "providers" ? "tab active" : "tab"}
            onClick={() => setTab("providers")}
          >
            By Provider
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "developers"}
            className={tab === "developers" ? "tab active" : "tab"}
            onClick={() => setTab("developers")}
          >
            By Developer
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "customers"}
            className={tab === "customers" ? "tab active" : "tab"}
            onClick={() => setTab("customers")}
          >
            By Customer
          </button>
          {tab === "features" && (
            <input
              type="search"
              className="tab-search"
              value={query}
              placeholder="Search features…"
              aria-label="Search features"
              onChange={(e) => setQuery(e.target.value)}
            />
          )}
        </div>
      )}

      {data && tab === "features" && data.features.length === 0 ? (
        <div className="empty-state">
          <p className="empty-title">No features yet</p>
          <p className="muted">
            Your dashboard fills in as you finish setup above — start by discovering features.
          </p>
        </div>
      ) : data && tab === "features" ? (
        <table className="features-table">
          <thead>
            <tr>
              <th>Feature</th>
              <th title="Which product this feature is part of">Product</th>
              <th title="What kind of feature this is">Type</th>
              <th className="num">Build cost</th>
              <th className="num">{data.months > 1 ? "Inference" : "Inference / mo"}</th>
              <th className="num">Active users</th>
              <th className="num">Cost / user</th>
              <th className="num">Requests</th>
              <th className="num" title="Monthly savings the Optimize engine can defend">
                Potential savings
              </th>
              <th title="Cost per active user, relative to your other features">Cost health</th>
              <th>Confidence</th>
            </tr>
          </thead>
          <tbody>
            {visibleFeatures.map((f) => (
              <tr
                key={f.feature_id}
                className="feature-row"
                onClick={() => navigate(`/features/${f.feature_id}`)}
              >
                <td>
                  <Link to={`/features/${f.feature_id}`} onClick={(e) => e.stopPropagation()}>
                    {f.name}
                  </Link>
                </td>
                <td>
                  <ProductBadge product={f.product_name} source={f.product_source} />
                </td>
                <td>
                  <CategoryBadge category={f.category} source={f.category_source} />
                </td>
                <td className="num">{money(f.build_cost)}</td>
                <td className="num">{money(f.inference_cost)}</td>
                <td className="num">{num(f.active_users)}</td>
                <td className="num">{money(f.cost_per_user)}</td>
                <td className="num" title="AI model calls this feature made">
                  {compact(f.requests)}
                </td>
                <td className="num savings-cell">
                  {/* Loaded separately; a dash means not yet known, which is not
                      the same answer as nothing to save. */}
                  {savings ? money(savings.byFeature[f.feature_id] ?? 0) : "—"}
                </td>
                <td>
                  <WorthBadge value={f.worth_it} />
                </td>
                <td>
                  <ConfidenceBadge level={f.confidence} />
                </td>
              </tr>
            ))}
            <tr className="unattributed-row">
              <td>Unattributed</td>
              <td className="muted">—</td>
              <td className="muted">—</td>
              <td className="num">{money(data.unattributed.build_cost)}</td>
              <td className="num">{money(data.unattributed.inference_cost)}</td>
              <td className="num">—</td>
              <td className="num">—</td>
              <td className="num">—</td>
              <td className="num">—</td>
              <td colSpan={2} className="muted">
                spend not yet mapped to a feature
              </td>
            </tr>
          </tbody>
        </table>
      ) : null}

      {data && tab === "features" && (
        <p className="muted legend">
          "Cost health" is directional (cost per active user), not a revenue-based ROI.
        </p>
      )}

      {data && tab === "products" && <ProductBreakdown range={range} refreshKey={refreshKey} />}

      {data && tab === "providers" && (
        <ProviderBreakdown
          range={range}
          refreshKey={refreshKey}
          source={providerSource}
          onSourceChange={setProviderSource}
        />
      )}

      {data && tab === "developers" && <DeveloperBreakdown range={range} refreshKey={refreshKey} />}

      {data && tab === "customers" && <CustomerBreakdown range={range} refreshKey={refreshKey} />}
    </div>
  );
}

/** Circular-arrow refresh glyph. */
function RefreshIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M21 12a9 9 0 1 1-2.64-6.36" />
      <polyline points="21 3 21 9 15 9" />
    </svg>
  );
}
