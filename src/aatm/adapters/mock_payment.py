"""Mock payment adapter: charge, query, refund.

``charge_payment`` is the canonical Tier-3 (irreversible externally) action and
the default pivot. Its recovery is never an "undo": a refund is a NEW, separate
transaction (forward/business-level reversal).

The adapter enforces idempotency by ``intent_id``: a repeated charge for the same
intent returns the SAME transaction instead of creating a duplicate. This is what
prevents double charges under crash/retry.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from ..enums import Outcome
from ..models import ActionIntent, OutcomeQuery, ToolResult, VerificationResult
from .base import BaseAdapter


class PaymentAdapter(BaseAdapter):
    tools = ("charge_payment", "query_payment", "refund_payment")

    async def _do_execute(self, intent: ActionIntent) -> ToolResult:
        tool = intent.tool_name
        if tool == "charge_payment":
            return self._charge(intent)
        if tool == "query_payment":
            return self._query(intent)
        if tool == "refund_payment":
            return self._refund(intent)
        return ToolResult(
            intent_id=intent.intent_id,
            outcome=Outcome.FAILURE,
            error_message=f"unknown payment tool: {tool}",
        )

    def _charge(self, intent: ActionIntent) -> ToolResult:
        # Idempotency by intent_id: never double-charge the same intent.
        existing = self._find_payment_by_intent(str(intent.intent_id))
        if existing is not None:
            data = self._payment_data(existing)
            return ToolResult(intent_id=intent.intent_id, outcome=Outcome.SUCCESS,
                              data=data)

        txn_id = self.world.next_id("PAY")
        amount = intent.parameters.get("amount", 0)
        currency = intent.parameters.get("currency", "INR")
        record = {
            "id": txn_id,
            "status": "captured",
            "amount": amount,
            "currency": currency,
            "intent_id": str(intent.intent_id),
            "refunded": False,
        }
        self.world.payments[txn_id] = record
        data = self._payment_data(record)
        self.world.record_effect(str(intent.intent_id), "payment", txn_id, data)
        return ToolResult(intent_id=intent.intent_id, outcome=Outcome.SUCCESS, data=data)

    def _query(self, intent: ActionIntent) -> ToolResult:
        target = intent.parameters.get("transaction_id") or intent.parameters.get(
            "payment_id"
        )
        record = self.world.payments.get(target) if target else None
        if record is None:
            return ToolResult(
                intent_id=intent.intent_id,
                outcome=Outcome.FAILURE,
                error_message=f"payment '{target}' not found",
            )
        return ToolResult(
            intent_id=intent.intent_id, outcome=Outcome.SUCCESS,
            data=self._payment_data(record),
        )

    def _refund(self, intent: ActionIntent) -> ToolResult:
        # A refund is a NEW transaction against an existing captured payment.
        target = (
            intent.parameters.get("transaction_id")
            or intent.parameters.get("payment_id")
        )
        if not target and self.world.payments:
            # Fall back to the most recent captured payment.
            for pid, rec in reversed(list(self.world.payments.items())):
                if rec["status"] == "captured" and not rec["refunded"]:
                    target = pid
                    break
        record = self.world.payments.get(target) if target else None
        if record is None:
            return ToolResult(
                intent_id=intent.intent_id,
                outcome=Outcome.FAILURE,
                error_message=f"cannot refund: payment '{target}' not found",
            )
        # Idempotent refund by intent.
        existing = self._find_refund_by_intent(str(intent.intent_id))
        if existing is not None:
            return ToolResult(
                intent_id=intent.intent_id, outcome=Outcome.SUCCESS,
                data={"refund_id": existing["id"], "status": "refunded",
                      "payment_id": existing["payment_id"]},
            )
        refund_id = self.world.next_id("RF")
        refund = {
            "id": refund_id,
            "payment_id": target,
            "amount": record["amount"],
            "status": "refunded",
            "intent_id": str(intent.intent_id),
        }
        self.world.refunds[refund_id] = refund
        record["refunded"] = True
        record["status"] = "refunded"  # business state after reversal
        data = {"refund_id": refund_id, "status": "refunded", "payment_id": target,
                "amount": record["amount"]}
        self.world.record_effect(str(intent.intent_id), "refund", refund_id, data)
        return ToolResult(intent_id=intent.intent_id, outcome=Outcome.SUCCESS, data=data)

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _payment_data(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "transaction_id": record["id"],
            "payment_id": record["id"],
            "status": record["status"],
            "payment_status": record["status"],
            "amount": record["amount"],
            "currency": record["currency"],
        }

    def _find_payment_by_intent(self, intent_id: str) -> dict[str, Any] | None:
        for rec in self.world.payments.values():
            if rec.get("intent_id") == intent_id:
                return rec
        return None

    def _find_refund_by_intent(self, intent_id: str) -> dict[str, Any] | None:
        for rec in self.world.refunds.values():
            if rec.get("intent_id") == intent_id:
                return rec
        return None

    def _record_hidden_effect(self, intent: ActionIntent) -> None:
        """For unknown-outcome-with-effect: apply the charge server-side."""
        if intent.tool_name == "charge_payment":
            self._charge(intent)

    async def query_status(self, intent_id: UUID) -> OutcomeQuery:
        """Query by intent_id whether a charge actually happened server-side.

        This is the crash-recovery safety net: even if AATM never saw a response,
        the payment system can be asked authoritatively.
        """
        record = self._find_payment_by_intent(str(intent_id))
        if record is None:
            return OutcomeQuery(
                intent_id=intent_id,
                found=False,
                outcome=Outcome.FAILURE,
                detail="no payment found for intent (safe to treat as not charged)",
            )
        return OutcomeQuery(
            intent_id=intent_id,
            found=True,
            outcome=Outcome.SUCCESS,
            data=self._payment_data(record),
            detail=f"payment {record['id']} is {record['status']}",
        )

    def _postcondition(self, intent: ActionIntent,
                       state: dict[str, Any]) -> VerificationResult:
        result = state.get("result", {}) or {}
        if intent.tool_name == "charge_payment":
            txn_id = result.get("transaction_id")
            record = self.world.payments.get(txn_id) if txn_id else None
            ok = record is not None and record["status"] in {"captured", "refunded"}
            return VerificationResult(
                passed=ok,
                expression="payment.status == 'captured'",
                detail="payment captured" if ok else "no captured payment (phantom?)",
                observed=result,
            )
        if intent.tool_name == "refund_payment":
            refund_id = result.get("refund_id")
            ok = bool(refund_id) and refund_id in self.world.refunds
            return VerificationResult(
                passed=ok,
                expression="refund exists and status == 'refunded'",
                detail="refund verified" if ok else "refund not verified",
            )
        return VerificationResult(passed=True, detail="no postcondition")
