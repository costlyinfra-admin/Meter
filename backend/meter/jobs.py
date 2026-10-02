"""Runs that make model calls inside Meter, written down so they survive (EX-4).

A prompt-rewrite evaluation (PO-4) and a Meter-hosted model test (EX-4) both
replay captured calls on the customer's own key, one case at a time, for
minutes. Until EX-4 that happened in a thread inside the web service, holding
its plan in memory, so a deploy, a crash or a free-tier service going to sleep
lost the run and left it "running" for ever.

Now a run is an `eval_job` row first. It names the samples it will replay, and
each case is stored the moment it finishes, so whoever picks the run up next
skips what is already done. A run is held by a lease that every finished case
renews. When the lease runs out, whoever held it is gone, and the run is taken
over by the next person to look at it (the status read starts it again) or by
the scheduled job, whichever comes first. There is no new service: the web
process and the daily cron are the workers.

Each kind of run supplies two functions (`_HANDLERS`): `run(tenant_id, job,
client, judge_client, keep_going)`, which works through the remaining cases
and returns "done", "paused" or "failed" without raising; and
`stop(tenant_id, job, why)`, which ends the run as failed with a reason.
`keep_going()` renews the lease after each case and says whether to carry on.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
import uuid
from decimal import Decimal
from typing import Optional

from .db import admin_dsn, app_dsn, connect, tenant_tx

logger = logging.getLogger("meter.jobs")

#: How long a run is held without a word from its holder. Longer than one case
#: can take (two replays and two judge calls, each with its own timeout).
LEASE = dt.timedelta(minutes=5)
#: A run whose holder vanished this many times is stopped, not retried again:
#: something about it, not the service, is the problem.
MAX_INTERRUPTIONS = 3
#: The scheduled job's time budget, across every run it picks up.
SCHEDULED_BUDGET_SECONDS = 600

KINDS = ("prompt_evaluation", "hosted_test")


def _handlers(kind: str) -> tuple:
    # Imported here: both modules import this one.
    if kind == "prompt_evaluation":
        from . import prompt_eval

        return prompt_eval._run_job, prompt_eval._stop_job
    from . import hosted_tests

    return hosted_tests._run_job, hosted_tests._stop_job


# ---------------------------------------------------------------------------
# Writing a run down
# ---------------------------------------------------------------------------
def enqueue(conn, tenant_id: str, kind: str, subject_id: str, sample_ids: list, estimate) -> str:
    """Record a run before its first call, in the caller's transaction."""
    if kind not in KINDS:
        raise ValueError(kind)
    column = "evaluation_id" if kind == "prompt_evaluation" else "experiment_id"
    return str(
        conn.execute(
            f"""
            INSERT INTO eval_job (tenant_id, kind, {column}, sample_ids, estimate)
            VALUES (%s, %s, %s, %s::uuid[], %s) RETURNING id
            """,  # noqa: S608 - `column` is one of two constants
            (tenant_id, kind, subject_id, [str(s) for s in sample_ids], Decimal(str(estimate))),
        ).fetchone()[0]
    )


