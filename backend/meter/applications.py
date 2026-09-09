"""AI applications: the product surface a workflow belongs to.

An application is what a customer would name in a sentence — "the support
agent", "document review". It sits above features so the drill-down reads
application -> feature -> trace -> span.

The SDK names one by SLUG, not by id, because an id would have to be looked up
and configured before the first event could be sent. A slug is something a
developer already knows ("support-agent") and can put straight in an env var,
so instrumenting a service is one line rather than a setup step.

That makes first-event auto-creation the difference between a five-minute
install and a twenty-minute one, which is why it is allowed here — but it is the
one place an unauthenticated-ish path (a valid ingest token) creates rows, so it
is bounded: the slug is normalized and validated, creation is idempotent within
the resolved tenant, and a tenant cannot accumulate unlimited applications by
sending unlimited distinct slugs.
"""

from __future__ import annotations

import re

#: A slug is lowercase, digits and single dashes, and short enough to live in a
#: URL and a column index. Anchored, so it validates the WHOLE string.
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
MAX_SLUG = 64
MAX_NAME = 120

#: How many applications one tenant may auto-create from ingest. Generous for
#: any real product surface and low enough that a misconfigured client looping
#: over random slugs fills a bounded amount of the table rather than the disk.
MAX_APPLICATIONS_PER_TENANT = 100


class ApplicationError(ValueError):
    """Invalid application input (maps to HTTP 400)."""


def normalize_slug(raw: str) -> str:
    """A slug from whatever the caller sent, or raise.

    Normalizing rather than rejecting outright: "Support Agent" and
    "support_agent" plainly mean the same application as "support-agent", and
    failing an install over a capital letter helps nobody. What is NOT accepted
    is anything that survives normalization as empty or over-long — those are
    bugs in the caller, not stylistic differences.
    """
    if not isinstance(raw, str):
        raise ApplicationError("application must be a string slug, e.g. 'support-agent'.")
    slug = raw.strip().lower()
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    if not slug:
        raise ApplicationError(
            "application slug must contain a letter or digit, e.g. 'support-agent'."
        )
    if len(slug) > MAX_SLUG:
        raise ApplicationError(f"application slug must be {MAX_SLUG} characters or fewer.")
    if not SLUG_RE.match(slug):
        raise ApplicationError("application slug must be lowercase letters, digits and dashes.")
    return slug


def resolve(conn, tenant_id: str, raw_slug: str) -> str:
    """The application id for a slug within this tenant, creating it if new.

    `conn` is already inside the tenant's RLS context, so this cannot resolve or
    create an application in another tenant however the slug is spelled — the
    tenant comes from the validated ingest token, never from the payload.
    """
    slug = normalize_slug(raw_slug)
    row = conn.execute("SELECT id FROM ai_application WHERE slug = %s", (slug,)).fetchone()
    if row:
        return str(row[0])

    count = conn.execute("SELECT count(*) FROM ai_application").fetchone()[0]
    if count >= MAX_APPLICATIONS_PER_TENANT:
        raise ApplicationError(
            f"This organization already has {MAX_APPLICATIONS_PER_TENANT} AI applications. "
            "Rename or remove one before adding another."
        )
    # The slug doubles as the initial display name: it is what the developer
    # chose, and a placeholder like "Untitled" would be worse. Renaming later
    # changes only `name`, never `slug`, so events keep resolving.
    row = conn.execute(
        "INSERT INTO ai_application (tenant_id, name, slug) VALUES (%s, %s, %s) "
        # Two workers racing on a first event is normal, not exceptional.
        "ON CONFLICT (tenant_id, slug) DO UPDATE SET slug = EXCLUDED.slug "
        "RETURNING id",
        (tenant_id, slug[:MAX_NAME], slug),
    ).fetchone()
    return str(row[0])


def validate_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name:
        raise ApplicationError("Application name cannot be empty.")
    if len(name) > MAX_NAME:
        raise ApplicationError(f"Application name must be {MAX_NAME} characters or fewer.")
    return name
