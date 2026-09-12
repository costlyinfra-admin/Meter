"""Products — the customer-defined grouping above features.

A company with one GitHub org can still sell several products. This module owns
the customer's product list, which repositories belong to each, and the rule that
derives a feature's product from the repositories its pull requests came from.

Three rules the rest of the code depends on:

  **A person's assignment wins.** ``product_source = 'user'`` is never overwritten,
  not by a discovery run and not by re-applying the repo mapping — the same rule
  ``category_source`` already follows.

  **A feature that spans products is Unassigned, not guessed.** If its pull
  requests come from repositories belonging to two different products, there is no
  answer the customer could check, so it stays Unassigned and is counted as
  "spanning" so the UI can ask a person to decide.

  **One repo belongs to one product.** That is what makes the derivation
  unambiguous. A repository genuinely shared by several products belongs to a
  product the customer creates for exactly that ("Shared", "Platform").
"""

from __future__ import annotations

import re
from typing import Optional

import psycopg

from .db import app_dsn, connect, tenant_tx


class ProductNotFound(Exception):
    """Raised when a product id does not exist for the tenant."""


class DuplicateProduct(ValueError):
    """Raised when a product name is already taken (case-insensitively)."""


MAX_REPOS = 500

# A repo full name: "owner/name". Anything else is not something discovery can
# produce, and mapping it would create a row nothing will ever match.
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def normalize_repo(repo: str) -> str:
    """Lowercased "owner/name". GitHub is case-insensitive about both halves."""
    return (repo or "").strip().lower()


def product_for(repos: list[str], repo_map: dict) -> Optional[str]:
    """The product a feature belongs to, given its repos and the repo mapping.

    One mapped product -> that product. None mapped -> Unassigned. More than one
    -> Unassigned, because a majority vote over pull-request counts produces a
    number the customer cannot check, and Unassigned is something they can act on.
    """
    mapped = {repo_map[normalize_repo(r)] for r in repos if normalize_repo(r) in repo_map}
    return next(iter(mapped)) if len(mapped) == 1 else None


def repo_map(conn: psycopg.Connection) -> dict:
    """repo -> product_id, inside an existing tenant transaction (for discovery)."""
    rows = conn.execute("SELECT repo, product_id FROM product_repo").fetchall()
    return {r[0]: str(r[1]) for r in rows}


def _feature_repos(conn: psycopg.Connection) -> dict:
    """feature_id -> the repos its evidence came from.

    Reads BOTH the 'repo' signals discovery writes and the repository half of its
    'pr' signals ("owner/name#123"). The pull-request half matters: it means the
    mapping works on features discovered before this existed, without waiting for
    a re-run.
    """
    rows = conn.execute(
        """
        SELECT feature_id,
               CASE WHEN signal_type = 'repo' THEN lower(external_ref)
                    ELSE lower(split_part(external_ref, '#', 1)) END
        FROM feature_signal
        WHERE signal_type IN ('repo', 'pr') AND external_ref LIKE %s
        """,
        ("%/%",),
    ).fetchall()
    out: dict = {}
    for feature_id, repo in rows:
        if repo:
            out.setdefault(str(feature_id), set()).add(repo)
    return out


def _product(conn: psycopg.Connection, product_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT id, name, description FROM product WHERE id = %s", (product_id,)
    ).fetchone()
    if row is None:
        return None
    repos = [
        r[0]
        for r in conn.execute(
            "SELECT repo FROM product_repo WHERE product_id = %s ORDER BY repo", (product_id,)
        ).fetchall()
    ]
    feature_count = conn.execute(
        "SELECT count(*) FROM feature WHERE product_id = %s "
        "AND status IN ('proposed', 'confirmed')",
        (product_id,),
    ).fetchone()[0]
    return {
        "id": str(row[0]),
        "name": row[1],
        "description": row[2] or "",
        "repos": repos,
        "feature_count": int(feature_count),
    }


def list_products(tenant_id: str) -> list[dict]:
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        ids = conn.execute("SELECT id FROM product ORDER BY lower(name)").fetchall()
        return [_product(conn, str(r[0])) for r in ids]


