"""Connector credentials: storage, replacement, and what never leaves the server."""

from __future__ import annotations

import pytest
from meter import credentials, crypto
from meter.db import app_dsn, connect, tenant_tx


def _rows(tenant_id: str, connector_type: str) -> list:
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return conn.execute(
            "SELECT ciphertext FROM connector_credential WHERE connector_type = %s",
            (connector_type,),
        ).fetchall()


def test_replacing_a_named_credential_overwrites_it(tenant_id):
    # Rotation is usually a response to a token being leaked. Keeping the old
    # ciphertext would defeat it: the compromised secret would stay in the
    # database, decryptable with the same key, indefinitely.
    cid = credentials.save_credential(tenant_id, "github", "ghp_old")
    credentials.save_credential(tenant_id, "github", "ghp_new", credential_id=cid)

    assert credentials.get_secret(tenant_id, "github") == "ghp_new"
    assert len(_rows(tenant_id, "github")) == 1


def test_saving_without_naming_one_adds_a_second_account(tenant_id):
    # Two Anthropic organisations billed separately are one connector with two
    # keys. Collapsing them would show only whichever synced last.
    first = credentials.save_credential(tenant_id, "anthropic", "sk-ant-a", label="Acme")
    second = credentials.save_credential(tenant_id, "anthropic", "sk-ant-b", label="Acme Labs")

    assert first != second
    assert sorted(s for _, s in credentials.get_secrets(tenant_id, "anthropic")) == [
        "sk-ant-a",
        "sk-ant-b",
    ]
    assert [c["label"] for c in credentials.list_credentials(tenant_id, "anthropic")] == [
        "Acme",
        "Acme Labs",
    ]


def test_the_old_secret_is_not_recoverable_after_replacement(tenant_id):
    # The strong form of the rule: decrypt everything still stored for this
    # connector and check the previous secret is not among it. This is a claim
    # about the row being gone, not about the encryption holding.
    cid = credentials.save_credential(tenant_id, "github", "ghp_leaked")
    credentials.save_credential(tenant_id, "github", "ghp_fresh", credential_id=cid)

    remaining = [crypto.decrypt(bytes(r[0])) for r in _rows(tenant_id, "github")]
    assert remaining == ["ghp_fresh"]
    assert "ghp_leaked" not in remaining


def test_replacing_one_connector_leaves_the_others_alone(tenant_id):
    gh = credentials.save_credential(tenant_id, "github", "ghp_a")
    credentials.save_credential(tenant_id, "anthropic", "sk-ant-a")
    credentials.save_credential(tenant_id, "github", "ghp_b", credential_id=gh)

    assert credentials.get_secret(tenant_id, "github") == "ghp_b"
    assert credentials.get_secret(tenant_id, "anthropic") == "sk-ant-a"


def test_removing_one_account_leaves_the_other_connected(tenant_id):
    a = credentials.save_credential(tenant_id, "anthropic", "sk-ant-a", label="Acme")
    credentials.save_credential(tenant_id, "anthropic", "sk-ant-b", label="Acme Labs")

    assert credentials.delete_credential(tenant_id, "anthropic", a) is True
    assert [s for _, s in credentials.get_secrets(tenant_id, "anthropic")] == ["sk-ant-b"]
    status = {c["type"]: c for c in credentials.connector_statuses(tenant_id)}
    assert status["anthropic"]["connected"] is True
    assert status["anthropic"]["credential_count"] == 1
    # Removing one that is already gone is not an error, it is just False.
    assert credentials.delete_credential(tenant_id, "anthropic", a) is False


def test_replacing_a_credential_that_no_longer_exists_is_refused(tenant_id):
    import uuid

    with pytest.raises(ValueError):
        credentials.save_credential(
            tenant_id, "anthropic", "sk-ant-x", credential_id=str(uuid.uuid4())
        )


def test_an_empty_credential_is_refused(tenant_id):
    # An accidental save must never blank out a working connector.
    cid = credentials.save_credential(tenant_id, "github", "ghp_real")
    for blank in ("", "   "):
        with pytest.raises(ValueError):
            credentials.save_credential(tenant_id, "github", blank, credential_id=cid)
    assert credentials.get_secret(tenant_id, "github") == "ghp_real"


def test_listing_credentials_never_includes_the_secret(tenant_id):
    credentials.save_credential(tenant_id, "anthropic", "sk-ant-SECRET-VALUE", label="Acme")
    listed = credentials.list_credentials(tenant_id, "anthropic")
    assert [c["label"] for c in listed] == ["Acme"]
    assert "sk-ant-SECRET-VALUE" not in str(listed)
    assert all("ciphertext" not in c and "secret" not in c for c in listed)


def test_status_reports_when_a_credential_was_set_and_never_the_secret(tenant_id):
    credentials.save_credential(tenant_id, "github", "ghp_secret_value")
    status = {c["type"]: c for c in credentials.connector_statuses(tenant_id)}

    assert status["github"]["connected"] is True
    assert status["github"]["credential_set_at"] is not None
    assert status["github"]["credential_count"] == 1
    # The secret must not appear anywhere in what the API is about to serialize.
    assert "ghp_secret_value" not in str(status)
    # An unconnected connector says so, with no date to imply otherwise.
    assert status["anthropic"]["connected"] is False
    assert status["anthropic"]["credential_set_at"] is None
    assert status["anthropic"]["credential_count"] == 0


def test_a_connector_whose_sync_reads_one_key_keeps_one(tenant_id):
    # GitHub's sync reads a single token. Accepting a second would store a key
    # that is never fetched with, and the customer would believe otherwise.
    assert credentials.supports_multiple("github") is False
    credentials.save_credential(tenant_id, "github", "ghp_first")
    credentials.save_credential(tenant_id, "github", "ghp_second")

    assert len(credentials.list_credentials(tenant_id, "github")) == 1
    assert credentials.get_secret(tenant_id, "github") == "ghp_second"


def test_inference_connectors_hold_several_accounts(tenant_id):
    # Anthropic's sync fetches from every key before writing the month, so two
    # organisations can be connected and both are billed.
    assert credentials.supports_multiple("anthropic") is True
    credentials.save_credential(tenant_id, "anthropic", "sk-a", label="Acme")
    credentials.save_credential(tenant_id, "anthropic", "sk-b", label="Labs")
    assert len(credentials.list_credentials(tenant_id, "anthropic")) == 2


def test_status_says_whether_a_second_key_would_be_read(tenant_id):
    status = {c["type"]: c for c in credentials.connector_statuses(tenant_id)}
    # Inference, infrastructure and build-activity syncs each fetch from every
    # credential before writing, so a second account is really read.
    for connector in ("anthropic", "aws", "gcp", "cursor", "okta"):
        assert status[connector]["supports_multiple"] is True, connector
    # GitHub reads one token and scans one owner: a second would change what is
    # scanned rather than add to it, which is a different question.
    assert status["github"]["supports_multiple"] is False
