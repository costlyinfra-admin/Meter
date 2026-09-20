"""Optimize mode: measured signals, and the text that must never ride along.

The whole point of this mode is that Meter's duplicate-call and prompt-caching
findings are *measured* rather than rules of thumb. The whole risk of it is that
it is the one part of the SDK that looks at a request body at all — so most of
what follows is about what does not leave the process.
"""

from __future__ import annotations

import json
import pathlib
from unittest import mock

import pytest

from costlyinfra_meter import (
    CANON_VERSION,
    Meter,
    UnsupportedRequest,
    _Optimizer,
    _Scope,
    canonical_request,
)

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


def test_a_call_shape_v1_could_not_read_is_now_compared_properly():
    """v1 read messages / input / contents and gave up on anything else.

    A legacy completion call has none of them, so it normalized to "" and was
    dropped — no duplicate detection at all. The canonical form reads the whole
    request, so these compare on their merits: two different prompts are two
    different calls, and two identical ones are a repeat.
    """
    t = Captured("pepper")
    m = meter(t, optimize=True, salt="pepper")
    client = client_for(m)
    client.messages.create(model="m", prompt="a legacy completion call")
    client.messages.create(model="m", prompt="a different one entirely")
    drain(m)
    assert t.signals("duplicate") == []

    client.messages.create(model="m", prompt="a legacy completion call")
    drain(m)
    assert len(t.signals("duplicate")) == 1


def test_a_request_this_cannot_read_is_never_called_a_duplicate():
    """Unreadable must never degrade into identical.

    A cyclic structure, or an object with no documented conversion, cannot be
    compared. v1's `str()` fallback turned both into a shared string — so two
    genuinely different requests matched. These now emit no duplicate at all.
    """
    t = Captured("pepper")
    m = meter(t, optimize=True, salt="pepper")
    client = client_for(m)

    cyclic: dict = {"role": "user"}
    cyclic["self"] = cyclic
    for _ in range(2):
        client.messages.create(model="m", messages=[cyclic])
    drain(m)
    assert t.signals("duplicate") == []

    class Opaque:
        def __str__(self):
            return "obj"  # v1 collapsed every instance onto this

    for value in (1, 2):
        client.messages.create(model="m", messages=[Opaque()])
    drain(m)
    assert t.signals("duplicate") == []


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


# --- request identity (v2) -------------------------------------------------
#: Every one of these changes what the model returns. v1 hashed only the
#: messages, so all seven produced the SAME fingerprint as the base request and
#: were reported as avoidable repeats.
OUTPUT_AFFECTING = [
    ("temperature", {"temperature": 0.9}),
    ("system prompt", {"system": "You are terse."}),
    ("tools", {"tools": [{"name": "search"}]}),
    ("tool choice", {"tool_choice": "required"}),
    ("max tokens", {"max_tokens": 32}),
    ("stop sequences", {"stop": ["STOP"]}),
    ("response format", {"response_format": {"type": "json_object"}}),
    ("json schema", {"response_format": {"type": "json_schema", "json_schema": {"name": "a"}}}),
    ("seed", {"seed": 7}),
    ("top_p", {"top_p": 0.5}),
    ("reasoning effort", {"reasoning_effort": "high"}),
    ("thinking budget", {"thinking": {"type": "enabled", "budget_tokens": 2048}}),
    ("conversation state", {"previous_response_id": "resp_42"}),
    ("a parameter nobody here has heard of", {"future_provider_knob": "on"}),
]


@pytest.mark.parametrize("label,extra", OUTPUT_AFFECTING, ids=[c[0] for c in OUTPUT_AFFECTING])
def test_a_field_that_changes_the_output_changes_the_identity(label, extra):
    base = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    assert canonical_request(base) != canonical_request({**base, **extra}), label


def test_identical_requests_in_one_scope_and_window_are_one_repeat():
    t = Captured("pepper")
    m = meter(t, optimize=True, salt="pepper")
    client = client_for(m)
    request = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "temperature": 0.2}
    for _ in range(3):
        client.messages.create(**request)
    drain(m)

    duplicates = t.signals("duplicate")
    assert len(duplicates) == 2  # the 2nd and 3rd calls, not the 1st
    assert all(d["fingerprint_version"] == "v2" for d in duplicates)


