"""The read-only MCP server: protocol, tenant isolation, and reuse.

Two properties matter more than the rest and are asserted directly. The tools
must return the SAME numbers the app's own read services return — anything else
means a second analytics implementation has grown inside MCP. And a tool must
never see another tenant's data, which is enforced by RLS underneath but is
worth proving at this layer, because this is the layer a credential reaches.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import subprocess
import sys

import pytest
from meter import dashboard, optimize_measured
from meter.mcp import server, tools
from meter.sampledata import insert_sample_data

PERIOD = dt.date(2026, 5, 1)  # sampledata's period
MONTH = "2026-05"


@pytest.fixture
def seeded(tenant_id, app_env):
    """One fully populated tenant: cost, traces, and optimize-mode signals."""
    insert_sample_data(app_env, tenant_id, extended=True)
    app_env.commit()
    return tenant_id


@pytest.fixture
def other_tenant(app_env):
    """A second, differently-populated tenant sharing the same database."""
    row = app_env.execute(
        "INSERT INTO tenant (name) VALUES ('Other Tenant') RETURNING id"
    ).fetchone()
    other = str(row[0])
    insert_sample_data(app_env, other)
    app_env.commit()
    return other


def _call(tenant_id: str, name: str, arguments: dict) -> dict:
    """Drive a tool the way the transport does, and unwrap the JSON it returns."""
    response = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}},
        tenant_id,
    )
    result = response["result"]
    return {"payload": json.loads(result["content"][0]["text"]), "is_error": result["isError"]}


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------
def test_initialize_declares_tools_and_agrees_on_a_version():
    reply = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-03-26"}},
        "tenant",
    )["result"]
    assert reply["protocolVersion"] == "2025-03-26"  # the client's, since we speak it
    assert reply["capabilities"]["tools"] is not None
    assert reply["serverInfo"]["name"] == "meter"


def test_initialize_falls_back_when_the_client_speaks_something_else():
    reply = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "1999-01-01"}},
        "tenant",
    )["result"]
    assert reply["protocolVersion"] == server.DEFAULT_PROTOCOL


def test_lists_exactly_the_three_read_tools():
    listed = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "t")["result"]
    assert [t["name"] for t in listed["tools"]] == [
        "get_cost_summary",
        "find_optimization_opportunities",
        "get_optimization_details",
    ]
    for tool in listed["tools"]:
        # A client validates against these, so they must be well-formed objects.
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"]
        assert json.dumps(tool)  # serializable, with no stray Python objects


def test_a_notification_gets_no_reply():
    # Replying to a notification is a protocol violation, and some clients hang.
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, "t") is None


def test_an_unknown_method_is_a_jsonrpc_error():
    reply = server.handle({"jsonrpc": "2.0", "id": 7, "method": "resources/list"}, "t")
    assert reply["error"]["code"] == server.METHOD_NOT_FOUND


def test_unparseable_input_does_not_kill_the_server():
    out = io.StringIO()
    server.serve(
        "t",
        io.StringIO('not json\n{"jsonrpc":"2.0","id":2,"method":"ping"}\n'),
        out,
    )
    first, second = [json.loads(line) for line in out.getvalue().splitlines()]
    assert first["error"]["code"] == server.PARSE_ERROR
    assert second["result"] == {}  # ...and the next message is still served


# ---------------------------------------------------------------------------
# get_cost_summary — the same numbers the app shows
# ---------------------------------------------------------------------------
def test_cost_summary_matches_the_dashboard_service(seeded):
    got = _call(seeded, "get_cost_summary", {"start": MONTH})["payload"]
    expected = dashboard.dashboard(seeded, PERIOD)

    assert got["totals"] == expected["totals"]
    assert got["unattributed"] == expected["unattributed"]
    assert got["feature_count"] == len(expected["features"])
    assert got["window"] == {"start": "2026-05-01", "end": "2026-05-01"}


def test_cost_summary_keeps_build_and_inference_apart(seeded):
    got = _call(seeded, "get_cost_summary", {"start": MONTH})["payload"]
    triage = next(f for f in got["features"] if f["name"] == "AI threat triage")
    expected = next(
        f for f in dashboard.dashboard(seeded, PERIOD)["features"] if f["name"] == triage["name"]
    )
    assert triage["build_cost"] == expected["build_cost"] > 0
    assert triage["inference_cost"] == expected["inference_cost"] > 0
    assert triage["build_cost"] != triage["inference_cost"]
    # No blended field anywhere — the invariant the whole product rests on.
    assert not any("total_cost" in f for f in got["features"])
    assert "never" in got["note"]


#: The list key each slice returns its rows under.
_ROWS = {
    "feature": "features",
    "provider": "providers",
    "model": "models",
    "workspace": "workspaces",
    "product": "products",
    "customer": "customers",
    "application": "applications",
}


def test_cost_summary_slices_by_every_dimension_meter_has(seeded):
    assert set(_ROWS) == set(tools.GROUPINGS), "a new dimension needs a row key here"
    for grouping in tools.GROUPINGS:
        got = _call(seeded, "get_cost_summary", {"start": MONTH, "group_by": grouping})
        assert not got["is_error"], grouping
        payload = got["payload"]
        assert payload["group_by"] == grouping
        # Not vacuous: the seeded tenant has data on every one of these, so an
        # empty slice means the projection is reading the wrong key.
        assert payload[_ROWS[grouping]], grouping


def test_provider_slice_matches_the_provider_service(seeded):
    got = _call(seeded, "get_cost_summary", {"start": MONTH, "group_by": "provider"})["payload"]
    expected = dashboard.spend_by_provider(seeded, PERIOD)
    assert got["inference_total"] == expected["total"]
    assert [p["provider"] for p in got["providers"]] == [
        p["provider"] for p in expected["by_provider"]
    ]


def test_one_feature_detail_carries_its_model_split(seeded):
    listed = _call(seeded, "get_cost_summary", {"start": MONTH})["payload"]
    feature_id = listed["features"][0]["feature_id"]
    got = _call(seeded, "get_cost_summary", {"start": MONTH, "feature_id": feature_id})["payload"]
    assert got["feature"]["feature_id"] == feature_id
    assert got["feature"]["by_model"]


def test_limit_is_capped_rather_than_trusted(seeded):
    got = _call(seeded, "get_cost_summary", {"start": MONTH, "limit": 10_000})["payload"]
    assert len(got["features"]) <= tools.MAX_LIMIT


def test_a_bad_argument_comes_back_as_a_readable_tool_error(seeded):
    got = _call(seeded, "get_cost_summary", {"group_by": "galaxy"})
    assert got["is_error"]
    assert "group_by" in got["payload"]["error"]

    got = _call(seeded, "get_cost_summary", {"start": "May 2026"})
    assert got["is_error"]
    assert "2026-06" in got["payload"]["error"]  # the message shows the format


def test_an_unknown_feature_is_refused_not_invented(seeded):
    got = _call(
        seeded,
        "get_cost_summary",
        {"start": MONTH, "feature_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert got["is_error"]
    assert "No feature" in got["payload"]["error"]


# ---------------------------------------------------------------------------
# find_optimization_opportunities
# ---------------------------------------------------------------------------
def test_opportunities_come_from_the_optimization_engine(seeded):
    got = _call(seeded, "find_optimization_opportunities", {"period": MONTH})["payload"]
    expected = optimize_measured.copilot_overview(seeded, PERIOD)

    assert got["totals_by_savings_type"] == expected["totals"]
    assert got["period"] == expected["period"]
    assert [o["lever"] for o in got["opportunities"]] == [
        o["lever"] for o in expected["top_recommendations"]
    ]


def test_each_opportunity_carries_what_an_agent_needs_to_act(seeded):
    got = _call(seeded, "find_optimization_opportunities", {"period": MONTH})["payload"]
    assert got["opportunities"], "the seeded tenant has measured findings"
    for opp in got["opportunities"]:
        assert opp["savings_type"] in tools.SAVINGS_TYPES
        assert opp["confidence"] in ("high", "med", "low")
        assert opp["engineering_effort"]
        assert opp["evidence"]
        assert opp["projected_monthly_savings"] >= 0
        # The three totals are never collapsed into one "savings" figure.
        assert "savings" not in opp


def test_the_three_savings_classes_stay_separate(seeded):
    got = _call(seeded, "find_optimization_opportunities", {"period": MONTH})["payload"]
    assert set(got["totals_by_savings_type"]) == set(tools.SAVINGS_TYPES)


def test_filters_narrow_the_list_without_changing_the_totals(seeded):
    everything = _call(seeded, "find_optimization_opportunities", {"period": MONTH})["payload"]
    biggest = max(o["projected_monthly_savings"] for o in everything["opportunities"])

    filtered = _call(
        seeded,
        "find_optimization_opportunities",
        {"period": MONTH, "min_monthly_savings": biggest},
    )["payload"]
    assert len(filtered["opportunities"]) < len(everything["opportunities"])
    assert all(o["projected_monthly_savings"] >= biggest for o in filtered["opportunities"])
    # Totals describe the organization, not the filtered page.
    assert filtered["totals_by_savings_type"] == everything["totals_by_savings_type"]

    ceiling = _call(
        seeded,
        "find_optimization_opportunities",
        {"period": MONTH, "savings_type": "modeled_ceiling"},
    )["payload"]
    assert all(o["savings_type"] == "modeled_ceiling" for o in ceiling["opportunities"])


def test_it_says_whether_the_telemetry_behind_the_levers_exists(seeded):
    got = _call(seeded, "find_optimization_opportunities", {"period": MONTH})["payload"]
    assert got["telemetry"]["has_optimize_signals"] is True


# ---------------------------------------------------------------------------
# get_optimization_details
# ---------------------------------------------------------------------------
@pytest.fixture
def an_opportunity(seeded):
    found = _call(seeded, "find_optimization_opportunities", {"period": MONTH})["payload"]
    return next(o for o in found["opportunities"] if o["lever"] == "duplicate_calls")


def test_details_round_trip_the_id_the_list_handed_out(seeded, an_opportunity):
    got = _call(
        seeded, "get_optimization_details", {"opportunity_id": an_opportunity["opportunity_id"]}
    )["payload"]
    assert got["opportunity_id"] == an_opportunity["opportunity_id"]
    assert got["opportunity"]["lever"] == "duplicate_calls"
    assert got["feature"]["name"]


def test_details_carry_the_evidence_and_how_to_check_it(seeded, an_opportunity):
    got = _call(
        seeded, "get_optimization_details", {"opportunity_id": an_opportunity["opportunity_id"]}
    )["payload"]
    opp = got["opportunity"]
    assert opp["evidence"] and opp["fix"]
    assert opp["validation_guidance"] and opp["verification"]
    # The request shapes behind it, listed once, on the opportunity itself.
    assert opp["trail"], "the repeated-request finding lists its request shapes"
    assert "evidence_trail" not in got, "one copy of the trail, not two"
    for entry in opp["trail"]:
        assert entry["call_count"] >= 1
        assert entry["model"]


def test_details_point_at_the_code_without_exposing_content(seeded, an_opportunity):
    got = _call(
        seeded,
        "get_optimization_details",
        {"opportunity_id": an_opportunity["opportunity_id"], "trace_limit": 5},
    )["payload"]
    examples = got["example_traces"]
    # Not vacuous: there must BE runs to inspect, or the tool has not done the
    # one thing that connects a finding to a line of code.
    assert examples["traces"], "an opportunity must come with runs to go and read"
    assert len(examples["traces"]) <= 5
    for trace in examples["traces"]:
        # operation_name is the step name from the call site: the bridge to code.
        assert trace["operation_name"]
        # Nothing that could be content, and not who the run was for.
        assert "customer_ref" not in trace
        assert not any("prompt" in key or "message" in key for key in trace)
    # No field anywhere in the response is a place content could hide. Checked
    # on KEYS, not on the text: the guidance prose legitimately says "response",
    # and a substring scan would either fail on that or be meaningless.
    assert not _content_shaped_keys(got)


_CONTENT_WORDS = ("prompt_text", "message", "content", "body", "arguments", "result_text",
                  "document", "stack", "traceback", "exception", "token_value", "secret")


def _content_shaped_keys(node, path="") -> list:
    """Every key in the payload that could plausibly hold prompt or tool content."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            where = f"{path}.{key}"
            if any(word in key.lower() for word in _CONTENT_WORDS):
                found.append(where)
            found += _content_shaped_keys(value, where)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            found += _content_shaped_keys(item, f"{path}[{i}]")
    return found


