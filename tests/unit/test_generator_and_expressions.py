"""Unit tests for the advisory compensation generator + safe expression eval."""

from __future__ import annotations

from aatm.compensation.generator import CompensationGenerator
from aatm.enums import CompensationSource, CompensationStrategy, Reversibility
from aatm.verification.expressions import bind_parameters, evaluate


def test_generator_compensatable_is_advisory():
    gen = CompensationGenerator()
    plan = gen.suggest("book_flight", reversibility=Reversibility.COMPENSATABLE,
                       returns_schema={"properties": {"booking_id": {}}})
    assert plan.tool == "cancel_flight"
    assert plan.source == CompensationSource.LLM
    assert plan.requires_approval is True
    assert plan.confidence < 0.6  # never high-confidence


def test_generator_unknown_is_manual_escalation():
    gen = CompensationGenerator()
    plan = gen.suggest("frobnicate_widget", reversibility=Reversibility.UNKNOWN)
    assert plan.strategy == CompensationStrategy.MANUAL_ESCALATION
    assert plan.confidence == 0.0
    assert plan.requires_approval is True
    assert plan.source == CompensationSource.LLM


def test_generator_irreversible_payment_forward_fix_advisory():
    gen = CompensationGenerator()
    plan = gen.suggest("charge_card", reversibility=Reversibility.IRREVERSIBLE)
    assert plan.strategy == CompensationStrategy.FORWARD_FIX
    assert plan.tool == "refund_card"
    assert plan.requires_approval is True


def test_evaluate_equality():
    ctx = {"result": {"booking_status": "confirmed"}}
    assert evaluate("result.booking_status == 'confirmed'", ctx) is True
    assert evaluate("result.booking_status == 'cancelled'", ctx) is False


def test_evaluate_boolean_and_numeric():
    ctx = {"result": {"all_confirmed": True, "count": 3}}
    assert evaluate("result.all_confirmed == true", ctx) is True
    assert evaluate("result.count >= 3", ctx) is True
    assert evaluate("result.count > 3", ctx) is False


def test_evaluate_missing_path_is_false():
    assert evaluate("result.missing == 'x'", {"result": {}}) is False


def test_evaluate_bare_truthy():
    assert evaluate("result.ok", {"result": {"ok": True}}) is True
    assert evaluate("result.ok", {"result": {"ok": False}}) is False


def test_bind_parameters_resolves_and_flags_unresolved():
    ctx = {"result": {"booking_id": "BK-1"}, "params": {"to": "a@b.com"},
           "variables": {}}
    bound, unresolved = bind_parameters(
        {"booking_id": "result.booking_id", "to": "params.to"}, ctx
    )
    assert bound == {"booking_id": "BK-1", "to": "a@b.com"}
    assert unresolved == []

    bound2, unresolved2 = bind_parameters(
        {"booking_id": "result.missing_id"}, ctx
    )
    assert "booking_id <- result.missing_id" in unresolved2
