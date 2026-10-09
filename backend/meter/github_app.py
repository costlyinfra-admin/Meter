"""Connecting GitHub by signing in, through Meter's GitHub App.

The second way to connect GitHub, beside pasting a personal access token. An
organization admin clicks "Connect with GitHub", signs in on github.com, picks
the organization and the repositories Meter may read, and comes back. Meter
never sees their password and never stores a long-lived token: what it keeps is
the installation's id, and every read mints a fresh one-hour token for that
installation from Meter's own App key.

Why an App rather than "Sign in with GitHub" (an OAuth App): an OAuth token that
can read private repositories needs the `repo` scope, which can also WRITE to
every one of them. A GitHub App's permissions are set once, by Meter, to read
pull requests and nothing else — invariant 6 (every connector read-only) is
enforced by GitHub, not by Meter's good behaviour — and the customer chooses
the repositories on GitHub's own screen.

Proving whose installation it is
--------------------------------
GitHub sends the browser back with an `installation_id`, and its documentation
is explicit that the parameter can be forged: anyone can type a URL with some
other organization's installation id in it. So an id is never trusted on its
own. The App asks GitHub to have the person authorize it during installation,
which brings back a one-time `code`; Meter exchanges that for a user token, asks
GitHub which installations of this App that person can see, and only accepts
one that appears there. The user token is used for that one question and
discarded — it is never stored.

A connection is then confirmed by a click in Meter, against a short-lived signed
claim bound to the tenant and the person. A link someone else crafted with their
own code and installation would otherwise connect the victim's tenant to the
attacker's repositories without the victim doing anything but opening it.

Configuration (all five, or the option is not offered):
  GITHUB_APP_ID, GITHUB_APP_SLUG, GITHUB_APP_CLIENT_ID,
  GITHUB_APP_CLIENT_SECRET, GITHUB_APP_PRIVATE_KEY
See docs/deploy.md, "Connect with GitHub".
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .github import GITHUB_API, GitHubError

GITHUB_WEB = "https://github.com"

#: The marker a stored GitHub credential carries when it is an installation
#: rather than a pasted token. A personal access token never starts with "{".
CREDENTIAL_KIND = "github_app"

#: How long a "connect this installation?" claim stays valid. Long enough to
#: read the confirmation, short enough that a leaked one is not worth much.
CLAIM_MAX_AGE_SECONDS = 15 * 60

_ENV = (
    "GITHUB_APP_ID",
    "GITHUB_APP_SLUG",
    "GITHUB_APP_CLIENT_ID",
    "GITHUB_APP_CLIENT_SECRET",
    "GITHUB_APP_PRIVATE_KEY",
)


class GitHubAppError(GitHubError):
    """Connecting through the App failed, with a message a person can act on.

    A GitHubError, so every route that already turns GitHub failures into a
    readable answer handles this one too."""

    def __init__(self, message: str):
        super().__init__(message, 400)


@dataclass(frozen=True)
class GitHubAuth:
    """How to read GitHub for one tenant: a token, and where it came from."""

    token: str
    #: The installation the token was minted for, or None for a pasted token.
    installation_id: Optional[int] = None
    #: The organization or user the installation is on.
    account: Optional[str] = None

    @property
    def is_installation(self) -> bool:
        return self.installation_id is not None


def configured() -> bool:
    """Whether this deployment can offer "Connect with GitHub" at all."""
    return all(os.environ.get(name) for name in _ENV)


def _require_configured() -> None:
    """Refuse up front, naming what is missing, rather than half-working: a
    deployment with a client id but no slug would sign people in and then have
    nowhere to send them."""
    missing = [name for name in _ENV if not os.environ.get(name)]
    if missing:
        raise GitHubAppError(
            "Connecting with GitHub is not set up on this Meter deployment "
            f"({', '.join(missing)} missing). Paste a personal access token instead."
        )


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise GitHubAppError(
            "Connecting with GitHub is not set up on this Meter deployment "
            f"({name} is missing). Paste a personal access token instead."
        )
    return value


# ---------------------------------------------------------------------------
# Stored form
# ---------------------------------------------------------------------------
def credential_secret(installation_id: int, account: str) -> str:
    """What is stored (encrypted) for an installation. Not itself a secret —
    an installation id is useless without Meter's App key — but it lives in
    the credential store so a tenant has one GitHub connection, either kind."""
    return json.dumps(
        {"kind": CREDENTIAL_KIND, "installation_id": installation_id, "account": account}
    )


def parse_credential(secret: str) -> Optional[dict]:
    """The installation a stored secret names, or None for a pasted token."""
    if not secret.startswith("{"):
        return None
    try:
        data = json.loads(secret)
    except ValueError:
        return None
    if data.get("kind") != CREDENTIAL_KIND or not isinstance(data.get("installation_id"), int):
        return None
    return data


def resolve(
    secret: Optional[str], *, client: Optional[httpx.Client] = None
) -> Optional[GitHubAuth]:
    """The token to read GitHub with, from whatever the tenant stored.

    A pasted token is used as it is. An installation gets a fresh token minted
    for it. None means nothing is stored — public repositories only.
    """
    if not secret:
        return None
    installation = parse_credential(secret)
    if installation is None:
        return GitHubAuth(token=secret)
    iid = installation["installation_id"]
    return GitHubAuth(
        token=installation_token(iid, client=client),
        installation_id=iid,
        account=installation.get("account"),
    )


def tenant_token(tenant_id: str) -> Optional[str]:
    """The token to read this tenant's GitHub with, whichever way it connected.

    Every reader goes through here rather than reading the stored secret: a
    stored installation is not a token, and passing it to GitHub as one would
    fail as "bad credentials" with no hint why.
    """
    from . import credentials  # here, not at the top: credentials is imported widely

    auth = resolve(credentials.get_secret(tenant_id, "github"))
    return auth.token if auth else None


# ---------------------------------------------------------------------------
# Starting: where to send the browser
# ---------------------------------------------------------------------------
def install_url() -> str:
    """GitHub's own screen for choosing an organization and its repositories."""
    return f"{GITHUB_WEB}/apps/{_env('GITHUB_APP_SLUG')}/installations/new"