def test_runs_from_another_window_are_labelled_as_such(seeded, an_opportunity):
    # The seeded traces are recent; the finding is for May 2026. Borrowing them
    # is useful, and passing them off as May's evidence would not be.
    got = _call(
        seeded, "get_optimization_details", {"opportunity_id": an_opportunity["opportunity_id"]}
    )["payload"]
    examples = got["example_traces"]
    assert examples["basis"] in ("period", "recent")
    if examples["basis"] == "recent":
        assert "not evidence for this month" in examples["note"]
    else:
        assert examples["period"] == got["period"]


def test_a_malformed_id_explains_the_format(seeded):
    got = _call(seeded, "get_optimization_details", {"opportunity_id": "nonsense"})
    assert got["is_error"]
    assert "<feature_id>:<lever>:<YYYY-MM-DD>" in got["payload"]["error"]


def test_a_lever_that_did_not_fire_is_reported_as_absent(seeded, an_opportunity):
    feature_id, _lever, period = tools.parse_opportunity_id(an_opportunity["opportunity_id"])
    got = _call(
        seeded,
        "get_optimization_details",
        {"opportunity_id": tools.opportunity_id(feature_id, "provider_switch", str(period))},
    )
    assert got["is_error"]
    assert "provider_switch" in got["payload"]["error"]


