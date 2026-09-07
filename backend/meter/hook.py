"""Metering hook: ingest token, per-call event ingest, and bill reconciliation.

The hook is the precision tier (design §7.2). Metered events are costed from the
internal pricing tables and written to inference_cost with source='hook' at HIGH
confidence. Reconciliation compares the hook total to the provider's authoritative
cost-API total per period and records the gap in bill_reconciliation; the gap is
surfaced as Unattributed by the dashboard (it is never silently dropped).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import random
import secrets
from decimal import Decimal
from typing import Optional

from . import compute
from .db import admin_dsn, app_dsn, connect, tenant_tx
from .pricing import PRICED_PROVIDERS, price
from .providers import month_start

_DEFAULT_TOLERANCE = Decimal("0.50")


# --------------------------------------------------------------------------
# Ingest token (per tenant)
# --------------------------------------------------------------------------
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def generate_token(tenant_id: str) -> str:
    """Create (or replace) a tenant's ingest token. The raw token is shown once."""
    token = secrets.token_urlsafe(32)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute("DELETE FROM hook_token WHERE tenant_id = %s", (tenant_id,))
        conn.execute(
            "INSERT INTO hook_token (tenant_id, token_hash) VALUES (%s, %s)",
            (tenant_id, _hash(token)),
        )
    return token


def resolve_tenant(token: str) -> Optional[str]:
    """Resolve an ingest token to a tenant id (admin path; bypasses RLS, like login)."""
    with connect(admin_dsn()) as conn:
        row = conn.execute(
            "SELECT tenant_id FROM hook_token WHERE token_hash = %s", (_hash(token),)
        ).fetchone()
    return str(row[0]) if row else None


