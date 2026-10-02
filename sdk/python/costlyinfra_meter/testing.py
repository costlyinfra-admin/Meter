"""meter-test — run a Meter test on your own machine, send back only numbers.

Meter can tell you that a cheaper model would cost less. Whether it would
answer your product's questions as well is a question about answers, and Meter
never sees answers. So this runs where they are: on your machine, on your test
cases, with your own provider keys, judged by a model you choose. What leaves
is a fixed set of numbers per case — tokens, latency, whether a call failed or
broke a check, and which answer the judge preferred. Nothing you wrote and
nothing a model wrote is ever sent. Run with --dry-run to see exactly what
would be.

Three ways to use it, each with the token Meter showed when you started the
test (METER_EXPERIMENT_TOKEN) and your Meter address (METER_URL):

    meter-test run --cases cases.jsonl
    meter-test import promptfoo results.json
    meter-test import inspect control.json candidate.json

`run` replays your cases through both models itself. The two `import` forms
read results from a tool you already use instead, and send the same numbers.

Standard library only, like the rest of this SDK.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from . import __version__

#: (url, headers, body or None) -> (status, body bytes). Injectable for tests.
Http = Callable[[str, dict, Optional[bytes]], "tuple[int, bytes]"]

CALL_TIMEOUT = 120.0
DEFAULT_MAX_TOKENS = 1024
VERDICTS = ("better", "same", "worse", "unjudged")

# The same deterministic checks Meter applies to a prompt rewrite, so a broken
# answer means the same thing in both places. About form, not taste.
_REFUSALS = re.compile(
    r"\b(i (can(not|'t)|am unable to|won't)|as an ai\b|i'm sorry, but)", re.IGNORECASE
)


class TestRunError(Exception):
    """Something the person running the test needs to fix, in words."""


def _json_like(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped or stripped[0] not in "[{":
        return False
    try:
        json.loads(stripped)
        return True
    except ValueError:
        return False


def failed_checks(control: str, candidate: str) -> list:
    """Checks the candidate's answer fails where the current model's did not."""
    broken = []
    if not (candidate or "").strip():
        broken.append("empty_answer")
    if _json_like(control) and not _json_like(candidate):
        broken.append("not_json")
    if _REFUSALS.search(candidate or "") and not _REFUSALS.search(control or ""):
        broken.append("refused")
    if control and len(candidate or "") > max(400, len(control) * 3):
        broken.append("much_longer")
    return broken


# ---------------------------------------------------------------------------
# Talking to Meter and to the providers
# ---------------------------------------------------------------------------
def _urllib(url: str, headers: dict, body: Optional[bytes]) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=CALL_TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class _Meter:
    def __init__(self, base_url: str, token: str, http: Http):
        self.base = base_url.rstrip("/")
        self.token = token
        self.http = http

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                "User-Agent": f"meter-test/{__version__}"}

    def spec(self) -> dict:
        status, body = self.http(f"{self.base}/api/experiment-runs/spec", self._headers(), None)
        if status == 401:
            raise TestRunError(
                "Meter did not accept this test's token: it expired, was used, or the test was "
                "cancelled. Start the test again in Meter for a new one."
            )
        if status >= 400:
            raise TestRunError(f"Meter answered {status} when asked for this test.")
        return json.loads(body)

    def submit(self, payload: dict) -> dict:
        status, body = self.http(f"{self.base}/api/experiment-runs/results", self._headers(),
                                 json.dumps(payload).encode())
        if status >= 400:
            detail = ""
            try:
                detail = json.loads(body).get("detail")
            except ValueError:
                pass
            raise TestRunError(f"Meter refused the results ({status}): {detail}")
        return json.loads(body)


def _call_model(http: Http, provider: str, model: str, api_key: str, case: dict,
                system: Optional[str] = None, max_tokens: Optional[int] = None) -> dict:
    """One model call. Returns text (kept on this machine), tokens, latency, error."""
    messages = [m for m in case["messages"] if m.get("role") in ("user", "assistant")]
    system = case.get("system") if system is None else system
    limit = int(max_tokens or case.get("max_tokens") or DEFAULT_MAX_TOKENS)
    began = time.perf_counter()
    if provider == "anthropic":
        body: dict = {"model": model, "max_tokens": limit, "messages": messages}
        if system:
            body["system"] = system
        if isinstance(case.get("temperature"), (int, float)):
            body["temperature"] = case["temperature"]
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
                   "Content-Type": "application/json"}
    else:
        body = {"model": model, "max_tokens": limit,
                "messages": ([{"role": "system", "content": system}] if system else []) + messages}
        if isinstance(case.get("temperature"), (int, float)):
            body["temperature"] = case["temperature"]
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        status, raw = http(url, headers, json.dumps(body).encode())
    except Exception:  # a network failure is a failed call, not a crashed run
        status, raw = 599, b""
    latency = int((time.perf_counter() - began) * 1000)
    if status >= 400:
        return {"text": "", "tokens_in": 0, "tokens_out": 0, "latency_ms": latency,
                "error": True}
    payload = json.loads(raw)
    if provider == "anthropic":
        text = "".join(b.get("text", "") for b in payload.get("content", [])
                       if b.get("type") == "text")
        usage = payload.get("usage") or {}
        tin = int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0) \
            + int(usage.get("cache_creation_input_tokens") or 0)
        tout = int(usage.get("output_tokens") or 0)
    else:
        text = ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        usage = payload.get("usage") or {}
        tin = int(usage.get("prompt_tokens") or 0)
        tout = int(usage.get("completion_tokens") or 0)
    return {"text": text, "tokens_in": tin, "tokens_out": tout, "latency_ms": latency,
            "error": False}


def _judge(http: Http, judge: dict, system: str, request: str, control: str,
           candidate: str) -> tuple[str, int, int]:
    """Which answer is better, asked both ways round. Disagreement is a tie.

    Returns (verdict for the candidate, judge tokens in, judge tokens out).
    """
    tin = tout = 0
    winners = []
    for first, second in ((control, candidate), (candidate, control)):
        case = {"messages": [{"role": "user", "content":
                              f"REQUEST:\n{request}\n\nANSWER A:\n{first}\n\nANSWER B:\n{second}"}],
                "temperature": 0}
        out = _call_model(http, judge["provider"], judge["model"], judge["key"], case,
                          system=system, max_tokens=300)
        tin += out["tokens_in"]
        tout += out["tokens_out"]
        if out["error"]:
            return "unjudged", tin, tout
        match = re.search(r"\{.*\}", out["text"] or "", re.DOTALL)
        try:
            parsed = json.loads(match.group(0)) if match else {}
            winner = str(parsed.get("winner", "tie")).upper()
        except ValueError:
            winner = "TIE"
        winners.append(winner if winner in ("A", "B") else "TIE")
    # First ordering: A is the current model. Second: A is the candidate.
    first_says = {"A": "worse", "B": "better", "TIE": "same"}[winners[0]]
    second_says = {"A": "better", "B": "worse", "TIE": "same"}[winners[1]]
    return (first_says if first_says == second_says else "same"), tin, tout


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------
def load_cases(path: str) -> list:
    """Read a JSONL file of cases.

    One case per line: {"messages": [{"role": "user", "content": "..."}], ...}
    or the shorthand {"input": "..."}. Optional: "id", "system", "max_tokens",
    "temperature". Nothing here is sent anywhere but your providers.
    """
    cases = []
    with open(path, encoding="utf-8") as fh:
        for number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except ValueError as exc:
                raise TestRunError(f"{path}:{number} is not valid JSON.") from exc
            if isinstance(raw.get("input"), str) and "messages" not in raw:
                raw["messages"] = [{"role": "user", "content": raw["input"]}]
            msgs = raw.get("messages")
            if not isinstance(msgs, list) or not msgs or not all(
                isinstance(m, dict) and isinstance(m.get("content"), str) for m in msgs
            ):
                raise TestRunError(
                    f"{path}:{number} needs \"messages\" (a list of {{role, content}}) or "
                    "\"input\" (text)."
                )
            raw.setdefault("id", str(number))
            cases.append(raw)
    if not cases:
        raise TestRunError(f"{path} has no cases.")
    return cases


def _hasher() -> Callable[[str], str]:
    """Case ids leave as salted hashes: not even your labels are sent, and a
    hash from one run cannot be matched against another's."""
    salt = secrets.token_hex(16)
    return lambda label: hashlib.sha256(f"{salt}:{label}".encode()).hexdigest()


