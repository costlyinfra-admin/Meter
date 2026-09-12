"""Read-side aggregation for the three screens (M6).

Combines the M4 (inference) and M5 (build) data per feature for the dashboard,
and assembles a per-feature drill-down with the evidence trail.

INVARIANT: build cost and inference cost are returned as separate fields and are
never summed into one number here. "cost per user" uses the recurring *inference*
cost only (build is one-time-ish); it is labelled directional, not ROI.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import defaultdict
from decimal import Decimal
from typing import Optional

from . import optimize, pricing
from .build import developer_label
from .db import app_dsn, connect, tenant_tx
from .providers import month_start, next_month

#: Token types spend is split across, in display order.
_TOKEN_TYPE_LABELS = {
    "input": "Input",
    "cache_write_5m": "Cache write (5m)",
    "cache_write_1h": "Cache write (1h)",
    "cache_write": "Cache write",
    "cache_read": "Cache read",
    "output": "Output",
    "unknown": "Unknown",
}


def token_buckets_add(
    dollars: dict,
    tokens: dict,
    *,
    provider: str,
    model: Optional[str],
    amount: float,
    tokens_in: int,
    tokens_out: int,
    cached: int,
    cache_write: int,
    write_5m: int = 0,
    write_1h: int = 0,
) -> None:
    """Split one (provider, model) group's billed dollars across its token types.

    IMPORTANT — this split is DERIVED, not reported: providers bill per line item,
    not per token type. We weight each type by what it should cost (the model's
    list rate x the type's multiplier — cache writes at a premium that varies by
    TTL, cache reads at a discount) and allocate the REAL billed dollars in those
    proportions, so the parts always sum back to the bill. Token COUNTS, by
    contrast, are reported by the provider and are exact.

    Unpriced models fall back to token counts; no token detail -> "unknown".
    """
    if amount <= 0:
        return
    # Split the cache-write total into its TTL buckets when the provider reports
    # them (they price differently); otherwise keep one undifferentiated bucket.
    if write_5m or write_1h:
        write_parts = {"cache_write_5m": write_5m, "cache_write_1h": write_1h}
    else:
        write_parts = {"cache_write": cache_write}
    # tokens_in is the TOTAL input; uncached is what's left after cache traffic.
    uncached = max(tokens_in - cached - cache_write, 0)
    counts = {"input": uncached, "cache_read": cached, "output": tokens_out, **write_parts}

    if sum(counts.values()) <= 0:
        dollars["unknown"] += amount
        tokens["unknown"] = tokens.get("unknown", 0)
        return

    r_in = pricing.rate_in(model or "", provider)
    r_out = pricing.rate_out(model or "", provider)
    if r_in > 0 or r_out > 0:
        read_mult = pricing.cache_read_mult(provider) or Decimal("1")
        rates = {
            "input": r_in,
            "cache_read": r_in * read_mult,
            "output": r_out,
            "cache_write": r_in * pricing.cache_write_mult(provider),
            "cache_write_5m": r_in * pricing.cache_write_mult(provider, "5m"),
            "cache_write_1h": r_in * pricing.cache_write_mult(provider, "1h"),
        }
        weights = {k: Decimal(v) * rates[k] for k, v in counts.items()}
    else:  # unpriced model -> weight by raw token counts (documented fallback)
        weights = {k: Decimal(v) for k, v in counts.items()}

    total_w = sum(weights.values())
    if total_w <= 0:
        dollars["unknown"] += amount
        return
    for key, w in weights.items():
        if counts[key] > 0:
            tokens[key] = tokens.get(key, 0) + counts[key]
        if w > 0:
            dollars[key] += amount * float(w / total_w)


_CONFIDENCE_RANK = {"high": 3, "med": 2, "low": 1}

# Spend the user marked "ignore" is excluded from normal reporting/optimization
# totals. NULL (legacy / unclassified snapshot) stays included.
_ACTIVE_ENV = "(environment IS NULL OR environment <> 'ignore')"

# The classification buckets shown in the inference trend (Ignore is excluded, and
# any unexpected value folds into unclassified).
_CLASSIFICATION_BUCKETS = ("production", "development", "internal", "unclassified")


def resolve_category(stored: Optional[str], source: Optional[str]) -> tuple:
    """What kind of feature is this? Returns (category, source); both may be None.

    A person's tag outranks the discovery guess, and nothing outranks a person.
    Unlike cost, there is no billing signal for "this is a UI feature" — so an
    untagged feature stays untagged rather than being assigned a default, and
    the UI asks for a tag instead of asserting one.
    """
    if source == "user" and stored:
        return stored, "user"
    if stored:
        return stored, "discovery"
    return None, None


def _min_confidence(current: Optional[str], candidate: Optional[str]) -> Optional[str]:
    """Most conservative (lowest) of two confidences."""
    if candidate is None:
        return current
    if current is None:
        return candidate
    return candidate if _CONFIDENCE_RANK[candidate] < _CONFIDENCE_RANK[current] else current


def _resolve_period(conn, period: Optional[dt.date]) -> dt.date:
    """Use the given month, or the latest month that has any cost/usage data.

    "This month" (the default) is the actual current calendar month once a working
    sync has ingested it — the latest-with-data fallback only matters before any
    data lands. We deliberately do NOT special-case one cost table over another: if
    the current month reads as empty, that is an honest signal to sync, surfaced by
    the per-month backfill errors, not something to paper over here.
    """
    if period is not None:
        return month_start(period)
    row = conn.execute(
        """
        SELECT max(p) FROM (
            SELECT max(period) p FROM build_cost
            UNION ALL SELECT max(period) FROM inference_cost
            UNION ALL SELECT max(period) FROM feature_usage
        ) periods
        """
    ).fetchone()
    return row[0] if row and row[0] else month_start(dt.date.today())


#: Named review periods, as (months_back_for_start, months_back_for_end) from the
#: latest month with data. Data is bucketed by month, so day-windows aren't exact.
_RANGE_SPECS = {
    "this_month": (0, 0),
    "last_month": (1, 1),
    "last_3_months": (2, 0),
    "last_6_months": (5, 0),
    "last_12_months": (11, 0),
}


def _resolve_range(
    conn,
    range_token: Optional[str],
    start: Optional[dt.date],
    end: Optional[dt.date],
) -> tuple[dt.date, dt.date]:
    """Resolve a review period to an inclusive (start_month, end_month).

    Explicit start/end (a custom range) win; otherwise a named range token is
    resolved relative to the latest month that has data. Both are first-of-month.
    """
    if start is not None:
        s = month_start(start)
        e = month_start(end) if end is not None else s
        return (s, e) if s <= e else (e, s)
    latest = _resolve_period(conn, None)
    back_start, back_end = _RANGE_SPECS.get(range_token or "this_month", (0, 0))
    return _months_back(latest, back_start), _months_back(latest, back_end)


def resolve_window(
    tenant_id: str,
    start: Optional[dt.date] = None,
    end: Optional[dt.date] = None,
    range_token: Optional[str] = None,
) -> tuple[dt.date, dt.date]:
    """The (start_month, end_month) a request's period arguments resolve to.

    Public so other read-side features — the budget forecast — land on exactly the
    window the Overview is showing, rather than resolving "last 3 months" their
    own way and disagreeing with the chart beside them.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return _resolve_range(conn, range_token, start, end)


