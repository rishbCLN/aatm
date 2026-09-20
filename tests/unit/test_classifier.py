"""Unit tests for the reversibility classifier (fail-closed behavior)."""

from __future__ import annotations

from aatm.enums import Reversibility, RiskLevel, Tier
from aatm.planner.reversibility import ReversibilityClassifier


def test_known_tier1(tmp_config):
    c = ReversibilityClassifier(tmp_config)
    result = c.classify("confirm_bookings")
    assert result.reversibility == Reversibility.FULLY_REVERSIBLE
    assert result.tier == Tier.ONE
    assert result.source == "registry"


def test_known_tier2(tmp_config):
    c = ReversibilityClassifier(tmp_config)
    result = c.classify("book_flight")
    assert result.reversibility == Reversibility.COMPENSATABLE
    assert result.tier == Tier.TWO


def test_known_tier3(tmp_config):
    c = ReversibilityClassifier(tmp_config)
    result = c.classify("charge_payment")
    assert result.reversibility == Reversibility.IRREVERSIBLE
    assert result.tier == Tier.THREE
    assert result.risk_level == RiskLevel.CRITICAL


def test_explicit_override_wins(tmp_config):
    c = ReversibilityClassifier(tmp_config)
    # Registry says compensatable, but explicit says irreversible.
    result = c.classify(
        "book_flight",
        explicit={"reversibility": "irreversible", "side_effect_scope": "external"},
    )
    assert result.reversibility == Reversibility.IRREVERSIBLE
    assert result.tier == Tier.THREE
    assert result.source == "explicit"


def test_unknown_tool_fails_closed(tmp_config):
    c = ReversibilityClassifier(tmp_config)
    result = c.classify("launch_missiles")
    assert result.reversibility == Reversibility.UNKNOWN
    assert result.tier == Tier.THREE
    assert result.risk_level == RiskLevel.CRITICAL
    assert result.approval_required is True
    assert result.source == "fail_closed"
    assert c.is_known("launch_missiles") is False
