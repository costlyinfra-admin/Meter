"""Discovery: capability-based heuristic clustering, LLM parsing, scope, persistence."""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest
from meter import discovery, discovery_llm, features
from meter.github import PullRequest


def _pr(number, repo, title, branch, *, body="", labels=None, author="dev"):
    return PullRequest(
        number=number,
        repo=repo,
        title=title,
        body=body,
        branch=branch,
        author=author,
        merged_at="2026-05-01T00:00:00Z",
        url=f"https://github.com/{repo}/pull/{number}",
        labels=labels or [],
    )


MCS = "transilienceai/mcs"
MCS_PRS = [
    _pr(201, MCS, "Add notifications system", "feat/notifications", labels=["notifications"]),
    _pr(202, MCS, "Notifications: email digest", "feat/notifications-digest"),
    _pr(203, MCS, "Chat UI polish", "feat/chat-ui"),
    _pr(204, MCS, "Chat backend streaming", "feat/chat-backend"),
    _pr(205, MCS, "Fix running session refire and duplicate spawn", "fix/running-session-refire"),
    _pr(206, MCS, "Selected branch propagation fix", "fix/selected-branch-propagation"),
    _pr(207, MCS, "522", "fix/522"),
]


def test_heuristic_clusters_by_product_capability_not_branch_tokens():
    proposals = discovery.heuristic_cluster(MCS_PRS)
    by_name = {p.name: p for p in proposals}

    # Related PRs roll up into ONE capability, named from the title (not the branch).
    assert "Notifications" in by_name
    assert set(by_name["Notifications"].pr_refs) == {f"{MCS}#201", f"{MCS}#202"}
    assert by_name["Notifications"].confidence == "high"  # two agreeing titles

    assert "Chat" in by_name
    assert set(by_name["Chat"].pr_refs) == {f"{MCS}#203", f"{MCS}#204"}

    # "fix/running-session-refire" -> Sessions, NEVER "Running".
    assert "Sessions" in by_name
    assert f"{MCS}#205" in by_name["Sessions"].pr_refs
    assert "Running" not in by_name


def test_heuristic_never_emits_junk_tokens_and_routes_weak_to_review():
    names = {p.name for p in discovery.heuristic_cluster(MCS_PRS)}
    for junk in ("Running", "Selected", "Branch", "522", "In", "Per", "Fix", "Feature"):
        assert junk not in names
    # "Selected branch propagation" and "522" carry no capability -> Needs review.
    review = next(p for p in discovery.heuristic_cluster(MCS_PRS) if p.name == "Needs review")
    assert {f"{MCS}#206", f"{MCS}#207"} <= set(review.pr_refs)
    assert review.confidence == "low"


def test_confidence_reflects_signal_strength():
    proposals = {p.name: p for p in discovery.heuristic_cluster(MCS_PRS)}
    assert proposals["Notifications"].confidence == "high"  # 2 PRs, strong titles
    assert proposals["Sessions"].confidence == "med"  # 1 strong PR
    assert proposals["Needs review"].confidence == "low"


def test_stopwords_and_acronyms():
    # A single unknown branch fragment is too weak to name -> review, not "Widget".
    prs = [_pr(1, MCS, "Improve the thing", "chore/improve-widget")]
    assert discovery.heuristic_cluster(prs)[0].name == "Needs review"
    # Acronyms get proper casing.
    mfa = discovery.heuristic_cluster([_pr(2, MCS, "Add MFA to login", "feat/mfa")])
    assert mfa[0].name == "MFA"


def test_proposals_from_json_filters_unknown_refs():
    text = f"""Here you go:
    [
      {{"name": "Chat", "description": "d", "confidence": "high",
       "pr_refs": ["{MCS}#203", "{MCS}#999"], "branch_pattern": "feat/chat-*"}},
      {{"name": "Bad", "confidence": "nonsense", "pr_refs": ["{MCS}#205"]}}
    ]"""
    proposals = discovery._proposals_from_json(text, MCS_PRS)
    assert proposals[0].pr_refs == [f"{MCS}#203"]  # unknown ref dropped
    assert proposals[1].confidence == "low"  # invalid confidence normalized


