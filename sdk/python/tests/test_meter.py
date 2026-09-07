"""Tests for the Python metering SDK (no network — a transport captures posts)."""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
import urllib.error

from costlyinfra_meter import (
    Meter,
    _detect_provider,  # noqa: PLC2701  (tested on purpose)
    wrap,
)


class _Capture:
    def __init__(self):
        self.calls = []

    def __call__(self, url, headers, body):
        payload = json.loads(body)
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "events": payload["events"],
                "batch_id": payload.get("batch_id"),
            }
        )


def _meter(capture, *, retry_backoff=None, **kw):
    if retry_backoff is not None:
        kw["retry_backoff"] = (retry_backoff,)  # keep retry tests instant
    return Meter(
        ingest_url="https://app.test/api/hook/events", token="tok", transport=capture, **kw
    )


def test_record_builds_event_and_authenticates():
    cap = _Capture()
    m = _meter(cap)
    m.record(
        provider="anthropic",
        model="claude-sonnet-4-6",
        tokens_in=1200,
        tokens_out=300,
        feature_id="f1",
    )
    m.flush()

    assert cap.calls[0]["headers"]["Authorization"] == "Bearer tok"
    event = cap.calls[0]["events"][0]
    assert event["provider"] == "anthropic"
    assert event["tokens_in"] == 1200
    assert event["feature_id"] == "f1"


def test_record_anthropic_maps_usage():
    cap = _Capture()
    m = _meter(cap)
    resp = type(
        "R", (), {"model": "claude-haiku-4-5", "usage": {"input_tokens": 50, "output_tokens": 7}}
    )()
    m.record_anthropic(resp, feature_id="f2")
    m.flush()
    event = cap.calls[0]["events"][0]
    assert _stamped(event) == {
        "provider": "anthropic",
        "model": "claude-haiku-4-5",
        "tokens_in": 50,
        "tokens_out": 7,
        "feature_id": "f2",
    }


def test_record_openai_maps_usage():
    cap = _Capture()
    m = _meter(cap)
    resp = {"model": "gpt-4o", "usage": {"prompt_tokens": 80, "completion_tokens": 20}}
    m.record_openai(resp, feature_id="f3")
    m.flush()
    event = cap.calls[0]["events"][0]
    assert event["provider"] == "openai"
    assert event["tokens_in"] == 80
    assert event["tokens_out"] == 20


def test_record_gemini_maps_usage_metadata():
    cap = _Capture()
    m = _meter(cap)
    resp = type(
        "R",
        (),
        {
            "model_version": "gemini-2.5-flash",
            "usage_metadata": {"prompt_token_count": 800, "candidates_token_count": 120},
        },
    )()
    m.record_gemini(resp, feature_id="f7")
    m.flush()
    event = cap.calls[0]["events"][0]
    assert _stamped(event) == {
        "provider": "google",
        "model": "gemini-2.5-flash",
        "tokens_in": 800,
        "tokens_out": 120,
        "feature_id": "f7",
    }


def test_record_openai_compatible_tags_hosted_oss_provider():
    cap = _Capture()
    m = _meter(cap)
    # A Together response uses the OpenAI usage shape; provider drives pricing.
    resp = {
        "model": "meta-llama-3.1-70b-instruct",
        "usage": {"prompt_tokens": 1200, "completion_tokens": 300},
    }
    m.record_openai_compatible(resp, provider="together", feature_id="f9")
    m.flush()
    event = cap.calls[0]["events"][0]
    assert _stamped(event) == {
        "provider": "together",
        "model": "meta-llama-3.1-70b-instruct",
        "tokens_in": 1200,
        "tokens_out": 300,
        "feature_id": "f9",
    }


def test_unconfigured_meter_is_a_noop():
    cap = _Capture()
    m = Meter(transport=cap)  # no url/token
    assert m.enabled is False
    assert m.record(provider="openai", model="gpt-4o", tokens_in=1, tokens_out=1) is None
    assert cap.calls == []


