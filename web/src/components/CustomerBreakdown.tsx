/**
 * Overview "By Customer" tab — which of the tenant's own customers consumed the
 * inference spend, over the Overview's selected review period.
 *
 * This is the one breakdown provider bills cannot produce: a bill says what was
 * spent, never on whose behalf. It is populated only from SDK-metered calls that
 * carry `metadata.customer_id`, so it is a SUBSET of the authoritative inference
 * bill — the coverage line says how big a subset, and the empty state explains
 * what to install when there is none. Build cost has no customer, so it never
 * appears here (invariant 2).
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type CustomerSpend, type ReviewRange } from "../api";
import { compact, money, num, unitMoney } from "../format";
import { SpendBars } from "./SpendBars";
import { TrendChart } from "./TrendChart";

/** Top slice shown as bars; the rest stay in the table below. */
const TOP_N = 8;

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
  }, [range, refreshKey]);

  return (
    <>
      <div className="section-head breakdown-head">
        <div>
          <h2>Inference cost by customer</h2>
          <span className="section-sub muted">
            Who consumed the spend — run cost only, from metered calls tagged with a customer.
          </span>
        </div>
      </div>

      {failed ? (
        <p className="muted">Couldn't load customer spend.</p>
      ) : data === null ? (
        <p className="muted">Loading…</p>
      ) : data.customers.length === 0 ? (
        <NoCustomerData />
      ) : (
        <>
          <section className="detail-section">
            <div className="inference-body">
              <div className="inference-col">
                <span className="chart-title">Metered spend · trend</span>
                <TrendChart trend={data.trend} />
              </div>
              <div className="inference-col">
                <span className="chart-title">
                  Top customers · {money(data.total)} metered
                  {data.customers.length > TOP_N && ` of ${data.customers.length} customers`}
                </span>
                <SpendBars
                  verbatim
                  rows={data.customers.slice(0, TOP_N).map((c) => ({
                    label: c.customer_id,
                    amount: c.amount,
                    pct: c.pct,
                    meta: c.requests ? `${compact(c.requests)} calls` : undefined,
                  }))}
                />
              </div>
            </div>
          </section>

          <section className="detail-section">
            <h3 className="breakdown-subhead">All customers</h3>
            <p className="section-sub muted">
              <Coverage data={data} />
            </p>
            <table className="features-table">
              <thead>
                <tr>
                  <th>Customer</th>
                  <th className="num">Inference cost</th>
                  <th className="num">Share</th>
                  <th className="num">Requests</th>
                  <th className="num">Cost / request</th>
                  <th className="num">vs prior period</th>
                </tr>
              </thead>
              <tbody>
                {data.customers.map((c) => (
                  <tr key={c.customer_id}>
                    <td>{c.customer_id}</td>
                    <td className="num">{money(c.amount)}</td>
                    <td className="num">{c.pct.toFixed(c.pct >= 10 ? 0 : 1)}%</td>
                    <td className="num">{num(c.requests)}</td>
                    <td className="num" title="Metered spend divided by metered calls">
                      {unitMoney(c.cost_per_request)}
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

/** Metered spend is a subset of the bill — say by how much, every time. */
function Coverage({ data }: { data: CustomerSpend }) {
  if (data.inference_total <= 0) {
    return <>Every metered call in this window carries a customer.</>;
  }
  return (
    <>
      Tagged calls account for {data.coverage_pct.toFixed(data.coverage_pct >= 10 ? 0 : 1)}% of the{" "}
      {money(data.inference_total)} inference bill this period ({money(data.total)}). The rest ran
      without a <code>customer_id</code>, so it isn't attributed to anyone here.
    </>
  );
}

/** Change vs the equal-length window before. New customers aren't a 0% change. */
function CustomerDelta({ customer }: { customer: CustomerSpend["customers"][number] }) {
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
