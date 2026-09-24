"""The three read-only tools, and the argument handling in front of them.

Every handler is the same shape: validate arguments, call one or more of Meter's
existing read services, and project the result down to what fits usefully in a
model's context. The projection is the only work done here — no aggregation, no
pricing, no detection. If a number in a response is wrong, it is wrong on a Meter
screen too, which is the property worth having.

Responses are trimmed on purpose. `dashboard.dashboard()` returns trends,
insights and open actions that a screen renders and an agent would only pay for;
the tools return the money, the shape of it, and the labels that stop a number
being misread — the savings taxonomy, the confidence, and the invariant that
build cost and inference cost are never added together.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from .. import ai_reads, dashboard, optimize_measured

#: Named review windows, matching the app's own vocabulary (api.py `_RANGE_RE`).
RANGES = ("this_month", "last_month", "last_3_months", "last_6_months", "last_12_months")

#: What `get_cost_summary` can slice by, and the existing service behind each.
#: There is no generic filter engine on the cost side — Meter's read layer is
#: dimensional — so this exposes the dimensions that exist rather than inventing
#: a query language over them.
GROUPINGS = ("feature", "provider", "model", "workspace", "product", "customer", "application")

#: The savings taxonomy, in the order a reader should trust it. Repeated in the
#: tool description because an agent acting on "modeled_ceiling" as if it were
#: "measured" would write a change nobody asked for.
SAVINGS_TYPES = ("measured", "modeled_ceiling", "directional")

#: Page ceilings. An agent asking for everything is an agent filling its context
#: with rows it will not read.
DEFAULT_LIMIT = 20
MAX_LIMIT = 100
#: Example runs returned with an opportunity's evidence.
DEFAULT_TRACES = 10
MAX_TRACES = 50


class ToolError(Exception):
    """A tool could not answer. Reported to the caller as a tool result, not a
    protocol error: the model should read it and try different arguments."""


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
def _month(value: Any, field: str) -> Optional[dt.date]:
    """`YYYY-MM` to the first of that month."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError(f"{field} must be a string like '2026-06'.")
    try:
        year, month = value.split("-")
        return dt.date(int(year), int(month), 1)
    except (ValueError, TypeError) as exc:
        raise ToolError(f"{field} must look like '2026-06', got {value!r}.") from exc


def _enum(value: Any, allowed: tuple, field: str, default: Optional[str] = None) -> Optional[str]:
    if value is None:
        return default
    if value not in allowed:
        raise ToolError(f"{field} must be one of {', '.join(allowed)}; got {value!r}.")
    return value


