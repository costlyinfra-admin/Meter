"""Consented prompt capture: nothing without both keys, nothing readable at rest,
nothing left after withdrawal, nothing kept past 30 days."""

from __future__ import annotations

import datetime as dt
import json
import logging

import psycopg
import pytest
from cryptography.fernet import Fernet, InvalidToken
from meter import prompt_capture as pc
from meter.db import app_dsn, connect, tenant_tx

SECRET_TEMPLATE = "You are ACME-SECRET-TEMPLATE. Classify the ticket."
SECRET_INPUT = "Customer wrote ACME-SECRET-INPUT about their invoice"
SECRET_OUTPUT = "ACME-SECRET-OUTPUT: billing"
SECRETS = ("ACME-SECRET-TEMPLATE", "ACME-SECRET-INPUT", "ACME-SECRET-OUTPUT")


@pytest.fixture(autouse=True)
def meters_model(monkeypatch):
    # Meter's own model is what consent names when the organization has none.
    monkeypatch.setenv("METER_DISCOVERY_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("METER_DISCOVERY_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setenv("METER_DISCOVERY_API_KEY", "test-key")


def make_feature(app_env, tenant_id, name="Ticket triage"):
    row = app_env.execute(
        "INSERT INTO feature (tenant_id, name) VALUES (%s, %s) RETURNING id", (tenant_id, name)
    ).fetchone()
    app_env.commit()
    return str(row[0])


def sample(feature_id, **over):
    body = {
        "feature_id": feature_id,
        "prompt_id": "triage",
        "prompt_version": "v7",
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "template": SECRET_TEMPLATE,
        "input": [{"role": "user", "text": SECRET_INPUT}],
        "output": SECRET_OUTPUT,
        "parameters": {"temperature": 0, "max_tokens": 200},
        "tokens_in": 1200,
        "tokens_out": 40,
        "latency_ms": 900,
    }
    body.update(over)
    return body


def consent(tenant_id, actor="cto@acme.com"):
    return pc.grant_consent(
        tenant_id,
        actor,
        accepted_version=pc.CONSENT_VERSION,
        accepted_disclosure=pc.disclosure(tenant_id),
    )


def open_capture(app_env, tenant_id):
    feature = make_feature(app_env, tenant_id)
    consent(tenant_id)
    pc.set_feature(tenant_id, feature, True, "cto@acme.com")
    return feature


def count(app_env, table, tenant_id):
    return app_env.execute(
        f"SELECT count(*) FROM {table} WHERE tenant_id = %s",  # noqa: S608 - test-only fixed names
        (tenant_id,),
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# The double key
# ---------------------------------------------------------------------------
def test_nothing_is_stored_without_organization_consent(app_env, tenant_id):
    feature = make_feature(app_env, tenant_id)
    with pytest.raises(pc.CaptureRefused) as refused:
        pc.store_sample(tenant_id, sample(feature))
    assert (refused.value.reason, refused.value.status) == ("no_consent", 403)
    assert count(app_env, "prompt_sample", tenant_id) == 0
    assert count(app_env, "prompt_template", tenant_id) == 0


def test_nothing_is_stored_for_a_feature_that_was_not_chosen(app_env, tenant_id):
    feature = make_feature(app_env, tenant_id)
    consent(tenant_id)
    with pytest.raises(pc.CaptureRefused) as refused:
        pc.store_sample(tenant_id, sample(feature))
    assert refused.value.reason == "feature_not_enabled"
    assert count(app_env, "prompt_sample", tenant_id) == 0


def test_a_feature_cannot_be_chosen_before_the_organization_consents(app_env, tenant_id):
    feature = make_feature(app_env, tenant_id)
    with pytest.raises(pc.PromptCaptureError, match="organization"):
        pc.set_feature(tenant_id, feature, True, "cto@acme.com")


def test_with_both_keys_a_sample_is_stored_and_capture_reports_open(app_env, tenant_id):
    feature = open_capture(app_env, tenant_id)
    assert pc.capture_open(tenant_id, feature) == {"open": True, "reason": None}
    stored = pc.store_sample(tenant_id, sample(feature))
    assert stored["stored"] is True
    status = pc.consent_status(tenant_id)
    assert status["capturing"] is True
    assert status["features"][0]["samples"] == 1


# ---------------------------------------------------------------------------
# What consent names must still be true
# ---------------------------------------------------------------------------
def test_consent_names_the_provider_and_model_that_would_see_prompts(app_env, tenant_id):
    assert pc.disclosure(tenant_id) == {
        "source": "meter",
        "provider": "Groq",
        "model": "openai/gpt-oss-120b",
    }


def test_consent_to_a_stale_screen_is_refused(app_env, tenant_id):
    with pytest.raises(pc.PromptCaptureError, match="consent text has changed"):
        pc.grant_consent(
            tenant_id,
            "cto@acme.com",
            accepted_version="2020-01-01",
            accepted_disclosure=pc.disclosure(tenant_id),
        )
    stale = {**pc.disclosure(tenant_id), "model": "some-other-model"}
    with pytest.raises(pc.PromptCaptureError, match="model that would see prompts"):
        pc.grant_consent(
            tenant_id,
            "cto@acme.com",
            accepted_version=pc.CONSENT_VERSION,
            accepted_disclosure=stale,
        )
    assert pc.consent_status(tenant_id)["consent"] is None


def test_consent_cannot_be_given_when_no_model_could_be_named(app_env, tenant_id, monkeypatch):
    monkeypatch.delenv("METER_DISCOVERY_BASE_URL")
    with pytest.raises(pc.PromptCaptureError, match="No model"):
        pc.grant_consent(
            tenant_id,
            "cto@acme.com",
            accepted_version=pc.CONSENT_VERSION,
            accepted_disclosure={"source": "meter", "provider": "Groq", "model": "x"},
        )


def test_capture_pauses_when_the_model_that_would_see_prompts_changes(
    app_env, tenant_id, monkeypatch
):
    feature = open_capture(app_env, tenant_id)
    monkeypatch.setenv("METER_DISCOVERY_MODEL", "a-different-model")
    with pytest.raises(pc.CaptureRefused) as refused:
        pc.store_sample(tenant_id, sample(feature))
    assert refused.value.reason == "disclosure_changed"
    status = pc.consent_status(tenant_id)
    assert (status["capturing"], status["disclosure_changed"]) == (False, True)
    assert pc.capture_open(tenant_id, feature)["reason"] == "disclosure_changed"


def test_capture_pauses_when_the_consent_text_changes(app_env, tenant_id, monkeypatch):
    feature = open_capture(app_env, tenant_id)
    monkeypatch.setattr(pc, "CONSENT_VERSION", "2099-01-01")
    with pytest.raises(pc.CaptureRefused) as refused:
        pc.store_sample(tenant_id, sample(feature))
    assert refused.value.reason == "consent_outdated"
    assert pc.consent_status(tenant_id)["version_outdated"] is True


# ---------------------------------------------------------------------------
# Bounds and shape
# ---------------------------------------------------------------------------
def test_an_oversized_sample_is_refused_not_truncated(app_env, tenant_id):
    feature = open_capture(app_env, tenant_id)
    with pytest.raises(pc.CaptureRefused) as refused:
        pc.store_sample(tenant_id, sample(feature, output="x" * (pc.MAX_SAMPLE_BYTES + 1)))
    assert (refused.value.reason, refused.value.status) == ("too_large", 413)
    assert count(app_env, "prompt_sample", tenant_id) == 0


def test_the_daily_cap_holds_whatever_the_client_sends(app_env, tenant_id, monkeypatch):
    feature = open_capture(app_env, tenant_id)
    monkeypatch.setattr(pc, "MAX_SAMPLES_PER_DAY", 2)
    pc.store_sample(tenant_id, sample(feature))
    pc.store_sample(tenant_id, sample(feature))
    with pytest.raises(pc.CaptureRefused) as refused:
        pc.store_sample(tenant_id, sample(feature))
    assert (refused.value.reason, refused.value.status) == ("daily_cap", 429)


def test_only_the_newest_samples_of_a_version_are_kept(app_env, tenant_id, monkeypatch):
    feature = open_capture(app_env, tenant_id)
    monkeypatch.setattr(pc, "MAX_SAMPLES_PER_VERSION", 3)
    for _ in range(5):
        pc.store_sample(tenant_id, sample(feature))
    assert len(pc.list_samples(tenant_id)) == 3


@pytest.mark.parametrize(
    "override",
    [
        {"tool_calls": [{"name": "lookup", "arguments": "{}"}]},
        {"input": [{"role": "tool", "text": "result"}]},
        {"input": [{"role": "user", "text": "hi", "images": ["data:..."]}]},
        {"documents": ["retrieved text"]},
        {"parameters": {"tools": "all"}},
    ],
)
def test_tool_calls_images_and_documents_are_refused_not_dropped(app_env, tenant_id, override):
    feature = open_capture(app_env, tenant_id)
    with pytest.raises(pc.PromptCaptureError):
        pc.store_sample(tenant_id, sample(feature, **override))
    assert count(app_env, "prompt_sample", tenant_id) == 0


# ---------------------------------------------------------------------------
# Unreadable at rest; readable only on an audited request
# ---------------------------------------------------------------------------
def test_content_is_not_readable_in_the_database(app_env, tenant_id):
    feature = open_capture(app_env, tenant_id)
    pc.store_sample(tenant_id, sample(feature))
    rows = app_env.execute(
        """
        SELECT s.ciphertext, t.ciphertext, t.template_digest, k.wrapped_key
        FROM prompt_sample s
        JOIN prompt_template t ON t.id = s.template_id
        JOIN prompt_data_key k ON k.tenant_id = s.tenant_id
        """
    ).fetchall()
    raw = b"".join(
        bytes(v) if isinstance(v, (bytes, memoryview)) else str(v).encode() for v in rows[0]
    )
    for secret in SECRETS:
        assert secret.encode() not in raw


def test_listing_never_carries_content(app_env, tenant_id):
    feature = open_capture(app_env, tenant_id)
    pc.store_sample(tenant_id, sample(feature))
    listed = pc.list_samples(tenant_id)
    blob = json.dumps(listed)
    for secret in SECRETS:
        assert secret not in blob
    assert listed[0]["tokens_in"] == 1200
    assert "ciphertext" not in listed[0]


def test_showing_content_decrypts_it_and_leaves_an_audit_row_without_it(app_env, tenant_id):
    feature = open_capture(app_env, tenant_id)
    sample_id = pc.store_sample(tenant_id, sample(feature))["sample_id"]
    shown = pc.show_sample(tenant_id, sample_id, "analyst@acme.com")
    assert shown["template"] == SECRET_TEMPLATE
    assert shown["input"] == [{"role": "user", "text": SECRET_INPUT}]
    assert shown["output"] == SECRET_OUTPUT

    events = pc.audit_log(tenant_id)
    viewed = [e for e in events if e["event"] == "sample_content_viewed"]
    assert viewed and viewed[0]["actor"] == "analyst@acme.com"
    blob = json.dumps(events)
    for secret in SECRETS:
        assert secret not in blob


def test_content_never_reaches_the_logs(app_env, tenant_id, caplog):
    caplog.set_level(logging.DEBUG)
    feature = open_capture(app_env, tenant_id)
    sample_id = pc.store_sample(tenant_id, sample(feature))["sample_id"]
    pc.show_sample(tenant_id, sample_id, "cto@acme.com")
    with pytest.raises(pc.PromptCaptureError):
        pc.store_sample(tenant_id, sample(feature, tool_calls=[SECRET_INPUT]))
    pc.withdraw_consent(tenant_id, "cto@acme.com")
    for secret in SECRETS:
        assert secret not in caplog.text


# ---------------------------------------------------------------------------
# Withdrawal and retention
# ---------------------------------------------------------------------------
def test_turning_a_feature_off_deletes_only_its_content(app_env, tenant_id):
    triage = open_capture(app_env, tenant_id)
    billing = make_feature(app_env, tenant_id, "Billing answers")
    pc.set_feature(tenant_id, billing, True, "cto@acme.com")
    pc.store_sample(tenant_id, sample(triage))
    pc.store_sample(tenant_id, sample(billing, prompt_id="billing"))

    pc.set_feature(tenant_id, triage, False, "cto@acme.com")
    remaining = pc.list_samples(tenant_id)
    assert [s["feature_id"] for s in remaining] == [billing]
    with pytest.raises(pc.CaptureRefused):
        pc.store_sample(tenant_id, sample(triage))
    assert any(e["event"] == "feature_disabled" for e in pc.audit_log(tenant_id))


def test_withdrawal_destroys_content_and_the_key_that_could_read_it(app_env, tenant_id):
    feature = open_capture(app_env, tenant_id)
    pc.store_sample(tenant_id, sample(feature))
    # What a backup taken before withdrawal would still hold.
    (old_ciphertext,) = app_env.execute("SELECT ciphertext FROM prompt_sample").fetchone()
    app_env.commit()

    status = pc.withdraw_consent(tenant_id, "cto@acme.com")
    assert status["consent"] is None and status["capturing"] is False
    for table in (
        "prompt_sample",
        "prompt_template",
        "prompt_capture_feature",
        "prompt_capture_consent",
        "prompt_data_key",
    ):
        assert count(app_env, table, tenant_id) == 0, table

    # Consenting again makes a NEW key; the old ciphertext stays unreadable.
    consent(tenant_id)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        new_key = pc._data_key(conn, create=False)
    with pytest.raises(InvalidToken):
        Fernet(new_key).decrypt(bytes(old_ciphertext))

    events = [e["event"] for e in pc.audit_log(tenant_id)]
    assert "consent_withdrawn" in events and "data_key_destroyed" in events


def test_the_audit_log_is_append_only(app_env, tenant_id):
    open_capture(app_env, tenant_id)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        for statement in (
            "UPDATE prompt_audit SET actor = 'someone else'",
            "DELETE FROM prompt_audit",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    conn.execute(statement)


def test_samples_and_unseen_templates_expire_after_thirty_days(app_env, tenant_id):
    feature = open_capture(app_env, tenant_id)
    pc.store_sample(tenant_id, sample(feature))
    app_env.execute("UPDATE prompt_sample SET received_at = now() - interval '31 days'")
    app_env.execute("UPDATE prompt_template SET last_seen_at = now() - interval '31 days'")
    app_env.commit()

    assert pc.purge_expired(tenant_id) == {"samples_deleted": 1, "templates_deleted": 1}
    assert count(app_env, "prompt_sample", tenant_id) == 0
    assert any(e["event"] == "samples_purged" for e in pc.audit_log(tenant_id))


def test_fresh_samples_survive_the_purge(app_env, tenant_id):
    feature = open_capture(app_env, tenant_id)
    pc.store_sample(tenant_id, sample(feature))
    assert pc.purge_expired(tenant_id) == {"samples_deleted": 0, "templates_deleted": 0}
    assert count(app_env, "prompt_sample", tenant_id) == 1


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------
def test_one_tenant_never_sees_another_tenants_prompts(app_env, tenant_id):
    feature = open_capture(app_env, tenant_id)
    sample_id = pc.store_sample(tenant_id, sample(feature))["sample_id"]
    other = str(
        app_env.execute("INSERT INTO tenant (name) VALUES ('Other Co') RETURNING id").fetchone()[0]
    )
    app_env.commit()

    assert pc.list_samples(other) == []
    assert pc.show_sample(other, sample_id, "intruder@other.com") is None
    assert pc.consent_status(other)["consent"] is None
    # And another tenant's consent does not open capture here.
    with pytest.raises(pc.CaptureRefused):
        pc.store_sample(other, sample(feature))


def test_the_retention_sweep_covers_every_tenant(app_env, tenant_id):
    feature = open_capture(app_env, tenant_id)
    pc.store_sample(tenant_id, sample(feature))
    app_env.execute("UPDATE prompt_sample SET received_at = now() - interval '40 days'")
    app_env.commit()
    swept = pc.purge_all_tenants(now=dt.datetime.now(dt.timezone.utc))
    assert [s["tenant_id"] for s in swept] == [tenant_id]
