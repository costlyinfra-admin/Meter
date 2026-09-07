"""Cost-source resource detail — the provider-generic view behind each source card.

One shape for every provider: a single detail table of attributable resources with
their current manual classification and cost. Providers that expose sub-resource
identity (today: Anthropic → workspace/API key) return ``classifiable`` rows the
user can classify; providers that don't yet return ``classifiable: False`` and a
short note, so the UI never forces a meaningless classification table.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from . import inference

#: Providers with a resource-level detail adapter today. Everything else falls back
#: to the "no resource-level detail yet" panel.
_ADAPTERS = {
    "anthropic": inference.anthropic_resource_detail,
}


def resource_detail(tenant_id: str, provider: str, period: Optional[dt.date] = None) -> dict:
    adapter = _ADAPTERS.get(provider)
    if adapter is not None:
        return adapter(tenant_id, period)
    return {
        "provider": provider,
        "classifiable": False,
        "rows": [],
        "message": (
            "This source doesn't expose resource-level detail yet. Its spend is "
            "tracked in Overview; per-resource classification will appear here once "
            "an adapter is available."
        ),
    }
