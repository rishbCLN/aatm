"""Mock travel adapters: flights, hotels, cars (book + cancel).

All state lives in the shared :class:`MockWorldState`. These are Tier-2
(compensatable) side effects: a booking can be cancelled via a semantic inverse.
"""

from __future__ import annotations

from typing import Any

from ..enums import Outcome
from ..models import ActionIntent, ToolResult, VerificationResult
from .base import BaseAdapter


class TravelAdapter(BaseAdapter):
    """Handles book/cancel for flight, hotel and car reservations."""

    tools = (
        "book_flight",
        "cancel_flight",
        "book_hotel",
        "cancel_hotel",
        "reserve_car",
        "cancel_car",
        "confirm_bookings",
    )

    _KIND = {
        "book_flight": ("flights", "FL", "flight"),
        "book_hotel": ("hotels", "HT", "hotel"),
        "reserve_car": ("cars", "CR", "car"),
    }
    _CANCEL = {
        "cancel_flight": "flights",
        "cancel_hotel": "hotels",
        "cancel_car": "cars",
    }

    async def _do_execute(self, intent: ActionIntent) -> ToolResult:
        tool = intent.tool_name
        if tool in self._KIND:
            return self._book(intent)
        if tool in self._CANCEL:
            return self._cancel(intent)
        if tool == "confirm_bookings":
            return self._confirm(intent)
        return ToolResult(
            intent_id=intent.intent_id,
            outcome=Outcome.FAILURE,
            error_message=f"unknown travel tool: {tool}",
        )

    def _book(self, intent: ActionIntent) -> ToolResult:
        collection, prefix, kind = self._KIND[intent.tool_name]
        store = getattr(self.world, collection)
        booking_id = self.world.next_id(prefix)
        record = {
            "id": booking_id,
            "status": "confirmed",
            "params": dict(intent.parameters),
            "intent_id": str(intent.intent_id),
        }
        store[booking_id] = record
        data = {
            "booking_id": booking_id,
            f"{kind}_id": booking_id,
            "booking_status": "confirmed",
            "status": "confirmed",
        }
        self.world.record_effect(str(intent.intent_id), kind, booking_id, data)
        return ToolResult(intent_id=intent.intent_id, outcome=Outcome.SUCCESS, data=data)

    def _cancel(self, intent: ActionIntent) -> ToolResult:
        collection = self._CANCEL[intent.tool_name]
        store = getattr(self.world, collection)
        # Find the target: explicit id in params, else the intent's mapped booking.
        target = (
            intent.parameters.get("booking_id")
            or intent.parameters.get("flight_id")
            or intent.parameters.get("hotel_id")
            or intent.parameters.get("car_id")
        )
        if target is None or target not in store:
            return ToolResult(
                intent_id=intent.intent_id,
                outcome=Outcome.FAILURE,
                error_message=f"cannot cancel: booking '{target}' not found",
            )
        store[target]["status"] = "cancelled"
        data = {"booking_id": target, "status": "cancelled", "booking_status": "cancelled"}
        self.world.record_effect(str(intent.intent_id), "cancel", target, data)
        return ToolResult(intent_id=intent.intent_id, outcome=Outcome.SUCCESS, data=data)

    def _confirm(self, intent: ActionIntent) -> ToolResult:
        """Confirm that all active reservations exist and are confirmed."""
        active_flights = [f for f in self.world.flights.values()
                          if f["status"] == "confirmed"]
        active_hotels = [h for h in self.world.hotels.values()
                         if h["status"] == "confirmed"]
        active_cars = [c for c in self.world.cars.values()
                       if c["status"] == "confirmed"]
        all_ok = bool(active_flights and active_hotels and active_cars)
        data = {
            "all_confirmed": all_ok,
            "flights": len(active_flights),
            "hotels": len(active_hotels),
            "cars": len(active_cars),
            "confirmation_status": "confirmed" if all_ok else "incomplete",
        }
        self.world.record_effect(str(intent.intent_id), "confirm", "confirm", data)
        return ToolResult(intent_id=intent.intent_id, outcome=Outcome.SUCCESS, data=data)

    def _postcondition(self, intent: ActionIntent,
                       state: dict[str, Any]) -> VerificationResult:
        tool = intent.tool_name
        result = state.get("result", {}) or {}
        if tool in self._KIND:
            collection, _, _ = self._KIND[tool]
            store = getattr(self.world, collection)
            booking_id = result.get("booking_id")
            # Phantom success => no real booking created in the world.
            if not booking_id or booking_id not in store:
                return VerificationResult(
                    passed=False,
                    expression="booking exists and status == confirmed",
                    detail="no booking record found (possible phantom success)",
                    observed=result,
                )
            ok = store[booking_id]["status"] == "confirmed"
            return VerificationResult(
                passed=ok,
                expression="result.booking_status == 'confirmed'",
                detail="booking confirmed" if ok else "booking not confirmed",
                observed={"status": store[booking_id]["status"]},
            )
        if tool in self._CANCEL:
            booking_id = result.get("booking_id")
            collection = self._CANCEL[tool]
            store = getattr(self.world, collection)
            ok = bool(booking_id) and store.get(booking_id, {}).get("status") == "cancelled"
            return VerificationResult(
                passed=ok,
                expression="booking.status == 'cancelled'",
                detail="cancellation verified" if ok else "cancellation not verified",
            )
        if tool == "confirm_bookings":
            ok = bool(result.get("all_confirmed"))
            return VerificationResult(
                passed=ok,
                expression="result.all_confirmed == true",
                detail="all confirmed" if ok else "confirmation incomplete",
            )
        return VerificationResult(passed=True, detail="no postcondition")
