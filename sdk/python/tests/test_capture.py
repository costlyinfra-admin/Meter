"""Consented prompt capture: off by default, nothing kept until the server says
capture is open, text only, and never at metering's expense."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import costlyinfra_meter as cm
from costlyinfra_meter import Meter

URL = "https://meter.example/api/hook/events"
CAPTURE = "https://meter.example/api/prompt-capture/samples"
OPEN = "https://meter.example/api/prompt-capture/open"
SECRET = "SECRET-PROMPT-TEXT"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("METER_CAPTURE_PROMPTS", raising=False)
    monkeypatch.delenv("METER_CAPTURE_URL", raising=False)


class Server:
    """Meter's side of both channels: events, the open check, and samples."""

    def __init__(self, open_=True, sample_status=200, reason=None, capture_down=False):
        self.open = open_
        self.sample_status = sample_status
        self.reason = reason
        self.capture_down = capture_down
        self.batches: list = []
        self.samples: list = []
        self.attempts = 0
        self.checks: list = []

    def __call__(self, url, headers, body):
        if url.startswith(OPEN):
            self.checks.append(url)
            return 200, {"open": self.open, "reason": None if self.open else "feature_not_enabled"}
        if url == CAPTURE:
            self.attempts += 1
            if self.capture_down:
                raise OSError("capture endpoint unreachable")
            if self.sample_status != 200:
                return self.sample_status, {"detail": "refused", "reason": self.reason}
            self.samples.append(json.loads(body.decode()))
            return 200, {"stored": True}
        self.batches.append(json.loads(body.decode()))
        return None

    @property
    def events(self) -> list:
        return [e for b in self.batches for e in b["events"]]


def meter(server, **kw) -> Meter:
    kw.setdefault("capture_prompts", True)
    kw.setdefault("capture_sample_rate", 1.0)
    return Meter(
        application="support-agent",
        environment="production",
        ingest_url=URL,
        token="tok",
        flush_interval=0.01,
        transport=server,
        **kw,
    )


def drain(m: Meter) -> None:
    assert m.flush(timeout=3.0)


def anthropic_reply(text="billing", usage=True):
    reply = SimpleNamespace(model="claude-sonnet-4-6",
                            content=[SimpleNamespace(type="text", text=text)])
    if usage:
        reply.usage = SimpleNamespace(input_tokens=1200, output_tokens=12,
                                      cache_read_input_tokens=0, cache_creation_input_tokens=0)
    return reply


def anthropic(reply=None):
    return SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: reply or anthropic_reply()))


def ask(client, **over):
    call = {
        "model": "claude-sonnet-4-6",
        "system": f"You classify security alerts. {SECRET}-SYSTEM",
        "messages": [{"role": "user", "content": f"Alert: {SECRET}-INPUT"}],
        "max_tokens": 200,
        "temperature": 0,
    }
    call.update(over)
    return client.messages.create(**call)


def warmed(server=None, client=None, **kw):
    """A meter whose first call has already asked whether capture is open."""
    server = server or Server()
    m = meter(server, **kw)
    wrapped = m.wrap(client or anthropic(), provider="anthropic", feature_id="f-1",
                     prompt_id="classify-alert", prompt_version="v7")
    ask(wrapped)
    drain(m)
    return server, m, wrapped


# ---------------------------------------------------------------------------
# The developer's half, and the server's
# ---------------------------------------------------------------------------
def test_capture_is_off_by_default():
    server = Server()
    m = Meter(application="a", ingest_url=URL, token="tok", flush_interval=0.01,
              transport=server)
    client = m.wrap(anthropic(), provider="anthropic", feature_id="f-1",
                    prompt_id="classify-alert", prompt_version="v7")
    ask(client)
    ask(client)
    drain(m)
    assert m.capture_enabled is False
    assert server.checks == [] and server.samples == []
    assert server.events  # metering carried on as ever


def test_the_first_call_only_asks_and_is_not_sampled():
    server, m, _ = warmed()
    assert len(server.checks) == 1
    assert server.samples == []