def test_openai_compatible_cluster_sends_body_and_labels(monkeypatch):
    import httpx

    monkeypatch.setenv("METER_DISCOVERY_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("METER_DISCOVERY_API_KEY", "gsk_free")

    def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        # The payload now carries body + labels, not just title/branch.
        user_msg = json.loads(sent["messages"][1]["content"])
        assert "labels" in user_msg[0] and "body" in user_msg[0]
        content = json.dumps(
            [{"name": "Notifications", "confidence": "high", "pr_refs": [f"{MCS}#201"]}]
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    proposals = discovery.openai_compatible_cluster(
        MCS_PRS, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert proposals[0].name == "Notifications"


def test_llm_backend_selection(monkeypatch):
    monkeypatch.delenv("METER_DISCOVERY_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert discovery._llm_backend() is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    assert discovery._llm_backend() is discovery.claude_cluster
    monkeypatch.setenv("METER_DISCOVERY_BASE_URL", "http://localhost:11434/v1")
    assert discovery._llm_backend() is discovery.openai_compatible_cluster


class _FakeGitHub:
    """Fake with repos across two orgs/repos to exercise scope filtering."""

    def __init__(self, prs, repos=None):
        self._prs = prs
        self._repos = repos or sorted({p.repo for p in prs})

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def list_repos(self, owner):
        target = owner.lower()
        return [r for r in self._repos if r.split("/", 1)[0].lower() == target]

    def fetch_merged_prs(self, owner, since, *, repos=None, with_stats=True):
        if repos:
            allowed = set(repos)
            return [p for p in self._prs if p.repo in allowed]
        target = owner.lower()
        return [p for p in self._prs if p.repo.split("/", 1)[0].lower() == target]


def test_run_discovery_scopes_to_selected_repos_and_persists(tenant_id, monkeypatch):
    # Two repos in the org; only "mcs" is selected — "docs" PRs must be excluded.
    prs = MCS_PRS + [_pr(9, "transilienceai/docs", "Fix typos", "fix/typos")]
    repos = ["transilienceai/mcs", "transilienceai/docs"]
    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub(prs, repos))

    summary = discovery.run_discovery(
        tenant_id, "transilienceai", "tok", repos=["transilienceai/mcs"]
    )
    assert summary["repos"] == ["transilienceai/mcs"]  # only the selected repo analyzed
    assert summary["prs"] == len(MCS_PRS)  # the docs PR was not fetched

    proposed = features.list_features(tenant_id, status="proposed")
    all_refs = {
        s["external_ref"] for f in proposed for s in f["signals"] if s["signal_type"] == "pr"
    }
    assert all(ref.startswith("transilienceai/mcs#") for ref in all_refs)

    # PR detail (title/branch/url) is persisted on the signal for the review UI.
    notif = next(f for f in proposed if f["name"] == "Notifications")
    pr = next(s for s in notif["signals"] if s["signal_type"] == "pr")
    assert pr["title"] and pr["branch"] and pr["url"]

    # Scope is remembered for the next run.
    scope = discovery.get_scope(tenant_id)
    assert scope == {"owner": "transilienceai", "repos": ["transilienceai/mcs"]}


def test_rerun_replaces_proposals_but_keeps_confirmed(tenant_id, monkeypatch):
    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub(MCS_PRS))
    discovery.run_discovery(tenant_id, "transilienceai", "tok")
    features.confirm_features(tenant_id)
    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub([MCS_PRS[0]]))
    discovery.run_discovery(tenant_id, "transilienceai", "tok")

    confirmed = features.list_features(tenant_id, status="confirmed")
    proposed = features.list_features(tenant_id, status="proposed")
    assert len(confirmed) >= 2  # earlier confirmations survive
    assert len(proposed) == 1  # regenerated from the smaller re-run


def test_rerun_keeps_proposed_feature_ids_stable(tenant_id, monkeypatch):
    """Re-running discovery must REUSE proposed features, not recreate them.

    Ids used to churn on every run, so bookmarked /features/<id> links broke,
    feature-scoped alerts detached, and cost rows were orphaned (feature_id is
    ON DELETE SET NULL). Matching by branch pattern / name keeps them stable.
    """
    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub(MCS_PRS))
    discovery.run_discovery(tenant_id, "transilienceai", "tok")
    first = {f["name"]: f["id"] for f in features.list_features(tenant_id, status="proposed")}
    assert first

    discovery.run_discovery(tenant_id, "transilienceai", "tok")  # identical re-run
    second = {f["name"]: f["id"] for f in features.list_features(tenant_id, status="proposed")}

    assert second == first  # same features, SAME ids


