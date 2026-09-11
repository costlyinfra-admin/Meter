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


def test_saving_again_replaces_rather_than_accumulates(tenant_id):
    # Rotation is usually a response to a token being leaked. Keeping the old
    # ciphertext would defeat it: the compromised secret would stay in the
    # database, decryptable with the same key, indefinitely.
    credentials.save_credential(tenant_id, "github", "ghp_old")
    credentials.save_credential(tenant_id, "github", "ghp_new")

    assert credentials.get_secret(tenant_id, "github") == "ghp_new"
    assert len(_rows(tenant_id, "github")) == 1


def test_the_old_secret_is_not_recoverable_after_replacement(tenant_id):
    # The strong form of the rule: decrypt everything still stored for this
    # connector and check the previous secret is not among it. This is a claim
    # about the row being gone, not about the encryption holding.
    credentials.save_credential(tenant_id, "github", "ghp_leaked")
    credentials.save_credential(tenant_id, "github", "ghp_fresh")

    remaining = [crypto.decrypt(bytes(r[0])) for r in _rows(tenant_id, "github")]
    assert remaining == ["ghp_fresh"]
    assert "ghp_leaked" not in remaining


def test_replacing_one_connector_leaves_the_others_alone(tenant_id):
    credentials.save_credential(tenant_id, "github", "ghp_a")
    credentials.save_credential(tenant_id, "anthropic", "sk-ant-a")
    credentials.save_credential(tenant_id, "github", "ghp_b")

    assert credentials.get_secret(tenant_id, "github") == "ghp_b"
    assert credentials.get_secret(tenant_id, "anthropic") == "sk-ant-a"


def test_an_empty_credential_is_refused(tenant_id):
    # An accidental save must never blank out a working connector.
    credentials.save_credential(tenant_id, "github", "ghp_real")
    for blank in ("", "   "):
        with pytest.raises(ValueError):
            credentials.save_credential(tenant_id, "github", blank)
    assert credentials.get_secret(tenant_id, "github") == "ghp_real"


def test_status_reports_when_a_credential_was_set_and_never_the_secret(tenant_id):
    credentials.save_credential(tenant_id, "github", "ghp_secret_value")
    status = {c["type"]: c for c in credentials.connector_statuses(tenant_id)}

    assert status["github"]["connected"] is True
    assert status["github"]["credential_set_at"] is not None
    # The secret must not appear anywhere in what the API is about to serialize.
    assert "ghp_secret_value" not in str(status)
    # An unconnected connector says so, with no date to imply otherwise.
    assert status["anthropic"]["connected"] is False
    assert status["anthropic"]["credential_set_at"] is None
