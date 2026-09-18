"""Optimize mode: measured signals, and the text that must never ride along.

The whole point of this mode is that Meter's duplicate-call and prompt-caching
findings are *measured* rather than rules of thumb. The whole risk of it is that
it is the one part of the SDK that looks at a request body at all — so most of
what follows is about what does not leave the process.
"""

from __future__ import annotations

import json

from costlyinfra_meter import Meter

URL = "https://meter.example/api/hook/events"
SECRET = "the quick brown fox jumped over the lazy dog"


class Captured:
    """Records ingest posts; answers the salt endpoint when one is configured."""

    def __init__(self, salt: str | None = None):
        self.batches: list = []
        self.salt_calls = 0
        self._salt = salt

    def __call__(self, url, headers, body):
        if url.endswith("/salt"):
            self.salt_calls += 1
            if self._salt is None:
                return 500, b"{}"
            return 200, json.dumps({"salt": self._salt}).encode()
        self.batches.append(json.loads(body.decode()))

    @property
    def events(self) -> list:
        return [e for b in self.batches for e in b["events"]]

    def signals(self, kind: str | None = None) -> list:
        out = [e["signal"] for e in self.events if e.get("signal")]
        return [s for s in out if kind is None or s["kind"] == kind]

    @property
    def raw(self) -> str:
        return json.dumps(self.batches)


class Response:
    """An Anthropic-shaped response."""

    def __init__(self, model="claude-sonnet-4-6", **usage):
        self.model = model
        self.usage = type("U", (), {
            "input_tokens": usage.get("input_tokens", 1000),
            "output_tokens": usage.get("output_tokens", 200),
            "cache_read_input_tokens": usage.get("cache_read", 0),
            "cache_creation_input_tokens": usage.get("cache_write", 0),
        })()


def client_for(meter, response=None):
    class Anthropic:
        class messages:
            @staticmethod
            def create(**kw):
                return response or Response()

    return meter.wrap(Anthropic(), feature_id="answer-generation")


def meter(transport, **kw) -> Meter:
    kw.setdefault("flush_interval", 0.01)
    kw.setdefault("optimize_flush_interval", 0.0)  # flush prefix counters every call
    return Meter(
        application="support-agent",
        ingest_url=URL,
        token="tok",
        feature_id="answer-generation",
        transport=transport,
        **kw,
    )


def drain(m: Meter) -> None:
    assert m.flush(timeout=3.0)


# --- off by default --------------------------------------------------------
def test_a_wrapped_call_sends_no_signal_unless_optimize_is_asked_for():
    t = Captured("pepper")
    m = meter(t)
    client_for(m).messages.create(model="claude-sonnet-4-6", messages=[{"role": "user"}])
    drain(m)

    assert t.signals() == []
    assert t.salt_calls == 0  # and nothing was fetched for it either