def test_rerun_updates_in_place_without_duplicating_signals(tenant_id, monkeypatch):
    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub(MCS_PRS))
    discovery.run_discovery(tenant_id, "transilienceai", "tok")
    before = features.list_features(tenant_id, status="proposed")
    discovery.run_discovery(tenant_id, "transilienceai", "tok")
    after = features.list_features(tenant_id, status="proposed")

    # Same count, and each feature's signals are regenerated — never doubled up.
    assert len(after) == len(before)
    by_name = {f["name"]: f for f in after}
    for f in before:
        assert len(by_name[f["name"]]["signals"]) == len(f["signals"])


def test_rerun_drops_proposals_that_left_the_window(tenant_id, monkeypatch):
    # Both runs name the window explicitly, because the fixture's pull requests
    # carry a fixed merge date (2026-05-01) that a relative lookback drifts past.
    # Dropping a proposal now requires the run to have actually covered its
    # evidence — a run cannot retire a feature it never looked at — so a window
    # that excludes these dates would (correctly) keep everything.
    window = dt.date(2026, 1, 1)
    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub(MCS_PRS))
    discovery.run_discovery(tenant_id, "transilienceai", "tok", since=window)
    kept_id = {f["name"]: f["id"] for f in features.list_features(tenant_id, status="proposed")}

    # The same window, but the analysis now yields only the first PR's cluster.
    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub([MCS_PRS[0]]))
    discovery.run_discovery(tenant_id, "transilienceai", "tok", since=window)
    proposed = features.list_features(tenant_id, status="proposed")

    assert len(proposed) == 1  # the vanished proposals are gone
    # ...and the surviving one kept its original id rather than being recreated.
    survivor = proposed[0]
    assert survivor["id"] == kept_id[survivor["name"]]


def test_rerun_keeps_build_cost_attached_to_the_same_feature(tenant_id, monkeypatch):
    """The end-to-end payoff: attributed build cost survives a re-analysis intact."""
    from decimal import Decimal

    from meter import build
    from meter.build import DeveloperSpend
    from meter.db import app_dsn, connect, tenant_tx

    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub(MCS_PRS))
    discovery.run_discovery(tenant_id, "transilienceai", "tok")

    author = MCS_PRS[0].author
    build.allocate_and_store(
        tenant_id, [DeveloperSpend(author, "cursor", Decimal("120"))], dt.date(2026, 5, 1)
    )

    def attribution():
        with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
            return {
                (str(f) if f else None): float(a)
                for f, a in conn.execute(
                    "SELECT feature_id, SUM(amount) FROM build_cost GROUP BY feature_id"
                ).fetchall()
            }

    before = attribution()
    assert None not in before  # every dollar attributed to a feature
    assert round(sum(before.values()), 2) == 120.0

    discovery.run_discovery(tenant_id, "transilienceai", "tok")  # the action that broke it

    after = attribution()
    assert after == before  # same feature ids, same dollars — nothing orphaned


@pytest.mark.parametrize(
    "title,branch,expected",
    [
        ("Add LLM-powered alert summaries", "feature/llm-summary", "ai"),
        ("Wire up Claude for triage", "feature/triage", "ai"),
        ("Embeddings index for semantic search", "feature/search", "ai"),
        ("Cache the system prompt between calls", "feature/prompt-cache", "ai"),
        # Ordinary engineering — no AI vocabulary anywhere.
        ("Add SAML single sign-on", "feature/sso", "non_ai"),
        ("Fix pagination on the invoice list", "feature/invoices", "non_ai"),
        # Words AI shares with ordinary software must NOT trigger a false positive:
        # a false "AI" label silently moves normal work onto the AI bill.
        ("Refactor the User model", "feature/user-model", "non_ai"),
        ("Rotate the auth token on refresh", "feature/token-rotate", "non_ai"),
        ("Parse the user agent string", "feature/user-agent", "non_ai"),
        ("Train new staff on the runbook", "chore/runbook", "non_ai"),
    ],
)
def test_ai_kind_reads_pr_evidence(title, branch, expected):
    assert discovery._ai_kind([_pr(1, "acme/core", title, branch)]) == expected


def test_ai_kind_reads_labels_and_body_too():
    pr = _pr(
        2,
        "acme/core",
        "Summaries for the incident feed",
        "feature/incident-feed",
        body="Uses the Anthropic API to condense the timeline.",
        labels=["backend"],
    )
    assert discovery._ai_kind([pr]) == "ai"