def test_credentials_and_transport_settings_are_not_part_of_identity():
    """A rotated key must not reshape every identity, and no fingerprint may be
    derived from a secret."""
    base = {"model": "m", "messages": [{"content": "hi"}]}
    noisy = {**base, "api_key": "sk-secret", "timeout": 30, "max_retries": 9,
             "headers": {"X-Trace": "abc"}, "base_url": "https://example"}
    assert canonical_request(base) == canonical_request(noisy)
    assert "sk-secret" not in canonical_request(noisy)


def test_message_order_and_text_are_preserved_exactly():
    a = {"messages": [{"content": "one"}, {"content": "two"}]}
    b = {"messages": [{"content": "two"}, {"content": "one"}]}
    assert canonical_request(a) != canonical_request(b)
    assert '"one"' in canonical_request(a)


def test_absent_is_not_null():
    assert canonical_request({"messages": [], "stop": None}) != canonical_request({"messages": []})


def test_oversized_and_cyclic_requests_fail_safely_rather_than_matching():
    cyclic: dict = {}
    cyclic["self"] = cyclic
    with pytest.raises(UnsupportedRequest):
        canonical_request({"messages": [cyclic]})

    deep: dict = {"end": True}
    for _ in range(80):
        deep = {"n": deep}
    with pytest.raises(UnsupportedRequest):
        canonical_request({"messages": [deep]})

    with pytest.raises(UnsupportedRequest):
        canonical_request({"messages": [{"content": object()}]})

    with pytest.raises(UnsupportedRequest):
        canonical_request({"temperature": float("nan")})


def test_the_golden_vectors_match_this_implementation():
    """The same file the Node suite reads. If the two ever disagree, a company
    running both languages is told two different stories about one request."""
    golden = json.loads(
        (pathlib.Path(__file__).resolve().parents[2] / "golden" / "request-fingerprints.json")
        .read_text(encoding="utf-8")
    )
    assert golden["canon_version"] == CANON_VERSION
    assert len(golden["vectors"]) >= 10
    for vector in golden["vectors"]:
        assert canonical_request(vector["request"]) == vector["canonical"], vector["name"]


# --- comparison scope ------------------------------------------------------
def test_two_customers_sending_the_same_prompt_are_not_one_avoidable_call():
    t = Captured("pepper")
    m = meter(t, optimize=True, salt="pepper")

    class Anthropic:
        class messages:
            @staticmethod
            def create(**kw):
                return Response()

    client = Anthropic()
    request = {"model": "m", "messages": [{"content": "hi"}]}
    m.wrap(client, feature_id="f", customer_id="acme").messages.create(**request)
    m.wrap(client, feature_id="f", customer_id="globex").messages.create(**request)
    drain(m)

    assert t.signals("duplicate") == []


@pytest.mark.parametrize(
    "field,other",
    [
        ("application", {"application": "other-app"}),
        ("feature_id", {"feature_id": "other-feature"}),
        ("cache_scope", {"cache_scope": "tenant-b"}),
    ],
)
def test_a_different_scope_is_a_different_call(field, other):
    collector = _Optimizer(meter(Captured("pepper"), salt="pepper"))
    request = {"model": "m", "messages": [{"content": "hi"}]}
    base = _Scope(application="app", feature_id="f", operation="messages.create",
                  environment="production", customer_id="acme")
    assert collector.on_call("anthropic", "m", request, {}, scope=base) is None
    assert collector.on_call("anthropic", "m", request, {}, scope=base._replace(**other)) is None