def spent_this_month(conn, now: Optional[dt.datetime] = None) -> Decimal:
    """What runs inside Meter have cost the organization this calendar month.

    Finished runs count what they spent. A run still going counts what it was
    estimated at when it was allowed to start, so two runs started together
    cannot both slip under the cap. In the caller's tenant transaction.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    row = conn.execute(
        """
        SELECT
          (SELECT coalesce(sum(e.spend), 0) FROM prompt_evaluation e
            WHERE e.started_at >= %(start)s
              AND NOT EXISTS (SELECT 1 FROM eval_job j WHERE j.evaluation_id = e.id
                                 AND j.status IN ('queued', 'running')))
        + (SELECT coalesce(sum(x.test_cost), 0) FROM experiment x
            WHERE x.runs_at = 'meter' AND x.created_at >= %(start)s
              AND NOT EXISTS (SELECT 1 FROM eval_job j WHERE j.experiment_id = x.id
                                 AND j.status IN ('queued', 'running')))
        + (SELECT coalesce(sum(estimate), 0) FROM eval_job
            WHERE created_at >= %(start)s AND status IN ('queued', 'running'))
        """,
        {"start": start},
    ).fetchone()
    return Decimal(row[0] or 0)


# ---------------------------------------------------------------------------
# Holding a run
# ---------------------------------------------------------------------------
_COLUMNS = "id, kind, evaluation_id, experiment_id, sample_ids, interruptions, lease_token"


def _job(row) -> dict:
    keys = [k.strip() for k in _COLUMNS.split(",")]
    d = dict(zip(keys, row))
    return {
        **d,
        "id": str(d["id"]),
        "evaluation_id": str(d["evaluation_id"]) if d["evaluation_id"] else None,
        "experiment_id": str(d["experiment_id"]) if d["experiment_id"] else None,
        "sample_ids": [str(s) for s in d["sample_ids"]],
        "lease_token": str(d["lease_token"]),
    }


def _claim(tenant_id: str, job_id: str) -> Optional[dict]:
    """Take the run if nobody holds it. Atomic: of two claimants, one wins.

    A run found `running` with a lapsed lease was abandoned by its holder,
    which counts as an interruption. One left `queued` (never started, or
    paused on purpose) does not.
    """
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            f"""
            UPDATE eval_job
               SET interruptions = interruptions
                       + CASE WHEN status = 'running' THEN 1 ELSE 0 END,
                   status = 'running', lease_token = %s, leased_until = now() + %s
             WHERE id = %s AND status IN ('queued', 'running')
               AND (leased_until IS NULL OR leased_until < now())
            RETURNING {_COLUMNS}
            """,  # noqa: S608 - constant
            (str(uuid.uuid4()), LEASE, job_id),
        ).fetchone()
    return _job(row) if row else None


def _renew(tenant_id: str, job: dict) -> bool:
    """Extend the lease. False if it is no longer this holder's to extend."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        return (
            conn.execute(
                """
                UPDATE eval_job SET leased_until = now() + %s
                 WHERE id = %s AND lease_token = %s AND status = 'running'
                """,
                (LEASE, job["id"], job["lease_token"]),
            ).rowcount
            == 1
        )


def _release(tenant_id: str, job: dict, status: str) -> None:
    """Let go of the run: finished (`done`/`failed`) or paused (`queued`)."""
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        conn.execute(
            """
            UPDATE eval_job
               SET status = %s, lease_token = NULL, leased_until = NULL,
                   finished_at = CASE WHEN %s = 'queued' THEN NULL ELSE now() END
             WHERE id = %s AND lease_token = %s
            """,
            (status, status, job["id"], job["lease_token"]),
        )


def end(conn, where: str, params: tuple) -> None:
    """Mark runs over without their holder: the subject was cancelled.

    The holder notices at its next case, when it can no longer renew.
    `where` is a fixed clause from the caller, never input.
    """
    conn.execute(
        f"UPDATE eval_job SET status = 'failed', finished_at = now(), lease_token = NULL, "  # noqa: S608
        f"leased_until = NULL WHERE status IN ('queued', 'running') AND {where}",
        params,
    )


# ---------------------------------------------------------------------------
# Working a run
# ---------------------------------------------------------------------------
def run(
    tenant_id: str,
    job_id: str,
    *,
    client=None,
    judge_client=None,
    deadline: Optional[float] = None,
) -> str:
    """Work one run until it finishes, is paused, or someone else holds it.

    Returns "done", "failed", "paused" or "busy" (held by someone else, or
    already over). `deadline` is a time.monotonic() value; past it the run is
    paused after the case in hand and left for the next worker.
    """
    job = _claim(tenant_id, job_id)
    if job is None:
        return "busy"
    run_job, stop_job = _handlers(job["kind"])
    if job["interruptions"] >= MAX_INTERRUPTIONS:
        stop_job(
            tenant_id,
            job,
            f"The run was interrupted {job['interruptions']} times and was stopped.",
        )
        _release(tenant_id, job, "failed")
        return "failed"

    held = {"lost": False}

    def keep_going() -> bool:
        if not _renew(tenant_id, job):
            held["lost"] = True  # cancelled, or taken over: stop without finishing
            return False
        return deadline is None or time.monotonic() < deadline

    try:
        outcome = run_job(tenant_id, job, client, judge_client, keep_going)
    except Exception:  # noqa: BLE001 - a handler is not supposed to raise; never leave it held
        logger.exception("run %s stopped unexpectedly", job_id)
        stop_job(tenant_id, job, "The run stopped unexpectedly.")
        outcome = "failed"
    if held["lost"]:
        return "busy"
    _release(tenant_id, job, {"done": "done", "failed": "failed"}.get(outcome, "queued"))
    return outcome


