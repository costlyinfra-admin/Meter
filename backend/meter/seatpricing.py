"""Per-seat monthly prices for AI coding tools (the build-cost "price book").

Seat-based coding tools bill a fixed amount per assigned user per month, so a
developer's build cost from such a tool is simply ``seat_price(tool, plan)`` —
no spend API needed, just the seat roster (who's assigned) times this table.
This mirrors pricing.py (which prices inference tokens); keep it current as
vendor prices change — drift is transparent, not a silently-wrong number.

Prices are USD per seat per month (list prices; approximate).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

SEAT_PRICING_VERSION = "2026-06-01"

# (tool, plan) -> monthly seat price in USD
_SEAT_PRICES: dict[tuple[str, str], str] = {
    # GitHub Copilot
    ("copilot", "business"): "19",
    ("copilot", "enterprise"): "39",
    # Cursor
    ("cursor", "pro"): "20",
    ("cursor", "business"): "40",
    # Amazon Q Developer
    ("amazon_q", "pro"): "19",
    # Gemini Code Assist
    ("gemini_code_assist", "standard"): "19",
    ("gemini_code_assist", "enterprise"): "45",
    # Tabnine
    ("tabnine", "pro"): "12",
    ("tabnine", "enterprise"): "39",
    # OpenAI Codex via ChatGPT seats. Note: since 2026 Codex on Business bills by
    # API token usage, so this per-seat figure is an approximation for licensed
    # seats; use the CSV import for exact usage-based Codex spend.
    ("codex", "business"): "25",
    ("codex", "enterprise"): "60",
}


#: Tools we have a seat price for (a seat source must map to one of these).
KNOWN_TOOLS = {tool for (tool, _plan) in _SEAT_PRICES}


def known_plans(tool: str) -> list[str]:
    return sorted(plan for (t, plan) in _SEAT_PRICES if t == tool)


def seat_price(tool: str, plan: str) -> Decimal:
    """Monthly USD price for one seat, or 0 for an unknown (tool, plan)."""
    rate = _SEAT_PRICES.get((tool, plan))
    return Decimal(rate) if rate is not None else Decimal("0")


def is_seat_priced(tool: str, plan: Optional[str]) -> bool:
    return plan is not None and (tool, plan) in _SEAT_PRICES
