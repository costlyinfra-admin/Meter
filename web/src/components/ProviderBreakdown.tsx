/**
 * Overview "By provider" tab — tenant-wide spend by source over the Overview's
 * selected review period. Inference (run) and build cost never blend (invariant 2),
 * so they live on two sub-tabs here: Inference cost (default — by provider, by
 * token type, by workspace/API key, by customer) and Build cost (by coding tool).
 */
import { useEffect, useState } from "react";
import { api, type ProviderSpend, type ReviewRange } from "../api";
import { compact, money, prettyTool } from "../format";
import { ClassificationTrendChart } from "./ClassificationTrendChart";
import { SpendBars } from "./SpendBars";
import { TrendChart } from "./TrendChart";

const EMPTY: ProviderSpend = {
  start: "",
  end: "",
  total: 0,
  by_provider: [],
  trend: [],
  daily_trend: [],
  build_total: 0,
  build_by_tool: [],
  build_by_developer: [],
  developer_activity: [],
  activity_coverage: {
    github_connected: false,
    dated_prs: 0,
    undated_prs: 0,
    first_merged: null,
    last_merged: null,
    runs: 0,
    covered_from: null,
    covered_to: null,
    last_run_at: null,
    last_run_status: null,
    last_run_trigger: null,
  },
  build_trend: [],
  customer_total: 0,
  by_customer: [],
  token_total: 0,
  by_token_type: [],
  workspace_total: 0,
  by_workspace: [],
};

/** Which half of the split this tab is showing. Build and inference never blend,
 *  so there is always exactly one of them on screen. */
export type SpendSource = "inference" | "build";