def manage_url(installation_id: int) -> str:
    """Where an admin changes which repositories Meter may read, or removes it."""
    return f"{GITHUB_WEB}/apps/{_env('GITHUB_APP_SLUG')}/installations/{installation_id}"


# ---------------------------------------------------------------------------
# Coming back: prove the installation, then claim it
# ---------------------------------------------------------------------------
def _serializer() -> URLSafeTimedSerializer:
    secret = os.environ.get("APP_SECRET_KEY")
    if not secret:
        raise RuntimeError("APP_SECRET_KEY is not set.")
    return URLSafeTimedSerializer(secret, salt="github-app-connect")


def make_claim(tenant_id: str, user_id: str, installation_id: int, account: str) -> str:
    return _serializer().dumps({"t": tenant_id, "u": user_id, "i": installation_id, "a": account})


def read_claim(claim: str, tenant_id: str, user_id: str) -> tuple[int, str]:
    """The installation a claim names, if it was issued to this person in this
    tenant within the last few minutes."""
    try:
        data = _serializer().loads(claim, max_age=CLAIM_MAX_AGE_SECONDS)
    except SignatureExpired as exc:
        raise GitHubAppError("That confirmation has expired. Connect with GitHub again.") from exc
    except BadSignature as exc:
        raise GitHubAppError("That confirmation is not valid. Connect with GitHub again.") from exc
    if data.get("t") != tenant_id or data.get("u") != user_id:
        raise GitHubAppError("That confirmation was issued to someone else.")
    return int(data["i"]), str(data["a"])