def start(
    tenant_id: str, job_id: str, *, background: bool = True, client=None, judge_client=None
) -> None:
    """Work a run now: in a thread for a request, inline for a test."""
    if not background:
        run(tenant_id, job_id, client=client, judge_client=judge_client)
        return
    threading.Thread(
        target=run,
        args=(tenant_id, job_id),
        kwargs={"client": client, "judge_client": judge_client},
        daemon=True,
    ).start()


def resume_if_abandoned(tenant_id: str, kind: str, subject_id: str) -> bool:
    """Called when a run is looked at: start it again if nobody holds it.

    This is what makes an interrupted run carry on as soon as anyone looks,
    instead of waiting for the daily job. Returns whether it was restarted.
    """
    column = "evaluation_id" if kind == "prompt_evaluation" else "experiment_id"
    with connect(app_dsn()) as conn, tenant_tx(conn, tenant_id):
        row = conn.execute(
            f"""
            SELECT id FROM eval_job
             WHERE {column} = %s AND status IN ('queued', 'running')
               AND (leased_until IS NULL OR leased_until < now())
            """,  # noqa: S608 - `column` is one of two constants
            (subject_id,),
        ).fetchone()
    if row is None:
        return False
    start(tenant_id, str(row[0]))
    return True


def progress(conn, kind: str, subject_id: str) -> Optional[dict]:
    """How far a run has got, in the caller's tenant transaction."""
    column = "evaluation_id" if kind == "prompt_evaluation" else "experiment_id"
    row = conn.execute(
        f"SELECT cardinality(sample_ids), status, interruptions FROM eval_job "  # noqa: S608
        f"WHERE {column} = %s",
        (subject_id,),
    ).fetchone()
    if row is None:
        return None
    return {"planned": int(row[0]), "status": row[1], "interruptions": int(row[2])}


def run_scheduled(
    budget_seconds: float = SCHEDULED_BUDGET_SECONDS, *, client=None, judge_client=None
) -> list:
    """Cron entry point: carry on every run nobody holds, within a time budget.

    A run still unfinished when the budget is spent is paused after its
    current case and left for the next look or the next day.
    """
    deadline = time.monotonic() + budget_seconds
    with connect(admin_dsn()) as conn:
        due = conn.execute(
            """
            SELECT id, tenant_id FROM eval_job
             WHERE status IN ('queued', 'running')
               AND (leased_until IS NULL OR leased_until < now())
             ORDER BY created_at
            """
        ).fetchall()
    from . import prompt_capture

    results = []
    for job_id, tenant_id in due:
        if time.monotonic() >= deadline:
            break
        if prompt_capture.disclosure(str(tenant_id)) is None:
            # This process cannot name the judge the organization's consent
            # named (Meter's own model is configured on the web service, and
            # may not be here), so it cannot tell whether consent still stands.
            # Left for the web service, which resumes it when it is looked at.
            results.append({"job_id": str(job_id), "status": "skipped"})
            continue
        try:
            outcome = run(
                str(tenant_id),
                str(job_id),
                client=client,
                judge_client=judge_client,
                deadline=deadline,
            )
        except Exception:  # noqa: BLE001 - one run failing must not stop the rest
            logger.warning("run %s could not be resumed", job_id)
            outcome = "error"
        results.append({"job_id": str(job_id), "status": outcome})
    return results


if __name__ == "__main__":
    summary = run_scheduled()
    print(f"Resumed {len(summary)} interrupted runs.")
