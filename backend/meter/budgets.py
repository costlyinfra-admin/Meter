"""The organization's AI budget: storage, proration, and the period forecast.

Three things live here, in that order:

1. **CRUD** over the single ``org_budget`` row an organization may have. There is
   no budget until someone sets one, and no default is invented on read — a
   missing budget is a real answer the UI is expected to show.

2. **Proration.** A budget is a monthly or annual figure; a reporting window is
   an arbitrary run of months. `applicable_budget` converts one to the other by
   calendar days, so a half-month window gets half a month's budget and a leap
   year is 366 days rather than 365. It returns how it got there, because a
   number nobody can explain is not much use to a CFO.

3. **Forecast.** `period_forecast` projects an open month from the daily spend
   already observed, and refuses to project a month that is over. Completed
   periods report their final spend and say so.

On "today": every date decision here goes through `as_of_date`, which reads the
organization's timezone, and — for the seeded demo tenant only — a fixed date
stored on the tenant row. A real organization has NULL there and gets the clock.
There is no code path where a missing budget or a failed forecast falls back to
an invented number.
"""

from __future__ import annotations

import calendar
import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import admin_dsn, app_dsn, connect, tenant_tx
from .providers import month_start, next_month

CADENCES = ("monthly", "annual")

#: Days of month-to-date history below which a weighted run rate is more noise
#: than signal, so the flat month-to-date average is used instead.
MIN_DAYS_FOR_WEIGHTED = 7

#: How many trailing days the weighted run rate leans on.
RECENT_WINDOW_DAYS = 7

#: Weight given to the recent window against the rest of the month. 0.7 says
#: "the last week is most of the story, but not all of it".
RECENT_WEIGHT = Decimal("0.7")


class BudgetError(ValueError):
    """Invalid budget input (maps to HTTP 400)."""


def _money(value) -> float:
    """Round to cents for transport. Decimal in, float out, once, at the edge."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# "Today", in the organization's terms
# ---------------------------------------------------------------------------
def as_of_date(tenant_id: str) -> tuple[dt.date, bool]:
    """The date to treat as today for this organization, and whether it is fixed.

    Real organizations get the current date in their configured timezone — a
    London evening and a Los Angeles afternoon are different days, and which one
    it is decides whether this month is still open.

    The seeded demo tenant gets ``tenant.demo_as_of``. Its dataset is historical,
    so the clock would call every month complete and the forecast would have
    nothing to say. That column is NULL for every real organization, so there is
    no path by which production picks up a demo date.
    """
    with connect(admin_dsn()) as conn:
        row = conn.execute(
            "SELECT timezone, demo_as_of FROM tenant WHERE id = %s", (tenant_id,)
        ).fetchone()
    if row is None:
        raise BudgetError("Organization not found.")
    tz_name, demo = row[0] or "UTC", row[1]
    if demo is not None:
        return demo, True
    try:
        zone = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        # A zone the host has no tzdata for should not take the page down; UTC is
        # at most a day out, and the settings list is a closed set of real zones.
        zone = ZoneInfo("UTC")
    return dt.datetime.now(zone).date(), False


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def get_budget(tenant_id: str) -> Optional[dict]:
    """The organization's budget, or None when it has not set one."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            """
            SELECT amount, cadence, currency, effective_from, updated_at, updated_by
            FROM org_budget WHERE tenant_id = %s
            """,
            (tenant_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "amount": _money(row[0]),
        "cadence": row[1],
        "currency": row[2],
        "effective_from": row[3].isoformat(),
        "updated_at": row[4].isoformat() if row[4] else None,
        "updated_by": row[5],
    }


def _validate(payload: dict, tenant_currency: str) -> dict:
    amount_raw = payload.get("amount")
    try:
        amount = Decimal(str(amount_raw))
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise BudgetError("Budget amount must be a number.") from exc
    if not amount.is_finite() or amount <= 0:
        raise BudgetError("Budget amount must be greater than 0.")
    if amount > Decimal("1e12"):
        raise BudgetError("Budget amount looks too large.")

    cadence = payload.get("cadence")
    if cadence not in CADENCES:
        raise BudgetError(f"Budget cadence must be one of {', '.join(CADENCES)}.")

    # The org's reporting currency is the only one its numbers are in, so a budget
    # in anything else could not be compared to spend.
    currency = (payload.get("currency") or tenant_currency or "USD").strip().upper()
    if currency != (tenant_currency or "USD").upper():
        raise BudgetError(
            f"Budget currency must match the organization's reporting currency "
            f"({tenant_currency}). Change the reporting currency first."
        )

    raw_from = payload.get("effective_from")
    if isinstance(raw_from, dt.date):
        effective_from = raw_from
    else:
        try:
            effective_from = dt.date.fromisoformat(str(raw_from))
        except (TypeError, ValueError) as exc:
            raise BudgetError("Effective start date must be a date (YYYY-MM-DD).") from exc

    return {
        "amount": amount,
        "cadence": cadence,
        "currency": currency,
        "effective_from": effective_from,
    }


def set_budget(tenant_id: str, payload: dict, *, actor: Optional[str] = None) -> dict:
    """Create or replace the organization's budget. Returns the stored row."""
    with connect(admin_dsn()) as conn:
        row = conn.execute("SELECT currency FROM tenant WHERE id = %s", (tenant_id,)).fetchone()
    if row is None:
        raise BudgetError("Organization not found.")
    clean = _validate(payload, row[0])

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute(
            """
            INSERT INTO org_budget (tenant_id, amount, cadence, currency, effective_from,
                                    updated_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id) DO UPDATE SET
                amount = EXCLUDED.amount,
                cadence = EXCLUDED.cadence,
                currency = EXCLUDED.currency,
                effective_from = EXCLUDED.effective_from,
                updated_by = EXCLUDED.updated_by,
                updated_at = now()
            """,
            (
                tenant_id,
                clean["amount"],
                clean["cadence"],
                clean["currency"],
                clean["effective_from"],
                actor,
            ),
        )
    return get_budget(tenant_id)  # type: ignore[return-value]


