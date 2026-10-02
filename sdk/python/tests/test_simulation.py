"""The content-free simulation counters behind "Test this" (EX-1).

A customer testing a response cache asks one question first: how old may a
reused answer be? These counters answer it for four limits at once, from the
same call timings optimize mode already sees, and send totals only — no
fingerprint, no request, no reply.
"""

from __future__ import annotations

from test_optimize import SECRET, Captured, client_for, drain, meter

from costlyinfra_meter import SIM_TTLS, _Optimizer, _Scope

REQUEST = {"model": "m", "messages": [{"content": "hi"}]}
SUFFIXES = [suffix for _ttl, suffix in SIM_TTLS]


def collector(clock, **kw):
    return _Optimizer(meter(Captured("pepper"), salt="pepper"), clock=lambda: clock["t"], **kw)


def simulation(events) -> list:
    return [e["signal"] for e in events if (e.get("signal") or {}).get("kind") == "simulation"]


def test_each_limit_counts_the_calls_a_cache_that_old_would_have_served():
    clock = {"t": 0.0}
    c = collector(clock)
    # t = 0, 30, 90, 3690
    for step in (0.0, 30.0, 60.0, 3600.0):
        clock["t"] += step
        c.on_call("anthropic", "m", REQUEST, {})

    (sim,) = simulation(c.due_summaries(force=True))
    assert sim["calls"] == 4
    # 1 minute: only t=30 is within a minute of the group that opened at 0;
    # t=90 opens a new group, and t=3690 another.
    assert sim["hits_1m"] == 1
    # 10 minutes and 1 hour: t=30 and t=90. t=3690 is past both.
    assert sim["hits_10m"] == 2
    assert sim["hits_1h"] == 2
    # 24 hours: all three repeats.
    assert sim["hits_24h"] == 3


def test_a_limit_runs_from_the_first_call_and_is_never_extended():
    """Steady traffic must not keep one cached answer alive past its limit."""
    clock = {"t": 0.0}
    c = collector(clock)
    c.on_call("anthropic", "m", REQUEST, {})
    for _ in range(3):
        clock["t"] += 40.0  # 40, 80, 120: each within a minute of the LAST call
        c.on_call("anthropic", "m", REQUEST, {})

    (sim,) = simulation(c.due_summaries(force=True))
    # t=40 hits. t=80 is 80s from the group's start: a miss that opens a new
    # group, which t=120 then hits.
    assert sim["hits_1m"] == 2


def test_the_ten_minute_limit_agrees_with_the_duplicate_detector():
    """The two must tell the same story about the same calls, or a customer who
    tests the detector's own setting would be told something different."""
    clock = {"t": 0.0}
    c = collector(clock, window=600.0)
    duplicates = 0
    for step in (0.0, 100.0, 450.0, 100.0, 5.0, 2000.0, 1.0):
        clock["t"] += step
        if c.on_call("anthropic", "m", REQUEST, {}) is not None:
            duplicates += 1

    (sim,) = simulation(c.due_summaries(force=True))
    assert sim["hits_10m"] == duplicates
    assert duplicates > 0


def test_only_the_calls_a_cache_would_have_served_are_priced_as_hits():
    clock = {"t": 0.0}
    c = collector(clock)
    c.on_call("anthropic", "m", REQUEST, {"tokens_in": 1000, "tokens_out": 300})
    clock["t"] += 10.0
    c.on_call("anthropic", "m", REQUEST, {"tokens_in": 1000, "tokens_out": 250})

    (sim,) = simulation(c.due_summaries(force=True))
    assert (sim["tokens_in"], sim["tokens_out"]) == (2000, 550)
    for suffix in SUFFIXES:
        assert sim[f"hit_tokens_in_{suffix}"] == 1000
        assert sim[f"hit_tokens_out_{suffix}"] == 250


