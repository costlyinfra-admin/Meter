"""Notification delivery — independent per-channel dispatch with safe failures.

Channels: in-app (always available — the alert_event itself is the in-app item),
email, Slack webhook, generic webhook. External channels are honest about being
unconfigured (they never claim a false success), validate URLs against SSRF, retry
transient failures with bounded backoff, and record only safe metadata (never a
secret). A failure in one channel never blocks the others.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import time
from typing import Optional
from urllib.parse import urlparse

import httpx

from . import alerts
from .db import app_dsn, connect, tenant_tx

# Email delivery uses Resend (https://resend.com). Both the API key and a verified
# sender address are required; until they're set we surface the email channel as
# "unconfigured" rather than pretending a message went out.
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
ALERT_EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM")
EMAIL_ENABLED = bool(RESEND_API_KEY and ALERT_EMAIL_FROM)
_RESEND_ENDPOINT = "https://api.resend.com/emails"

_MAX_ATTEMPTS = 3


def _sleep(seconds: float) -> None:  # seam so tests don't actually wait
    time.sleep(seconds)


def _http_post(url: str, payload: dict, timeout: float = 8.0, headers: Optional[dict] = None):
    # Seam for tests. `headers` carries auth for provider APIs (e.g. Resend); it is
    # never logged or persisted, so a bearer token can't leak into alert_notification.
    return httpx.post(url, json=payload, timeout=timeout, headers=headers, follow_redirects=False)


def is_safe_url(url: str) -> tuple[bool, str]:
    """SSRF guard: only http(s), a resolvable host, and no private/loopback/reserved
    address. Redirects are refused at request time (follow_redirects=False)."""
    if not url:
        return False, "No URL."
    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "Invalid URL."
    if parsed.scheme not in ("http", "https"):
        return False, "URL must use http or https."
    host = parsed.hostname
    if not host:
        return False, "URL has no host."
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False, "Host does not resolve."
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False, "URL resolves to a private or disallowed address."
    return True, ""


def _post_with_retry(
    url: str, payload: dict, headers: Optional[dict] = None
) -> tuple[str, Optional[str], int]:
    """POST with bounded exponential backoff on TRANSIENT failures only.

    Returns (status, error, attempts). 4xx and redirects are permanent -> no retry.
    """
    last_error = "Delivery failed."
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = _http_post(url, payload, headers=headers)
        except Exception as exc:  # noqa: BLE001 — network/transport error is transient
            last_error = f"Delivery error: {type(exc).__name__}."
        else:
            if 300 <= resp.status_code < 400:
                return "failed", "Redirects are not allowed.", attempt  # permanent
            if 400 <= resp.status_code < 500:
                return "failed", f"Provider rejected the request ({resp.status_code}).", attempt
            if resp.status_code >= 500:
                last_error = f"Provider error ({resp.status_code})."  # transient -> retry
            else:
                return "sent", None, attempt
        if attempt < _MAX_ATTEMPTS:
            _sleep(0.2 * (2 ** (attempt - 1)))  # 0.2s, 0.4s
    return "failed", last_error, _MAX_ATTEMPTS


def _deliver(dest: dict, payload: dict) -> tuple[str, Optional[str], int]:
    """Deliver to one channel. Returns (status, error, attempts). Never raises."""
    channel = dest["channel"]
    if channel == "in_app":
        return "sent", None, 1  # the alert_event is the in-app notification
    if channel == "email":
        if not EMAIL_ENABLED:
            return "unconfigured", "Email delivery is not configured.", 1
        to = dest.get("target")
        if not to:
            return "unconfigured", "Email has no recipient configured.", 1
        headers = {"Authorization": f"Bearer {RESEND_API_KEY}"}
        return _post_with_retry(_RESEND_ENDPOINT, _email_body(to, payload), headers)
    if channel in ("slack", "webhook"):
        url = dest.get("target")
        if not url:
            return "unconfigured", f"{channel.title()} has no URL configured.", 1
        ok, why = is_safe_url(url)
        if not ok:
            return "failed", why, 1
        body = _slack_body(payload) if channel == "slack" else payload
        return _post_with_retry(url, body)
    return "failed", "Unknown channel.", 1


def _slack_body(payload: dict) -> dict:
    """A minimal Slack incoming-webhook body (text only)."""
    return {"text": payload.get("text", "Meter alert")}


def _email_body(to: str, payload: dict) -> dict:
    """A Resend send-email request body built from the alert payload."""
    org = payload.get("org", "your organization")
    kind = {"resolved": "Resolved", "test": "Test"}.get(payload.get("event_type"), "Triggered")
    metric = payload.get("metric", "AI cost")
    subject = f"[{org}] Alert {kind}: {metric}"
    text = payload.get("text", "Meter alert")
    return {
        "from": ALERT_EMAIL_FROM,
        "to": [to],
        "subject": subject,
        "text": text,
    }


def dispatch(tenant_id: str, alert_id: str, event_id: str, payload: dict) -> list[dict]:
    """Send `payload` to every channel of a rule, recording each attempt.

    Independent per channel: one failure never blocks the rest. If any channel
    fails or is unconfigured, a `delivery_error` alert_event is recorded so the
    problem is visible without changing the underlying alert state.
    """
    destinations = alerts.get_destination_secrets(tenant_id, alert_id)
    results: list[dict] = []
    any_problem = False
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        for dest in destinations:
            status, error, attempts = _deliver(dest, payload)
            if status != "sent":
                any_problem = True
            conn.execute(
                """
                INSERT INTO alert_notification
                    (tenant_id, alert_id, event_id, channel, status, attempts, error)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (tenant_id, alert_id, event_id, dest["channel"], status, attempts, error),
            )
            results.append({"channel": dest["channel"], "status": status, "error": error})

        if any_problem:
            # Surface delivery trouble as its own activity item (idempotent per event).
            conn.execute(
                """
                INSERT INTO alert_event
                    (tenant_id, alert_id, event_type, event_key, message, occurred_at)
                VALUES (%s, %s, 'delivery_error', %s, %s, now())
                ON CONFLICT (tenant_id, event_key) DO NOTHING
                """,
                (
                    tenant_id,
                    alert_id,
                    f"delivery:{event_id}",
                    "One or more notification channels could not deliver.",
                ),
            )
    return results
