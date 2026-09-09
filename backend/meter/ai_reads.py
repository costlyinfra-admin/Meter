"""Reading request-level evidence: applications, traces, spans.

Separate from `traces.py`, which writes. Ingest is a hot path with one shape;
this is a cold path with many, and keeping them apart means a new filter cannot
accidentally change what an event does to the books.

Two rules run through everything here:

**Staleness is computed, never stored.** A trace is stale when it is still
`running` and has been quiet longer than the tenant's threshold. That is a
function of the clock, so it is evaluated on read — which is why a trace can
stop being stale simply by sending a heartbeat, and why nothing about becoming
stale touches money.

**Nothing internal leaves.** These functions return display shapes, not rows.
Tenant ids, credentials, batch bookkeeping and internal primary keys either are
not selected or are not serialized. The trace id a caller sees is the one their
own SDK generated.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from .db import app_dsn, connect, tenant_tx

#: A page size a customer can ask for, and the ceiling they cannot exceed. An
#: unbounded page is an unbounded query, and this table is the largest in the
#: product by row count.
DEFAULT_PAGE = 50
MAX_PAGE = 200

#: Sorts the listing offers. A closed map, because these strings become ORDER BY
#: — a caller must never be able to put text into a query.
SORTS = {
    "newest": "t.started_at DESC",
    "expensive": "t.total_cost DESC",
    "tokens": "t.total_tokens DESC",
    "slowest": "t.duration_ms DESC NULLS LAST",
    "longest": "t.started_at ASC",
    "steps": "t.span_count DESC",
}

STATUSES = ("running", "success", "error", "cancelled")


def stale_threshold(conn) -> int:
    row = conn.execute("SELECT agent_stale_after_minutes FROM tenant LIMIT 1").fetchone()
    return int(row[0]) if row else 10


def _live_status(status: str, last_activity: Optional[dt.datetime], minutes: int, now) -> str:
    """The status a person should see, which is not always the one stored.

    `stale` is a presentation of `running`, not a replacement for it: the trace
    is still going as far as the database is concerned, and may still finish.
    """
    if status != "running" or last_activity is None:
        return status
    if (now - last_activity) > dt.timedelta(minutes=minutes):
        return "stale"
    return "running"


_TRACE_COLUMNS = """
    t.id, t.external_trace_id, t.operation_name, t.status, t.started_at, t.ended_at,
    t.duration_ms, t.last_activity_at, t.last_heartbeat_at, t.current_span_id,
    t.total_cost, t.total_tokens, t.span_count, t.environment, t.release_version,
    t.customer_ref, a.name AS application_name, a.slug AS application_slug,
    a.id AS application_id, f.name AS feature_name, t.feature_id,
    (SELECT count(*) FROM ai_span s
      WHERE s.trace_id = t.id AND s.span_kind = 'llm') AS llm_calls
