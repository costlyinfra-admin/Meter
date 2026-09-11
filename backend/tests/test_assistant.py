"""The support assistant: grounded answers, honest failures, no leaked key."""

from __future__ import annotations

import json
import time

import httpx
import pytest
from meter import assistant

PASSAGES = [
    {
        "id": "getting-started/what-meter-does",
        "title": "What Meter does",
        "category": "Getting started",
        "text": "Build cost is what a feature cost to make. "
        "Inference cost is what it costs to run.",
    },
    {
        "id": "attribution/unattributed",
        "title": "The Unattributed bucket",
        "category": "Attribution",
        "text": "Spend that cannot be tied to a feature lands in Unattributed rather than "
        "being spread across features.",
    },
]


@pytest.fixture(autouse=True)
def _byok_env(monkeypatch):
    """Point the assistant at a stub endpoint, and clear the rate-limit window."""
    monkeypatch.setenv("METER_DISCOVERY_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("METER_DISCOVERY_API_KEY", "sk-super-secret-key-value")
    monkeypatch.setenv("METER_DISCOVERY_MODEL", "llama-3.3-70b-versatile")
    assistant._recent.clear()


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _replies(content: str, status_code: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        handler.request = request
        if status_code >= 400:
            return httpx.Response(status_code, text=content)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return handler


def test_answers_the_question_without_citing_documentation():
    handler = _replies(
        json.dumps(
            {
                "answer": "Build cost is what a feature cost to make; inference is what it "
                "costs to run. They are never added together.",
                "sources": ["getting-started/what-meter-does"],
                "answered": True,
            }
        )
    )
    with _client(handler) as client:
        result = assistant.answer(
            "what is the difference between build and inference cost?",
            passages=PASSAGES,
            client=client,
        )

    assert result["answered"] is True
    assert result["composed"] is True
    assert "never added together" in result["answer"]
    # Documentation is supporting material, not something to cite back at
    # someone who asked about their own product. An answer stands on its own.
    assert result["sources"] == []

    # The reference, the question and the screen all still reach the model.
    sent = json.loads(handler.request.content)
    prompt = sent["messages"][-1]["content"]
    assert "The Unattributed bucket" in prompt
    assert "what is the difference" in prompt
    # And the model is told to use it as reference, not as the only source.
    system = sent["messages"][0]["content"]
    assert "REFERENCE" in system and "authority on anything about their usage" in system


def test_no_citation_is_returned_even_if_the_model_offers_one():
    # The model may still emit sources; they are dropped rather than rendered.
    handler = _replies(
        json.dumps(
            {"answer": "Yes.", "sources": ["getting-started/what-meter-does"], "answered": True}
        )
    )
    with _client(handler) as client:
        result = assistant.answer("anything?", passages=PASSAGES, client=client)
    assert result["sources"] == []


def test_a_model_that_ignores_the_json_format_still_answers():
    with _client(_replies("Build cost and inference cost are kept separate.")) as client:
        result = assistant.answer("build vs inference?", passages=PASSAGES, client=client)
    assert result["answer"] == "Build cost and inference cost are kept separate."
    assert result["sources"] == []


def test_unanswered_when_the_handbook_does_not_cover_it():
    handler = _replies(
        json.dumps({"answer": "The handbook doesn't cover that.", "sources": [], "answered": False})
    )
    with _client(handler) as client:
        result = assistant.answer("what is the weather?", passages=PASSAGES, client=client)
    assert result["answered"] is False


def test_current_page_is_offered_as_context():
    # "Why is this blank?" means something different on each screen.
    handler = _replies('{"answer": "ok", "sources": [], "answered": true}')
    with _client(handler) as client:
        assistant.answer(
            "why is this blank?", passages=PASSAGES, page="Cost sources", client=client
        )
    prompt = json.loads(handler.request.content)["messages"][-1]["content"]
    assert "currently on the Cost sources screen" in prompt


def test_history_is_passed_through_and_capped():
    handler = _replies('{"answer": "ok", "sources": [], "answered": true}')
    history = [{"role": "user", "content": f"q{i}"} for i in range(20)]
    with _client(handler) as client:
        assistant.answer("follow up?", passages=PASSAGES, history=history, client=client)
    sent = json.loads(handler.request.content)
    # system + MAX_HISTORY turns + the question itself
    assert len(sent["messages"]) == assistant.MAX_HISTORY + 2
    assert sent["messages"][1]["content"] == "q14"


def test_without_a_model_it_answers_the_data_question_from_the_data(monkeypatch):
    # It used to paste a documentation excerpt, which read as a search result
    # and could not touch the customer's data at all.
    monkeypatch.delenv("METER_DISCOVERY_BASE_URL", raising=False)
    facts = {
        "agents": {
            "stale_after_minutes": 10,
            "stale_now": 1,
            "running_now": 0,
            "quiet_runs": [{"workflow": "resolve-ticket", "running_for_minutes": 95}],
            "repeated_steps_last_7d": [],
        }
    }
    result = assistant.answer("is an agent stuck?", passages=PASSAGES, facts=facts)
    assert result["composed"] is False
    assert result["answered"] is True
    assert "resolve-ticket" in result["answer"]
    assert "95 min" in result["answer"]
    # No documentation, quoted or named.
    assert "What Meter does" not in result["answer"]
    assert result["sources"] == []


def test_without_a_model_and_without_data_it_says_so_plainly(monkeypatch):
    monkeypatch.delenv("METER_DISCOVERY_BASE_URL", raising=False)
    result = assistant.answer("what is build cost?", passages=PASSAGES, facts=None)
    assert result["composed"] is False
    assert result["answered"] is False
    # Admitting the limit beats handing back an excerpt dressed as a reply.
    assert "isn't reachable" in result["answer"]
    assert "What Meter does" not in result["answer"]


def test_a_provider_failure_degrades_rather_than_erroring():
    with _client(_replies("upstream exploded", status_code=500)) as client:
        result = assistant.answer("what is build cost?", passages=PASSAGES, client=client)
    assert result["composed"] is False
    assert result["answered"] is False


def test_a_network_failure_degrades_the_same_way():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with _client(boom) as client:
        result = assistant.answer("what is build cost?", passages=PASSAGES, client=client)
    assert result["composed"] is False


def test_no_reference_and_no_data_means_no_invented_answer():
    result = assistant.answer("something obscure", passages=[])
    assert result["answered"] is False
    assert "/traces" in result["answer"] or "/cost-sources" in result["answer"]


def test_the_api_key_never_reaches_the_reply_or_the_log(caplog):
    key = "sk-super-secret-key-value"
    # A provider that quotes the request back, key and all — the realistic leak.
    with _client(_replies(f"401 invalid key {key}", status_code=401)) as client:
        with caplog.at_level("WARNING"):
            result = assistant.answer("hello?", passages=PASSAGES, client=client)
    assert key not in json.dumps(result)
    assert key not in caplog.text
    assert "***" in caplog.text


def test_rate_limit_stops_a_script_but_not_a_person():
    for _ in range(assistant.RATE_LIMIT):
        assistant.check_rate("tenant-a")
    with pytest.raises(assistant.RateLimited):
        assistant.check_rate("tenant-a")

    # One tenant hitting the limit must not silence anyone else.
    assistant.check_rate("tenant-b")

    # And the window reopens rather than locking the tenant out for good.
    assistant.check_rate("tenant-a", now=time.monotonic() + assistant.RATE_WINDOW + 1)


def test_only_the_capped_number_of_excerpts_is_sent():
    handler = _replies('{"answer": "ok", "sources": [], "answered": true}')
    many = [dict(PASSAGES[0], id=f"cat/topic-{i}") for i in range(20)]
    with _client(handler) as client:
        assistant.answer("q?", passages=many, client=client)
    prompt = json.loads(handler.request.content)["messages"][-1]["content"]
    assert prompt.count("--- id:") == assistant.MAX_PASSAGES


# --------------------------------------------------------------------------
# The HTTP surface
# --------------------------------------------------------------------------
GOOD_PASSWORD = "correct horse battery"


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    from fastapi.testclient import TestClient
    from meter.api import create_app

    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cto@acme.com", "password": GOOD_PASSWORD})
    return c


def test_the_assistant_needs_a_signed_in_user(
    admin_conn, admin_conninfo, app_conninfo, monkeypatch
):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    from fastapi.testclient import TestClient
    from meter.api import create_app

    anonymous = TestClient(create_app())
    assert anonymous.post("/api/assistant/chat", json={"question": "hi"}).status_code == 401
    assert anonymous.get("/api/assistant/meta").status_code == 401


def test_meta_says_whether_answers_are_written_or_quoted(client, monkeypatch):
    meta = client.get("/api/assistant/meta").json()
    assert meta["composed"] is True
    assert "@" in meta["support_email"]

    monkeypatch.delenv("METER_DISCOVERY_BASE_URL")
    assert client.get("/api/assistant/meta").json()["composed"] is False


def test_chat_answers_over_http(client, monkeypatch):
    monkeypatch.setattr(
        assistant,
        "answer",
        lambda q, **kw: {"answer": f"re: {q}", "sources": [], "answered": True, "composed": True},
    )
    resp = client.post(
        "/api/assistant/chat",
        json={"question": "what is build cost?", "passages": PASSAGES, "page": "Overview"},
    )
    assert resp.status_code == 200
    assert resp.json()["answer"] == "re: what is build cost?"


def test_chat_rejects_an_oversized_question(client):
    resp = client.post("/api/assistant/chat", json={"question": "x" * 5000})
    assert resp.status_code == 422


def test_chat_rate_limits_a_flood(client, monkeypatch):
    monkeypatch.setattr(
        assistant,
        "answer",
        lambda q, **kw: {"answer": "ok", "sources": [], "answered": True, "composed": True},
    )
    for _ in range(assistant.RATE_LIMIT):
        assert client.post("/api/assistant/chat", json={"question": "hi"}).status_code == 200
    assert client.post("/api/assistant/chat", json={"question": "hi"}).status_code == 429
