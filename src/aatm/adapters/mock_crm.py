"""Mock CRM adapter: create + delete database record.

Internal DB writes are Tier-2 (compensatable): a created record can be deleted
(a semantic inverse) as long as it still exists.
"""

from __future__ import annotations

from typing import Any

from ..enums import Outcome
from ..models import ActionIntent, ToolResult, VerificationResult
from .base import BaseAdapter


class CRMAdapter(BaseAdapter):
    tools = ("create_database_record", "delete_database_record", "update_crm")

    async def _do_execute(self, intent: ActionIntent) -> ToolResult:
        tool = intent.tool_name
        if tool in ("create_database_record", "update_crm"):
            return self._create(intent)
        if tool == "delete_database_record":
            return self._delete(intent)
        return ToolResult(
            intent_id=intent.intent_id,
            outcome=Outcome.FAILURE,
            error_message=f"unknown CRM tool: {tool}",
        )

    def _create(self, intent: ActionIntent) -> ToolResult:
        record_id = self.world.next_id("CRM")
        record = {
            "id": record_id,
            "status": "active",
            "data": dict(intent.parameters),
            "intent_id": str(intent.intent_id),
        }
        self.world.crm_records[record_id] = record
        data = {"record_id": record_id, "status": "active"}
        self.world.record_effect(str(intent.intent_id), "crm", record_id, data)
        return ToolResult(intent_id=intent.intent_id, outcome=Outcome.SUCCESS, data=data)

    def _delete(self, intent: ActionIntent) -> ToolResult:
        target = intent.parameters.get("record_id")
        if not target or target not in self.world.crm_records:
            return ToolResult(
                intent_id=intent.intent_id,
                outcome=Outcome.FAILURE,
                error_message=f"cannot delete: record '{target}' not found",
            )
        self.world.crm_records[target]["status"] = "deleted"
        data = {"record_id": target, "status": "deleted"}
        self.world.record_effect(str(intent.intent_id), "crm_delete", target, data)
        return ToolResult(intent_id=intent.intent_id, outcome=Outcome.SUCCESS, data=data)

    def _record_hidden_effect(self, intent: ActionIntent) -> None:
        if intent.tool_name in ("create_database_record", "update_crm"):
            self._create(intent)

    def _postcondition(self, intent: ActionIntent,
                       state: dict[str, Any]) -> VerificationResult:
        result = state.get("result", {}) or {}
        if intent.tool_name in ("create_database_record", "update_crm"):
            record_id = result.get("record_id")
            ok = bool(record_id) and record_id in self.world.crm_records
            return VerificationResult(
                passed=ok,
                expression="crm record exists",
                detail="record created" if ok else "no record (phantom?)",
                observed=result,
            )
        if intent.tool_name == "delete_database_record":
            record_id = result.get("record_id")
            ok = (
                bool(record_id)
                and self.world.crm_records.get(record_id, {}).get("status") == "deleted"
            )
            return VerificationResult(
                passed=ok,
                expression="crm record deleted",
                detail="record deleted" if ok else "record not deleted",
            )
        return VerificationResult(passed=True, detail="no postcondition")
