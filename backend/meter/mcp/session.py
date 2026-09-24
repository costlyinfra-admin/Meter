"""Who the MCP server is acting as.

One process serves one tenant. That is deliberate: the server runs on a
developer's machine next to their editor, and the alternative — a process that
can reach several tenants and picks one per call — would put tenant selection in
a tool argument, where a confused model could change it.

The credential is the user's own Meter login, checked by `auth.login()` — the
same bcrypt comparison the web app makes. No new authentication model, no new
table, and no privilege that a person signing in does not already have.

That is also the known rough edge: a password in an environment variable is a
worse secret than a scoped read-only token would be. Meter has no such token
today (`hook_token` is write-scoped, for ingest, and widening it to reads would
make a write credential more powerful), so this uses what exists. It is checked
once, at startup, and neither the password nor the email is logged, returned by
a tool, or kept after the tenant is resolved.
"""

from __future__ import annotations

import os

from .. import auth

#: Where the server reads its credential from. Named for the product, not for
#: MCP: the same two variables would serve any other headless Meter client.
EMAIL_VAR = "METER_EMAIL"
PASSWORD_VAR = "METER_PASSWORD"


class NotAuthenticated(Exception):
    """The server has no usable Meter identity, so it must not start."""


def resolve_tenant() -> str:
    """The tenant this process serves, or raise.

    Deliberately fails at startup rather than on the first tool call: a client
    that connects successfully and then fails every query looks like a Meter
    outage, when it is a missing environment variable.
    """
    email = os.environ.get(EMAIL_VAR, "").strip()
    password = os.environ.get(PASSWORD_VAR, "")
    if not email or not password:
        raise NotAuthenticated(
            f"Set {EMAIL_VAR} and {PASSWORD_VAR} to a Meter login. "
            "The server reads your organization's data as that user."
        )
    user = auth.login(email, password)
    if user is None:
        # No detail about which half was wrong: this message can reach a log.
        raise NotAuthenticated("Meter rejected those credentials.")
    return user["tenant_id"]