def create_product(tenant_id: str, name: str, description: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("a product needs a name")
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        try:
            product_id = conn.execute(
                "INSERT INTO product (tenant_id, name, description) VALUES (%s, %s, %s) "
                "RETURNING id",
                (tenant_id, name, description or None),
            ).fetchone()[0]
        except psycopg.errors.UniqueViolation as exc:
            raise DuplicateProduct(name) from exc
        return _product(conn, str(product_id))


def rename_product(
    tenant_id: str,
    product_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        existing = _product(conn, product_id)
        if existing is None:
            raise ProductNotFound(product_id)
        try:
            conn.execute(
                "UPDATE product SET name = %s, description = %s, updated_at = now() WHERE id = %s",
                (
                    (name or existing["name"]).strip(),
                    description if description is not None else (existing["description"] or None),
                    product_id,
                ),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise DuplicateProduct(name or "") from exc
        return _product(conn, product_id)


def delete_product(tenant_id: str, product_id: str) -> None:
    """Delete a product. Its features survive and become Unassigned.

    The foreign key clears product_id on its own, but it cannot clear
    product_source — a feature would be left claiming a person assigned it to a
    product that no longer exists. Both are cleared here, in one transaction.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if _product(conn, product_id) is None:
            raise ProductNotFound(product_id)
        conn.execute(
            "UPDATE feature SET product_id = NULL, product_source = NULL WHERE product_id = %s",
            (product_id,),
        )
        conn.execute("DELETE FROM product WHERE id = %s", (product_id,))


def set_repos(tenant_id: str, product_id: str, repos: list[str]) -> dict:
    """Replace this product's repositories.

    Claiming a repo that another product holds is an UPSERT, not an error: that is
    the customer moving a repository, and refusing it would leave them stuck.
    """
    cleaned: list[str] = []
    for repo in repos[:MAX_REPOS]:
        norm = normalize_repo(repo)
        if norm and _REPO_RE.match(norm) and norm not in cleaned:
            cleaned.append(norm)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        if _product(conn, product_id) is None:
            raise ProductNotFound(product_id)
        conn.execute("DELETE FROM product_repo WHERE product_id = %s", (product_id,))
        for repo in cleaned:
            conn.execute(
                """
                INSERT INTO product_repo (tenant_id, product_id, repo) VALUES (%s, %s, %s)
                ON CONFLICT (tenant_id, repo) DO UPDATE SET product_id = EXCLUDED.product_id
                """,
                (tenant_id, product_id, repo),
            )
        return _product(conn, product_id)


def reassign_from_repos(tenant_id: str) -> dict:
    """Re-derive every feature's product from the repo mapping.

    This is the function that does the real work for a new customer: the
    derivation inside a discovery run is a no-op until products exist, and when a
    customer first maps their repositories their features already exist. It is
    also what the "Re-apply" button calls after the mapping changes.

    Features a person assigned are left exactly as they are.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        mapping = repo_map(conn)
        by_feature = _feature_repos(conn)
        rows = conn.execute(
            "SELECT id FROM feature WHERE product_source IS DISTINCT FROM 'user'"
        ).fetchall()

        assigned = unassigned = spanning = 0
        for (fid,) in rows:
            fid = str(fid)
            repos = sorted(by_feature.get(fid, set()))
            product_id = product_for(repos, mapping)
            mapped = {mapping[r] for r in repos if r in mapping}
            if product_id is not None:
                assigned += 1
            else:
                unassigned += 1
                if len(mapped) > 1:
                    spanning += 1
            conn.execute(
                "UPDATE feature SET product_id = %s, product_source = %s WHERE id = %s",
                (product_id, "discovery" if product_id else None, fid),
            )
        return {"assigned": assigned, "unassigned": unassigned, "spanning": spanning}


def spanning_features(tenant_id: str) -> list[dict]:
    """Features whose repositories belong to more than one product.

    These are the only ones a person has to decide, so the UI can list them
    instead of making the customer hunt through everything that is Unassigned.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        mapping = repo_map(conn)
        by_feature = _feature_repos(conn)
        names = {
            str(r[0]): r[1]
            for r in conn.execute(
                "SELECT id, name FROM feature WHERE status IN ('proposed', 'confirmed')"
            ).fetchall()
        }
        products = {
            str(r[0]): r[1] for r in conn.execute("SELECT id, name FROM product").fetchall()
        }
    out = []
    for fid, name in names.items():
        repos = sorted(by_feature.get(fid, set()))
        mapped = {mapping[r] for r in repos if r in mapping}
        if len(mapped) > 1:
            out.append(
                {
                    "feature_id": fid,
                    "name": name,
                    "repos": repos,
                    "products": sorted(products.get(p, "?") for p in mapped),
                }
            )
    return out


def suggest_from_repos(tenant_id: str) -> dict:
    """Propose products from repository names, so mapping 20+ repos is quick.

    Repositories usually carry the product in their name — sentinel-api,
    sentinel-web, sentinel-ingest. Grouping on the leading word turns a long list
    into a handful of candidates the customer confirms or edits. Nothing is
    created here: this only ever proposes.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        known = set(repo_map(conn))
        seen: set = set()
        scope = conn.execute("SELECT repos FROM discovery_scope").fetchone()
        for repo in (scope[0] if scope and scope[0] else []) or []:
            norm = normalize_repo(repo)
            if _REPO_RE.match(norm):
                seen.add(norm)
        for repos in _feature_repos(conn).values():
            seen.update(r for r in repos if _REPO_RE.match(r))

    unmapped = sorted(seen - known)
    groups: dict = {}
    for repo in unmapped:
        # "owner/sentinel-api" -> "sentinel". A one-word repo is its own group.
        tail = repo.split("/", 1)[1]
        token = re.split(r"[-_.]", tail)[0] or tail
        groups.setdefault(token, []).append(repo)

    suggestions = [
        {"name": token.replace("-", " ").title(), "repos": repos}
        for token, repos in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    ]
    return {"suggestions": suggestions, "unmapped": unmapped, "mapped_count": len(known)}