def _month_count(start: dt.date, end: dt.date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def _worth_indicator(inference: float, users: Optional[int]) -> str:
    """Directional only (not ROI). healthy / watch / unknown."""
    if not users:
        return "unknown"
    cost_per_user = inference / users
    return "healthy" if cost_per_user <= 10.0 else "watch"


def dashboard(
    tenant_id: str,
    start: Optional[dt.date] = None,
    end: Optional[dt.date] = None,
    range_token: Optional[str] = None,
) -> dict:
    """Money screen over an inclusive month range (default: the latest month).

    Build and inference cost are summed over the range; active users are the
    range's latest month (a point-in-time count), and the deltas compare to the
    equal-length window immediately before.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        start, end = _resolve_range(conn, range_token, start, end)

        features = conn.execute(
            """
            SELECT f.id, f.name, f.status, f.discovery_confidence,
                   f.category, f.category_source, f.product_id, p.name, f.product_source
            FROM feature f LEFT JOIN product p ON p.id = f.product_id
            WHERE f.status IN ('proposed', 'confirmed')
            ORDER BY f.created_at
            """
        ).fetchall()

        build = _rollup(conn, "build_cost", start, end)
        inference, inference_unattributed = _inference_rollup(conn, start, end)
        # Active users is a point-in-time count, not additive across months — use
        # the range's latest month.
        usage = {
            str(fid): users
            for fid, users in conn.execute(
                "SELECT feature_id, active_users FROM feature_usage WHERE period = %s", (end,)
            ).fetchall()
        }

        # Prior equal-length window (for the deltas) + the range's token split.
        n = _month_count(start, end)
        prev_end = _months_back(start, 1)
        prev_start = _months_back(prev_end, n - 1)
        prev_build = float(
            conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM build_cost WHERE period BETWEEN %s AND %s",
                (prev_start, prev_end),
            ).fetchone()[0]
        )
        prev_inference = float(
            conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM inference_cost "
                f"WHERE period BETWEEN %s AND %s AND {_ACTIVE_ENV}",  # noqa: S608
                (prev_start, prev_end),
            ).fetchone()[0]
        )
        # The estimated (not-yet-billed) portion already included in inference_cost
        # above — surfaced separately so the UI can label "incl ~$X estimated".
        estimated_inference = float(
            conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM inference_cost "
                f"WHERE period BETWEEN %s AND %s AND source = 'cost_api_est' AND {_ACTIVE_ENV}",  # noqa: S608
                (start, end),
            ).fetchone()[0]
        )
        # When cost data was actually last written (an ingest replaces a period's
        # rows, so created_at tracks the last successful sync/import). This is what
        # the UI's "Updated ..." stamp reports — NOT when the page happened to load.
        freshness = conn.execute(
            """
            SELECT (SELECT max(created_at) FROM inference_cost),
                   (SELECT max(created_at) FROM build_cost)
            """
        ).fetchone()
        inference_updated_at, build_updated_at = freshness if freshness else (None, None)

        tok = conn.execute(
            "SELECT COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0) "
            f"FROM inference_cost WHERE period BETWEEN %s AND %s AND {_ACTIVE_ENV}",  # noqa: S608
            (start, end),
        ).fetchone()
        tokens_in, tokens_out = int(tok[0]), int(tok[1])

        # Extra aggregates the Key insights narrative reads (anomaly, pace,
        # environment split, key concentration, cache coverage).
        facts = _insight_facts(conn, start, end)
        # The Overview's trend chart and provider list, read in the same
        # transaction so they describe the same instant as everything else.
        trend = _monthly_trend(conn, start, end)
        providers = _provider_spend(conn, start, end)

    rows = []
    for (
        fid,
        name,
        _status,
        _disc,
        category,
        category_source,
        product_id,
        product_name,
        product_source,
    ) in features:
        fid = str(fid)
        kind, kind_source = resolve_category(category, category_source)
        b = build.get(fid, {"amount": 0.0, "confidence": None})
        i = inference.get(fid, {"amount": 0.0, "confidence": None, "requests": None})
        users = usage.get(fid)
        cost_per_user = (i["amount"] / users) if users else None
        rows.append(
            {
                "feature_id": fid,
                "name": name,
                "build_cost": b["amount"],
                "inference_cost": i["amount"],  # kept separate from build
                "active_users": users,
                "cost_per_user": cost_per_user,
                # Number of AI model calls this feature made (None when unknown —
                # e.g. connector-only, no hook, since cost APIs don't report counts).
                "requests": i.get("requests"),
                # Feature type (chat/api/ui/...), or None when untagged;
                # category_source says whether a person or discovery set it.
                "category": kind,
                "category_source": kind_source,
                # The product this feature rolls up into, or None (Unassigned).
                "product_id": str(product_id) if product_id else None,
                "product_name": product_name,
                "product_source": product_source,
                "worth_it": _worth_indicator(i["amount"], users),
                "confidence": _min_confidence(b["confidence"], i["confidence"]),
            }
        )

    unattributed = {
        "build_cost": build.get(None, {"amount": 0.0})["amount"],
        "inference_cost": inference_unattributed,
    }
    totals = {
        "build_cost": sum(r["build_cost"] for r in rows) + unattributed["build_cost"],
        "inference_cost": sum(r["inference_cost"] for r in rows) + unattributed["inference_cost"],
        # Portion of inference_cost that is estimated (not yet billed), for labelling.
        "estimated_inference": estimated_inference,
        "prev_build_cost": prev_build,
        "prev_inference_cost": prev_inference,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }
    return {
        "period": end.isoformat(),  # the range's latest month (back-compat)
        "start": start.isoformat(),
        "end": end.isoformat(),
        "months": n,
        "features": rows,
        "unattributed": unattributed,
        "highlights": _highlights(rows),
        "insights": _insights(rows, unattributed, totals, facts, end),
        "actions": _open_actions(unattributed, totals, facts),
        "trend": trend,
        "providers": providers,
        "totals": totals,
        # Data freshness: when inference / build cost were last ingested. The max
        # of the two is what the Overview shows; both are exposed for detail.
        "inference_updated_at": inference_updated_at.isoformat() if inference_updated_at else None,
        "build_updated_at": build_updated_at.isoformat() if build_updated_at else None,
        "data_updated_at": max(
            [t for t in (inference_updated_at, build_updated_at) if t], default=None
        ).isoformat()
        if (inference_updated_at or build_updated_at)
        else None,
    }


def _fmt_pct(p: float) -> str:
    """Whole percent when >= 10 (54%), one decimal when smaller (9.7%)."""
    return f"{round(p)}%" if p >= 10 else f"{p:.1f}%"


def _fmt_ratio(r: float) -> str:
    """1.98 -> '2x', 2.34 -> '2.3x'."""
    return f"{r:.1f}".rstrip("0").rstrip(".") + "x"


def _insight_facts(conn, start: dt.date, end: dt.date) -> dict:
    """Extra aggregates the Key insights narrative needs (same tx, one pass each).

    Everything here is a plain SUM over stored rows — the insights layer does no
    modelling of its own, so every sentence stays traceable to the numbers.
    """
    # Where a provider has BOTH connector and hook rows they describe the same
    # spend, and _inference_rollup reconciles them so the bill is never counted
    # twice. These breakdowns must use the same basis or a share could exceed
    # 100%: keep the connector rows (they carry the workspace / API-key / env
    # identity) and drop that provider's hook rows. A hook-only provider — self
    # hosted, or metered before its connector was added — keeps its rows.
    connector_providers = [
        p
        for p, has_connector in conn.execute(
            "SELECT provider, bool_or(source <> 'hook') FROM inference_cost "
            f"WHERE period BETWEEN %s AND %s AND {_ACTIVE_ENV} GROUP BY provider",  # noqa: S608
            (start, end),
        ).fetchall()
        if has_connector
    ]
    reconciled = "AND NOT (source = 'hook' AND provider = ANY(%s))"
    args = (start, end, connector_providers)

    env = {
        (e or "unclassified"): float(a)
        for e, a in conn.execute(
            "SELECT COALESCE(environment, 'unclassified'), SUM(amount) FROM inference_cost "
            f"WHERE period BETWEEN %s AND %s AND {_ACTIVE_ENV} {reconciled} GROUP BY 1",  # noqa: S608, E501
            args,
        ).fetchall()
    }
    # The denominator for every share below: the same rows the buckets came from,
    # so the parts can never add up to more than the whole.
    resource_basis = sum(env.values())
    # Day-resolution connector spend (hook/self-host stay monthly), used for the
    # anomaly and pace rules. `end` is a first-of-month, so scan to the month after.
    daily = [
        (d, float(a))
        for d, a in conn.execute(
            "SELECT day, SUM(amount) FROM inference_cost_daily "
            f"WHERE day >= %s AND day < %s AND {_ACTIVE_ENV} "  # noqa: S608
            "GROUP BY day ORDER BY day",
            (start, next_month(end)),
        ).fetchall()
    ]
    # The calendar month before the range's last one, from the SAME table so the
    # pace comparison is like-for-like (and independent of how long the range is).
    prev_month_daily = float(
        conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM inference_cost_daily "
            f"WHERE day >= %s AND day < %s AND {_ACTIVE_ENV}",  # noqa: S608
            (_months_back(end, 1), end),
        ).fetchone()[0]
    )
    top_key = conn.execute(
        "SELECT COALESCE(api_key_name, api_key_id), SUM(amount) FROM inference_cost "
        f"WHERE period BETWEEN %s AND %s AND {_ACTIVE_ENV} {reconciled} "  # noqa: S608
        "AND (api_key_name IS NOT NULL OR api_key_id IS NOT NULL) "
        "GROUP BY 1 ORDER BY 2 DESC LIMIT 1",
        args,
    ).fetchone()
    # Input-token cache coverage, plus the provider that spent the most (its
    # published cache-read discount is the one worth quoting).
    cache_rows = conn.execute(
        "SELECT provider, COALESCE(SUM(tokens_in), 0), COALESCE(SUM(cached_tokens_in), 0), "
        f"SUM(amount) FROM inference_cost WHERE period BETWEEN %s AND %s AND {_ACTIVE_ENV} "  # noqa: S608, E501
        "GROUP BY provider",
        (start, end),
    ).fetchall()
    lead = max(cache_rows, key=lambda r: float(r[3] or 0), default=None)
    return {
        "months": _month_count(start, end),
        "env": env,
        "resource_basis": resource_basis,
        "daily": daily,
        "prev_month_daily": prev_month_daily,
        "top_key": (top_key[0], float(top_key[1])) if top_key and top_key[1] else None,
        "tokens_in": sum(int(r[1]) for r in cache_rows),
        "cached_tokens_in": sum(int(r[2]) for r in cache_rows),
        "lead_provider": lead[0] if lead else None,
    }


#: Materiality gates for the Key insights narrative. An insight has to clear BOTH
#: a relative and an absolute bar — a 90% swing on $4 is noise, not news.
_SPIKE_RATIO = 2.0  # costliest day vs the median day
_SPIKE_MIN_ABS = 25.0
_TREND_MIN_PCT = 15.0
_TREND_MIN_ABS = 50.0
_PACE_MIN_PCT = 10.0
_PACE_MIN_ABS = 50.0
_PACE_MIN_DAYS = 5  # too few days in and the projection is guesswork
_NONPROD_MIN_PCT = 15.0
_NONPROD_MIN_ABS = 25.0
_UNCLASSIFIED_MIN_PCT = 20.0
_KEY_CONCENTRATION_PCT = 40.0
_FEATURE_CONCENTRATION_PCT = 30.0
_UNATTRIBUTED_MIN_PCT = 5.0
_EFFICIENCY_MIN_RATIO = 2.0
_CACHE_LOW_PCT = 15.0
_CACHE_MIN_TOKENS = 5_000_000
#: The card stays readable at about five lines; candidates are ranked, not dropped
#: at random, so the most urgent ones survive the cut.
_MAX_INSIGHTS = 5

_ENV_LABELS = {"development": "Development", "internal": "Internal"}


def _fmt_money(amount: float) -> str:
    """$4,381 above $100 (cents are noise there), $27.40 below it."""
    return f"${amount:,.0f}" if abs(amount) >= 100 else f"${amount:,.2f}"


def _fmt_day(day: dt.date) -> str:
    return day.strftime("%b %-d")


def _insights(rows: list, unattributed: dict, totals: dict, facts: dict, end: dt.date) -> list:
    """Auto-generated, plain-language executive insights (deterministic).

    Every sentence is derived directly from the dashboard numbers — no model,
    no black box. Each insight names the feature(s) and basis behind it.

    Candidates are collected with a rank and the top few are returned, so the
    card leads with anomalies and cost-cutting angles rather than with whatever
    rule happened to be written first. Observed spend is never called savings.
    """
    total_ai = totals["build_cost"] + totals["inference_cost"]
    if total_ai <= 0:
        return []
    candidates: list[tuple[int, dict]] = []

    def add(rank: int, kind: str, text: str, detail: str = "") -> None:
        """One insight. `text` is the finding; `detail` is the qualifier that
        would otherwise trail it in the same sentence, split out so the card can
        lead with the finding and let the reader stop there."""
        candidates.append((rank, {"kind": kind, "text": text, "detail": detail}))

    # 1. Anomaly — one day far above the period's typical day.
    daily = [(d, a) for d, a in facts["daily"] if a > 0]
    if len(daily) >= 7:
        peak_day, peak = max(daily, key=lambda p: p[1])
        mid = statistics.median(a for _, a in daily)
        if mid > 0 and peak / mid >= _SPIKE_RATIO and peak - mid >= _SPIKE_MIN_ABS:
            add(
                1,
                "spike",
                f"{_fmt_day(peak_day)} was the costliest day at {_fmt_money(peak)} — "
                f"{_fmt_ratio(peak / mid)} the {_fmt_money(mid)} median day this period.",
            )

    # 2a. Pace — the current calendar month is still running, so a period-over-period
    #     comparison would pit a partial month against a full one. Project instead,
    #     from the same daily table on both sides, and label it a projection.
    today = dt.date.today()
    current_month = end == month_start(today)
    month_daily = [(d, a) for d, a in daily if d >= end]
    if current_month and month_daily:
        through, _ = month_daily[-1]
        covered = through.day
        mtd = sum(a for _, a in month_daily)
        days_in_month = (next_month(end) - end).days
        if covered >= _PACE_MIN_DAYS and mtd > 0:
            projected = mtd / covered * days_in_month
            prev = facts["prev_month_daily"]
            text = (
                f"{end.strftime('%B')} is at {_fmt_money(mtd)} through {_fmt_day(through)} — "
                f"on pace for about {_fmt_money(projected)} by month end"
            )
            delta = projected - prev
            if (
                prev > 0
                and abs(delta) >= _PACE_MIN_ABS
                and abs(delta) / prev * 100 >= _PACE_MIN_PCT
            ):
                direction = "above" if delta > 0 else "below"
                prev_label = _months_back(end, 1).strftime("%B")
                add(
                    2,
                    "pace" if delta > 0 else "pace-down",
                    f"{text}, {_fmt_pct(abs(delta) / prev * 100)} {direction} "
                    f"{prev_label}'s {_fmt_money(prev)}.",
                )
            else:
                add(2, "pace", f"{text}.")

    # 2b. Trend — for a closed window, compare like with like against the one before.
    prev_ai = totals["prev_build_cost"] + totals["prev_inference_cost"]
    if not current_month and prev_ai > 0:
        delta = total_ai - prev_ai
        pct = abs(delta) / prev_ai * 100
        if abs(delta) >= _TREND_MIN_ABS and pct >= _TREND_MIN_PCT:
            months = facts["months"]
            window = "month" if months == 1 else f"{months} months"
            d_run = totals["inference_cost"] - totals["prev_inference_cost"]
            d_build = totals["build_cost"] - totals["prev_build_cost"]
            driver = ""
            if abs(d_run) >= abs(delta) * 0.6:
                driver = "Mostly inference (run) cost."
            elif abs(d_build) >= abs(delta) * 0.6:
                driver = "Mostly build cost."
            add(
                2,
                "trend" if delta > 0 else "trend-down",
                f"AI spend is {'up' if delta > 0 else 'down'} {_fmt_pct(pct)} "
                f"({_fmt_money(abs(delta))}) vs the previous {window}.",
                driver,
            )

    # 3. Non-production spend — the clearest cost-cutting angle billing data supports.
    #    Named as spend under review, never as savings: only the customer knows
    #    whether a development key is still needed.
    # Shares below use the reconciled basis these buckets were summed from, not
    # the headline inference total — the two differ when a provider is metered by
    # both hook and connector, and a share over 100% would be nonsense.
    basis = facts["resource_basis"]
    nonprod = {k: v for k, v in facts["env"].items() if k in _ENV_LABELS and v > 0}
    nonprod_total = sum(nonprod.values())
    if basis > 0 and nonprod_total >= _NONPROD_MIN_ABS:
        share = nonprod_total / basis * 100
        if share >= _NONPROD_MIN_PCT:
            if len(nonprod) == 1:
                label = _ENV_LABELS[next(iter(nonprod))]
                add(
                    3,
                    "waste",
                    f"{label} keys are {_fmt_pct(share)} of inference spend.",
                    f"{_fmt_money(nonprod_total)} this period.",
                )
            else:
                add(
                    3,
                    "waste",
                    f"Non-production keys are {_fmt_pct(share)} of inference spend — "
                    f"{_fmt_money(nonprod_total)} on development and internal work "
                    f"this period.",
                )

    # 4. Blast radius — a single key carrying most of the bill.
    if facts["top_key"] and basis > 0:
        key, amount = facts["top_key"]
        share = amount / basis * 100
        if share >= _KEY_CONCENTRATION_PCT:
            add(
                4,
                "resource",
                f"One API key ({key}) drives {_fmt_pct(share)} of inference spend "
                f"({_fmt_money(amount)}).",
            )

    # 5. Concentration — the single largest slice of all AI spend, when it dominates.
    combos = [(r, r["build_cost"] + r["inference_cost"]) for r in rows]
    top = max(combos, key=lambda c: c[1], default=None)
    if top and top[1] > 0 and top[1] / total_ai * 100 >= _FEATURE_CONCENTRATION_PCT:
        add(
            5,
            "concentration",
            f"{top[0]['name']} represents {_fmt_pct(top[1] / total_ai * 100)} "
            f"of all AI spend ({_fmt_money(top[1])}).",
        )

    # 6. Governance — how much spend isn't mapped to a feature yet (invariant 4).
    unatt = unattributed["build_cost"] + unattributed["inference_cost"]
    if unatt > 0 and unatt / total_ai * 100 >= _UNATTRIBUTED_MIN_PCT:
        add(
            6,
            "governance",
            f"Unattributed spend represents {_fmt_pct(unatt / total_ai * 100)} "
            f"of total AI costs ({_fmt_money(unatt)}).",
        )

    # 7. Coverage — unclassified keys make every environment split above partial.
    unclassified = facts["env"].get("unclassified", 0.0)
    if basis > 0 and unclassified > 0:
        share = unclassified / basis * 100
        if share >= _UNCLASSIFIED_MIN_PCT:
            add(
                7,
                "coverage",
                f"{_fmt_pct(share)} of inference spend ({_fmt_money(unclassified)}) is on "
                f"keys with no environment set, so the production split is incomplete.",
            )

    # 8. Cache coverage — an observation about what was billed, not advice: whether
    #    this workload CAN cache more needs request-level evidence we don't have.
    tokens_in = facts["tokens_in"]
    if tokens_in >= _CACHE_MIN_TOKENS:
        share = facts["cached_tokens_in"] / tokens_in * 100
        if share < _CACHE_LOW_PCT:
            mult = pricing.cache_read_mult(facts["lead_provider"])
            rate = (
                f"; cached input bills at {_fmt_pct(float(mult) * 100)} of the standard rate"
                if mult
                else ""
            )
            seen = (
                "No input tokens were served from cache this period"
                if facts["cached_tokens_in"] == 0
                else f"Cache reads were {_fmt_pct(share)} of input tokens this period"
            )
            add(8, "cache", f"{seen}{rate}.")

    # 9. Efficiency — widest gap in cost per active user between two features.
    cpu = [r for r in rows if r["cost_per_user"]]
    if len(cpu) >= 2:
        high = max(cpu, key=lambda r: r["cost_per_user"])
        low = min(cpu, key=lambda r: r["cost_per_user"])
        ratio = high["cost_per_user"] / low["cost_per_user"]
        if ratio >= _EFFICIENCY_MIN_RATIO:
            add(
                9,
                "efficiency",
                f"{high['name']} costs {_fmt_ratio(ratio)} more per user than "
                f"{low['name']} ({_fmt_money(high['cost_per_user'])} vs "
                f"{_fmt_money(low['cost_per_user'])} per active user).",
            )

    # 10. Build-vs-run split (kept separate per invariant 2, shown as shares).
    if totals["build_cost"] > 0 and totals["inference_cost"] > 0:
        run = totals["inference_cost"] / total_ai * 100
        built = totals["build_cost"] / total_ai * 100
        add(
            10,
            "split",
            f"Running these features is {_fmt_pct(run)} of AI spend; "
            f"building them is {_fmt_pct(built)}.",
        )

    candidates.sort(key=lambda c: c[0])
    return [ins for _, ins in candidates[:_MAX_INSIGHTS]]


def _monthly_trend(conn, start: dt.date, end: dt.date) -> list[dict]:
    """Cost and tokens per month across the range, each kind kept apart.

    One row per month in the range, including months with nothing in them, so a
    chart shows a gap as a gap rather than closing it up. Build and inference
    cost are never summed here — a chart may stack them, which is a drawing
    decision, not a claim that they are the same kind of money.

    Tokens ride along because they answer a different question from cost: the
    same dollars can buy very different amounts of work, and cached input bills
    at a fraction of the standard rate.
    """
    build = dict(
        conn.execute(
            "SELECT period, COALESCE(SUM(amount), 0) FROM build_cost "
            "WHERE period BETWEEN %s AND %s GROUP BY period",
            (start, end),
        ).fetchall()
    )
    inference = {
        row[0]: row
        for row in conn.execute(
            "SELECT period, COALESCE(SUM(amount), 0), COALESCE(SUM(tokens_in), 0), "
            "COALESCE(SUM(cached_tokens_in), 0), COALESCE(SUM(tokens_out), 0) "
            f"FROM inference_cost WHERE period BETWEEN %s AND %s AND {_ACTIVE_ENV} "  # noqa: S608
            "GROUP BY period",
            (start, end),
        ).fetchall()
    }
    out, month = [], start
    while month <= end:
        row = inference.get(month)
        tokens_in = int(row[2]) if row else 0
        cached = int(row[3]) if row else 0
        out.append(
            {
                "period": month.isoformat(),
                "build_cost": float(build.get(month, 0)),
                "inference_cost": float(row[1]) if row else 0.0,
                "tokens_in": tokens_in,
                # A subset of tokens_in, not an addition to it: the provider
                # counts a cached read as input and bills it at a fraction.
                "cached_tokens_in": cached,
                "tokens_out": int(row[4]) if row else 0,
                "cache_rate": (cached / tokens_in * 100) if tokens_in else 0.0,
            }
        )
        month = dt.date(month.year + (month.month // 12), (month.month % 12) + 1, 1)
    return out


def _provider_spend(conn, start: dt.date, end: dt.date) -> list[dict]:
    """Total spend per provider over the range — build and inference together.

    This is the one place the two are added, and only to answer "who do we pay":
    a vendor invoices for both, so a vendor list that split them would be
    answering a question nobody asked. Each row still carries the split.
    """
    rows: dict = {}

    def add(name: str, key: str, amount: float) -> None:
        if amount <= 0:
            return
        entry = rows.setdefault(name, {"provider": name, "build_cost": 0.0, "inference_cost": 0.0})
        entry[key] += amount

    for provider, amount in conn.execute(
        "SELECT provider, COALESCE(SUM(amount), 0) FROM inference_cost "
        f"WHERE period BETWEEN %s AND %s AND {_ACTIVE_ENV} GROUP BY provider",  # noqa: S608
        (start, end),
    ).fetchall():
        add(provider or "unknown", "inference_cost", float(amount))

    # Build cost is grouped by `tool`, not `source`: the tool IS the vendor here
    # (Claude Code, Cursor, Copilot), which is what a "who do we pay" list means.
    for tool, amount in conn.execute(
        "SELECT tool, COALESCE(SUM(amount), 0) FROM build_cost "
        "WHERE period BETWEEN %s AND %s GROUP BY tool",
        (start, end),
    ).fetchall():
        add(str(tool), "build_cost", float(amount))

    out = sorted(rows.values(), key=lambda r: r["build_cost"] + r["inference_cost"], reverse=True)
    total = sum(r["build_cost"] + r["inference_cost"] for r in out)
    for row in out:
        row["amount"] = row["build_cost"] + row["inference_cost"]
        row["share"] = (row["amount"] / total * 100) if total else 0.0
    return out


def _open_actions(unattributed: dict, totals: dict, facts: dict) -> list[dict]:
    """Things a person could go and fix, each with where to fix it.

    Only conditions that are actually actionable, and only when they are true:
    an empty list means there is nothing to do, which is a real answer and is
    shown as one.
    """
    actions: list[dict] = []
    total_ai = totals["build_cost"] + totals["inference_cost"]
    unattributed_total = unattributed["build_cost"] + unattributed["inference_cost"]

    if unattributed_total > 0 and total_ai > 0:
        actions.append(
            {
                "kind": "unattributed",
                "title": f"Resolve {_fmt_money(unattributed_total)} unattributed spend",
                "detail": f"{_fmt_pct(unattributed_total / total_ai * 100)} of AI spend is not "
                "tied to a feature.",
                "href": "/cost-sources",
                "tone": "warn",
            }
        )

    unset = facts.get("env", {}).get("unclassified", 0.0)
    if unset > 0 and totals["inference_cost"] > 0:
        actions.append(
            {
                "kind": "environment",
                "title": "Classify keys by environment",
                "detail": f"{_fmt_money(unset)} of inference spend has no environment set, so "
                "the production split is incomplete.",
                "href": "/cost-sources",
                "tone": "info",
            }
        )

    return actions


def _highlights(rows: list) -> dict:
    """Executive-summary picks. Each is a full feature row (or None).

    Build and inference stay separate on each card; ranking uses their sum only to
    *pick* the row, never to display a blended per-feature number (invariant 2).
    """
    most_expensive = max(rows, key=lambda r: r["build_cost"] + r["inference_cost"], default=None)
    if most_expensive and (most_expensive["build_cost"] + most_expensive["inference_cost"]) == 0:
        most_expensive = None

    cost_per_user_rows = [r for r in rows if r["cost_per_user"] is not None]
    highest_cost_per_user = max(cost_per_user_rows, key=lambda r: r["cost_per_user"], default=None)

    # The biggest lever to optimize: the costliest feature flagged as "watch"
    # (high cost per active user).
    watch_rows = [r for r in rows if r["worth_it"] == "watch"]
    optimization = max(watch_rows, key=lambda r: r["inference_cost"], default=None)

    return {
        "most_expensive": most_expensive,
        "optimization": optimization,
        "highest_cost_per_user": highest_cost_per_user,
    }


def _rollup(conn, table: str, start: dt.date, end: Optional[dt.date] = None) -> dict:
    """feature_id (str or None) -> {amount, confidence(min)} for a cost table/range."""
    end = end or start
    rows = conn.execute(
        f"SELECT feature_id, amount, confidence FROM {table} "  # noqa: S608
        "WHERE period BETWEEN %s AND %s",
        (start, end),
    ).fetchall()
    out: dict = {}
    for feature_id, amount, confidence in rows:
        key = str(feature_id) if feature_id is not None else None
        entry = out.setdefault(key, {"amount": 0.0, "confidence": None})
        entry["amount"] += float(amount)
        entry["confidence"] = _min_confidence(entry["confidence"], confidence)
    return out


def _accum(
    features: dict, key: str, amount: float, confidence: Optional[str], requests=None
) -> None:
    entry = features.setdefault(key, {"amount": 0.0, "confidence": None, "requests": None})
    entry["amount"] += amount
    entry["confidence"] = _min_confidence(entry["confidence"], confidence)
    if requests is not None:
        entry["requests"] = (entry["requests"] or 0) + int(requests)


def _inference_rollup(conn, start: dt.date, end: Optional[dt.date] = None) -> tuple[dict, float]:
    """Hook-aware inference per feature + Unattributed amount, over a month range.

    Where the hook is active for a provider, hook rows give the per-feature truth
    and the connector (cost_api) total is the bill; the gap (bill - hook) flows to
    Unattributed. Providers without a hook attribute via their connector rows as
    before. This prevents double-counting the same spend. Reconciliation is over
    the whole range (bill and hook totals are summed across months first).
    """
    end = end or start
    rows = conn.execute(
        "SELECT feature_id, amount, confidence, source, provider, request_count "
        f"FROM inference_cost WHERE period BETWEEN %s AND %s AND {_ACTIVE_ENV}",  # noqa: S608
        (start, end),
    ).fetchall()
    hook_providers = {provider for (_f, _a, _c, src, provider, _r) in rows if src == "hook"}

    features: dict = {}
    unattributed = 0.0
    billed: dict = {}
    hooked: dict = {}
    for feature_id, amount, confidence, source, provider, requests in rows:
        amt = float(amount)
        if source == "hook":
            hooked[provider] = hooked.get(provider, 0.0) + amt
            if feature_id is None:
                unattributed += amt
            else:
                _accum(features, str(feature_id), amt, confidence, requests)
        else:  # cost_api
            billed[provider] = billed.get(provider, 0.0) + amt
            if provider in hook_providers:
                continue  # hook supersedes per-feature display; bill drives reconciliation
            if feature_id is None:
                unattributed += amt
            else:
                _accum(features, str(feature_id), amt, confidence, requests)

    for provider in hook_providers:
        gap = billed.get(provider, 0.0) - hooked.get(provider, 0.0)
        if gap > 0:
            unattributed += gap  # untagged calls / mispriced models land in Unattributed
    return features, unattributed


def heuristic_optimization(conn, feature_id: str, start: dt.date) -> dict:
    """The heuristic (estimated) optimization tier for one feature/month.

    Directional rules of thumb over the month's inference usage — spend, model
    mix, and the input/output token split. Labelled as an estimate in the UI; the
    *measured* tier (optimize_measured) sits above it when the SDK is installed.
    """
    opt_rows = conn.execute(
        """
        SELECT model,
               SUM(amount),
               SUM(COALESCE(tokens_in, 0)),
               SUM(COALESCE(tokens_out, 0)),
               SUM(COALESCE(request_count, 0))
        FROM inference_cost
        WHERE feature_id = %s AND period = %s
        GROUP BY model
        """,
        (feature_id, start),
    ).fetchall()

    opt_total = sum(float(a) for _m, a, _ti, _to, _r in opt_rows)
    in_tokens = sum(int(ti) for _m, _a, ti, _to, _r in opt_rows)
    out_tokens = sum(int(to) for _m, _a, _ti, to, _r in opt_rows)
    requests = sum(int(r) for _m, _a, _ti, _to, r in opt_rows)
    # Split spend into input/output by token share (fallback 70/30 when tokens
    # are unknown, e.g. connector-only rows that don't report token counts).
    token_sum = in_tokens + out_tokens
    input_share = (in_tokens / token_sum) if token_sum else 0.7
    input_cost = opt_total * input_share
    output_cost = opt_total - input_cost
    return optimize.estimate(opt_total, input_cost, output_cost, requests)


def feature_detail(
    tenant_id: str,
    feature_id: str,
    start: Optional[dt.date] = None,
    end: Optional[dt.date] = None,
    range_token: Optional[str] = None,
) -> Optional[dict]:
    """One feature over a review period (default: the latest month with data).

    Cost — build, inference, and the optimization anchor — is scoped to the same
    inclusive month range the Overview uses, so the totals here reconcile with the
    Overview's feature row. Engineering activity (PRs / commits / files) and the
    evidence trail are deliberately ALL-TIME, and labelled as such in the UI.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        start, end = _resolve_range(conn, range_token, start, end)

        feature = conn.execute(
            """
            SELECT f.id, f.name, f.description, f.status, f.discovery_confidence,
                   f.category, f.category_source, f.product_id, p.name, f.product_source
            FROM feature f LEFT JOIN product p ON p.id = f.product_id
            WHERE f.id = %s
            """,
            (feature_id,),
        ).fetchone()
        if feature is None:
            return None
        # Resolved exactly as the Overview resolves it (user tag > discovery guess).
        category, category_source = resolve_category(feature[5], feature[6])

        # Cost headlines are summed over the selected range (matching the Overview).
        build_total = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM build_cost "
            "WHERE feature_id = %s AND period BETWEEN %s AND %s",
            (feature_id, start, end),
        ).fetchone()[0]
        inference_range = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM inference_cost "
            f"WHERE feature_id = %s AND period BETWEEN %s AND %s AND {_ACTIVE_ENV}",  # noqa: S608
            (feature_id, start, end),
        ).fetchone()[0]
        # Active users is a point-in-time count -> the range's latest month.
        active_users = conn.execute(
            "SELECT active_users FROM feature_usage WHERE feature_id = %s AND period = %s",
            (feature_id, end),
        ).fetchone()

        # Avg call latency (ms) from metered (hook) rows over the range: sum / calls.
        # None when the SDK hasn't reported latency for this feature/range.
        lat_row = conn.execute(
            "SELECT SUM(latency_ms_sum), SUM(request_count) FROM inference_cost "
            "WHERE feature_id = %s AND period BETWEEN %s AND %s AND source = 'hook' "
            "AND latency_ms_sum IS NOT NULL",
            (feature_id, start, end),
        ).fetchone()
        avg_latency_ms = (
            round(float(lat_row[0]) / float(lat_row[1]))
            if lat_row and lat_row[0] is not None and lat_row[1]
            else None
        )

        # PRs / commits / files each developer authored for this feature. This is
        # ALL-TIME engineering activity (not scoped to the cost range).
        pr_stats = {
            actor: {"prs": prs, "commits": commits, "files_changed": files}
            for actor, prs, commits, files in conn.execute(
                """
                SELECT actor,
                       COUNT(DISTINCT external_ref),
                       SUM(commits),
                       SUM(files_changed)
                FROM feature_signal
                WHERE feature_id = %s AND signal_type = 'pr' AND actor IS NOT NULL
                GROUP BY actor
                """,
                (feature_id,),
            ).fetchall()
        }

        # Per-developer build spend IS scoped to the range (reconciles with build_total).
        by_developer = [
            {
                "developer_id": dev,
                "tool": tool,
                "amount": float(amount),
                "confidence": conf,
                # None when unknown (no matched PR authorship / stats not collected).
                "prs": pr_stats.get(dev, {}).get("prs"),
                "commits": pr_stats.get(dev, {}).get("commits"),
                "files_changed": pr_stats.get(dev, {}).get("files_changed"),
            }
            for dev, tool, amount, conf in conn.execute(
                """
                SELECT developer_id, tool, SUM(amount), MIN(confidence)
                FROM build_cost WHERE feature_id = %s AND period BETWEEN %s AND %s
                GROUP BY developer_id, tool ORDER BY SUM(amount) DESC
                """,
                (feature_id, start, end),
            ).fetchall()
        ]
        build_contributors = conn.execute(
            "SELECT COUNT(DISTINCT developer_id) FROM build_cost "
            "WHERE feature_id = %s AND period BETWEEN %s AND %s",
            (feature_id, start, end),
        ).fetchone()[0]

        # Evidence trail: ALL-TIME signals behind the feature (not range-scoped).
        evidence = [
            {
                "signal_type": st,
                "external_ref": ref,
                "confidence": conf,
                "actor": actor,
                "source": src,
            }
            for st, ref, conf, actor, src in conn.execute(
                """
                SELECT signal_type, external_ref, confidence, actor, source
                FROM feature_signal WHERE feature_id = %s
                ORDER BY signal_type, external_ref
                """,
                (feature_id,),
            ).fetchall()
        ]

        sources = sorted(
            {
                s
                for (s,) in conn.execute(
                    "SELECT DISTINCT source FROM inference_cost WHERE feature_id = %s",
                    (feature_id,),
                ).fetchall()
            }
        )

        # Cost-optimization estimate (heuristic): anchored at the range's latest
        # month, so it reflects the period being viewed.
        optimization = heuristic_optimization(conn, feature_id, end)

    return {
        "feature_id": str(feature[0]),
        "name": feature[1],
        "description": feature[2],
        "status": feature[3],
        "category": category,
        "category_source": category_source,
        # The product this feature rolls up into, so the drill-down can name it
        # and offer to change it.
        "product_id": str(feature[7]) if feature[7] else None,
        "product_name": feature[8],
        "product_source": feature[9],
        "discovery_confidence": feature[4],
        "period": end.isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "headline": {
            "build_cost": float(build_total),
            "inference_cost": float(inference_range),  # separate from build
            "active_users": active_users[0] if active_users else None,
            "avg_latency_ms": avg_latency_ms,  # from metered calls; None if unknown
        },
        "build_total": float(build_total),  # AI build spend for this feature in-range
        "build_contributors": build_contributors,
        "build_by_developer": by_developer,
        "evidence": evidence,
        "inference_sources": sources,  # ["cost_api"] now; "hook" arrives in M7
        "optimization": optimization,  # heuristic estimate (clearly labelled in UI)
    }


