"""Feature editing — the wizard's Review-step actions.

Rename, add manually, delete, split (one proposal is really two), merge (two are
really one), and confirm (proposed -> confirmed). All operations are tenant-scoped
through the app role, so RLS guarantees a tenant only ever touches its own features.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Optional

import psycopg

from . import dashboard, discovery, products
from .db import app_dsn, connect, tenant_tx
from .providers import month_start


class UsageImportError(Exception):
    """A usage CSV that cannot be read, named by the row that broke it."""


class FeatureNotFound(Exception):
    """Raised when a feature id does not exist for the tenant."""


# Signals a user can attach by hand to drive cost attribution (design §7.1).
# `usage_tag` is a cloud cost-allocation tag VALUE (e.g. the "triage" in
# feature=triage). It was already a valid signal_type in the schema but could
# not be attached by hand; the infrastructure connector attributes by it, and a
# cloud tag is not an API key, so it should not have to pretend to be one.
MANUAL_SIGNAL_TYPES = {"api_key", "service", "repo", "branch", "usage_tag"}


def _signals(conn: psycopg.Connection, feature_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, signal_type, external_ref, confidence, title, branch, url
        FROM feature_signal WHERE feature_id = %s
        ORDER BY signal_type, external_ref
        """,
        (feature_id,),
    ).fetchall()
    return [
        {
            "id": str(r[0]),
            "signal_type": r[1],
            "external_ref": r[2],
            "confidence": r[3],
            "title": r[4],
            "branch": r[5],
            "url": r[6],
        }
        for r in rows
    ]


def _feature(conn: psycopg.Connection, feature_id: str) -> Optional[dict]:
    row = conn.execute(
        """
        SELECT f.id, f.name, f.description, f.status, f.discovery_confidence,
               f.category, f.category_source, f.product_id, p.name, f.product_source
        FROM feature f LEFT JOIN product p ON p.id = f.product_id
        WHERE f.id = %s
        """,
        (feature_id,),
    ).fetchone()
    if row is None:
        return None
    # Resolved the same way the Overview resolves it, so the two never disagree.
    category, category_source = dashboard.resolve_category(row[5], row[6])
    return {
        "id": str(row[0]),
        "name": row[1],
        "description": row[2],
        "status": row[3],
        "discovery_confidence": row[4],
        # Feature type (chat/api/ui/...), or None when nobody has tagged it.
        "category": category,
        "category_source": category_source,
        # The product this feature belongs to — the customer's own grouping, one
        # level above the feature. None means Unassigned.
        "product_id": str(row[7]) if row[7] else None,
        "product_name": row[8],
        "product_source": row[9],
        "signals": _signals(conn, str(row[0])),
    }


