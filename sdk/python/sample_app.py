"""A runnable sketch of both ways to instrument an agent.

Run it with nothing configured and it prints what it would have sent; set
METER_INGEST_URL and METER_INGEST_TOKEN and the same code reports for real.
There are no provider SDKs here — the "model calls" are stubs — because the
point is the shape of the instrumentation, not the model.
"""

from __future__ import annotations

import os
import time

from costlyinfra_meter import Meter


class FakeResponse:
    """What a provider client hands back: a model and a usage block."""

    model = "claude-sonnet-4-6"
    usage = type(
        "Usage",
        (),
        {
            "input_tokens": 8_400,
            "output_tokens": 920,
            "cache_read_input_tokens": 6_100,
            "cache_creation_input_tokens": 0,
        },
    )()


class FakeAnthropic:
    """Stands in for `anthropic.Anthropic()`."""

    class messages:
        @staticmethod
        def create(**kwargs):
            time.sleep(0.05)
            return FakeResponse()


def retrieve_documents():
    """A tool. Meter records that it ran and how long it took — never its result."""
    time.sleep(0.02)
    return ["doc-1", "doc-2"]


def main() -> None:
    meter = Meter(application="support-agent", environment="development")
    if not meter.enabled:
        print("METER_INGEST_URL/METER_INGEST_TOKEN unset — running as a no-op.\n")

    # 1. One model call. No trace management: wrapping is the whole setup.
    client = meter.wrap(FakeAnthropic(), feature_id=os.environ.get("METER_FEATURE"))
    client.messages.create(model="claude-sonnet-4-6", messages=[{"role": "user", "content": "hi"}])
    print("one-call trace sent")

    # 2. A workflow. Each step becomes a span under one run.
    with meter.agent("resolve-ticket", customer_id="customer-123") as run:
        run.llm("classify", lambda: FakeResponse(), prompt_id="classify-ticket",
                prompt_version="3.2")
        run.tool("retrieve-documents", retrieve_documents)
        run.guardrail("policy-check", lambda: True)
        run.llm("generate-answer", lambda: FakeResponse(), prompt_id="answer-ticket",
                prompt_version="5.0")

        # 3. Hand the rest to a worker. The context is identifiers only.
        context = run.export_context()
    print(f"agent trace sent (context for a worker: {sorted(context)})")

    with meter.resume(context) as worker:
        worker.tool("post-to-crm", lambda: None)
    print("resumed trace sent")

    meter.flush()
    if meter.dropped:
        print(f"warning: {meter.dropped} events dropped")


if __name__ == "__main__":
    main()