def _months_back(day: dt.date, n: int) -> dt.date:
    """First-of-month n months before ``day`` (month-aligned)."""
    month = day.month - n
    year = day.year
    while month <= 0:
        month += 12
        year -= 1
    return dt.date(year, month, 1)


def feature_inference(
    tenant_id: str,
    feature_id: str,
    start: Optional[dt.date] = None,
    end: Optional[dt.date] = None,
    range_token: Optional[str] = None,
) -> dict:
    """Inference cost for a feature over the review period: per-model + monthly trend.

    Scoped to the same month range as the rest of the detail page (and the
    Overview), so per-model amounts sum to the in-range total (used for the % share
    + pie), and the trend has one point per month in the range.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        start, end = _resolve_range(conn, range_token, start, end)

        model_rows = conn.execute(
            f"""
            SELECT model, SUM(amount), SUM(request_count)
            FROM inference_cost
            WHERE feature_id = %s AND period BETWEEN %s AND %s AND {_ACTIVE_ENV}
            GROUP BY model ORDER BY SUM(amount) DESC
            """,  # noqa: S608
            (feature_id, start, end),
        ).fetchall()
        total = sum(float(amount) for _model, amount, _req in model_rows) or 0.0
        by_model = [
            {
                "model": model or "unknown",
                "amount": float(amount),
                "pct": (float(amount) / total * 100.0) if total else 0.0,
                "requests": int(req) if req is not None else None,
            }
            for model, amount, req in model_rows
        ]
        trend = [
            {"period": p.isoformat(), "amount": float(amount)}
            for p, amount in conn.execute(
                f"""
                SELECT period, SUM(amount)
                FROM inference_cost
                WHERE feature_id = %s AND period BETWEEN %s AND %s AND {_ACTIVE_ENV}
                GROUP BY period ORDER BY period
                """,  # noqa: S608
                (feature_id, start, end),
            ).fetchall()
        ]

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total": total,
        "by_model": by_model,
        "trend": trend,
    }


def spend_by_customer(
    tenant_id: str,
    start: Optional[dt.date] = None,
    end: Optional[dt.date] = None,
    range_token: Optional[str] = None,
) -> dict:
    """Who the inference spend was consumed BY, over a month range.

    Provider bills say what was spent, never on whose behalf — this view exists
    only because the metering SDK tags calls with `metadata.customer_id`, and it
    is populated from `customer_cost` (hook events) alone. It is therefore a
    SUBSET of the authoritative inference bill, not a second version of it: the
    coverage figure says how much of the bill carries a customer tag, so a reader
    can never mistake the part for the whole.

    Build cost has no customer (it is what the team spent making the feature),
    so it does not appear here — invariant 2 again.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        start, end = _resolve_range(conn, range_token, start, end)
        n = _month_count(start, end)
        prev_end = _months_back(start, 1)
        prev_start = _months_back(prev_end, n - 1)

        rows = conn.execute(
            """
            SELECT customer_id, SUM(amount), SUM(request_count), COUNT(DISTINCT period)
            FROM customer_cost
            WHERE period BETWEEN %s AND %s
            GROUP BY customer_id ORDER BY SUM(amount) DESC
            """,
            (start, end),
        ).fetchall()
        # Same customers over the preceding equal-length window, for the delta.
        prev = {
            cid: float(a)
            for cid, a in conn.execute(
                "SELECT customer_id, SUM(amount) FROM customer_cost "
                "WHERE period BETWEEN %s AND %s GROUP BY customer_id",
                (prev_start, prev_end),
            ).fetchall()
        }
        trend_rows = conn.execute(
            "SELECT period, SUM(amount) FROM customer_cost "
            "WHERE period BETWEEN %s AND %s GROUP BY period ORDER BY period",
            (start, end),
        ).fetchall()
        # The whole inference bill for the same window — the denominator that
        # keeps "metered" honest about being a subset. Read through the same
        # reconciliation the Overview uses, so this ties to the headline number:
        # a hook row and its connector row are the same dollar, counted once.
        feats, unattributed = _inference_rollup(conn, start, end)
        inference_total = sum(f["amount"] for f in feats.values()) + unattributed

    total = sum(float(a) for _c, a, _r, _m in rows) or 0.0
    customers = []
    for cid, amount, requests, months_active in rows:
        amount = float(amount)
        requests = int(requests) if requests is not None else 0
        was = prev.get(cid)
        customers.append(
            {
                "customer_id": cid,
                "amount": amount,
                "pct": (amount / total * 100.0) if total else 0.0,
                "requests": requests or None,
                # Unit economics: what one metered call from this customer costs.
                "cost_per_request": (amount / requests) if requests else None,
                # Spend over the equal-length window before this one. None means
                # the customer is new to this window — not a 0% change.
                "prev_amount": was,
                "delta_pct": ((amount - was) / was * 100.0) if was else None,
                "months_active": int(months_active),
            }
        )
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "months": n,
        "total": total,
        "customers": customers,
        "trend": [{"period": p.isoformat(), "amount": float(a)} for p, a in trend_rows],
        # Metered spend as a share of the real inference bill for this window.
        "inference_total": inference_total,
        "coverage_pct": (total / inference_total * 100.0) if inference_total else 0.0,
    }