def installations_for_code(code: str, *, client: Optional[httpx.Client] = None) -> list[dict]:
    """The installations of Meter's App that the person behind `code` can see.

    The one-time code is exchanged for a user token, which is asked one
    question and then dropped. Each installation is returned as
    {installation_id, account, account_type, repository_selection}.
    """
    _require_configured()
    owns = client is None
    http = client or httpx.Client(timeout=20.0)
    try:
        resp = http.post(
            f"{GITHUB_WEB}/login/oauth/access_token",
            data={
                "client_id": _env("GITHUB_APP_CLIENT_ID"),
                "client_secret": _env("GITHUB_APP_CLIENT_SECRET"),
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
        body = resp.json() if resp.content else {}
        user_token = body.get("access_token")
        if resp.status_code != 200 or not user_token:
            # bad_verification_code is the usual one: the code is single-use and
            # expires after ten minutes, so a refreshed page lands here.
            raise GitHubAppError(
                "GitHub did not accept the sign-in (it may have expired). "
                "Connect with GitHub again."
            )
        found: list[dict] = []
        page = 1
        while True:
            listing = http.get(
                f"{GITHUB_API}/user/installations",
                params={"per_page": 100, "page": page},
                headers={
                    "Authorization": f"Bearer {user_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            if listing.status_code != 200:
                raise GitHubAppError("GitHub would not list your installations of Meter.")
            data = listing.json()
            for inst in data.get("installations", []):
                account = inst.get("account") or {}
                found.append(
                    {
                        "installation_id": int(inst["id"]),
                        "account": account.get("login", ""),
                        "account_type": account.get("type", ""),
                        "repository_selection": inst.get("repository_selection", ""),
                    }
                )
            if len(data.get("installations", [])) < 100:
                return found
            page += 1
    finally:
        if owns:
            http.close()


# ---------------------------------------------------------------------------
# Reading: an installation token per request
# ---------------------------------------------------------------------------
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(now: Optional[float] = None) -> str:
    """A ten-minute token that proves a request comes from Meter's App.

    RS256 by hand with `cryptography`, already a dependency, rather than a JWT
    library for one signature. Issued a minute in the past, as GitHub advises,
    so a slightly fast clock here is not a rejected token there.
    """
    now = int(now if now is not None else time.time())
    pem = _env("GITHUB_APP_PRIVATE_KEY").replace("\\n", "\n").encode()
    key = serialization.load_pem_private_key(pem, password=None)
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64(
        json.dumps({"iat": now - 60, "exp": now + 9 * 60, "iss": _env("GITHUB_APP_ID")}).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr]
    return f"{header}.{payload}.{_b64(signature)}"


_cache: dict[int, tuple[str, float]] = {}
_cache_lock = threading.Lock()
#: Re-mint this long before GitHub's one-hour expiry, so a token is never
#: handed to a discovery run that will outlive it.
_REFRESH_MARGIN_SECONDS = 10 * 60


def installation_token(installation_id: int, *, client: Optional[httpx.Client] = None) -> str:
    """A token that reads what the installation was granted, for about an hour.

    Minting is a POST, the one non-GET request Meter makes to GitHub — and it is
    to GitHub's App endpoint, about Meter's own App; nothing in the customer's
    repositories is touched. The token carries only the App's read permissions.
    """
    now = time.time()
    with _cache_lock:
        held = _cache.get(installation_id)
        if held and held[1] - _REFRESH_MARGIN_SECONDS > now:
            return held[0]
    owns = client is None
    http = client or httpx.Client(timeout=20.0)
    try:
        resp = http.post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt(now)}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    finally:
        if owns:
            http.close()
    if resp.status_code == 404:
        # Uninstalled, or suspended, on GitHub's side. That is the customer's
        # call and a real answer; say what it means rather than "error 404".
        raise GitHubError(
            "Meter's GitHub App is no longer installed on this organization. "
            "Connect with GitHub again, or paste a token.",
            404,
        )
    if resp.status_code != 201:
        raise GitHubError(
            f"GitHub would not issue a token for the installation ({resp.status_code}).",
            resp.status_code,
        )
    token = resp.json()["token"]
    with _cache_lock:
        # GitHub's expiry is an hour; the margin above keeps clear of it.
        _cache[installation_id] = (token, now + 60 * 60)
    return token


def forget(installation_id: int) -> None:
    """Drop a cached token, so a disconnected installation is not read again."""
    with _cache_lock:
        _cache.pop(installation_id, None)


# ---------------------------------------------------------------------------
# The tenant's connection
# ---------------------------------------------------------------------------
def connect(tenant_id: str, installation_id: int, account: str) -> None:
    """Store a VERIFIED installation as the tenant's GitHub connection.

    Replaces whatever was there, a pasted token included: discovery reads one
    GitHub credential. Only call this with an installation that came through
    `installations_for_code` and a claim — it is the one path allowed to store
    an installation (see credentials.save_credential).
    """
    from . import credentials

    credentials.save_credential(
        tenant_id,
        "github",
        credential_secret(installation_id, account),
        label=account,
        verified_installation=True,
    )
    forget(installation_id)


def connection(tenant_id: str) -> Optional[dict]:
    """The installation the tenant is connected through, or None (a pasted
    token, or nothing at all)."""
    from . import credentials

    secret = credentials.get_secret(tenant_id, "github")
    installation = parse_credential(secret) if secret else None
    if installation is None:
        return None
    iid = installation["installation_id"]
    return {
        "installation_id": iid,
        "account": installation.get("account"),
        "manage_url": manage_url(iid) if configured() else None,
    }
