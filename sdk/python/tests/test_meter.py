"""The Python SDK: lifecycle, propagation, privacy, and never breaking the agent."""

from __future__ import annotations

import json
import pathlib
import re
import threading
import time

import costlyinfra_meter
import pytest
from costlyinfra_meter import Meter

URL = "https://meter.example/api/hook/events"


class Captured:
    """A transport that records what would have been posted."""

    def __init__(self, fail: int = 0, status: int = 0):
        self.batches: list = []
        self.calls = 0
        self._fail, self._status = fail, status

    def __call__(self, url, headers, body):
        self.calls += 1
        payload = json.loads(body.decode())
        self.batches.append(payload)
        if self.calls <= self._fail:
            if self._status:
                import urllib.error

                raise urllib.error.HTTPError(url, self._status, "no", {}, None)
            raise OSError("network")

    @property
    def events(self) -> list:
        return [e for b in self.batches for e in b["events"]]

    def of(self, kind: str) -> list:
        return [e for e in self.events if e["event_type"] == kind]


def meter(transport=None, **kw) -> Meter:
    kw.setdefault("flush_interval", 0.01)
    kw.setdefault("token", "tok")
    return Meter(
        application="support-agent",
        environment="production",
        ingest_url=URL,
        transport=transport or Captured(),
        **kw,
    )


def drain(m: Meter) -> None:
    assert m.flush(timeout=3.0)


class Response:
    """A provider response shaped like Anthropic's."""

    def __init__(self, model="claude-sonnet-4-6", **usage):
        self.model = model
        self.usage = type("U", (), {
            "input_tokens": usage.get("input_tokens", 1000),
            "output_tokens": usage.get("output_tokens", 200),
            "cache_read_input_tokens": usage.get("cache_read", 0),
            "cache_creation_input_tokens": usage.get("cache_write", 0),
        })()


# --- the simple case -------------------------------------------------------
def test_a_wrapped_call_produces_a_complete_one_span_trace():
    t = Captured()
    m = meter(t)

    class Anthropic:
        class messages:
            @staticmethod
            def create(**kw):
                return Response()

    client = m.wrap(Anthropic(), feature_id="answer-generation")
    client.messages.create(model="claude-sonnet-4-6", messages=[])
    drain(m)

    kinds = [e["event_type"] for e in t.events]
    assert kinds == ["trace.started", "span.started", "span.completed", "trace.completed"]
    done = t.of("span.completed")[0]
    assert done["tokens_in"] == 1000 and done["tokens_out"] == 200
    assert done["model"] == "claude-sonnet-4-6"
    assert done["feature_id"] == "answer-generation"
    assert done["span_kind"] == "llm"
    # One trace, one span: the ids agree across all four events.
    assert len({e["trace_id"] for e in t.events}) == 1


def test_a_wrapped_call_returns_the_provider_response_untouched():
    t = Captured()
    m = meter(t)
    original = Response()

    class Anthropic:
        class messages:
            @staticmethod
            def create(**kw):
                return original

    assert m.wrap(Anthropic()).messages.create() is original


def test_a_failing_provider_call_is_recorded_and_re_raised():
    t = Captured()
    m = meter(t)

    class Anthropic:
        class messages:
            @staticmethod
            def create(**kw):
                raise RuntimeError("upstream 500: secret-detail")

    with pytest.raises(RuntimeError):
        m.wrap(Anthropic()).messages.create()
    drain(m)

    assert [e["event_type"] for e in t.events][-2:] == ["span.failed", "trace.failed"]
    # The exception's text is the customer's; it is not ours to copy.
    assert "secret-detail" not in json.dumps(t.batches)


# --- multi-step agents -----------------------------------------------------
def test_an_agent_run_records_its_steps_in_order():
    t = Captured()
    m = meter(t)

    with m.agent("resolve-ticket", feature_id="f1", customer_id="customer-123") as run:
        run.llm("classify", lambda: Response(model="claude-haiku-4-5"))
        run.tool("retrieve-documents", lambda: ["doc"])
        run.llm("generate-answer", lambda: Response())
    drain(m)

    kinds = [e["event_type"] for e in t.events]
    assert kinds[0] == "trace.started"
    assert kinds[-1] == "trace.completed"
    completed = t.of("span.completed")
    assert [e["operation_name"] for e in completed] == [
        "classify", "retrieve-documents", "generate-answer"
    ]
    assert [e["span_kind"] for e in completed] == ["llm", "tool", "llm"]
    # A tool call has no tokens; only the model calls cost anything.
    assert "tokens_in" not in completed[1]
    assert all(e["customer_id"] == "customer-123" for e in completed)