export function ProviderBreakdown({
  range,
  refreshKey = 0,
  source,
  onSourceChange,
}: {
  range: ReviewRange;
  /** Bumped by the Overview's refresh control to re-pull this breakdown. */
  refreshKey?: number;
  /** Controlled by the Overview so the headline card's "build" and "run" can
   *  open the matching half directly. */
  source: SpendSource;
  onSourceChange: (source: SpendSource) => void;
}) {
  const [data, setData] = useState<ProviderSpend | null>(null);
  const sourceTab = source;
  const setSourceTab = onSourceChange;

  useEffect(() => {
    let active = true;
    setData(null);
    api
      .providerSpend(range)
      .then((d) => active && setData(d))
      .catch(() => active && setData(EMPTY));
    return () => {
      active = false;
    };
  }, [range, refreshKey]);

  const nothing = data && data.by_provider.length === 0 && data.build_by_tool.length === 0;
  // Use the day-resolution trend for short ranges (a single month), the monthly
  // rollup for longer ones — and only when daily data is actually present.
  const useDaily =
    (range.kind === "this_month" || range.kind === "last_month") &&
    (data?.daily_trend.length ?? 0) > 0;

  return (
    <>
      <div className="section-head breakdown-head">
        <div>
          <h2>Spend by source</h2>
          <span className="section-sub muted">
            Inference (run) and build cost are never blended — switch between them below.
          </span>
        </div>
      </div>

      {data === null ? (
        <p className="muted">Loading…</p>
      ) : nothing ? (
        <div className="empty-state">
          <p className="empty-title">No cost yet</p>
          <p className="muted">
            Connect sources on Cost sources and run a sync to see spend by provider and tool.
          </p>
        </div>
      ) : (
        <>
          {/* Inference (run) and build cost never blend — split into two views. */}
          <div className="tabs" role="tablist" aria-label="Spend by source">
            <button
              type="button"
              role="tab"
              aria-selected={sourceTab === "inference"}
              className={sourceTab === "inference" ? "tab active" : "tab"}
              onClick={() => setSourceTab("inference")}
            >
              Inference cost
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={sourceTab === "build"}
              className={sourceTab === "build" ? "tab active" : "tab"}
              onClick={() => setSourceTab("build")}
            >
              Build cost
            </button>
          </div>
        </>
      )}

      {data && !nothing && sourceTab === "inference" && (
        <>
          <section className="detail-section">
            <h3 className="breakdown-subhead">Inference (run) cost by provider</h3>
            {data.by_provider.length === 0 ? (
              <p className="muted">No inference cost in this window.</p>
            ) : (
              <div className="inference-body">
                <div className="inference-col">
                  <span className="chart-title">
                    {useDaily ? "Daily trend · by classification" : "Trend · by classification"}
                  </span>
                  <ClassificationTrendChart
                    trend={useDaily ? data.daily_trend : data.trend}
                    granularity={useDaily ? "day" : "month"}
                  />
                </div>
                <div className="inference-col">
                  <span className="chart-title">By provider · {money(data.total)} total</span>
                  <SpendBars
                    rows={data.by_provider.map((p) => ({
                      label: p.provider,
                      amount: p.amount,
                      pct: p.pct,
                      models: p.by_model.map((m) => ({
                        label: m.model,
                        amount: m.amount,
                        pct: m.pct,
                      })),
                    }))}
                  />
                </div>
              </div>
            )}
          </section>

          {/* What KIND of tokens the spend went on. */}
          {data.by_token_type.length > 0 && (
            <section className="detail-section">
              <h3 className="breakdown-subhead">Inference cost by token type</h3>
              <p className="section-sub muted">
                Token counts are reported by the provider. The dollar split is{" "}
                <strong>derived</strong>, not billed: providers charge per line item, not per token
                type, so we weight each type by its published rate (cache writes cost more, and more
                again at a 1-hour TTL; cache reads cost less) and apportion the real{" "}
                {money(data.token_total)} bill — the parts always sum back to it.
              </p>
              <SpendBars
                rows={data.by_token_type.map((t) => ({
                  label: t.label,
                  amount: t.amount,
                  pct: t.pct,
                  meta: t.tokens > 0 ? `${compact(t.tokens)} tok` : undefined,
                }))}
              />
            </section>
          )}

          {/* Provider resource identity (today: Anthropic workspaces + API keys). */}
          {data.by_workspace.length > 0 && (
            <section className="detail-section">
              <h3 className="breakdown-subhead">Inference cost by workspace &amp; API key</h3>
              <p className="section-sub muted">
                Each provider workspace, broken down by the API key that spent —{" "}
                {money(data.workspace_total)} total.
              </p>
              <SpendBars
                rows={data.by_workspace.map((w) => ({
                  label: w.workspace,
                  amount: w.amount,
                  pct: w.pct,
                  meta: w.tokens > 0 ? `${compact(w.tokens)} tok` : undefined,
                  models: w.by_key.map((k) => ({
                    label: k.api_key,
                    amount: k.amount,
                    pct: k.pct,
                    meta: k.tokens > 0 ? `${compact(k.tokens)} tok` : undefined,
                  })),
                }))}
              />
            </section>
          )}

          {/* Only when the metering SDK has tagged customers (metadata.customer_id). */}
          {data.by_customer.length > 0 && (
            <section className="detail-section">
              <h3 className="breakdown-subhead">Inference cost by customer</h3>
              <p className="section-sub muted">
                From metered (SDK) calls tagged with a customer — your top spenders.
              </p>
              <SpendBars
                rows={data.by_customer.map((c) => ({
                  label: c.customer_id,
                  amount: c.amount,
                  pct: c.pct,
                }))}
              />
            </section>
          )}
        </>
      )}

      {data && !nothing && sourceTab === "build" && (
        <section className="detail-section">
          <h3 className="breakdown-subhead">Build cost by tool</h3>
          {data.build_by_tool.length === 0 ? (
            <p className="muted">No build cost in this window.</p>
          ) : (
            <div className="inference-body">
              <div className="inference-col">
                <span className="chart-title">Trend</span>
                <TrendChart trend={data.build_trend} />
              </div>
              <div className="inference-col">
                <span className="chart-title">By tool · {money(data.build_total)} total</span>
                <SpendBars
                  rows={data.build_by_tool.map((t) => ({
                    label: prettyTool(t.tool),
                    amount: t.amount,
                    pct: t.pct,
                  }))}
                />
              </div>
            </div>
          )}
        </section>
      )}
    </>
  );
}