def spend_by_product(
    tenant_id: str,
    start: Optional[dt.date] = None,
    end: Optional[dt.date] = None,
    range_token: Optional[str] = None,
) -> dict:
    """What each product cost over a month range — build and inference, apart.

    A product is the customer's own grouping above features, so this is the
    feature rollup re-keyed by product. It reuses `_rollup` and `_inference_rollup`
    rather than querying the cost tables again: those two already apply the active
    environment filter and reconcile hook rows against the provider bill, so the
    numbers here cannot drift from the Overview's.

    TWO RESIDUALS, AND THEY ARE NOT THE SAME THING:

      `unassigned`   — features that exist but belong to no product. Assignable:
                       the customer can map a repo or pick a product and it moves.
      `unattributed` — spend with no feature at all. Part of it is not even a row
                       (it is the bill-minus-hook reconciliation gap), so it can
                       never belong to a product, no matter how complete the
                       mapping gets.

    Merging them would let unattributable dollars appear under a product heading.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        start, end = _resolve_range(conn, range_token, start, end)
        n = _month_count(start, end)

        features = conn.execute(
            """
            SELECT f.id, f.product_id, p.name
            FROM feature f LEFT JOIN product p ON p.id = f.product_id
            WHERE f.status IN ('proposed', 'confirmed')
            """
        ).fetchall()
        repos = conn.execute("SELECT product_id, repo FROM product_repo ORDER BY repo").fetchall()
        # Products with no features yet still belong on the screen: a customer who
        # has just created one should see it sitting at zero, not wonder where it
        # went.
        all_products = conn.execute("SELECT id, name FROM product ORDER BY lower(name)").fetchall()

        build = _rollup(conn, "build_cost", start, end)
        inference, inference_unattributed = _inference_rollup(conn, start, end)

    by_product: dict = {
        str(pid): {
            "product_id": str(pid),
            "name": pname,
            "build_cost": 0.0,
            "inference_cost": 0.0,
            "feature_count": 0,
            "repos": [],
            "confidence": None,
        }
        for pid, pname in all_products
    }
    for pid, repo in repos:
        entry = by_product.get(str(pid))
        if entry is not None:
            entry["repos"].append(repo)

    unassigned = {
        "build_cost": 0.0,
        "inference_cost": 0.0,
        "feature_count": 0,
        "spanning_count": 0,
    }
    for fid, product_id, _pname in features:
        fid = str(fid)
        b = build.get(fid, {"amount": 0.0, "confidence": None})
        i = inference.get(fid, {"amount": 0.0, "confidence": None})
        target = by_product.get(str(product_id)) if product_id else None
        if target is None:
            unassigned["build_cost"] += b["amount"]
            unassigned["inference_cost"] += i["amount"]
            unassigned["feature_count"] += 1
            continue
        target["build_cost"] += b["amount"]
        target["inference_cost"] += i["amount"]
        target["feature_count"] += 1
        target["confidence"] = _min_confidence(
            target["confidence"], _min_confidence(b["confidence"], i["confidence"])
        )

    # Spend with no feature at all. Build's Unattributed is the NULL-feature key;
    # inference's also carries the reconciliation gap, which has no row to assign.
    unattributed = {
        "build_cost": build.get(None, {"amount": 0.0})["amount"],
        "inference_cost": inference_unattributed,
    }
    # Ranked by what a product costs in total — to ORDER the list only. The two
    # figures are never added together for display (invariant 2).
    products_out = sorted(
        by_product.values(),
        key=lambda p: p["build_cost"] + p["inference_cost"],
        reverse=True,
    )
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "months": n,
        "products": products_out,
        "unassigned": unassigned,
        "unattributed": unattributed,
        "totals": {
            "build_cost": sum(p["build_cost"] for p in products_out)
            + unassigned["build_cost"]
            + unattributed["build_cost"],
            "inference_cost": sum(p["inference_cost"] for p in products_out)
            + unassigned["inference_cost"]
            + unattributed["inference_cost"],
        },
    }


def spend_by_provider(
    tenant_id: str,
    start: Optional[dt.date] = None,
    end: Optional[dt.date] = None,
    range_token: Optional[str] = None,
) -> dict:
    """Tenant-wide spend by source over a month range, with trends.

    Two parallel, never-blended views (invariant 2):
      - inference (run) cost grouped by provider — self-hosted pools appear
        under their provider label;
      - build cost grouped by coding tool.
    Each carries its own total and per-month trend across the range.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        start, end = _resolve_range(conn, range_token, start, end)

        # ---- Inference: by provider + trend ----
        provider_rows = conn.execute(
            f"""
            SELECT provider, SUM(amount), SUM(request_count)
            FROM inference_cost
            WHERE period BETWEEN %s AND %s AND {_ACTIVE_ENV}
            GROUP BY provider ORDER BY SUM(amount) DESC
            """,  # noqa: S608
            (start, end),
        ).fetchall()
        total = sum(float(amount) for _p, amount, _req in provider_rows) or 0.0

        # Per-provider model split (so each provider's total breaks down by model).
        model_rows = conn.execute(
            f"""
            SELECT provider, model, SUM(amount)
            FROM inference_cost
            WHERE period BETWEEN %s AND %s AND {_ACTIVE_ENV}
            GROUP BY provider, model ORDER BY SUM(amount) DESC
            """,  # noqa: S608
            (start, end),
        ).fetchall()
        models_by_provider: dict[str, list] = {}
        for provider, model, amount in model_rows:
            models_by_provider.setdefault(provider, []).append((model, float(amount)))

        by_provider = [
            {
                "provider": provider,
                "amount": float(amount),
                "pct": (float(amount) / total * 100.0) if total else 0.0,
                "requests": int(req) if req is not None else None,
                "by_model": [
                    {
                        "model": model or "unknown",
                        "amount": amt,
                        # Share of THIS provider's spend (models under a provider sum to ~100%).
                        "pct": (amt / float(amount) * 100.0) if amount else 0.0,
                    }
                    for model, amt in models_by_provider.get(provider, [])
                ],
            }
            for provider, amount, req in provider_rows
        ]
        # Inference trend, segmented by classification per month (a stacked bar).
        # NULL environment (legacy / never-classified) counts as unclassified;
        # 'ignore' is excluded (via _ACTIVE_ENV), so the four buckets sum to the
        # month's active inference total.
        trend_rows = conn.execute(
            f"""
            SELECT period, COALESCE(environment, 'unclassified') AS env, SUM(amount)
            FROM inference_cost
            WHERE period BETWEEN %s AND %s AND {_ACTIVE_ENV}
            GROUP BY period, env ORDER BY period
            """,  # noqa: S608
            (start, end),
        ).fetchall()
        trend_by_period: dict[str, dict] = {}
        for p, env, amount in trend_rows:
            key = p.isoformat()
            entry = trend_by_period.setdefault(
                key,
                {
                    "period": key,
                    "total": 0.0,
                    "production": 0.0,
                    "development": 0.0,
                    "internal": 0.0,
                    "unclassified": 0.0,
                },
            )
            amt = float(amount)
            entry["total"] += amt
            bucket = env if env in _CLASSIFICATION_BUCKETS else "unclassified"
            entry[bucket] += amt
        trend = sorted(trend_by_period.values(), key=lambda t: t["period"])

        # ---- Inference: DAILY trend (from the day-resolution table) ----
        # Same classification-bucketed shape as the monthly trend, one point per day
        # over the range. The UI uses this for short ranges (a month or two) and the
        # monthly trend for long ones.
        daily_rows = conn.execute(
            f"""
            SELECT day, COALESCE(environment, 'unclassified') AS env, SUM(amount)
            FROM inference_cost_daily
            WHERE day >= %s AND day < %s AND {_ACTIVE_ENV}
            GROUP BY day, env ORDER BY day
            """,  # noqa: S608
            (start, next_month(end)),
        ).fetchall()
        daily_by_day: dict[str, dict] = {}
        for d, env, amount in daily_rows:
            key = d.isoformat()
            entry = daily_by_day.setdefault(
                key,
                {
                    "period": key,
                    "total": 0.0,
                    "production": 0.0,
                    "development": 0.0,
                    "internal": 0.0,
                    "unclassified": 0.0,
                },
            )
            amt = float(amount)
            entry["total"] += amt
            bucket = env if env in _CLASSIFICATION_BUCKETS else "unclassified"
            entry[bucket] += amt

        # Per-day workspace split, attached to each point so the chart's hover can
        # show WHERE a day's spend came from alongside what kind it was.
        for d, ws, amount in conn.execute(
            f"""
            SELECT day, COALESCE(workspace_name, workspace_id) AS ws, SUM(amount)
            FROM inference_cost_daily
            WHERE day >= %s AND day < %s AND {_ACTIVE_ENV}
              AND (workspace_id IS NOT NULL OR workspace_name IS NOT NULL)
            GROUP BY day, ws ORDER BY day, SUM(amount) DESC
            """,  # noqa: S608
            (start, next_month(end)),
        ).fetchall():
            entry = daily_by_day.get(d.isoformat())
            if entry is not None:
                entry.setdefault("workspaces", []).append(
                    {"workspace": ws, "amount": float(amount)}
                )
        daily_trend = sorted(daily_by_day.values(), key=lambda t: t["period"])

        # Same workspace split for the MONTHLY trend points.
        for p, ws, amount in conn.execute(
            f"""
            SELECT period, COALESCE(workspace_name, workspace_id) AS ws, SUM(amount)
            FROM inference_cost
            WHERE period BETWEEN %s AND %s AND {_ACTIVE_ENV}
              AND (workspace_id IS NOT NULL OR workspace_name IS NOT NULL)
            GROUP BY period, ws ORDER BY period, SUM(amount) DESC
            """,  # noqa: S608
            (start, end),
        ).fetchall():
            entry = trend_by_period.get(p.isoformat())
            if entry is not None:
                entry.setdefault("workspaces", []).append(
                    {"workspace": ws, "amount": float(amount)}
                )

        # ---- Build: by coding tool + trend (kept separate from inference) ----
        tool_rows = conn.execute(
            """
            SELECT tool, SUM(amount)
            FROM build_cost
            WHERE period BETWEEN %s AND %s
            GROUP BY tool ORDER BY SUM(amount) DESC
            """,
            (start, end),
        ).fetchall()
        build_total = sum(float(amount) for _t, amount in tool_rows) or 0.0
        build_by_tool = [
            {
                "tool": tool,
                "amount": float(amount),
                "pct": (float(amount) / build_total * 100.0) if build_total else 0.0,
            }
            for tool, amount in tool_rows
        ]
        build_trend = [
            {"period": p.isoformat(), "amount": float(amount)}
            for p, amount in conn.execute(
                """
                SELECT period, SUM(amount)
                FROM build_cost
                WHERE period BETWEEN %s AND %s
                GROUP BY period ORDER BY period
                """,
                (start, end),
            ).fetchall()
        ]

        # ---- Build: by developer, each broken down by tool ----
        # Build cost is the only cost attributable to a person (who ran which
        # coding tool). Rows with no developer (e.g. fine-tuning, seat pools) fall
        # into an Unattributed bucket so developers + Unattributed reconcile to the
        # build total.
        # `label` combines the display name and GitHub handle ("Name (handle)"),
        # falling back to whichever is present — this is the only view that shows
        # the combined identity.
        dev_rows = conn.execute(
            """
            SELECT developer_id, developer_name, github_handle, tool, SUM(amount)
            FROM build_cost
            WHERE period BETWEEN %s AND %s
            GROUP BY developer_id, developer_name, github_handle, tool
            ORDER BY SUM(amount) DESC
            """,
            (start, end),
        ).fetchall()
        developers: dict[str, dict] = {}
        for developer_id, dev_name, handle, tool, amount in dev_rows:
            dev = developer_id or "Unattributed"
            entry = developers.setdefault(
                dev,
                {"developer_id": dev, "name": None, "handle": None, "amount": 0.0, "by_tool": []},
            )
            # name/handle are consistent within a developer_id; keep the first seen.
            entry["name"] = entry["name"] or dev_name
            entry["handle"] = entry["handle"] or handle
            entry["amount"] += float(amount)
            entry["by_tool"].append((tool, float(amount)))
        build_by_developer = [
            {
                "developer_id": d["developer_id"],
                "label": developer_label(d["name"], d["handle"], fallback=d["developer_id"]),
                "amount": d["amount"],
                "pct": (d["amount"] / build_total * 100.0) if build_total else 0.0,
                "by_tool": [
                    {
                        "tool": tool,
                        "amount": amt,
                        # Share of THIS developer's build spend (tools sum to ~100%).
                        "pct": (amt / d["amount"] * 100.0) if d["amount"] else 0.0,
                    }
                    for tool, amt in sorted(d["by_tool"], key=lambda t: -t[1])
                ],
            }
            for d in sorted(developers.values(), key=lambda d: -d["amount"])
        ]

        # ---- Engineering activity per developer (what they shipped) --------
        # Build cost says what a developer's AI tooling cost; this says what came
        # out the other side. It reads PR evidence — the same feature_signal rows
        # behind every build-cost attribution — scoped by the PR's own merge date,
        # so it lines up with the spend beside it instead of being all-time.
        # PRs merged before migration 0035 have no merged_at and no line counts;
        # they are reported as unknown ("—"), never as zero.
        # Collapsed to one row per pull request BEFORE the sums. Clustering may
        # place a single PR under two features, and nothing forbids it — there is
        # no unique constraint on (feature_id, external_ref). Summing the raw
        # rows therefore counted that PR's commits, files and lines twice, so a
        # developer whose work spanned two features read as having shipped twice
        # as much. The PR count was already distinct; these were not.
        activity_rows = conn.execute(
            """
            WITH windowed AS (
                SELECT lower(actor) AS actor, external_ref, feature_id,
                       commits, files_changed, additions, deletions
                FROM feature_signal
                WHERE signal_type = 'pr' AND actor IS NOT NULL
                  AND merged_at >= %s AND merged_at < %s
            ),
            per_pr AS (
                -- Every row for one PR carries that PR's own stats, so MAX is
                -- simply "the value", and it survives a NULL beside it.
                SELECT actor, external_ref,
                       MAX(commits) AS commits, MAX(files_changed) AS files_changed,
                       MAX(additions) AS additions, MAX(deletions) AS deletions
                FROM windowed
                GROUP BY actor, external_ref
            )
            SELECT p.actor,
                   COUNT(*),
                   -- Features touched stays a property of the rows, not the PRs:
                   -- two PRs into one feature is one feature.
                   (SELECT COUNT(DISTINCT w.feature_id)
                      FROM windowed w WHERE w.actor = p.actor),
                   SUM(p.commits), SUM(p.files_changed),
                   SUM(p.additions), SUM(p.deletions)
            FROM per_pr p
            GROUP BY p.actor
            """,
            (start, next_month(end)),
        ).fetchall()
        # Spend per GitHub handle, matched case-insensitively — the same rule the
        # build-cost allocator uses to attribute a PR to a developer.
        spend_by_handle: dict[str, dict] = {}
        for d in developers.values():
            if d["handle"]:
                spend_by_handle[d["handle"].lower()] = d
        developer_activity = [
            {
                "handle": handle,
                "label": (
                    developer_label(
                        spend_by_handle[handle]["name"], spend_by_handle[handle]["handle"]
                    )
                    if handle in spend_by_handle
                    else handle
                ),
                "prs": int(prs),
                # Features this developer's merged PRs touched.
                "features": int(features),
                "commits": int(commits) if commits is not None else None,
                "files_changed": int(files) if files is not None else None,
                "additions": int(adds) if adds is not None else None,
                "deletions": int(dels) if dels is not None else None,
                # Their AI coding-tool spend over the SAME window, so the two
                # halves of the row are comparable.
                "build_cost": spend_by_handle[handle]["amount"]
                if handle in spend_by_handle
                else 0.0,
                "cost_per_pr": (spend_by_handle[handle]["amount"] / prs)
                if handle in spend_by_handle and prs
                else None,
            }
            for handle, prs, features, commits, files, adds, dels in sorted(
                activity_rows, key=lambda r: -r[1]
            )
        ]

        # ---- Why the activity table is empty, when it is ------------------
        # An empty table has four quite different causes, and the reader can only
        # act on the right one: GitHub was never connected, discovery has not run,
        # it ran but covered other months, or nothing was merged in this window.
        # These facts let the UI say which, instead of rendering nothing at all.
        coverage = conn.execute(
            """
            SELECT COUNT(*) FILTER (WHERE merged_at IS NOT NULL),
                   COUNT(*) FILTER (WHERE merged_at IS NULL),
                   MIN(merged_at), MAX(merged_at)
            FROM feature_signal WHERE signal_type = 'pr'
            """
        ).fetchone()
        github = conn.execute(
            "SELECT EXISTS (SELECT 1 FROM connector_credential WHERE connector_type = 'github')"
        ).fetchone()
        # What discovery actually did, which is the only thing that separates
        # "we looked and there was nothing" from "we never looked". Read here
        # rather than inferred from the PR dates below, which cannot tell them
        # apart. `discovery_run` is empty for tenants that predate it, so the PR
        # dates stay as the fallback.
        run = conn.execute(
            """
            SELECT MIN(covered_from), MAX(covered_to), COUNT(*)
            FROM discovery_run WHERE status = 'success'
            """
        ).fetchone()
        latest = conn.execute(
            "SELECT started_at, status, trigger FROM discovery_run ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        activity_coverage = {
            "github_connected": bool(github[0]),
            # PR evidence anywhere in the tenant, not just this window — that is
            # the difference between "discovery never ran" and "it ran elsewhere".
            "dated_prs": int(coverage[0] or 0),
            # Merged before migration 0035, so they carry no date and cannot be
            # placed in any window. Counted, never silently dropped.
            "undated_prs": int(coverage[1] or 0),
            "first_merged": coverage[2].isoformat() if coverage[2] else None,
            "last_merged": coverage[3].isoformat() if coverage[3] else None,
            # The recorded coverage: the earliest merge date any successful run
            # reached back to, and the latest day one ran up to.
            "runs": int(run[2] or 0),
            "covered_from": run[0].isoformat() if run[0] else None,
            "covered_to": run[1].isoformat() if run[1] else None,
            "last_run_at": latest[0].isoformat() if latest else None,
            "last_run_status": latest[1] if latest else None,
            "last_run_trigger": latest[2] if latest else None,
        }

        # ---- Per-customer metered spend (from SDK metadata.customer_id) ----
        customer_rows = conn.execute(
            """
            SELECT customer_id, SUM(amount), SUM(request_count)
            FROM customer_cost
            WHERE period BETWEEN %s AND %s
            GROUP BY customer_id ORDER BY SUM(amount) DESC
            """,
            (start, end),
        ).fetchall()
        customer_total = sum(float(a) for _c, a, _r in customer_rows) or 0.0
        by_customer = [
            {
                "customer_id": cid,
                "amount": float(a),
                "pct": (float(a) / customer_total * 100.0) if customer_total else 0.0,
                "requests": int(r) if r is not None else None,
            }
            for cid, a, r in customer_rows
        ]

        # ---- Inference: by TOKEN TYPE (input / cache write / cache read / output) ----
        # Providers bill dollars per line item, not per token type, so we split each
        # (provider, model) group's AUTHORITATIVE dollars across its token types by
        # priced weight — list rates x the type's multiplier (cache writes cost a
        # premium, cache reads a discount). The parts therefore always sum back to
        # the billed total; unpriced models fall back to raw token counts, and rows
        # with no token detail land in "Unknown" rather than being guessed.
        token_rows = conn.execute(
            f"""
            SELECT provider, model,
                   SUM(amount), SUM(tokens_in), SUM(tokens_out),
                   SUM(cached_tokens_in), SUM(cache_write_tokens),
                   SUM(cache_write_5m_tokens), SUM(cache_write_1h_tokens)
            FROM inference_cost
            WHERE period BETWEEN %s AND %s AND {_ACTIVE_ENV}
            GROUP BY provider, model
            """,  # noqa: S608
            (start, end),
        ).fetchall()
        token_dollars: dict[str, float] = defaultdict(float)
        token_counts: dict[str, int] = {}
        for prov, model, amount, t_in, t_out, t_cached, t_write, t_5m, t_1h in token_rows:
            token_buckets_add(
                token_dollars,
                token_counts,
                provider=prov,
                model=model,
                amount=float(amount or 0),
                tokens_in=int(t_in or 0),
                tokens_out=int(t_out or 0),
                cached=int(t_cached or 0),
                cache_write=int(t_write or 0),
                write_5m=int(t_5m or 0),
                write_1h=int(t_1h or 0),
            )
        token_total = sum(token_dollars.values()) or 0.0
        by_token_type = [
            {
                "token_type": key,
                "label": _TOKEN_TYPE_LABELS[key],
                "amount": amt,
                "pct": (amt / token_total * 100.0) if token_total else 0.0,
                # Provider-reported and exact (unlike the derived dollar split).
                "tokens": token_counts.get(key, 0),
            }
            for key, amt in sorted(token_dollars.items(), key=lambda kv: -kv[1])
            if amt > 0
        ]

        # ---- Inference: by workspace, each broken down by API key ----
        # Provider-resource identity (today only Anthropic populates it): the same
        # workspace/key detail the Cost Sources page classifies, rolled up over the
        # review range so it reconciles with the inference total above. Rows without
        # any workspace/key identity (other providers) are simply omitted here.
        ws_rows = conn.execute(
            f"""
            SELECT COALESCE(workspace_name, workspace_id) AS ws,
                   COALESCE(api_key_name, api_key_id) AS api_key,
                   SUM(amount),
                   SUM(COALESCE(tokens_in, 0) + COALESCE(tokens_out, 0))
            FROM inference_cost
            WHERE period BETWEEN %s AND %s AND {_ACTIVE_ENV}
              AND (workspace_id IS NOT NULL OR api_key_id IS NOT NULL)
            GROUP BY ws, api_key ORDER BY SUM(amount) DESC
            """,  # noqa: S608
            (start, end),
        ).fetchall()
        ws_grouped: dict[str, dict] = {}
        for ws, api_key, amount, tokens in ws_rows:
            entry = ws_grouped.setdefault(ws or "—", {"amount": 0.0, "tokens": 0, "keys": []})
            entry["amount"] += float(amount)
            entry["tokens"] += int(tokens or 0)
            entry["keys"].append((api_key, float(amount), int(tokens or 0)))
        workspace_total = sum(e["amount"] for e in ws_grouped.values()) or 0.0
        by_workspace = [
            {
                "workspace": ws,
                "amount": e["amount"],
                "pct": (e["amount"] / workspace_total * 100.0) if workspace_total else 0.0,
                # Provider-reported token counts (input + output), exact.
                "tokens": e["tokens"],
                "by_key": [
                    {
                        "api_key": api_key or "—",
                        "amount": amt,
                        # Share of THIS workspace's spend (keys sum to ~100%).
                        "pct": (amt / e["amount"] * 100.0) if e["amount"] else 0.0,
                        "tokens": tok,
                    }
                    for api_key, amt, tok in sorted(e["keys"], key=lambda k: -k[1])
                ],
            }
            for ws, e in sorted(ws_grouped.items(), key=lambda kv: -kv[1]["amount"])
        ]

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "total": total,
        "by_provider": by_provider,
        "trend": trend,
        "daily_trend": daily_trend,
        "build_total": build_total,
        "build_by_tool": build_by_tool,
        "build_by_developer": build_by_developer,
        "developer_activity": developer_activity,
        "activity_coverage": activity_coverage,
        "build_trend": build_trend,
        "customer_total": customer_total,
        "by_customer": by_customer,
        "workspace_total": workspace_total,
        "by_workspace": by_workspace,
        "token_total": token_total,
        "by_token_type": by_token_type,
    }