def _limit(value: Any, default: int, ceiling: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ToolError(f"limit must be a positive whole number, at most {ceiling}.")
    return min(value, ceiling)


def _window(args: dict) -> tuple:
    """(start, end, range_token) as the read services expect them."""
    start = _month(args.get("start"), "start")
    end = _month(args.get("end"), "end")
    range_token = _enum(args.get("range"), RANGES, "range")
    return start, end, range_token


def _text(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{field} must be a non-empty string.")
    return value.strip()


# ---------------------------------------------------------------------------
# Opportunity addressing
# ---------------------------------------------------------------------------
# Opportunities are computed on read, not stored, so there is no row id to hand
# out. The address is what actually identifies one: which feature, which lever,
# which month it was computed for. Encoding it means `get_optimization_details`
# recomputes exactly the finding the caller was shown, instead of "whatever that
# lever says now" — and it needs no table.
_ID_PARTS = 3


def opportunity_id(feature_id: str, lever: str, period: str) -> str:
    return f"{feature_id}:{lever}:{period}"


def parse_opportunity_id(value: Any) -> tuple:
    """(feature_id, lever, period) — or raise with the format spelled out."""
    raw = _text(value, "opportunity_id")
    parts = raw.split(":") if raw else []
    if len(parts) != _ID_PARTS or not all(parts):
        raise ToolError(
            "opportunity_id must look like '<feature_id>:<lever>:<YYYY-MM-DD>', "
            "exactly as find_optimization_opportunities returned it."
        )
    feature_id, lever, period = parts
    try:
        return feature_id, lever, dt.date.fromisoformat(period)
    except ValueError as exc:
        raise ToolError(f"The period in opportunity_id is not a date: {period!r}.") from exc


# ---------------------------------------------------------------------------
# get_cost_summary
# ---------------------------------------------------------------------------
_NEVER_BLENDED = (
    "build_cost (what it cost to make) and inference_cost (what it costs to run) "
    "are reported separately and must never be added together."
)


def _by_feature(tenant_id: str, window: tuple, limit: int) -> dict:
    data = dashboard.dashboard(tenant_id, *window)
    rows = sorted(
        data["features"],
        key=lambda r: r["inference_cost"] + r["build_cost"],
        reverse=True,
    )
    return {
        "group_by": "feature",
        "totals": data["totals"],
        "unattributed": data["unattributed"],
        "feature_count": len(rows),
        "features": [
            {
                "feature_id": r["feature_id"],
                "name": r["name"],
                "build_cost": r["build_cost"],
                "inference_cost": r["inference_cost"],
                "requests": r["requests"],
                "active_users": r["active_users"],
                "product_name": r["product_name"],
                "category": r["category"],
                "confidence": r["confidence"],
            }
            for r in rows[:limit]
        ],
        "data_updated_at": data["data_updated_at"],
    }


def _by_provider(tenant_id: str, window: tuple, limit: int) -> dict:
    data = dashboard.spend_by_provider(tenant_id, *window)
    return {
        "group_by": "provider",
        "inference_total": data["total"],
        # What the money was spent ON: input, output, cache read, cache write.
        # Each row carries dollars AND the provider's own token count, because
        # the service's `token_total` is a total of DOLLARS — returning that
        # under a name starting "token" invited exactly the wrong reading, and
        # it is the same figure as inference_total anyway.
        "spend_by_token_type": data["by_token_type"][:limit],
        "providers": [
            {
                "provider": p["provider"],
                "amount": p["amount"],
                "pct": p["pct"],
                "requests": p["requests"],
                "models": p["by_model"][:limit],
            }
            for p in data["by_provider"][:limit]
        ],
    }


def _by_model(tenant_id: str, window: tuple, limit: int) -> dict:
    data = dashboard.spend_by_provider(tenant_id, *window)
    models = [
        {"provider": p["provider"], "model": m["model"], "amount": m["amount"]}
        for p in data["by_provider"]
        for m in p["by_model"]
    ]
    models.sort(key=lambda m: m["amount"], reverse=True)
    return {
        "group_by": "model",
        "inference_total": data["total"],
        "models": models[:limit],
    }


def _by_workspace(tenant_id: str, window: tuple, limit: int) -> dict:
    data = dashboard.spend_by_provider(tenant_id, *window)
    return {
        "group_by": "workspace",
        # Workspace attribution covers only the providers that report it, so this
        # total is a subset of inference_total and saying so prevents the two
        # being read as a discrepancy.
        "workspace_total": data["workspace_total"],
        "inference_total": data["total"],
        "workspaces": [
            {
                "workspace": w["workspace"],
                "amount": w["amount"],
                "pct": w["pct"],
                "tokens": w["tokens"],
                "api_keys": [
                    {"api_key": k["api_key"], "amount": k["amount"]} for k in w["by_key"][:limit]
                ],
            }
            for w in data["by_workspace"][:limit]
        ],
    }


def _by_product(tenant_id: str, window: tuple, limit: int) -> dict:
    data = dashboard.spend_by_product(tenant_id, *window)
    return {
        "group_by": "product",
        "totals": data["totals"],
        "unassigned": data["unassigned"],
        "unattributed": data["unattributed"],
        "products": [
            {
                "product_id": p["product_id"],
                "name": p["name"],
                "build_cost": p["build_cost"],
                "inference_cost": p["inference_cost"],
                "feature_count": p["feature_count"],
            }
            for p in data["products"][:limit]
        ],
    }


def _by_customer(tenant_id: str, window: tuple, limit: int) -> dict:
    data = dashboard.spend_by_customer(tenant_id, *window)
    return {
        "group_by": "customer",
        # Customer attribution exists only where the SDK tagged a call, so this
        # is a labelled subset of the bill, never a second version of it.
        "metered_total": data["total"],
        "inference_total": data["inference_total"],
        "coverage_pct": data["coverage_pct"],
        "customers": data["customers"][:limit],
    }


def _by_application(tenant_id: str, window: tuple, limit: int) -> dict:
    start, end = dashboard.resolve_window(tenant_id, *window)
    since, until = _as_datetimes(start, end)
    rows = ai_reads.list_applications(tenant_id, since, until)
    return {
        "group_by": "application",
        # Applications come from request-level telemetry, so this covers only
        # instrumented services — not the whole bill.
        "applications": [
            {
                "id": a["id"],
                "name": a["name"],
                "slug": a["slug"],
                "runs": a["runs"],
                "spend": a["spend"],
                "tokens": a["tokens"],
                "cost_per_run": a["cost_per_run"],
                "error_rate": a["error_rate"],
                "features": a["features"],
            }
            for a in sorted(rows, key=lambda a: a["spend"], reverse=True)[:limit]
        ],
    }


def _as_datetimes(start: dt.date, end: dt.date) -> tuple:
    """A month range as the half-open UTC instant range the trace side uses."""
    since = dt.datetime.combine(start, dt.time.min, tzinfo=dt.timezone.utc)
    after_end = (end.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
    until = dt.datetime.combine(after_end, dt.time.min, tzinfo=dt.timezone.utc)
    return since, min(until, dt.datetime.now(dt.timezone.utc))


_GROUPERS = {
    "feature": _by_feature,
    "provider": _by_provider,
    "model": _by_model,
    "workspace": _by_workspace,
    "product": _by_product,
    "customer": _by_customer,
    "application": _by_application,
}


def get_cost_summary(tenant_id: str, args: dict) -> dict:
    window = _window(args)
    limit = _limit(args.get("limit"), DEFAULT_LIMIT, MAX_LIMIT)
    feature_id = _text(args.get("feature_id"), "feature_id")
    start, end = dashboard.resolve_window(tenant_id, *window)
    head = {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "currency": "USD",
        "note": _NEVER_BLENDED,
    }

    if feature_id:
        detail = dashboard.feature_detail(tenant_id, feature_id, *window)
        if detail is None:
            raise ToolError(f"No feature with id {feature_id} in this organization.")
        models = dashboard.feature_inference(tenant_id, feature_id, *window)
        return {
            **head,
            "group_by": "feature",
            "feature": {
                "feature_id": detail["feature_id"],
                "name": detail["name"],
                "description": detail["description"],
                "category": detail["category"],
                "product_name": detail["product_name"],
                "build_cost": detail["headline"]["build_cost"],
                "inference_cost": detail["headline"]["inference_cost"],
                "active_users": detail["headline"]["active_users"],
                "avg_latency_ms": detail["headline"]["avg_latency_ms"],
                "inference_sources": detail["inference_sources"],
                "by_model": models["by_model"][:limit],
            },
        }

    group_by = _enum(args.get("group_by"), GROUPINGS, "group_by", "feature")
    return {**head, **_GROUPERS[group_by](tenant_id, window, limit)}


# ---------------------------------------------------------------------------
# find_optimization_opportunities
# ---------------------------------------------------------------------------
def _opportunity(entry: dict, period: str) -> dict:
    """One finding, as an agent needs to read it: what, where, how much, how sure."""
    return {
        "opportunity_id": opportunity_id(entry["feature_id"], entry["lever"], period),
        "lever": entry["lever"],
        "title": entry["title"],
        "feature_id": entry["feature_id"],
        "feature_name": entry["feature_name"],
        "savings_type": entry["savings_type"],
        "projected_monthly_savings": entry["projected_monthly_savings"],
        "projected_annual_savings": entry["projected_annual_savings"],
        "confidence": entry["confidence"],
        "confidence_reason": entry["confidence_reason"],
        "engineering_effort": entry["engineering_effort"],
        "priority_score": entry["priority_score"],
        "status": entry["status"],
        # Set when a better-evidenced finding already prices the same tokens.
        # An agent must not act on both, or it will double-count the saving.
        "overlaps": entry["overlaps"],
        "evidence": entry["evidence"],
        "fix": entry["fix"],
        "source": entry["source"],
    }


def _feature_opportunities(tenant_id: str, feature_id: str, period) -> dict:
    """Every opportunity on one feature — not just the ranked shortlist."""
    result = optimize_measured.opportunities(tenant_id, feature_id, period, period)
    if result is None:
        raise ToolError(f"No feature with id {feature_id} in this organization.")
    detail = dashboard.feature_detail(tenant_id, feature_id, period, period)
    name = detail["name"] if detail else None
    return {
        "period": result["period"],
        "totals": result["totals"],
        "opportunities": [
            {**o, "feature_id": feature_id, "feature_name": name}
            for o in result["opportunities"]
        ],
        "actions": result["actions"],
    }


def find_optimization_opportunities(tenant_id: str, args: dict) -> dict:
    period = _month(args.get("period"), "period")
    feature_id = _text(args.get("feature_id"), "feature_id")
    limit = _limit(args.get("limit"), DEFAULT_LIMIT, MAX_LIMIT)
    wanted = _enum(args.get("savings_type"), SAVINGS_TYPES, "savings_type")
    minimum = args.get("min_monthly_savings")
    if minimum is not None and not isinstance(minimum, (int, float)):
        raise ToolError("min_monthly_savings must be a number.")

    if feature_id:
        # One feature: the complete list, including directional estimates and
        # findings superseded by a better-evidenced one.
        source = _feature_opportunities(tenant_id, feature_id, period)
        scope = f"feature:{feature_id}"
        by_lever, applied, telemetry = [], source["actions"], None
    else:
        # Tenant-wide: Meter's own ranked shortlist, which is what the Optimize
        # screen shows. It is a shortlist, not the whole list — pass feature_id
        # to see everything on one feature.
        overview = optimize_measured.copilot_overview(tenant_id, period)
        source = {
            "period": overview["period"],
            "totals": overview["totals"],
            "verified_monthly_savings": overview["verified_monthly_savings"],
            "opportunities": overview["top_recommendations"],
        }
        scope = "organization"
        by_lever, applied = overview["by_lever"], overview["applied"]
        telemetry = {
            "has_sdk_telemetry": overview["has_sdk_telemetry"],
            "has_optimize_signals": overview["has_optimize_signals"],
            "has_billing_data": overview["has_billing_data"],
        }

    found = [_opportunity(o, source["period"]) for o in source["opportunities"]]
    if wanted:
        found = [o for o in found if o["savings_type"] == wanted]
    if minimum is not None:
        found = [o for o in found if o["projected_monthly_savings"] >= minimum]

    result = {
        "scope": scope,
        "period": source["period"],
        "currency": "USD",
        # Three totals, never one. `measured` is what the traffic guarantees,
        # `modeled_ceiling` is an upper bound whose realization depends on an
        # assumption Meter cannot verify, and `directional` is a rule of thumb.
        "totals_by_savings_type": source["totals"],
        "opportunities": found[:limit],
        "by_lever": by_lever,
        "applied": applied,
        "note": (
            "savings_type says how much to trust a number: measured is counted from "
            "traffic; modeled_ceiling is an upper bound that assumes every candidate "
            "was safely avoidable; directional is a heuristic. An opportunity with "
            "`overlaps` set is already priced by another one — never add the two. "
            "Call get_optimization_details with an opportunity_id before changing code."
        ),
    }
    if telemetry is not None:
        # Organization scope only. `verified_monthly_savings` is a roll-up across
        # every feature and `telemetry` describes the tenant, so neither is
        # reported — as a number or as a null — on a single feature's view.
        result["verified_monthly_savings"] = source["verified_monthly_savings"]
        result["telemetry"] = telemetry
    return result


# ---------------------------------------------------------------------------
# get_optimization_details
# ---------------------------------------------------------------------------
def _trace_page(tenant_id: str, feature_id: str, since, until, limit: int) -> list:
    """The most expensive runs of one feature in a window, projected down.

    `operation_name` is the step name the SDK was given at the call site, which
    is the bridge from a finding to the code that caused it. Deliberately
    trimmed: investigating a repeated request needs the operation, the model and
    the cost, not who the run was for.
    """
    page = ai_reads.list_traces(
        tenant_id,
        {
            "feature_id": feature_id,
            "since": since,
            "until": until,
            "sort": "expensive",
            "limit": limit,
            "offset": 0,
        },
    )
    return [
        {
            "trace_id": t["trace_id"],
            "operation_name": t["operation_name"],
            "application": t["application"]["slug"],
            "status": t["status"],
            "total_cost": t["total_cost"],
            "total_tokens": t["total_tokens"],
            "llm_calls": t["llm_calls"],
            "duration_ms": t["duration_ms"],
            "started_at": t["started_at"],
        }
        for t in page["traces"]
    ]


def _example_traces(tenant_id: str, feature_id: str, period: dt.date, limit: int) -> dict:
    """Example runs behind a finding, and which window they came from.

    Findings are monthly and permanent; traces are request-level and expire on
    the tenant's retention window. So an opportunity from an older month often
    has none of its own runs left. Rather than return an empty list — which
    reads as "this feature never ran" — fall back to that feature's most recent
    runs, which still show the call path, and say which window they came from.
    A reader must never mistake last week's runs for evidence about May.
    """
    since, until = _as_datetimes(period, period)
    traces = _trace_page(tenant_id, feature_id, since, until, limit)
    if traces:
        return {"basis": "period", "period": period.isoformat(), "traces": traces}
    return {
        "basis": "recent",
        "note": (
            "No runs from this period are still within the trace retention window. "
            "These are the feature's most recent runs — the same call paths, but "
            "not evidence for this month's numbers."
        ),
        "traces": _trace_page(tenant_id, feature_id, None, None, limit),
    }


def get_optimization_details(tenant_id: str, args: dict) -> dict:
    feature_id, lever, period = parse_opportunity_id(args.get("opportunity_id"))
    traces = _limit(args.get("trace_limit"), DEFAULT_TRACES, MAX_TRACES)

    # Recomputed for the period the id names, so the caller gets the finding they
    # were shown rather than whatever that lever says this month.
    result = optimize_measured.opportunities(tenant_id, feature_id, period, period)
    if result is None:
        raise ToolError(f"No feature with id {feature_id} in this organization.")
    found = next((o for o in result["opportunities"] if o["lever"] == lever), None)
    if found is None:
        raise ToolError(
            f"No '{lever}' opportunity for that feature in {period.isoformat()[:7]}. "
            "It may have been resolved, or the period may be wrong."
        )

    detail = dashboard.feature_detail(tenant_id, feature_id, period, period)
    models = dashboard.feature_inference(tenant_id, feature_id, period, period)
    action = next((a for a in result["actions"] if a["lever"] == lever), None)
    return {
        "opportunity_id": opportunity_id(feature_id, lever, result["period"]),
        "period": result["period"],
        "currency": "USD",
        "opportunity": found,
        "feature": {
            "feature_id": feature_id,
            "name": detail["name"] if detail else None,
            "description": detail["description"] if detail else None,
            "inference_cost": detail["headline"]["inference_cost"] if detail else None,
            "by_model": models["by_model"],
        },
        "cache_utilization": result["cache_utilization"],
        "applied_action": action,
        "example_traces": _example_traces(tenant_id, feature_id, period, traces),
        "note": (
            "opportunity.trail lists the request shapes behind the finding: a "
            "salted one-way fingerprint and a repeat count per shape, never any "
            "prompt, response or tool content — Meter stores none. Use "
            "example_traces' operation_name to locate the call site in the "
            "codebase, then follow validation_guidance before changing anything."
        ),
    }


# ---------------------------------------------------------------------------
# The MCP tool surface
# ---------------------------------------------------------------------------
_WINDOW_SCHEMA = {
    "range": {
        "type": "string",
        "enum": list(RANGES),
        "description": "Named review window, relative to the latest month with data.",
    },
    "start": {"type": "string", "description": "First month, as YYYY-MM. Wins over range."},
    "end": {"type": "string", "description": "Last month, as YYYY-MM. Defaults to start."},
}

TOOLS = [
    {
        "name": "get_cost_summary",
        "description": (
            "Cost, token and request metrics for an organization's AI spend over a "
            "month range. Build cost (AI coding tools used to make a feature) and "
            "inference cost (model calls the feature makes in production) are always "
            "returned separately and must never be summed. Slice with group_by, or "
            "pass feature_id for one feature's detail."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                **_WINDOW_SCHEMA,
                "group_by": {
                    "type": "string",
                    "enum": list(GROUPINGS),
                    "description": (
                        "Dimension to slice by. Default feature. workspace and model "
                        "come from provider billing; application comes from SDK "
                        "telemetry and covers instrumented services only."
                    ),
                },
                "feature_id": {
                    "type": "string",
                    "description": "Return one feature's detail instead of a slice.",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Rows per group, at most {MAX_LIMIT}.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "find_optimization_opportunities",
        "description": (
            "Optimization opportunities Meter has already detected. With no "
            "feature_id this is the organization-wide shortlist Meter ranks by "
            "priority (savings x confidence x engineering effort) — the same one "
            "the Optimize screen shows. Pass feature_id for every opportunity on "
            "one feature. Each carries a savings_type — measured, modeled_ceiling "
            "or directional — and an opportunity_id for get_optimization_details."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "description": "Month to analyse, as YYYY-MM."},
                "feature_id": {
                    "type": "string",
                    "description": (
                        "Every opportunity on this feature, rather than the "
                        "organization-wide shortlist."
                    ),
                },
                "savings_type": {
                    "type": "string",
                    "enum": list(SAVINGS_TYPES),
                    "description": "Only opportunities of this evidence class.",
                },
                "min_monthly_savings": {
                    "type": "number",
                    "description": "Drop opportunities projected to save less than this.",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        f"At most {MAX_LIMIT}. The organization-wide shortlist is "
                        "short by design; feature_id returns the full list."
                    ),
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_optimization_details",
        "description": (
            "The evidence behind one opportunity: how it was detected, the request "
            "shapes and counts behind it, the model split, the projected saving and "
            "how to validate it, plus the most expensive runs of that feature so the "
            "call site can be found in code. Returns no prompt or response content — "
            "Meter does not store any."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "opportunity_id": {
                    "type": "string",
                    "description": "As returned by find_optimization_opportunities.",
                },
                "trace_limit": {
                    "type": "integer",
                    "description": f"Example runs to include, at most {MAX_TRACES}.",
                },
            },
            "required": ["opportunity_id"],
            "additionalProperties": False,
        },
    },
]

_HANDLERS = {
    "get_cost_summary": get_cost_summary,
    "find_optimization_opportunities": find_optimization_opportunities,
    "get_optimization_details": get_optimization_details,
}


def call_tool(name: str, arguments: Optional[dict], tenant_id: str) -> dict:
    """Run one tool. Raises ToolError for anything the caller can fix."""
    handler = _HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"Unknown tool {name!r}. Available: {', '.join(_HANDLERS)}.")
    if arguments is not None and not isinstance(arguments, dict):
        raise ToolError("arguments must be an object.")
    return handler(tenant_id, arguments or {})
