"""Dashboard + drill-down aggregation over a realistic seeded tenant."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from meter import dashboard, inference, providers, resources
from meter.providers import CostRecord, UsageRecord
from meter.sampledata import insert_sample_data

PERIOD = dt.date(2026, 5, 1)  # sampledata's period


@pytest.fixture
def seeded(tenant_id, app_env):
    insert_sample_data(app_env, tenant_id)
    app_env.commit()
    return tenant_id


def _daily(app_env, tenant_id: str, day: dt.date, amount: float) -> None:
    """One day of connector-billed inference spend (the daily detail table)."""
    app_env.execute(
        """
        INSERT INTO inference_cost_daily
            (tenant_id, provider, model, amount, day, source, confidence)
        VALUES (%s, 'anthropic', 'c', %s, %s, 'cost_api', 'high')
        """,
        (tenant_id, amount, day),
    )


def _monthly(app_env, tenant_id: str, period: dt.date, amount: float) -> None:
    """The monthly inference_cost row an ingest writes alongside the daily rows."""
    app_env.execute(
        """
        INSERT INTO inference_cost
            (tenant_id, provider, model, amount, period, source, confidence)
        VALUES (%s, 'anthropic', 'c', %s, %s, 'cost_api', 'high')
        """,
        (tenant_id, amount, period),
    )


def test_dashboard_keeps_build_and_inference_separate(seeded):
    data = dashboard.dashboard(seeded, PERIOD)
    by_name = {f["name"]: f for f in data["features"]}
    triage = by_name["AI threat triage"]

    # Separate fields — never blended into one number.
    assert triage["build_cost"] == 181.0  # alice 117 + bob 64
    assert triage["inference_cost"] == 4200.0
    assert triage["confidence"] in {"high", "med", "low"}

    # cost per user uses recurring inference only.
    assert triage["active_users"] == 540
    assert abs(triage["cost_per_user"] - 4200.0 / 540) < 1e-6

    # Requests = number of AI model calls the feature made (from inference_cost).
    assert triage["requests"] == 320_000

    # The Unattributed bucket carries both unmapped build and inference spend.
    assert data["unattributed"]["build_cost"] == 30.0
    assert data["unattributed"]["inference_cost"] == 760.0


def test_dashboard_range_sums_across_months(seeded):
    # A 3-month range (Mar–May) sums more inference than the single latest month.
    one = dashboard.dashboard(seeded, range_token="this_month")
    three = dashboard.dashboard(seeded, range_token="last_3_months")
    assert three["months"] == 3
    assert one["months"] == 1
    assert three["totals"]["inference_cost"] > one["totals"]["inference_cost"]
    # The response reports the resolved window.
    assert three["start"] < three["end"]
    assert one["start"] == one["end"]


def test_dashboard_totals_have_prev_month_and_token_split(seeded):
    totals = dashboard.dashboard(seeded, PERIOD)["totals"]
    # Month-over-month deltas: the prior month's spend is reported alongside.
    assert "prev_build_cost" in totals
    assert "prev_inference_cost" in totals
    # April carried inference cost in the base fixture, so prev inference > 0.
    assert totals["prev_inference_cost"] > 0
    # Token split for the current month, summed from connector/hook rows.
    assert totals["tokens_in"] > 0
    assert totals["tokens_out"] > 0


def test_dashboard_executive_highlights(seeded):
    h = dashboard.dashboard(seeded, PERIOD)["highlights"]

    # Most expensive overall (build + inference): AI threat triage (181 + 4200).
    assert h["most_expensive"]["name"] == "AI threat triage"
    # Highest cost/user: Report generator (1850 / 120 ≈ 15.42 > SOC's 15.08).
    assert h["highest_cost_per_user"]["name"] == "Report generator"
    # Biggest optimization lever: costliest "watch" feature -> Report generator (1850/mo).
    assert h["optimization"]["name"] == "Report generator"
    assert h["optimization"]["worth_it"] == "watch"


def test_dashboard_generates_executive_insights(seeded):
    insights = dashboard.dashboard(seeded, PERIOD)["insights"]
    texts = [i["text"] for i in insights]

    # Concentration: triage (4200 + 181) is 54% of all AI spend (8131.75).
    assert "AI threat triage represents 54% of all AI spend ($4,381)." in texts
    # Governance: unattributed (790) is 9.7% of total AI costs.
    assert "Unattributed spend represents 9.7% of total AI costs ($790)." in texts
    # Trend: May's 8131.75 against April's 4499.75 in the base fixture. The
    # finding leads; what drove it is the second line, so the card can be
    # skimmed on the first.
    trend = next(i for i in insights if i["kind"] == "trend")
    assert trend["text"] == "AI spend is up 81% ($3,632) vs the previous month."
    assert trend["detail"] == "Mostly inference (run) cost."
    # The card stays readable: ranked candidates, capped.
    assert len(insights) <= 5


def test_insights_flag_an_unusually_expensive_day(seeded, app_env):
    # Eight ordinary $20 days and one $300 day: the outlier is called out against
    # the MEDIAN day, so a single spike can't hide inside a monthly average.
    for day in range(1, 9):
        _daily(app_env, seeded, dt.date(2026, 5, day), 20)
    _daily(app_env, seeded, dt.date(2026, 5, 9), 300)
    app_env.commit()

    insights = dashboard.dashboard(seeded, PERIOD)["insights"]
    assert insights[0]["kind"] == "spike"  # anomalies lead the card
    assert insights[0]["text"] == (
        "May 9 was the costliest day at $300 — 15x the $20.00 median day this period."
    )


def test_insights_stay_quiet_when_every_day_looks_alike(seeded, app_env):
    # Same total, spread evenly: nothing anomalous, so no spike insight.
    for day in range(1, 10):
        _daily(app_env, seeded, dt.date(2026, 5, day), 51)
    app_env.commit()

    kinds = {i["kind"] for i in dashboard.dashboard(seeded, PERIOD)["insights"]}
    assert "spike" not in kinds


def test_insights_project_the_current_month_pace(seeded, app_env):
    # The running month is partial, so comparing it to a full prior month would
    # understate it. Project from the days so far instead, and say so.
    today = dt.date.today()
    this_month = providers.month_start(today)
    last_month = providers.month_start(this_month - dt.timedelta(days=1))
    days_in_month = (providers.next_month(this_month) - this_month).days

    # A flat $10/day makes the projection exactly $10 x days-in-month, whenever
    # this test happens to run (at least the 5 days the rule requires).
    covered = min(10, max(today.day, 5))
    for day in range(1, covered + 1):
        _daily(app_env, seeded, this_month.replace(day=day), 10)
    _daily(app_env, seeded, last_month.replace(day=1), 100)  # prior month: $100
    # An ingest writes the monthly authority row in the same transaction as the
    # daily detail, so the fixture does too.
    _monthly(app_env, seeded, this_month, 10 * covered)
    _monthly(app_env, seeded, last_month, 100)
    app_env.commit()

    insights = dashboard.dashboard(seeded, start=this_month)["insights"]
    pace = next(i for i in insights if i["kind"].startswith("pace"))
    assert pace["kind"] == "pace"  # projected well above last month
    assert f"on pace for about ${10 * days_in_month:,}" in pace["text"]
    assert f"{this_month.strftime('%B')} is at ${10 * covered}" in pace["text"]
    assert f"above {last_month.strftime('%B')}'s $100" in pace["text"]

    # ...and the closed-window trend insight stays out of the way while it does.
    assert not any(i["kind"].startswith("trend") for i in insights)


def test_insights_size_non_production_spend(seeded, app_env):
    # Development/internal keys are the clearest cost-cutting angle billing data
    # supports — reported as spend under review, never as savings.
    for env, amt in [("development", 2000), ("internal", 500)]:
        app_env.execute(
            """
            INSERT INTO inference_cost
                (tenant_id, provider, model, amount, period, environment, source, confidence)
            VALUES (%s, 'anthropic', 'c', %s, %s, %s, 'cost_api', 'high')
            """,
            (seeded, amt, PERIOD, env),
        )
    app_env.commit()

    texts = [i["text"] for i in dashboard.dashboard(seeded, PERIOD)["insights"]]
    waste = next(t for t in texts if t.startswith("Non-production keys"))
    # 2500 of 10290 inference dollars.
    assert waste == (
        "Non-production keys are 24% of inference spend — $2,500 on development "
        "and internal work this period."
    )
    assert "savings" not in waste


def test_insights_flag_a_single_dominant_api_key(seeded, app_env):
    # Blast radius / negotiating position: one key carrying most of the bill.
    app_env.execute(
        """
        INSERT INTO inference_cost
            (tenant_id, provider, model, amount, period, api_key_name, source, confidence)
        VALUES (%s, 'anthropic', 'c', 20000, %s, 'triage-prod', 'cost_api', 'high')
        """,
        (seeded, PERIOD),
    )
    app_env.commit()

    texts = [i["text"] for i in dashboard.dashboard(seeded, PERIOD)["insights"]]
    assert any(
        t.startswith("One API key (triage-prod) drives 72% of inference spend") for t in texts
    )


def test_feature_detail_has_breakdowns_and_evidence(seeded):
    data = dashboard.dashboard(seeded, PERIOD)
    triage = next(f for f in data["features"] if f["name"] == "AI threat triage")

    detail = dashboard.feature_detail(seeded, triage["feature_id"], PERIOD)
    assert detail["headline"]["build_cost"] == 181.0
    assert detail["headline"]["inference_cost"] == 4200.0
    assert detail["headline"]["active_users"] == 540

    by_dev = {d["developer_id"]: d for d in detail["build_by_developer"]}
    assert set(by_dev) == {"alice", "bob"}
    # PRs / commits / files per developer come from the authored-PR evidence
    # (alice: #1421 [9c,21f] + #1432 [5c,16f] -> 2 PRs, 14 commits, 37 files).
    assert by_dev["alice"]["prs"] == 2
    assert by_dev["alice"]["commits"] == 14
    assert by_dev["alice"]["files_changed"] == 37
    assert by_dev["bob"]["prs"] == 1
    assert by_dev["bob"]["commits"] == 7
    # Total AI build spend + contributors for this feature.
    assert detail["build_total"] == 181.0
    assert detail["build_contributors"] == 2

    # Evidence trail: the actual signals behind the number.
    signal_types = {s["signal_type"] for s in detail["evidence"]}
    assert "pr" in signal_types and "branch" in signal_types
    assert detail["inference_sources"] == ["cost_api"]  # connector, not hook (yet)

    # Optimization opportunities: heuristic, derived from this month's inference.
    opt = detail["optimization"]
    names = {o["opportunity"] for o in opt["opportunities"]}
    # Triage is input-heavy, high volume -> the heuristic surfaces prompt caching.
    # (Model downgrade is now a grounded MEASURED opportunity, not a heuristic here.)
    assert "Prompt caching" in names
    assert "Model downgrade" not in names
    assert opt["monthly_savings"] > 0
    assert opt["annual_savings"] == round(opt["monthly_savings"] * 12, 2)
    # Conservative: combined estimate stays well under the $4,200 bill.
    assert opt["monthly_savings"] < 4200.0 * 0.6
    for o in opt["opportunities"]:
        assert o["confidence"] in {"high", "med", "low"} and o["rationale"]


def test_spend_by_provider_daily_trend(seeded, app_env):
    # The By provider tab exposes a day-resolution trend from inference_cost_daily.
    for day, amt, env in [
        (dt.date(2026, 5, 4), 30, "production"),
        (dt.date(2026, 5, 5), 45, "development"),
        (dt.date(2026, 5, 20), 25, "production"),
    ]:
        app_env.execute(
            """
            INSERT INTO inference_cost_daily
                (tenant_id, provider, model, amount, day, environment, source, confidence)
            VALUES (%s, 'anthropic', 'c', %s, %s, %s, 'cost_api', 'high')
            """,
            (seeded, amt, day, env),
        )
    app_env.commit()

    data = dashboard.spend_by_provider(seeded, range_token="this_month")
    by_day = {p["period"]: p for p in data["daily_trend"]}
    assert by_day["2026-05-04"]["production"] == 30.0
    assert by_day["2026-05-05"]["development"] == 45.0
    assert by_day["2026-05-20"]["total"] == 25.0
    # One point per day, buckets summing to each day's total.
    for p in data["daily_trend"]:
        parts = p["production"] + p["development"] + p["internal"] + p["unclassified"]
        assert round(parts, 2) == round(p["total"], 2)


def test_spend_by_token_type_splits_billed_dollars(seeded, app_env):
    # One priced Anthropic row: 1M total input (600k uncached, 300k cache write,
    # 100k cache read) + 200k output, billed $100. The split weights each type by
    # its real rate (sonnet: $3/Mtok in, $15/Mtok out; write 1.25x in, read 0.10x in)
    # and allocates the ACTUAL dollars, so the parts sum to the bill.
    app_env.execute(
        """
        INSERT INTO inference_cost
            (tenant_id, provider, model, amount, period, tokens_in, tokens_out,
             cached_tokens_in, cache_write_tokens, cache_write_5m_tokens,
             cache_write_1h_tokens, source, confidence)
        VALUES (%s, 'anthropic', 'claude-sonnet-4-6', 100, %s,
                1000000, 200000, 100000, 300000, 200000, 100000, 'cost_api', 'high')
        """,
        (seeded, dt.date(2026, 1, 1)),
    )
    app_env.commit()

    data = dashboard.spend_by_provider(seeded, start=dt.date(2026, 1, 1), end=dt.date(2026, 1, 1))
    by_type = {t["token_type"]: t for t in data["by_token_type"]}
    # Cache writes split by TTL, which price differently (5m 1.25x, 1h 2x input).
    assert set(by_type) == {"input", "cache_write_5m", "cache_write_1h", "cache_read", "output"}
    # Weights ($/Mtok x Mtok): in .6*3=1.8, w5m .2*3*1.25=0.75, w1h .1*3*2=0.6,
    # read .1*3*0.1=0.03, out .2*15=3.0 -> total 6.18.
    assert by_type["output"]["amount"] == pytest.approx(100 * 3.0 / 6.18, abs=0.01)
    assert by_type["input"]["amount"] == pytest.approx(100 * 1.8 / 6.18, abs=0.01)
    assert by_type["cache_write_5m"]["amount"] == pytest.approx(100 * 0.75 / 6.18, abs=0.01)
    assert by_type["cache_write_1h"]["amount"] == pytest.approx(100 * 0.6 / 6.18, abs=0.01)
    assert by_type["cache_read"]["amount"] == pytest.approx(100 * 0.03 / 6.18, abs=0.01)
    # Token COUNTS come straight from the provider — exact, not derived.
    assert by_type["input"]["tokens"] == 600_000  # 1M total input - cache traffic
    assert by_type["cache_write_5m"]["tokens"] == 200_000
    assert by_type["cache_write_1h"]["tokens"] == 100_000
    assert by_type["cache_read"]["tokens"] == 100_000
    assert by_type["output"]["tokens"] == 200_000
    # The split always reconciles with the billed dollars, and shares add to 100.
    assert data["token_total"] == pytest.approx(100.0, abs=0.01)
    assert sum(t["pct"] for t in data["by_token_type"]) == pytest.approx(100.0, abs=1e-6)
    # Output dominates -> it sorts first, and every row is labelled for the UI.
    assert data["by_token_type"][0]["token_type"] == "output"
    assert by_type["cache_write_5m"]["label"] == "Cache write (5m)"


def test_token_type_falls_back_to_unknown_without_token_detail(seeded, app_env):
    # A row with dollars but no token counts is reported as Unknown, never guessed.
    app_env.execute(
        """
        INSERT INTO inference_cost (tenant_id, provider, model, amount, period,
                                    source, confidence)
        VALUES (%s, 'together', 'mystery-model', 50, %s, 'cost_api', 'low')
        """,
        (seeded, dt.date(2026, 1, 1)),
    )
    app_env.commit()

    data = dashboard.spend_by_provider(seeded, start=dt.date(2026, 1, 1), end=dt.date(2026, 1, 1))
    by_type = {t["token_type"]: t["amount"] for t in data["by_token_type"]}
    assert by_type["unknown"] == pytest.approx(50.0, abs=0.01)


def test_trend_points_carry_a_workspace_breakdown(seeded, app_env):
    # Each trend point exposes where its spend came from, so the chart's hover can
    # show a per-workspace split alongside the classification split.
    day = dt.date(2026, 5, 6)
    for ws, amt in [("Triage WS", 70), ("Shared WS", 30)]:
        app_env.execute(
            """
            INSERT INTO inference_cost_daily
                (tenant_id, provider, model, amount, day, workspace_id, workspace_name,
                 environment, source, confidence)
            VALUES (%s, 'anthropic', 'c', %s, %s, %s, %s, 'production', 'cost_api', 'high')
            """,
            (seeded, amt, day, ws, ws),
        )
        app_env.execute(
            """
            INSERT INTO inference_cost
                (tenant_id, provider, model, amount, period, workspace_id, workspace_name,
                 environment, source, confidence)
            VALUES (%s, 'anthropic', 'c', %s, %s, %s, %s, 'production', 'cost_api', 'high')
            """,
            (seeded, amt, PERIOD, ws, ws),
        )
    app_env.commit()

    data = dashboard.spend_by_provider(seeded, range_token="this_month")
    point = next(p for p in data["daily_trend"] if p["period"] == day.isoformat())
    assert {w["workspace"]: w["amount"] for w in point["workspaces"]} == {
        "Triage WS": 70.0,
        "Shared WS": 30.0,
    }
    # Workspace amounts reconcile with that point's total, largest first.
    assert round(sum(w["amount"] for w in point["workspaces"]), 2) == round(point["total"], 2)
    assert [w["workspace"] for w in point["workspaces"]] == ["Triage WS", "Shared WS"]
    # The monthly trend carries the same split.
    m = next(p for p in data["trend"] if p["period"] == PERIOD.isoformat())
    assert {w["workspace"] for w in m["workspaces"]} == {"Triage WS", "Shared WS"}


def test_spend_by_provider_workspace_api_key_breakdown(seeded, app_env):
    # Anthropic rows carry workspace/api_key identity; the By provider tab rolls it
    # up into a workspace -> API key breakdown that reconciles to the inference total.
    for ws_id, ws_name, key, amt, t_in, t_out in [
        ("ws_triage", "Triage WS", "triage-key", 100, 900, 100),
        ("ws_triage", "Triage WS", "triage-key-2", 40, 400, 50),
        ("ws_shared", "Shared WS", "shared-key", 25, 200, 20),
    ]:
        app_env.execute(
            """
            INSERT INTO inference_cost
                (tenant_id, provider, model, amount, currency, period,
                 workspace_id, workspace_name, api_key_id, api_key_name,
                 tokens_in, tokens_out, source, confidence)
            VALUES (%s, 'anthropic', 'claude', %s, 'USD', %s, %s, %s, %s, %s,
                    %s, %s, 'cost_api', 'high')
            """,
            (seeded, amt, PERIOD, ws_id, ws_name, key, key, t_in, t_out),
        )
    app_env.commit()

    data = dashboard.spend_by_provider(seeded, range_token="this_month")
    by_ws = {w["workspace"]: w for w in data["by_workspace"]}
    assert by_ws["Triage WS"]["amount"] == 140.0
    assert {k["api_key"]: k["amount"] for k in by_ws["Triage WS"]["by_key"]} == {
        "triage-key": 100.0,
        "triage-key-2": 40.0,
    }
    assert by_ws["Shared WS"]["amount"] == 25.0
    assert data["workspace_total"] == 165.0
    # Provider-reported token counts (input + output) ride alongside the dollars.
    assert by_ws["Triage WS"]["tokens"] == 1450
    assert {k["api_key"]: k["tokens"] for k in by_ws["Triage WS"]["by_key"]} == {
        "triage-key": 1000,
        "triage-key-2": 450,
    }
    assert by_ws["Shared WS"]["tokens"] == 220
    # Each workspace's keys reconcile to its subtotal, with per-workspace shares to 100.
    for w in data["by_workspace"]:
        assert round(sum(k["amount"] for k in w["by_key"]), 2) == round(w["amount"], 2)
        assert abs(sum(k["pct"] for k in w["by_key"]) - 100.0) < 1e-6


def test_feature_detail_reconciles_with_overview_over_range(seeded):
    # A feature's detail totals must equal its Overview row for the SAME range.
    board = dashboard.dashboard(seeded, range_token="last_3_months")
    report = next(f for f in board["features"] if f["name"] == "Report generator")

    detail = dashboard.feature_detail(seeded, report["feature_id"], range_token="last_3_months")
    assert detail["build_total"] == report["build_cost"]
    assert detail["headline"]["inference_cost"] == report["inference_cost"]
    # Per-developer build spend reconciles with the feature's build total.
    assert round(sum(d["amount"] for d in detail["build_by_developer"]), 2) == round(
        detail["build_total"], 2
    )

    # The range genuinely widens inference vs a single month (report has 3 months
    # of history), proving the detail respects the selected period — and the single
    # month still reconciles with that month's Overview row.
    one_month = dashboard.feature_detail(seeded, report["feature_id"], range_token="this_month")
    assert detail["headline"]["inference_cost"] > one_month["headline"]["inference_cost"]
    board_month = dashboard.dashboard(seeded, range_token="this_month")
    report_month = next(f for f in board_month["features"] if f["name"] == "Report generator")
    assert one_month["headline"]["inference_cost"] == report_month["inference_cost"]


def test_feature_inference_breakdown_and_window(seeded):
    data = dashboard.dashboard(seeded, PERIOD)
    report_id = next(f for f in data["features"] if f["name"] == "Report generator")["feature_id"]

    # This-month range: three models summing to the month's $1,850, gpt-4o on top.
    month = dashboard.feature_inference(seeded, report_id, range_token="this_month")
    by_model = {m["model"]: m for m in month["by_model"]}
    assert set(by_model) == {"gpt-4o", "claude-sonnet-4-6", "claude-haiku-4-5"}
    assert by_model["gpt-4o"]["amount"] == 1250.0
    assert by_model["gpt-4o"]["requests"] == 60_000
    assert abs(sum(m["pct"] for m in month["by_model"]) - 100.0) < 1e-6
    assert round(by_model["gpt-4o"]["pct"]) == 68  # 1250 / 1850
    assert len(month["trend"]) == 1  # just the latest month

    # Last-3-months range pulls in the prior months: gpt-4o 1250 + 1000 + 800, 3 trend points.
    quarter = dashboard.feature_inference(seeded, report_id, range_token="last_3_months")
    q_by_model = {m["model"]: m for m in quarter["by_model"]}
    assert q_by_model["gpt-4o"]["amount"] == 3050.0
    assert len(quarter["trend"]) == 3


def test_spend_by_provider_breakdown_and_window(seeded):
    # Month window: providers sum to the tenant's total inference for the month,
    # ordered by spend, with pct shares that add to 100.
    month = dashboard.spend_by_provider(seeded, range_token="this_month")
    by_provider = {p["provider"]: p for p in month["by_provider"]}
    assert set(by_provider) >= {"openai", "anthropic"}
    assert month["total"] == sum(p["amount"] for p in month["by_provider"])
    assert abs(sum(p["pct"] for p in month["by_provider"]) - 100.0) < 1e-6
    # Ordered by spend descending.
    amounts = [p["amount"] for p in month["by_provider"]]
    assert amounts == sorted(amounts, reverse=True)
    assert len(month["trend"]) == 1

    # The trend is a per-month classification stack whose buckets sum to the total.
    # Seeded rows have NULL environment -> counted as unclassified (not production).
    m = month["trend"][0]
    assert m["production"] + m["development"] + m["internal"] + m["unclassified"] == pytest.approx(
        m["total"], abs=0.01
    )
    assert m["total"] == pytest.approx(month["total"], abs=0.01)
    assert m["production"] == 0.0  # nothing auto-classified
    assert m["unclassified"] == pytest.approx(m["total"], abs=0.01)

    # Each provider carries a model breakdown that sums back to its own total,
    # with per-provider pct shares adding to 100.
    for p in month["by_provider"]:
        assert p["by_model"], f"{p['provider']} should have a model breakdown"
        assert abs(sum(m["amount"] for m in p["by_model"]) - p["amount"]) < 1e-6
        assert abs(sum(m["pct"] for m in p["by_model"]) - 100.0) < 1e-6

    # Build cost is grouped by tool, separately from inference (never blended).
    by_tool = {t["tool"]: t for t in month["build_by_tool"]}
    assert set(by_tool) <= {"claude_code", "cursor", "copilot", "codex"}
    assert month["build_total"] == sum(t["amount"] for t in month["build_by_tool"])
    if month["build_by_tool"]:
        assert abs(sum(t["pct"] for t in month["build_by_tool"]) - 100.0) < 1e-6
    # Build trend is its own series, not summed with inference.
    assert "build_trend" in month

    # Build cost also breaks down by developer, each with a per-tool split, and
    # developers reconcile to the same build total (invariant 4: nothing dropped).
    devs = month["build_by_developer"]
    assert devs, "seeded data should attribute build cost to developers"
    assert month["build_total"] == pytest.approx(sum(d["amount"] for d in devs), abs=1e-6)
    assert abs(sum(d["pct"] for d in devs) - 100.0) < 1e-6
    # Ordered by spend descending.
    dev_amounts = [d["amount"] for d in devs]
    assert dev_amounts == sorted(dev_amounts, reverse=True)
    # Each developer's tools sum back to that developer's total (shares add to 100).
    for d in devs:
        assert d["by_tool"], f"{d['developer_id']} should have a tool breakdown"
        assert abs(sum(t["amount"] for t in d["by_tool"]) - d["amount"]) < 1e-6
        assert abs(sum(t["pct"] for t in d["by_tool"]) - 100.0) < 1e-6
        # Every developer carries a non-empty display label.
        assert d["label"]
    # The seed stores both a name and a handle, so at least one label combines them.
    assert any(d["label"].endswith(")") and " (" in d["label"] for d in devs)

    # A 3-month range pulls in prior months and yields three trend points.
    quarter = dashboard.spend_by_provider(seeded, range_token="last_3_months")
    assert len(quarter["trend"]) == 3
    assert quarter["total"] >= month["total"]


def test_detail_missing_feature_returns_none(seeded):
    assert dashboard.feature_detail(seeded, "00000000-0000-0000-0000-000000000000", PERIOD) is None


# ---------------------------------------------------------------------------
# Inference trend: per-month classification stack (provider-independent).
# ---------------------------------------------------------------------------
def _acost(ws, amount, period=PERIOD):
    return CostRecord("anthropic", period, Decimal(str(amount)), project=ws)


def _ausage(ws, key, period=PERIOD):
    return UsageRecord(
        workspace_id=ws, api_key_id=key, model="claude-sonnet-4-6", tokens_in=1_000_000
    )


def test_inference_trend_classification_stack(tenant_id):
    workspaces = {"ws1": "ws-one"}
    api_keys = {
        "k_prod": {"name": "prod", "workspace_id": "ws1"},
        "k_dev": {"name": "dev", "workspace_id": "ws1"},
        "k_ign": {"name": "ign", "workspace_id": "ws1"},
    }
    # $99 across three equally-used keys -> $33 each; plus an OpenAI row (no env).
    cost = [_acost("ws1", 99)]
    usage = [_ausage("ws1", "k_prod"), _ausage("ws1", "k_dev"), _ausage("ws1", "k_ign")]
    inference.ingest_anthropic(tenant_id, PERIOD, cost, usage, workspaces, api_keys)
    inference.ingest_records(
        tenant_id, "openai", PERIOD, [CostRecord("openai", PERIOD, Decimal("30"), project="p")]
    )

    resources.set_classification(tenant_id, "anthropic", "api_key", "k_prod", "production")
    resources.set_classification(tenant_id, "anthropic", "api_key", "k_dev", "development")
    resources.set_classification(tenant_id, "anthropic", "api_key", "k_ign", "ignore")
    # Re-snapshot the classifications onto the cost rows.
    inference.ingest_anthropic(tenant_id, PERIOD, cost, usage, workspaces, api_keys)

    data = dashboard.spend_by_provider(tenant_id, range_token="this_month")
    assert len(data["trend"]) == 1
    m = data["trend"][0]
    # Each classification lands in its own stack; OpenAI (NULL env) -> unclassified.
    assert m["production"] == pytest.approx(33.0, abs=0.01)  # from Anthropic
    assert m["development"] == pytest.approx(33.0, abs=0.01)
    assert m["internal"] == 0.0
    assert m["unclassified"] == pytest.approx(30.0, abs=0.01)  # the OpenAI row
    # Ignore ($33) is excluded: buckets sum to total, which excludes it.
    assert m["total"] == pytest.approx(96.0, abs=0.01)
    assert (
        m["production"] + m["development"] + m["internal"] + m["unclassified"]
    ) == pytest.approx(m["total"], abs=0.01)
    # Provider totals also exclude the ignored spend.
    by_provider = {p["provider"]: p for p in data["by_provider"]}
    assert by_provider["anthropic"]["amount"] == pytest.approx(66.0, abs=0.01)
    assert by_provider["openai"]["amount"] == pytest.approx(30.0, abs=0.01)


def test_multi_month_trend_is_independent_per_month(tenant_id):
    may, apr = dt.date(2026, 5, 1), dt.date(2026, 4, 1)
    ws, keys = {"ws1": "ws-one"}, {"k1": {"name": "k1", "workspace_id": "ws1"}}
    inference.ingest_anthropic(
        tenant_id, apr, [_acost("ws1", 40, apr)], [_ausage("ws1", "k1", apr)], ws, keys
    )
    inference.ingest_anthropic(
        tenant_id, may, [_acost("ws1", 60, may)], [_ausage("ws1", "k1", may)], ws, keys
    )
    resources.set_classification(tenant_id, "anthropic", "api_key", "k1", "production")
    inference.ingest_anthropic(
        tenant_id, apr, [_acost("ws1", 40, apr)], [_ausage("ws1", "k1", apr)], ws, keys
    )
    inference.ingest_anthropic(
        tenant_id, may, [_acost("ws1", 60, may)], [_ausage("ws1", "k1", may)], ws, keys
    )

    data = dashboard.spend_by_provider(tenant_id, range_token="last_3_months")
    by_period = {t["period"]: t for t in data["trend"]}
    assert by_period["2026-04-01"]["production"] == pytest.approx(40.0, abs=0.01)
    assert by_period["2026-05-01"]["production"] == pytest.approx(60.0, abs=0.01)


def test_classification_is_independent_of_feature_attribution(tenant_id):
    # A production key with NO feature mapping is Production in the trend, yet
    # Unattributed on the feature side — the two dimensions never conflate.
    ws, keys = {"ws1": "ws-one"}, {"k1": {"name": "k1", "workspace_id": "ws1"}}
    cost, usage = [_acost("ws1", 50)], [_ausage("ws1", "k1")]
    inference.ingest_anthropic(tenant_id, PERIOD, cost, usage, ws, keys)
    resources.set_classification(tenant_id, "anthropic", "api_key", "k1", "production")
    inference.ingest_anthropic(tenant_id, PERIOD, cost, usage, ws, keys)

    trend = dashboard.spend_by_provider(tenant_id, range_token="this_month")["trend"][0]
    assert trend["production"] == pytest.approx(50.0, abs=0.01)  # classification dimension

    board = dashboard.dashboard(tenant_id, PERIOD)
    # Attribution dimension: no feature mapping -> Unattributed inference, unchanged.
    assert board["unattributed"]["inference_cost"] == pytest.approx(50.0, abs=0.01)


def test_dashboard_reports_when_cost_was_last_ingested(seeded, app_env):
    # The Overview's "Updated ..." stamp must reflect when cost data was WRITTEN,
    # not when the page/request happened — otherwise every login looks fresh.
    data = dashboard.dashboard(seeded, PERIOD)
    assert data["inference_updated_at"] is not None
    assert data["build_updated_at"] is not None
    # The headline stamp is the newer of the two sources.
    assert data["data_updated_at"] == max(data["inference_updated_at"], data["build_updated_at"])

    # Ingesting again moves the stamp forward; a plain re-read does not.
    before = data["data_updated_at"]
    unchanged = dashboard.dashboard(seeded, PERIOD)["data_updated_at"]
    assert unchanged == before  # re-reading is not "updating"

    app_env.execute(
        """
        INSERT INTO inference_cost (tenant_id, provider, model, amount, period,
                                    source, confidence)
        VALUES (%s, 'anthropic', 'c', 5, %s, 'cost_api', 'high')
        """,
        (seeded, PERIOD),
    )
    app_env.commit()
    assert dashboard.dashboard(seeded, PERIOD)["data_updated_at"] > before


def test_dashboard_freshness_is_null_before_any_cost(tenant_id):
    # A brand-new tenant has never synced: report null rather than "now".
    data = dashboard.dashboard(tenant_id, PERIOD)
    assert data["data_updated_at"] is None
    assert data["inference_updated_at"] is None
    assert data["build_updated_at"] is None


def _customer(app_env, tenant_id: str, cid: str, period: dt.date, amount, requests) -> None:
    """A month of SDK-metered spend for one of the tenant's own customers."""
    app_env.execute(
        """
        INSERT INTO customer_cost (tenant_id, customer_id, period, amount, request_count)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (tenant_id, cid, period, amount, requests),
    )


def test_spend_by_customer_ranks_with_unit_economics(seeded, app_env):
    # Metered spend (hook events tagged with metadata.customer_id) — the one
    # breakdown a provider bill can't produce.
    _customer(app_env, seeded, "acme", PERIOD, 600, 20_000)
    _customer(app_env, seeded, "globex", PERIOD, 150, 30_000)
    app_env.commit()

    data = dashboard.spend_by_customer(seeded, PERIOD)
    top, second = data["customers"]
    assert (top["customer_id"], second["customer_id"]) == ("acme", "globex")  # by spend
    assert top["amount"] == 600.0 and top["pct"] == 80.0
    assert data["total"] == 750.0
    # Unit economics: acme costs 6x more per call than globex despite fewer calls.
    assert top["cost_per_request"] == 0.03
    assert second["cost_per_request"] == 0.005


def test_spend_by_customer_reports_coverage_of_the_real_bill(seeded, app_env):
    # Metered spend is a SUBSET of the authoritative bill, never a second version
    # of it — the view says how much of the bill actually carries a customer tag.
    _customer(app_env, seeded, "acme", PERIOD, 779, 1_000)
    app_env.commit()

    data = dashboard.spend_by_customer(seeded, PERIOD)
    assert data["inference_total"] == 7790.0  # the whole month's inference bill
    assert data["total"] == 779.0
    assert round(data["coverage_pct"], 1) == 10.0
    assert data["total"] < data["inference_total"]
    # ...and that denominator is the Overview's number, not a second opinion.
    assert (
        data["inference_total"] == dashboard.dashboard(seeded, PERIOD)["totals"]["inference_cost"]
    )


def test_spend_by_customer_deltas_and_new_customers(seeded, app_env):
    # April -> May, one growing customer and one that only shows up in May.
    _customer(app_env, seeded, "acme", dt.date(2026, 4, 1), 100, 500)
    _customer(app_env, seeded, "acme", PERIOD, 150, 700)
    _customer(app_env, seeded, "newco", PERIOD, 40, 100)
    app_env.commit()

    by_id = {c["customer_id"]: c for c in dashboard.spend_by_customer(seeded, PERIOD)["customers"]}
    assert by_id["acme"]["prev_amount"] == 100.0
    assert by_id["acme"]["delta_pct"] == 50.0
    # A customer with no prior spend is "new", not a 0% change.
    assert by_id["newco"]["prev_amount"] is None
    assert by_id["newco"]["delta_pct"] is None


def test_spend_by_customer_is_empty_without_the_sdk(seeded):
    # Connector-only tenant: no metered calls, so nothing to attribute — and the
    # coverage figure says so rather than implying the bill has no customers.
    data = dashboard.spend_by_customer(seeded, PERIOD)
    assert data["customers"] == []
    assert data["total"] == 0.0
    assert data["coverage_pct"] == 0.0
    assert data["inference_total"] > 0  # there IS a bill; it just isn't tagged


def test_spend_by_customer_trend_spans_the_range(seeded, app_env):
    _customer(app_env, seeded, "acme", dt.date(2026, 3, 1), 10, 100)
    _customer(app_env, seeded, "acme", dt.date(2026, 5, 1), 30, 300)
    app_env.commit()

    data = dashboard.spend_by_customer(seeded, range_token="last_3_months")
    assert data["months"] == 3
    assert [(t["period"], t["amount"]) for t in data["trend"]] == [
        ("2026-03-01", 10.0),
        ("2026-05-01", 30.0),
    ]
    assert data["customers"][0]["months_active"] == 2


def test_insight_shares_never_exceed_the_reconciled_bill(seeded, app_env):
    # Regression: a provider metered by BOTH the hook and its cost connector
    # describes the same spend twice. The dashboard total reconciles them, so the
    # insight shares must use the same basis — summing raw rows once produced
    # "101% of inference spend", which is exactly the black-box number invariant 3
    # forbids.
    app_env.execute(
        """
        INSERT INTO inference_cost
            (tenant_id, provider, model, amount, period, source, confidence)
        VALUES (%s, 'anthropic', 'c', 500, %s, 'hook', 'high')
        """,
        (seeded, PERIOD),
    )
    app_env.commit()

    data = dashboard.dashboard(seeded, PERIOD)
    coverage = next(t for t in (i["text"] for i in data["insights"]) if "no environment set" in t)
    # The seeded month's connector bill is $7,790, all of it unclassified — the
    # hook rows don't inflate it past 100%.
    assert coverage.startswith("100% of inference spend ($7,790)")
    assert data["totals"]["inference_cost"] == 7790.0


def test_customer_coverage_uses_the_reconciled_bill(seeded, app_env):
    # Same reconciliation trap as the insights: hook rows and their connector rows
    # are the same dollar. Coverage must divide by the bill the Overview reports,
    # or a fully-tagged tenant would appear to cover less than it does.
    _customer(app_env, seeded, "acme", PERIOD, 500, 1_000)
    app_env.execute(
        """
        INSERT INTO inference_cost
            (tenant_id, provider, model, amount, period, source, confidence)
        VALUES (%s, 'anthropic', 'c', 500, %s, 'hook', 'high')
        """,
        (seeded, PERIOD),
    )
    app_env.commit()

    data = dashboard.spend_by_customer(seeded, PERIOD)
    # $7,790 stays the bill — the hook row doesn't add a sixth $500 on top of it.
    assert data["inference_total"] == 7790.0


def test_demo_seed_fills_the_by_customer_tab(tenant_id, app_env):
    # The demo tenant must never land on the "install the SDK" empty state — that
    # message is for real connector-only tenants, and it makes the demo look
    # broken. This pins the extended (demo) seed to a populated, coherent view.
    insert_sample_data(app_env, tenant_id, extended=True)
    app_env.commit()

    data = dashboard.spend_by_customer(tenant_id, PERIOD)
    assert len(data["customers"]) >= 5
    assert data["total"] > 0

    # Metered spend stays a believable SUBSET of the bill, as the tab claims.
    assert 0 < data["coverage_pct"] < 100
    assert data["total"] < data["inference_total"]
    assert round(sum(c["pct"] for c in data["customers"]), 2) == 100.0

    # The table is worth looking at: the rows differ from each other.
    assert any(c["delta_pct"] is None for c in data["customers"])  # a new customer
    assert any((c["delta_pct"] or 0) < 0 for c in data["customers"])  # one shrinking
    per_call = [c["cost_per_request"] for c in data["customers"]]
    assert max(per_call) > min(per_call) * 10  # unit economics actually vary

    # And a trend to draw: 12 months of history for the longest-running customers.
    assert len(dashboard.spend_by_customer(tenant_id, range_token="last_12_months")["trend"]) == 12


def test_developer_activity_is_scoped_to_the_period(seeded, app_env):
    # Activity sits beside per-developer spend on the same tab, so it must cover
    # the same window — scoped by the PR's own MERGE date, not by when Meter
    # happened to sync it.
    app_env.execute(
        """
        INSERT INTO feature_signal
            (tenant_id, feature_id, signal_type, external_ref, confidence, source,
             actor, commits, files_changed, additions, deletions, merged_at)
        SELECT %s, id, 'pr', 'acme/core#900', 'high', 'github',
               'alice', 4, 8, 200, 50, %s
        FROM feature WHERE tenant_id = %s ORDER BY created_at LIMIT 1
        """,
        (seeded, dt.date(2026, 2, 14), seeded),
    )
    app_env.commit()

    may = {
        a["handle"]: a for a in dashboard.spend_by_provider(seeded, PERIOD)["developer_activity"]
    }
    # The fixture merged alice's three PRs in May; February's falls outside it.
    assert may["alice"]["prs"] == 3
    assert may["alice"]["commits"] == 20  # 9 + 5 + 6
    assert may["alice"]["additions"] == 915  # 480 + 260 + 175

    wide = {
        a["handle"]: a
        for a in dashboard.spend_by_provider(seeded, range_token="last_6_months")[
            "developer_activity"
        ]
    }
    assert wide["alice"]["prs"] == 4  # February's PR is inside this window


def test_activity_coverage_explains_an_empty_activity_table(seeded, app_env):
    # An empty table has several causes and the reader can only act on the right
    # one, so the payload carries the facts that tell them apart.
    cov = dashboard.spend_by_provider(seeded, PERIOD)["activity_coverage"]
    assert cov["github_connected"] is False  # the fixture connects nothing
    assert cov["dated_prs"] > 0
    assert cov["first_merged"] is not None and cov["last_merged"] is not None

    # A window with no merged PRs still reports the evidence that exists
    # elsewhere -- which is what turns "nothing here" into "re-run discovery".
    far = dashboard.spend_by_provider(seeded, dt.date(2020, 1, 1))
    assert far["developer_activity"] == []
    assert far["activity_coverage"]["dated_prs"] == cov["dated_prs"]


def test_activity_coverage_counts_undated_prs_rather_than_dropping_them(seeded, app_env):
    # PRs discovered before migration 0035 carry no merge date, so they cannot be
    # placed in any window. They are counted, not silently ignored.
    app_env.execute(
        """
        INSERT INTO feature_signal
            (tenant_id, feature_id, signal_type, external_ref, confidence, source, actor)
        SELECT %s, id, 'pr', 'acme/core#901', 'high', 'github', 'alice'
        FROM feature WHERE tenant_id = %s ORDER BY created_at LIMIT 1
        """,
        (seeded, seeded),
    )
    app_env.commit()
    cov = dashboard.spend_by_provider(seeded, PERIOD)["activity_coverage"]
    assert cov["undated_prs"] == 1


def test_activity_coverage_notices_a_connected_github(seeded, app_env):
    app_env.execute(
        """
        INSERT INTO connector_credential (tenant_id, connector_type, ciphertext)
        VALUES (%s, 'github', %s)
        """,
        (seeded, b"x"),
    )
    app_env.commit()
    assert dashboard.spend_by_provider(seeded, PERIOD)["activity_coverage"]["github_connected"]


def test_developer_activity_joins_spend_by_github_handle(seeded):
    activity = dashboard.spend_by_provider(seeded, PERIOD)["developer_activity"]
    by_handle = {a["handle"]: a for a in activity}

    # Ranked by PRs merged, and each row carries the same window's tooling spend
    # so cost per PR is a like-for-like number.
    assert [a["handle"] for a in activity] == sorted(by_handle, key=lambda h: -by_handle[h]["prs"])
    alice = by_handle["alice"]
    assert alice["label"] == "Alice (alice)"  # matched case-insensitively to build_cost
    assert alice["features"] == 2  # her PRs touched triage + report
    assert alice["build_cost"] > 0
    assert round(alice["cost_per_pr"], 2) == round(alice["build_cost"] / alice["prs"], 2)


def test_developer_activity_reports_missing_stats_as_unknown(seeded, app_env):
    # A PR discovered before migration 0035 has no line counts. Reporting 0 lines
    # would read as "wrote nothing", which is a different claim from "we don't know".
    app_env.execute(
        """
        INSERT INTO feature_signal
            (tenant_id, feature_id, signal_type, external_ref, confidence, source,
             actor, merged_at)
        SELECT %s, id, 'pr', 'acme/core#901', 'high', 'github', 'zoe', %s
        FROM feature WHERE tenant_id = %s ORDER BY created_at LIMIT 1
        """,
        (seeded, PERIOD, seeded),
    )
    app_env.commit()

    zoe = next(
        a
        for a in dashboard.spend_by_provider(seeded, PERIOD)["developer_activity"]
        if a["handle"] == "zoe"
    )
    assert zoe["prs"] == 1
    assert zoe["additions"] is None and zoe["commits"] is None
    # No build-cost row for zoe: no AI tooling spend, so no cost per PR to report.
    assert zoe["build_cost"] == 0.0
    assert zoe["cost_per_pr"] is None
    assert zoe["label"] == "zoe"


def test_demo_seed_fills_the_developer_activity_table(tenant_id, app_env):
    # Same guard as the By Customer tab: the demo must show a populated, varied
    # table, not an empty section.
    insert_sample_data(app_env, tenant_id, extended=True)
    app_env.commit()

    rows = dashboard.spend_by_provider(tenant_id, PERIOD)["developer_activity"]
    assert len(rows) >= 6
    # The table is worth reading: whoever merges the most PRs is NOT whoever
    # writes the most code, which is the whole caveat the section carries.
    assert max(rows, key=lambda r: r["prs"]) is not max(rows, key=lambda r: r["additions"])
    assert all(r["commits"] and r["additions"] for r in rows)


def test_category_precedence_user_tag_beats_the_guess():
    # No billing signal says "this is a UI feature", so there are only two
    # sources — and a person's tag is the stronger of them.
    assert dashboard.resolve_category("ui", "user") == ("ui", "user")
    assert dashboard.resolve_category("api", "discovery") == ("api", "discovery")
    # Untagged stays untagged: no default is assigned on the user's behalf.
    assert dashboard.resolve_category(None, None) == (None, None)


def test_dashboard_rows_carry_the_category(seeded, app_env):
    # The fixture's features carry the tag a discovery run would have written.
    rows = {r["name"]: r for r in dashboard.dashboard(seeded, PERIOD)["features"]}
    assert rows["AI threat triage"]["category"] == "api"
    assert rows["AI threat triage"]["category_source"] == "discovery"
    assert rows["SOC copilot"]["category"] == "chat"

    fid = app_env.execute(
        """
        INSERT INTO feature (tenant_id, name, status, category, category_source)
        VALUES (%s, 'SSO login', 'confirmed', 'auth', 'discovery') RETURNING id
        """,
        (seeded,),
    ).fetchone()[0]
    app_env.execute(
        """
        INSERT INTO build_cost (tenant_id, feature_id, developer_id, tool, amount,
                                period, confidence, source)
        VALUES (%s, %s, 'dave', 'cursor', 74, %s, 'high', 'github+tool_admin')
        """,
        (seeded, fid, PERIOD),
    )
    app_env.commit()

    sso = next(
        r for r in dashboard.dashboard(seeded, PERIOD)["features"] if r["name"] == "SSO login"
    )
    assert sso["category"] == "auth"
    assert sso["category_source"] == "discovery"
    assert sso["build_cost"] == 74.0  # it still costs AI money to BUILD


def test_demo_seed_tags_features_across_several_categories(tenant_id, app_env):
    # A demo where every feature shares one category can't show what the column
    # is for — the point is that a product is a mix of surfaces.
    insert_sample_data(app_env, tenant_id, extended=True)
    app_env.commit()

    rows = dashboard.dashboard(tenant_id, PERIOD)["features"]
    tagged = {r["category"] for r in rows if r["category"]}
    assert len(tagged) >= 4
    assert "auth" in tagged and "reporting" in tagged


# ---------------------------------------------------------------------------
# Overview additions: the monthly trend, the provider list, open actions.
# ---------------------------------------------------------------------------
def test_the_trend_has_a_row_for_every_month_including_empty_ones(app_env, tenant_id):
    _daily(app_env, tenant_id, dt.date(2026, 3, 4), 30)
    _monthly(app_env, tenant_id, dt.date(2026, 3, 1), 30)
    _monthly(app_env, tenant_id, dt.date(2026, 5, 1), 70)
    app_env.commit()

    trend = dashboard.dashboard(tenant_id, range_token="last_3_months")["trend"]
    # April had no spend; the chart must show a gap, not close it up.
    assert [t["period"] for t in trend] == ["2026-03-01", "2026-04-01", "2026-05-01"]
    assert [t["inference_cost"] for t in trend] == [30.0, 0.0, 70.0]


def test_the_trend_keeps_build_and_inference_apart(app_env, tenant_id):
    _monthly(app_env, tenant_id, dt.date(2026, 5, 1), 70)
    app_env.execute(
        "INSERT INTO build_cost (tenant_id, tool, amount, period, confidence) "
        "VALUES (%s, 'claude_code', 25, %s, 'high')",
        (tenant_id, dt.date(2026, 5, 1)),
    )
    app_env.commit()

    [month] = dashboard.dashboard(tenant_id)["trend"]
    assert month["build_cost"] == 25.0
    assert month["inference_cost"] == 70.0
    assert "total" not in month  # never blended into one number


def test_provider_spend_ranks_vendors_and_keeps_each_ones_split(app_env, tenant_id):
    _monthly(app_env, tenant_id, dt.date(2026, 5, 1), 70)  # anthropic
    app_env.execute(
        "INSERT INTO inference_cost (tenant_id, provider, model, amount, period, source, "
        "confidence) VALUES (%s, 'openai', 'gpt', 20, %s, 'cost_api', 'high')",
        (tenant_id, dt.date(2026, 5, 1)),
    )
    app_env.execute(
        "INSERT INTO build_cost (tenant_id, tool, amount, period, confidence) "
        "VALUES (%s, 'copilot', 10, %s, 'high')",
        (tenant_id, dt.date(2026, 5, 1)),
    )
    app_env.commit()

    providers = dashboard.dashboard(tenant_id)["providers"]
    assert [p["provider"] for p in providers] == ["anthropic", "openai", "copilot"]
    assert providers[0]["amount"] == 70.0
    assert providers[0]["share"] == pytest.approx(70 / 100 * 100)
    # A vendor's own split survives, even though the list ranks on the sum.
    assert providers[2] == pytest.approx(
        {
            "provider": "copilot",
            "build_cost": 10.0,
            "inference_cost": 0.0,
            "amount": 10.0,
            "share": 10.0,
        },
        abs=0.001,
    )


def test_provider_shares_add_up(app_env, tenant_id):
    _monthly(app_env, tenant_id, dt.date(2026, 5, 1), 70)
    app_env.execute(
        "INSERT INTO build_cost (tenant_id, tool, amount, period, confidence) "
        "VALUES (%s, 'copilot', 30, %s, 'high')",
        (tenant_id, dt.date(2026, 5, 1)),
    )
    app_env.commit()
    providers = dashboard.dashboard(tenant_id)["providers"]
    assert sum(p["share"] for p in providers) == pytest.approx(100.0)


def test_open_actions_appear_only_when_there_is_something_to_do(app_env, tenant_id):
    # Everything attributed and classified: no actions, which is a real answer.
    feature = app_env.execute(
        "INSERT INTO feature (tenant_id, name, status) VALUES (%s, 'Chat', 'confirmed') "
        "RETURNING id",
        (tenant_id,),
    ).fetchone()[0]
    app_env.execute(
        "INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount, period, "
        "source, confidence, environment) VALUES (%s, %s, 'anthropic', 'c', 100, %s, 'cost_api', "
        "'high', 'production')",
        (tenant_id, feature, dt.date(2026, 5, 1)),
    )
    app_env.execute(
        "INSERT INTO inference_cost_daily (tenant_id, feature_id, provider, model, amount, day, "
        "source, confidence, environment) VALUES (%s, %s, 'anthropic', 'c', 100, %s, 'cost_api', "
        "'high', 'production')",
        (tenant_id, feature, dt.date(2026, 5, 4)),
    )
    app_env.commit()
    assert dashboard.dashboard(tenant_id)["actions"] == []


def test_unattributed_spend_becomes_an_action_that_says_where_to_go(app_env, tenant_id):
    _daily(app_env, tenant_id, dt.date(2026, 5, 4), 100)
    _monthly(app_env, tenant_id, dt.date(2026, 5, 1), 100)  # no feature_id
    app_env.commit()

    actions = dashboard.dashboard(tenant_id)["actions"]
    unattributed = [a for a in actions if a["kind"] == "unattributed"]
    assert len(unattributed) == 1
    assert "$100" in unattributed[0]["title"]
    assert unattributed[0]["href"] == "/cost-sources"


def test_the_trend_carries_tokens_alongside_cost(app_env, tenant_id):
    app_env.execute(
        """
        INSERT INTO inference_cost (tenant_id, provider, model, amount, period, source,
                                    confidence, tokens_in, cached_tokens_in, tokens_out)
        VALUES (%s, 'anthropic', 'c', 70, %s, 'cost_api', 'high', 1000, 250, 400)
        """,
        (tenant_id, dt.date(2026, 5, 1)),
    )
    app_env.commit()

    [month] = dashboard.dashboard(tenant_id)["trend"]
    assert month["tokens_in"] == 1000
    assert month["tokens_out"] == 400
    # Cached input is a SUBSET of tokens_in, so the rate is out of input alone.
    assert month["cached_tokens_in"] == 250
    assert month["cache_rate"] == 25.0


def test_a_month_with_no_tokens_reports_no_cache_rate_rather_than_dividing_by_zero(
    app_env, tenant_id
):
    _monthly(app_env, tenant_id, dt.date(2026, 5, 1), 70)  # cost, no token counts
    app_env.commit()
    [month] = dashboard.dashboard(tenant_id)["trend"]
    assert month["tokens_in"] == 0
    assert month["cache_rate"] == 0.0
