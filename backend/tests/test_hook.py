"""Metering hook: token auth, event ingest, reconciliation, no double-counting."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from meter import dashboard, features, hook, inference
from meter.db import app_dsn, connect, tenant_tx
from meter.providers import CostRecord

PERIOD = dt.date(2026, 6, 1)


def test_token_roundtrip(tenant_id):
    token = hook.generate_token(tenant_id)
    assert hook.resolve_tenant(token) == tenant_id
    assert hook.resolve_tenant("not-a-real-token") is None
    # Regenerating replaces the old token.
    token2 = hook.generate_token(tenant_id)
    assert hook.resolve_tenant(token) is None
    assert hook.resolve_tenant(token2) == tenant_id


def test_hook_events_write_high_confidence_rows(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")
    summary = hook.ingest_events(
        tenant_id,
        [
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "tokens_in": 100_000_000,
                "tokens_out": 0,
                "feature_id": triage["id"],
                "occurred_at": "2026-06-15T10:00:00Z",
            },
            {  # foreign feature id -> Unattributed
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "tokens_in": 0,
                "tokens_out": 0,
                "feature_id": "00000000-0000-0000-0000-000000000000",
            },
        ],
    )
    assert summary["accepted"] == 2
    assert summary["cost"] == 300.0  # 100M input tokens * $3/M

    detail = dashboard.feature_detail(tenant_id, triage["id"], PERIOD)
    assert detail["headline"]["inference_cost"] == 300.0
    assert detail["inference_sources"] == ["hook"]  # connector-vs-hook indicator


def test_hook_prices_hosted_open_source_provider(tenant_id):
    # A hosted open-source provider (Together) is metered and priced per its rates.
    phishing = features.add_feature(tenant_id, "Phishing detection")
    summary = hook.ingest_events(
        tenant_id,
        [
            {
                "provider": "together",
                "model": "meta-llama-3.1-70b-instruct",  # $0.88/M in + out
                "tokens_in": 1_000_000,
                "tokens_out": 1_000_000,
                "feature_id": phishing["id"],
                "occurred_at": "2026-06-10T12:00:00Z",
            }
        ],
    )
    assert summary["accepted"] == 1
    assert summary["cost"] == 1.76  # 0.88 + 0.88

    detail = dashboard.feature_detail(tenant_id, phishing["id"], PERIOD)
    assert detail["headline"]["inference_cost"] == 1.76


def test_reconciliation_routes_gap_to_unattributed(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")

    # Provider bill (authoritative) = $1000 for anthropic, unmapped at the connector level.
    inference.ingest_records(
        tenant_id,
        "anthropic",
        PERIOD,
        [CostRecord("anthropic", PERIOD, Decimal("1000"), api_key_ref="key:shared")],
    )
    # Hook meters $300 of that to the triage feature.
    hook.ingest_events(
        tenant_id,
        [
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "tokens_in": 100_000_000,
                "tokens_out": 0,
                "feature_id": triage["id"],
                "occurred_at": "2026-06-15T10:00:00Z",
            }
        ],
    )

    recon = hook.reconcile(tenant_id, PERIOD)
    anthropic = next(r for r in recon if r["provider"] == "anthropic")
    assert anthropic["billed"] == 1000.0
    assert anthropic["attributed"] == 300.0
    assert anthropic["delta"] == 700.0
    assert anthropic["status"] == "delta"

    # Dashboard: hook drives the feature number; the gap is Unattributed; the
    # total still equals the bill (no double-counting of connector + hook).
    data = dashboard.dashboard(tenant_id, PERIOD)
    triage_row = next(f for f in data["features"] if f["name"] == "AI threat triage")
    assert triage_row["inference_cost"] == 300.0
    assert triage_row["confidence"] == "high"  # hook lifts to top of the ladder
    assert data["unattributed"]["inference_cost"] == 700.0
    assert data["totals"]["inference_cost"] == 1000.0


def test_onboarding_unaffected_without_hook(tenant_id):
    # No hook token, no events: dashboard still works (connector path stands alone).
    features.add_feature(tenant_id, "Report generator")
    data = dashboard.dashboard(tenant_id, PERIOD)
    assert data["totals"]["inference_cost"] == 0.0


def test_hook_captures_latency_and_customer(tenant_id):
    # SDK v0.2 sends optional latency_ms and metadata.customer_id.
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "tokens_in": 1_000_000,
                "tokens_out": 0,
                "feature_id": triage["id"],
                "occurred_at": "2026-06-15T10:00:00Z",
                "latency_ms": 800,
                "metadata": {"customer_id": "acme"},
            },
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "tokens_in": 1_000_000,
                "tokens_out": 0,
                "feature_id": triage["id"],
                "occurred_at": "2026-06-16T10:00:00Z",
                "latency_ms": 1200,
                "metadata": {"customer_id": "acme"},
            },
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "tokens_in": 1_000_000,
                "tokens_out": 0,
                "feature_id": triage["id"],
                "occurred_at": "2026-06-16T11:00:00Z",
                "latency_ms": 400,
                "metadata": {"customer_id": "globex"},
            },
        ],
    )

    # Avg latency across the 3 metered calls = (800 + 1200 + 400) / 3 = 800 ms.
    detail = dashboard.feature_detail(tenant_id, triage["id"], PERIOD)
    assert detail["headline"]["avg_latency_ms"] == 800

    # Per-customer metered spend (anthropic input = $3/M): acme 2M -> $6, globex 1M -> $3.
    prov = dashboard.spend_by_provider(tenant_id, PERIOD, PERIOD)
    by_customer = {c["customer_id"]: c for c in prov["by_customer"]}
    assert set(by_customer) == {"acme", "globex"}
    assert by_customer["acme"]["amount"] == 6.0
    assert by_customer["acme"]["requests"] == 2
    assert by_customer["globex"]["amount"] == 3.0
    assert prov["customer_total"] == 9.0


def test_hook_records_optimization_signals(tenant_id):
    # opt spec M-opt-1: duplicate + prefix signals persist; cost is unchanged.
    triage = features.add_feature(tenant_id, "AI threat triage")
    summary = hook.ingest_events(
        tenant_id,
        [
            # Two real metered calls that are also repeats of the same request.
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "tokens_in": 1_000_000,
                "tokens_out": 0,
                "feature_id": triage["id"],
                "occurred_at": "2026-06-15T10:00:00Z",
                "signal": {"kind": "duplicate", "fingerprint": "fp-req-1", "count": 1},
            },
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "tokens_in": 1_000_000,
                "tokens_out": 0,
                "feature_id": triage["id"],
                "occurred_at": "2026-06-16T10:00:00Z",
                "signal": {"kind": "duplicate", "fingerprint": "fp-req-1", "count": 1},
            },
            # A flushed prefix summary — represents 320 already-metered calls, so it
            # must NOT add cost (only the signal is recorded).
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "feature_id": triage["id"],
                "occurred_at": "2026-06-16T11:00:00Z",
                "signal": {
                    "kind": "prefix",
                    "fingerprint": "fp-prefix-1",
                    "count": 320,
                    "prefix_tokens": 4100,
                    "cached_count": 0,
                    "tokens_in": 1_312_000,
                    "tokens_out": 0,
                },
            },
        ],
    )
    assert summary["accepted"] == 3

    # Cost accounting UNCHANGED: only the two real calls (2M input @ $3/M) are
    # costed; the prefix summary contributes nothing.
    assert summary["cost"] == 6.0
    detail = dashboard.feature_detail(tenant_id, triage["id"], PERIOD)
    assert detail["headline"]["inference_cost"] == 6.0

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            "SELECT signal_kind, fingerprint, call_count, prefix_tokens, tokens_in, cached_count "
            "FROM usage_signal ORDER BY signal_kind"
        ).fetchall()
    by_kind = {r[0]: r for r in rows}
    assert set(by_kind) == {"duplicate", "prefix"}
    # Two duplicate events of one fingerprint -> call_count 2, no prefix size.
    assert by_kind["duplicate"][2] == 2
    assert by_kind["duplicate"][3] is None
    assert by_kind["duplicate"][4] == 2_000_000
    # Prefix summary keeps its aggregated counts.
    assert by_kind["prefix"][2] == 320
    assert by_kind["prefix"][3] == 4100
    assert by_kind["prefix"][4] == 1_312_000
    assert by_kind["prefix"][5] == 0


def test_usage_signal_is_tenant_isolated(tenant_id, app_env):
    # A signal written for one tenant is invisible to another under RLS.
    triage = features.add_feature(tenant_id, "AI threat triage")
    hook.ingest_events(
        tenant_id,
        [
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-6",
                "tokens_in": 1_000_000,
                "tokens_out": 0,
                "feature_id": triage["id"],
                "occurred_at": "2026-06-15T10:00:00Z",
                "signal": {"kind": "duplicate", "fingerprint": "fp-x", "count": 1},
            }
        ],
    )
    other = str(
        app_env.execute("INSERT INTO tenant (name) VALUES ('Other') RETURNING id").fetchone()[0]
    )
    app_env.commit()

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        assert conn.execute("SELECT count(*) FROM usage_signal").fetchone()[0] == 1
    with connect(app_dsn()) as conn, tenant_tx(conn, other):
        assert conn.execute("SELECT count(*) FROM usage_signal").fetchone()[0] == 0


# --- Idempotent ingest: retries must not become double charges -------------


def _event(feature_id, tokens_in=1_000_000):
    return {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "tokens_in": tokens_in,
        "tokens_out": 0,
        "feature_id": feature_id,
        "occurred_at": f"{PERIOD:%Y-%m-%d}T10:00:00Z",
    }


def _hook_total(tenant_id, feature_id):
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM inference_cost "
            "WHERE source = 'hook' AND feature_id = %s",
            (feature_id,),
        ).fetchone()
    return Decimal(row[0])


def test_replaying_a_batch_costs_nothing_extra(tenant_id):
    """The whole reason the batch id exists.

    Costing is additive (_upsert_hook_row does amount = amount + ...), so a retry
    of a batch whose response was lost would charge a feature twice. And
    reconciliation only routes a POSITIVE gap to Unattributed, so over-counting
    is the one direction nothing downstream catches.
    """
    feature = features.add_feature(tenant_id, "Triage")["id"]
    batch = [_event(feature), _event(feature)]

    first = hook.ingest_events(tenant_id, batch, batch_id="b-abc-123")
    once = _hook_total(tenant_id, feature)
    assert first["accepted"] == 2
    assert once > 0

    # The same batch again — a client retrying after a timeout it could not
    # distinguish from a failure.
    again = hook.ingest_events(tenant_id, batch, batch_id="b-abc-123")
    assert _hook_total(tenant_id, feature) == once, "a retry double-charged the feature"
    # ...and it is told what the first delivery did, not a silent zero.
    assert again["accepted"] == first["accepted"]
    assert again["cost"] == first["cost"]
    assert again["duplicate"] is True


def test_a_different_batch_id_is_a_different_batch(tenant_id):
    """Idempotency must key on the batch, not collapse genuinely new events."""
    feature = features.add_feature(tenant_id, "Triage")["id"]
    hook.ingest_events(tenant_id, [_event(feature)], batch_id="b-1")
    once = _hook_total(tenant_id, feature)
    hook.ingest_events(tenant_id, [_event(feature)], batch_id="b-2")
    assert _hook_total(tenant_id, feature) == once * 2


def test_ingest_without_a_batch_id_still_applies(tenant_id):
    """Pre-0.4 SDKs send no batch id and must keep working exactly as before."""
    feature = features.add_feature(tenant_id, "Triage")["id"]
    hook.ingest_events(tenant_id, [_event(feature)])
    once = _hook_total(tenant_id, feature)
    hook.ingest_events(tenant_id, [_event(feature)])
    assert _hook_total(tenant_id, feature) == once * 2


def test_batch_ids_are_tenant_scoped(tenant_id, app_env):
    """One tenant's batch id must never suppress another's ingest."""
    other = str(
        app_env.execute("INSERT INTO tenant (name) VALUES ('Other Co') RETURNING id").fetchone()[0]
    )
    app_env.commit()

    mine = features.add_feature(tenant_id, "Triage")["id"]
    theirs = features.add_feature(other, "Triage")["id"]

    hook.ingest_events(tenant_id, [_event(mine)], batch_id="same-id")
    result = hook.ingest_events(other, [_event(theirs)], batch_id="same-id")

    assert result.get("duplicate") is None  # not treated as a replay
    assert _hook_total(other, theirs) > 0