def test_record_does_not_raise_when_a_thread_cannot_start(monkeypatch):
    """An app at its thread limit must still be able to call record().

    Metering spawns a thread per call, so it helps push a busy application to
    the limit — and Thread.start() then raises. That must never reach the
    caller's request path: the event is dropped, and record() returns None.
    """
    cap = _Capture()
    m = _meter(cap)

    def _refuse(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", _refuse)

    assert m.record(provider="openai", model="gpt-4o", tokens_in=1, tokens_out=1) is None
    assert m.record_anthropic(_anthropic_resp(), feature_id="f1") is None
    assert cap.calls == []  # nothing sent, but nothing raised either


def test_wrapped_call_still_returns_the_response_when_a_thread_cannot_start(monkeypatch):
    """The wrapped client keeps working even when metering cannot run at all."""
    cap = _Capture()
    resp = _anthropic_resp()
    client = wrap(_FakeAnthropic(resp), provider="anthropic", meter=_meter(cap))

    def _refuse(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", _refuse)

    assert client.messages.create(model="claude-sonnet-4-6", messages=[]) is resp


# --- wrap() auto-instrumentation, latency, metadata ------------------------


def _flushed(meter, timeout=2.0):
    """Delivery is batched on a worker; force it and wait for the queue to drain."""
    assert meter.flush(timeout=timeout), "meter did not drain within the timeout"


class _FakeMessages:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.resp


class _FakeAnthropic:
    def __init__(self, resp):
        self.messages = _FakeMessages(resp)
        self.api_key = "sk-real"


def _anthropic_resp():
    return type(
        "R", (), {"model": "claude-sonnet-4-6", "usage": {"input_tokens": 10, "output_tokens": 2}}
    )()


def test_wrap_records_completion_and_passes_through():
    cap = _Capture()
    resp = _anthropic_resp()
    client = _FakeAnthropic(resp)
    m = Meter(
        ingest_url="https://app.test/api/hook/events",
        token="tok",
        feature_id="f1",
        transport=cap,
    )
    wrapped = wrap(client, provider="anthropic", meter=m)

    out = wrapped.messages.create(model="claude-sonnet-4-6", messages=[])
    assert out is resp  # returns the real response, unchanged
    assert wrapped.api_key == "sk-real"  # non-instrumented attribute passes through
    assert client.messages.calls  # the underlying call actually ran

    _flushed(m)
    ev = cap.calls[0]["events"][0]
    assert ev["provider"] == "anthropic"
    assert ev["feature_id"] == "f1"
    assert ev["tokens_in"] == 10 and ev["tokens_out"] == 2
    assert isinstance(ev["latency_ms"], int) and ev["latency_ms"] >= 0


def test_wrap_merges_default_metadata():
    cap = _Capture()
    m = Meter(
        ingest_url="https://app.test/api/hook/events",
        token="tok",
        metadata={"environment": "prod"},
        transport=cap,
    )
    wrapped = wrap(_FakeAnthropic(_anthropic_resp()), provider="anthropic", meter=m)
    wrapped.messages.create()
    _flushed(m)
    assert cap.calls[0]["events"][0]["metadata"] == {"environment": "prod"}


def test_wrap_skips_streaming_or_async_responses():
    cap = _Capture()
    stream = iter([])  # no usage attribute -> looks like a stream
    m = _meter(cap)
    wrapped = wrap(_FakeAnthropic(stream), provider="anthropic", meter=m)
    out = wrapped.messages.create(stream=True)
    assert out is stream
    _flushed(m, timeout=0.3)
    assert cap.calls == []  # nothing recorded for a non-usage response


def test_detect_provider_from_client_module():
    for module, expected in [
        ("anthropic.resources.messages", "anthropic"),
        ("openai._client", "openai"),
        ("google.genai", "google"),
    ]:
        cls = type("Client", (), {})
        cls.__module__ = module
        assert _detect_provider(cls()) == expected


# --- optimize mode (opt spec M-opt-2) --------------------------------------


def _opt_meter(capture, **kw):
    # salt is supplied directly so the optimizer never hits the network in tests.
    kw.setdefault("salt", "test-salt")
    return Meter(
        ingest_url="https://app.test/api/hook/events",
        token="tok",
        feature_id="f1",
        optimize=True,
        transport=capture,
        **kw,
    )


def _stamped(event):
    """Strip (and check) the delivery timestamp every event now carries."""
    event = dict(event)
    assert event.pop("occurred_at").endswith("Z"), "event was not timestamped"
    return event


def _all_events(cap):
    return [ev for call in cap.calls for ev in call["events"]]


def test_optimize_is_off_by_default():
    cap = _Capture()
    m = _meter(cap)
    assert m._optimizer is None
    wrapped = wrap(_FakeAnthropic(_anthropic_resp()), provider="anthropic", meter=m)
    wrapped.messages.create(model="claude-sonnet-4-6", messages=[{"role": "user", "content": "hi"}])
    _flushed(m)
    assert "signal" not in cap.calls[0]["events"][0]


def test_optimize_flags_a_duplicate_call():
    cap = _Capture()
    m = _opt_meter(cap)
    wrapped = wrap(_FakeAnthropic(_anthropic_resp()), provider="anthropic", meter=m)
    req = {"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "same"}]}

    wrapped.messages.create(**req)
    wrapped.messages.create(**req)
    _flushed(m)  # both events ride one batch; queue order is preserved

    events = _all_events(cap)
    # The first call is novel (no signal); the repeat carries a duplicate signal.
    assert "signal" not in events[0]
    dup = events[1]["signal"]
    assert dup["kind"] == "duplicate" and dup["count"] == 1
    assert len(dup["fingerprint"]) == 64  # sha256 hex, salted — no prompt text


def test_optimize_emits_prefix_summaries_on_flush():
    cap = _Capture()
    m = _opt_meter(cap, optimize_flush_interval=0.0)  # flush every call, for the test
    wrapped = wrap(_FakeAnthropic(_anthropic_resp()), provider="anthropic", meter=m)
    wrapped.messages.create(
        model="claude-sonnet-4-6",
        system="You are a security triage assistant. " * 50,
        messages=[{"role": "user", "content": "alert 1"}],
    )
    _flushed(m)

    events = _all_events(cap)
    prefixes = [e for e in events if e.get("signal", {}).get("kind") == "prefix"]
    assert len(prefixes) == 1
    sig = prefixes[0]["signal"]
    assert sig["count"] == 1
    assert sig["prefix_tokens"] > 0  # the large static system prompt was measured
    assert len(sig["fingerprint"]) == 64


def test_optimize_emits_nothing_without_a_salt():
    cap = _Capture()
    # An empty salt models a failed salt fetch — never emit unsalted fingerprints.
    m = _opt_meter(cap, optimize_flush_interval=0.0, salt="")
    wrapped = wrap(_FakeAnthropic(_anthropic_resp()), provider="anthropic", meter=m)
    req = {"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "x"}]}
    wrapped.messages.create(**req)
    wrapped.messages.create(**req)
    _flushed(m)
    assert all("signal" not in e for e in _all_events(cap))


# --- delivery: queue, batching, bounds, shutdown ---------------------------


def test_events_are_batched_into_one_request():
    """The point of the queue: many calls, few requests."""
    cap = _Capture()
    m = _meter(cap)
    for i in range(20):
        m.record(provider="openai", model="gpt-4o", tokens_in=i, tokens_out=1)
    m.flush()

    assert len(cap.calls) == 1, "20 events should not be 20 requests"
    assert len(cap.calls[0]["events"]) == 20
    # Order is preserved, so a duplicate signal still follows the call it repeats.
    assert [e["tokens_in"] for e in cap.calls[0]["events"]] == list(range(20))


def test_a_batch_is_capped_at_batch_size():
    cap = _Capture()
    m = _meter(cap, batch_size=5)
    for _ in range(12):
        m.record(provider="openai", model="gpt-4o", tokens_in=1, tokens_out=1)
    m.flush()

    assert [len(c["events"]) for c in cap.calls] == [5, 5, 2]


def test_recording_does_not_wait_on_the_network():
    """The call path must not pay for a slow — or hung — ingest endpoint."""
    releases = threading.Event()
    m = Meter(
        ingest_url="https://app.test/api/hook/events",
        token="tok",
        transport=lambda *a: releases.wait(5),  # a transport that will not return
    )

    start = time.monotonic()
    for _ in range(100):
        m.record(provider="openai", model="gpt-4o", tokens_in=1, tokens_out=1)
    elapsed = time.monotonic() - start

    assert elapsed < 0.5, f"recording blocked for {elapsed:.2f}s behind the transport"
    releases.set()


def test_a_full_queue_drops_oldest_and_counts_it():
    """Bounded memory: a stalled endpoint can never grow the queue without limit."""
    blocked = threading.Event()
    cap = _Capture()

    def _stall(url, headers, body):
        blocked.wait(5)
        cap(url, headers, body)

    m = Meter(
        ingest_url="https://app.test/api/hook/events",
        token="tok",
        transport=_stall,
        queue_max=10,
        batch_size=1,
    )
    for i in range(200):
        m.record(provider="openai", model="gpt-4o", tokens_in=i, tokens_out=1)

    assert m.dropped > 0
    assert len(m._queue) <= 10  # noqa: SLF001  (the bound is the point)
    blocked.set()


def test_record_never_raises_even_when_delivery_is_broken(monkeypatch):
    """Queueing is on the call path, so it must be as fail-safe as sending."""
    m = _meter(_Capture())

    def _boom(self):
        raise RuntimeError("no threads left")

    monkeypatch.setattr(threading.Thread, "start", _boom)
    m.record(provider="openai", model="gpt-4o", tokens_in=1, tokens_out=1)  # must not raise
    assert m.flush(timeout=0.1) is False  # nothing can drain it, and it says so


def test_every_event_is_timestamped_when_it_happens_not_when_it_is_sent():
    """Delivery is deferred, and the server bills by occurred_at.

    Without a stamp at record time a call made at 23:59 on the last day of a
    month could be posted seconds later and land in the following month.
    """
    cap = _Capture()
    m = _meter(cap)
    m.record(provider="openai", model="gpt-4o", tokens_in=1, tokens_out=1)
    recorded = time.time()
    time.sleep(0.05)
    m.flush()

    stamp = cap.calls[0]["events"][0]["occurred_at"]
    sent_at = dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    assert abs(sent_at.timestamp() - recorded) < 5

    # An explicit occurred_at still wins — backfilling stays possible.
    m.record(
        provider="openai",
        model="gpt-4o",
        tokens_in=1,
        tokens_out=1,
        occurred_at="2026-01-15T10:00:00Z",
    )
    m.flush()
    assert cap.calls[-1]["events"][-1]["occurred_at"] == "2026-01-15T10:00:00Z"


def test_only_one_worker_thread_regardless_of_volume():
    """SDK overhead is constant, not a function of the customer's traffic."""
    cap = _Capture()
    m = _meter(cap)
    before = threading.active_count()
    for _ in range(500):
        m.record(provider="openai", model="gpt-4o", tokens_in=1, tokens_out=1)
    m.flush()

    assert threading.active_count() - before <= 1


def test_flush_returns_true_when_there_is_nothing_to_send():
    assert _meter(_Capture()).flush() is True
    assert Meter().flush() is True  # unconfigured -> no-op


def test_unconfigured_meter_queues_nothing():
    m = Meter()  # no url/token
    assert m.record(provider="openai", model="gpt-4o", tokens_in=1, tokens_out=1) is None
    assert len(m._queue) == 0  # noqa: SLF001


# --- retries: safe because every attempt carries the same batch id ---------


def _http_error(code):
    return urllib.error.HTTPError("https://app.test", code, "boom", {}, None)


def test_every_batch_carries_an_id_and_retries_reuse_it():
    """The property that makes retrying safe at all.

    The server applies the first delivery of a batch id and recognises the rest
    as replays. If a retry invented a new id it would be applied again, and
    because costing is additive that silently doubles a feature's spend.
    """
    seen = []

    def _fail_twice(url, headers, body):
        payload = json.loads(body)
        seen.append(payload["batch_id"])
        if len(seen) < 3:
            raise TimeoutError("ingest is waking up")

    m = _meter(_fail_twice, retry_backoff=0.0)
    m.record(provider="openai", model="gpt-4o", tokens_in=1, tokens_out=1)
    m.flush(timeout=5)

    assert len(seen) == 3, "did not retry"
    assert len(set(seen)) == 1, f"retries invented new batch ids: {set(seen)}"
    assert len(seen[0]) >= 8


def test_separate_batches_get_separate_ids():
    cap = _Capture()
    m = _meter(cap, batch_size=1)
    m.record(provider="openai", model="gpt-4o", tokens_in=1, tokens_out=1)
    m.record(provider="openai", model="gpt-4o", tokens_in=2, tokens_out=1)
    m.flush(timeout=5)

    ids = {json.loads(json.dumps(c))["events"] and c["batch_id"] for c in cap.calls}
    assert len(ids) == 2, "distinct batches must not share an id"


def test_a_transient_failure_is_retried_and_then_delivered():
    """The Render-sleep case: the first attempts time out, a later one lands."""
    attempts = []

    def _wake_up(url, headers, body):
        attempts.append(1)
        if len(attempts) < 2:
            raise TimeoutError("service is asleep")

    m = _meter(_wake_up, retry_backoff=0.0)
    m.record(provider="openai", model="gpt-4o", tokens_in=1, tokens_out=1)
    m.flush(timeout=5)

    assert len(attempts) == 2
    assert m.dropped == 0  # delivered, not lost


def test_a_client_error_is_not_retried():
    """A bad token or a malformed event fails identically forever."""
    attempts = []

    def _reject(url, headers, body):
        attempts.append(1)
        raise _http_error(401)

    m = _meter(_reject, retry_backoff=0.0)
    m.record(provider="openai", model="gpt-4o", tokens_in=1, tokens_out=1)
    m.flush(timeout=5)

    assert len(attempts) == 1, "retried a permanent failure"
    assert m.dropped == 1  # counted, not silently forgotten


def test_rate_limiting_is_retried():
    """429 is the one 4xx that explicitly invites coming back."""
    attempts = []

    def _throttle(url, headers, body):
        attempts.append(1)
        raise _http_error(429)

    m = _meter(_throttle, retry_backoff=0.0)
    m.record(provider="openai", model="gpt-4o", tokens_in=1, tokens_out=1)
    m.flush(timeout=5)
    assert len(attempts) == 3


def test_giving_up_counts_the_loss_and_keeps_serving():
    """After the last attempt the batch is dropped — and the meter carries on."""
    cap = _Capture()
    calls = {"n": 0}

    def _down_then_up(url, headers, body):
        calls["n"] += 1
        if calls["n"] <= 3:  # the whole first batch's attempts fail
            raise TimeoutError("down")
        cap(url, headers, body)

    m = _meter(_down_then_up, retry_backoff=0.0, batch_size=1)
    m.record(provider="openai", model="gpt-4o", tokens_in=1, tokens_out=1)
    m.flush(timeout=5)
    assert m.dropped == 1
    assert cap.calls == []

    m.record(provider="openai", model="gpt-4o", tokens_in=2, tokens_out=1)
    m.flush(timeout=5)
    assert len(cap.calls) == 1, "the worker died with the failed batch"
    assert cap.calls[0]["events"][0]["tokens_in"] == 2