"""


def _trace_row(row: dict, minutes: int, now) -> dict:
    return {
        "id": str(row["id"]),
        "trace_id": row["external_trace_id"],
        "operation_name": row["operation_name"],
        "status": row["status"],
        "live_status": _live_status(row["status"], row["last_activity_at"], minutes, now),
        "started_at": row["started_at"].isoformat(),
        "ended_at": row["ended_at"].isoformat() if row["ended_at"] else None,
        "duration_ms": row["duration_ms"],
        "last_activity_at": row["last_activity_at"].isoformat(),
        "last_heartbeat_at": (
            row["last_heartbeat_at"].isoformat() if row["last_heartbeat_at"] else None
        ),
        "current_span_id": row["current_span_id"],
        "total_cost": float(row["total_cost"]),
        "total_tokens": int(row["total_tokens"]),
        "span_count": int(row["span_count"]),
        "llm_calls": int(row["llm_calls"] or 0),
        "environment": row["environment"],
        "release_version": row["release_version"],
        "customer_ref": row["customer_ref"],
        "application": {
            "id": str(row["application_id"]),
            "name": row["application_name"],
            "slug": row["application_slug"],
        },
        "feature": (
            {"id": str(row["feature_id"]), "name": row["feature_name"]}
            if row["feature_id"]
            else None
        ),
    }


def _like(raw: str) -> str:
    """Escape LIKE's metacharacters so a search means what was typed.

    Without this, `_` quietly matches any character and `%` matches everything
    — so searching for a workflow called "sync_user" would also return
    "syncXuser", and a search for "100%" would return the whole table.
    """
    return str(raw).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_traces(tenant_id: str, filters: dict) -> dict:
    """A page of traces. Deterministic order, bounded size, parameterised filters.

    Every filter is a bound parameter. The only caller-controlled SQL is the
    sort, and that is looked up in `SORTS` rather than interpolated — a caller
    cannot reach the query text.
    """
    limit = min(int(filters.get("limit") or DEFAULT_PAGE), MAX_PAGE)
    offset = max(int(filters.get("offset") or 0), 0)
    sort = SORTS.get(filters.get("sort") or "newest", SORTS["newest"])

    where = ["1 = 1"]
    params: list = []

    def clause(sql: str, value) -> None:
        where.append(sql)
        params.append(value)

    if filters.get("application_id"):
        clause("t.application_id = %s", filters["application_id"])
    if filters.get("feature_id"):
        clause("t.feature_id = %s", filters["feature_id"])
    if filters.get("environment"):
        clause("t.environment = %s", filters["environment"])
    if filters.get("release_version"):
        clause("t.release_version = %s", filters["release_version"])
    if filters.get("status") in STATUSES:
        clause("t.status = %s", filters["status"])
    if filters.get("since"):
        clause("t.started_at >= %s", filters["since"])
    if filters.get("until"):
        clause("t.started_at < %s", filters["until"])
    if filters.get("min_cost") is not None:
        clause("t.total_cost >= %s", filters["min_cost"])
    if filters.get("q"):
        # Free-text over the WORKFLOW NAME only. Application and feature have
        # their own filters, and the count query above joins nothing, so
        # reaching into a joined table here would make the total disagree with
        # the page. The pattern is a bound parameter with LIKE's own
        # metacharacters escaped, so a search for "50%" means "50%".
        clause("t.operation_name ILIKE %s ESCAPE '\\'", f"%{_like(filters['q'])}%")
    # Provider, model and prompt live on spans; EXISTS keeps one row per trace
    # rather than multiplying it by its matching spans.
    for key, column in (
        ("provider", "provider"),
        ("model", "model"),
        ("prompt_id", "prompt_id"),
        ("prompt_version", "prompt_version"),
    ):
        if filters.get(key):
            where.append(
                f"EXISTS (SELECT 1 FROM ai_span s WHERE s.trace_id = t.id AND s.{column} = %s)"
            )
            params.append(filters[key])

    now = dt.datetime.now(dt.timezone.utc)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        minutes = stale_threshold(conn)
        # "running" and "stale" are views of the same stored status, so they are
        # expressed as an activity cutoff rather than a status filter.
        cutoff = now - dt.timedelta(minutes=minutes)
        if filters.get("running"):
            where.append("t.status = 'running' AND t.last_activity_at > %s")
            params.append(cutoff)
        elif filters.get("stale"):
            where.append("t.status = 'running' AND t.last_activity_at <= %s")
            params.append(cutoff)

        predicate = " AND ".join(where)
        total = conn.execute(
            f"SELECT count(*) FROM ai_trace t WHERE {predicate}",  # noqa: S608
            params,
        ).fetchone()[0]
        cursor = conn.execute(
            f"""
            SELECT {_TRACE_COLUMNS}
            FROM ai_trace t
            JOIN ai_application a ON a.id = t.application_id
            LEFT JOIN feature f ON f.id = t.feature_id
            WHERE {predicate}
            -- id breaks ties so paging is stable when timestamps collide.
            ORDER BY {sort}, t.id DESC
            LIMIT %s OFFSET %s
            """,  # noqa: S608
            [*params, limit, offset],
        )
        names = [d.name for d in cursor.description]
        rows = cursor.fetchall()

    traces = [_trace_row(dict(zip(names, r)), minutes, now) for r in rows]
    return {
        "traces": traces,
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "stale_after_minutes": minutes,
    }


def get_trace(tenant_id: str, trace_id: str) -> Optional[dict]:
    """One trace with its ordered spans, or None.

    Spans come back in start order with their parent ids intact, so the caller
    can build the tree without a recursive query — and an unresolved parent is
    left as-is rather than dropped, because a step whose parent event was lost
    is still a step that happened and still cost money.
    """
    now = dt.datetime.now(dt.timezone.utc)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        minutes = stale_threshold(conn)
        cursor = conn.execute(
            f"""
            SELECT {_TRACE_COLUMNS}
            FROM ai_trace t
            JOIN ai_application a ON a.id = t.application_id
            LEFT JOIN feature f ON f.id = t.feature_id
            WHERE t.id::text = %s OR t.external_trace_id = %s
            LIMIT 1
            """,  # noqa: S608
            (trace_id, trace_id),
        )
        names = [d.name for d in cursor.description]
        row = cursor.fetchone()
        if row is None:
            return None
        trace = _trace_row(dict(zip(names, row)), minutes, now)

        span_cursor = conn.execute(
            """
            SELECT external_span_id, parent_span_id, span_kind, operation_name, provider,
                   model, tokens_in, tokens_out, cache_read_tokens, cache_write_tokens,
                   reasoning_tokens, amount, latency_ms, status, prompt_id, prompt_version,
                   started_at, ended_at
            FROM ai_span WHERE trace_id = %s ORDER BY started_at, external_span_id
            """,
            (trace["id"],),
        )
        span_names = [d.name for d in span_cursor.description]
        spans = span_cursor.fetchall()

    known = {s[0] for s in spans}
    out = []
    for raw in spans:
        span = dict(zip(span_names, raw))
        span["amount"] = float(span["amount"])
        span["started_at"] = span["started_at"].isoformat()
        span["ended_at"] = span["ended_at"].isoformat() if span["ended_at"] else None
        # The UI renders an unresolved parent as a root. Saying so here keeps
        # that decision out of the component.
        span["parent_missing"] = bool(
            span["parent_span_id"] and span["parent_span_id"] not in known
        )
        out.append(span)
    trace["spans"] = out
    return trace


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
def _app_metrics(conn, window: tuple, minutes: int, now) -> dict:
    """Per-application economics for a window, plus the comparable prior one.

    One query over both windows rather than two round trips: the comparison is
    always shown, so fetching it separately would double the cost of the page
    for no benefit.
    """
    since, until = window
    span = until - since
    prior_since = since - span
    rows = conn.execute(
        """
        SELECT a.id, a.name, a.slug, a.description, a.owner,
               COUNT(t.id) FILTER (WHERE t.started_at >= %s AND t.started_at < %s) AS runs,
               COALESCE(SUM(t.total_cost)
                   FILTER (WHERE t.started_at >= %s AND t.started_at < %s), 0) AS spend,
               COALESCE(SUM(t.total_tokens)
                   FILTER (WHERE t.started_at >= %s AND t.started_at < %s), 0) AS tokens,
               COUNT(t.id) FILTER (WHERE t.started_at >= %s AND t.started_at < %s
                   AND t.status = 'error') AS errors,
               COUNT(DISTINCT t.feature_id) FILTER (WHERE t.feature_id IS NOT NULL) AS features,
               COUNT(t.id) FILTER (WHERE t.status = 'running'
                   AND t.last_activity_at > %s) AS active,
               COUNT(t.id) FILTER (WHERE t.status = 'running'
                   AND t.last_activity_at <= %s) AS stale,
               COALESCE(SUM(t.total_cost)
                   FILTER (WHERE t.started_at >= %s AND t.started_at < %s), 0) AS prior_spend,
               COUNT(t.id) FILTER (WHERE t.started_at >= %s AND t.started_at < %s) AS prior_runs
        FROM ai_application a
        LEFT JOIN ai_trace t ON t.application_id = a.id
        GROUP BY a.id, a.name, a.slug, a.description, a.owner
        ORDER BY spend DESC, a.name
        """,
        (
            since,
            until,
            since,
            until,
            since,
            until,
            since,
            until,
            now - dt.timedelta(minutes=minutes),
            now - dt.timedelta(minutes=minutes),
            prior_since,
            since,
            prior_since,
            since,
        ),
    ).fetchall()
    return rows


def list_applications(tenant_id: str, since: dt.datetime, until: dt.datetime) -> list[dict]:
    now = dt.datetime.now(dt.timezone.utc)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        minutes = stale_threshold(conn)
        rows = _app_metrics(conn, (since, until), minutes, now)

    out = []
    for (
        app_id,
        name,
        slug,
        description,
        owner,
        runs,
        spend,
        tokens,
        errors,
        features,
        active,
        stale,
        prior_spend,
        prior_runs,
    ) in rows:
        runs = int(runs)
        spend = float(spend)
        prior_spend = float(prior_spend)
        out.append(
            {
                "id": str(app_id),
                "name": name,
                "slug": slug,
                "description": description,
                "owner": owner,
                "runs": runs,
                "spend": spend,
                "tokens": int(tokens),
                "features": int(features),
                "active": int(active),
                "stale": int(stale),
                # Guarded: a window with no runs has no cost per run, and printing
                # 0 would read as "free" rather than "nothing happened".
                "cost_per_run": (spend / runs) if runs else None,
                "error_rate": (int(errors) / runs) if runs else None,
                "prior_spend": prior_spend,
                "prior_runs": int(prior_runs),
                # None, not 0%: "no change" and "nothing to compare against" are
                # different answers and the UI shows them differently.
                "spend_change": (((spend - prior_spend) / prior_spend) if prior_spend else None),
            }
        )
    return out


def get_application(
    tenant_id: str, app_id: str, since: dt.datetime, until: dt.datetime
) -> Optional[dict]:
    apps = [a for a in list_applications(tenant_id, since, until) if a["id"] == app_id]
    if not apps:
        return None
    app = apps[0]
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        app["by_feature"] = [
            {"feature": r[0] or "Unattributed", "spend": float(r[1]), "runs": int(r[2])}
            for r in conn.execute(
                """
                SELECT f.name, COALESCE(SUM(t.total_cost), 0), COUNT(*)
                FROM ai_trace t LEFT JOIN feature f ON f.id = t.feature_id
                WHERE t.application_id = %s AND t.started_at >= %s AND t.started_at < %s
                GROUP BY f.name ORDER BY 2 DESC LIMIT 50
                """,
                (app_id, since, until),
            )
        ]
        app["by_model"] = [
            {"provider": r[0], "model": r[1], "spend": float(r[2]), "calls": int(r[3])}
            for r in conn.execute(
                """
                SELECT s.provider, s.model, COALESCE(SUM(s.amount), 0), COUNT(*)
                FROM ai_span s JOIN ai_trace t ON t.id = s.trace_id
                WHERE t.application_id = %s AND s.span_kind = 'llm'
                  AND s.occurred_at >= %s AND s.occurred_at < %s
                GROUP BY s.provider, s.model ORDER BY 3 DESC LIMIT 50
                """,
                (app_id, since, until),
            )
        ]
        app["releases"] = [
            {"release": r[0], "runs": int(r[1]), "spend": float(r[2])}
            for r in conn.execute(
                """
                SELECT t.release_version, COUNT(*), COALESCE(SUM(t.total_cost), 0)
                FROM ai_trace t
                WHERE t.application_id = %s AND t.release_version IS NOT NULL
                  AND t.started_at >= %s AND t.started_at < %s
                GROUP BY t.release_version ORDER BY MAX(t.started_at) DESC LIMIT 10
                """,
                (app_id, since, until),
            )
        ]
    return app


def rename_application(tenant_id: str, app_id: str, changes: dict) -> Optional[dict]:
    """Rename or annotate. The SLUG is never touched — the SDK resolves by it,
    and changing it would silently orphan every event already in flight."""
    from . import applications

    sets, params = [], []
    if "name" in changes:
        sets.append("name = %s")
        params.append(applications.validate_name(changes["name"]))
    for field, limit in (("description", 500), ("owner", 200)):
        if field in changes:
            value = (changes[field] or "").strip() or None
            if value and len(value) > limit:
                raise applications.ApplicationError(f"{field} must be {limit} characters or fewer.")
            sets.append(f"{field} = %s")
            params.append(value)
    if not sets:
        raise applications.ApplicationError("Nothing to update.")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            f"UPDATE ai_application SET {', '.join(sets)}, updated_at = now() "  # noqa: S608
            "WHERE id = %s RETURNING id, name, slug, description, owner",
            [*params, app_id],
        ).fetchone()
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "name": row[1],
        "slug": row[2],
        "description": row[3],
        "owner": row[4],
    }


def create_application(tenant_id: str, name: str, slug: Optional[str] = None) -> dict:
    from . import applications

    display = applications.validate_name(name)
    normalized = applications.normalize_slug(slug or name)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        existing = conn.execute(
            "SELECT id FROM ai_application WHERE slug = %s", (normalized,)
        ).fetchone()
        if existing:
            raise applications.ApplicationError(
                f"An application with the slug '{normalized}' already exists."
            )
        count = conn.execute("SELECT count(*) FROM ai_application").fetchone()[0]
        if count >= applications.MAX_APPLICATIONS_PER_TENANT:
            raise applications.ApplicationError(
                f"This organization already has "
                f"{applications.MAX_APPLICATIONS_PER_TENANT} AI applications."
            )
        row = conn.execute(
            "INSERT INTO ai_application (tenant_id, name, slug) VALUES (%s, %s, %s) "
            "RETURNING id, name, slug",
            (tenant_id, display, normalized),
        ).fetchone()
    return {"id": str(row[0]), "name": row[1], "slug": row[2]}


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
#: Deleted per pass. Request-level evidence is the highest-volume table here, so
#: the sweep is chunked: a single unbounded DELETE would hold locks over a table
#: an ingest path is actively writing to.
RETENTION_CHUNK = 1000


def purge_expired_traces(tenant_id: str, now: Optional[dt.datetime] = None) -> dict:
    """Delete request-level evidence past the tenant's retention window.

    Financial aggregates are deliberately untouched. inference_cost,
    inference_cost_daily, customer_cost, reconciliation, optimization actions,
    alerts and budgets all survive: what a month cost is a fact about the month,
    not about how long we keep the traces that explain it. Spans cascade from
    ai_trace; nothing else does.

    Rerunnable, and it leaves running traces alone — a long-lived agent that
    started before the window must not be deleted out from under itself.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    deleted = 0
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        days = conn.execute("SELECT trace_retention_days FROM tenant LIMIT 1").fetchone()
        cutoff = now - dt.timedelta(days=int(days[0]) if days else 30)
        while True:
            removed = conn.execute(
                """
                DELETE FROM ai_trace WHERE id IN (
                    SELECT id FROM ai_trace
                    WHERE started_at < %s AND status <> 'running'
                    LIMIT %s
                ) RETURNING id
                """,
                (cutoff, RETENTION_CHUNK),
            ).fetchall()
            deleted += len(removed)
            if len(removed) < RETENTION_CHUNK:
                break
    return {"deleted": deleted, "cutoff": cutoff.isoformat()}


def purge_all_tenants(now: Optional[dt.datetime] = None) -> list[dict]:
    """Retention sweep across every tenant. Cron entry point."""
    from .db import admin_dsn

    with connect(admin_dsn()) as conn:
        tenants = [str(r[0]) for r in conn.execute("SELECT id FROM tenant")]
    out = []
    for tenant_id in tenants:
        result = purge_expired_traces(tenant_id, now)
        if result["deleted"]:
            out.append({"tenant_id": tenant_id, **result})
    return out


if __name__ == "__main__":
    swept = purge_all_tenants()
    print(f"Purged expired traces for {len(swept)} tenants.")
    for entry in swept:
        print(" ", entry)
