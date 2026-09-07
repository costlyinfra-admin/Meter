"""Sample app — how a customer wires the metering hook into their LLM calls.

Run offline:  python sample_app.py
(With METER_INGEST_URL + METER_INGEST_TOKEN set, events are reported.)
"""

from costlyinfra_meter import Meter

# One meter per feature (or pass feature_id per call).
meter = Meter(feature_id="feature-threat-triage")


def classify_alert(alert_text: str) -> str:
    # --- your real LLM call would look like this -------------------------
    # from anthropic import Anthropic
    # resp = Anthropic().messages.create(model="claude-sonnet-4-6", max_tokens=200,
    #                                    messages=[{"role": "user", "content": alert_text}])
    # meter.record_anthropic(resp)            # <-- one line; that's the whole hook
    # return resp.content[0].text
    #
    # For this offline sample we simulate the response object:
    fake_response = type(
        "Resp",
        (),
        {"model": "claude-sonnet-4-6", "usage": {"input_tokens": 1200, "output_tokens": 300}},
    )()
    meter.record_anthropic(fake_response)
    return "severity: high"


if __name__ == "__main__":
    print("classification:", classify_alert("suspicious login from new ASN"))
    print("hook enabled:", meter.enabled, "(set METER_INGEST_URL/TOKEN to report)")