# --- duplicates ------------------------------------------------------------
def test_the_same_request_twice_reports_the_second_as_avoidable():
    t = Captured("pepper")
    m = meter(t, optimize=True, salt="pepper")
    client = client_for(m)
    request = {"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "hi"}]}
    client.messages.create(**request)
    client.messages.create(**request)
    drain(m)

    duplicates = t.signals("duplicate")
    assert len(duplicates) == 1  # the repeat, not the original
    assert duplicates[0]["count"] == 1
    assert len(duplicates[0]["fingerprint"]) == 64


def test_two_different_requests_are_not_duplicates_of_each_other():
    t = Captured("pepper")
    m = meter(t, optimize=True, salt="pepper")
    client = client_for(m)
    client.messages.create(model="m", messages=[{"role": "user", "content": "one"}])
    client.messages.create(model="m", messages=[{"role": "user", "content": "two"}])
    drain(m)

    assert t.signals("duplicate") == []


def test_a_duplicate_rides_on_the_span_the_call_was_already_sending():
    t = Captured("pepper")
    m = meter(t, optimize=True, salt="pepper")
    client = client_for(m)
    request = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    client.messages.create(**request)
    client.messages.create(**request)
    drain(m)

    carrier = [e for e in t.events if (e.get("signal") or {}).get("kind") == "duplicate"]
    assert len(carrier) == 1
    # Not an extra event of its own: it is the completed span, with its tokens.
    assert carrier[0]["event_type"] == "span.completed"
    assert carrier[0]["tokens_in"] == 1000


# --- prefixes --------------------------------------------------------------
def test_a_shared_static_head_is_summarised_as_its_own_span():
    t = Captured("pepper")
    m = meter(t, optimize=True, salt="pepper")
    client = client_for(m)
    for i in range(3):
        client.messages.create(
            model="m",
            system="You are a security analyst. " * 100,
            messages=[{"role": "user", "content": f"question {i}"}],
        )
    drain(m)

    prefixes = t.signals("prefix")
    assert prefixes, "no prefix summary was flushed"
    assert sum(p["count"] for p in prefixes) == 3
    assert all(len(p["fingerprint"]) == 64 for p in prefixes)
    # The summary is its own span, and it is not a priced call being re-reported.
    summary = [e for e in t.events if (e.get("signal") or {}).get("kind") == "prefix"][0]
    assert summary["event_type"] == "span.completed"
    assert summary["operation_name"] == "optimize.prefix"
    assert "tokens_in" not in summary  # only inside the signal, where it is a sum


def test_a_call_served_from_cache_is_not_counted_as_a_caching_opportunity():
    t = Captured("pepper")
    m = meter(t, optimize=True, salt="pepper")
    client = client_for(m, Response(cache_read=900))
    client.messages.create(model="m", system="static", messages=[{"role": "user"}])
    drain(m)

    assert t.signals("prefix")[0]["cached_count"] == 1


def test_the_prefix_size_is_the_providers_own_count_when_the_provider_gives_one():
    t = Captured("pepper")
    m = meter(t, optimize=True, salt="pepper")
    client = client_for(m, Response(cache_write=4242))
    client.messages.create(model="m", system="static block", messages=[{"role": "user"}])
    drain(m)

    signal = t.signals("prefix")[0]
    assert signal["prefix_tokens"] == 4242
    assert signal["prefix_measured"] is True


def test_the_prefix_size_is_declared_an_estimate_when_the_provider_gives_none():
    t = Captured("pepper")
    m = meter(t, optimize=True, salt="pepper")
    client = client_for(m)  # no cache_creation_input_tokens
    client.messages.create(model="m", system="s" * 400, messages=[{"role": "user"}])
    drain(m)

    signal = t.signals("prefix")[0]
    assert signal["prefix_measured"] is False
    # Characters over four, which is why it may not be called measured.
    assert 0 < signal["prefix_tokens"] < 400


# --- the privacy boundary --------------------------------------------------
def test_no_part_of_a_request_or_reply_is_transmitted():
    t = Captured("pepper")
    m = meter(t, optimize=True, salt="pepper")
    client = client_for(m)
    request = {
        "model": "m",
        "system": f"SYSTEM {SECRET}",
        "tools": [{"name": f"tool_{SECRET}"}],
        "messages": [{"role": "user", "content": SECRET}],
        "metadata": {"user": "alice@example.com"},
    }
    client.messages.create(**request)
    client.messages.create(**request)  # and again, to force a duplicate signal
    drain(m)

    assert t.signals("duplicate") and t.signals("prefix")  # both kinds were sent
    body = t.raw
    for leak in (SECRET, "alice@example.com", "SYSTEM", "tool_"):
        assert leak not in body, f"{leak!r} reached the wire"


def test_a_fingerprint_is_salted_so_two_tenants_never_agree():
    prints = []
    for salt in ("pepper", "paprika"):
        t = Captured(salt)
        m = meter(t, optimize=True, salt=salt)
        client = client_for(m)
        request = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        client.messages.create(**request)
        client.messages.create(**request)
        drain(m)
        prints.append(t.signals("duplicate")[0]["fingerprint"])

    assert prints[0] != prints[1]


def test_without_a_salt_nothing_is_fingerprinted_at_all():
    t = Captured(salt=None)  # the salt endpoint fails
    m = meter(t, optimize=True)
    client = client_for(m)
    request = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    for _ in range(4):
        client.messages.create(**request)
        drain(m)

    assert t.signals() == []
    # Asked once and then left alone: an unsalted hash is never the fallback.
    assert t.salt_calls == 1


def test_the_salt_is_fetched_off_the_callers_thread():
    t = Captured("pepper")
    m = meter(t, optimize=True)
    client = client_for(m)
    request = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}

    # The first call cannot have waited for a salt it had not fetched yet, so it
    # carries no signal — and the call itself still completed normally.
    client.messages.create(**request)
    drain(m)
    assert t.signals() == []
    assert [e["event_type"] for e in t.events][-1] == "trace.completed"

    for _ in range(50):
        if m.salt():
            break
        import time

        time.sleep(0.02)
    assert m.salt() == "pepper"

    client.messages.create(**request)
    client.messages.create(**request)
    drain(m)
    assert t.signals("duplicate"), "signals never started once the salt arrived"


