/**
 * Overview "By Customer" tab — what it costs to serve each of the tenant's own
 * customers, in machines and in people.
 *
 * Two different kinds of number share this screen, and the whole design is
 * about not letting them blur:
 *
 *   **Metered AI cost** comes only from SDK-metered calls carrying
 *   `metadata.customer_id`. A provider bill says what was spent, never on whose
 *   behalf, so this is a SUBSET of the authoritative inference bill — the
 *   coverage line says how big a subset, every time.
 *
 *   **Human cost** is hours the tenant uploaded, priced at the loaded rate they
 *   supplied. Meter neither estimates hours nor holds a rate table.
 *
 * Their sum is labelled *customer-attributed delivery cost* and is never called
 * the AI bill: it covers only the customers on this screen and only their
 * metered calls. Build cost is not here at all — it is what the team spent
 * MAKING a feature, it has no customer, and invariant 2 keeps it separate.
 *
 * Missing human data renders as "Not provided", never as $0 — "nobody logged
 * hours" and "nobody worked" are different facts, and the second one would be a
 * lie about the customer this feature is most useful for.
 */
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, type CustomerSpend, type ReviewRange } from "../api";
import { compact, money, num, unitMoney } from "../format";
import { DeliveryCostTrend } from "./DeliveryCostTrend";
import { HumanEffortImport } from "./HumanEffortImport";
import { SpendBars } from "./SpendBars";

/** Top slice shown as bars; the rest stay in the table below. */
const TOP_N = 8;

type Customer = CustomerSpend["customers"][number];
type Metric = "delivery" | "ai" | "human_cost" | "human_hours";

const METRICS: { key: Metric; label: string; of: (c: Customer) => number | null }[] = [
  { key: "delivery", label: "Delivery cost", of: (c) => c.total_delivery_cost },
  { key: "ai", label: "AI cost", of: (c) => c.amount },
  { key: "human_cost", label: "Human cost", of: (c) => c.human_cost },
  { key: "human_hours", label: "Human hours", of: (c) => c.human_hours },
];

/** Nothing recorded — said in words, because 0 would be a different claim. */
function NotProvided({ title }: { title?: string }) {
  return (
    <span className="muted not-provided" title={title}>
      Not provided
    </span>
  );
}

