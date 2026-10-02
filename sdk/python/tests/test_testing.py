"""meter-test: a test run on the customer's machine that sends back only numbers.

The whole promise of this command is in what it does NOT send, so most of what
follows plants a secret in the cases, in the models' answers and in the
imported tools' files, and checks it never appears in anything that leaves.
"""

from __future__ import annotations

import io
import json
import re

import pytest

from costlyinfra_meter import testing

SECRET = "the quick brown fox jumped over the lazy dog"
BASE = "https://meter.test"
CONTROL, CANDIDATE = "claude-sonnet-4-6", "claude-haiku-4-5"
SPEC = {
    "experiment_id": "e1",
    "lever": "model_rightsizing",
    "provider": "anthropic",
    "control_model": CONTROL,
    "candidate_model": CANDIDATE,
    "rule": {"loss_margin": 0.1, "min_cases": 20},
    "max_cases": 500,
    "checks": ["empty_answer", "not_json", "refused", "much_longer"],
    "judge": {"system": "You compare two answers.", "protocol": "both_orders"},
}
ENV = {"METER_EXPERIMENT_TOKEN": "mtx_t", "METER_URL": BASE, "ANTHROPIC_API_KEY": "sk-a"}


class Net:
    """Meter and Anthropic, as far as this command can tell."""

    def __init__(self, answers=None, judge=("B", "A"), fail_model=None, spec_status=200):
        self.answers = answers or {CONTROL: f"Answer: {SECRET}", CANDIDATE: f"Short: {SECRET}"}
        self.judge = list(judge)
        self.fail_model = fail_model
        self.spec_status = spec_status
        self.posted = []
        self.calls = []

    def __call__(self, url, headers, body):
        if url == f"{BASE}/api/experiment-runs/spec":
            assert headers["Authorization"] == "Bearer mtx_t"
            return self.spec_status, json.dumps(SPEC).encode()
        if url == f"{BASE}/api/experiment-runs/results":
            self.posted.append(json.loads(body))
            return 200, json.dumps(
                {"status": "completed", "outcome": "passed", "outcome_reason": "ok"}
            ).encode()
        req = json.loads(body)
        self.calls.append(req)
        if req["model"] == self.fail_model:
            return 529, b'{"error": "overloaded"}'
        if req.get("system") == SPEC["judge"]["system"]:
            winner = self.judge[
                (len([c for c in self.calls if c.get("system") == SPEC["judge"]["system"]]) - 1) % 2
            ]
            text = json.dumps({"winner": winner, "reason": f"because {SECRET}"})
            return 200, json.dumps(
                {
                    "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": 500, "output_tokens": 20},
                }
            ).encode()
        return 200, json.dumps(
            {
                "content": [{"type": "text", "text": self.answers[req["model"]]}],
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 200 if req["model"] == CONTROL else 80,
                },
            }
        ).encode()


def cases_file(tmp_path, n=3):
    path = tmp_path / "cases.jsonl"
    path.write_text(
        "\n".join(
            json.dumps({"id": f"refund-{i}", "system": SECRET, "input": f"{SECRET} #{i}"})
            for i in range(n)
        )
    )
    return str(path)


def run(argv, net, env=ENV):
    out = io.StringIO()
    code = testing.main(argv, http=net, env=env, out=out)
    return code, out.getvalue()


def test_a_run_sends_numbers_and_nothing_else(tmp_path):
    net = Net()
    code, _ = run(["run", "--cases", cases_file(tmp_path)], net)
    assert code == 0
    (payload,) = net.posted
    assert payload["source"] == "runner" and payload["judge_model"] == CONTROL
    assert len(payload["cases"]) == 3
    case = payload["cases"][0]
    assert set(case) == {"case", "control", "candidate", "check_failures", "verdict"}
    assert set(case["control"]) == {"tokens_in", "tokens_out", "latency_ms", "error"}
    assert (case["control"]["tokens_out"], case["candidate"]["tokens_out"]) == (200, 80)
    sent = json.dumps(net.posted)
    assert SECRET not in sent  # not the cases, the answers or the judge's reasons
    assert "refund-" not in sent  # not even the customer's own case labels
    assert re.fullmatch(r"[0-9a-f]{64}", case["case"])
    # The judge's own tokens are reported, so the run's cost can be priced.
    assert payload["judge_usage"] == {"provider": "anthropic", "tokens_in": 3000, "tokens_out": 120}


