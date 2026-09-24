"""The MCP credential, and how fast a client may ask.

A token is the thing that replaces a person's password in an agent's config, so
the properties that matter are: it is shown once, it is revocable on its own, it
cannot reach another organization, and it records that it was used.
"""

from __future__ import annotations

import pytest
from meter import db, ratelimit
from meter.mcp import session, tokens


@pytest.fixture
def other_tenant(app_env):
    row = app_env.execute("INSERT INTO tenant (name) VALUES ('Other') RETURNING id").fetchone()
    app_env.commit()
    return str(row[0])


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------
def test_the_token_is_shown_once_and_stored_only_as_a_hash(tenant_id, app_env):
    made = tokens.create(tenant_id, "laptop")
    assert made["token"].startswith(tokens.TOKEN_PREFIX)

    stored = app_env.execute("SELECT token_hash FROM mcp_token").fetchall()
    assert len(stored) == 1
    # The plaintext appears nowhere in the row, and the hash is not the token.
    assert stored[0][0] != made["token"]
    assert made["token"] not in stored[0][0]

    # And it is not recoverable afterwards through any listing.
    assert all("token" not in row for row in tokens.list_tokens(tenant_id))


def test_two_tokens_are_different(tenant_id):
    first = tokens.create(tenant_id, "laptop")
    second = tokens.create(tenant_id, "ci")
    assert first["token"] != second["token"]
    assert tokens.resolve(first["token"]) == tokens.resolve(second["token"]) == tenant_id


def test_a_token_needs_a_label_that_identifies_a_machine(tenant_id):
    # Several per tenant is the point; without labels, revoking the right one
    # is guesswork.
    with pytest.raises(tokens.TokenError):
        tokens.create(tenant_id, "   ")
    with pytest.raises(tokens.TokenError):
        tokens.create(tenant_id, "x" * (tokens.MAX_LABEL + 1))


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------
def test_an_unknown_token_resolves_to_nobody(tenant_id):
    tokens.create(tenant_id, "laptop")
    assert tokens.resolve("mtr_mcp_invented") is None
    assert tokens.resolve("") is None


def test_using_a_token_records_when(tenant_id):
    made = tokens.create(tenant_id, "laptop")
    assert tokens.list_tokens(tenant_id)[0]["last_used_at"] is None
    tokens.resolve(made["token"])
    assert tokens.list_tokens(tenant_id)[0]["last_used_at"] is not None


def test_it_resolves_under_the_read_only_role(tenant_id, monkeypatch, read_conninfo):
    # The server holds SELECT and no privilege at all on mcp_token: the lookup
    # goes through a SECURITY DEFINER function. If that ever stopped working,
    # the server would not start.
    made = tokens.create(tenant_id, "laptop")
    monkeypatch.setattr(db, "_read_only", True)
    monkeypatch.setenv("DATABASE_READ_URL", read_conninfo)
    assert tokens.resolve(made["token"]) == tenant_id


# ---------------------------------------------------------------------------
# Revoking
# ---------------------------------------------------------------------------
def test_revoking_one_token_leaves_the_others_working(tenant_id):
    laptop = tokens.create(tenant_id, "laptop")
    ci = tokens.create(tenant_id, "ci")
    tokens.revoke(tenant_id, laptop["id"])

    assert tokens.resolve(laptop["token"]) is None
    assert tokens.resolve(ci["token"]) == tenant_id


def test_a_revoked_token_is_kept_as_history_not_deleted(tenant_id):
    made = tokens.create(tenant_id, "laptop")
    tokens.revoke(tenant_id, made["id"])
    listed = tokens.list_tokens(tenant_id)
    # "It worked until Tuesday, then stopped" is the answer to a security
    # question; DELETE would destroy it.
    assert len(listed) == 1
    assert listed[0]["active"] is False
    assert listed[0]["revoked_at"]


def test_revoking_twice_keeps_the_first_time(tenant_id):
    made = tokens.create(tenant_id, "laptop")
    first = tokens.revoke(tenant_id, made["id"])["revoked_at"]
    assert tokens.revoke(tenant_id, made["id"])["revoked_at"] == first


def test_revoking_something_that_does_not_exist_says_so(tenant_id):
    with pytest.raises(tokens.TokenError):
        tokens.revoke(tenant_id, "00000000-0000-0000-0000-000000000000")


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------
def test_one_organization_cannot_see_or_revoke_anothers_tokens(tenant_id, other_tenant):
    mine = tokens.create(tenant_id, "laptop")
    theirs = tokens.create(other_tenant, "their laptop")

    assert [t["id"] for t in tokens.list_tokens(tenant_id)] == [mine["id"]]
    with pytest.raises(tokens.TokenError):
        tokens.revoke(tenant_id, theirs["id"])
    # ...and it still works, because nothing happened to it.
    assert tokens.resolve(theirs["token"]) == other_tenant


def test_a_token_resolves_to_its_own_organization(tenant_id, other_tenant):
    mine = tokens.create(tenant_id, "laptop")
    theirs = tokens.create(other_tenant, "their laptop")
    assert tokens.resolve(mine["token"]) == tenant_id
    assert tokens.resolve(theirs["token"]) == other_tenant


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
def test_a_client_in_a_loop_is_stopped():
    session.reset_rate()
    for _ in range(session.RATE_LIMIT):
        session.check_rate("tenant-a")
    with pytest.raises(ratelimit.RateLimited):
        session.check_rate("tenant-a")


def test_the_limit_is_per_organization():
    session.reset_rate()
    for _ in range(session.RATE_LIMIT):
        session.check_rate("tenant-a")
    session.check_rate("tenant-b")  # unaffected


def test_the_window_moves():
    limiter = ratelimit.Limiter(2, 60.0, "slow down")
    limiter.check("t", now=0.0)
    limiter.check("t", now=1.0)
    with pytest.raises(ratelimit.RateLimited):
        limiter.check("t", now=2.0)
    # Once the first event is older than the window, there is room again.
    limiter.check("t", now=62.0)


def test_the_message_says_what_to_do():
    session.reset_rate()
    for _ in range(session.RATE_LIMIT):
        session.check_rate("tenant-a")
    with pytest.raises(ratelimit.RateLimited) as caught:
        session.check_rate("tenant-a")
    assert "Wait" in str(caught.value)