export function CustomerBreakdown({
  range,
  refreshKey = 0,
}: {
  range: ReviewRange;
  /** Bumped by the Overview's refresh control to re-pull this breakdown. */
  refreshKey?: number;
}) {
  const [data, setData] = useState<CustomerSpend | null>(null);
  const [failed, setFailed] = useState(false);
  const [reload, setReload] = useState(0);
  const [metric, setMetric] = useState<Metric | null>(null);

  useEffect(() => {
    let active = true;
    setData(null);
    setFailed(false);
    api
      .customerSpend(range)
      .then((d) => active && setData(d))
      .catch(() => active && setFailed(true));
    return () => {
      active = false;
    };
  }, [range, refreshKey, reload]);

  // Delivery cost is the point of the screen when there is effort to include;
  // without it, ranking by "delivery" would just be AI cost under a name that
  // promises more than it shows.
  const effective: Metric = metric ?? (data?.human_effort_present ? "delivery" : "ai");
  const chosen = METRICS.find((m) => m.key === effective) ?? METRICS[1];

  const ranked = useMemo(() => {
    if (!data) return [];
    return [...data.customers]
      .filter((c) => (chosen.of(c) ?? 0) > 0)
      .sort((a, b) => (chosen.of(b) ?? 0) - (chosen.of(a) ?? 0));
  }, [data, chosen]);

  const rankedTotal = ranked.reduce((sum, c) => sum + (chosen.of(c) ?? 0), 0);

  return (
    <>
      <div className="section-head breakdown-head">
        <div>
          <h2>Customer economics</h2>
          <span className="section-sub muted">
            Understand the metered AI and human effort used to serve each customer.
          </span>
        </div>
        <HumanEffortImport onImported={() => setReload((n) => n + 1)} />
      </div>

      {failed ? (
        <p className="muted">Couldn't load customer spend.</p>
      ) : data === null ? (
        <p className="muted">Loading…</p>
      ) : data.customers.length === 0 ? (
        <NoCustomerData />
      ) : (
        <>
          <Summary data={data} />

          <section className="detail-section">
            <div className="inference-body">
              <div className="inference-col">
                <span className="chart-title">Delivery cost · trend</span>
                <DeliveryCostTrend
                  ai={data.trend}
                  human={data.human_effort_trend}
                  effortPresent={data.human_effort_present}
                />
              </div>
              <div className="inference-col">
                <div className="chart-title top-customers-head">
                  <span>Top customers</span>
                  <label className="metric-pick">
                    <span className="sr-only">Rank customers by</span>
                    <select
                      aria-label="Rank customers by"
                      value={effective}
                      onChange={(e) => setMetric(e.target.value as Metric)}
                    >
                      {METRICS.map((m) => (
                        <option key={m.key} value={m.key}>
                          {m.label}
                        </option>
                      ))}
                    </select>
                  </label>
                </div>
                {ranked.length === 0 ? (
                  <p className="muted">No {chosen.label.toLowerCase()} recorded this period.</p>
                ) : (
                  /* Same reason as the table below: these bar labels are the
                     customers' own identifiers. Masked here rather than inside
                     SpendBars, which also draws providers and tools — those are
                     vendor names and carry nothing private. */
                  <div data-dd-privacy="mask">
                    <SpendBars
                      verbatim
                      rows={ranked.slice(0, TOP_N).map((c) => {
                        const value = chosen.of(c) ?? 0;
                        return {
                          label: c.customer_id,
                          amount: value,
                          pct: rankedTotal ? (value / rankedTotal) * 100 : 0,
                          meta: c.requests ? `${compact(c.requests)} calls` : undefined,
                        };
                      })}
                      // Hours are not dollars; say so rather than drawing "$40".
                      format={
                        effective === "human_hours" ? (v) => `${num(Math.round(v))} hrs` : undefined
                      }
                    />
                  </div>
                )}
              </div>
            </div>
          </section>

          <section className="detail-section">
            <h3 className="breakdown-subhead">All customers</h3>
            <p className="section-sub muted">
              <Coverage data={data} />
            </p>
            <table className="features-table customer-table">
              <thead>
                <tr>
                  <th>Customer</th>
                  <th className="num">Metered AI cost</th>
                  <th className="num">Requests</th>
                  <th className="num">Cost / request</th>
                  <th className="num">Human hours</th>
                  <th className="num">Human cost</th>
                  <th className="num">Delivery cost</th>
                  <th className="num">vs prior period</th>
                </tr>
              </thead>
              <tbody>
                {data.customers.map((c) => (
                  <tr key={c.customer_id}>
                    {/* The identifier is whatever the customer's own privacy
                        setting stores — possibly a real name. Session replay
                        records the DOM, so it is masked at the cell rather than
                        left to the "mask user input" default, which only covers
                        form fields. */}
                    <td data-dd-privacy="mask">{c.customer_id}</td>
                    <td className="num">
                      {c.amount === null ? (
                        <NotProvided title="No metered calls carry this customer's id" />
                      ) : (
                        money(c.amount)
                      )}
                    </td>
                    <td className="num">{c.requests === null ? "—" : num(c.requests)}</td>
                    <td className="num" title="Metered spend divided by metered calls">
                      {unitMoney(c.cost_per_request)}
                    </td>
                    <td className="num">
                      {c.human_hours === null ? <NotProvided /> : num(c.human_hours)}
                    </td>
                    <td className="num">
                      {c.human_cost === null ? <NotProvided /> : money(c.human_cost)}
                    </td>
                    <td className="num">
                      {c.total_delivery_cost === null ? (
                        <NotProvided />
                      ) : (
                        money(c.total_delivery_cost)
                      )}
                    </td>
                    <td className="num">
                      <CustomerDelta customer={c} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      )}
    </>
  );
}

/** The four figures the screen is for, before any chart. */
function Summary({ data }: { data: CustomerSpend }) {
  return (
    <section className="kpi-row customer-summary" aria-label="Customer economics summary">
      <article className="kpi-card">
        <h3 className="kpi-label">Metered AI cost</h3>
        <div className="kpi-main">
          <span className="kpi-value">{money(data.total)}</span>
        </div>
        <span className="muted kpi-note">from calls tagged with a customer</span>
      </article>
      <article className="kpi-card">
        <h3 className="kpi-label">Human effort</h3>
        <div className="kpi-main">
          <span className="kpi-value">
            {data.human_hours === null ? <NotProvided /> : `${num(Math.round(data.human_hours))}`}
            {data.human_hours !== null && <span className="kpi-unit"> hrs</span>}
          </span>
        </div>
        <span className="muted kpi-note">
          {data.human_hours === null ? "no timesheet data for this period" : "hours uploaded"}
        </span>
      </article>
      <article className="kpi-card">
        <h3 className="kpi-label">Human cost</h3>
        <div className="kpi-main">
          <span className="kpi-value">
            {data.human_cost === null ? <NotProvided /> : money(data.human_cost)}
          </span>
        </div>
        <span className="muted kpi-note">at the loaded rates you supplied</span>
      </article>
      <article className="kpi-card">
        <h3 className="kpi-label">Customer-attributed delivery cost</h3>
        <div className="kpi-main">
          <span className="kpi-value">{money(data.total_delivery_cost)}</span>
        </div>
        <span
          className="muted kpi-note"
          title="Metered AI plus human effort for these customers. Not your total AI bill."
        >
          metered AI + people · not the full bill
        </span>
      </article>
    </section>
  );
}

/** Metered spend is a subset of the bill — say by how much, every time. */
function Coverage({ data }: { data: CustomerSpend }) {
  const withEffort = data.human_effort_customer_count;
  const listed = data.customers.length;
  return (
    <>
      {data.inference_total <= 0 ? (
        <>Every metered call in this window carries a customer. </>
      ) : (
        <>
          Tagged calls account for {data.coverage_pct.toFixed(data.coverage_pct >= 10 ? 0 : 1)}% of
          the {money(data.inference_total)} inference bill this period ({money(data.total)}). The
          rest ran without a <code>customer_id</code>, so it isn't attributed to anyone here.{" "}
        </>
      )}
      {/* The denominator is the customers LISTED here — the union of metered and
          effort-logged — not every customer the company has, which Meter has no
          way to know. */}
      {withEffort > 0 ? (
        <>
          Human-effort data was provided for {withEffort} of {listed} customer
          {listed === 1 ? "" : "s"} shown in this period.
        </>
      ) : data.human_effort_ever ? (
        <>No human effort was recorded in this period, though earlier periods have it.</>
      ) : (
        <>No human effort has been uploaded yet, so delivery cost is metered AI only.</>
      )}
    </>
  );
}

/** Change vs the equal-length window before. New customers aren't a 0% change. */
function CustomerDelta({ customer }: { customer: Customer }) {
  if (customer.delta_pct === null || customer.prev_amount === null) {
    return <span className="muted">new</span>;
  }
  const up = customer.delta_pct >= 0;
  return (
    <span
      className={`delta ${up ? "delta-up" : "delta-down"}`}
      title={`${money(customer.prev_amount)} in the prior period`}
    >
      {up ? "▲" : "▼"} {Math.abs(customer.delta_pct).toFixed(0)}%
    </span>
  );
}

/** No metered customer data: explain exactly what produces it. */
function NoCustomerData() {
  return (
    <div className="empty-state">
      <p className="empty-title">No customer-attributed spend yet</p>
      <p className="muted">
        Provider bills record what was spent, never who it was spent on — so this view can't be
        filled in from a cost connector. It needs the Meter metering SDK in your application,
        passing <code>metadata.customer_id</code> on each model call. Once calls arrive tagged, cost
        per customer, cost per request, and period-over-period change appear here.
      </p>
      <p className="muted">
        <Link to="/install-sdk" className="link">
          Install the SDK
        </Link>{" "}
        — it's a few lines, and every other view keeps working without it.
      </p>
    </div>
  );
}
