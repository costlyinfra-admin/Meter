"""Self-hosted compute pools: usage routing + infra-cost allocation."""

from __future__ import annotations

import datetime as dt

from meter import compute, dashboard, features, hook

PERIOD = dt.date(2026, 6, 1)


def _pool(tenant_id, monthly_cost="18000"):
    return compute.register_pool(tenant_id, "Llama-3.1-70B GPU pool", "self_hosted", monthly_cost)


def test_register_pool_is_idempotent(tenant_id):
    a = _pool(tenant_id, "18000")
    b = compute.register_pool(tenant_id, "Renamed pool", "self_hosted", "20000")
    assert a["id"] == b["id"]  # same label -> upsert, not a duplicate
    pools = compute.list_pools(tenant_id)
    assert len(pools) == 1
    assert pools[0]["name"] == "Renamed pool"
    assert pools[0]["monthly_cost"] == 20000.0


def test_self_hosted_events_route_to_pool_usage_not_priced(tenant_id):
    _pool(tenant_id)
    triage = features.add_feature(tenant_id, "AI threat triage")

    summary = hook.ingest_events(
        tenant_id,
        [
            {
                "provider": "self_hosted",  # matches the pool label
                "model": "llama-3.1-70b",
                "tokens_in": 600_000_000,
                "tokens_out": 0,
                "feature_id": triage["id"],
                "occurred_at": "2026-06-10T00:00:00Z",
            }
        ],
    )
    # Accepted as usage, but priced cost is 0 (no per-token price for self-hosted).
    assert summary["accepted"] == 1
    assert summary["cost"] == 0.0

    # Nothing in inference_cost yet (allocation hasn't run); usage is recorded.
    detail = dashboard.feature_detail(tenant_id, triage["id"], PERIOD)
    assert detail["headline"]["inference_cost"] == 0.0


def test_allocate_splits_pool_cost_by_usage_share(tenant_id):
    _pool(tenant_id, "18000")  # $18k GPU bill
    triage = features.add_feature(tenant_id, "AI threat triage")
    soc = features.add_feature(tenant_id, "SOC copilot")

    # triage 600M tokens (75%), soc 200M (25%).
    hook.ingest_events(
        tenant_id,
        [
            {
                "provider": "self_hosted",
                "model": "llama",
                "tokens_in": 600_000_000,
                "tokens_out": 0,
                "feature_id": triage["id"],
                "occurred_at": "2026-06-10T00:00:00Z",
            },
            {
                "provider": "self_hosted",
                "model": "llama",
                "tokens_in": 200_000_000,
                "tokens_out": 0,
                "feature_id": soc["id"],
                "occurred_at": "2026-06-11T00:00:00Z",
            },
        ],
    )

    result = compute.allocate(tenant_id, PERIOD)
    assert result[0]["allocated"] == 18000.0
    assert result[0]["unattributed"] == 0.0

    data = dashboard.dashboard(tenant_id, PERIOD)
    by_name = {f["name"]: f for f in data["features"]}
    assert by_name["AI threat triage"]["inference_cost"] == 13500.0  # 75%
    assert by_name["SOC copilot"]["inference_cost"] == 4500.0  # 25%
    # Allocation parts sum back to the pool bill (reconciles by construction).
    assert (
        by_name["AI threat triage"]["inference_cost"] + by_name["SOC copilot"]["inference_cost"]
        == 18000.0
    )
    # It's an allocation, not a metered price -> med confidence.
    assert by_name["AI threat triage"]["confidence"] == "med"


def test_untagged_usage_goes_to_unattributed(tenant_id):
    _pool(tenant_id, "1000")
    triage = features.add_feature(tenant_id, "AI threat triage")

    hook.ingest_events(
        tenant_id,
        [
            {
                "provider": "self_hosted",
                "model": "llama",
                "tokens_in": 750_000_000,
                "tokens_out": 0,
                "feature_id": triage["id"],
                "occurred_at": "2026-06-10T00:00:00Z",
            },
            # No feature_id -> untagged self-hosted traffic.
            {
                "provider": "self_hosted",
                "model": "llama",
                "tokens_in": 250_000_000,
                "tokens_out": 0,
                "occurred_at": "2026-06-10T00:00:00Z",
            },
        ],
    )
    compute.allocate(tenant_id, PERIOD)

    data = dashboard.dashboard(tenant_id, PERIOD)
    by_name = {f["name"]: f for f in data["features"]}
    assert by_name["AI threat triage"]["inference_cost"] == 750.0  # 75% of $1000
    assert data["unattributed"]["inference_cost"] == 250.0  # untagged 25%


def test_allocate_is_idempotent(tenant_id):
    _pool(tenant_id, "1000")
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            {
                "provider": "self_hosted",
                "model": "llama",
                "tokens_in": 1_000_000,
                "tokens_out": 0,
                "feature_id": triage["id"],
                "occurred_at": "2026-06-10T00:00:00Z",
            }
        ],
    )
    compute.allocate(tenant_id, PERIOD)
    compute.allocate(tenant_id, PERIOD)  # re-run must not double-count

    data = dashboard.dashboard(tenant_id, PERIOD)
    by_name = {f["name"]: f for f in data["features"]}
    assert by_name["AI threat triage"]["inference_cost"] == 1000.0
