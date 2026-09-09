"""Importing a downloaded bill: what the parser matches, and what it refuses."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from meter import infra_csv
from meter.infra_csv import CsvImportError, parse

REDIS = """Subscription,Database,Usage Date,Cost (USD),Currency
prod-cache,sessions,2026-05-01,142.50,USD
prod-cache,search,2026-05-01,88.25,USD
staging,scratch,2026-05-02,$12.10,USD
"""


def test_it_matches_columns_by_meaning_not_by_exact_name():
    report = parse(REDIS)
    # "Cost (USD)" is a cost column. Failing to see that and reporting "no
    # amount column" would be a confusing thing to say about a file whose
    # column is plainly labelled Cost.
    assert report.mapping["amount"] == "Cost (USD)"
    assert report.mapping["date"] == "Usage Date"
    assert report.mapping["tag"] == "Subscription"
    assert report.mapping["usage_type"] == "Database"
    assert report.total == Decimal("242.85")
    assert (report.first_day, report.last_day) == (dt.date(2026, 5, 1), dt.date(2026, 5, 2))


@pytest.mark.parametrize(
    "header",
    ["Cost", "Cost (USD)", "cost_usd", "Amount", "Total Cost", "BilledCost", "Charge", "Spend"],
)
def test_the_amount_column_is_recognised_however_it_is_labelled(header):
    report = parse(f"Date,{header}\n2026-05-01,10.00\n")
    assert report.mapping["amount"] == header
    assert report.total == Decimal("10.00")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("142.50", Decimal("142.50")),
        ("$1,234.56", Decimal("1234.56")),
        ("  88.00  ", Decimal("88.00")),
        # Accountancy negatives. A credit read as a charge overstates the bill.
        ("(25.00)", Decimal("-25.00")),
        ("-25.00", Decimal("-25.00")),
        ("USD 42.00", Decimal("42.00")),
    ],
)
def test_money_is_read_the_way_spreadsheets_write_it(raw, expected):
    (item,) = parse(f'Date,Amount\n2026-05-01,"{raw}"\n').items
    assert item.amount == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-05-04", dt.date(2026, 5, 4)),
        ("2026-05-04T12:00:00Z", dt.date(2026, 5, 4)),
        ("2026/05/04", dt.date(2026, 5, 4)),
        ("04-May-2026", dt.date(2026, 5, 4)),
        # A month-only column anchors to the first, like every monthly bill.
        ("2026-05", dt.date(2026, 5, 1)),
    ],
)
def test_dates_are_read_in_the_formats_bills_use(raw, expected):
    (item,) = parse(f"Date,Amount\n{raw},10.00\n").items
    assert item.period == expected


# "05/04/2026" is 4 May or 5 April depending on convention, and the two readings
# put the same spend in different MONTHS. The column decides, not a guess.
def test_a_slashed_date_column_disambiguates_itself():
    day_first = parse("Date,Amount\n05/04/2026,10.00\n25/04/2026,10.00\n")
    assert [i.period for i in day_first.items] == [dt.date(2026, 4, 5), dt.date(2026, 4, 25)]

    month_first = parse("Date,Amount\n05/04/2026,10.00\n04/25/2026,10.00\n")
    assert [i.period for i in month_first.items] == [dt.date(2026, 5, 4), dt.date(2026, 4, 25)]


def test_a_genuinely_ambiguous_date_column_says_so():
    report = parse("Date,Amount\n05/04/2026,10.00\n")
    assert report.items[0].period == dt.date(2026, 5, 4)
    # Reassuring someone about a date we cannot actually determine would be
    # worse than the ambiguity itself.
    assert any("ambiguous" in w for w in report.warnings)


def test_a_column_that_cannot_be_read_either_way_is_refused():
    with pytest.raises(CsvImportError, match="mixes day-first and month-first"):
        parse("Date,Amount\n25/04/2026,10.00\n04/25/2026,10.00\n")


def test_skipped_rows_are_counted_and_explained_never_dropped_silently():
    report = parse(
        "Date,Amount\n"
        "2026-05-01,10.00\n"
        "not-a-date,5.00\n"
        "2026-05-02,not-a-number\n"
        "2026-05-03,0.00\n"
        "\n"  # a blank line is not a skipped row
    )
    assert len(report.items) == 1
    assert report.rows_read == 4
    assert report.rows_skipped == 3
    assert sorted(report.warnings) == [
        "1 row skipped: unreadable amount",
        "1 row skipped: unreadable date",
        "1 row skipped: zero amount",
    ]


def test_a_column_we_did_not_understand_is_named_not_ignored():
    report = parse("Date,Amount,Cost Centre,Notes\n2026-05-01,10.00,cc-1,hello\n")
    assert set(report.unmapped) == {"Cost Centre", "Notes"}
    assert any("Columns not used" in w for w in report.warnings)


def test_mixed_currencies_are_flagged_because_the_total_would_be_meaningless():
    report = parse("Date,Amount,Currency\n2026-05-01,10.00,USD\n2026-05-02,10.00,EUR\n")
    assert report.currency == "mixed"
    assert any("mixes" in w for w in report.warnings)
    # The amounts are still stored as given rather than converted.
    assert [i.currency for i in report.items] == ["USD", "EUR"]


def test_a_caller_can_correct_a_column_we_guessed_wrong():
    # Two plausible amount columns; the customer says which is the real one.
    csv = "Date,List Price,Charge\n2026-05-01,99.00,42.00\n"
    assert parse(csv).total == Decimal("42.00")  # "Charge" wins by alias order
    corrected = parse(csv, mapping_override={"amount": "List Price"})
    assert corrected.total == Decimal("99.00")
    assert corrected.mapping["amount"] == "List Price"


def test_a_bill_with_no_service_column_is_labelled_with_the_platform():
    # Otherwise every row classifies as unclassified, which is a worse answer
    # than the one we actually know: whose bill this is.
    (item,) = parse("Date,Amount\n2026-05-01,10.00\n", default_service="Redis Cloud").items
    assert item.service == "Redis Cloud"


def test_the_tag_column_drives_feature_attribution():
    (item,) = parse("Date,Amount,Feature\n2026-05-01,10.00,triage\n", tag_key="feature").items
    assert (item.tag_key, item.tag_value) == ("feature", "triage")


@pytest.mark.parametrize(
    "text, message",
    [
        ("", "empty"),
        ("   ", "empty"),
        ("Date,Description\n2026-05-01,hello\n", "an amount"),
        ("Amount,Description\n10.00,hello\n", "a date"),
        ("Date,Amount\nnot-a-date,nope\n", "No rows could be imported"),
    ],
)
def test_an_unreadable_file_says_what_is_wrong_with_it(text, message):
    with pytest.raises(CsvImportError, match=message):
        parse(text)


def test_the_missing_column_error_lists_what_the_file_actually_has():
    # So the customer can see the mismatch rather than guess at it.
    with pytest.raises(CsvImportError) as exc:
        parse("День,Сумма\n2026-05-01,10.00\n")
    assert "День" in str(exc.value)


def test_every_meaning_has_aliases_and_none_are_shared():
    # A header claimed by two meanings would make matching order-dependent in a
    # way nobody reading the table would expect.
    seen: dict = {}
    for meaning, aliases in infra_csv._ALIASES.items():
        assert aliases, meaning
        for alias in aliases:
            assert alias not in seen, f"{alias} claimed by {seen.get(alias)} and {meaning}"
            seen[alias] = meaning


def test_the_report_reports_the_file_s_own_inclusive_range():
    # The window persist replaces is half-open; the range shown to a customer is
    # the file's. They are different numbers and must not be conflated.
    report = parse("Date,Amount\n2026-05-01,10.00\n2026-05-02,10.00\n")
    assert (report.first_day, report.last_day) == (dt.date(2026, 5, 1), dt.date(2026, 5, 2))
    assert (report.as_dict()["from"], report.as_dict()["to"]) == ("2026-05-01", "2026-05-02")