def test_a_step_returns_its_own_value():
    m = meter()
    with m.agent("resolve") as run:
        assert run.tool("fetch", lambda: {"rows": 3}) == {"rows": 3}


def test_a_failing_step_fails_its_span_and_the_run_but_keeps_the_exception():
    t = Captured()
    m = meter(t)

    with pytest.raises(ValueError, match="boom"):
        with m.agent("resolve") as run:
            run.tool("explode", lambda: (_ for _ in ()).throw(ValueError("boom")))
    drain(m)

    assert len(t.of("span.failed")) == 1
    assert len(t.of("trace.failed")) == 1
    assert not t.of("trace.completed")


def test_a_crashed_agent_is_recorded_as_failed_not_left_looking_hung():
    t = Captured()
    m = meter(t)
    with pytest.raises(KeyError):
        with m.agent("resolve"):
            raise KeyError("nope")
    drain(m)
    assert t.of("trace.failed")


# --- heartbeats ------------------------------------------------------------
def test_a_short_run_sends_no_heartbeat_at_all():
    # Heartbeats exist for runs long enough to look hung. A fast one should
    # cost exactly two events.
    t = Captured()
    m = meter(t, heartbeat_interval=1.0)
    with m.agent("quick"):
        pass
    drain(m)
    assert not t.of("trace.heartbeat")
    assert [e["event_type"] for e in t.events] == ["trace.started", "trace.completed"]


def test_the_heartbeat_interval_has_a_floor():
    # A pathological config must not turn metering into a denial of service
    # against the customer's own ingest endpoint.
    m = meter(heartbeat_interval=0.001)
    assert m._heartbeat_interval >= 1.0


def test_heartbeats_stop_the_moment_the_run_ends():
    t = Captured()
    m = meter(t, heartbeat_interval=1.0)  # the floor; see the test above
    with m.agent("slow"):
        time.sleep(1.2)
    drain(m)
    beats_at_exit = len(t.of("trace.heartbeat"))
    assert beats_at_exit >= 1  # it did beat while working

    time.sleep(1.4)
    drain(m)
    # A completed run must never keep claiming to be alive.
    assert len(t.of("trace.heartbeat")) == beats_at_exit
    assert t.of("trace.completed")


def test_no_heartbeat_thread_outlives_the_run():
    m = meter(heartbeat_interval=1.0)
    before = threading.active_count()
    for _ in range(5):
        with m.agent("x"):
            pass
    time.sleep(0.3)
    # Timers are cancelled, not merely abandoned.
    assert threading.active_count() <= before + 2


# --- context propagation ---------------------------------------------------
def test_exported_context_carries_identifiers_and_nothing_else():
    m = meter(token="super-secret-token")
    with m.agent("resolve", feature_id="f1", customer_id="customer-123") as run:
        context = run.export_context()

    assert context["trace_id"] == run.trace_id
    assert context["application"] == "support-agent"
    assert context["feature_id"] == "f1"
    # A context goes on a queue, which means assuming it gets logged.
    blob = json.dumps(context)
    assert "super-secret-token" not in blob
    assert "customer-123" not in blob


def test_resuming_continues_the_same_trace_in_another_worker():
    t = Captured()
    producer = meter(t)
    with producer.agent("resolve-ticket", feature_id="f1") as run:
        context = run.export_context()
        run.tool("enqueue", lambda: None)

    consumer = meter(t)
    with consumer.resume(context) as resumed:
        resumed.tool("process-document", lambda: "done")
    drain(producer)
    drain(consumer)

    # Both processes wrote to one trace, which is the whole point.
    assert len({e["trace_id"] for e in t.events}) == 1
    assert resumed.trace_id == run.trace_id