def get_or_create_salt(tenant_id: str) -> str:
    """Return the tenant's optimize-mode salt, generating one on first use.

    A per-tenant secret (opt spec §4): the SDK fetches it once with its ingest
    token and uses it to salt the request/prefix fingerprints, so the hashes are
    useless to anyone without it. Never leaves the tenant's own RLS context.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute("SELECT opt_salt FROM tenant WHERE id = %s", (tenant_id,)).fetchone()
        if row and row[0]:
            return row[0]
        salt = secrets.token_urlsafe(32)
        conn.execute("UPDATE tenant SET opt_salt = %s WHERE id = %s", (salt, tenant_id))
        return salt


# --------------------------------------------------------------------------
# Event ingest
# --------------------------------------------------------------------------
def _period_of(occurred_at: Optional[str]) -> dt.date:
    if not occurred_at:
        return month_start(dt.date.today())
    text = occurred_at.replace("Z", "+00:00")
    try:
        return month_start(dt.datetime.fromisoformat(text).date())
    except ValueError:
        return month_start(dt.date.fromisoformat(text[:10]))


#: How long a batch id is remembered — comfortably longer than any client will
#: still be retrying, and short enough that the table stays small by itself.
_BATCH_MEMORY = "1 hour"


def _replayed(conn, tenant_id: str, batch_id: str) -> Optional[dict]:
    """Claim `batch_id`, or return the result of its first delivery.

    Costing is additive, so applying a batch twice inflates a feature's spend.
    The insert is the claim: whoever gets the row does the work, and a retry that
    loses the race is handed the original answer instead of a second helping.
    """
    claimed = conn.execute(
        """
        INSERT INTO hook_batch (tenant_id, batch_id) VALUES (%s, %s)
        ON CONFLICT (tenant_id, batch_id) DO NOTHING
        RETURNING batch_id
        """,
        (tenant_id, batch_id),
    ).fetchone()
    if claimed:
        return None  # ours to process
    prior = conn.execute(
        "SELECT accepted, cost FROM hook_batch WHERE tenant_id = %s AND batch_id = %s",
        (tenant_id, batch_id),
    ).fetchone()
    # A replay reports what the first delivery did, so a retrying client sees one
    # consistent answer rather than a silent zero.
    return {"accepted": int(prior[0]), "cost": float(prior[1]), "duplicate": True}


def ingest_events(tenant_id: str, events: list[dict], batch_id: Optional[str] = None) -> dict:
    """Cost and persist metered events into monthly hook rows. Returns a summary.

    Pass `batch_id` to make the call idempotent: re-sending the same batch (a
    client retry, a duplicated delivery) applies nothing and returns the original
    result. Without one, every call is applied — the pre-0.4 SDK behaviour.
    """
    total = Decimal("0")
    accepted = 0
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if batch_id:
            prior = _replayed(conn, tenant_id, batch_id)
            if prior is not None:
                return prior
        valid_features = {str(r[0]) for r in conn.execute("SELECT id FROM feature").fetchall()}
        pools = compute.pool_labels(conn)  # provider_label -> pool_id (self-hosted)
        accumulator: dict[tuple, dict] = {}
        pool_acc: dict[tuple, dict] = {}
        customer_acc: dict[tuple, dict] = {}  # (customer_id, period) -> metered cost
        signal_acc: dict[tuple, dict] = {}  # optimization signals (opt spec §6)
        for event in events:
            provider = event.get("provider")
            is_pool = provider in pools
            if not is_pool and provider not in PRICED_PROVIDERS:
                continue  # unknown provider (no price, no pool) -> skip
            model = event.get("model") or ""
            tokens_in = int(event.get("tokens_in") or 0)
            tokens_out = int(event.get("tokens_out") or 0)
            feature_id = event.get("feature_id")
            if feature_id is not None and str(feature_id) not in valid_features:
                feature_id = None  # unknown/foreign feature -> Unattributed
            period = _period_of(event.get("occurred_at"))
            latency_ms = event.get("latency_ms")
            customer_id = _customer_of(event.get("metadata"))

            if is_pool:
                # Self-hosted: no per-token price. Record usage; cost is allocated
                # later from the pool's infra bill (compute.allocate).
                key = (pools[provider], feature_id, model or None, period)
                entry = pool_acc.setdefault(key, {"tin": 0, "tout": 0, "count": 0})
                entry["tin"] += tokens_in
                entry["tout"] += tokens_out
                entry["count"] += 1
                accepted += 1
                continue

            # Optional optimization signal (opt spec §6) — priced providers only;
            # pool usage has no per-token price for the detectors to work from.
            sig = event.get("signal")
            sig_kind = sig.get("kind") if isinstance(sig, dict) else None
            if sig_kind in ("duplicate", "prefix"):
                _accumulate_signal(
                    signal_acc,
                    sig,
                    sig_kind,
                    feature_id,
                    provider,
                    model,
                    period,
                    tokens_in,
                    tokens_out,
                )
            if sig_kind == "prefix":
                # A prefix event is a flushed summary; its calls were already
                # metered individually — record the signal, never re-cost it.
                accepted += 1
                continue

            cost = price(model, tokens_in, tokens_out, provider)
            key = (feature_id, provider, model, period)
            entry = accumulator.setdefault(
                key, {"amount": Decimal("0"), "tin": 0, "tout": 0, "count": 0, "latency": 0}
            )
            entry["amount"] += cost
            entry["tin"] += tokens_in
            entry["tout"] += tokens_out
            entry["count"] += 1
            if latency_ms is not None:
                entry["latency"] += int(latency_ms)
            # Per-customer metered spend (only when the SDK tagged a customer).
            if customer_id is not None:
                centry = customer_acc.setdefault(
                    (customer_id, period), {"amount": Decimal("0"), "count": 0}
                )
                centry["amount"] += cost
                centry["count"] += 1
            accepted += 1

        for (feature_id, provider, model, period), entry in accumulator.items():
            _upsert_hook_row(conn, tenant_id, feature_id, provider, model, period, entry)
            total += entry["amount"]

        for (customer_id, period), centry in customer_acc.items():
            _upsert_customer_cost(conn, tenant_id, customer_id, period, centry)

        for skey, sentry in signal_acc.items():
            feature_id, provider, model, period, kind, fingerprint = skey
            _upsert_signal(
                conn, tenant_id, feature_id, provider, model, period, kind, fingerprint, sentry
            )

        for (pool_id, feature_id, model, period), entry in pool_acc.items():
            compute.record_usage(
                conn,
                tenant_id,
                pool_id,
                feature_id,
                model,
                period,
                entry["tin"],
                entry["tout"],
                entry["count"],
            )

        if batch_id:
            conn.execute(
                "UPDATE hook_batch SET accepted = %s, cost = %s "
                "WHERE tenant_id = %s AND batch_id = %s",
                (accepted, total, tenant_id, batch_id),
            )
            # Amortised cleanup: a batch id is only useful while a client might
            # still retry it. Pruning on a small fraction of calls keeps the
            # table bounded without a DELETE on every ingest or a cron job.
            if random.random() < 0.02:  # noqa: S311  (not security-sensitive)
                conn.execute(
                    f"DELETE FROM hook_batch WHERE tenant_id = %s "  # noqa: S608
                    f"AND seen_at < now() - interval '{_BATCH_MEMORY}'",
                    (tenant_id,),
                )

    return {"accepted": accepted, "cost": float(total)}


def _customer_of(metadata) -> Optional[str]:
    """Extract the customer identifier from an event's optional metadata."""
    if isinstance(metadata, dict):
        cid = metadata.get("customer_id")
        if cid is not None and str(cid).strip():
            return str(cid)
    return None