# ---------------------------------------------------------------------------
# Isolation and read-only
# ---------------------------------------------------------------------------
def test_a_tenant_sees_only_its_own_numbers(seeded, other_tenant):
    mine = _call(seeded, "get_cost_summary", {"start": MONTH})["payload"]
    theirs = _call(other_tenant, "get_cost_summary", {"start": MONTH})["payload"]

    assert mine["totals"] != theirs["totals"]
    # The extended seed adds features the plain one does not have.
    assert {f["name"] for f in mine["features"]} - {f["name"] for f in theirs["features"]}
    assert theirs["totals"] == dashboard.dashboard(other_tenant, PERIOD)["totals"]


def test_another_tenants_feature_id_is_not_readable(seeded, other_tenant):
    theirs = _call(other_tenant, "get_cost_summary", {"start": MONTH})["payload"]
    stolen = theirs["features"][0]["feature_id"]

    got = _call(seeded, "get_cost_summary", {"start": MONTH, "feature_id": stolen})
    assert got["is_error"]
    assert "No feature" in got["payload"]["error"]


def test_another_tenants_opportunity_id_is_not_readable(seeded, other_tenant):
    found = _call(seeded, "find_optimization_opportunities", {"period": MONTH})["payload"]
    borrowed = found["opportunities"][0]
    feature_id, lever, period = tools.parse_opportunity_id(borrowed["opportunity_id"])

    got = _call(
        other_tenant,
        "get_optimization_details",
        {"opportunity_id": tools.opportunity_id(feature_id, lever, str(period))},
    )
    assert got["is_error"]