def test_nothing_is_sent_while_the_server_says_capture_is_closed():
    server, m, client = warmed(Server(open_=False))
    for _ in range(3):
        ask(client)
    drain(m)
    assert server.samples == [] and server.attempts == 0
    assert len(server.checks) == 1  # the answer is remembered, not asked per call


def test_a_call_without_a_named_prompt_is_never_sampled():
    server = Server()
    m = meter(server)
    client = m.wrap(anthropic(), provider="anthropic", feature_id="f-1")
    ask(client)
    ask(client)
    drain(m)
    assert server.checks == [] and server.samples == []


# ---------------------------------------------------------------------------
# What a sample holds, and what it never does
# ---------------------------------------------------------------------------
def test_a_sample_carries_the_text_and_the_prompt_identity():
    server, m, client = warmed()
    ask(client)
    drain(m)
    (sample,) = server.samples
    assert sample["template"].startswith("You classify security alerts.")
    assert sample["input"] == [{"role": "user", "text": f"Alert: {SECRET}-INPUT"}]
    assert sample["output"] == "billing"
    assert (sample["feature_id"], sample["prompt_id"], sample["prompt_version"]) == (
        "f-1", "classify-alert", "v7")
    assert (sample["provider"], sample["model"]) == ("anthropic", "claude-sonnet-4-6")
    assert (sample["tokens_in"], sample["tokens_out"]) == (1200, 12)
    assert sample["parameters"] == {"temperature": 0, "max_tokens": 200}


def test_tool_calls_tool_results_and_images_are_never_read():
    server, m, client = warmed()
    ask(client, messages=[
        {"role": "user", "content": [
            {"type": "text", "text": "Look at this alert"},
            {"type": "image", "source": {"data": "IMAGE-SECRET"}},
        ]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "lookup", "input": {"host": "TOOL-SECRET"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "TOOL-SECRET"},
            {"type": "text", "text": "What now?"},
        ]},
    ])
    drain(m)
    (sample,) = server.samples
    assert sample["input"] == [
        {"role": "user", "text": "Look at this alert"},
        {"role": "user", "text": "What now?"},
    ]
    blob = json.dumps(sample)
    assert "TOOL-SECRET" not in blob and "IMAGE-SECRET" not in blob


def test_openai_system_and_developer_text_is_the_template_and_tool_messages_are_skipped():
    reply = SimpleNamespace(
        model="gpt-4o",
        choices=[SimpleNamespace(message=SimpleNamespace(content="phishing", tool_calls=None))],
        usage=SimpleNamespace(prompt_tokens=900, completion_tokens=5,
                              prompt_tokens_details=None, completion_tokens_details=None),
    )
    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=lambda **kw: reply)))
    server = Server()
    m = meter(server)
    wrapped = m.wrap(client, provider="openai", feature_id="f-1",
                     prompt_id="classify-alert", prompt_version="v7")
    call = {
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": "You classify alerts."},
            {"role": "developer", "content": "Answer with one word."},
            {"role": "user", "content": "Alert text"},
            {"role": "assistant", "content": None, "tool_calls": [{"arguments": "TOOL-SECRET"}]},
            {"role": "tool", "content": "TOOL-SECRET"},
        ],
        "response_format": {"type": "json_object"},
        "max_completion_tokens": 300,
    }
    wrapped.chat.completions.create(**call)
    drain(m)
    wrapped.chat.completions.create(**call)
    drain(m)
    (sample,) = server.samples
    assert sample["template"] == "You classify alerts.\n\nAnswer with one word."
    assert sample["input"] == [{"role": "user", "text": "Alert text"}]
    assert sample["output"] == "phishing"
    assert sample["parameters"] == {"max_tokens": 300, "response_format": "json_object"}
    assert "TOOL-SECRET" not in json.dumps(sample)