def test_an_unscoped_repeat_is_recorded_but_not_called_safe():
    """Absent scope is absent, never permission. A repeat with nobody saying the
    two calls belong to the same user is still a repeat — it just cannot be
    presented as a safe reuse."""
    collector = _Optimizer(meter(Captured("pepper"), salt="pepper"))
    request = {"model": "m", "messages": [{"content": "hi"}]}
    collector.on_call("anthropic", "m", request, {}, scope=_Scope(application="app"))
    signal = collector.on_call("anthropic", "m", request, {}, scope=_Scope(application="app"))
    assert signal["scope_kind"] == "unscoped"

    scoped = _Scope(application="app", customer_id="acme")
    collector.on_call("anthropic", "m", request, {}, scope=scoped)
    assert collector.on_call("anthropic", "m", request, {}, scope=scoped)["scope_kind"] == "explicit"


def test_the_response_model_is_preferred_and_the_request_model_is_the_fallback():
    """v1 used the response alone and fell back to "", so two DIFFERENT models
    shared one identity whenever the response shape was unfamiliar."""
    collector = _Optimizer(meter(Captured("pepper"), salt="pepper"))
    assert collector.on_call("anthropic", "", {"model": "haiku", "messages": []}, {}) is None
    # A different requested model is a different call, even with no response model.
    assert collector.on_call("anthropic", "", {"model": "opus", "messages": []}, {}) is None
    # ...and the same one repeats.
    assert collector.on_call("anthropic", "", {"model": "haiku", "messages": []}, {}) is not None


# --- the window ------------------------------------------------------------
def test_a_repeat_after_the_window_is_not_an_avoidable_call():
    clock = {"t": 1000.0}
    collector = _Optimizer(meter(Captured("pepper"), salt="pepper"), window=600.0)
    request = {"model": "m", "messages": [{"content": "hi"}]}

    with mock.patch("costlyinfra_meter.time.monotonic", lambda: clock["t"]):
        assert collector.on_call("anthropic", "m", request, {}) is None  # first
        clock["t"] += 599.0
        assert collector.on_call("anthropic", "m", request, {}) is not None  # inside
        clock["t"] += 2.0  # 601s from the FIRST call
        assert collector.on_call("anthropic", "m", request, {}) is None  # expired
        # ...and that expiry opened a new group, so the next one repeats again.
        clock["t"] += 1.0
        assert collector.on_call("anthropic", "m", request, {}) is not None


def test_steady_traffic_cannot_keep_one_cached_response_alive_forever():
    """The window runs from the FIRST call of a group. If each repeat pushed it
    out, a request made every minute would claim a six-hour-old response was
    still good."""
    clock = {"t": 0.0}
    collector = _Optimizer(meter(Captured("pepper"), salt="pepper"), window=600.0)
    request = {"model": "m", "messages": [{"content": "hi"}]}

    with mock.patch("costlyinfra_meter.time.monotonic", lambda: clock["t"]):
        collector.on_call("anthropic", "m", request, {})
        for _ in range(9):
            clock["t"] += 60.0  # t = 60 .. 540, all inside the group's window
            assert collector.on_call("anthropic", "m", request, {}) is not None
        clock["t"] += 61.0  # t = 601, past the group's start by more than 600
        assert collector.on_call("anthropic", "m", request, {}) is None


def test_the_window_boundary_is_inclusive():
    """Stated, not incidental: a repeat at exactly the window is still inside
    it, and one microsecond later is not."""
    clock = {"t": 0.0}
    collector = _Optimizer(meter(Captured("pepper"), salt="pepper"), window=600.0)
    request = {"model": "m", "messages": [{"content": "hi"}]}

    with mock.patch("costlyinfra_meter.time.monotonic", lambda: clock["t"]):
        collector.on_call("anthropic", "m", request, {})
        clock["t"] = 600.0
        assert collector.on_call("anthropic", "m", request, {}) is not None

    clock2 = {"t": 0.0}
    collector2 = _Optimizer(meter(Captured("pepper"), salt="pepper"), window=600.0)
    with mock.patch("costlyinfra_meter.time.monotonic", lambda: clock2["t"]):
        collector2.on_call("anthropic", "m", request, {})
        clock2["t"] = 600.000001
        assert collector2.on_call("anthropic", "m", request, {}) is None
