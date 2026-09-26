"""The real SDK, in optimize mode, against the real route.

This is the test whose absence let the emitter disappear. `test_signal_route.py`
proves the server accepts a hand-written signal; `test_optimize_measured.py`
proves the detector maths. Neither would notice if the SDK stopped sending
anything at all — which is exactly what happened in the v2 rewrite, for two of
the four optimization levers, for every paying customer.

So: the installed SDK, wrapping a fake provider client, posting through
`POST /api/hook/events` into this app, and the Recommendations screen's own
function reading the result back.
"""

from __future__ import annotations

import datetime as dt
import json
import threading

import costlyinfra_meter
import pytest
from fastapi.testclient import TestClient
from meter import features, optimize_measured
from meter.api import create_app
from meter.db import app_dsn, connect, tenant_tx

PASSWORD = "correct horse battery"
PERIOD = dt.date.today().replace(day=1)
SECRET = "the quick brown fox jumped over the lazy dog"
SYSTEM = "You are a security analyst. " * 800  # a big, very cacheable prefix


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cto@acme.com", "password": PASSWORD})
    return c


class Anthropic:
    """An Anthropic-shaped client. Returns cache-creation tokens if asked to.

    Named for the provider on purpose: `wrap()` identifies a client by its type,
    and `messages.create` is only an instrumented path for Anthropic.
    """

    def __init__(self, cache_write: int = 0):
        outer = self

        class messages:
            @staticmethod
            def create(**kwargs):
                return type(
                    "Response",
                    (),
                    {
                        "model": "claude-sonnet-4-6",
                        "usage": type(
                            "U",
                            (),
                            {
                                "input_tokens": 4000,
                                "output_tokens": 100,
                                "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": outer.cache_write,
                            },
                        )(),
                    },
                )()

        self.cache_write = cache_write
        self.messages = messages


def sdk_meter(client, token, feature_id, **kw):
    """A real Meter whose transport is this app, so the SDK posts into the route."""
    sent: list = []
    lock = threading.Lock()

    def transport(url, headers, body):
        path = url[url.index("/api/") :]
        # One at a time: TestClient drives the ASGI app through a single portal,
        # and the SDK's delivery thread would otherwise be calling it while this
        # test's own thread is.
        with lock:
            if body is None:
                resp = client.get(path, headers=headers)
            else:
                sent.append(json.loads(body.decode()))
                resp = client.post(path, headers=headers, content=body)
        return resp.status_code, resp.content

    meter = costlyinfra_meter.Meter(
        application="support-agent",
        ingest_url="https://app.test/api/hook/events",
        token=token,
        feature_id=feature_id,
        transport=transport,
        flush_interval=0.01,
        optimize=True,
        optimize_flush_interval=0.0,  # flush prefix counters on every call
        **kw,
    )
    meter.sent = sent
    return meter


def signals(tenant_id):
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return conn.execute(
            """
            SELECT signal_kind, call_count, cached_count, prefix_tokens, prefix_measured,
                   prefix_tokens_sum, prefix_tokens_n
            FROM usage_signal ORDER BY signal_kind
            """
        ).fetchall()


def setup(client):
    tenant = client.get("/api/auth/me").json()["tenant_id"]
    token = client.post("/api/hook/token").json()["token"]
    feature = features.add_feature(tenant, "AI threat triage")
    return tenant, token, feature["id"]


def wait_for_salt(meter):
    """The salt is fetched on its own thread; nothing is signalled before it lands."""
    for _ in range(200):
        if meter.salt():
            return
        import time

        time.sleep(0.01)
    raise AssertionError("the SDK never got a salt")