def test_the_server_offers_nothing_that_writes():
    # The whole surface, so a fourth tool cannot arrive without this test failing.
    assert set(tools._HANDLERS) == {t["name"] for t in tools.TOOLS}
    assert len(tools.TOOLS) == 3
    for name in tools._HANDLERS:
        assert name.startswith(("get_", "find_")), f"{name} does not read"


def test_an_unexpected_failure_reports_nothing_internal(seeded, monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("postgresql://meter_app:hunter2@db.internal/meter")

    monkeypatch.setattr(tools.dashboard, "resolve_window", explode)
    got = _call(seeded, "get_cost_summary", {"start": MONTH})
    assert got["is_error"]
    assert "hunter2" not in json.dumps(got["payload"])
    assert "See the server log" in got["payload"]["error"]


# ---------------------------------------------------------------------------
# The process itself — framing, startup, and the credential path
# ---------------------------------------------------------------------------
def _run_server(env: dict, *messages: dict) -> tuple:
    """Spawn `python -m meter.mcp`, feed it messages, return (replies, stderr, code)."""
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "meter.mcp"],
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=120,
    )
    replies = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return replies, proc.stderr, proc.returncode


@pytest.fixture
def server_env(app_env, admin_conninfo, app_conninfo):
    """A real login whose organization has data, plus the DB wiring for a subprocess."""
    from meter import auth

    user = auth.signup("mcp-user@example.com", "correct-horse-battery")
    insert_sample_data(app_env, user["tenant_id"], extended=True)
    app_env.commit()
    return {
        "DATABASE_URL": admin_conninfo,
        "DATABASE_APP_URL": app_conninfo,
        "METER_EMAIL": "mcp-user@example.com",
        "METER_PASSWORD": "correct-horse-battery",
    }