def test_scoped_and_unscoped_calls_are_reported_apart():
    """A cache nobody scoped to a customer may serve one customer's answer to
    another. The server keeps the two apart, so the SDK has to."""
    clock = {"t": 0.0}
    c = collector(clock)
    scoped = _Scope(customer_id="acme")
    for _ in range(2):
        c.on_call("anthropic", "m", REQUEST, {}, scope=scoped)
        c.on_call("anthropic", "m", REQUEST, {})
        clock["t"] += 5.0

    sims = {s["scope_kind"]: s for s in simulation(c.due_summaries(force=True))}
    assert set(sims) == {"explicit", "unscoped"}
    assert sims["explicit"]["calls"] == 2 and sims["explicit"]["hits_1m"] == 1
    assert sims["unscoped"]["calls"] == 2 and sims["unscoped"]["hits_1m"] == 1


def test_a_request_shape_forgotten_early_is_counted_not_hidden():
    """The memory is bounded. Losing a shape whose group was still open loses
    its future hits, and the summary says so per limit, so the long limits are
    reported as lower bounds rather than quietly understated."""
    clock = {"t": 0.0}
    c = collector(clock, sim_capacity=2)
    c.on_call("anthropic", "m", {"model": "m", "messages": [{"content": "a"}]}, {})
    clock["t"] += 120.0
    c.on_call("anthropic", "m", {"model": "m", "messages": [{"content": "b"}]}, {})
    c.on_call("anthropic", "m", {"model": "m", "messages": [{"content": "c"}]}, {})  # evicts "a"

    (sim,) = simulation(c.due_summaries(force=True))
    # "a" opened 120s ago: its 1-minute group had already closed, so nothing
    # was lost there; at every longer limit it was still open.
    assert sim["evicted_1m"] == 0
    assert sim["evicted_10m"] == 1
    assert sim["evicted_1h"] == 1
    assert sim["evicted_24h"] == 1


def test_a_request_that_cannot_be_read_is_a_call_but_never_a_hit():
    clock = {"t": 0.0}
    c = collector(clock)
    unreadable = {"model": "m", "messages": [{"content": object()}]}
    c.on_call("anthropic", "m", unreadable, {})
    c.on_call("anthropic", "m", unreadable, {})

    (sim,) = simulation(c.due_summaries(force=True))
    assert sim["calls"] == 2
    assert all(sim[f"hits_{s}"] == 0 for s in SUFFIXES)


def test_a_simulation_summary_is_totals_only():
    """No fingerprint, no request, no reply: a fixed set of counters."""
    transport = Captured("pepper")
    m = meter(transport, salt="pepper", optimize=True)
    client = client_for(m)
    for _ in range(2):
        client.messages.create(model="claude-sonnet-4-6", messages=[{"content": SECRET}])
    drain(m)

    sims = simulation(transport.events)  # this test meter flushes after every call
    expected = {"kind", "scope_kind", "calls", "tokens_in", "tokens_out"}
    for suffix in SUFFIXES:
        expected |= {
            f"hits_{suffix}",
            f"hit_tokens_in_{suffix}",
            f"hit_tokens_out_{suffix}",
            f"evicted_{suffix}",
        }
    assert sims and all(set(sim) == expected for sim in sims)
    assert sum(s["calls"] for s in sims) == 2
    assert sum(s["hits_10m"] for s in sims) == 1
    assert SECRET not in transport.raw


def test_the_one_hour_cache_lifetime_is_counted_from_the_same_gaps():
    clock = {"t": 0.0}
    c = collector(clock, cache_window=300.0)
    request = {"system": "static", "messages": [{"role": "user"}]}
    for step in (0.0, 400.0, 400.0, 4000.0):
        clock["t"] += step
        c.on_call("anthropic", "m", request, {})

    summaries = c.due_summaries(force=True)
    (prefix,) = [e["signal"] for e in summaries if e["signal"]["kind"] == "prefix"]
    # Five minutes: every gap outlived it, so each of the four calls writes.
    assert prefix["cache_windows"] == 4
    # One hour: the first call, and the 4,000-second gap.
    assert prefix["cache_windows_1h"] == 2