def test_metering_events_still_never_carry_prompt_text():
    server, m, client = warmed()
    ask(client)
    drain(m)
    assert server.samples
    assert SECRET not in json.dumps(server.batches)
    done = [e for e in server.events if e["event_type"] == "span.completed"][-1]
    assert (done["prompt_id"], done["prompt_version"]) == ("classify-alert", "v7")


def test_a_conversation_changed_after_the_call_does_not_change_the_sample():
    server, m, client = warmed()
    conversation = [{"role": "user", "content": "First question"}]
    ask(client, messages=conversation)
    conversation.append({"role": "user", "content": "ADDED-LATER"})
    drain(m)
    assert "ADDED-LATER" not in json.dumps(server.samples)


def test_a_stream_is_not_sampled():
    server, m, client = warmed(client=anthropic(anthropic_reply(usage=False)))
    ask(client)
    drain(m)
    assert server.checks == [] and server.samples == []


# ---------------------------------------------------------------------------
# Redaction, size, refusals and caps
# ---------------------------------------------------------------------------
def test_redaction_runs_before_anything_leaves():
    def redact(sample):
        sample["input"] = [{**msg, "text": msg["text"].replace(SECRET, "[redacted]")}
                           for msg in sample["input"]]
        return sample

    server, m, client = warmed(redact=redact)
    ask(client)
    drain(m)
    assert server.samples[0]["input"][0]["text"] == "Alert: [redacted]-INPUT"


@pytest.mark.parametrize("redact", [lambda s: None, lambda s: 1 / 0])
def test_a_redactor_that_returns_nothing_or_fails_sends_nothing(redact):
    server, m, client = warmed(redact=redact)
    ask(client)
    drain(m)
    assert server.attempts == 0
    assert m.capture_dropped == 1


def test_an_oversized_sample_is_dropped_not_truncated():
    server, m, client = warmed()
    ask(client, system="x" * (cm.CAPTURE_MAX_BYTES + 1))
    drain(m)
    assert server.attempts == 0
    assert m.capture_dropped == 1


def test_a_refusal_closes_capture_for_the_feature():
    server, m, client = warmed(Server(sample_status=403, reason="feature_not_enabled"))
    ask(client)
    drain(m)
    for _ in range(3):
        ask(client)
    drain(m)
    assert server.attempts == 1
    assert m.capture_dropped == 1


def test_the_servers_daily_cap_stops_that_prompt_for_the_day():
    server, m, client = warmed(Server(sample_status=429, reason="daily_cap"))
    ask(client)
    drain(m)
    for _ in range(3):
        ask(client)
    drain(m)
    assert server.attempts == 1


def test_the_client_keeps_its_own_daily_cap(monkeypatch):
    monkeypatch.setattr(cm, "CAPTURE_MAX_PER_DAY", 2)
    server, m, client = warmed()
    for _ in range(5):
        ask(client)
    drain(m)
    assert len(server.samples) == 2


def test_a_sample_rate_of_zero_sends_nothing():
    server, m, client = warmed(capture_sample_rate=0.0)
    for _ in range(5):
        ask(client)
    drain(m)
    assert server.attempts == 0


def test_a_broken_capture_endpoint_never_touches_metering_or_the_caller():
    server, m, client = warmed(Server(capture_down=True))
    reply = ask(client)
    drain(m)
    assert reply.content[0].text == "billing"
    assert m.capture_dropped == 1
    assert m.dropped == 0
    assert [e for e in server.events if e["event_type"] == "span.completed"]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def test_capture_can_be_switched_on_by_environment_and_finds_its_endpoint(monkeypatch):
    monkeypatch.setenv("METER_CAPTURE_PROMPTS", "true")
    m = Meter(ingest_url=URL, token="tok")
    assert m.capture_enabled is True
    assert m.capture_url == CAPTURE


def test_capture_stays_off_when_its_endpoint_cannot_be_worked_out():
    m = Meter(ingest_url="https://meter.example/custom-path", token="tok", capture_prompts=True)
    assert m.capture_enabled is False
