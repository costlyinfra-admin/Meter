"""`python -m meter.mcp.mint` — create, list and revoke MCP tokens.

Run where Meter's database is reachable, by someone who can already sign in to
Meter. It is the bootstrap: the MCP server authenticates with a token, and this
is what produces the first one.

  python -m meter.mcp.mint create "Bipin's laptop"
  python -m meter.mcp.mint list
  python -m meter.mcp.mint revoke <token-id>

The Meter password is asked for at the prompt and never taken as an argument —
an argument would be in shell history, in `ps`, and in any CI log that echoed
the command. It authenticates the person and is not stored; what the MCP server
gets is the token, which can be revoked without changing anyone's login.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from typing import Optional

from .. import auth
from . import tokens


def _sign_in(email: Optional[str]) -> str:
    """The tenant of whoever is running this, via the ordinary login path."""
    email = email or os.environ.get("METER_EMAIL") or input("Meter email: ").strip()
    password = getpass.getpass(f"Meter password for {email}: ")
    user = auth.login(email, password)
    if user is None:
        print("meter-mcp: those credentials were not accepted.", file=sys.stderr)
        raise SystemExit(2)
    return user["tenant_id"]


def _create(tenant_id: str, label: str) -> None:
    made = tokens.create(tenant_id, label)
    print(f"\nToken for {made['label']}:\n")
    print(f"  {made['token']}\n")
    print("This is the only time it is shown — Meter stores only its hash.")
    print("Give it to the MCP server as METER_MCP_TOKEN. See docs/mcp-server.md.")


def _list(tenant_id: str) -> None:
    rows = tokens.list_tokens(tenant_id)
    if not rows:
        print("No MCP tokens yet.")
        return
    print(f"{'id':38} {'label':24} {'last used':22} state")
    for row in rows:
        state = "active" if row["active"] else f"revoked {row['revoked_at'][:10]}"
        print(f"{row['id']:38} {row['label'][:24]:24} {row['last_used_at'] or '—':22} {state}")


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m meter.mcp.mint", description=__doc__)
    parser.add_argument("--email", help="Meter login. Also read from METER_EMAIL.")
    sub = parser.add_subparsers(dest="command", required=True)
    made = sub.add_parser("create", help="Mint a token.")
    made.add_argument("label", help="What it is for, e.g. \"Bipin's laptop\".")
    sub.add_parser("list", help="Show this organization's tokens.")
    gone = sub.add_parser("revoke", help="Turn one token off.")
    gone.add_argument("token_id", help="From `list`.")

    args = parser.parse_args(argv)
    tenant_id = _sign_in(args.email)
    try:
        if args.command == "create":
            _create(tenant_id, args.label)
        elif args.command == "list":
            _list(tenant_id)
        else:
            done = tokens.revoke(tenant_id, args.token_id)
            print(f"Revoked {done['label']} ({done['id']}).")
    except tokens.TokenError as exc:
        print(f"meter-mcp: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
