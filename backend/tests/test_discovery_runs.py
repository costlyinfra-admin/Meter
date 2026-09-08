"""Discovery run history, coverage, and the optional schedule.

The point of recording runs is to be able to say "we looked at March and found
nothing" rather than "March is empty". These tests are mostly about that
distinction, and about the schedule staying off until someone asks for it.
"""

from __future__ import annotations

import datetime as dt

import pytest
from meter import discovery
from meter.db import app_dsn, connect, tenant_tx
from meter.github import PullRequest


def _pr(number, repo, title, branch, merged="2026-05-01T00:00:00Z"):
    return PullRequest(number, repo, title, "", branch, "alice", merged, "")


PRS = [
    _pr(1, "acme/core", "Threat triage automation", "feature/threat-triage"),
    _pr(2, "acme/core", "Report generator", "feature/report-gen"),
]


class _FakeGitHub:
    """Records the `since` it was asked for, so tests can assert the window."""

    last_since: dt.date | None = None

    def __init__(self, prs=PRS, fail: Exception | None = None):
        self._prs = prs
        self._fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def list_repos(self, owner):
        return sorted({p.repo for p in self._prs})

    def fetch_merged_prs(self, owner, since, *, repos=None, with_stats=True):
        _FakeGitHub.last_since = since
        if self._fail:
            raise self._fail
        return self._prs


