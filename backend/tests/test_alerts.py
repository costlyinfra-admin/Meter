"""Alerting: validation, metrics/conditions, state machine, idempotency, delivery."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from meter import alerts, alerts_eval, auth, budgets, notify
from meter.db import app_dsn, connect, tenant_tx

MAY = dt.date(2026, 5, 1)
APR = dt.date(2026, 4, 1)
# A moment inside May (UTC) so the evaluator's "current month" is May.
NOW = dt.datetime(2026, 5, 15, 12, 0, tzinfo=dt.timezone.utc)


def _add_inference(
    tenant_id,
    amount,
    period=MAY,
    *,
    provider="anthropic",
    model="m",
    environment=None,
    feature_id=None,
    tokens_in=0,
    tokens_out=0,
):
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute(
            """
            INSERT INTO inference_cost (tenant_id, feature_id, provider, model, amount, currency,
                period, tokens_in, tokens_out, environment, source, confidence)
            VALUES (%s, %s, %s, %s, %s, 'USD', %s, %s, %s, %s, 'cost_api', 'high')
            """,
            (
                tenant_id,
                feature_id,
                provider,
                model,
                Decimal(str(amount)),
                period,
                tokens_in,
                tokens_out,
                environment,
            ),
        )


def _set_budget(tenant_id, amount, cadence, effective_from="2020-01-01"):
    """Give the org a real budget the way Settings -> Budgets would."""
    return budgets.set_budget(
        tenant_id,
        {"amount": amount, "cadence": cadence, "effective_from": effective_from},
    )


def _rule(**over):
    base = {
        "name": "Test alert",
        "metric": "inference_cost",
        "scope_type": "organization",
        "condition_type": "exceeds",
        "threshold": 100,
        "window": "daily",
        "cooldown": "none",
        "channels": [{"channel": "in_app"}],
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    # External channels must never hit the network in tests.
    monkeypatch.setattr(notify, "_sleep", lambda *_: None)
    monkeypatch.setattr(
        notify, "_http_post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no net"))
    )


# ---- Validation -----------------------------------------------------------
def test_create_and_validation(tenant_id):
    rule = alerts.create_rule(tenant_id, _rule(), created_by="cto@acme.com")
    assert rule["status"] == "insufficient_data"
    assert rule["created_by"] == "cto@acme.com"
    assert rule["channels"][0]["channel"] == "in_app"

    with pytest.raises(alerts.AlertError):
        alerts.create_rule(tenant_id, _rule(name="  "))  # empty name
    with pytest.raises(alerts.AlertError):
        alerts.create_rule(tenant_id, _rule(threshold=-5))  # negative
    with pytest.raises(alerts.AlertError):
        alerts.create_rule(
            tenant_id, _rule(metric="build_cost", scope_type="provider", scope_ref="x")
        )  # invalid scope for metric
    with pytest.raises(alerts.AlertError):
        alerts.create_rule(tenant_id, _rule(scope_type="feature"))  # missing scope_ref
    with pytest.raises(alerts.AlertError):
        alerts.create_rule(tenant_id, _rule(channels=[]))  # no channels


def test_secrets_are_masked_never_returned(tenant_id):
    rule = alerts.create_rule(
        tenant_id,
        _rule(
            channels=[{"channel": "slack", "target": "https://hooks.slack.com/services/T/B/xyz"}]
        ),
    )
    ch = rule["channels"][0]
    assert ch["channel"] == "slack"
    assert "xyz" not in str(ch)  # the secret URL is never exposed
    assert ch["label"].startswith("Slack")
    # The evaluator-only accessor can decrypt it, but that never crosses the API.
    secret = alerts.get_destination_secrets(tenant_id, rule["id"])[0]
    assert secret["target"] == "https://hooks.slack.com/services/T/B/xyz"


# ---- Conditions -----------------------------------------------------------
def test_fixed_threshold_triggers_and_resolves(tenant_id):
    rule = alerts.create_rule(tenant_id, _rule(threshold=100))
    _add_inference(tenant_id, 150)  # over threshold
    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=NOW)
    assert alerts.get_rule(tenant_id, rule["id"])["status"] == "triggered"
    events = alerts.rule_events(tenant_id, rule["id"])["events"]
    assert [e["event_type"] for e in events] == ["triggered"]

    # Re-evaluate while still breached -> no duplicate triggered event.
    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=NOW)
    assert [e["event_type"] for e in alerts.rule_events(tenant_id, rule["id"])["events"]] == [
        "triggered"
    ]

    # Cost drops -> resolves with exactly one resolved event.
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute("UPDATE inference_cost SET amount = 10 WHERE tenant_id = %s", (tenant_id,))
    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=NOW)
    assert alerts.get_rule(tenant_id, rule["id"])["status"] == "healthy"
    types = [e["event_type"] for e in alerts.rule_events(tenant_id, rule["id"])["events"]]
    assert types.count("resolved") == 1


def test_percentage_increase_condition(tenant_id):
    rule = alerts.create_rule(
        tenant_id, _rule(condition_type="increase_pct", threshold=25, window="monthly")
    )
    _add_inference(tenant_id, 100, period=APR)  # previous
    _add_inference(tenant_id, 140, period=MAY)  # +40% > 25%
    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=NOW)
    assert alerts.get_rule(tenant_id, rule["id"])["status"] == "triggered"


def test_budget_percentage_condition(tenant_id):
    # The denominator is the organization's own budget, not a number carried on
    # the rule. A $1,000/month budget over a whole month prorates to $1,000.
    _set_budget(tenant_id, 1000, "monthly")
    rule = alerts.create_rule(
        tenant_id, _rule(condition_type="budget_pct", threshold=80, window="monthly")
    )
    _add_inference(tenant_id, 850)  # 85% of 1000 > 80%
    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=NOW)
    assert alerts.get_rule(tenant_id, rule["id"])["status"] == "triggered"


def test_a_budget_rule_cannot_be_saved_without_a_budget(tenant_id):
    with pytest.raises(alerts.AlertError) as exc:
        alerts.create_rule(
            tenant_id, _rule(condition_type="budget_pct", threshold=80, window="monthly")
        )
    # The message has to be actionable: it names where to go and what to do.
    assert "Settings" in str(exc.value) and "Budgets" in str(exc.value)
    assert alerts.list_rules(tenant_id) == []


def test_removing_the_budget_leaves_an_existing_rule_unable_to_evaluate(tenant_id):
    _set_budget(tenant_id, 1000, "monthly")
    rule = alerts.create_rule(
        tenant_id, _rule(condition_type="budget_pct", threshold=80, window="monthly")
    )
    _add_inference(tenant_id, 850)
    budgets.remove_budget(tenant_id)

    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=NOW)
    # No denominator means no answer -- never a default budget, and never a
    # trigger inferred from one.
    assert alerts.get_rule(tenant_id, rule["id"])["status"] == "insufficient_data"


def test_an_annual_budget_is_prorated_to_the_month_being_evaluated(tenant_id):
    # $12,410/year over a 365-day year is $1,054 for May's 31 days. $850 is 80.6%
    # of that -- just over an 80% threshold, and well under a naive $12,410.
    _set_budget(tenant_id, 12_410, "annual")
    rule = alerts.create_rule(
        tenant_id, _rule(condition_type="budget_pct", threshold=80, window="monthly")
    )
    _add_inference(tenant_id, 850)
    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=NOW)
    assert alerts.get_rule(tenant_id, rule["id"])["status"] == "triggered"


def test_a_budget_that_starts_after_the_month_does_not_arm_the_rule(tenant_id):
    # Effective in June; May has no budget to measure against.
    _set_budget(tenant_id, 1000, "monthly", effective_from="2026-06-01")
    rule = alerts.create_rule(
        tenant_id, _rule(condition_type="budget_pct", threshold=80, window="monthly")
    )
    _add_inference(tenant_id, 850)
    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=NOW)
    assert alerts.get_rule(tenant_id, rule["id"])["status"] == "insufficient_data"


def test_ignore_is_excluded_from_metric(tenant_id):
    rule = alerts.create_rule(tenant_id, _rule(threshold=100))
    _add_inference(tenant_id, 90, environment="unclassified")
    _add_inference(tenant_id, 500, environment="ignore")  # excluded
    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=NOW)
    # Active total is 90 (< 100) -> healthy, ignored $500 doesn't trigger.
    assert alerts.get_rule(tenant_id, rule["id"])["status"] == "healthy"


# ---- Insufficient data ----------------------------------------------------
def test_no_data_is_insufficient_not_triggered(tenant_id):
    rule = alerts.create_rule(tenant_id, _rule(threshold=1))
    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=NOW)
    r = alerts.get_rule(tenant_id, rule["id"])
    assert r["status"] == "insufficient_data"
    assert alerts.rule_events(tenant_id, rule["id"])["events"] == []  # no threshold event


def test_increase_pct_without_prior_is_insufficient(tenant_id):
    rule = alerts.create_rule(
        tenant_id, _rule(condition_type="increase_pct", threshold=10, window="monthly")
    )
    _add_inference(tenant_id, 100, period=MAY)  # no April baseline
    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=NOW)
    assert alerts.get_rule(tenant_id, rule["id"])["status"] == "insufficient_data"


# ---- Cooldown + idempotency ----------------------------------------------
def test_cooldown_suppresses_repeat_notifications(tenant_id):
    # recovery_notify off so we count only trigger notifications here.
    rule = alerts.create_rule(
        tenant_id, _rule(threshold=100, cooldown="day", recovery_notify=False)
    )
    _add_inference(tenant_id, 150)
    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=NOW)
    # Resolve then re-breach within the cooldown window.
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute("UPDATE inference_cost SET amount = 10 WHERE tenant_id = %s", (tenant_id,))
    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=NOW + dt.timedelta(hours=1))
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute("UPDATE inference_cost SET amount = 200 WHERE tenant_id = %s", (tenant_id,))
    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=NOW + dt.timedelta(hours=2))
    # Two triggered events (two incidents) but in-app notifications gated by cooldown:
    notifs = alerts.rule_events(tenant_id, rule["id"])["notifications"]
    # First trigger notified; the second (within 1 day cooldown) did not.
    assert sum(1 for n in notifs if n["channel"] == "in_app") == 1


# ---- Isolation ------------------------------------------------------------
def test_alerts_are_org_isolated(tenant_id, monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "unit-test-secret-key")
    other = auth.signup("other@globex.com", "correct horse battery")["tenant_id"]
    alerts.create_rule(tenant_id, _rule(name="A-only"))
    assert [r["name"] for r in alerts.list_rules(other)] == []  # can't see tenant A's rule


# ---- Summary + activity ---------------------------------------------------
def test_summary_counts_and_unread_activity(tenant_id):
    rule = alerts.create_rule(tenant_id, _rule(threshold=100))
    _add_inference(tenant_id, 150)
    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=NOW)
    summary = alerts.summary_counts(tenant_id)
    assert summary["triggered"] == 1
    assert summary["unread"] == 1  # the triggered event is unread

    activity = alerts.list_activity(tenant_id)
    assert activity[0]["event_type"] == "triggered"
    alerts.mark_all_read(tenant_id)
    assert alerts.summary_counts(tenant_id)["unread"] == 0


def test_send_test_notification(tenant_id):
    rule = alerts.create_rule(tenant_id, _rule())
    result = alerts_eval.send_test(tenant_id, rule["id"])
    assert result["ok"] is True
    assert result["deliveries"][0]["channel"] == "in_app"
    assert result["deliveries"][0]["status"] == "sent"
    assert alerts.rule_events(tenant_id, rule["id"])["events"][0]["event_type"] == "test"


def test_duplicate_and_enable_disable(tenant_id):
    rule = alerts.create_rule(tenant_id, _rule(name="Original"))
    dup = alerts.duplicate_rule(tenant_id, rule["id"])
    assert dup["name"] == "Original (copy)"
    assert dup["enabled"] is False  # duplicates start disabled
    toggled = alerts.set_enabled(tenant_id, dup["id"], True)
    assert toggled["enabled"] is True


# ---- Delivery: SSRF + independent channel failure -------------------------
@pytest.mark.parametrize(
    "url",
    ["http://localhost/x", "http://127.0.0.1/x", "http://169.254.169.254/latest", "ftp://x/y"],
)
def test_ssrf_blocks_unsafe_urls(url):
    ok, _why = notify.is_safe_url(url)
    assert ok is False


def test_channel_failure_is_independent_and_visible(tenant_id, monkeypatch):
    # in_app succeeds; email is unconfigured -> a delivery_error event is recorded
    # but the in-app delivery still went through.
    rule = alerts.create_rule(
        tenant_id,
        _rule(
            threshold=100,
            channels=[{"channel": "in_app"}, {"channel": "email", "target": "cto@acme.com"}],
        ),
    )
    _add_inference(tenant_id, 150)
    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=NOW)
    hist = alerts.rule_events(tenant_id, rule["id"])
    statuses = {n["channel"]: n["status"] for n in hist["notifications"]}
    assert statuses["in_app"] == "sent"
    assert statuses["email"] == "unconfigured"  # honest, not a fake success
    assert any(e["event_type"] == "delivery_error" for e in hist["events"])
    # The underlying alert state is preserved despite the delivery problem.
    assert alerts.get_rule(tenant_id, rule["id"])["status"] == "triggered"


class _FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code


def _enable_resend(monkeypatch, capture=None):
    """Configure Resend and stub the HTTP seam; optionally capture the request."""
    monkeypatch.setattr(notify, "RESEND_API_KEY", "re_test_key")
    monkeypatch.setattr(notify, "ALERT_EMAIL_FROM", "alerts@acme.com")
    monkeypatch.setattr(notify, "EMAIL_ENABLED", True)

    def fake_post(url, payload, timeout=8.0, headers=None):
        if capture is not None:
            capture.append({"url": url, "payload": payload, "headers": headers})
        return _FakeResp(200)

    monkeypatch.setattr(notify, "_http_post", fake_post)


def test_email_sends_via_resend_when_configured(tenant_id, monkeypatch):
    calls: list[dict] = []
    _enable_resend(monkeypatch, calls)
    rule = alerts.create_rule(
        tenant_id,
        _rule(threshold=100, channels=[{"channel": "email", "target": "cto@acme.com"}]),
    )
    _add_inference(tenant_id, 150)
    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=NOW)
    hist = alerts.rule_events(tenant_id, rule["id"])
    statuses = {n["channel"]: n["status"] for n in hist["notifications"]}
    assert statuses["email"] == "sent"  # real send, not "unconfigured"
    # Posted to Resend with the recipient, sender, and a bearer token in the header.
    assert calls and calls[0]["url"] == "https://api.resend.com/emails"
    assert calls[0]["payload"]["to"] == ["cto@acme.com"]
    assert calls[0]["payload"]["from"] == "alerts@acme.com"
    assert calls[0]["headers"]["Authorization"] == "Bearer re_test_key"
    # The secret must never be persisted on the notification row.
    assert all("re_test_key" not in (n.get("error") or "") for n in hist["notifications"])


def test_email_rejected_by_resend_is_a_permanent_failure(tenant_id, monkeypatch):
    _enable_resend(monkeypatch)
    # A 401 (bad key / unverified domain) is a 4xx -> permanent, no retry, no fake success.
    monkeypatch.setattr(notify, "_http_post", lambda *a, **k: _FakeResp(401))
    rule = alerts.create_rule(
        tenant_id,
        _rule(threshold=100, channels=[{"channel": "email", "target": "cto@acme.com"}]),
    )
    _add_inference(tenant_id, 150)
    alerts_eval.evaluate_rule(tenant_id, rule["id"], now=NOW)
    hist = alerts.rule_events(tenant_id, rule["id"])
    statuses = {n["channel"]: n["status"] for n in hist["notifications"]}
    assert statuses["email"] == "failed"
    assert any(e["event_type"] == "delivery_error" for e in hist["events"])
    # Alert state itself is unaffected by the delivery failure.
    assert alerts.get_rule(tenant_id, rule["id"])["status"] == "triggered"


def test_deep_link_path_matches_scope():
    assert alerts_eval._deep_link_path("feature", "abc-123") == "/features/abc-123"
    assert alerts_eval._deep_link_path("provider", "anthropic") == "/cost-sources"
    assert alerts_eval._deep_link_path("model", "claude") == "/cost-sources"
    assert alerts_eval._deep_link_path("organization", None) == "/"
