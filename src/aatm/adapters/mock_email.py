"""Mock email adapter: send + corrective email.

Email is Tier-3 (irreversible externally): you cannot un-send. The "compensation"
for a wrong email is a corrective/forward email, NOT an undo. The adapter models
this distinction explicitly.
"""

from __future__ import annotations

from typing import Any

from ..enums import Outcome
from ..models import ActionIntent, ToolResult, VerificationResult
from .base import BaseAdapter


class EmailAdapter(BaseAdapter):
    tools = ("send_email", "send_correction_email")

    async def _do_execute(self, intent: ActionIntent) -> ToolResult:
        if intent.tool_name == "send_email":
            return self._send(intent, kind="email")
        if intent.tool_name == "send_correction_email":
            return self._send(intent, kind="correction")
        return ToolResult(
            intent_id=intent.intent_id,
            outcome=Outcome.FAILURE,
            error_message=f"unknown email tool: {intent.tool_name}",
        )

    def _send(self, intent: ActionIntent, kind: str) -> ToolResult:
        email_id = self.world.next_id("EM")
        record = {
            "id": email_id,
            "type": kind,
            "to": intent.parameters.get("to", "traveler@example.com"),
            "subject": intent.parameters.get(
                "subject", "Booking confirmation" if kind == "email" else "Correction"
            ),
            "intent_id": str(intent.intent_id),
            "status": "sent",
        }
        self.world.emails[email_id] = record
        data = {"email_id": email_id, "status": "sent", "type": kind}
        self.world.record_effect(str(intent.intent_id), kind, email_id, data)
        return ToolResult(intent_id=intent.intent_id, outcome=Outcome.SUCCESS, data=data)

    def _record_hidden_effect(self, intent: ActionIntent) -> None:
        kind = "email" if intent.tool_name == "send_email" else "correction"
        self._send(intent, kind=kind)

    def _postcondition(self, intent: ActionIntent,
                       state: dict[str, Any]) -> VerificationResult:
        result = state.get("result", {}) or {}
        email_id = result.get("email_id")
        ok = bool(email_id) and email_id in self.world.emails
        return VerificationResult(
            passed=ok,
            expression="email exists and status == 'sent'",
            detail="email recorded" if ok else "no email record (phantom?)",
            observed=result,
        )
