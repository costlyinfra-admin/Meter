"""Where AI spend is heading, and what is pushing it there.

The Overview's budget panel answers one question — where does this month land?
This answers the ones a CFO asks next: what about the next three months, which
feature is driving it, and how sure are we. It is also the single place any
projection in the product is made, so the Overview panel (budgets.period_forecast)
and the Forecast page cannot disagree about the same month.

Two horizons, two methods, because the data behind them is different:

**The open month** is projected from spend already observed this month. Where a
connector reports daily spend, the existing recent-weighted run rate is used and
an 80% range is drawn from how much that daily spend moves around. Where it does
not — the SDK writes monthly rows only — the month-to-date figure is spread
evenly over the days elapsed. Most tenants have some of each, so both are
computed and added: the daily rate covers what the daily rows explain, and the
remainder of the month-to-date spend is carried forward at its own average.

**The next three months** are projected from monthly history: a straight-line
trend through up to six full months, plus this month's projection when it is
trustworthy enough to count, with a range from how far those months sat off the
line. Fewer than three full months is too little to draw a line through, and the
answer is "insufficient", never a guess. Seasonality is not modelled.

Inference and build are projected SEPARATELY and never summed into a figure that
is not labelled as a total (invariant 2). Build is billed per month with no day
resolution, so the open month's build cost is its actual, not a projection.

Every read goes through dashboard's counting rule — one dollar counted once, and
nothing the customer marked `ignore` — because a forecast on a different basis
from the chart beside it is a forecast of a number nobody else in the product
reports.
"""

from __future__ import annotations

import calendar
import datetime as dt
import math
from decimal import Decimal
from typing import Optional

from . import budgets, dashboard
from .db import admin_dsn, app_dsn, connect, tenant_tx
from .providers import month_start, next_month

#: Months ahead of the open one.
HORIZON_MONTHS = 3
#: Full months of history the trend is fitted to, at most and at least.
TREND_MAX_MONTHS = 6
TREND_MIN_MONTHS = 3
#: Two-sided 80% interval of a normal distribution. 80% rather than 95% because
#: a range wide enough to be nearly certain is too wide to plan with, and the
#: page says which it is.
Z80 = 1.2816

#: The open month's projection joins the trend only when it is at least this
#: sure of itself. A projection from three days is noise, and fitting a line to
#: noise would carry the noise into three more months.
_CARRY_OPEN_MONTH = ("medium", "high")
_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}

UNATTRIBUTED = "Unattributed"


# ---------------------------------------------------------------------------
# Reads, on the reconciled basis
# ---------------------------------------------------------------------------
def _inference_by_feature(conn, start: dt.date, end: dt.date) -> dict:
    """Inference spend per feature_id (None = Unattributed) for a month window."""
    rows = conn.execute(
        f"""
        SELECT feature_id, COALESCE(SUM(amount), 0) FROM inference_cost
        WHERE period >= %s AND period <= %s
          AND {dashboard._ACTIVE_ENV} {dashboard._NOT_DOUBLE_COUNTED}
        GROUP BY feature_id
        """,  # noqa: S608
        (start, end, dashboard._connector_providers(conn, start, end)),
    ).fetchall()
    return {(str(r[0]) if r[0] else None): Decimal(str(r[1])) for r in rows}


def _monthly_totals(conn, table: str, start: dt.date, end: dt.date) -> dict:
    """Spend per month for `inference_cost` or `build_cost` across a window."""
    if table == "inference_cost":
        sql = f"""
            SELECT period, COALESCE(SUM(amount), 0) FROM inference_cost
            WHERE period >= %s AND period <= %s
              AND {dashboard._ACTIVE_ENV} {dashboard._NOT_DOUBLE_COUNTED}
            GROUP BY period
        """  # noqa: S608
        params = (start, end, dashboard._connector_providers(conn, start, end))
    else:
        # Build cost has no provider/connector overlap and no environment: it is
        # seat and tool spend, recorded once per month.
        sql = """
            SELECT period, COALESCE(SUM(amount), 0) FROM build_cost
            WHERE period >= %s AND period <= %s GROUP BY period
        """
        params = (start, end)
    return {r[0]: Decimal(str(r[1])) for r in conn.execute(sql, params).fetchall()}


