"""Feature editing: add, rename, delete, split, merge, confirm — all persist."""

from __future__ import annotations

import datetime as dt

import pytest
from meter import discovery, features
from meter.github import PullRequest


def _pr(number, repo, title, branch):
    return PullRequest(number, repo, title, "", branch, "dev", "2026-05-01T00:00:00Z", "")


class _FakeGitHub:
    def __init__(self, prs):
        self._prs = prs

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def list_repos(self, owner):
        return sorted({p.repo for p in self._prs})

    def fetch_merged_prs(self, owner, since):
        return self._prs


@pytest.fixture
def discovered(tenant_id, monkeypatch):
    """Run heuristic discovery so there are proposed features to edit."""
    prs = [
        _pr(1, "acme/core", "Threat triage automation", "feature/threat-triage"),
        _pr(2, "acme/core", "Threat scoring model", "feature/threat-scoring"),
        _pr(3, "acme/core", "Report generator", "feature/report-gen"),
    ]
    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub(prs))
    discovery.run_discovery(tenant_id, "acme", "tok")
    return tenant_id


def test_add_manual_feature(discovered):
    feature = features.add_feature(discovered, "Manual feature", "typed by hand")
    assert feature["status"] == "proposed"
    assert feature["name"] == "Manual feature"
    assert any(f["id"] == feature["id"] for f in features.list_features(discovered))


def test_rename_feature(discovered):
    target = features.list_features(discovered, status="proposed")[0]
    renamed = features.rename_feature(discovered, target["id"], name="Renamed")
    assert renamed["name"] == "Renamed"


def test_delete_feature(discovered):
    target = features.list_features(discovered, status="proposed")[0]
    features.delete_feature(discovered, target["id"])
    assert all(f["id"] != target["id"] for f in features.list_features(discovered))


def test_delete_missing_raises(discovered):
    with pytest.raises(features.FeatureNotFound):
        features.delete_feature(discovered, "00000000-0000-0000-0000-000000000000")


def test_split_feature(discovered):
    threat = next(f for f in features.list_features(discovered) if f["name"] == "Threat")
    pr_signals = [s for s in threat["signals"] if s["signal_type"] == "pr"]
    assert len(pr_signals) == 2

    peeled, *_ = pr_signals
    new_features = features.split_feature(
        discovered,
        threat["id"],
        groups=[
            {"name": "Threat triage", "signal_ids": [peeled["id"]]},
            {"name": "Threat scoring", "signal_ids": [s["id"] for s in pr_signals[1:]]},
        ],
    )
    assert {f["name"] for f in new_features} == {"Threat triage", "Threat scoring"}
    # original is gone, replaced by the two splits
    names = {f["name"] for f in features.list_features(discovered)}
    assert "Threat" not in names
    assert {"Threat triage", "Threat scoring"} <= names
    # the peeled PR signal followed its new feature
    triage = next(f for f in new_features if f["name"] == "Threat triage")
    assert [s["external_ref"] for s in triage["signals"]] == [peeled["external_ref"]]


def test_merge_features(discovered):
    all_feats = features.list_features(discovered, status="proposed")
    threat = next(f for f in all_feats if f["name"] == "Threat")
    report = next(f for f in all_feats if f["name"] == "Reports")
    total_signals = len(threat["signals"]) + len(report["signals"])

    merged = features.merge_features(discovered, [threat["id"], report["id"]], name="Combined")
    assert merged["name"] == "Combined"
    assert len(merged["signals"]) == total_signals
    assert all(f["id"] != report["id"] for f in features.list_features(discovered))


def test_confirm_features(discovered):
    confirmed = features.confirm_features(discovered)
    assert confirmed and all(f["status"] == "confirmed" for f in confirmed)
    assert features.list_features(discovered, status="proposed") == []


def test_set_category_is_a_user_tag_that_survives_rediscovery(tenant_id, app_env):
    # Discovery guessed "ui" from a PR about a screen; it's really the auth flow.
    # The correction has to outlive the next discovery run, or the user re-fixes
    # it forever.
    feature = features.add_feature(tenant_id, "Login screen")
    app_env.execute(
        "UPDATE feature SET category = 'ui', category_source = 'discovery' WHERE id = %s",
        (feature["id"],),
    )
    app_env.commit()

    updated = features.set_category(tenant_id, feature["id"], "auth")
    assert updated["category"] == "auth"
    assert updated["category_source"] == "user"

    # What discovery would write on a re-run: the CASE guard must leave it alone.
    app_env.execute(
        """
        UPDATE feature
        SET category = CASE WHEN category_source = 'user' THEN category ELSE 'ui' END,
            category_source = CASE WHEN category_source = 'user'
                                   THEN 'user' ELSE 'discovery' END
        WHERE id = %s
        """,
        (feature["id"],),
    )
    app_env.commit()
    assert features.list_features(tenant_id)[0]["category"] == "auth"


def test_clearing_a_category_hands_the_feature_back_to_the_guess(tenant_id):
    feature = features.add_feature(tenant_id, "Report export")
    features.set_category(tenant_id, feature["id"], "reporting")

    cleared = features.set_category(tenant_id, feature["id"], None)
    assert cleared["category"] is None and cleared["category_source"] is None


def test_set_category_rejects_a_value_outside_the_vocabulary(tenant_id):
    feature = features.add_feature(tenant_id, "Anything")
    with pytest.raises(ValueError):
        features.set_category(tenant_id, feature["id"], "miscellaneous")