def _upsert_customer_cost(conn, tenant_id, customer_id, period, centry) -> None:
    conn.execute(
        """
        INSERT INTO customer_cost (tenant_id, customer_id, period, amount, request_count)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (tenant_id, customer_id, period) DO UPDATE
        SET amount = customer_cost.amount + EXCLUDED.amount,
            request_count = customer_cost.request_count + EXCLUDED.request_count,
            updated_at = now()
        """,
        (tenant_id, customer_id, period, centry["amount"], centry["count"]),
    )


def _accumulate_signal(
    signal_acc, sig, kind, feature_id, provider, model, period, tokens_in, tokens_out
) -> None:
    """Fold one optimization signal into the batch accumulator (opt spec §6).

    A 'duplicate' signal rides on a real metered call, so its token sizes come
    from the event itself; a 'prefix' signal is a flushed client-side summary
    carrying its own aggregated counts and token sums (used to price a
    representative call). Never any prompt text — only hashes and counts.
    """
    fingerprint = sig.get("fingerprint")
    if not fingerprint:
        return  # a signal with no fingerprint is unusable; drop it
    key = (feature_id, provider, model, period, kind, str(fingerprint))
    entry = signal_acc.setdefault(
        key, {"call_count": 0, "tin": 0, "tout": 0, "cached": 0, "prefix_tokens": None}
    )
    if kind == "duplicate":
        # count = number of avoidable repeats this event represents (default 1).
        entry["call_count"] += int(sig.get("count") or 1)
        entry["tin"] += int(tokens_in)
        entry["tout"] += int(tokens_out)
    else:  # prefix summary
        entry["call_count"] += int(sig.get("count") or 0)
        entry["tin"] += int(sig.get("tokens_in") or 0)
        entry["tout"] += int(sig.get("tokens_out") or 0)
        entry["cached"] += int(sig.get("cached_count") or 0)
        ptok = sig.get("prefix_tokens")
        if ptok is not None:
            prev = entry["prefix_tokens"] or 0
            entry["prefix_tokens"] = max(prev, int(ptok))


