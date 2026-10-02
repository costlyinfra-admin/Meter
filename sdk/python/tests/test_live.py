"""Live tests (EX-3): tagging calls with a test and group, and quality scores.

The customer's own feature flag picks the client; the SDK only says which
test and group each call was in. A score is a number for a group — never
the answer it was about.
"""

from __future__ import annotations

import pytest
from test_optimize import SECRET, Captured, Response, drain, meter

EXP = "6f1c1b3e-1d1f-4c64-9a0b-5d0b6f1b9a11"


def _client(m, response=None, fail=False, **kw):
    class Anthropic:
        class messages:
            @staticmethod
            def create(**_kw):
                if fail:
                    raise RuntimeError("overloaded")
                return response or Response()

    return m.wrap(Anthropic(), feature_id="answer-generation", **kw)


def spans(transport, kind):
    return [e for e in transport.events if e["event_type"] == kind]


def test_every_call_is_tagged_with_its_test_and_group():
    t = Captured()
    m = meter(t)
    _client(m, experiment=EXP, group="candidate").messages.create(
        model="claude-haiku-4-5", messages=[{"content": SECRET}]
    )
    drain(m)
    (done,) = spans(t, "span.completed")
    assert (done["experiment_id"], done["experiment_group"]) == (EXP, "candidate")
    assert SECRET not in t.raw


def test_a_failed_call_counts_against_its_group():
    t = Captured()
    m = meter(t)
    with pytest.raises(RuntimeError):
        _client(m, fail=True, experiment=EXP, group="candidate").messages.create(model="m")
    drain(m)
    (failed,) = spans(t, "span.failed")
    assert (failed["experiment_id"], failed["experiment_group"]) == (EXP, "candidate")


def test_an_untagged_client_sends_no_tag_at_all():
    t = Captured()
    m = meter(t)
    _client(m).messages.create(model="m")
    drain(m)
    (done,) = spans(t, "span.completed")
    assert "experiment_id" not in done and "experiment_group" not in done


@pytest.mark.parametrize(
    "kw, message",
    [
        ({"experiment": EXP}, "both experiment and group"),
        ({"group": "control"}, "both experiment and group"),
        ({"experiment": EXP, "group": "treatment"}, '"control" or "candidate"'),
    ],
)
def test_a_tagging_mistake_is_the_developer_s_to_fix_at_once(kw, message):
    with pytest.raises(ValueError, match=message):
        _client(meter(Captured()), **kw)


def test_a_score_is_a_number_for_a_group_and_nothing_else():
    t = Captured()
    m = meter(t)
    m.score(EXP, "control", 1)
    m.score(EXP, "candidate", 0.5)
    drain(m)
    scores = spans(t, "experiment.score")
    assert [(s["experiment_group"], s["score"]) for s in scores] == [
        ("control", 1.0),
        ("candidate", 0.5),
    ]
    assert set(scores[0]) == {
        "event_type",
        "event_id",
        "experiment_id",
        "experiment_group",
        "score",
        "occurred_at",
    }


@pytest.mark.parametrize("bad", [True, "good", float("nan"), None])
def test_a_score_that_is_not_a_number_is_refused(bad):
    with pytest.raises(ValueError, match="must be a number"):
        meter(Captured()).score(EXP, "control", bad)