def _open_month_streams(conn, month: dt.date, through: dt.date) -> dict:
    """This month's spend as separate streams, one per (feature, provider).

    Projected per provider rather than as one org-wide sum, because the
    providers are not projected the same way and summing first hides that. An
    earlier version subtracted the org's daily rows from its monthly total and
    carried the remainder forward — and a provider whose daily rows ran AHEAD of
    its monthly figure quietly cancelled out another provider that reported no
    daily rows at all. A per-provider error is not visible inside a sum.

    Each stream is one of:

    * ``daily`` — the provider reports day-resolution spend (a connector);
      projected from those days with the run rate.
    * ``prorata`` — no daily rows (the SDK writes monthly only); the month so far
      is carried forward at its own flat average.
    * ``fixed`` — a self-hosted pool's allocation, which is the pool's WHOLE
      monthly cost split by usage (compute.allocate). It is already the month;
      spreading it over the rest of the month would count it about one and a
      half times. Reported as actual, the same way build cost is.

    Monthly figures are read on the reconciled basis and are what the stream's
    actual IS — the provider's cost API is authoritative on dollars (invariant 5).
    Daily rows only ever supply the shape of the month, never its level.
    """
    providers = dashboard._connector_providers(conn, month, month)
    monthly = conn.execute(
        f"""
        SELECT feature_id, provider, bool_or(source = 'self_host'),
               COALESCE(SUM(amount), 0)
        FROM inference_cost
        WHERE period = %s AND {dashboard._ACTIVE_ENV} {dashboard._NOT_DOUBLE_COUNTED}
        GROUP BY feature_id, provider
        """,  # noqa: S608
        (month, providers),
    ).fetchall()
    # inference_cost_daily is written by connectors only — the SDK reports
    # monthly — so there is no hook/connector overlap to remove, but ignored
    # spend still has to go.
    daily = conn.execute(
        f"""
        SELECT feature_id, provider, day, COALESCE(SUM(amount), 0)
        FROM inference_cost_daily
        WHERE day >= %s AND day <= %s AND {dashboard._ACTIVE_ENV}
        GROUP BY feature_id, provider, day ORDER BY day
        """,  # noqa: S608
        (month, through),
    ).fetchall()

    by_day: dict = {}
    for fid, provider, day, amount in daily:
        key = (str(fid) if fid else None, provider)
        by_day.setdefault(key, []).append((day, Decimal(str(amount))))

    streams: dict = {}
    for fid, provider, is_pool, amount in monthly:
        key = (str(fid) if fid else None, provider)
        series = by_day.get(key, [])
        kind = "fixed" if is_pool else ("daily" if series else "prorata")
        streams[key] = {"actual": Decimal(str(amount)), "daily": series, "kind": kind}
    # Daily rows with no monthly row yet (a sync that has written days but not
    # the month) still describe real spend: their own total is the actual.
    for key, series in by_day.items():
        if key not in streams:
            streams[key] = {
                "actual": sum((a for _, a in series), Decimal("0")),
                "daily": series,
                "kind": "daily",
            }
    return streams


# ---------------------------------------------------------------------------
# The open month
# ---------------------------------------------------------------------------
#: How each stream kind reads, in the order methods are listed when mixed.
_METHOD_ORDER = (
    "recent_weighted",
    "month_to_date_average",
    "month_to_date_prorata",
    "monthly_allocation",
)


def _stdev(values: list[Decimal]) -> Optional[float]:
    """Sample standard deviation, or None when two points are too few to say."""
    if len(values) < 2:
        return None
    mean = sum(values, Decimal("0")) / Decimal(len(values))
    var = sum(((v - mean) ** 2 for v in values), Decimal("0")) / Decimal(len(values) - 1)
    return math.sqrt(float(var))


