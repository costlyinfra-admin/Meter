"""Connect with GitHub: the GitHub App path beside the pasted token."""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient
from meter import credentials, discovery, github_app
from meter.api import create_app
from meter.github import GitHubClient, GitHubError

PASSWORD = "correct horse battery"


@pytest.fixture
def app_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def configured(monkeypatch, app_key):
    pem = app_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("GITHUB_APP_ID", "424242")
    monkeypatch.setenv("GITHUB_APP_SLUG", "meter-test")
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "Iv1.client")
    monkeypatch.setenv("GITHUB_APP_CLIENT_SECRET", "client-secret")
    # As a hosting dashboard stores it: one line, with literal "\n"s.
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", pem.replace("\n", "\\n"))
    github_app._cache.clear()
    yield
    github_app._cache.clear()


def _unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


# ---------------------------------------------------------------------------
# The App's own token, and an installation's
# ---------------------------------------------------------------------------
def test_the_app_token_is_a_signed_rs256_jwt_github_can_check(configured, app_key):
    token = github_app.app_jwt(now=1_800_000_000)
    header, payload, signature = token.split(".")
    assert json.loads(_unb64(header)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(_unb64(payload))
    # Back-dated a minute for clock drift; under GitHub's ten-minute ceiling.
    assert claims == {"iat": 1_800_000_000 - 60, "exp": 1_800_000_000 + 540, "iss": "424242"}
    # Verifies against the App's public key, which is what GitHub holds.
    app_key.public_key().verify(
        _unb64(signature), f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256()
    )


def _mint_transport(calls: list, status: int = 201):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if status != 201:
            return httpx.Response(status, json={"message": "Not Found"})
        return httpx.Response(201, json={"token": f"ghs_minted{len(calls)}"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_an_installation_token_is_minted_with_the_app_token_and_reused(configured):
    calls: list = []
    client = _mint_transport(calls)
    first = github_app.installation_token(77, client=client)
    second = github_app.installation_token(77, client=client)
    assert first == second == "ghs_minted1"
    assert len(calls) == 1, "an hour-long token is reused, not re-minted per request"
    request = calls[0]
    assert request.method == "POST"
    assert request.url.path == "/app/installations/77/access_tokens"
    assert request.headers["Authorization"].startswith("Bearer ey")


def test_an_uninstalled_app_says_so_in_words(configured):
    with pytest.raises(GitHubError, match="no longer installed") as exc:
        github_app.installation_token(77, client=_mint_transport([], status=404))
    assert exc.value.status == 404


def test_a_stored_installation_resolves_to_a_fresh_token_and_a_pasted_one_to_itself(configured):
    calls: list = []
    stored = github_app.credential_secret(77, "acme")
    auth = github_app.resolve(stored, client=_mint_transport(calls))
    assert auth.token == "ghs_minted1"
    assert (auth.installation_id, auth.account, auth.is_installation) == (77, "acme", True)

    pat = github_app.resolve("ghp_pasted")
    assert (pat.token, pat.is_installation) == ("ghp_pasted", False)
    assert github_app.resolve(None) is None
    # Only the exact stored shape counts as an installation.
    assert github_app.parse_credential('{"kind": "other", "installation_id": 1}') is None
    assert github_app.parse_credential('{"kind": "github_app", "installation_id": "1"}') is None


def test_nothing_is_offered_until_every_setting_is_present(monkeypatch, configured):
    assert github_app.configured()
    monkeypatch.delenv("GITHUB_APP_CLIENT_SECRET")
    assert not github_app.configured()
    with pytest.raises(github_app.GitHubAppError, match="GITHUB_APP_CLIENT_SECRET missing"):
        # Refused before any request: no client is even passed to send one with.
        github_app.installations_for_code("abc")


# ---------------------------------------------------------------------------
# Proving the installation is the signed-in person's
# ---------------------------------------------------------------------------
def _github(installations: list, *, token_ok: bool = True, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if request.url.path == "/login/oauth/access_token":
            if not token_ok:
                return httpx.Response(200, json={"error": "bad_verification_code"})
            return httpx.Response(200, json={"access_token": "ghu_user", "token_type": "bearer"})
        if request.url.path == "/user/installations":
            assert request.headers["Authorization"] == "Bearer ghu_user"
            return httpx.Response(200, json={"installations": installations})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _inst(iid: int, login: str, selection: str = "selected") -> dict:
    return {"id": iid, "account": {"login": login, "type": "Organization"},
            "repository_selection": selection}  # fmt: skip


def test_a_code_is_exchanged_once_and_answers_which_installations_are_yours(configured):
    seen: list = []
    found = github_app.installations_for_code(
        "code-1", client=_github([_inst(77, "acme"), _inst(78, "acme-labs", "all")], seen=seen)
    )
    assert found == [
        {"installation_id": 77, "account": "acme", "account_type": "Organization",
         "repository_selection": "selected"},
        {"installation_id": 78, "account": "acme-labs", "account_type": "Organization",
         "repository_selection": "all"},
    ]  # fmt: skip
    exchange = seen[0]
    assert exchange.method == "POST"
    form = dict(httpx.QueryParams(exchange.content.decode()))
    assert form == {"client_id": "Iv1.client", "client_secret": "client-secret", "code": "code-1"}


def test_an_expired_code_is_a_readable_refusal(configured):
    with pytest.raises(github_app.GitHubAppError, match="did not accept the sign-in"):
        github_app.installations_for_code("stale", client=_github([], token_ok=False))


def test_a_claim_is_bound_to_its_tenant_and_person_and_expires(configured, monkeypatch):
    claim = github_app.make_claim("t1", "u1", 77, "acme")
    assert github_app.read_claim(claim, "t1", "u1") == (77, "acme")
    with pytest.raises(github_app.GitHubAppError, match="someone else"):
        github_app.read_claim(claim, "t2", "u1")
    with pytest.raises(github_app.GitHubAppError, match="someone else"):
        github_app.read_claim(claim, "t1", "u2")
    with pytest.raises(github_app.GitHubAppError, match="not valid"):
        github_app.read_claim(claim[:-2] + "xx", "t1", "u1")
    later = time.time() + github_app.CLAIM_MAX_AGE_SECONDS + 5
    monkeypatch.setattr(time, "time", lambda: later)
    with pytest.raises(github_app.GitHubAppError, match="expired"):
        github_app.read_claim(claim, "t1", "u1")


# ---------------------------------------------------------------------------
# Reading with an installation token
# ---------------------------------------------------------------------------
def test_an_installation_lists_only_the_repositories_it_was_given():
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/installation/repositories":
            return httpx.Response(
                200,
                json={
                    "total_count": 2,
                    "repositories": [{"full_name": "acme/api"}, {"full_name": "acme/web"}],
                },
            )
        return httpx.Response(500)

    gh = GitHubClient("ghs_install", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert gh.list_repos("ACME") == ["acme/api", "acme/web"]
    # Never the org listing: that would add every public repo nobody chose.
    assert seen == ["/installation/repositories"]
    with pytest.raises(GitHubError, match="can't see any repositories under 'other'"):
        gh.list_repos("other")


# ---------------------------------------------------------------------------
# Through the API
# ---------------------------------------------------------------------------
@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch, configured):
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cto@acme.com", "password": PASSWORD})
    return c


def _signed_in_as_owner_of(monkeypatch, installations: list):
    """GitHub, as seen through a person who can see these installations."""
    monkeypatch.setattr(
        github_app,
        "installations_for_code",
        lambda code, **_: [
            {"installation_id": i, "account": login, "account_type": "Organization",
             "repository_selection": "selected"}
            for i, login in installations
        ] if code == "good-code" else (_ for _ in ()).throw(
            github_app.GitHubAppError("GitHub did not accept the sign-in.")
        ),
    )  # fmt: skip


def _tenant(client) -> str:
    return client.get("/api/auth/me").json()["tenant_id"]


def test_signing_in_and_confirming_connects_the_installation(client, monkeypatch):
    status = client.get("/api/github/app").json()
    assert status == {
        "configured": True,
        "install_url": "https://github.com/apps/meter-test/installations/new",
        "connection": None,
    }
    _signed_in_as_owner_of(monkeypatch, [(77, "acme")])

    verified = client.post(
        "/api/github/app/verify", json={"code": "good-code", "installation_id": 77}
    ).json()["installations"]
    assert [(i["installation_id"], i["account"]) for i in verified] == [(77, "acme")]
    # Verifying stores nothing: connecting is a separate, deliberate click.
    assert client.get("/api/github/app").json()["connection"] is None

    done = client.post("/api/github/app/connect", json={"claim": verified[0]["claim"]})
    assert done.status_code == 200
    assert done.json()["connection"] == {
        "installation_id": 77,
        "account": "acme",
        "manage_url": "https://github.com/apps/meter-test/installations/77",
    }
    tenant = _tenant(client)
    github = next(c for c in client.get("/api/connectors").json() if c["type"] == "github")
    assert github["connected"] is True
    assert credentials.list_credentials(tenant, "github")[0]["label"] == "acme"
    # Discovery now points at the organization that was just connected.
    assert discovery.get_scope(tenant) == {"owner": "acme", "repos": []}

    # And every reader gets a minted token, never the stored installation.
    monkeypatch.setattr(github_app, "installation_token", lambda iid, **_: f"ghs_for_{iid}")
    assert github_app.tenant_token(tenant) == "ghs_for_77"


def test_a_forged_installation_id_is_refused(client, monkeypatch):
    # GitHub's own warning: the installation_id in the return URL can be typed
    # by anyone. The person signed in can see 77; the URL claims 99.
    _signed_in_as_owner_of(monkeypatch, [(77, "acme")])
    resp = client.post("/api/github/app/verify", json={"code": "good-code", "installation_id": 99})
    assert resp.status_code == 400
    assert "can't see that installation" in resp.json()["detail"]
    assert client.get("/api/github/app").json()["connection"] is None


def test_a_bad_code_stores_nothing(client, monkeypatch):
    _signed_in_as_owner_of(monkeypatch, [(77, "acme")])
    resp = client.post("/api/github/app/verify", json={"code": "stale"})
    assert resp.status_code == 400
    assert "did not accept" in resp.json()["detail"]


def test_a_claim_from_another_tenant_connects_nothing(client, monkeypatch):
    _signed_in_as_owner_of(monkeypatch, [(77, "acme")])
    foreign = github_app.make_claim("someone-else", "u", 77, "acme")
    resp = client.post("/api/github/app/connect", json={"claim": foreign})
    assert resp.status_code == 400
    assert client.get("/api/github/app").json()["connection"] is None


def test_an_installation_cannot_be_pasted_in_through_the_token_box(client):
    # The verified path is the only one allowed to store this shape: a pasted
    # one would mint tokens for someone else's organization.
    resp = client.post(
        "/api/connectors/github/credential",
        json={"secret": github_app.credential_secret(99, "victim")},
    )
    assert resp.status_code == 400
    assert "Connect with GitHub" in resp.json()["detail"]
    connectors = client.get("/api/connectors").json()
    assert not any(c["type"] == "github" and c["connected"] for c in connectors)


def test_a_pasted_token_still_works_and_reads_as_itself(client):
    resp = client.post("/api/connectors/github/credential", json={"secret": "ghp_pasted"})
    assert resp.status_code == 204
    tenant = _tenant(client)
    assert github_app.tenant_token(tenant) == "ghp_pasted"
    # A token connection is not an installation, so there is nothing to manage on GitHub.
    assert client.get("/api/github/app").json()["connection"] is None


def test_an_unconfigured_deployment_offers_no_sign_in(client, monkeypatch):
    class NoNetwork:
        def Client(*_a, **_k):  # noqa: N802 — stands in for httpx.Client
            raise AssertionError("an unconfigured deployment must not call GitHub")

    monkeypatch.setattr(github_app, "httpx", NoNetwork)
    monkeypatch.delenv("GITHUB_APP_SLUG")
    assert client.get("/api/github/app").json() == {
        "configured": False,
        "install_url": None,
        "connection": None,
    }
    resp = client.post("/api/github/app/verify", json={"code": "good-code"})
    assert resp.status_code == 400
    assert "not set up" in resp.json()["detail"]


def test_an_uninstalled_app_fails_discovery_with_a_reason_not_a_crash(client, monkeypatch):
    _signed_in_as_owner_of(monkeypatch, [(77, "acme")])
    claim = client.post("/api/github/app/verify", json={"code": "good-code"}).json()[
        "installations"
    ][0]["claim"]
    client.post("/api/github/app/connect", json={"claim": claim})

    def gone(iid, **_):
        raise GitHubError("Meter's GitHub App is no longer installed on this organization.", 404)

    monkeypatch.setattr(github_app, "installation_token", gone)
    resp = client.get("/api/discovery/repos", params={"owner": "acme"})
    assert resp.status_code == 400
    assert "no longer installed" in resp.json()["detail"]