@pytest.mark.parametrize(
    "title,branch,expected",
    [
        ("Streaming chat assistant for the console", "feature/chat", "chat"),
        ("Add SAML single sign-on", "feature/sso", "auth"),
        ("Scheduled invoice exports for finance", "feature/invoice-export", "reporting"),
        ("Slack connector for alerts", "feature/slack-integration", "integration"),
        ("Nightly ingestion pipeline for logs", "feature/ingest", "data"),
        ("Terraform module for the staging cluster", "chore/terraform", "infra"),
        ("Rewrite the onboarding guide", "docs/onboarding", "docs"),
        ("Public REST endpoints for incidents", "feature/api-incidents", "api"),
        ("Responsive layout for the alerts screen", "feature/alerts-ui", "ui"),
    ],
)
def test_category_reads_pr_evidence(title, branch, expected):
    assert discovery._category([_pr(1, "acme/core", title, branch)]) == expected


def test_category_prefers_the_more_specific_surface():
    # A PR can honestly match several buckets. "SSO login screen" is Auth work
    # that happens to have a UI; "chat API" is Chat that happens to be an API.
    assert (
        discovery._category([_pr(1, "acme/core", "SSO login screen", "feature/sso-ui")]) == "auth"
    )
    assert (
        discovery._category([_pr(2, "acme/core", "Chat API endpoint", "feature/chat-api")])
        == "chat"
    )


def test_category_is_none_when_the_evidence_does_not_say():
    # No guess is better than a wrong one: the user is the one who corrects it,
    # and an untagged row invites a tag where a wrong tag hides the question.
    assert discovery._category([_pr(1, "acme/core", "Bump dependencies", "chore/bump")]) is None
    assert discovery._category([]) is None


# --- BYOK: a tenant's own LLM for discovery --------------------------------


def _saved(tenant_id, **kw):
    defaults = {
        "provider": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "api_key": "gsk_tenant_secret_value",
    }
    return discovery_llm.save(tenant_id, **{**defaults, **kw})


def test_byok_never_returns_the_key(tenant_id):
    """The one thing this module must not do.

    Not the key, not a prefix, not a suffix, not its length — `has_key` is the
    whole truth the API tells about the secret.
    """
    status = _saved(tenant_id)
    assert status["configured"] is True
    assert status["has_key"] is True
    blob = json.dumps(status)
    assert "gsk_tenant_secret_value" not in blob
    assert "gsk" not in blob
    # A read path returns the same shape, with no key anywhere in it.
    assert "gsk" not in json.dumps(discovery_llm.status(tenant_id))


def test_byok_stores_the_key_encrypted(tenant_id, app_env):
    _saved(tenant_id)
    row = app_env.execute("SELECT ciphertext FROM discovery_llm").fetchone()
    assert b"gsk_tenant_secret_value" not in bytes(row[0])  # ciphertext, not plaintext
    # ...and it round-trips for the outbound request that actually needs it.
    assert discovery_llm.active_config(tenant_id).api_key == "gsk_tenant_secret_value"