def test_the_judge_is_asked_both_ways_round_and_must_agree(tmp_path):
    # First ordering: A is the current model, B the candidate -> "B" means better.
    # Second ordering: A is the candidate -> "A" means better. Agreement: better.
    agree = Net(judge=("B", "A"))
    run(["run", "--cases", cases_file(tmp_path, 1)], agree)
    assert agree.posted[0]["cases"][0]["verdict"] == "better"
    # The judge just preferring whatever it read second is not a verdict.
    biased = Net(judge=("B", "B"))
    run(["run", "--cases", cases_file(tmp_path, 1)], biased)
    assert biased.posted[0]["cases"][0]["verdict"] == "same"


def test_a_broken_answer_is_counted_as_a_failed_check(tmp_path):
    net = Net(answers={CONTROL: '{"refund": true}', CANDIDATE: "Yes, a refund."})
    run(["run", "--cases", cases_file(tmp_path, 1)], net)
    assert net.posted[0]["cases"][0]["check_failures"] == 1  # not_json


def test_a_failed_call_is_reported_as_one_and_not_judged(tmp_path):
    net = Net(fail_model=CANDIDATE)
    run(["run", "--cases", cases_file(tmp_path, 2)], net)
    case = net.posted[0]["cases"][0]
    assert case["candidate"]["error"] is True and case["verdict"] == "unjudged"


def test_the_customer_chooses_the_judge(tmp_path):
    net = Net()
    run(["run", "--cases", cases_file(tmp_path, 1), "--judge-model", "claude-opus-4-8"], net)
    judged = [c for c in net.calls if c.get("system") == SPEC["judge"]["system"]]
    assert {c["model"] for c in judged} == {"claude-opus-4-8"}
    assert net.posted[0]["judge_model"] == "claude-opus-4-8"


def test_a_dry_run_shows_what_would_be_sent_and_sends_nothing(tmp_path):
    net = Net()
    code, out = run(["--dry-run", "run", "--cases", cases_file(tmp_path, 2)], net)
    assert code == 0 and net.posted == []
    assert "Dry run: nothing was sent." in out
    assert '"cases"' in out and SECRET not in out


def test_without_a_provider_key_it_says_which(tmp_path, capsys):
    env = {k: v for k, v in ENV.items() if k != "ANTHROPIC_API_KEY"}
    code, _ = run(["run", "--cases", cases_file(tmp_path, 1)], Net(), env=env)
    assert code == 2
    assert "Set ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_a_spent_token_is_explained(tmp_path, capsys):
    code, _ = run(["run", "--cases", cases_file(tmp_path, 1)], Net(spec_status=401))
    assert code == 2
    assert "expired, was used, or the test was cancelled" in capsys.readouterr().err


def test_a_case_file_says_where_it_is_wrong(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"input": "ok"}\n{"messages": "not a list"}\n')
    with pytest.raises(testing.TestRunError, match="bad.jsonl:2"):
        testing.load_cases(str(bad))


# ---------------------------------------------------------------------------
# Importers
# ---------------------------------------------------------------------------
def _pf_result(test, provider, success, tokens, error=None):
    return {
        "testIdx": test,
        "promptIdx": 0,
        "provider": {"id": f"anthropic:messages:{provider}"},
        "prompt": {"raw": SECRET},
        "vars": {"question": SECRET},
        "response": {
            "output": f"{SECRET} answer",
            "tokenUsage": {"prompt": tokens[0], "completion": tokens[1], "total": sum(tokens)},
        },
        "success": success,
        "score": 1.0 if success else 0.0,
        "latencyMs": 700,
        "gradingResult": {"pass": success, "reason": SECRET},
        "error": error,
        "failureReason": 2 if error else 0,
    }


