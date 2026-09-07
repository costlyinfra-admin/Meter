"""Per-seat price book for AI coding tools."""

from __future__ import annotations

from decimal import Decimal

from meter import seatpricing


def test_known_seat_prices():
    assert seatpricing.seat_price("copilot", "business") == Decimal("19")
    assert seatpricing.seat_price("copilot", "enterprise") == Decimal("39")
    assert seatpricing.is_seat_priced("copilot", "business")


def test_unknown_seat_is_zero():
    assert seatpricing.seat_price("copilot", "mystery") == Decimal("0")
    assert not seatpricing.is_seat_priced("copilot", None)