def project_streams(streams: list[dict], observed_days: int, days_in_month: int) -> dict:
    """Where a set of spend streams lands at month end, and how sure that is.

    Pure arithmetic, so the org-wide figure and every per-feature driver come
    out of one function and cannot be computed two ways. See
    `_open_month_streams` for what the three stream kinds are and why each is
    projected differently.
    """
    remaining = max(days_in_month - observed_days, 0)
    actual = sum((s["actual"] for s in streams), Decimal("0"))
    if observed_days <= 0 or not streams or (actual <= 0 and not any(s["daily"] for s in streams)):
        return {
            "actual": actual,
            "projected": None,
            "low": None,
            "high": None,
            "method": "none",
            "confidence": "none",
            "observed_days": max(observed_days, 0),
        }

    projected = Decimal("0")
    methods: set = set()
    daily_share = Decimal("0")
    # Only the spend that is actually being PROJECTED can make a projection
    # unsure. A self-hosted allocation is the whole month, known exactly, and
    # counting it against the daily-observed share made a month with a large
    # pool look less certain than it is.
    projectable = Decimal("0")
    daily_method = None
    per_day: dict = {}
    for s in streams:
        if s["kind"] == "fixed":
            projected += s["actual"]
            methods.add("monthly_allocation")
        elif s["kind"] == "daily":
            rate, m = budgets._run_rate(s["daily"], observed_days)
            projected += s["actual"] + rate * Decimal(remaining)
            methods.add(m)
            daily_method = daily_method or m
            daily_share += s["actual"]
            projectable += s["actual"]
            for day, amount in s["daily"]:
                per_day[day] = per_day.get(day, Decimal("0")) + amount
        else:  # prorata
            projected += s["actual"] * Decimal(days_in_month) / Decimal(observed_days)
            methods.add("month_to_date_prorata")
            projectable += s["actual"]

    # Confidence follows the part of the month that was actually observed day
    # by day. Where most of the spend was carried forward from a monthly total,
    # there is no daily shape behind the projection, and it says so.
    if daily_method is None:
        confidence = "low" if "month_to_date_prorata" in methods else "high"
    else:
        confidence = budgets._confidence(observed_days, days_in_month, daily_method)
        if projectable > 0 and daily_share / projectable < Decimal("0.5"):
            confidence = "low"
    if methods == {"monthly_allocation"}:
        # Nothing but fixed allocations: the month is already known, not
        # projected. Certain, and labelled as what it is.
        confidence = "high"

    # The range comes from how much DAILY spend moves around. Without daily rows
    # there is nothing to measure that from, and a range invented from nothing
    # would be worse than none — the page says why it is missing.
    sigma = _stdev([a for _, a in sorted(per_day.items())])
    low = high = None
    if sigma is not None and remaining > 0:
        band = Decimal(str(Z80 * sigma * math.sqrt(remaining)))
        low, high = max(projected - band, actual), projected + band
    elif remaining == 0 or methods == {"monthly_allocation"}:
        low = high = projected

    return {
        "actual": actual,
        "projected": projected,
        "low": low,
        "high": high,
        "method": "+".join(m for m in _METHOD_ORDER if m in methods),
        "confidence": confidence,
        "observed_days": observed_days,
    }


def open_month(conn, as_of: dt.date) -> dict:
    """The open month's inference projection, org-wide and per feature."""
    current = month_start(as_of)
    in_month = calendar.monthrange(current.year, current.month)[1]
    through = min(as_of, next_month(current) - dt.timedelta(days=1))
    observed = (through - current).days + 1

    streams = _open_month_streams(conn, current, through)
    by_feature: dict = {}
    for (fid, _provider), stream in streams.items():
        by_feature.setdefault(fid, []).append(stream)

    return {
        "month": current,
        "days_in_month": in_month,
        "observed_days": observed,
        "org": project_streams(list(streams.values()), observed, in_month),
        "by_feature": {
            fid: project_streams(group, observed, in_month) for fid, group in by_feature.items()
        },
    }


# ---------------------------------------------------------------------------
# The next months
# ---------------------------------------------------------------------------
def _fit(points: list[tuple[int, Decimal]]) -> tuple[float, float, float]:
    """Least-squares line through (x, y): intercept, slope, residual std dev."""
    n = len(points)
    xs = [float(x) for x, _ in points]
    ys = [float(y) for _, y in points]
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 0.0
    intercept = my - slope * mx
    # n - 2 degrees of freedom: two were spent on the line itself.
    sse = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    sigma = math.sqrt(sse / (n - 2)) if n > 2 else 0.0
    return intercept, slope, sigma


def _months_back(month: dt.date, n: int) -> dt.date:
    for _ in range(n):
        month = month_start(month - dt.timedelta(days=1))
    return month