def _upsert_signal(
    conn, tenant_id, feature_id, provider, model, period, kind, fingerprint, entry
) -> None:
    existing = conn.execute(
        """
        SELECT id FROM usage_signal
        WHERE period = %s AND provider = %s AND signal_kind = %s AND fingerprint = %s
          AND feature_id IS NOT DISTINCT FROM %s
          AND model IS NOT DISTINCT FROM %s
        LIMIT 1
        """,
        (period, provider, kind, fingerprint, feature_id, model),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE usage_signal
            SET call_count = call_count + %s,
                tokens_in = tokens_in + %s,
                tokens_out = tokens_out + %s,
                cached_count = cached_count + %s,
                prefix_tokens = CASE WHEN %s IS NULL THEN prefix_tokens
                                     ELSE GREATEST(COALESCE(prefix_tokens, 0), %s) END,
                updated_at = now()
            WHERE id = %s
            """,
            (
                entry["call_count"],
                entry["tin"],
                entry["tout"],
                entry["cached"],
                entry["prefix_tokens"],
                entry["prefix_tokens"],
                existing[0],
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO usage_signal
                (tenant_id, feature_id, provider, model, period, signal_kind, fingerprint,
                 call_count, prefix_tokens, tokens_in, tokens_out, cached_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                tenant_id,
                feature_id,
                provider,
                model,
                period,
                kind,
                fingerprint,
                entry["call_count"],
                entry["prefix_tokens"],
                entry["tin"],
                entry["tout"],
                entry["cached"],
            ),
        )


def _upsert_hook_row(conn, tenant_id, feature_id, provider, model, period, entry) -> None:
    latency = entry.get("latency", 0)
    existing = conn.execute(
        """
        SELECT id FROM inference_cost
        WHERE source = 'hook' AND period = %s AND provider = %s
          AND feature_id IS NOT DISTINCT FROM %s
          AND model IS NOT DISTINCT FROM %s
        LIMIT 1
        """,
        (period, provider, feature_id, model),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE inference_cost
            SET amount = amount + %s, tokens_in = COALESCE(tokens_in, 0) + %s,
                tokens_out = COALESCE(tokens_out, 0) + %s,
                request_count = COALESCE(request_count, 0) + %s,
                latency_ms_sum = COALESCE(latency_ms_sum, 0) + %s
            WHERE id = %s
            """,
            (entry["amount"], entry["tin"], entry["tout"], entry["count"], latency, existing[0]),
        )
    else:
        conn.execute(
            """
            INSERT INTO inference_cost
                (tenant_id, feature_id, provider, model, amount, period,
                 tokens_in, tokens_out, request_count, latency_ms_sum, source, confidence)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'hook', 'high')
            """,
            (
                tenant_id,
                feature_id,
                provider,
                model,
                entry["amount"],
                period,
                entry["tin"],
                entry["tout"],
                entry["count"],
                latency,
            ),
        )


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------
def reconcile(
    tenant_id: str, period: dt.date, tolerance: Decimal = _DEFAULT_TOLERANCE
) -> list[dict]:
    """Compare hook totals to the provider bill per provider; record the gap."""
    start = month_start(period)
    results: list[dict] = []
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        providers = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT provider FROM inference_cost WHERE period = %s", (start,)
            ).fetchall()
        ]
        for provider in providers:
            billed = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM inference_cost "
                "WHERE provider = %s AND period = %s AND source = 'cost_api'",
                (provider, start),
            ).fetchone()[0]
            attributed = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM inference_cost "
                "WHERE provider = %s AND period = %s AND source = 'hook'",
                (provider, start),
            ).fetchone()[0]
            status = "balanced" if abs(billed - attributed) <= tolerance else "delta"
            conn.execute(
                "DELETE FROM bill_reconciliation WHERE provider = %s AND period = %s",
                (provider, start),
            )
            conn.execute(
                """
                INSERT INTO bill_reconciliation
                    (tenant_id, provider, period, billed_total, attributed_total, status)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (tenant_id, provider, start, billed, attributed, status),
            )
            results.append(
                {
                    "provider": provider,
                    "period": start.isoformat(),
                    "billed": float(billed),
                    "attributed": float(attributed),
                    "delta": float(billed - attributed),
                    "status": status,
                }
            )
    return results
