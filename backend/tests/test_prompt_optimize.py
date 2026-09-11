"""Proposing a cheaper prompt: grounded in real usage, strictly checked, and
never called recommended before anything has been tested."""

from __future__ import annotations

import json

import httpx
import pytest
from meter import prompt_capture as pc
from meter import prompt_optimize as po
from meter import traces

SECRET_TEMPLATE = "You classify {ticket}. ACME-SECRET-TEMPLATE. Answer in one word."
REWRITE = "Classify {ticket}. One word."


@pytest.fixture(autouse=True)
def meters_model(monkeypatch):
    monkeypatch.setenv("METER_DISCOVERY_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("METER_DISCOVERY_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setenv("METER_DISCOVERY_API_KEY", "sk-model-key")


def model(reply: str, status: int = 200, body: str = ""):
    """An OpenAI-compatible endpoint that answers with `reply`."""

    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, text=body)
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    return httpx.Client(transport=httpx.MockTransport(handler))


def rewrite_json(template=REWRITE, changes=None):
    if changes is None:
        changes = [
            {
                "category": "redundancy",
                "before": "Answer in one word.",
                "after": "One word.",
                "reason": "The same instruction, shorter.",
                "expected_effect": "Fewer input tokens per call.",
            }
        ]
    return json.dumps({"template": template, "changes": changes})


def make_feature(app_env, tenant_id, name="Ticket triage"):
    row = app_env.execute(
        "INSERT INTO feature (tenant_id, name) VALUES (%s, %s) RETURNING id", (tenant_id, name)
    ).fetchone()
    app_env.commit()
    return str(row[0])


def sample(feature_id, text="Alert one"):
    return {
        "feature_id": feature_id,
        "prompt_id": "triage",
        "prompt_version": "v7",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "template": SECRET_TEMPLATE,
        "input": [{"role": "user", "text": text}],
        "output": "billing",
        "tokens_in": 1200,
        "tokens_out": 12,
    }


def captured(app_env, tenant_id, count=2):
    """A consented feature with `count` samples of one prompt version."""
    feature = make_feature(app_env, tenant_id)
    pc.grant_consent(
        tenant_id,
        "cto@acme.com",
        accepted_version=pc.CONSENT_VERSION,
        accepted_disclosure=pc.disclosure(tenant_id),
    )
    pc.set_feature(tenant_id, feature, True, "cto@acme.com")
    for n in range(count):
        pc.store_sample(tenant_id, sample(feature, f"Alert {n}"))
    template_id = po.list_prompts(tenant_id)["prompts"][0]["template_id"]
    return feature, template_id


def meter_calls(tenant_id, feature_id, calls=3):
    """Real metered traffic through the same prompt version."""
    for n in range(calls):
        traces.ingest(
            tenant_id,
            [
                {
                    "event_type": "span.completed",
                    "trace_id": f"t-{n}",
                    "span_id": f"s-{n}",
                    "span_kind": "llm",
                    "operation_name": "classify",
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-6",
                    "feature_id": feature_id,
                    "prompt_id": "triage",
                    "prompt_version": "v7",
                    "tokens_in": 1_000_000,
                    "tokens_out": 0,
                }
            ],
        )


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------
def test_usage_comes_from_metering_not_from_the_samples(app_env, tenant_id):
    feature, _ = captured(app_env, tenant_id, count=2)
    meter_calls(tenant_id, feature, calls=3)
    (prompt,) = po.list_prompts(tenant_id)["prompts"]
    assert prompt["samples"] == 2
    assert prompt["calls"] == 3  # samples are a fraction of traffic; calls are all of it
    assert prompt["cost"] == pytest.approx(9.0)  # 3 x 1M input tokens at $3/M
    assert prompt["candidate_status"] is None


def test_the_list_carries_no_prompt_text(app_env, tenant_id):
    captured(app_env, tenant_id)
    assert "ACME-SECRET-TEMPLATE" not in json.dumps(po.list_prompts(tenant_id))


def test_one_tenant_never_sees_anothers_prompts(app_env, tenant_id):
    captured(app_env, tenant_id)
    other = str(
        app_env.execute("INSERT INTO tenant (name) VALUES ('Other Co') RETURNING id").fetchone()[0]
    )
    app_env.commit()
    assert po.list_prompts(other)["prompts"] == []


# ---------------------------------------------------------------------------
# Generating a candidate
# ---------------------------------------------------------------------------
def test_a_rewrite_needs_enough_real_calls_behind_it(app_env, tenant_id):
    _, template_id = captured(app_env, tenant_id, count=2)
    with pytest.raises(po.OptimizeError, match=f"2 of {po.MIN_SAMPLES} samples"):
        po.generate_candidate(tenant_id, template_id, "cto@acme.com", client=model(rewrite_json()))


def test_a_candidate_is_stored_unreadable_and_never_called_recommended(
    app_env, tenant_id, monkeypatch
):
    monkeypatch.setattr(po, "MIN_SAMPLES", 2)
    _, template_id = captured(app_env, tenant_id, count=2)
    made = po.generate_candidate(
        tenant_id, template_id, "cto@acme.com", client=model(rewrite_json())
    )
    assert made["status"] == "not_evaluated"
    assert made["change_count"] == 1
    assert (made["provider"], made["model"]) == ("Groq", "openai/gpt-oss-120b")

    stored = app_env.execute(
        "SELECT ciphertext, changes_cipher FROM prompt_candidate WHERE tenant_id = %s",
        (tenant_id,),
    ).fetchone()
    raw = bytes(stored[0]) + bytes(stored[1])
    assert b"Classify" not in raw and b"shorter" not in raw

    detail = po.prompt_detail(tenant_id, template_id)
    assert detail["candidate"]["candidate_id"] == made["candidate_id"]
    assert "Classify" not in json.dumps(detail)