def test_resumed_work_hangs_off_the_step_that_queued_it():
    t = Captured()
    m = meter(t)
    with m.agent("resolve") as run:
        context = {**run.export_context(), "parent_span_id": "queueing-step"}
    with m.resume(context) as resumed:
        resumed.tool("process", lambda: None)
    drain(m)

    child = [e for e in t.of("span.completed") if e["operation_name"] == "process"][0]
    assert child["parent_span_id"] == "queueing-step"


def test_a_context_survives_json_round_tripping():
    m = meter()
    with m.agent("resolve") as run:
        context = run.export_context()
    with m.resume(json.dumps(context)) as resumed:
        assert resumed.trace_id == run.trace_id


# --- privacy ---------------------------------------------------------------
def test_no_prompt_or_response_content_is_ever_serialized():
    t = Captured()
    m = meter(t)

    class Anthropic:
        class messages:
            @staticmethod
            def create(**kw):
                return Response()

    m.wrap(Anthropic()).messages.create(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "MY-SECRET-PROMPT"}],
        system="MY-SECRET-SYSTEM",
    )
    with m.agent("resolve") as run:
        run.tool("search", lambda: ["MY-SECRET-DOCUMENT"])
    drain(m)

    blob = json.dumps(t.batches)
    for secret in ("MY-SECRET-PROMPT", "MY-SECRET-SYSTEM", "MY-SECRET-DOCUMENT"):
        assert secret not in blob


def test_prompt_identity_travels_without_the_prompt():
    t = Captured()
    m = meter(t)
    with m.agent("resolve") as run:
        run.llm("answer", lambda: Response(), prompt_id="answer-ticket",
                prompt_version="5.0", prompt_hash="abc123")
    drain(m)

    done = t.of("span.completed")[0]
    assert (done["prompt_id"], done["prompt_version"]) == ("answer-ticket", "5.0")
    assert done["prompt_hash"] == "abc123"


def test_every_field_sent_is_one_the_server_allows():
    t = Captured()
    m = meter(t)
    with m.agent("resolve", customer_id="c1") as run:
        run.llm("answer", lambda: Response(cache_read=800, cache_write=200))
    drain(m)

    allowed = {
        "event_type", "event_id", "trace_id", "span_id", "parent_span_id", "span_kind",
        "operation_name", "application", "feature_id", "provider", "model", "tokens_in",
        "tokens_out", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
        "latency_ms", "prompt_id", "prompt_version", "prompt_hash", "environment",
        "release_version", "customer_id", "occurred_at",
    }
    for event in t.events:
        assert set(event) <= allowed, set(event) - allowed


# --- token extraction ------------------------------------------------------
def test_anthropic_cache_tokens_are_extracted():
    t = Captured()
    m = meter(t)
    with m.agent("x") as run:
        run.llm("call", lambda: Response(cache_read=6100, cache_write=1200))
    drain(m)
    done = t.of("span.completed")[0]
    assert done["cache_read_tokens"] == 6100
    assert done["cache_write_tokens"] == 1200
    assert done["provider"] == "anthropic"


def test_openai_reasoning_tokens_are_extracted():
    t = Captured()
    m = meter(t)

    class OpenAIResponse:
        model = "gpt-4o"
        usage = type("U", (), {
            "prompt_tokens": 900, "completion_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 400},
            "completion_tokens_details": {"reasoning_tokens": 64},
        })()

    with m.agent("x") as run:
        run.llm("call", lambda: OpenAIResponse())
    drain(m)
    done = t.of("span.completed")[0]
    assert (done["tokens_in"], done["tokens_out"]) == (900, 120)
    assert done["cache_read_tokens"] == 400
    assert done["reasoning_tokens"] == 64
    assert done["provider"] == "openai"


def test_an_unfamiliar_response_still_records_the_step():
    t = Captured()
    m = meter(t)
    with m.agent("x") as run:
        run.llm("call", lambda: object())
    drain(m)
    # Timing and status survive even when tokens cannot be read.
    done = t.of("span.completed")[0]
    assert done["operation_name"] == "call"
    assert "latency_ms" in done


