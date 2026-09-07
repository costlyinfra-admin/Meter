"""Cost-optimization estimator (heuristic, transparent rules)."""

from __future__ import annotations

from meter import optimize


def _names(result):
    return [o["opportunity"] for o in result["opportunities"]]


def test_no_inference_yields_no_opportunities():
    result = optimize.estimate(0.0, 0.0, 0.0, 0)
    assert result["opportunities"] == []
    assert result["monthly_savings"] == 0.0
    assert result["annual_savings"] == 0.0


def test_input_heavy_high_volume_surfaces_the_right_levers():
    # Input-heavy, high request volume. (Model downgrade is now a grounded MEASURED
    # opportunity, no longer a flat heuristic here.)
    result = optimize.estimate(total=4200.0, input_cost=3700.0, output_cost=500.0, requests=320_000)
    names = _names(result)
    assert "Model downgrade" not in names  # moved to the measured tier
    assert "Prompt caching" in names  # input-heavy
    assert "Semantic caching" in names  # high volume
    # Annual is exactly 12x monthly, and the total is bounded well below spend.
    assert result["annual_savings"] == round(result["monthly_savings"] * 12, 2)
    assert 0 < result["monthly_savings"] < result_total_guard(4200.0)


def result_total_guard(total: float) -> float:
    # Conservative: combined estimate should never approach the full bill.
    return total * 0.6


def test_caching_confidence_scales_with_input_share():
    high = optimize.estimate(1000.0, 900.0, 100.0, 0)  # 90% input
    med = optimize.estimate(1000.0, 550.0, 450.0, 0)  # 55% input
    caching_high = next(o for o in high["opportunities"] if o["opportunity"] == "Prompt caching")
    caching_med = next(o for o in med["opportunities"] if o["opportunity"] == "Prompt caching")
    assert caching_high["confidence"] == "high"
    assert caching_med["confidence"] == "med"


def test_low_volume_omits_semantic_caching():
    result = optimize.estimate(1000.0, 700.0, 300.0, requests=1_000)
    assert "Semantic caching" not in _names(result)


def test_every_opportunity_carries_a_rationale_and_confidence():
    result = optimize.estimate(4200.0, 3700.0, 500.0, 320_000)
    for o in result["opportunities"]:
        assert o["rationale"]
        assert o["confidence"] in {"high", "med", "low"}
        assert o["savings"] >= 1