def list_features(tenant_id: str, status: Optional[str] = None) -> list[dict]:
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if status:
            rows = conn.execute(
                "SELECT id FROM feature WHERE status = %s ORDER BY created_at", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT id FROM feature ORDER BY created_at").fetchall()
        return [_feature(conn, str(r[0])) for r in rows]


def add_feature(tenant_id: str, name: str, description: str = "") -> dict:
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        feature_id = conn.execute(
            """
            INSERT INTO feature (tenant_id, name, description, status)
            VALUES (%s, %s, %s, 'proposed')
            RETURNING id
            """,
            (tenant_id, name, description),
        ).fetchone()[0]
        return _feature(conn, str(feature_id))


def rename_feature(
    tenant_id: str,
    feature_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        existing = _feature(conn, feature_id)
        if existing is None:
            raise FeatureNotFound(feature_id)
        conn.execute(
            "UPDATE feature SET name = %s, description = %s WHERE id = %s",
            (
                name if name is not None else existing["name"],
                description if description is not None else existing["description"],
                feature_id,
            ),
        )
        return _feature(conn, feature_id)


def set_product(tenant_id: str, feature_id: str, product_id: Optional[str]) -> dict:
    """Assign a feature to one of the customer's products.

    Passing None clears the assignment, handing the feature back to the repo
    mapping. An assignment made here is never overwritten by a later discovery
    run or by re-applying the mapping — see products.reassign_from_repos.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if _feature(conn, feature_id) is None:
            raise FeatureNotFound(feature_id)
        if product_id is not None:
            exists = conn.execute("SELECT 1 FROM product WHERE id = %s", (product_id,)).fetchone()
            if exists is None:
                raise products.ProductNotFound(product_id)
        conn.execute(
            "UPDATE feature SET product_id = %s, product_source = %s WHERE id = %s",
            (product_id, "user" if product_id else None, feature_id),
        )
        return _feature(conn, feature_id)


def set_category(tenant_id: str, feature_id: str, category: Optional[str]) -> dict:
    """Tag a feature with the kind of thing it is (its Type).

    Passing None clears the tag, handing the feature back to the discovery guess.
    A tag set here is never overwritten by a later discovery run — see
    discovery._persist_proposals.
    """
    if category is not None and category not in discovery.CATEGORIES:
        raise ValueError(f"category must be one of {discovery.CATEGORIES} or null")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if _feature(conn, feature_id) is None:
            raise FeatureNotFound(feature_id)
        conn.execute(
            "UPDATE feature SET category = %s, category_source = %s WHERE id = %s",
            (category, "user" if category else None, feature_id),
        )
        return _feature(conn, feature_id)


def delete_feature(tenant_id: str, feature_id: str) -> None:
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        deleted = conn.execute(
            "DELETE FROM feature WHERE id = %s RETURNING id", (feature_id,)
        ).fetchone()
        if deleted is None:
            raise FeatureNotFound(feature_id)


def split_feature(tenant_id: str, feature_id: str, groups: list[dict]) -> list[dict]:
    """Split one feature into several. Each group: {name, signal_ids:[...]}.

    Signals listed in a group move to a new feature; the original is removed
    (any signals not reassigned go with it).
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        original = _feature(conn, feature_id)
        if original is None:
            raise FeatureNotFound(feature_id)

        new_features: list[dict] = []
        for group in groups:
            new_id = conn.execute(
                """
                INSERT INTO feature (tenant_id, name, description, status,
                                     discovery_confidence, product_id, product_source)
                VALUES (%s, %s, %s, 'proposed', %s, %s, %s)
                RETURNING id
                """,
                (
                    tenant_id,
                    group["name"],
                    group.get("description", f"Split from {original['name']}."),
                    original["discovery_confidence"],
                    # Both halves stay in the product the original belonged to;
                    # without this, splitting silently unassigns a feature.
                    original["product_id"],
                    original["product_source"],
                ),
            ).fetchone()[0]
            signal_ids = group.get("signal_ids", [])
            if signal_ids:
                conn.execute(
                    "UPDATE feature_signal SET feature_id = %s "
                    "WHERE feature_id = %s AND id = ANY(%s)",
                    (new_id, feature_id, list(signal_ids)),
                )
            new_features.append(_feature(conn, str(new_id)))

        conn.execute("DELETE FROM feature WHERE id = %s", (feature_id,))
        return new_features


def merge_features(tenant_id: str, feature_ids: list[str], name: Optional[str] = None) -> dict:
    """Merge several features into the first. Their signals move to the target."""
    if len(feature_ids) < 2:
        raise ValueError("Merging needs at least two features.")
    target, *rest = feature_ids
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if _feature(conn, target) is None:
            raise FeatureNotFound(target)
        conn.execute(
            "UPDATE feature_signal SET feature_id = %s WHERE feature_id = ANY(%s)",
            (target, list(rest)),
        )
        conn.execute("DELETE FROM feature WHERE id = ANY(%s)", (list(rest),))
        if name is not None:
            conn.execute("UPDATE feature SET name = %s WHERE id = %s", (name, target))
        return _feature(conn, target)


def add_signal(
    tenant_id: str,
    feature_id: str,
    signal_type: str,
    external_ref: str,
    confidence: Optional[str] = None,
) -> dict:
    """Attach an evidence signal (e.g. an api_key -> feature mapping for ingest)."""
    if signal_type not in MANUAL_SIGNAL_TYPES:
        raise ValueError(f"Unsupported signal type: {signal_type}")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if _feature(conn, feature_id) is None:
            raise FeatureNotFound(feature_id)
        conn.execute(
            """
            INSERT INTO feature_signal (tenant_id, feature_id, signal_type, external_ref,
                                        confidence, source)
            VALUES (%s, %s, %s, %s, %s, 'manual')
            """,
            (tenant_id, feature_id, signal_type, external_ref, confidence),
        )
        return _feature(conn, feature_id)


def set_usage(
    tenant_id: str,
    feature_id: str,
    active_users: int,
    events: Optional[int] = None,
    period: Optional[dt.date] = None,
) -> dict:
    """Set a feature's usage for a month (manual/CSV input; design §9.3)."""
    start = month_start(period or dt.date.today())
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if _feature(conn, feature_id) is None:
            raise FeatureNotFound(feature_id)
        conn.execute(
            "DELETE FROM feature_usage WHERE feature_id = %s AND period = %s", (feature_id, start)
        )
        conn.execute(
            """
            INSERT INTO feature_usage (tenant_id, feature_id, period, active_users, events, source)
            VALUES (%s, %s, %s, %s, %s, 'manual')
            """,
            (tenant_id, feature_id, start, active_users, events),
        )
        return _feature(conn, feature_id)


def parse_usage_csv(text: str, default_period: Optional[dt.date] = None) -> list[dict]:
    """Parse a per-feature usage export.

        feature,active_users[,events][,period]

    `feature` is a feature name or its id; names are matched case-insensitively
    because the person exporting from an analytics tool is typing what they see
    on the Features screen, not a uuid. Header names are matched the same
    flexible way the build-cost import matches them.

    Rows are validated here and resolved to features by the caller, so a bad
    file is rejected whole rather than half-applied.
    """
    reader = csv.DictReader(io.StringIO(text.strip()))
    if reader.fieldnames is None:
        raise UsageImportError("CSV has no header row.")
    fields = {name.strip().lower(): name for name in reader.fieldnames}

    def pick(row, *names):
        for n in names:
            if n in fields and row[fields[n]] not in (None, ""):
                return row[fields[n]].strip()
        return None

    def count(raw: str, label: str, i: int) -> int:
        try:
            value = int(raw.replace(",", ""))
        except ValueError as exc:
            raise UsageImportError(f"Row {i}: invalid {label} '{raw}'.") from exc
        if value < 0:
            raise UsageImportError(f"Row {i}: {label} cannot be negative, got '{raw}'.")
        return value

    rows: list[dict] = []
    for i, row in enumerate(reader, start=2):  # row 1 is the header
        if not any((value or "").strip() for value in row.values()):
            continue  # a trailing blank line is not an error
        ref = pick(row, "feature", "feature_name", "name", "feature_id", "id")
        if not ref:
            raise UsageImportError(f"Row {i}: missing feature.")
        users_raw = pick(row, "active_users", "users", "active", "mau")
        if users_raw is None:
            raise UsageImportError(f"Row {i}: missing active_users.")
        events_raw = pick(row, "events", "event_count", "requests")
        period_raw = pick(row, "period", "month")
        if period_raw:
            try:
                period = dt.date.fromisoformat(f"{period_raw[:7]}-01")
            except ValueError as exc:
                raise UsageImportError(
                    f"Row {i}: invalid period '{period_raw}', expected YYYY-MM."
                ) from exc
        else:
            period = month_start(default_period or dt.date.today())
        rows.append(
            {
                "feature": ref,
                "active_users": count(users_raw, "active_users", i),
                "events": count(events_raw, "events", i) if events_raw is not None else None,
                "period": period,
                "row": i,
            }
        )
    if not rows:
        raise UsageImportError("CSV has no rows.")
    return rows


def import_usage(tenant_id: str, text: str, period: Optional[dt.date] = None) -> dict:
    """Load a month of per-feature usage from CSV.

    All or nothing: every row is resolved to a feature before anything is
    written, so a typo in the last row does not leave half a month loaded and
    the other half stale. A name that matches no feature is an error rather than
    a skip — silently dropping a row would show a blank column that the person
    believes they just filled.
    """
    rows = parse_usage_csv(text, default_period=period)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        by_name: dict = {}
        by_id: dict = {}
        for fid, name in conn.execute("SELECT id, name FROM feature").fetchall():
            by_id[str(fid)] = str(fid)
            # Two features sharing a name is legal; a name that is ambiguous
            # cannot be used to address one of them, so it addresses neither.
            key = name.strip().lower()
            by_name[key] = None if key in by_name else str(fid)

        resolved = []
        for entry in rows:
            ref = entry["feature"]
            feature_id = by_id.get(ref)
            if feature_id is None:
                key = ref.strip().lower()
                if key in by_name and by_name[key] is None:
                    raise UsageImportError(
                        f"Row {entry['row']}: more than one feature is called '{ref}'; "
                        "use the feature id instead."
                    )
                feature_id = by_name.get(key)
            if feature_id is None:
                raise UsageImportError(f"Row {entry['row']}: no feature called '{ref}'.")
            resolved.append((feature_id, entry))

        periods = set()
        for feature_id, entry in resolved:
            conn.execute(
                "DELETE FROM feature_usage WHERE feature_id = %s AND period = %s",
                (feature_id, entry["period"]),
            )
            conn.execute(
                """
                INSERT INTO feature_usage
                    (tenant_id, feature_id, period, active_users, events, source)
                VALUES (%s, %s, %s, %s, %s, 'csv')
                """,
                (tenant_id, feature_id, entry["period"], entry["active_users"], entry["events"]),
            )
            periods.add(entry["period"])

    return {
        "imported": len(resolved),
        "features": len({fid for fid, _ in resolved}),
        "periods": sorted(p.isoformat() for p in periods),
    }


def confirm_features(tenant_id: str, feature_ids: Optional[list[str]] = None) -> list[dict]:
    """Confirm proposed features (all, or the given ids). Returns the confirmed set."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if feature_ids:
            conn.execute(
                "UPDATE feature SET status = 'confirmed' "
                "WHERE status = 'proposed' AND id = ANY(%s)",
                (list(feature_ids),),
            )
        else:
            conn.execute("UPDATE feature SET status = 'confirmed' WHERE status = 'proposed'")
        rows = conn.execute(
            "SELECT id FROM feature WHERE status = 'confirmed' ORDER BY created_at"
        ).fetchall()
        return [_feature(conn, str(r[0])) for r in rows]