def test_discovery_uses_the_tenants_config_when_set(tenant_id):
    _saved(tenant_id, provider="openai", base_url="https://byok.test/v1", model="my-model")
    config = discovery_llm.active_config(tenant_id)

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["model"] = json.loads(request.content)["model"]
        content = json.dumps(
            [{"name": "Notifications", "confidence": "high", "pr_refs": [f"{MCS}#201"]}]
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    proposals = discovery.openai_compatible_cluster(
        MCS_PRS, client=httpx.Client(transport=httpx.MockTransport(handler)), config=config
    )
    assert proposals[0].name == "Notifications"
    assert seen["url"] == "https://byok.test/v1/chat/completions"
    assert seen["auth"] == "Bearer gsk_tenant_secret_value"
    assert seen["model"] == "my-model"


def test_no_byok_leaves_the_existing_behaviour_alone(tenant_id, monkeypatch):
    """Fallback must be exact: Meter's own env configuration, untouched."""
    assert discovery_llm.active_config(tenant_id) is None

    monkeypatch.setenv("METER_DISCOVERY_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("METER_DISCOVERY_API_KEY", "meter_server_key")
    monkeypatch.delenv("METER_DISCOVERY_MODEL", raising=False)

    config = discovery.env_llm_config()
    assert config.base_url == "https://api.groq.com/openai/v1"
    assert config.api_key == "meter_server_key"
    assert config.model == "llama-3.3-70b-versatile"  # the shipped default
    assert discovery._llm_backend() is discovery.openai_compatible_cluster


def test_disabling_byok_falls_back_without_discarding_it(tenant_id):
    _saved(tenant_id)
    assert discovery_llm.active_config(tenant_id) is not None

    off = discovery_llm.set_enabled(tenant_id, False)
    assert off["configured"] is True and off["enabled"] is False
    assert discovery_llm.active_config(tenant_id) is None  # back to Meter's endpoint

    on = discovery_llm.set_enabled(tenant_id, True)
    assert on["enabled"] is True
    assert discovery_llm.active_config(tenant_id).api_key == "gsk_tenant_secret_value"


def test_removing_byok_deletes_the_key(tenant_id, app_env):
    _saved(tenant_id)
    gone = discovery_llm.remove(tenant_id)
    assert gone == {"configured": False, "enabled": False, "has_key": False}
    assert app_env.execute("SELECT count(*) FROM discovery_llm").fetchone()[0] == 0
    assert discovery_llm.active_config(tenant_id) is None


def test_editing_keeps_the_stored_key(tenant_id):
    """The UI never shows the key, so editing a model must not require re-entry."""
    _saved(tenant_id)
    discovery_llm.save(
        tenant_id,
        provider="groq",
        base_url="https://api.groq.com/openai/v1",
        model="llama-3.1-8b-instant",
    )  # no api_key
    config = discovery_llm.active_config(tenant_id)
    assert config.model == "llama-3.1-8b-instant"
    assert config.api_key == "gsk_tenant_secret_value"


def test_a_first_save_requires_a_key(tenant_id):
    with pytest.raises(discovery_llm.ByokError):
        discovery_llm.save(tenant_id, provider="groq", model="llama-3.3-70b-versatile")


@pytest.mark.parametrize(
    "kw",
    [
        {"provider": "not-a-provider"},
        {"provider": "custom", "base_url": ""},  # custom needs its own URL
        {"base_url": "ftp://nope"},  # not http(s)
        {"model": "   "},  # blank model
    ],
)
def test_byok_rejects_invalid_configuration(tenant_id, kw):
    with pytest.raises(discovery_llm.ByokError):
        _saved(tenant_id, **kw)


def test_invalid_credentials_report_an_error_without_echoing_the_key(tenant_id):
    """A 401 must be surfaced usefully — and must not quote the key back."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Some providers echo the request; make sure that can't leak.
        return httpx.Response(401, text='{"error":"invalid api key: gsk_tenant_secret_value"}')

    result = discovery_llm.test_connection(
        tenant_id,
        provider="groq",
        base_url="https://api.groq.com/openai/v1",
        model="llama-3.3-70b-versatile",
        api_key="gsk_tenant_secret_value",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert result["ok"] is False
    assert "401" in result["error"]
    assert "gsk_tenant_secret_value" not in result["error"]
    assert "***" in result["error"]


def test_test_connection_succeeds_against_a_working_endpoint(tenant_id):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "llama-3.3-70b-versatile"
        assert body["max_tokens"] == 1  # a probe, not a real completion
        return httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})

    _saved(tenant_id)
    result = discovery_llm.test_connection(
        tenant_id, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert result == {"ok": True, "model": "llama-3.3-70b-versatile"}


def test_test_connection_without_any_config(tenant_id):
    assert discovery_llm.test_connection(tenant_id)["ok"] is False


def test_a_broken_byok_key_degrades_to_the_heuristic(tenant_id):
    """A tenant's own key failing must not break their discovery run."""
    config = discovery.LlmConfig(base_url="https://byok.test/v1", api_key="bad", model="m")

    def boom(prs, *, config=None):
        raise RuntimeError("401 Unauthorized")

    original = discovery.openai_compatible_cluster
    discovery.openai_compatible_cluster = boom
    try:
        proposals = discovery.cluster_prs(MCS_PRS, config=config)
    finally:
        discovery.openai_compatible_cluster = original
    assert proposals, "discovery produced nothing instead of falling back"


def test_byok_is_tenant_isolated(tenant_id, app_env):
    other = str(
        app_env.execute("INSERT INTO tenant (name) VALUES ('Other Co') RETURNING id").fetchone()[0]
    )
    app_env.commit()

    _saved(tenant_id, model="mine")
    _saved(other, model="theirs", api_key="gsk_other_tenant_key")

    assert discovery_llm.active_config(tenant_id).model == "mine"
    assert discovery_llm.active_config(tenant_id).api_key == "gsk_tenant_secret_value"
    assert discovery_llm.active_config(other).model == "theirs"
    assert discovery_llm.active_config(other).api_key == "gsk_other_tenant_key"

    # Removing one tenant's configuration leaves the other's intact.
    discovery_llm.remove(tenant_id)
    assert discovery_llm.active_config(tenant_id) is None
    assert discovery_llm.active_config(other) is not None