def test_promptfoo_results_become_the_same_numbers(tmp_path):
    results = [
        _pf_result(0, CONTROL, True, (1000, 200)),
        _pf_result(0, CANDIDATE, True, (1000, 90)),
        _pf_result(1, CONTROL, False, (900, 150)),
        _pf_result(1, CANDIDATE, True, (900, 70)),
        _pf_result(2, CONTROL, True, (800, 100)),
        _pf_result(2, CANDIDATE, False, (800, 50)),
        _pf_result(3, "gpt-4o", True, (1, 1)),  # a third provider: ignored
    ]
    path = tmp_path / "results.json"
    path.write_text(
        json.dumps(
            {
                "evalId": "x",
                "results": {
                    "version": 3,
                    "timestamp": "t",
                    "results": results,
                    "prompts": [],
                    "stats": {},
                },
                "config": {"prompts": [SECRET]},
            }
        )
    )
    net = Net()
    code, _ = run(["import", "promptfoo", str(path)], net)
    assert code == 0
    payload = net.posted[0]
    assert payload["source"] == "promptfoo" and payload["judge_model"] is None
    assert [c["verdict"] for c in payload["cases"]] == ["same", "better", "worse"]
    assert payload["cases"][0]["candidate"]["tokens_out"] == 90
    assert SECRET not in json.dumps(payload)


def test_an_old_promptfoo_file_is_refused_with_how_to_make_a_new_one(tmp_path):
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"results": {"version": 2, "results": []}}))
    with pytest.raises(testing.TestRunError, match="promptfoo eval -o results.json"):
        testing.import_promptfoo(SPEC, str(path))


def _inspect_log(model, values, tokens_out):
    return {
        "version": 2,
        "status": "success",
        "eval": {"task": "refunds", "model": f"anthropic/{model}"},
        "samples": [
            {
                "id": i,
                "epoch": 1,
                "input": SECRET,
                "output": {"completion": SECRET},
                "scores": {"match": {"value": v, "explanation": SECRET}},
                "model_usage": {
                    f"anthropic/{model}": {"input_tokens": 1000, "output_tokens": tokens_out}
                },
                "total_time": 1.5,
            }
            for i, v in enumerate(values)
        ],
    }


def test_two_inspect_logs_become_the_same_numbers(tmp_path):
    control = tmp_path / "control.json"
    candidate = tmp_path / "candidate.json"
    control.write_text(json.dumps(_inspect_log(CONTROL, ["C", "I", "C"], 200)))
    candidate.write_text(json.dumps(_inspect_log(CANDIDATE, ["C", "C", "I"], 80)))
    net = Net()
    code, _ = run(["import", "inspect", str(control), str(candidate)], net)
    assert code == 0
    payload = net.posted[0]
    assert payload["source"] == "inspect"
    assert [c["verdict"] for c in payload["cases"]] == ["same", "better", "worse"]
    assert payload["cases"][0]["candidate"]["latency_ms"] == 1500
    assert SECRET not in json.dumps(payload)


def test_an_inspect_log_from_the_wrong_model_is_refused(tmp_path):
    control = tmp_path / "c.json"
    candidate = tmp_path / "k.json"
    control.write_text(json.dumps(_inspect_log("claude-opus-4-8", ["C"], 1)))
    candidate.write_text(json.dumps(_inspect_log(CANDIDATE, ["C"], 1)))
    with pytest.raises(testing.TestRunError, match="not claude-sonnet-4-6"):
        testing.import_inspect(SPEC, str(control), str(candidate))