def test_the_prompt_and_its_rewrite_are_shown_together_and_the_look_is_logged(
    app_env, tenant_id, monkeypatch
):
    monkeypatch.setattr(po, "MIN_SAMPLES", 2)
    _, template_id = captured(app_env, tenant_id, count=2)
    po.generate_candidate(tenant_id, template_id, "cto@acme.com", client=model(rewrite_json()))

    shown = po.prompt_content(tenant_id, template_id, "analyst@acme.com")
    assert shown["template"] == SECRET_TEMPLATE
    assert shown["candidate"]["template"] == REWRITE
    assert shown["candidate"]["changes"][0]["reason"] == "The same instruction, shorter."

    events = pc.audit_log(tenant_id)
    assert {"candidate_generated", "candidate_viewed"} <= {e["event"] for e in events}
    assert "ACME-SECRET-TEMPLATE" not in json.dumps(events)


def test_generation_is_refused_while_consent_is_paused(app_env, tenant_id, monkeypatch):
    monkeypatch.setattr(po, "MIN_SAMPLES", 2)
    _, template_id = captured(app_env, tenant_id, count=2)
    monkeypatch.setenv("METER_DISCOVERY_MODEL", "a-different-model")  # disclosure changed
    with pytest.raises(po.OptimizeError, match="not turned on, or is paused"):
        po.generate_candidate(tenant_id, template_id, "cto@acme.com", client=model(rewrite_json()))


# ---------------------------------------------------------------------------
# What the model returns is checked, not trusted
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "reply, message",
    [
        (rewrite_json(template="Classify the ticket. One word."), "dropped values"),
        ("I would shorten the second paragraph.", "did not return a rewrite"),
        ('{"template": "", "changes": []}', "did not return a rewrite"),
        (rewrite_json(template=SECRET_TEMPLATE, changes=[]), "nothing to change"),
    ],
)
def test_an_unusable_rewrite_is_refused(app_env, tenant_id, monkeypatch, reply, message):
    monkeypatch.setattr(po, "MIN_SAMPLES", 2)
    _, template_id = captured(app_env, tenant_id, count=2)
    with pytest.raises(po.OptimizeError, match=message):
        po.generate_candidate(tenant_id, template_id, "cto@acme.com", client=model(reply))
    assert po.prompt_detail(tenant_id, template_id)["candidate"] is None


def test_changes_are_normalised_and_the_unexplained_ones_dropped(app_env, tenant_id, monkeypatch):
    monkeypatch.setattr(po, "MIN_SAMPLES", 2)
    _, template_id = captured(app_env, tenant_id, count=2)
    changes = [
        {"category": "invented-category", "reason": "Still a real reason.", "before": "x"},
        {"category": "examples", "before": "y", "after": ""},  # no reason: explains nothing
        {"category": "output_format", "reason": "Tighter format.", "expected_effect": "e"},
    ]
    po.generate_candidate(
        tenant_id, template_id, "cto@acme.com", client=model(rewrite_json(changes=changes))
    )
    kept = po.prompt_content(tenant_id, template_id, "cto@acme.com")["candidate"]["changes"]
    assert [c["category"] for c in kept] == ["wording", "output_format"]


def test_a_provider_error_never_carries_the_key(app_env, tenant_id, monkeypatch):
    monkeypatch.setattr(po, "MIN_SAMPLES", 2)
    _, template_id = captured(app_env, tenant_id, count=2)
    failing = model("", status=401, body='{"error": "invalid key sk-model-key"}')
    with pytest.raises(po.OptimizeError) as raised:
        po.generate_candidate(tenant_id, template_id, "cto@acme.com", client=failing)
    assert "sk-model-key" not in str(raised.value)
    assert "***" in str(raised.value)


# ---------------------------------------------------------------------------
# Discarding
# ---------------------------------------------------------------------------
def test_discarding_removes_the_text_but_keeps_the_record(app_env, tenant_id, monkeypatch):
    monkeypatch.setattr(po, "MIN_SAMPLES", 2)
    _, template_id = captured(app_env, tenant_id, count=2)
    made = po.generate_candidate(
        tenant_id, template_id, "cto@acme.com", client=model(rewrite_json())
    )

    assert po.discard_candidate(tenant_id, made["candidate_id"], "cto@acme.com") is True
    assert po.prompt_detail(tenant_id, template_id)["candidate"] is None
    assert po.prompt_content(tenant_id, template_id, "cto@acme.com")["candidate"] is None

    row = app_env.execute(
        "SELECT status, ciphertext, changes_cipher FROM prompt_candidate WHERE id = %s",
        (made["candidate_id"],),
    ).fetchone()
    assert row[0] == "discarded"
    assert bytes(row[1]) == b"" and bytes(row[2]) == b""
    assert any(e["event"] == "candidate_discarded" for e in pc.audit_log(tenant_id))


def test_withdrawing_consent_takes_the_candidates_with_it(app_env, tenant_id, monkeypatch):
    monkeypatch.setattr(po, "MIN_SAMPLES", 2)
    _, template_id = captured(app_env, tenant_id, count=2)
    po.generate_candidate(tenant_id, template_id, "cto@acme.com", client=model(rewrite_json()))

    pc.withdraw_consent(tenant_id, "cto@acme.com")
    remaining = app_env.execute(
        "SELECT count(*) FROM prompt_candidate WHERE tenant_id = %s", (tenant_id,)
    ).fetchone()[0]
    assert remaining == 0
    assert po.list_prompts(tenant_id)["prompts"] == []