def _pr_signals(tenant_id):
    """Every PR signal in the tenant — the evidence the activity table reads."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            "SELECT external_ref, merged_at FROM feature_signal WHERE signal_type = 'pr'"
        ).fetchall()
    return [{"external_ref": r[0], "merged_at": r[1]} for r in rows]


@pytest.fixture
def gh(monkeypatch):
    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub())
    _FakeGitHub.last_since = None
    return _FakeGitHub


# ---------------------------------------------------------------------------
# Recording what ran
# ---------------------------------------------------------------------------
def test_a_run_is_recorded_with_the_window_it_asked_for(tenant_id, gh):
    discovery.run_discovery(tenant_id, "acme", "tok", days=30, started_by="cto@acme.com")

    run = discovery.last_run(tenant_id)
    assert run["status"] == "success"
    assert run["trigger"] == "manual"
    assert run["started_by"] == "cto@acme.com"
    assert run["prs"] == 2
    assert run["covered_from"] == (dt.date.today() - dt.timedelta(days=30)).isoformat()
    assert run["covered_to"] == dt.date.today().isoformat()


def test_an_explicit_since_wins_over_the_lookback(tenant_id, gh):
    # This is what "cover March and April" turns into: the caller names the
    # window and never has to convert it against a clock it cannot see.
    summary = discovery.run_discovery(tenant_id, "acme", "tok", days=7, since=dt.date(2026, 3, 1))
    assert gh.last_since == dt.date(2026, 3, 1)
    assert summary["covered_from"] == "2026-03-01"
    assert discovery.last_run(tenant_id)["covered_from"] == "2026-03-01"


def test_a_failed_run_is_recorded_too_and_the_error_reaches_the_caller(tenant_id, monkeypatch):
    monkeypatch.setattr(
        discovery,
        "_make_github_client",
        lambda token: _FakeGitHub(fail=RuntimeError("GitHub said no")),
    )
    with pytest.raises(RuntimeError):
        discovery.run_discovery(tenant_id, "acme", "tok", days=30)

    run = discovery.last_run(tenant_id)
    # A failed run is a fact about coverage: those months were attempted and are
    # NOT covered. Recording only successes would let the UI claim otherwise.
    assert run["status"] == "error"
    assert "GitHub said no" in run["error_message"]
    assert run["prs"] == 0
    assert discovery.coverage(tenant_id)["runs"] == 0  # successes only


def test_coverage_is_the_earliest_any_successful_run_reached(tenant_id, gh):
    discovery.run_discovery(tenant_id, "acme", "tok", since=dt.date(2026, 4, 1))
    discovery.run_discovery(tenant_id, "acme", "tok", since=dt.date(2026, 1, 1))
    discovery.run_discovery(tenant_id, "acme", "tok", since=dt.date(2026, 5, 1))

    cov = discovery.coverage(tenant_id)
    # Runs overlap and get re-run with different lookbacks, so the earliest one
    # is what has been covered -- not the most recent run's own start.
    assert cov["covered_from"] == "2026-01-01"
    assert cov["covered_to"] == dt.date.today().isoformat()
    assert cov["runs"] == 3
    assert cov["last_run_status"] == "success"


def test_a_tenant_with_no_runs_reports_no_coverage(tenant_id):
    cov = discovery.coverage(tenant_id)
    assert cov == {
        "runs": 0,
        "covered_from": None,
        "covered_to": None,
        "last_run_at": None,
        "last_run_status": None,
        "last_run_trigger": None,
    }
    assert discovery.last_run(tenant_id) is None


def test_run_history_is_newest_first(tenant_id, gh):
    discovery.run_discovery(tenant_id, "acme", "tok", since=dt.date(2026, 1, 1))
    discovery.run_discovery(tenant_id, "acme", "tok", since=dt.date(2026, 2, 1))
    history = discovery.run_history(tenant_id)
    assert [r["covered_from"] for r in history] == ["2026-02-01", "2026-01-01"]


# ---------------------------------------------------------------------------
# A run only speaks for the window it fetched
# ---------------------------------------------------------------------------
def _prs_merged(*specs):
    """PullRequests at given (number, repo, branch, merged-date) tuples."""
    return [
        _pr(n, repo, f"{branch} work", branch, merged=f"{day}T00:00:00Z")
        for n, repo, branch, day in specs
    ]


def test_a_narrow_rerun_keeps_evidence_from_months_it_never_looked_at(tenant_id, monkeypatch):
    # The reported bug: running discovery for the current month from the By
    # Developer screen blanked every earlier month. A run asks GitHub for pull
    # requests merged since a date; it can speak for those months and no others.
    march = _prs_merged((1, "acme/core", "feature/reporting", "2026-03-04"))
    may = _prs_merged((2, "acme/core", "feature/alerting", "2026-05-06"))

    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub(march + may))
    discovery.run_discovery(tenant_id, "acme", "tok", since=dt.date(2026, 3, 1))

    before = _pr_signals(tenant_id)
    assert {r["external_ref"] for r in before} == {"acme/core#1", "acme/core#2"}

    # Now re-run for May only — exactly what the "Run discovery back to May"
    # button does. March was never fetched, so it must survive untouched.
    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub(may))
    discovery.run_discovery(tenant_id, "acme", "tok", since=dt.date(2026, 5, 1))

    after = _pr_signals(tenant_id)
    assert {r["external_ref"] for r in after} == {"acme/core#1", "acme/core#2"}, (
        "a run for May deleted evidence from March"
    )
    # And March's feature is still there, not swept as "no longer proposed".
    assert any(r["merged_at"] == dt.date(2026, 3, 4) for r in after)


def test_a_narrow_rerun_does_not_duplicate_a_pull_request(tenant_id, monkeypatch):
    # There is no unique constraint on (feature_id, external_ref), so re-adding
    # what a run fetched has to be preceded by clearing it.
    may = _prs_merged((2, "acme/core", "feature/alerting", "2026-05-06"))
    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub(may))

    for _ in range(3):
        discovery.run_discovery(tenant_id, "acme", "tok", since=dt.date(2026, 5, 1))

    refs = [r["external_ref"] for r in _pr_signals(tenant_id)]
    assert refs.count("acme/core#2") == 1, f"duplicated on re-run: {refs}"


def test_a_wide_rerun_still_drops_a_feature_it_could_see_and_did_not_produce(
    tenant_id, monkeypatch
):
    # The pruning that made the old code destructive is still correct when the
    # run genuinely covers the evidence: a feature whose PRs are all inside the
    # window, and which the clustering no longer produces, is gone.
    old = _prs_merged((1, "acme/core", "feature/reporting", "2026-05-02"))
    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub(old))
    discovery.run_discovery(tenant_id, "acme", "tok", since=dt.date(2026, 5, 1))
    assert _pr_signals(tenant_id)

    # Same window, but that pull request has vanished from GitHub's answer.
    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub([]))
    discovery.run_discovery(tenant_id, "acme", "tok", since=dt.date(2026, 5, 1))
    assert _pr_signals(tenant_id) == []


def test_a_confirmed_feature_is_never_touched_by_a_rerun(tenant_id, monkeypatch):
    may = _prs_merged((2, "acme/core", "feature/alerting", "2026-05-06"))
    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub(may))
    discovery.run_discovery(tenant_id, "acme", "tok", since=dt.date(2026, 5, 1))

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute("UPDATE feature SET status = 'confirmed'")

    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub([]))
    discovery.run_discovery(tenant_id, "acme", "tok", since=dt.date(2026, 5, 1))

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute("SELECT count(*) FROM feature WHERE status = 'confirmed'").fetchone()
    assert rows[0] == 1


# ---------------------------------------------------------------------------
# The schedule
# ---------------------------------------------------------------------------
def test_a_schedule_cannot_be_set_before_discovery_has_a_scope(tenant_id):
    before = discovery.get_schedule(tenant_id)
    assert before["configurable"] is False
    assert before["enabled"] is False

    with pytest.raises(discovery.ScheduleError) as exc:
        discovery.set_schedule(tenant_id, enabled=True)
    assert "Run discovery once" in str(exc.value)


def test_automatic_discovery_is_off_until_it_is_asked_for(tenant_id, gh):
    discovery.run_discovery(tenant_id, "acme", "tok")
    schedule = discovery.get_schedule(tenant_id)
    # Running discovery must not sign anyone up for running it nightly: it costs
    # rate limit, it can cost LLM spend, and it raises proposals to review.
    assert schedule["configurable"] is True
    assert schedule["enabled"] is False
    assert schedule["next_run_at"] is None


def test_enabling_schedules_the_next_run_rather_than_firing_now(tenant_id, gh):
    discovery.run_discovery(tenant_id, "acme", "tok")
    now = dt.datetime(2026, 5, 21, 9, 0, tzinfo=dt.timezone.utc)
    schedule = discovery.set_schedule(tenant_id, enabled=True, lookback_days=21, now=now)

    assert schedule["enabled"] is True
    assert schedule["lookback_days"] == 21
    # Compared as instants: the database hands timestamps back in the session's
    # own offset, which says nothing about when the run is due.
    assert dt.datetime.fromisoformat(schedule["next_run_at"]) == now + discovery.SCHEDULE_INTERVAL
    # Turning a setting on spent nobody's rate limit: still one run on record.
    assert discovery.coverage(tenant_id)["runs"] == 1


def test_disabling_clears_the_next_run(tenant_id, gh):
    discovery.run_discovery(tenant_id, "acme", "tok")
    discovery.set_schedule(tenant_id, enabled=True)
    off = discovery.set_schedule(tenant_id, enabled=False)
    assert off["enabled"] is False
    assert off["next_run_at"] is None


def test_lookback_is_validated(tenant_id, gh):
    discovery.run_discovery(tenant_id, "acme", "tok")
    for bad in (0, 400, "14", True):
        with pytest.raises(discovery.ScheduleError):
            discovery.set_schedule(tenant_id, enabled=True, lookback_days=bad)


# ---------------------------------------------------------------------------
# The scheduled runner
# ---------------------------------------------------------------------------
def test_the_scheduler_skips_tenants_that_did_not_ask(tenant_id, gh, app_env):
    discovery.run_discovery(tenant_id, "acme", "tok")  # scope exists, auto off
    assert discovery.run_scheduled_discovery() == []


def test_the_scheduler_runs_a_due_tenant_and_marks_the_run_scheduled(tenant_id, gh, app_env):
    discovery.run_discovery(tenant_id, "acme", "tok")
    discovery.set_schedule(tenant_id, enabled=True, lookback_days=14)

    # Not yet due.
    assert discovery.run_scheduled_discovery(now=dt.datetime.now(dt.timezone.utc)) == []

    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)
    results = discovery.run_scheduled_discovery(now=later)
    assert [r["status"] for r in results] == ["success"]

    run = discovery.last_run(tenant_id)
    assert run["trigger"] == "scheduled"
    assert run["started_by"] == "schedule"
    # And it is rescheduled, so it does not run again on the next tick.
    assert discovery.get_schedule(tenant_id)["next_run_at"] is not None
    assert discovery.run_scheduled_discovery(now=later) == []


def test_a_scheduled_run_stretches_back_to_cover_a_gap(tenant_id, gh, app_env):
    # Nothing has run for a month -- longer than the configured lookback. The run
    # has to reach past the last one, or that month is never collected and the
    # schedule quietly leaves a hole.
    discovery.run_discovery(tenant_id, "acme", "tok", since=dt.date(2026, 1, 1))
    discovery.set_schedule(tenant_id, enabled=True, lookback_days=14)
    stale = dt.date.today() - dt.timedelta(days=30)
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute("UPDATE discovery_run SET covered_to = %s", (stale,))

    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)
    discovery.run_scheduled_discovery(now=later)

    assert gh.last_since == stale - dt.timedelta(days=discovery.SCHEDULE_OVERLAP_DAYS)
    # Coverage still reaches back to January: the earlier run is not forgotten.
    assert discovery.coverage(tenant_id)["covered_from"] == "2026-01-01"


def test_a_nightly_run_fetches_days_rather_than_the_whole_quarter(tenant_id, gh, app_env):
    # The point of the schedule: a run the night after the last one asks GitHub
    # for a few days, not for everything since the original 90-day sweep.
    discovery.run_discovery(tenant_id, "acme", "tok", since=dt.date(2026, 1, 1))
    discovery.set_schedule(tenant_id, enabled=True, lookback_days=3)

    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)
    discovery.run_scheduled_discovery(now=later)

    # Just past the last run, not back to January.
    assert gh.last_since == dt.date.today() - dt.timedelta(days=discovery.SCHEDULE_OVERLAP_DAYS)


def test_the_configured_lookback_is_the_floor_for_a_scheduled_run(tenant_id, gh, app_env):
    discovery.run_discovery(tenant_id, "acme", "tok")
    # A 60-day lookback reaches further back than "just past the last run", so a
    # run always covers at least that much.
    discovery.set_schedule(tenant_id, enabled=True, lookback_days=60)

    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)
    discovery.run_scheduled_discovery(now=later)
    # Measured from the scheduler's own clock, which is what it runs against.
    assert gh.last_since == later.date() - dt.timedelta(days=60)


def test_one_tenants_failure_does_not_stop_the_scheduler_or_wedge_it(
    tenant_id, monkeypatch, app_env
):
    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub())
    discovery.run_discovery(tenant_id, "acme", "tok")
    discovery.set_schedule(tenant_id, enabled=True)

    monkeypatch.setattr(
        discovery,
        "_make_github_client",
        lambda token: _FakeGitHub(fail=RuntimeError("token expired")),
    )
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2)
    results = discovery.run_scheduled_discovery(now=later)

    assert results[0]["status"] == "error"
    assert discovery.last_run(tenant_id)["status"] == "error"
    # Rescheduled anyway: a tenant with an expired token must not be retried in a
    # tight loop, and the failure is already on record.
    assert discovery.get_schedule(tenant_id)["next_run_at"] is not None
    assert discovery.run_scheduled_discovery(now=later) == []


def test_one_tenants_runs_are_invisible_to_another(tenant_id, gh, app_env):
    discovery.run_discovery(tenant_id, "acme", "tok")
    other = str(
        app_env.execute("INSERT INTO tenant (name) VALUES ('Other') RETURNING id").fetchone()[0]
    )
    app_env.commit()

    assert discovery.run_history(other) == []
    assert discovery.coverage(other)["runs"] == 0
    with connect(app_dsn()) as conn, tenant_tx(conn, other):
        assert conn.execute("SELECT COUNT(*) FROM discovery_run").fetchone()[0] == 0