def test_a_repeated_call_becomes_a_duplicate_signal_in_the_database(client):
    tenant, token, feature_id = setup(client)
    meter = sdk_meter(client, token, feature_id)
    wrapped = meter.wrap(Anthropic(), feature_id=feature_id)
    wait_for_salt(meter)

    request = {"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "hi"}]}
    wrapped.messages.create(**request)
    wrapped.messages.create(**request)
    assert meter.flush(timeout=5.0)

    kinds = {row[0] for row in signals(tenant)}
    assert "duplicate" in kinds, "the SDK's duplicate signal never reached usage_signal"


def test_a_shared_prefix_becomes_a_prefix_signal_in_the_database(client):
    tenant, token, feature_id = setup(client)
    meter = sdk_meter(client, token, feature_id)
    wrapped = meter.wrap(Anthropic(), feature_id=feature_id)
    wait_for_salt(meter)

    for i in range(5):
        wrapped.messages.create(
            model="claude-sonnet-4-6",
            system=SYSTEM,
            messages=[{"role": "user", "content": f"question {i}"}],
        )
    assert meter.flush(timeout=5.0)

    prefixes = [row for row in signals(tenant) if row[0] == "prefix"]
    assert prefixes, "the SDK's prefix summary never reached usage_signal"
    assert sum(row[1] for row in prefixes) == 5
    # Nothing was served from cache, which is the whole opportunity.
    assert all(row[2] == 0 for row in prefixes)
    # No provider count, so the size is the SDK's estimate and says so.
    assert all(row[4] is False for row in prefixes)
    assert all((row[5], row[6]) == (0, 0) for row in prefixes)


def test_the_provider_s_own_prefix_count_arrives_as_measured(client):
    tenant, token, feature_id = setup(client)
    meter = sdk_meter(client, token, feature_id)
    wrapped = meter.wrap(Anthropic(cache_write=3500), feature_id=feature_id)
    wait_for_salt(meter)

    wrapped.messages.create(model="claude-sonnet-4-6", system=SYSTEM, messages=[{"role": "u"}])
    assert meter.flush(timeout=5.0)

    prefix = [row for row in signals(tenant) if row[0] == "prefix"][0]
    # The provider's count arrives summed and counted, so the server holds a
    # mean rather than a high-water mark: one report of 3,500 tokens.
    assert (prefix[5], prefix[6]) == (3500, 1), "the provider's count did not arrive"
    assert prefix[4] is True
    # ...and it does NOT land in prefix_tokens, which carries the SDK's
    # character estimate and nothing else. The two shared a column once, folded
    # with GREATEST, and an estimate could outrank a measurement and be
    # labelled as one.
    assert prefix[3] != 3500
    assert prefix[3] == len(json.dumps([SYSTEM, None], sort_keys=True)) // 4


def test_the_recommendation_a_customer_sees_comes_out_the_far_end(client):
    """The whole chain: wrapped call -> route -> usage_signal -> the screen."""
    tenant, token, feature_id = setup(client)
    meter = sdk_meter(client, token, feature_id)
    # With a customer named, repeats are scoped: these calls are all one
    # customer's, so "the second could have been served from the first" is at
    # least a coherent claim. Without it the finding says so — see the test below.
    wrapped = meter.wrap(Anthropic(), feature_id=feature_id, customer_id="acme-corp")
    wait_for_salt(meter)

    # Both levers gate on volume — 100 cacheable calls, a dollar of savings —
    # because a finding below that is noise. This is the smallest traffic that
    # clears both, which is also the point: nothing here is contrived upward.
    request = {
        "model": "claude-sonnet-4-6",
        "system": SYSTEM,
        "messages": [{"role": "user", "content": "the same question"}],
    }
    for _ in range(151):
        wrapped.messages.create(**request)
    assert meter.flush(timeout=60.0)

    result = optimize_measured.opportunities(tenant, feature_id, PERIOD)
    levers = {o["lever"] for o in result["opportunities"]}
    assert "duplicate_calls" in levers, "151 identical calls produced no repeated-request finding"
    assert "prompt_caching" in levers, "a 22,400-character static prefix produced no finding"
    repeats = next(o for o in result["opportunities"] if o["lever"] == "duplicate_calls")
    # The real SDK now sends a v2 identity and an explicit customer scope, so the
    # finding is built from a comparison of the WHOLE request within one scope.
    assert repeats["savings_type"] == "modeled_ceiling"
    assert "reuse safety is unverified" not in repeats["evidence"]

    # And the Recommendations screen's own payload, with no "install the SDK"
    # prompt for someone who plainly has.
    overview = optimize_measured.copilot_overview(tenant, PERIOD)
    assert overview["has_sdk_telemetry"] is True
    by_lever = {e["lever"]: e["monthly"] for e in overview["by_lever"]}
    assert by_lever.get("prompt_caching", 0) > 0

    # The repeated-request finding is DETECTED and visible on the feature, but
    # it is excluded from the rollup because these are the same input tokens the
    # prefix finding already prices. Counting both would sell the same saving
    # twice; the overlap cannot be quantified from these aggregates, so the
    # smaller one is dropped rather than added.
    assert repeats["overlaps"] == "Prompt caching"
    assert by_lever.get("duplicate_calls", 0) == 0


def test_nothing_a_customer_typed_is_anywhere_in_what_was_sent(client):
    """The privacy boundary, checked on the actual bytes that left the SDK."""
    _tenant, token, feature_id = setup(client)
    meter = sdk_meter(client, token, feature_id)
    wrapped = meter.wrap(Anthropic(), feature_id=feature_id)
    wait_for_salt(meter)

    request = {
        "model": "claude-sonnet-4-6",
        "system": f"SYSTEM {SECRET}",
        "tools": [{"name": f"tool_{SECRET}"}],
        "messages": [{"role": "user", "content": SECRET}],
        "metadata": {"user": "alice@example.com"},
    }
    wrapped.messages.create(**request)
    wrapped.messages.create(**request)
    assert meter.flush(timeout=5.0)

    wire = json.dumps(meter.sent)
    assert '"signal"' in wire, "this test proves nothing if no signal was sent"
    for leak in (SECRET, "alice@example.com", "SYSTEM", "tool_"):
        assert leak not in wire, f"{leak!r} reached the wire"


def test_without_a_customer_the_finding_says_reuse_is_unverified(client):
    """The honest half of the scope rule, end to end.

    `wrap()` without a customer or cache scope still detects the repeats — they
    are real — but nobody has said the two calls belong to the same user, tenant
    or cache, so nothing may imply the second could have served the first.
    """
    tenant, token, feature_id = setup(client)
    meter = sdk_meter(client, token, feature_id)
    wrapped = meter.wrap(Anthropic(), feature_id=feature_id)  # no customer
    wait_for_salt(meter)

    request = {
        "model": "claude-sonnet-4-6",
        "system": SYSTEM,
        "messages": [{"role": "user", "content": "the same question"}],
    }
    for _ in range(151):
        wrapped.messages.create(**request)
    assert meter.flush(timeout=60.0)

    result = optimize_measured.opportunities(tenant, feature_id, PERIOD)
    repeats = next(o for o in result["opportunities"] if o["lever"] == "duplicate_calls")
    assert "reuse safety is unverified" in repeats["evidence"]
    # Below a scoped finding, never above it.
    assert repeats["confidence"] == "low"
    assert repeats["savings_type"] == "modeled_ceiling"