def _case_result(case_hash: str, control: dict, candidate: dict, checks: int, verdict: str) -> dict:
    keep = ("tokens_in", "tokens_out", "latency_ms", "error")
    return {"case": case_hash,
            "control": {k: control[k] for k in keep},
            "candidate": {k: candidate[k] for k in keep},
            "check_failures": checks, "verdict": verdict}


def run_cases(spec: dict, cases: list, keys: dict, judge: dict, http: Http,
              concurrency: int = 4) -> dict:
    """Replay every case through both models, check and judge. Returns the payload."""
    provider = spec["provider"]
    key = keys.get(provider)
    if not key:
        raise TestRunError(f"Set {provider.upper()}_API_KEY: the test calls {provider} with it.")
    if not judge.get("key"):
        raise TestRunError(
            f"Set {judge['provider'].upper()}_API_KEY for the judge ({judge['model']})."
        )
    cases = cases[: int(spec["max_cases"])]
    digest = _hasher()

    def one(case: dict) -> tuple[dict, int, int]:
        control = _call_model(http, provider, spec["control_model"], key, case)
        candidate = _call_model(http, provider, spec["candidate_model"], key, case)
        if control["error"] or candidate["error"]:
            return _case_result(digest(case["id"]), control, candidate, 0, "unjudged"), 0, 0
        checks = len(failed_checks(control["text"], candidate["text"]))
        request = "\n\n".join(m["content"] for m in case["messages"])
        verdict, jin, jout = _judge(http, judge, spec["judge"]["system"], request,
                                    control["text"], candidate["text"])
        return _case_result(digest(case["id"]), control, candidate, checks, verdict), jin, jout

    with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as pool:
        done = list(pool.map(one, cases))
    return {
        "source": "runner",
        "runner_version": __version__,
        "judge_model": judge["model"],
        "judge_usage": {"provider": judge["provider"], "tokens_in": sum(d[1] for d in done),
                        "tokens_out": sum(d[2] for d in done)},
        "cases": [d[0] for d in done],
    }