def test_a_new_feature_starts_untagged(tenant_id):
    # Nothing guesses a category for a hand-added feature — it is untagged until
    # somebody says otherwise, which is what the Overview column reports.
    features.add_feature(tenant_id, "Triage")
    assert features.list_features(tenant_id)[0]["category"] is None
    assert features.list_features(tenant_id)[0]["category_source"] is None


# --- per-feature usage: the only way Active users / Cost per user can fill ---
# Until Slice 2 brings product-analytics connectors, these two paths are it.
def _usage(tenant_id, feature_id, period):
    from meter.db import app_dsn, connect, tenant_tx

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return conn.execute(
            "SELECT active_users, events, source FROM feature_usage "
            "WHERE feature_id = %s AND period = %s",
            (feature_id, period),
        ).fetchone()


def test_a_csv_fills_a_month_of_usage_for_every_named_feature(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")
    reports = features.add_feature(tenant_id, "Report generator")

    result = features.import_usage(
        tenant_id,
        "feature,active_users,events\nAI threat triage,540,12000\nReport generator,120,3000\n",
        period=dt.date(2026, 5, 1),
    )

    assert result["imported"] == 2
    assert result["features"] == 2
    assert result["periods"] == ["2026-05-01"]
    assert _usage(tenant_id, triage["id"], dt.date(2026, 5, 1))[:2] == (540, 12000)
    assert _usage(tenant_id, reports["id"], dt.date(2026, 5, 1))[:2] == (120, 3000)


def test_a_feature_can_be_named_or_addressed_by_id(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")

    features.import_usage(
        tenant_id, f"feature,active_users\n{triage['id']},99\n", period=dt.date(2026, 5, 1)
    )
    assert _usage(tenant_id, triage["id"], dt.date(2026, 5, 1))[0] == 99

    # ...and by name, case-insensitively, because that is what the person
    # exporting from an analytics tool has in front of them.
    features.import_usage(
        tenant_id, "feature,active_users\nai THREAT triage,101\n", period=dt.date(2026, 5, 1)
    )
    assert _usage(tenant_id, triage["id"], dt.date(2026, 5, 1))[0] == 101


def test_a_row_can_carry_its_own_month(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")

    features.import_usage(
        tenant_id,
        "feature,active_users,period\nAI threat triage,10,2026-03\nAI threat triage,20,2026-04\n",
        period=dt.date(2026, 5, 1),
    )

    assert _usage(tenant_id, triage["id"], dt.date(2026, 3, 1))[0] == 10
    assert _usage(tenant_id, triage["id"], dt.date(2026, 4, 1))[0] == 20
    assert _usage(tenant_id, triage["id"], dt.date(2026, 5, 1)) is None


def test_an_unknown_feature_stops_the_whole_import(tenant_id):
    """Half a month loaded is worse than none: the blank column the person was
    trying to fill would look filled, and the rest would be silently stale."""
    triage = features.add_feature(tenant_id, "AI threat triage")

    with pytest.raises(features.UsageImportError) as exc:
        features.import_usage(
            tenant_id,
            "feature,active_users\nAI threat triage,540\nNo such feature,7\n",
            period=dt.date(2026, 5, 1),
        )
    assert "Row 3" in str(exc.value) and "No such feature" in str(exc.value)
    assert _usage(tenant_id, triage["id"], dt.date(2026, 5, 1)) is None


def test_an_ambiguous_name_asks_for_the_id_instead_of_guessing(tenant_id):
    features.add_feature(tenant_id, "Duplicate name")
    features.add_feature(tenant_id, "Duplicate name")

    with pytest.raises(features.UsageImportError) as exc:
        features.import_usage(
            tenant_id, "feature,active_users\nDuplicate name,5\n", period=dt.date(2026, 5, 1)
        )
    assert "more than one feature" in str(exc.value)


@pytest.mark.parametrize(
    "body,message",
    [
        ("feature,active_users\nA,-3\n", "cannot be negative"),
        ("feature,active_users\nA,many\n", "invalid active_users"),
        ("feature,active_users\n,5\n", "missing feature"),
        ("feature,events\nA,5\n", "missing active_users"),
        ("feature,active_users,period\nA,5,May\n", "invalid period"),
        ("feature,active_users\n", "no rows"),
    ],
)
def test_a_bad_row_says_which_row_and_why(tenant_id, body, message):
    features.add_feature(tenant_id, "A")
    with pytest.raises(features.UsageImportError) as exc:
        features.import_usage(tenant_id, body, period=dt.date(2026, 5, 1))
    assert message in str(exc.value)


def test_a_reimport_replaces_the_month_rather_than_adding_to_it(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")
    for count in (100, 250):
        features.import_usage(
            tenant_id,
            f"feature,active_users\nAI threat triage,{count}\n",
            period=dt.date(2026, 5, 1),
        )

    # Active users is a point-in-time count, not a running total.
    assert _usage(tenant_id, triage["id"], dt.date(2026, 5, 1))[0] == 250


def test_the_import_is_marked_as_coming_from_a_csv(tenant_id):
    triage = features.add_feature(tenant_id, "AI threat triage")
    features.import_usage(
        tenant_id, "feature,active_users\nAI threat triage,7\n", period=dt.date(2026, 5, 1)
    )
    features.set_usage(tenant_id, triage["id"], 8, period=dt.date(2026, 6, 1))

    assert _usage(tenant_id, triage["id"], dt.date(2026, 5, 1))[2] == "csv"
    assert _usage(tenant_id, triage["id"], dt.date(2026, 6, 1))[2] == "manual"
