"""Read-only GitHub connector.

Fetches merged pull requests (with repo, branch, author) for an owner over a
time window. READ-ONLY: only GET requests are ever issued. The token is the
customer's own personal access token, supplied per tenant (stored encrypted).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

import httpx

from .retrying import http_get_with_retry

GITHUB_API = "https://api.github.com"
_PER_PAGE = 100
_MAX_PAGES_PER_REPO = 10  # safety cap; 90 days of PRs per repo fits comfortably


class GitHubError(Exception):
    """A GitHub API call failed. `status` is the HTTP status when available."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@dataclass
class PullRequest:
    number: int
    repo: str  # "owner/name"
    title: str
    body: str
    branch: str  # head ref
    author: str
    merged_at: str  # ISO-8601
    url: str
    labels: list[str] = field(default_factory=list)  # PR labels (strong capability signal)
    # All four filled from the PR detail endpoint in one request (best-effort).
    commits: Optional[int] = None
    changed_files: Optional[int] = None
    additions: Optional[int] = None
    deletions: Optional[int] = None

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}"


@dataclass
class CopilotSeat:
    login: str  # the developer assigned this Copilot seat
    last_activity_at: Optional[str] = None  # ISO-8601 or None (informational)


class GitHubClient:
    """Minimal read-only GitHub REST client.

    Pass an ``httpx.Client`` to inject transport (the tests use a MockTransport);
    otherwise one is created and owned by this client.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        client: Optional[httpx.Client] = None,
        base_url: str = GITHUB_API,
    ):
        # token is optional: without it, only PUBLIC orgs/repos are visible
        # (GitHub's unauthenticated API — lower rate limit).
        self._token = token or None
        self._base = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _get(self, path: str, params: Optional[dict] = None) -> httpx.Response:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:  # unauthenticated requests work for public data
            headers["Authorization"] = f"Bearer {self._token}"
        resp = http_get_with_retry(
            self._client, f"{self._base}{path}", params=params, headers=headers
        )
        if resp.status_code == 401:
            raise GitHubError("GitHub authentication failed — check the access token.", 401)
        if resp.status_code == 403 and "rate limit" in resp.text.lower():
            raise GitHubError(
                "GitHub rate limit reached. Add a personal access token (Connect GitHub) "
                "for higher limits and access to private repositories.",
                403,
            )
        if resp.status_code >= 400:
            raise GitHubError(
                f"GitHub API error {resp.status_code}: {resp.text[:200]}", resp.status_code
            )
        return resp

    def list_repos(self, owner: str) -> list[str]:
        """Full names ("owner/name") of ``owner``'s repos the token can see.

        The UNION of two listings, because neither is complete on its own:

        * ``/user/repos`` sees private repos the token reaches personally —
          including ones under a personal account or an org whose repo listing
          the token cannot read.
        * ``/orgs/{owner}/repos`` sees every repo in the org, including ones the
          token reaches through a team rather than directly.

        This used to return the first listing as soon as it produced anything,
        which meant a token that could see twenty repos personally hid the other
        eighty the org listing would have returned. A partial answer must never
        win over a complete one, so both are asked and the results merged.
        """
        target = owner.lower()
        found: dict[str, None] = {}  # dict, not set: first-seen order is stable

        for full in self._list_accessible_repos():
            if full.split("/", 1)[0].lower() == target:
                found[full] = None

        # An owner is an org or a user, never both. Try the org listing first and
        # stop at whichever answers; a 404 means "wrong kind of owner", not "no
        # repos". `type=all` includes private repos for a member.
        failure: Optional[GitHubError] = None
        for path in (f"/orgs/{owner}/repos", f"/users/{owner}/repos"):
            try:
                for full in self._paginate_full_names(path, {"type": "all"}):
                    found[full] = None
                break
            except GitHubError as exc:
                if exc.status == 404:
                    continue
                # Anything else (rate limit, SAML enforcement, a scope the token
                # lacks) is only fatal when it leaves us with nothing at all —
                # otherwise the personal listing above still gives a real answer.
                failure = exc
                break

        if found:
            return list(found)
        if failure is not None:
            raise failure
        raise GitHubError(
            f"GitHub owner '{owner}' not found, or no repositories are accessible "
            "with this token (private repos need a token with repo access).",
            404,
        )

    def _list_accessible_repos(self) -> list[str]:
        """Every repo the token can access, including private ones.

        `affiliation` is spelled out because GitHub's default drops nothing but
        this call used to: it asked for `owner,organization_member` only, which
        silently excluded every repo the user reaches as a direct collaborator —
        the normal shape of an enterprise account.
        """
        if not self._token:
            return []  # /user/repos needs auth; unauthenticated -> public listing only
        try:
            return self._paginate_full_names(
                "/user/repos", {"affiliation": "owner,collaborator,organization_member"}
            )
        except GitHubError:
            # Some token types can't call /user/repos; the org listing still can.
            return []

    def _paginate_full_names(self, path: str, extra_params: Optional[dict] = None) -> list[str]:
        names: list[str] = []
        page = 1
        while True:
            params = {"per_page": _PER_PAGE, "page": page}
            if extra_params:
                params.update(extra_params)
            resp = self._get(path, params)
            batch = resp.json()
            names.extend(repo["full_name"] for repo in batch)
            if len(batch) < _PER_PAGE:
                return names
            page += 1

    def fetch_merged_prs(
        self,
        owner: str,
        since: dt.date,
        *,
        repos: Optional[list[str]] = None,
        with_stats: bool = True,
    ) -> list[PullRequest]:
        """PRs merged on/after ``since`` across the owner's repos.

        Pass ``repos`` (full "owner/name" names) to fetch ONLY those repositories —
        the caller-selected scope — instead of every repo in the org. Names are
        validated to belong to ``owner`` so a caller can't reach outside it.

        When ``with_stats`` is set, each PR's commit and changed-file counts are
        fetched from its detail endpoint (one extra GET per PR; best-effort, so a
        failure leaves those counts as None rather than breaking discovery).
        """
        # Unauthenticated requests have a tight rate limit (60/hr) — skip the
        # per-PR stat calls to conserve it.
        with_stats = with_stats and bool(self._token)
        if repos:
            target = owner.lower()
            selected = [r for r in repos if r.split("/", 1)[0].lower() == target]
        else:
            selected = self.list_repos(owner)
        prs: list[PullRequest] = []
        for repo in selected:
            prs.extend(self._fetch_repo_merged_prs(repo, since, with_stats=with_stats))
        return prs

    def _fetch_repo_merged_prs(
        self, repo: str, since: dt.date, *, with_stats: bool = True
    ) -> list[PullRequest]:
        out: list[PullRequest] = []
        for page in range(1, _MAX_PAGES_PER_REPO + 1):
            resp = self._get(
                f"/repos/{repo}/pulls",
                {
                    "state": "closed",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": _PER_PAGE,
                    "page": page,
                },
            )
            batch = resp.json()
            if not batch:
                break
            page_all_stale = True
            for pr in batch:
                updated = _parse_date(pr.get("updated_at"))
                if updated and updated >= since:
                    page_all_stale = False
                merged_at = pr.get("merged_at")
                if not merged_at:
                    continue
                if _parse_date(merged_at) < since:
                    continue
                pull = _to_pull_request(repo, pr)
                if with_stats:
                    (
                        pull.commits,
                        pull.changed_files,
                        pull.additions,
                        pull.deletions,
                    ) = self._fetch_pr_stats(repo, pull.number)
                out.append(pull)
            # Sorted by updated desc: once an entire page predates the window, stop.
            if page_all_stale:
                break
            if len(batch) < _PER_PAGE:
                break
        return out

    def _fetch_pr_stats(self, repo: str, number: int) -> tuple:
        """Size of a PR from its detail endpoint: commits, files, +lines, -lines.

        One request covers all four — the endpoint returns them together — so
        line counts cost nothing beyond the call already being made.
        """
        try:
            data = self._get(f"/repos/{repo}/pulls/{number}").json()
            if not isinstance(data, dict):
                return None, None, None, None
            return (
                data.get("commits"),
                data.get("changed_files"),
                data.get("additions"),
                data.get("deletions"),
            )
        except Exception:
            # Stats are non-critical; never let them break the connector.
            return None, None, None, None

    # ---- GitHub Copilot seat billing (build cost) ----------------------
    def copilot_plan_type(self, owner: str) -> str:
        """The org's Copilot plan ("business" | "enterprise"), drives seat price.

        Requires a token with Copilot billing admin access (org owner or
        manage_billing:copilot).
        """
        data = self._get(f"/orgs/{owner}/copilot/billing").json()
        plan = (data or {}).get("plan_type")
        return plan if plan in ("business", "enterprise") else "business"

    def fetch_copilot_seats(self, owner: str) -> list[CopilotSeat]:
        """Every assigned Copilot seat in the org (one per developer)."""
        seats: list[CopilotSeat] = []
        page = 1
        while True:
            resp = self._get(
                f"/orgs/{owner}/copilot/billing/seats", {"per_page": _PER_PAGE, "page": page}
            )
            batch = (resp.json() or {}).get("seats", [])
            for seat in batch:
                login = (seat.get("assignee") or {}).get("login")
                if login:
                    seats.append(CopilotSeat(login, seat.get("last_activity_at")))
            if len(batch) < _PER_PAGE:
                return seats
            page += 1


def _parse_date(value: Optional[str]) -> Optional[dt.date]:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def _to_pull_request(repo: str, pr: dict) -> PullRequest:
    return PullRequest(
        number=pr["number"],
        repo=repo,
        title=pr.get("title") or "",
        body=pr.get("body") or "",
        branch=(pr.get("head") or {}).get("ref") or "",
        author=(pr.get("user") or {}).get("login") or "",
        merged_at=pr["merged_at"],
        url=pr.get("html_url") or "",
        labels=[label["name"] for label in (pr.get("labels") or []) if label.get("name")],
    )