def test_a_provider_call_still_returns_its_response_when_optimizing():
    t = Captured("pepper")
    m = meter(t, optimize=True, salt="pepper")
    original = Response()
    assert client_for(m, original).messages.create(messages=[]) is original


def test_the_map_of_seen_requests_cannot_grow_with_traffic():
    """A fingerprint map keyed by request shape is a leak unless it is capped."""
    from costlyinfra_meter import _Optimizer

    collector = _Optimizer(meter(Captured("pepper"), salt="pepper"), dup_capacity=10)
    for i in range(400):
        collector.on_call("anthropic", "m", {"messages": [{"c": i}]}, {})
    assert len(collector._seen) == 10

    # And the oldest went first, so a request repeated inside the window is
    # still caught while an ancient one is not.
    recent = {"messages": [{"c": 399}]}
    assert collector.on_call("anthropic", "m", recent, {})["kind"] == "duplicate"
    assert collector.on_call("anthropic", "m", {"messages": [{"c": 0}]}, {}) is None


def test_a_request_shape_this_cannot_read_is_never_called_a_duplicate():
    """Two calls it cannot compare are not two calls it knows to be the same.

    `_normalize` reads messages / input / contents. A call with none of them
    normalizes to "" — and every such call would then share one fingerprint, so
    the second would be reported as an avoidable repeat of the first. That is a
    finding about this code, not about the customer's traffic.
    """
    t = Captured("pepper")
    m = meter(t, optimize=True, salt="pepper")
    client = client_for(m)
    client.messages.create(model="m", prompt="a legacy completion call")
    client.messages.create(model="m", prompt="a different one entirely")
    drain(m)

    assert t.signals() == []


def test_a_prefix_is_attributed_to_the_feature_the_call_was_for():
    """`wrap(feature_id=...)` can name a feature the meter's default does not."""
    t = Captured("pepper")
    m = meter(t, optimize=True, salt="pepper")  # meter default: answer-generation

    class Anthropic:
        class messages:
            @staticmethod
            def create(**kw):
                return Response()

    client = m.wrap(Anthropic(), feature_id="threat-triage")
    client.messages.create(model="m", system="static", messages=[{"role": "user"}])
    drain(m)

    summary = [e for e in t.events if (e.get("signal") or {}).get("kind") == "prefix"][0]
    assert summary["feature_id"] == "threat-triage"


def test_prefix_counters_are_not_lost_when_a_short_process_ends():
    """A script, a batch job, one serverless invocation.

    Prefix counters leave on a 60-second timer. A process that does not live
    that long would take every one of them to the grave — which is most of the
    traffic this feature exists to measure — so a flush takes them too.
    """
    t = Captured("pepper")
    m = meter(t, optimize=True, salt="pepper", optimize_flush_interval=3600.0)
    client = client_for(m)
    for i in range(3):
        client.messages.create(model="m", system="static", messages=[{"content": i}])

    assert t.signals("prefix") == []  # the interval is nowhere near elapsed
    drain(m)
    prefixes = t.signals("prefix")
    assert prefixes and prefixes[0]["count"] == 3
