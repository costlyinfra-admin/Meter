"""Self-hosted compute pools: register, meter usage, allocate infra cost.

Open-source / self-hosted serving has no per-token price — its cost is a GPU /
infra bill. Each serving deployment is a `compute_pool` with a monthly cost. The
metering hook records per-feature token usage into `pool_usage`; `allocate` then
splits the pool's monthly cost across features by usage share and writes
``inference_cost`` rows with ``source='self_host'`` at MED confidence (it's an
allocation, not a metered price). Usage with no feature_id -> Unattributed. The
allocated parts always sum to the pool cost, so it reconciles by construction.

Build cost (fine-tuning runs) is handled separately and never blended in here
(invariant 2). Self-hosted = the *run* side only.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from .db import app_dsn, connect, tenant_tx
from .providers import month_start

# Allocating a pooled bill by usage share is an estimate, not a metered price.
ALLOCATION_CONFIDENCE = "med"


# --------------------------------------------------------------------------
# Pool registry
# --------------------------------------------------------------------------
def register_pool(tenant_id: str, name: str, provider_label: str, monthly_cost) -> dict:
    """Create or update a self-hosted pool (idempotent on provider_label)."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            """
            INSERT INTO compute_pool (tenant_id, name, provider_label, monthly_cost)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (tenant_id, provider_label)
            DO UPDATE SET name = EXCLUDED.name, monthly_cost = EXCLUDED.monthly_cost
            RETURNING id, name, provider_label, monthly_cost
            """,
            (tenant_id, name, provider_label, monthly_cost),
        ).fetchone()
    return _pool_dict(row)


def list_pools(tenant_id: str) -> list[dict]:
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            "SELECT id, name, provider_label, monthly_cost FROM compute_pool ORDER BY name"
        ).fetchall()
    return [_pool_dict(r) for r in rows]


def pool_labels(conn) -> dict:
    """provider_label -> pool_id for the current tenant tx (used by the hook)."""
    return {
        r[1]: str(r[0])
        for r in conn.execute("SELECT id, provider_label FROM compute_pool").fetchall()
    }


def _pool_dict(row) -> dict:
    return {
        "id": str(row[0]),
        "name": row[1],
        "provider_label": row[2],
        "monthly_cost": float(row[3]),
    }


# --------------------------------------------------------------------------
# Usage capture (called by the hook, within its tenant tx)
# --------------------------------------------------------------------------
def record_usage(
    conn, tenant_id, pool_id, feature_id, model, period, tokens_in, tokens_out, requests
):
    """Accumulate metered usage for (pool, feature, model, period) — no pricing."""
    existing = conn.execute(
        """
        SELECT id FROM pool_usage
        WHERE pool_id = %s AND period = %s
          AND feature_id IS NOT DISTINCT FROM %s
          AND model IS NOT DISTINCT FROM %s
        LIMIT 1
        """,
        (pool_id, period, feature_id, model),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE pool_usage SET tokens_in = tokens_in + %s, tokens_out = tokens_out + %s, "
            "request_count = request_count + %s WHERE id = %s",
            (tokens_in, tokens_out, requests, existing[0]),
        )
    else:
        conn.execute(
            """
            INSERT INTO pool_usage
                (tenant_id, pool_id, feature_id, model, period,
                 tokens_in, tokens_out, request_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (tenant_id, pool_id, feature_id, model, period, tokens_in, tokens_out, requests),
        )


# --------------------------------------------------------------------------
# Allocation: pool cost -> per-feature inference_cost
# --------------------------------------------------------------------------
def allocate(tenant_id: str, period: dt.date, pool_id: Optional[str] = None) -> list[dict]:
    """Split each pool's monthly cost across features by usage share for ``period``.

    Writes inference_cost rows with source='self_host'. Idempotent per
    (pool, period): re-running replaces the prior allocation.
    """
    start = month_start(period)
    results: list[dict] = []
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        query = "SELECT id, name, provider_label, monthly_cost FROM compute_pool"
        params: tuple = ()
        if pool_id is not None:
            query += " WHERE id = %s"
            params = (pool_id,)
        pools = conn.execute(query, params).fetchall()

        for pid, name, label, monthly_cost in pools:
            cost = Decimal(monthly_cost)
            # Idempotent: clear any prior allocation for this pool/period.
            conn.execute(
                "DELETE FROM inference_cost WHERE source = 'self_host' "
                "AND provider = %s AND period = %s",
                (label, start),
            )
            usage = conn.execute(
                """
                SELECT feature_id, SUM(tokens_in + tokens_out), SUM(request_count)
                FROM pool_usage WHERE pool_id = %s AND period = %s
                GROUP BY feature_id
                """,
                (pid, start),
            ).fetchall()

            total_tokens = sum(int(toks) for _f, toks, _r in usage)
            unattributed = Decimal("0")

            if total_tokens <= 0:
                # Pool is paid for but nothing is attributed -> all Unattributed.
                _insert_self_host(conn, tenant_id, None, label, name, cost, start, 0)
                unattributed = cost
            else:
                weights = {feat: int(toks) for feat, toks, _r in usage}
                requests = {feat: int(r or 0) for feat, _t, r in usage}
                for feat, amount in _split_cost(cost, weights).items():
                    _insert_self_host(
                        conn, tenant_id, feat, label, name, amount, start, requests.get(feat, 0)
                    )
                    if feat is None:
                        unattributed += amount

            results.append(
                {
                    "pool": name,
                    "provider_label": label,
                    "allocated": float(cost),
                    "unattributed": float(unattributed),
                }
            )
    return results


def _insert_self_host(conn, tenant_id, feature_id, label, name, amount, period, requests):
    conn.execute(
        """
        INSERT INTO inference_cost
            (tenant_id, feature_id, provider, model, api_key_ref, amount, period,
             request_count, source, confidence)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'self_host', %s)
        """,
        (
            tenant_id,
            feature_id,
            label,
            name,  # the pool name doubles as the "model" label in breakdowns
            f"pool:{label}",
            amount,
            period,
            requests,
            ALLOCATION_CONFIDENCE,
        ),
    )


def _split_cost(total: Decimal, weights: dict) -> dict:
    """Split ``total`` across keys proportional to integer weights, exactly.

    Keys may include ``None`` (Unattributed). The rounding remainder lands on the
    largest share so the parts sum to exactly ``total``.
    """
    weight_sum = sum(weights.values())
    ordered = sorted(weights.items(), key=lambda kv: (-kv[1], str(kv[0])))
    out: dict = {}
    allocated = Decimal("0")
    for key, weight in ordered:
        share = (total * weight / weight_sum).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        out[key] = share
        allocated += share
    out[ordered[0][0]] += total - allocated
    return out