# ---------------------------------------------------------------------------
# Importers: results from a tool you already use, reduced to the same numbers
# ---------------------------------------------------------------------------
def _matches(name: Any, model: str) -> bool:
    text = str(name or "")
    return text == model or text.endswith(f":{model}") or text.endswith(f"/{model}")


def _verdict_from(control_score: Optional[float], candidate_score: Optional[float]) -> str:
    if control_score is None or candidate_score is None:
        return "unjudged"
    if candidate_score > control_score:
        return "better"
    if candidate_score < control_score:
        return "worse"
    return "same"


def import_promptfoo(spec: dict, path: str) -> dict:
    """A promptfoo results file (`promptfoo eval -o results.json`).

    Both models must be providers in the same eval. Each test case's two
    results are paired; the verdict comes from your own assertions — passing
    where the current model failed is better, the reverse is worse. Only
    token counts, latency, errors and pass/fail are read.
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    summary = data.get("results") if isinstance(data, dict) else None
    if not isinstance(summary, dict) or summary.get("version") != 3:
        raise TestRunError(
            f"{path} is not a promptfoo results file this can read (summary version 3). "
            "Write one with: promptfoo eval -o results.json"
        )
    pairs: dict = {}
    for r in summary.get("results") or []:
        provider = r.get("provider") or {}
        names = (provider.get("id"), provider.get("label"))
        if any(_matches(n, spec["control_model"]) for n in names):
            side = "control"
        elif any(_matches(n, spec["candidate_model"]) for n in names):
            side = "candidate"
        else:
            continue
        pairs.setdefault((r.get("testIdx"), r.get("promptIdx")), {})[side] = r
    complete = {k: p for k, p in pairs.items() if "control" in p and "candidate" in p}
    if not complete:
        raise TestRunError(
            f"{path} has no test case run on both {spec['control_model']} and "
            f"{spec['candidate_model']}. Add both as providers in one eval."
        )
    digest = _hasher()

    def call(r: dict) -> dict:
        usage = r.get("tokenUsage") or (r.get("response") or {}).get("tokenUsage") or {}
        return {"tokens_in": int(usage.get("prompt") or 0),
                "tokens_out": int(usage.get("completion") or 0),
                "latency_ms": int(r.get("latencyMs") or 0),
                "error": bool(r.get("error")) or r.get("failureReason") == 2}

    cases = []
    for (test, prompt), pair in sorted(complete.items(), key=lambda kv: str(kv[0])):
        control, candidate = call(pair["control"]), call(pair["candidate"])
        verdict = "unjudged" if control["error"] or candidate["error"] else _verdict_from(
            1.0 if pair["control"].get("success") else 0.0,
            1.0 if pair["candidate"].get("success") else 0.0,
        )
        cases.append(_case_result(digest(f"{test}:{prompt}"), control, candidate, 0, verdict))
    return {"source": "promptfoo", "runner_version": __version__, "judge_model": None,
            "judge_usage": None, "cases": cases}


_INSPECT_VALUES = {"C": 1.0, "I": 0.0, "P": 0.5, "N": 0.0}


def _inspect_score(sample: dict, scorer: Optional[str]) -> Optional[float]:
    scores = sample.get("scores") or {}
    if not scores:
        return None
    entry = scores.get(scorer) if scorer else next(iter(scores.values()))
    value = (entry or {}).get("value")
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return _INSPECT_VALUES.get(value.upper())
    return None


def import_inspect(spec: dict, control_path: str, candidate_path: str,
                   scorer: Optional[str] = None) -> dict:
    """Two Inspect AI logs as JSON (`inspect log dump LOG > file.json`), one per
    model, from the same task. Samples are paired by id and epoch; the verdict
    compares your scorer's values (C/I/P/N, true/false, or numbers). Only token
    usage, timing, errors and score values are read."""
    logs = {}
    for side, path, model in (("control", control_path, spec["control_model"]),
                              ("candidate", candidate_path, spec["candidate_model"])):
        with open(path, encoding="utf-8") as fh:
            log = json.load(fh)
        if not isinstance(log, dict) or "samples" not in log:
            raise TestRunError(f"{path} is not an Inspect log as JSON. Write one with: "
                               "inspect log dump LOGFILE > file.json")
        if not _matches((log.get("eval") or {}).get("model"), model):
            raise TestRunError(f"{path} was run on {(log.get('eval') or {}).get('model')}, "
                               f"not {model}.")
        logs[side] = {(s.get("id"), s.get("epoch", 1)): s for s in log.get("samples") or []}
    shared = [k for k in logs["control"] if k in logs["candidate"]]
    if not shared:
        raise TestRunError("The two logs share no samples. Run the same task on both models.")
    digest = _hasher()

    def call(sample: dict, model: str) -> dict:
        usage = sample.get("model_usage") or {}
        mine = [u for name, u in usage.items() if _matches(name, model)] or list(usage.values())
        return {"tokens_in": sum(int(u.get("input_tokens") or 0) for u in mine[:1]),
                "tokens_out": sum(int(u.get("output_tokens") or 0) for u in mine[:1]),
                "latency_ms": int(float(sample.get("total_time") or 0) * 1000),
                "error": bool(sample.get("error"))}

    cases = []
    for key in sorted(shared, key=str):
        c_sample, k_sample = logs["control"][key], logs["candidate"][key]
        control = call(c_sample, spec["control_model"])
        candidate = call(k_sample, spec["candidate_model"])
        verdict = "unjudged" if control["error"] or candidate["error"] else _verdict_from(
            _inspect_score(c_sample, scorer), _inspect_score(k_sample, scorer))
        cases.append(_case_result(digest(f"{key[0]}:{key[1]}"), control, candidate, 0, verdict))
    return {"source": "inspect", "runner_version": __version__, "judge_model": None,
            "judge_usage": None, "cases": cases}


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------
def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="meter-test",
        description="Run a Meter test on your machine and send back only numbers.",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="print exactly what would be sent, and send nothing")
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="replay your cases through both models")
    run.add_argument("--cases", required=True, help="JSONL file of cases")
    run.add_argument("--judge-model", help="model that compares the answers "
                     "(default: the model being replaced)")
    run.add_argument("--judge-provider", choices=("anthropic", "openai"),
                     help="the judge's provider (default: the test's)")
    run.add_argument("--limit", type=int, help="use only the first N cases")
    run.add_argument("--concurrency", type=int, default=4)
    imp = sub.add_parser("import", help="send results from a tool you already use")
    imp_sub = imp.add_subparsers(dest="tool", required=True)
    pf = imp_sub.add_parser("promptfoo")
    pf.add_argument("results", help="file written by: promptfoo eval -o results.json")
    ins = imp_sub.add_parser("inspect")
    ins.add_argument("control", help="Inspect log (JSON) for the model being replaced")
    ins.add_argument("candidate", help="Inspect log (JSON) for the cheaper model")
    ins.add_argument("--scorer", help="which scorer to compare (default: the first)")
    return p


def main(argv: Optional[list] = None, http: Http = _urllib, env: Optional[dict] = None,
         out=None) -> int:
    env = os.environ if env is None else env
    out = out or sys.stdout
    args = _parser().parse_args(argv)
    try:
        token = env.get("METER_EXPERIMENT_TOKEN")
        base = env.get("METER_URL")
        if not token or not base:
            raise TestRunError("Set METER_EXPERIMENT_TOKEN and METER_URL — both are shown in "
                               "Meter on the test's page.")
        meter = _Meter(base, token, http)
        spec = meter.spec()
        print(f"Testing {spec['candidate_model']} in place of {spec['control_model']}.",
              file=out)
        if args.command == "run":
            cases = load_cases(args.cases)
            if args.limit:
                cases = cases[: args.limit]
            judge_provider = args.judge_provider or spec["provider"]
            judge = {"provider": judge_provider,
                     "model": args.judge_model or spec["control_model"],
                     "key": env.get(f"{judge_provider.upper()}_API_KEY")}
            keys = {p: env.get(f"{p.upper()}_API_KEY") for p in ("anthropic", "openai")}
            print(f"{len(cases)} case(s); judged by {judge['model']}.", file=out)
            payload = run_cases(spec, cases, keys, judge, http, args.concurrency)
        elif args.tool == "promptfoo":
            payload = import_promptfoo(spec, args.results)
        else:
            payload = import_inspect(spec, args.control, args.candidate, args.scorer)
        if args.dry_run:
            print(json.dumps(payload, indent=2), file=out)
            print("Dry run: nothing was sent.", file=out)
            return 0
        answer = meter.submit(payload)
        print(f"{answer['outcome']}: {answer['outcome_reason']}", file=out)
        return 0
    except TestRunError as exc:
        print(f"meter-test: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