def project_months(
    history: dict,
    open_month_start: dt.date,
    open_projection: Optional[Decimal] = None,
    horizon: int = HORIZON_MONTHS,
) -> dict:
    """The next `horizon` months after the open one, from monthly history.

    `history` maps full months (before the open month) to spend. Months before
    the first one with any spend are left out: a tenant that joined four months
    ago has not spent nothing for the two months before that, it did not exist,
    and fitting a line through those zeros would invent a growth rate.

    `open_projection`, when given, joins the fit as the latest point, so a month
    running hot carries into the next one instead of being ignored.
    """
    full = [open_month_start]
    for _ in range(TREND_MAX_MONTHS):
        full.insert(0, _months_back(full[0], 1))
    full = full[:-1]  # the open month itself is not a full month
    spent = [m for m in full if history.get(m, Decimal("0")) > 0]
    if spent:
        full = [m for m in full if m >= spent[0]]
    else:
        full = []

    months_out = []
    m = open_month_start
    for _ in range(horizon):
        m = next_month(m)
        months_out.append(m)

    if len(full) < TREND_MIN_MONTHS:
        return {
            "status": "insufficient",
            "history_months": len(full),
            "method": "linear_trend",
            "carried_open_month": False,
            "months": [
                {"month": mo, "projected": None, "low": None, "high": None} for mo in months_out
            ],
        }

    points = [(i, history.get(mo, Decimal("0"))) for i, mo in enumerate(full)]
    carried = open_projection is not None
    if carried:
        points.append((len(full), open_projection))
    intercept, slope, sigma = _fit(points)

    base = len(full)  # x of the open month
    out = []
    for h, mo in enumerate(months_out, start=1):
        y = max(intercept + slope * (base + h), 0.0)
        # Further out, less sure: the spread grows with the square root of the
        # distance, the way a random walk's does.
        band = Z80 * sigma * math.sqrt(h)
        out.append(
            {
                "month": mo,
                "projected": Decimal(str(y)),
                "low": Decimal(str(max(y - band, 0.0))),
                "high": Decimal(str(y + band)),
            }
        )
    return {
        "status": "ok",
        "history_months": len(full),
        "method": "linear_trend",
        "carried_open_month": carried,
        "months": out,
    }


# ---------------------------------------------------------------------------
# The whole forecast
# ---------------------------------------------------------------------------
def _m(value) -> Optional[float]:
    return None if value is None else budgets._money(value)


def _pct(total: Optional[Decimal], budget: Optional[Decimal]) -> Optional[float]:
    if total is None or budget is None or budget <= 0:
        return None
    return round(float(total / budget * Decimal("100")), 1)


def _budget_for(budget: Optional[dict], month: dt.date) -> Optional[Decimal]:
    if budget is None:
        return None
    return Decimal(str(budgets.prorate(budget, month, month)["amount"]))


