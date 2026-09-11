"""Build-cost allocation on a known fixture, plus CSV parsing."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from meter import build, discovery
from meter.build import DeveloperSpend
from meter.github import CopilotSeat, PullRequest

PERIOD = dt.date(2026, 5, 1)


def _pr(number, title, branch, author):
    return PullRequest(number, "acme/core", title, "", branch, author, "2026-05-01T00:00:00Z", "")


# alice: 2 PRs on Threat; bob: 1 on Report; carol: 1 Threat + 1 Report; dave: none
FIXTURE_PRS = [
    _pr(1, "Threat triage automation", "feature/threat-triage", "alice"),
    _pr(2, "Threat scoring model", "feature/threat-scoring", "alice"),
    _pr(3, "Report generator", "feature/report-gen", "bob"),
    _pr(4, "Report CSV export", "feature/report-export", "carol"),
    _pr(5, "Threat intel feed", "feature/threat-intel", "carol"),
]


class _FakeGitHub:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def list_repos(self, owner):
        return sorted({p.repo for p in FIXTURE_PRS})

    def fetch_merged_prs(self, owner, since):
        return FIXTURE_PRS


@pytest.fixture
def discovered(tenant_id, monkeypatch):
    monkeypatch.setattr(discovery, "_make_github_client", lambda token: _FakeGitHub())
    discovery.run_discovery(tenant_id, "acme", "tok")
    return tenant_id


def test_split_amount_is_exact():
    out = build._split_amount(Decimal("100.00"), {"a": 1, "b": 1, "c": 1})
    assert sum(out.values()) == Decimal("100.00")  # remainder absorbed, exact total


def test_parse_csv_flexible_headers_and_amounts():
    # Legacy format (no github_handle column) still parses; name/handle stay None.
    text = 'developer,tool,amount\nalice,cursor,100\nbob,claude_code,"$1,234.50"\n'
    spends = build.parse_csv(text)
    assert spends[0] == DeveloperSpend("alice", "cursor", Decimal("100"), None)
    assert spends[0].name is None and spends[0].handle is None
    assert spends[1].amount == Decimal("1234.50")


def test_parse_csv_new_name_and_handle_format():
    text = "developer,github_handle,tool,amount\nMuzaffar,Muzaffar-ni,claude_code,50.00\n"
    (spend,) = build.parse_csv(text)
    assert spend.name == "Muzaffar"
    assert spend.handle == "Muzaffar-ni"
    assert spend.developer_id == "Muzaffar-ni"  # handle is the attribution key
    assert spend.amount == Decimal("50.00")


def test_parse_csv_handle_only_and_name_only():
    # Name missing -> handle is both key and identity.
    (s1,) = build.parse_csv("developer,github_handle,tool,amount\n,octo,cursor,10\n")
    assert s1.name is None and s1.handle == "octo" and s1.developer_id == "octo"
    # Handle missing -> name is the key.
    (s2,) = build.parse_csv("developer,github_handle,tool,amount\nDana,,cursor,10\n")
    assert s2.handle is None and s2.name == "Dana" and s2.developer_id == "Dana"


def test_parse_csv_rejects_bad_tool():
    with pytest.raises(build.CsvImportError):
        build.parse_csv("developer,tool,amount\nalice,myspace,10\n")


def test_parse_csv_rejects_blanks_and_invalid_amounts():
    # Both name and handle blank in the new format.
    with pytest.raises(build.CsvImportError, match="name or github_handle"):
        build.parse_csv("developer,github_handle,tool,amount\n,,cursor,10\n")
    # Missing amount.
    with pytest.raises(build.CsvImportError, match="missing amount"):
        build.parse_csv("developer,github_handle,tool,amount\nA,a,cursor,\n")
    # Non-numeric amount.
    with pytest.raises(build.CsvImportError, match="invalid amount"):
        build.parse_csv("developer,github_handle,tool,amount\nA,a,cursor,lots\n")
    # Negative amount.
    with pytest.raises(build.CsvImportError, match="cannot be negative"):
        build.parse_csv("developer,github_handle,tool,amount\nA,a,cursor,-5\n")
    # Legacy format: missing developer.
    with pytest.raises(build.CsvImportError, match="missing developer"):
        build.parse_csv("developer,tool,amount\n,cursor,10\n")


def test_developer_label_variants():
    assert build.developer_label("Muzaffar", "Muzaffar-ni") == "Muzaffar (Muzaffar-ni)"
    assert build.developer_label("Muzaffar", None) == "Muzaffar"
    assert build.developer_label("", "octo") == "octo"
    assert build.developer_label(None, None, fallback="Unattributed") == "Unattributed"


def test_attribution_uses_github_handle_case_insensitively(discovered):
    # Display name differs from the handle, and the handle's case differs from the
    # recorded PR actor ('alice') — attribution should still land on Alice's feature.
    text = (
        "developer,github_handle,tool,amount\n"
        "Alice Smith,ALICE,cursor,100\n"
        "Muzaffar,Muzaffar-ni,claude_code,50\n"  # no PRs -> Unattributed
    )
    spends = build.parse_csv(text)
    summary = build.allocate_and_store(discovered, spends, PERIOD)

    features = {f["name"]: f for f in summary["features"]}
    assert features["Threat"]["amount"] == 100.0  # matched despite ALICE vs alice
    assert summary["unattributed"] == 50.0  # Muzaffar had no attributable PRs


def test_allocation_by_pr_overlap(discovered):
    spends = [
        DeveloperSpend("alice", "cursor", Decimal("100")),
        DeveloperSpend("bob", "cursor", Decimal("60")),
        DeveloperSpend("carol", "claude_code", Decimal("80")),
        DeveloperSpend("dave", "cursor", Decimal("30")),  # no PRs -> Unattributed
    ]
    summary = build.allocate_and_store(discovered, spends, PERIOD)

    features = {f["name"]: f for f in summary["features"]}
    # Threat: alice 100 (cursor) + carol 40 (claude_code split) = 140
    assert features["Threat"]["amount"] == 140.0
    assert features["Threat"]["by_tool"] == {"cursor": 100.0, "claude_code": 40.0}
    assert features["Threat"]["confidence"] == "high"  # alice's PRs are all in Threat
    # Report: bob 60 + carol 40 = 100
    assert features["Reports"]["amount"] == 100.0

    devs = {d["developer_id"]: d for d in summary["developers"]}
    assert devs["alice"]["amount"] == 100.0
    assert devs["carol"]["by_tool"] == {"claude_code": 80.0}

    assert summary["unattributed"] == 30.0  # dave
    assert summary["total"] == 270.0  # 100 + 60 + 80 + 30


def test_reimport_is_idempotent(discovered):
    spends = [DeveloperSpend("alice", "cursor", Decimal("100"))]
    build.allocate_and_store(discovered, spends, PERIOD)
    summary = build.allocate_and_store(discovered, spends, PERIOD)  # again
    assert summary["total"] == 100.0  # not doubled


def test_parse_csv_months_column():
    # An explicit months value is parsed.
    (s,) = build.parse_csv("developer,github_handle,tool,amount,months\nA,a,cursor,50,12\n")
    assert s.months == 12
    # Absent or blank -> defaults to a single month.
    (s1,) = build.parse_csv("developer,github_handle,tool,amount\nA,a,cursor,50\n")
    assert s1.months == 1
    (s2,) = build.parse_csv("developer,github_handle,tool,amount,months\nA,a,cursor,50,\n")
    assert s2.months == 1
    # Zero, negative, fractional, non-numeric, and over-cap are all rejected.
    for bad in ("0", "-3", "1.5", "lots", "999"):
        with pytest.raises(build.CsvImportError, match="months"):
            build.parse_csv(f"developer,github_handle,tool,amount,months\nA,a,cursor,50,{bad}\n")


def test_allocate_backfills_history_months(discovered):
    # months=3 backfills May, Apr, Mar 2026 with an identical record each.
    text = "developer,github_handle,tool,amount,months\nAlice,alice,cursor,100,3\n"
    summary = build.allocate_and_store(discovered, build.parse_csv(text), PERIOD)
    assert summary["months_imported"] == 3
    assert summary["total"] == 100.0  # anchor month holds the full $100

    for m in (dt.date(2026, 5, 1), dt.date(2026, 4, 1), dt.date(2026, 3, 1)):
        month = build.build_summary(discovered, m)
        assert month["total"] == 100.0
        # Attribution (Alice -> Threat) is applied identically to every month.
        assert {f["name"]: f["amount"] for f in month["features"]} == {"Threat": 100.0}
    # The month just before the span is untouched.
    assert build.build_summary(discovered, dt.date(2026, 2, 1))["total"] == 0.0

    # Re-importing the same span replaces rather than doubling any month.
    build.allocate_and_store(discovered, build.parse_csv(text), PERIOD)
    assert build.build_summary(discovered, dt.date(2026, 4, 1))["total"] == 100.0


class _FakeCopilotGitHub:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def copilot_plan_type(self, owner):
        return "enterprise"

    def fetch_copilot_seats(self, owner):
        return [CopilotSeat("alice"), CopilotSeat("bob")]


def test_import_copilot_seats(discovered, monkeypatch):
    # Seats pulled from GitHub become per-developer build cost, allocated to
    # features by PR authorship — no CSV upload.
    monkeypatch.setattr(build, "_make_github_client", lambda token: _FakeCopilotGitHub())
    summary = build.import_copilot_seats(discovered, "acme", "tok", PERIOD)

    assert summary["seats"] == 2
    assert summary["plan"] == "enterprise"
    assert summary["seat_price"] == 39.0  # enterprise seat

    features = {f["name"]: f for f in summary["features"]}
    # alice's PRs are all in Threat -> her $39 seat lands there (high confidence).
    assert features["Threat"]["amount"] == 39.0
    assert features["Threat"]["by_tool"] == {"copilot": 39.0}
    assert features["Threat"]["confidence"] == "high"
    # bob's PR is in Report.
    assert features["Reports"]["amount"] == 39.0

    devs = {d["developer_id"]: d for d in summary["developers"]}
    assert devs["alice"]["amount"] == 39.0
    assert devs["bob"]["amount"] == 39.0


# ---------------------------------------------------------------------------
# Re-attribution after discovery regenerates proposals.
# ---------------------------------------------------------------------------
def _build_total_by_feature(tenant_id):
    from meter.db import app_dsn, connect, tenant_tx

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        rows = conn.execute(
            "SELECT feature_id, SUM(amount) FROM build_cost GROUP BY feature_id"
        ).fetchall()
    return {(str(f) if f else None): float(a) for f, a in rows}


def test_rediscovery_no_longer_zeroes_build_cost(discovered, monkeypatch):
    """The reported bug: 'analyze last 90 days' reset every feature's build cost.

    Discovery deletes proposed features; build_cost.feature_id is ON DELETE SET
    NULL, so the spend silently fell into Unattributed. Re-running discovery must
    now re-attribute it against the regenerated proposals instead.
    """
    build.allocate_and_store(
        discovered,
        [DeveloperSpend("alice", "cursor", Decimal("100"))],  # alice: 2 PRs, one feature
        PERIOD,
    )
    before = _build_total_by_feature(discovered)
    assert None not in before  # fully attributed to a feature
    assert sum(before.values()) == 100.0

    discovery.run_discovery(discovered, "acme", "tok")  # re-runs, deleting proposals

    after = _build_total_by_feature(discovered)
    # The money did not vanish, and did not land in the Unattributed bucket.
    assert sum(after.values()) == 100.0
    assert after.get(None) is None
    # It is attributed to a feature again (a NEW id — proposals were regenerated).
    assert len(after) == 1


def test_reattribute_is_idempotent_and_total_preserving(discovered):
    build.allocate_and_store(
        discovered,
        [
            DeveloperSpend("carol", "claude_code", Decimal("80")),  # spans 2 features
            DeveloperSpend("dave", "cursor", Decimal("30")),  # no PRs -> Unattributed
        ],
        PERIOD,
    )
    first = build.reattribute(discovered)
    snapshot = _build_total_by_feature(discovered)
    second = build.reattribute(discovered)

    assert _build_total_by_feature(discovered) == snapshot  # running twice changes nothing
    assert first == second
    assert round(first["attributed"] + first["unattributed"], 2) == 110.0
    assert first["unattributed"] == 30.0  # dave still has no attributable PRs


def test_reattribute_leaves_directly_attributed_fine_tuning_alone(discovered):
    """Fine-tuning runs are attributed by the USER — never re-derived from PRs."""
    features = build.build_summary(discovered, PERIOD)  # noqa: F841 - ensures schema is live
    from meter.db import app_dsn, connect, tenant_tx

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id=discovered):
        feature_id = conn.execute(
            "SELECT id FROM feature WHERE status = 'proposed' LIMIT 1"
        ).fetchone()[0]
    build.record_training_cost(discovered, str(feature_id), 500, "Llama tuning", PERIOD)

    build.reattribute(discovered)

    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id=discovered):
        row = conn.execute(
            "SELECT feature_id, amount FROM build_cost WHERE source = 'fine_tune'"
        ).fetchone()
    assert str(row[0]) == str(feature_id)  # still on the feature the user chose
    assert float(row[1]) == 500.0


def test_reattribute_with_no_allocated_rows_is_a_no_op(tenant_id):
    assert build.reattribute(tenant_id) == {"rows": 0, "attributed": 0.0, "unattributed": 0.0}


# ---- Manually entered build cost ------------------------------------------
# Every other path here is authoritative about a tool and a month: it replaces
# that cell, because it can see the whole of it. A typed figure exists precisely
# because no connector can see it, so it must be additive in both directions.
def _manual(tenant_id, **over):
    kwargs = {
        "developer": "Dana",
        "handle": "dana",
        "tool": "cursor",
        "amount": Decimal("120.00"),
        "period": PERIOD,
        "months": 1,
    }
    kwargs.update(over)
    return build.add_manual_spend(tenant_id, **kwargs)


def test_a_manual_entry_adds_to_what_a_sync_already_wrote(tenant_id):
    build.allocate_and_store(
        tenant_id, [build.DeveloperSpend("alice", "cursor", Decimal("80.00"))], PERIOD
    )
    summary = _manual(tenant_id)
    # $80 synced + $120 typed. Replacing the cell would have shown $120.
    assert summary["total"] == pytest.approx(200.0)


def test_a_later_sync_does_not_delete_a_manual_entry(tenant_id):
    _manual(tenant_id)
    # A Cursor sync is authoritative about what Cursor can see — and it cannot
    # see an invoice somebody typed in.
    summary = build.allocate_and_store(
        tenant_id, [build.DeveloperSpend("alice", "cursor", Decimal("80.00"))], PERIOD
    )
    assert summary["total"] == pytest.approx(200.0)


def test_a_sync_still_replaces_its_own_rows(tenant_id):
    # The additive rule must not turn syncs into accumulators.
    for _ in range(3):
        summary = build.allocate_and_store(
            tenant_id, [build.DeveloperSpend("alice", "cursor", Decimal("80.00"))], PERIOD
        )
    assert summary["total"] == pytest.approx(80.0)


def test_manual_entries_accumulate_because_that_is_the_point(tenant_id):
    _manual(tenant_id, developer="Dana", amount=Decimal("120.00"))
    summary = _manual(tenant_id, developer="Sam", handle="sam", amount=Decimal("30.00"))
    assert summary["total"] == pytest.approx(150.0)


def test_a_manual_entry_can_be_listed_and_removed(tenant_id):
    # Without this a typo would be permanent, and invisible among synced rows.
    _manual(tenant_id, amount=Decimal("999.00"))
    entries = build.list_manual_spend(tenant_id, PERIOD)
    assert [e["developer"] for e in entries] == ["Dana"]
    assert entries[0]["amount"] == pytest.approx(999.0)

    assert build.delete_manual_spend(tenant_id, entries[0]["id"]) is True
    assert build.list_manual_spend(tenant_id, PERIOD) == []
    assert build.build_summary(tenant_id, PERIOD)["total"] == pytest.approx(0.0)


def test_a_manual_entry_can_backfill_several_months(tenant_id):
    _manual(tenant_id, amount=Decimal("50.00"), months=3)
    # $50 in each of three months, the same rule the CSV import follows.
    assert build.build_summary(tenant_id, PERIOD)["total"] == pytest.approx(50.0)
    earlier = dt.date(PERIOD.year, PERIOD.month - 1, 1) if PERIOD.month > 1 else PERIOD
    if earlier != PERIOD:
        assert build.build_summary(tenant_id, earlier)["total"] == pytest.approx(50.0)


@pytest.mark.parametrize(
    "bad",
    [
        {"amount": Decimal("0")},
        {"amount": Decimal("-5")},
        {"tool": "notatool"},
        {"developer": "   "},
    ],
)
def test_a_nonsense_manual_entry_is_refused(tenant_id, bad):
    with pytest.raises(ValueError):
        _manual(tenant_id, **bad)
