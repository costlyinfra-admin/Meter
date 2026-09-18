"""An optimization signal, through the route a customer's SDK actually posts to.

Every other test of the measured levers calls `hook.ingest_events` directly — a
function no HTTP request can reach, because `POST /api/hook/events` routes to
`traces.ingest`. So the detector maths was well covered while the question of
whether a signal can ARRIVE was not covered at all, which is how the SDK came to
stop sending them without a single test noticing.

These go through the real route.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from meter import features, optimize_measured
from meter.api import create_app
from meter.db import app_dsn, connect, tenant_tx

PASSWORD = "correct horse battery"
PERIOD = dt.date.today().replace(day=1)
NOW = dt.datetime.now(dt.timezone.utc)


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cto@acme.com", "password": PASSWORD})
    return c


def _post(client, token, events):
    resp = client.post(
        "/api/hook/events",
        headers={"Authorization": f"Bearer {token}"},
        json={"events": events},
    )
    assert resp.status_code == 200, resp.text
    return resp


def _span(trace, span, signal, **over):
    base = {
        "event_type": "span.completed",
        "trace_id": trace,
        "span_id": span,
        "span_kind": "llm",
        "application": "support-agent",
        "operation_name": "classify",
        "environment": "production",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "tokens_in": 1000,
        "tokens_out": 100,
        "occurred_at": NOW.isoformat(),
        "signal": signal,
    }
    base.update(over)
    return base


def _signals(tenant_id):
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return conn.execute(
            "SELECT signal_kind, fingerprint, call_count, cached_count, prefix_tokens "
            "FROM usage_signal ORDER BY signal_kind"
        ).fetchall()


def test_a_duplicate_signal_survives_the_real_route(client):
    tenant = client.get("/api/auth/me").json()["tenant_id"]
    token = client.post("/api/hook/token").json()["token"]

    _post(
        client,
        token,
        [
            {
                "event_type": "trace.started",
                "trace_id": "t1",
                "application": "support-agent",
                "operation_name": "resolve",
                "occurred_at": NOW.isoformat(),
            },
            _span("t1", "s1", {"kind": "duplicate", "fingerprint": "fp-dup", "count": 2}),
        ],
    )

    rows = _signals(tenant)
    assert rows, "a signal posted to the live route never reached usage_signal"
    kind, fingerprint, call_count, _cached, _prefix = rows[0]
    assert (kind, fingerprint) == ("duplicate", "fp-dup")
    assert call_count == 2


def test_a_prefix_signal_carries_its_cached_calls(client):
    """The field that decides the dollars.

    `cacheable = call_count - cached_count`, so a cached count that never
    arrives makes every already-cached call look like an available saving. The
    validator and the accumulator used different names for this field, which is
    exactly the kind of gap a route-level test catches and a unit test does not.
    """
    tenant = client.get("/api/auth/me").json()["tenant_id"]
    token = client.post("/api/hook/token").json()["token"]

    _post(
        client,
        token,
        [
            {
                "event_type": "trace.started",
                "trace_id": "t2",
                "application": "support-agent",
                "operation_name": "resolve",
                "occurred_at": NOW.isoformat(),
            },
            _span(
                "t2",
                "s2",
                {
                    "kind": "prefix",
                    "fingerprint": "fp-prefix",
                    "count": 100,
                    "cached_count": 40,
                    "prefix_tokens": 4000,
                    "tokens_in": 100_000,
                    "tokens_out": 1_000,
                },
            ),
        ],
    )

    rows = _signals(tenant)
    kind, fingerprint, call_count, cached, prefix_tokens = rows[0]
    assert (kind, fingerprint) == ("prefix", "fp-prefix")
    assert call_count == 100
    assert cached == 40, "cached calls were dropped between the validator and the store"
    assert prefix_tokens == 4000


def test_the_measured_lever_fires_on_a_signal_that_arrived_this_way(client):
    """Not just stored — actually reaching the screen that sells the product."""
    tenant = client.get("/api/auth/me").json()["tenant_id"]
    token = client.post("/api/hook/token").json()["token"]
    feature = features.add_feature(tenant, "AI threat triage")["id"]

    events = [
        {
            "event_type": "trace.started",
            "trace_id": "t3",
            "application": "support-agent",
            "operation_name": "resolve",
            "feature_id": feature,
            "occurred_at": NOW.isoformat(),
        }
    ]
    for i in range(3):
        events.append(
            _span(
                "t3",
                f"s3-{i}",
                {"kind": "duplicate", "fingerprint": "fp-repeat", "count": 1},
                feature_id=feature,
                tokens_in=1_000_000,
            )
        )
    _post(client, token, events)

    result = optimize_measured.opportunities(tenant, feature, PERIOD)
    levers = {o["lever"]: o for o in result["opportunities"]}
    assert "duplicate_calls" in levers, "a real signal produced no duplicate-call finding"
    assert levers["duplicate_calls"]["savings_type"] == "measured"
    assert levers["duplicate_calls"]["projected_monthly_savings"] > 0


def test_the_same_duplicate_fingerprint_can_arrive_in_more_than_one_batch(client):
    """The second batch used to fail the whole request.

    `upsert_signal`'s UPDATE compares an untyped parameter against NULL, and a
    duplicate signal carries no prefix size — so the first batch INSERTed fine
    and every later one raised `could not determine data type of parameter $5`,
    a 500 that took the batch's traces and cost down with it. Nothing caught it
    because no test had ever sent the same fingerprint twice, and no SDK had
    sent a duplicate signal at all since the v2 rewrite.
    """
    tenant = client.get("/api/auth/me").json()["tenant_id"]
    token = client.post("/api/hook/token").json()["token"]

    for i in range(3):
        _post(
            client,
            token,
            [
                _span(
                    f"t-{i}",
                    f"s-{i}",
                    {"kind": "duplicate", "fingerprint": "fp-same", "count": 1},
                )
            ],
        )

    rows = [r for r in _signals(tenant) if r[0] == "duplicate"]
    assert len(rows) == 1, "one fingerprint, one row"
    assert rows[0][2] == 3, "later batches were lost"


def test_a_prefix_says_whether_its_token_count_was_measured(client):
    """The flag the prompt-caching lever reads before calling itself measured."""
    tenant = client.get("/api/auth/me").json()["tenant_id"]
    token = client.post("/api/hook/token").json()["token"]

    base = {
        "kind": "prefix",
        "count": 10,
        "cached_count": 0,
        "prefix_tokens": 4000,
        "tokens_in": 40_000,
        "tokens_out": 100,
    }
    _post(client, token, [_span("t-e", "s-e", {**base, "fingerprint": "fp-est"})])
    _post(
        client,
        token,
        [_span("t-m", "s-m", {**base, "fingerprint": "fp-meas", "prefix_measured": True})],
    )

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant):
        flags = dict(
            conn.execute(
                "SELECT fingerprint, prefix_measured FROM usage_signal WHERE signal_kind = 'prefix'"
            ).fetchall()
        )
    # Absent means estimated: a claim of measurement is made, never inferred.
    assert flags == {"fp-est": False, "fp-meas": True}


def test_a_tenant_with_traces_is_not_told_to_install_the_sdk(client):
    """`has_sdk_telemetry` asks whether request telemetry arrives at all.

    It used to ask only whether optimize-mode signals had arrived — the
    narrowest possible way to have it. A customer with fully traced agent runs
    and no optimize mode was shown "Install the SDK to unlock them", which they
    had done.
    """
    tenant = client.get("/api/auth/me").json()["tenant_id"]
    token = client.post("/api/hook/token").json()["token"]

    # A perfectly ordinary metered call. No signal anywhere.
    _post(client, token, [_span("t-plain", "s-plain", None)])

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant):
        assert conn.execute("SELECT count(*) FROM usage_signal").fetchone()[0] == 0

    overview = optimize_measured.copilot_overview(tenant, PERIOD)
    assert overview["has_sdk_telemetry"] is True
    # ...but the measured levers still have nothing to work from, and the screen
    # needs to be able to tell those two states apart.
    assert overview["has_optimize_signals"] is False

    _post(
        client,
        token,
        [_span("t-sig", "s-sig", {"kind": "duplicate", "fingerprint": "fp-x", "count": 1})],
    )
    after = optimize_measured.copilot_overview(tenant, PERIOD)
    assert (after["has_sdk_telemetry"], after["has_optimize_signals"]) == (True, True)