def forecast(tenant_id: str) -> dict:
    """Where spend lands this month and the next three, and what is driving it."""
    # Local import: optimize_measured reads the dashboard and pricing modules,
    # and the savings figure is the only thing needed from it here.
    from . import optimize_measured

    as_of, as_of_is_fixed = budgets.as_of_date(tenant_id)
    budget = budgets.get_budget(tenant_id)
    current = month_start(as_of)
    first_history = _months_back(current, TREND_MAX_MONTHS)
    last_full = _months_back(current, 1)

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        om = open_month(conn, as_of)
        inf_history = _monthly_totals(conn, "inference_cost", first_history, last_full)
        build_history = _monthly_totals(conn, "build_cost", first_history, last_full)
        build_now = _monthly_totals(conn, "build_cost", current, current).get(
            current, Decimal("0")
        )
        prior_by = _inference_by_feature(conn, last_full, last_full)
        names = {
            str(r[0]): r[1] for r in conn.execute("SELECT id, name FROM feature").fetchall()
        }
    # The tenant row is read the way budgets reads it, by id on the admin
    # connection: it is not a tenant-scoped table.
    with connect(admin_dsn()) as conn:
        currency = conn.execute(
            "SELECT currency FROM tenant WHERE id = %s", (tenant_id,)
        ).fetchone()

    org = om["org"]
    carry = org["projected"] if org["confidence"] in _CARRY_OPEN_MONTH else None
    inf_months = project_months(inf_history, current, carry)
    # Build has no day resolution, so the open month's build figure is an actual
    # that may or may not be complete. It does not join the build trend.
    build_months = project_months(build_history, current, None)

    open_budget = _budget_for(budget, current)
    open_total = None if org["projected"] is None else org["projected"] + build_now

    months = []
    for inf, bld in zip(inf_months["months"], build_months["months"]):
        mb = _budget_for(budget, inf["month"])
        total = (
            None
            if inf["projected"] is None or bld["projected"] is None
            else inf["projected"] + bld["projected"]
        )
        months.append(
            {
                "month": inf["month"].isoformat(),
                "inference": {k: _m(inf[k]) for k in ("projected", "low", "high")},
                "build": {k: _m(bld[k]) for k in ("projected", "low", "high")},
                # A total, labelled as one, because a budget covers both.
                "total_projected": _m(total),
                "budget": _m(mb),
                "projected_budget_pct": _pct(total, mb),
            }
        )

    # Savings Meter has already identified, per feature, from the same numbers
    # the Optimize screens show. One call for all of them. A slow or failing
    # Optimize must not take the forecast down with it.
    try:
        overview = optimize_measured.copilot_overview(tenant_id, current)
        savings = {
            # Tested included: passing a test must not remove a saving from here.
            f["feature_id"]: f["measured"] + f["tested"] + f["modeled_ceiling"]
            for f in overview["by_feature"]
        }
    except Exception:  # noqa: BLE001
        savings = {}

    drivers = []
    for fid in set(om["by_feature"]) | set(prior_by):
        proj = om["by_feature"].get(fid)
        projected = proj["projected"] if proj else None
        prior = prior_by.get(fid, Decimal("0"))
        if projected is None and prior == 0:
            continue
        change = None if projected is None else projected - prior
        drivers.append(
            {
                "feature_id": fid,
                # Never dropped, never merged into "other": invariant 4.
                "name": names.get(fid, UNATTRIBUTED) if fid else UNATTRIBUTED,
                "prior_month": _m(prior),
                "projected": _m(projected),
                "confidence": proj["confidence"] if proj else "none",
                "change": _m(change),
                "change_pct": (
                    None
                    if change is None or prior <= 0
                    else round(float(change / prior * Decimal("100")), 1)
                ),
                "identified_savings": _m(savings.get(fid)) if fid else None,
            }
        )
    # Biggest projected INCREASE first: that is the one to look at.
    drivers.sort(
        key=lambda d: (d["change"] is None, -(d["change"] or 0), d["name"].lower())
    )

    return {
        "as_of": as_of.isoformat(),
        "as_of_is_fixed": as_of_is_fixed,
        "currency": (currency[0] if currency else None) or "USD",
        "has_budget": budget is not None,
        "open_month": {
            "month": current.isoformat(),
            "days_in_month": om["days_in_month"],
            "observed_days": om["observed_days"],
            "inference": {
                "actual": _m(org["actual"]),
                "projected": _m(org["projected"]),
                "low": _m(org["low"]),
                "high": _m(org["high"]),
                "method": org["method"],
                "confidence": org["confidence"],
            },
            # Billed per month with no day resolution: an actual, never projected.
            "build": {"actual": _m(build_now)},
            "total_projected": _m(open_total),
            "budget": _m(open_budget),
            "projected_budget_pct": _pct(open_total, open_budget),
        },
        "horizon": {
            "status": inf_months["status"],
            "history_months": inf_months["history_months"],
            "carried_open_month": inf_months["carried_open_month"],
            "build_status": build_months["status"],
        },
        # The full months behind the trend, so a chart can draw what happened
        # before the line it projects — reconciled the same way, split the same
        # way, oldest first.
        "history": [
            {
                "month": mo.isoformat(),
                "inference": _m(inf_history.get(mo, Decimal("0"))),
                "build": _m(build_history.get(mo, Decimal("0"))),
            }
            for mo in _history_months(current)
        ],
        "months": months,
        "drivers": drivers,
    }


def _history_months(current: dt.date) -> list:
    """The TREND_MAX_MONTHS full months before the open one, oldest first."""
    out = [_months_back(current, 1)]
    for _ in range(TREND_MAX_MONTHS - 1):
        out.insert(0, _months_back(out[0], 1))
    return out
