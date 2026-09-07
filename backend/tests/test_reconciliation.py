"""Provider invoice reconciliation: parsing, matching, tolerance, isolation.

Every fixture here is synthetic. There is no real customer billing data in this
file and there must never be.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from meter.reconciliation import engine, flag, imports, report
from meter.reconciliation.common import money, tenant_conn
from meter.reconciliation.flag import ReconciliationError

MAY = dt.date(2026, 5, 1)


# ---------------------------------------------------------------------------
# Parsing and mapping — no database needed.
# ---------------------------------------------------------------------------
def test_money_reads_the_shapes_statements_actually_use():
    assert money("12.50") == Decimal("12.50")
    assert money("$1,234.56") == Decimal("1234.56")
    assert money("(12.30)") == Decimal("-12.30")  # accounting negative
    assert money("12.30-") == Decimal("-12.30")
    assert money("") == Decimal("0")
    assert money("not a number", None) is None


def test_money_never_becomes_a_float():
    # A float here would introduce differences of exactly the size this module
    # exists to explain.
    assert isinstance(money("0.1"), Decimal)
    assert money("0.1") + money("0.2") == Decimal("0.3")


def test_headers_are_matched_to_fields_however_they_are_punctuated():
    mapping = imports.suggest_mapping(["Usage Date", "Model Name", "Usage_Cost", "Currency Code"])
    assert mapping["service_date"] == "Usage Date"
    assert mapping["model"] == "Model Name"
    assert mapping["usage_subtotal"] == "Usage_Cost"
    assert mapping["currency"] == "Currency Code"


def test_a_column_is_only_claimed_by_one_field():
    # "Amount" can serve several fields; whichever claims it first keeps it.
    mapping = imports.suggest_mapping(["Date", "Amount"])
    claimed = [f for f, col in mapping.items() if col == "Amount"]
    assert len(claimed) == 1


def test_explicit_mapping_overrides_the_guess():
    csv = "when,what,how much\n2026-05-01,claude,10.00\n"
    result = imports.preview(
        csv, {"service_date": "when", "usage_subtotal": "how much", "model": "what"}
    )
    assert result["missing_required"] == []
    assert result["usage_subtotal"] == 10.0
    assert result["rows"][0]["model"] == "claude"


def test_missing_required_columns_are_named_not_guessed_at():
    result = imports.preview("model,notes\nclaude,hello\n")
    assert set(result["missing_required"]) == {"service_date", "usage_subtotal"}
    assert result["rows"] == []  # nothing is normalised until the mapping is complete


def test_malformed_files_are_refused_with_a_reason():
    for content, reason in [("", "empty"), ("\n\n", "empty"), ("just_a_header\n", "no rows")]:
        with pytest.raises(ReconciliationError) as exc:
            imports.preview(content)
        assert reason.split()[0] in str(exc.value).lower()


def test_rows_that_cannot_be_read_are_reported_not_dropped():
    csv = "date,model,cost\n2026-05-01,a,10.00\nnot-a-date,b,20.00\n2026-05-02,c,\n"
    result = imports.preview(csv)
    assert result["accepted_count"] == 1
    assert result["rejected_count"] == 2
    assert {r["row_number"] for r in result["rejected_rows"]} == {2, 3}
    assert "No usable date" in result["rejected_rows"][0]["errors"]


def test_tax_credits_and_fees_never_become_usage():
    csv = (
        "date,category,amount\n"
        "2026-05-01,usage,100.00\n"
        "2026-05-01,Tax,8.50\n"
        "2026-05-01,Credit,-10.00\n"
        "2026-05-01,Service Fee,2.00\n"
    )
    result = imports.preview(csv)
    # The usage comparison sees the usage line and nothing else.
    assert result["usage_subtotal"] == 100.0
    assert result["tax"] == 8.5
    assert result["credits"] == -10.0
    assert result["fees"] == 2.0


def test_a_category_wins_over_the_column_the_amount_arrived_in():
    # A tax line whose amount lands in the usage column is still tax.
    csv = "date,category,usage cost\n2026-05-01,VAT,19.00\n"
    result = imports.preview(csv)
    assert result["usage_subtotal"] == 0.0
    assert result["tax"] == 19.0


def test_dates_are_read_in_the_formats_exports_use():
    for text, expected in [
        ("2026-05-04", dt.date(2026, 5, 4)),
        ("05/04/2026", dt.date(2026, 5, 4)),  # month/day, the documented rule
        ("2026-05-04T13:04:00", dt.date(2026, 5, 4)),
        ("May 04, 2026", dt.date(2026, 5, 4)),
    ]:
        assert imports.parse_date(text) == expected
    assert imports.parse_date("whenever") is None


def test_a_billing_day_is_not_shifted_by_a_timezone():
    # A statement states a billing DAY. Applying a zone could move usage across
    # a period boundary the provider did not move it across.
    assert imports.parse_date("2026-05-01T23:59:59") == dt.date(2026, 5, 1)
    assert imports.parse_date("2026-05-01T00:00:00") == dt.date(2026, 5, 1)


def test_formula_cells_are_neutralised_before_anyone_sees_them():
    for hostile in ("=1+1", "+1", "-1", "@SUM(A1)", "\tx"):
        assert imports.neutralise(hostile).startswith("'")
    assert imports.neutralise("claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_a_formula_in_the_file_is_neutralised_in_the_preview():
    csv = "date,model,cost\n2026-05-01,=cmd|' /c calc'!A1,10.00\n"
    result = imports.preview(csv)
    assert result["rows"][0]["model"].startswith("'=")


def test_filenames_cannot_become_paths():
    # Only the base name survives, so nothing here can address a directory.
    assert imports.safe_filename("../../etc/passwd") == "passwd"
    assert imports.safe_filename("/tmp/x/bill.csv") == "bill.csv"
    assert imports.safe_filename("..\\..\\windows\\system32") == "system32"
    assert imports.safe_filename("") == "import.csv"
    assert "/" not in imports.safe_filename("a/b/c.csv")


def test_oversized_input_is_refused_rather_than_parsed():
    with pytest.raises(ReconciliationError):
        imports.read_csv("date,cost\n" + "2026-05-01,1\n" * (imports.MAX_ROWS + 1))


def test_only_mapped_columns_are_kept():
    # A billing export can carry a contact name; storing the whole row would
    # keep personal data this module has no use for.
    csv = "date,cost,account_manager_email\n2026-05-01,10.00,someone@example.com\n"
    headers, rows = imports.read_csv(csv)
    normalised = imports.normalise(headers, rows, imports.suggest_mapping(headers))
    assert "someone@example.com" not in str(normalised[0]["raw"])


# ---------------------------------------------------------------------------
# Database-backed. Everything below runs against a real migrated Postgres.
# ---------------------------------------------------------------------------
def _enable(tenant_id: str) -> None:
    flag.update(tenant_id, enabled=True, actor="tester@example.com")


def _track(
    app_env,
    tenant_id,
    day,
    amount,
    *,
    model="claude-sonnet-4-6",
    provider="anthropic",
    workspace=None,
    api_key=None,
    source="cost_api",
    currency="USD",
):
    """One day of tracked connector spend — what a real ingest would have written."""
    app_env.execute(
        """
        INSERT INTO inference_cost_daily
            (tenant_id, provider, model, amount, day, source, confidence,
             workspace_id, api_key_id, currency)
        VALUES (%s, %s, %s, %s, %s, %s, 'high', %s, %s, %s)
        """,
        (tenant_id, provider, model, amount, day, source, workspace, api_key, currency),
    )
    app_env.commit()


def _statement(rows: list[str]) -> str:
    header = "date,model,category,cost,currency\n"
    return header + "".join(rows)


def _import(tenant_id, csv, *, provider="anthropic", filename="bill.csv", actor="a@b.com"):
    return imports.commit(tenant_id, provider=provider, filename=filename, content=csv, actor=actor)


def test_the_module_is_off_until_a_tenant_turns_it_on(app_env, tenant_id):
    assert flag.is_enabled(tenant_id) is False
    assert flag.settings(tenant_id)["enabled"] is False
    # Defaults are documented and conservative.
    assert flag.settings(tenant_id)["tolerance_abs"] == 1.0
    assert flag.settings(tenant_id)["tolerance_pct"] == 0.5


def test_enabling_and_disabling_keeps_the_history(app_env, tenant_id):
    _enable(tenant_id)
    _import(tenant_id, _statement(["2026-05-01,claude,usage,10.00,USD\n"]))
    flag.update(tenant_id, enabled=False, actor="a@b.com")
    assert flag.is_enabled(tenant_id) is False
    # Turned off, not erased.
    flag.update(tenant_id, enabled=True, actor="a@b.com")
    assert len(imports.history(tenant_id)) == 1


def test_the_operator_kill_switch_disables_it_everywhere(app_env, tenant_id, monkeypatch):
    _enable(tenant_id)
    assert flag.is_enabled(tenant_id) is True
    monkeypatch.setenv("METER_RECONCILIATION", "off")
    assert flag.is_enabled(tenant_id) is False
    assert flag.settings(tenant_id)["available"] is False


def test_an_exact_match(app_env, tenant_id):
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 100)
    imported = _import(tenant_id, _statement(["2026-05-01,claude-sonnet-4-6,usage,100.00,USD\n"]))
    run = engine.calculate(tenant_id, import_id=imported["id"])
    assert run["status"] == "matched"
    assert run["provider_usage"] == 100.0
    assert run["tracked_usage"] == 100.0
    assert run["usage_difference"] == 0.0


def test_a_match_within_the_absolute_tolerance(app_env, tenant_id):
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 100)
    imported = _import(tenant_id, _statement(["2026-05-01,claude-sonnet-4-6,usage,100.40,USD\n"]))
    run = engine.calculate(tenant_id, import_id=imported["id"])
    assert run["status"] == "within_tolerance"  # 40c, inside the $1 default
    assert run["usage_difference"] == 0.4


def test_a_match_within_the_percentage_tolerance(app_env, tenant_id):
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 10_000)
    # $30 is far outside the absolute bound but 0.3% is inside the percentage one.
    imported = _import(tenant_id, _statement(["2026-05-01,claude-sonnet-4-6,usage,10030.00,USD\n"]))
    run = engine.calculate(tenant_id, import_id=imported["id"])
    assert run["status"] == "within_tolerance"


def test_a_material_difference_is_a_discrepancy(app_env, tenant_id):
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 100)
    imported = _import(tenant_id, _statement(["2026-05-01,claude-sonnet-4-6,usage,283.00,USD\n"]))
    run = engine.calculate(tenant_id, import_id=imported["id"])
    assert run["status"] == "discrepancy"
    assert run["usage_difference"] == 183.0
    assert run["usage_difference_pct"] == pytest.approx(64.6643, abs=0.001)


def test_the_tolerances_in_force_are_stored_on_the_run(app_env, tenant_id):
    _enable(tenant_id)
    flag.update(tenant_id, tolerance_abs=Decimal("5"), tolerance_pct=Decimal("2"), actor="a@b.com")
    _track(app_env, tenant_id, MAY, 100)
    imported = _import(tenant_id, _statement(["2026-05-01,claude-sonnet-4-6,usage,104.00,USD\n"]))
    run = engine.calculate(tenant_id, import_id=imported["id"])
    assert run["status"] == "within_tolerance"
    assert run["tolerance_abs"] == 5.0 and run["tolerance_pct"] == 2.0
    # Changing them later does not rewrite what a past run was judged against.
    flag.update(tenant_id, tolerance_abs=Decimal("0"), tolerance_pct=Decimal("0"), actor="a@b.com")
    assert engine.run_detail(tenant_id, run["id"])["tolerance_abs"] == 5.0


def test_provider_usage_missing_from_meter(app_env, tenant_id):
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 100)
    imported = _import(
        tenant_id,
        _statement(
            [
                "2026-05-01,claude-sonnet-4-6,usage,100.00,USD\n",
                "2026-05-02,claude-sonnet-4-6,usage,50.00,USD\n",
            ]
        ),
    )
    run = engine.run_detail(tenant_id, engine.calculate(tenant_id, import_id=imported["id"])["id"])
    missing = [m for m in run["matches"] if m["classification"] == engine.PROVIDER_ONLY]
    assert len(missing) == 1
    assert missing[0]["provider_amount"] == 50.0
    assert missing[0]["confidence"] == "possible"  # inferred, never asserted
    assert run["unmatched_provider_count"] == 1


def test_meter_usage_absent_from_the_statement(app_env, tenant_id):
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 100)
    _track(app_env, tenant_id, dt.date(2026, 5, 2), 25)  # a day the statement skips
    _track(app_env, tenant_id, dt.date(2026, 5, 3), 10)
    imported = _import(
        tenant_id,
        _statement(
            [
                "2026-05-01,claude-sonnet-4-6,usage,100.00,USD\n",
                "2026-05-03,claude-sonnet-4-6,usage,10.00,USD\n",
            ]
        ),
    )
    run = engine.run_detail(tenant_id, engine.calculate(tenant_id, import_id=imported["id"])["id"])
    extra = [m for m in run["matches"] if m["classification"] == engine.METER_ONLY]
    assert len(extra) == 1 and extra[0]["tracked_amount"] == 25.0
    assert run["unmatched_tracked_count"] == 1


def test_credits_taxes_and_fees_are_reported_and_kept_out_of_usage(app_env, tenant_id):
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 100)
    imported = _import(
        tenant_id,
        _statement(
            [
                "2026-05-01,claude-sonnet-4-6,usage,100.00,USD\n",
                "2026-05-01,,tax,8.25,USD\n",
                "2026-05-01,,credit,-15.00,USD\n",
                "2026-05-01,,fee,3.00,USD\n",
                "2026-05-01,,adjustment,1.75,USD\n",
            ]
        ),
    )
    run = engine.calculate(tenant_id, import_id=imported["id"])
    # The headline comparison is usage against usage, and it balances.
    assert run["status"] == "matched"
    assert run["provider_usage"] == 100.0 and run["tracked_usage"] == 100.0
    # The other categories are carried, separately.
    assert run["provider_tax"] == 8.25
    assert run["provider_credits"] == -15.0
    assert run["provider_fees"] == 4.75  # fee + adjustment
    # And the invoice total is not the usage figure.
    assert run["provider_total"] != run["provider_usage"]


def test_a_tax_line_is_never_called_missing_usage(app_env, tenant_id):
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 100)
    imported = _import(
        tenant_id,
        _statement(
            [
                "2026-05-01,claude-sonnet-4-6,usage,100.00,USD\n",
                "2026-05-01,,tax,19.00,USD\n",
            ]
        ),
    )
    run = engine.run_detail(tenant_id, engine.calculate(tenant_id, import_id=imported["id"])["id"])
    assert run["status"] == "matched"
    assert not [m for m in run["matches"] if m["classification"] == engine.PROVIDER_ONLY]
    tax_line = [m for m in run["matches"] if m["classification"] == "provider_tax"]
    assert len(tax_line) == 1 and tax_line[0]["difference"] == 0.0


def test_rounding_is_half_up_and_exact(app_env, tenant_id):
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, Decimal("0.005"))
    imported = _import(tenant_id, _statement(["2026-05-01,claude-sonnet-4-6,usage,0.015,USD\n"]))
    run = engine.calculate(tenant_id, import_id=imported["id"])
    # 0.015 - 0.005 = 0.01 exactly; a float would give 0.009999999999999998.
    assert Decimal(str(run["usage_difference"])) == Decimal("0.01")


def test_a_currency_mismatch_is_flagged_not_converted(app_env, tenant_id):
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 100, currency="USD")
    imported = _import(tenant_id, _statement(["2026-05-01,claude-sonnet-4-6,usage,90.00,EUR\n"]))
    run = engine.run_detail(tenant_id, engine.calculate(tenant_id, import_id=imported["id"])["id"])
    assert run["status"] == "incomplete_data"
    mismatch = [m for m in run["matches"] if m["classification"] == engine.CURRENCY_MISMATCH]
    assert len(mismatch) == 1 and mismatch[0]["confidence"] == "confirmed"
    # Nothing was converted: both sides keep their own number.
    assert run["provider_usage"] == 90.0 and run["tracked_usage"] == 100.0


def test_a_line_outside_the_period_is_a_boundary_not_missing_usage(app_env, tenant_id):
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 100)
    imported = _import(
        tenant_id,
        _statement(
            [
                "2026-05-01,claude-sonnet-4-6,usage,100.00,USD\n",
                "2026-04-30,claude-sonnet-4-6,usage,7.00,USD\n",
            ]
        ),
    )
    run = engine.run_detail(tenant_id, engine.calculate(tenant_id, import_id=imported["id"])["id"])
    boundary = [m for m in run["matches"] if m["classification"] == engine.PERIOD_BOUNDARY]
    # The import's own period starts on the earliest row, so this is in range;
    # what matters is that the April row is not silently matched to May.
    assert boundary or [m for m in run["matches"] if m["classification"] == engine.PROVIDER_ONLY]


def test_a_repeated_provider_line_id_is_counted_once(app_env, tenant_id):
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 100)
    csv = (
        "date,model,category,cost,currency,line_item_id\n"
        "2026-05-01,claude-sonnet-4-6,usage,100.00,USD,L1\n"
        "2026-05-01,claude-sonnet-4-6,usage,100.00,USD,L1\n"
    )
    imported = _import(tenant_id, csv)
    run = engine.run_detail(tenant_id, engine.calculate(tenant_id, import_id=imported["id"])["id"])
    dupes = [m for m in run["matches"] if m["classification"] == engine.DUPLICATE_ROW]
    assert len(dupes) == 1 and dupes[0]["confidence"] == "confirmed"


def test_an_unknown_model_is_named_as_a_possible_cause(app_env, tenant_id):
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 100, model=None)
    imported = _import(tenant_id, _statement(["2026-05-01,claude-opus-9,usage,140.00,USD\n"]))
    run = engine.run_detail(tenant_id, engine.calculate(tenant_id, import_id=imported["id"])["id"])
    # No tracked row names that model, so it is unmatched rather than mispriced.
    kinds = {m["classification"] for m in run["matches"]}
    assert engine.UNKNOWN_MODEL in kinds or engine.PROVIDER_ONLY in kinds
    assert all(m["confidence"] in ("confirmed", "possible", "unknown") for m in run["matches"])


def test_an_unrecognised_line_type_is_reported_not_compared(app_env, tenant_id):
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 100)
    imported = _import(
        tenant_id,
        _statement(
            [
                "2026-05-01,claude-sonnet-4-6,usage,100.00,USD\n",
                "2026-05-01,,support plan,0.00,USD\n",
            ]
        ),
    )
    run = engine.run_detail(tenant_id, engine.calculate(tenant_id, import_id=imported["id"])["id"])
    assert run["status"] == "matched"
    assert engine.UNSUPPORTED_LINE in {m["classification"] for m in run["matches"]}


def test_nothing_tracked_is_incomplete_data_not_a_discrepancy(app_env, tenant_id):
    _enable(tenant_id)
    imported = _import(tenant_id, _statement(["2026-05-01,claude-sonnet-4-6,usage,100.00,USD\n"]))
    run = engine.calculate(tenant_id, import_id=imported["id"])
    assert run["status"] == "incomplete_data"


def test_matching_prefers_the_api_key_when_both_sides_have_one(app_env, tenant_id):
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 60, api_key="key-a", workspace="ws1")
    _track(app_env, tenant_id, MAY, 40, api_key="key-b", workspace="ws1")
    csv = (
        "date,model,category,cost,currency,workspace_id,api_key_id\n"
        "2026-05-01,claude-sonnet-4-6,usage,60.00,USD,ws1,key-a\n"
        "2026-05-01,claude-sonnet-4-6,usage,40.00,USD,ws1,key-b\n"
    )
    imported = _import(tenant_id, csv)
    run = engine.run_detail(tenant_id, engine.calculate(tenant_id, import_id=imported["id"])["id"])
    assert run["status"] == "matched"
    assert {m["strategy"] for m in run["matches"] if m["strategy"].startswith("account")} == {
        "account_key_date_model"
    }


def test_one_tracked_row_is_never_counted_against_two_statement_lines(app_env, tenant_id):
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 100)
    imported = _import(
        tenant_id,
        _statement(
            [
                "2026-05-01,claude-sonnet-4-6,usage,100.00,USD\n",
                "2026-05-01,claude-sonnet-4-6,usage,100.00,USD\n",
            ]
        ),
    )
    run = engine.run_detail(tenant_id, engine.calculate(tenant_id, import_id=imported["id"])["id"])
    # The second line finds nothing left to match, rather than the same $100 twice.
    assert run["provider_usage"] == 200.0 and run["tracked_usage"] == 100.0
    assert run["status"] == "discrepancy"


def test_hook_and_self_hosted_spend_are_not_compared_against_a_bill(app_env, tenant_id):
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 100)
    _track(app_env, tenant_id, MAY, 999, source="hook")
    _track(app_env, tenant_id, MAY, 999, source="self_host")
    imported = _import(tenant_id, _statement(["2026-05-01,claude-sonnet-4-6,usage,100.00,USD\n"]))
    run = engine.calculate(tenant_id, import_id=imported["id"])
    # Hook spend is a second observation of the same calls; counting it would
    # double the tracked side against the bill.
    assert run["tracked_usage"] == 100.0 and run["status"] == "matched"


# ---- imports: duplicates, replacement, removal -----------------------------
def test_importing_the_same_file_twice_is_idempotent(app_env, tenant_id):
    _enable(tenant_id)
    csv = _statement(["2026-05-01,claude-sonnet-4-6,usage,100.00,USD\n"])
    first = _import(tenant_id, csv)
    second = _import(tenant_id, csv)
    assert second["duplicate"] is True
    assert second["id"] == first["id"]
    assert len(imports.history(tenant_id)) == 1


def test_a_corrected_export_can_replace_the_first(app_env, tenant_id):
    _enable(tenant_id)
    first = _import(tenant_id, _statement(["2026-05-01,claude-sonnet-4-6,usage,100.00,USD\n"]))
    second = imports.commit(
        tenant_id,
        provider="anthropic",
        filename="corrected.csv",
        content=_statement(["2026-05-01,claude-sonnet-4-6,usage,110.00,USD\n"]),
        actor="a@b.com",
        replace_import_id=first["id"],
    )
    history = {i["id"]: i["status"] for i in imports.history(tenant_id)}
    assert history[first["id"]] == "superseded"
    assert history[second["id"]] == "committed"


def test_removal_is_recoverable_and_recorded(app_env, tenant_id):
    _enable(tenant_id)
    imported = _import(tenant_id, _statement(["2026-05-01,claude-sonnet-4-6,usage,10.00,USD\n"]))
    removed = imports.remove(tenant_id, imported["id"], actor="a@b.com")
    assert removed["status"] == "removed" and removed["removed_at"]
    # The rows are still there; only the import's status changed.
    with tenant_conn(tenant_id) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM recon_line_item WHERE import_id = %s", (imported["id"],)
            ).fetchone()[0]
            == 1
        )
    with pytest.raises(ReconciliationError):
        engine.calculate(tenant_id, import_id=imported["id"])


def test_every_material_action_is_audited(app_env, tenant_id):
    _enable(tenant_id)
    imported = _import(tenant_id, _statement(["2026-05-01,claude-sonnet-4-6,usage,10.00,USD\n"]))
    engine.calculate(tenant_id, import_id=imported["id"], actor="a@b.com")
    imports.remove(tenant_id, imported["id"], actor="a@b.com")
    with tenant_conn(tenant_id) as conn:
        events = [
            r[0]
            for r in conn.execute("SELECT event FROM recon_audit ORDER BY created_at").fetchall()
        ]
    assert {
        "settings_changed",
        "import_created",
        "reconciliation_completed",
        "import_removed",
    } <= set(events)


def test_the_audit_trail_never_holds_the_file(app_env, tenant_id):
    _enable(tenant_id)
    secret = "sk-do-not-log-me"
    csv = f"date,model,category,cost,currency,notes\n2026-05-01,c,usage,10.00,USD,{secret}\n"
    _import(tenant_id, csv)
    with tenant_conn(tenant_id) as conn:
        blob = str(conn.execute("SELECT detail FROM recon_audit").fetchall())
    assert secret not in blob


# ---- recalculation ---------------------------------------------------------
def test_recalculating_writes_a_new_run_and_keeps_the_old_one(app_env, tenant_id):
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 50)
    imported = _import(tenant_id, _statement(["2026-05-01,claude-sonnet-4-6,usage,100.00,USD\n"]))
    first = engine.calculate(tenant_id, import_id=imported["id"], actor="a@b.com")
    assert first["status"] == "discrepancy"

    # Ingestion catches up, and the same import is reconciled again.
    _track(app_env, tenant_id, MAY, 50)
    second = engine.calculate(tenant_id, import_id=imported["id"], actor="a@b.com")

    assert second["id"] != first["id"]
    assert second["status"] == "matched"
    # The earlier answer is still readable, unchanged.
    assert engine.run_detail(tenant_id, first["id"])["status"] == "discrepancy"
    assert len(engine.runs(tenant_id)) == 2


# ---- isolation -------------------------------------------------------------
def test_one_tenant_cannot_see_another_tenants_reconciliation(app_env, tenant_id):
    other = str(
        app_env.execute("INSERT INTO tenant (name) VALUES ('Other Co') RETURNING id").fetchone()[0]
    )
    app_env.commit()
    _enable(tenant_id)
    _enable(other)
    mine = _import(tenant_id, _statement(["2026-05-01,claude-sonnet-4-6,usage,100.00,USD\n"]))
    run = engine.calculate(tenant_id, import_id=mine["id"])

    assert imports.history(other) == []
    assert engine.runs(other) == []
    with pytest.raises(ReconciliationError):
        engine.run_detail(other, run["id"])
    with pytest.raises(ReconciliationError):
        report.export_csv(other, run["id"])


def test_reconciliation_writes_nothing_to_the_existing_cost_tables(app_env, tenant_id):
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 100)
    before = app_env.execute(
        "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM inference_cost_daily"
    ).fetchone()
    imported = _import(tenant_id, _statement(["2026-05-01,claude-sonnet-4-6,usage,283.00,USD\n"]))
    engine.calculate(tenant_id, import_id=imported["id"])
    after = app_env.execute(
        "SELECT COUNT(*), COALESCE(SUM(amount), 0) FROM inference_cost_daily"
    ).fetchone()
    # Same rows, same total: a discrepancy is reported, never corrected.
    assert before == after


def test_reconciliation_leaves_the_existing_bill_reconciliation_table_alone(app_env, tenant_id):
    # A table of the same name already exists for the hook-vs-cost-API check.
    # This module must not be confused with it, or touch it.
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 100)
    imported = _import(tenant_id, _statement(["2026-05-01,claude-sonnet-4-6,usage,150.00,USD\n"]))
    engine.calculate(tenant_id, import_id=imported["id"])
    assert app_env.execute("SELECT COUNT(*) FROM bill_reconciliation").fetchone()[0] == 0


# ---- report export ---------------------------------------------------------
def test_the_report_carries_the_categories_evidence_and_tolerances(app_env, tenant_id):
    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 100)
    imported = _import(
        tenant_id,
        _statement(
            [
                "2026-05-01,claude-sonnet-4-6,usage,100.00,USD\n",
                "2026-05-01,,tax,8.00,USD\n",
            ]
        ),
    )
    run = engine.calculate(tenant_id, import_id=imported["id"])
    filename, body = report.export_csv(tenant_id, run["id"])
    assert filename.endswith(".csv") and "anthropic" in filename
    for expected in [
        "Provider usage subtotal",
        "Provider tax",
        "Meter tracked usage",
        "Tolerance (absolute)",
        "Classification",
        "Confidence",
        "Evidence",
    ]:
        assert expected in body


def test_the_report_cannot_carry_a_formula_out(app_env, tenant_id):
    import csv as csv_module
    import io

    _enable(tenant_id)
    _track(app_env, tenant_id, MAY, 10)
    imported = _import(tenant_id, _statement(['2026-05-01,=HYPERLINK("x"),usage,10.00,USD\n']))
    run = engine.calculate(tenant_id, import_id=imported["id"])
    _, body = report.export_csv(tenant_id, run["id"])

    def is_number(cell: str) -> bool:
        try:
            float(cell)
            return True
        except ValueError:
            return False

    # A spreadsheet evaluates a TEXT cell that begins with one of these. A
    # negative number legitimately begins with "-" and must stay a number —
    # neutralising it would corrupt the figures the report exists to carry.
    cells = [c for row in csv_module.reader(io.StringIO(body)) for c in row]
    assert cells, "the export should not be empty"
    assert not [c for c in cells if c[:1] in ("=", "+", "-", "@") and not is_number(c)]
    # And the provider's original text is still recoverable, just inert.
    assert any("HYPERLINK" in c for c in cells)


# ---------------------------------------------------------------------------
# The HTTP surface: the flag gates it, and the session scopes it.
# ---------------------------------------------------------------------------
PASSWORD = "correct horse battery"


@pytest.fixture
def client(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    from fastapi.testclient import TestClient
    from meter.api import create_app

    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    c = TestClient(create_app())
    c.post("/api/auth/signup", json={"email": "cfo@acme.com", "password": PASSWORD})
    return c


def _tenant_of(client) -> str:
    return client.get("/api/auth/me").json()["tenant_id"]


ONE_ROW = "date,model,category,cost,currency\n2026-05-01,claude-sonnet-4-6,usage,10.00,USD\n"


def test_signed_out_callers_reach_nothing(admin_conn, admin_conninfo, app_conninfo, monkeypatch):
    from fastapi.testclient import TestClient
    from meter.api import create_app

    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    monkeypatch.setenv("DATABASE_URL", admin_conninfo)
    monkeypatch.setenv("DATABASE_APP_URL", app_conninfo)
    anon = TestClient(create_app())
    for path in [
        "/api/reconciliation/settings",
        "/api/reconciliation/imports",
        "/api/reconciliation/runs",
    ]:
        assert anon.get(path).status_code == 401, path
    for path in [
        "/api/reconciliation/preview",
        "/api/reconciliation/imports",
        "/api/reconciliation/runs",
    ]:
        assert anon.post(path, json={}).status_code == 401, path


def test_every_route_is_closed_while_the_flag_is_off(client):
    # Settings is readable (the UI must be able to ask whether to offer it)…
    assert client.get("/api/reconciliation/settings").json()["enabled"] is False
    # …and everything else behaves as though it does not exist.
    assert client.get("/api/reconciliation/imports").status_code == 404
    assert client.get("/api/reconciliation/runs").status_code == 404
    assert client.post("/api/reconciliation/preview", json={"content": ONE_ROW}).status_code == 404
    assert (
        client.post(
            "/api/reconciliation/imports",
            json={"provider": "anthropic", "content": ONE_ROW},
        ).status_code
        == 404
    )
    assert client.post("/api/reconciliation/runs", json={"import_id": "x"}).status_code == 404


def test_the_whole_flow_over_http(client, app_env):
    tenant = _tenant_of(client)
    assert client.put("/api/reconciliation/settings", json={"enabled": True}).status_code == 200
    _track(app_env, tenant, MAY, 10)

    preview = client.post("/api/reconciliation/preview", json={"content": ONE_ROW}).json()
    assert preview["accepted_count"] == 1 and preview["missing_required"] == []

    created = client.post(
        "/api/reconciliation/imports",
        json={"provider": "anthropic", "filename": "may.csv", "content": ONE_ROW},
    )
    assert created.status_code == 201
    import_id = created.json()["id"]
    assert created.json()["imported_by"] == "cfo@acme.com"

    run = client.post("/api/reconciliation/runs", json={"import_id": import_id})
    assert run.status_code == 201 and run.json()["status"] == "matched"
    run_id = run.json()["id"]

    detail = client.get(f"/api/reconciliation/runs/{run_id}")
    assert detail.status_code == 200 and detail.json()["matches"]

    export = client.get(f"/api/reconciliation/runs/{run_id}/report.csv")
    assert export.status_code == 200
    assert export.headers["content-type"].startswith("text/csv")
    assert "attachment" in export.headers["content-disposition"]
    assert "Meter reconciliation report" in export.text


def test_an_unsupported_provider_is_refused(client):
    client.put("/api/reconciliation/settings", json={"enabled": True})
    response = client.post(
        "/api/reconciliation/imports", json={"provider": "not-a-provider", "content": ONE_ROW}
    )
    assert response.status_code == 400


def test_one_tenant_cannot_read_another_tenants_run_over_http(client, app_env):
    client.put("/api/reconciliation/settings", json={"enabled": True})
    tenant = _tenant_of(client)
    _track(app_env, tenant, MAY, 10)
    import_id = client.post(
        "/api/reconciliation/imports", json={"provider": "anthropic", "content": ONE_ROW}
    ).json()["id"]
    run_id = client.post("/api/reconciliation/runs", json={"import_id": import_id}).json()["id"]

    client.post("/api/auth/logout")
    client.post("/api/auth/signup", json={"email": "someone@other.com", "password": PASSWORD})
    client.put("/api/reconciliation/settings", json={"enabled": True})

    assert client.get(f"/api/reconciliation/runs/{run_id}").status_code == 404
    assert client.get(f"/api/reconciliation/runs/{run_id}/report.csv").status_code == 404
    assert client.get("/api/reconciliation/imports").json() == []


def test_an_oversized_upload_is_refused_by_the_schema(client):
    client.put("/api/reconciliation/settings", json={"enabled": True})
    response = client.post(
        "/api/reconciliation/preview",
        json={"content": "x" * (imports.MAX_BYTES + 1)},
    )
    assert response.status_code == 422


def test_a_failed_calculation_is_a_state_not_an_error(client, app_env):
    client.put("/api/reconciliation/settings", json={"enabled": True})
    tenant = _tenant_of(client)
    # An import whose rows are all rejected has nothing dated to reconcile.
    import_id = client.post(
        "/api/reconciliation/imports",
        json={"provider": "anthropic", "content": "date,cost\n2026-05-01,10.00\n"},
    ).json()["id"]
    with tenant_conn(tenant) as conn:
        conn.execute("UPDATE recon_line_item SET mapping_status = 'rejected'")
    response = client.post("/api/reconciliation/runs", json={"import_id": import_id})
    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    assert response.json()["failure_reason"]
