from __future__ import annotations

from typing import Any, Literal, TypedDict

Intent = Literal[
    "create", "lookup", "reschedule", "cancel", "business_info", "service_info", "unknown"
]
ConfirmationStatus = Literal["pending", "confirmed", "rejected", "modifying"]


class BookingAgentState(TypedDict, total=False):
    """Single auditable source of truth for one booking conversation."""

    messages: list[Any]
    thread_id: str
    intent: Intent
    booking_draft: dict[str, Any]
    draft_updates: dict[str, Any]
    missing_fields: list[str]
    availability: list[str]
    selected_slot: str | None
    confirmation_status: ConfirmationStatus
    booking_result: dict[str, Any] | None
    stage: str
    error: str | None
    next_question: str
    trace: list[dict[str, Any]]