def test_latency_is_measured_without_the_caller_doing_anything():
    t = Captured()
    m = meter(t)
    with m.agent("x") as run:
        run.tool("slow", lambda: time.sleep(0.05))
    drain(m)
    assert t.of("span.completed")[0]["latency_ms"] >= 40


# --- delivery --------------------------------------------------------------
def test_unconfigured_is_a_silent_no_op():
    m = Meter(application="x")  # no URL, no token
    assert not m.enabled
    with m.agent("resolve") as run:
        assert run.tool("work", lambda: 42) == 42
    assert m.flush() is True


def test_a_broken_endpoint_never_reaches_the_caller():
    def explode(url, headers, body):
        raise OSError("connection refused")

    m = meter(explode, max_attempts=1, retry_backoff=(0.0,))
    with m.agent("resolve") as run:
        assert run.tool("work", lambda: "value") == "value"
    m.flush(timeout=1.0)
    assert m.dropped > 0  # visible, not silent


def test_a_retry_reuses_one_batch_id_so_the_server_can_dedupe():
    t = Captured(fail=1)
    m = meter(t, retry_backoff=(0.0,))
    with m.agent("resolve") as run:
        run.llm("answer", lambda: Response())
    drain(m)

    assert t.calls >= 2
    # Identical id across attempts is what makes an ambiguous timeout safe.
    assert len({b["batch_id"] for b in t.batches}) == 1


def test_a_rejected_batch_is_dropped_rather_than_hammered():
    t = Captured(fail=99, status=400)
    m = meter(t, retry_backoff=(0.0,))
    with m.agent("resolve"):
        pass
    m.flush(timeout=1.0)
    # A 400 fails identically forever; retrying only hurts the endpoint.
    assert t.calls == 1
    assert m.dropped > 0


def test_the_queue_is_bounded_and_sheds_oldest_first():
    m = meter(queue_max=5, flush_interval=60.0)
    m._queue.clear()
    for i in range(50):
        m._send([{"event_type": "trace.heartbeat", "trace_id": f"t{i}"}])
    assert len(m._queue) <= 5
    assert m.dropped >= 45
    # The newest always gets in.
    assert m._queue[-1]["trace_id"] == "t49"


def test_flush_returns_true_when_the_queue_drains():
    t = Captured()
    m = meter(t)
    with m.agent("resolve") as run:
        run.llm("answer", lambda: Response())
    assert m.flush(timeout=3.0) is True
    assert t.events


def test_ids_are_generated_client_side_and_are_unique():
    m = meter()
    with m.agent("a") as one, m.agent("b") as two:
        assert one.trace_id != two.trace_id
        assert len(one.trace_id) >= 16


def test_environment_and_release_ride_on_every_event(monkeypatch):
    monkeypatch.setenv("METER_RELEASE_VERSION", "2026.9.1")
    t = Captured()
    m = Meter(application="support-agent", environment="staging", ingest_url=URL,
              token="tok", transport=t, flush_interval=0.01)
    with m.agent("resolve") as run:
        run.tool("x", lambda: None)
    drain(m)
    assert all(e["environment"] == "staging" for e in t.events)
    assert all(e["release_version"] == "2026.9.1" for e in t.events)


def test_configuration_comes_from_the_documented_environment(monkeypatch):
    for key, value in {
        "METER_INGEST_URL": URL,
        "METER_INGEST_TOKEN": "env-token",
        "METER_APPLICATION": "document-review",
        "METER_ENVIRONMENT": "staging",
    }.items():
        monkeypatch.setenv(key, value)
    m = Meter()
    assert m.enabled
    assert (m.application, m.environment) == ("document-review", "staging")


def test_the_version_the_sdk_reports_is_the_version_the_package_ships():
    """`__version__` and pyproject must be one number.

    Not a hardcoded literal, which has to be edited on every release and says
    nothing when it is. The invariant that matters is agreement: the constant a
    customer quotes in a bug report is what PyPI actually served them, and a
    half-finished bump — one file changed, the other not — fails here.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    manifest = (root / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"', manifest, re.MULTILINE)
    assert declared, "pyproject.toml has no version"
    assert costlyinfra_meter.__version__ == declared.group(1)