def remove_budget(tenant_id: str) -> bool:
    """Delete the organization's budget. True when there was one to delete."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        cur = conn.execute("DELETE FROM org_budget WHERE tenant_id = %s", (tenant_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Proration
# ---------------------------------------------------------------------------
def _days_in_year(year: int) -> int:
    return 366 if calendar.isleap(year) else 365


def window_days(start: dt.date, end_month: dt.date) -> tuple[dt.date, dt.date]:
    """The inclusive calendar-day span of a window given as first-of-month dates."""
    last_day = next_month(month_start(end_month)) - dt.timedelta(days=1)
    return month_start(start), last_day


def prorate(
    budget: dict,
    start: dt.date,
    end_month: dt.date,
) -> dict:
    """The share of ``budget`` that applies to the window, and how it was worked out.

    Monthly budgets are prorated per calendar month: a month the window covers in
    full contributes the whole amount, a partial month contributes the fraction of
    its own days that are covered. Annual budgets are prorated per calendar year
    against that year's own length, so a leap year divides by 366.

    Days before the budget's effective date contribute nothing — a budget set in
    March does not retroactively cover January.
    """
    win_start, win_end = window_days(start, end_month)
    effective = dt.date.fromisoformat(budget["effective_from"])
    covered_start = max(win_start, effective)

    if covered_start > win_end:
        return {
            "amount": 0.0,
            "method": budget["cadence"],
            "covered_days": 0,
            "window_days": (win_end - win_start).days + 1,
            "covered_start": None,
            "covered_end": None,
            "fully_covered": False,
        }

    amount = Decimal(str(budget["amount"]))
    total = Decimal("0")
    covered_days = 0

    if budget["cadence"] == "monthly":
        month = month_start(win_start)
        while month <= month_start(win_end):
            m_start, m_end = month, next_month(month) - dt.timedelta(days=1)
            lo, hi = max(m_start, covered_start), min(m_end, win_end)
            if lo <= hi:
                days = (hi - lo).days + 1
                in_month = calendar.monthrange(month.year, month.month)[1]
                total += amount * Decimal(days) / Decimal(in_month)
                covered_days += days
            month = next_month(month)
    else:
        year = covered_start.year
        while year <= win_end.year:
            y_start, y_end = dt.date(year, 1, 1), dt.date(year, 12, 31)
            lo, hi = max(y_start, covered_start), min(y_end, win_end)
            if lo <= hi:
                days = (hi - lo).days + 1
                total += amount * Decimal(days) / Decimal(_days_in_year(year))
                covered_days += days
            year += 1

    window_len = (win_end - win_start).days + 1
    return {
        "amount": _money(total),
        "method": budget["cadence"],
        "covered_days": covered_days,
        "window_days": window_len,
        "covered_start": covered_start.isoformat(),
        "covered_end": win_end.isoformat(),
        "fully_covered": covered_days == window_len,
    }


# ---------------------------------------------------------------------------
# Forecast
# ---------------------------------------------------------------------------
def _daily_spend(conn, month: dt.date, through: dt.date) -> list[tuple[dt.date, Decimal]]:
    """Observed inference spend per day for `month`, up to and including `through`.

    Build cost has no day resolution — it is billed and recorded per month — so
    only inference is projected. The month's build cost is added back whole.
    """
    rows = conn.execute(
        """
        SELECT day, COALESCE(SUM(amount), 0)
        FROM inference_cost_daily
        WHERE day >= %s AND day <= %s
        GROUP BY day ORDER BY day
        """,
        (month, through),
    ).fetchall()
    return [(r[0], Decimal(str(r[1]))) for r in rows]


def _run_rate(daily: list[tuple[dt.date, Decimal]], elapsed_days: int) -> tuple[Decimal, str]:
    """Spend per day for the rest of the month, and which method produced it.

    With a decent run of days, the recent week carries most of the weight —
    spend usually trends rather than oscillating, and a rollout three weeks ago
    should not still dominate the projection. With less than that, the flat
    month-to-date average is the honest answer.
    """
    if elapsed_days <= 0 or not daily:
        return Decimal("0"), "none"
    total = sum((amount for _, amount in daily), Decimal("0"))
    average = total / Decimal(elapsed_days)
    if elapsed_days < MIN_DAYS_FOR_WEIGHTED:
        return average, "month_to_date_average"
    recent = daily[-RECENT_WINDOW_DAYS:]
    recent_avg = sum((amount for _, amount in recent), Decimal("0")) / Decimal(len(recent))
    blended = recent_avg * RECENT_WEIGHT + average * (Decimal("1") - RECENT_WEIGHT)
    return blended, "recent_weighted"


def _confidence(elapsed: int, in_month: int, method: str) -> str:
    """How much to trust the projection. Days observed is the whole story."""
    if method == "none":
        return "none"
    share = elapsed / in_month if in_month else 0
    if elapsed >= MIN_DAYS_FOR_WEIGHTED and share >= 0.5:
        return "high"
    if elapsed >= 3:
        return "medium"
    return "low"


def period_forecast(
    tenant_id: str,
    start: dt.date,
    end_month: dt.date,
    *,
    identified_savings: Optional[float] = None,
) -> dict:
    """Where the reporting window lands: actual, budget, forecast and variance.

    ``status`` is the field to read first:

    ``closed``       the window ended before today. There is nothing to forecast;
                     ``forecast`` is the final spend and is equal to ``actual``.
    ``open``         the window includes the current month and there is enough
                     daily spend to project it.
    ``insufficient`` the window is open but the open month has no observed daily
                     spend to project from. ``forecast`` is null.

    ``budget`` is null when the organization has not set one. Nothing here
    invents a budget, and no branch substitutes a default.
    """
    as_of, as_of_is_fixed = as_of_date(tenant_id)
    start, end_month = month_start(start), month_start(end_month)
    win_start, win_end = window_days(start, end_month)
    current_month = month_start(as_of)

    budget = get_budget(tenant_id)
    proration = prorate(budget, start, end_month) if budget else None

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        # Actual spend for the window, split as it always is.
        row = conn.execute(
            """
            SELECT COALESCE((SELECT SUM(amount) FROM build_cost
                             WHERE period >= %s AND period <= %s), 0),
                   COALESCE((SELECT SUM(amount) FROM inference_cost
                             WHERE period >= %s AND period <= %s), 0)
            """,
            (start, end_month, start, end_month),
        ).fetchone()
        build_actual, inference_actual = Decimal(str(row[0])), Decimal(str(row[1]))
        actual = build_actual + inference_actual

        closed = win_end < as_of
        method, confidence, forecast = "closed", "final", actual
        observed_days = 0

        if not closed:
            # Only the current month is open; anything after it has not started,
            # and projecting an unstarted month from nothing would be invention.
            in_month = calendar.monthrange(current_month.year, current_month.month)[1]
            through = min(as_of, next_month(current_month) - dt.timedelta(days=1))
            daily = _daily_spend(conn, current_month, through)
            observed_days = (through - current_month).days + 1
            rate, method = _run_rate(daily, observed_days)
            confidence = _confidence(observed_days, in_month, method)
            if method == "none":
                forecast = None
            else:
                remaining = max(in_month - observed_days, 0)
                forecast = actual + rate * Decimal(remaining)

    optimized = None
    if forecast is not None and identified_savings is not None:
        optimized = max(forecast - Decimal(str(identified_savings)), Decimal("0"))

    budget_amount = Decimal(str(proration["amount"])) if proration else None
    variance = variance_pct = None
    if budget_amount is not None and forecast is not None and budget_amount > 0:
        variance = forecast - budget_amount
        variance_pct = float(variance / budget_amount * Decimal("100"))

    if closed:
        status = "closed"
    elif forecast is None:
        status = "insufficient"
    else:
        status = "open"

    return {
        "status": status,
        "as_of": as_of.isoformat(),
        "as_of_is_fixed": as_of_is_fixed,
        "window_start": win_start.isoformat(),
        "window_end": win_end.isoformat(),
        "actual": _money(actual),
        "actual_build": _money(build_actual),
        "actual_inference": _money(inference_actual),
        "budget": None if budget_amount is None else _money(budget_amount),
        "budget_detail": proration,
        "budget_cadence": budget["cadence"] if budget else None,
        "currency": budget["currency"] if budget else None,
        "forecast": None if forecast is None else _money(forecast),
        "forecast_optimized": None if optimized is None else _money(optimized),
        "identified_savings": None if identified_savings is None else _money(identified_savings),
        "variance": None if variance is None else _money(variance),
        "variance_pct": None if variance_pct is None else round(variance_pct, 2),
        "method": method,
        "confidence": confidence,
        "observed_days": observed_days,
    }