def test_the_process_speaks_the_protocol_over_stdio(server_env):
    replies, stderr, code = _run_server(
        server_env,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "get_cost_summary", "arguments": {"start": MONTH}}},
    )
    assert code == 0
    # One reply per request and none for the notification — that is the framing.
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert replies[0]["result"]["serverInfo"]["name"] == "meter"
    assert len(replies[1]["result"]["tools"]) == 3

    payload = json.loads(replies[2]["result"]["content"][0]["text"])
    assert payload["totals"]["inference_cost"] > 0
    # Logging goes to stderr; stdout carried only protocol, or the parse above
    # would already have failed.
    assert "meter MCP server ready" in stderr


def test_the_password_never_reaches_the_output(server_env):
    replies, stderr, _code = _run_server(
        server_env, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    secret = server_env["METER_PASSWORD"]
    assert secret not in stderr
    assert secret not in json.dumps(replies)


def test_it_refuses_to_start_without_a_credential(server_env):
    without = {**server_env, "METER_PASSWORD": ""}
    replies, stderr, code = _run_server(without, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    # Exit, rather than start and fail every call — which looks like an outage.
    assert code == 2
    assert replies == []
    assert "METER_PASSWORD" in stderr


def test_it_refuses_to_start_with_the_wrong_password(server_env):
    wrong = {**server_env, "METER_PASSWORD": "not-the-password"}
    _replies, stderr, code = _run_server(wrong, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert code == 2
    assert "rejected" in stderr
    # No hint about which half was wrong: this message can reach a log.
    assert "mcp-user@example.com" not in stderr


def test_a_feature_id_returns_more_than_the_ranked_shortlist(seeded):
    shortlist = _call(seeded, "find_optimization_opportunities", {"period": MONTH})["payload"]
    assert shortlist["scope"] == "organization"
    busiest = shortlist["opportunities"][0]["feature_id"]

    one = _call(
        seeded,
        "find_optimization_opportunities",
        {"period": MONTH, "feature_id": busiest},
    )["payload"]
    assert one["scope"] == f"feature:{busiest}"
    assert {o["feature_id"] for o in one["opportunities"]} == {busiest}
    # The shortlist drops directional estimates and superseded findings; the
    # per-feature list is the whole thing, which is the point of the argument.
    expected = optimize_measured.opportunities(seeded, busiest, PERIOD, PERIOD)
    assert len(one["opportunities"]) == len(expected["opportunities"])
    assert one["totals_by_savings_type"] == expected["totals"]
    # Tenant roll-ups are absent rather than null: a per-feature view has no
    # verified-savings figure, and reporting one as 0 would claim it was checked.
    assert "verified_monthly_savings" not in one
    assert "telemetry" not in one


def test_a_superseded_finding_says_what_supersedes_it(seeded):
    # Two levers can price the same tokens. Meter marks the loser rather than
    # deleting it; an agent that added both would double-count the saving.
    found = _call(seeded, "find_optimization_opportunities", {"period": MONTH})["payload"]
    assert all("overlaps" in o for o in found["opportunities"])
    assert "never add the two" in found["note"]


def test_an_unknown_feature_id_is_refused_by_the_opportunity_tool(seeded):
    got = _call(
        seeded,
        "find_optimization_opportunities",
        {"period": MONTH, "feature_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert got["is_error"]
    assert "No feature" in got["payload"]["error"]
