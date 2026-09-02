from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph


REQUIRED_DRAFT_FIELDS = [
    "service_id",
    "pet_name",
    "pet_type",
    "appointment_date",
    "appointment_time",
    "customer_name",
    "phone",
]


class BookingFlowState(TypedDict, total=False):
    draft: dict[str, Any]
    stage: str
    missing_fields: list[str]
    ready_for_confirmation: bool


def _normalize_draft(state: BookingFlowState) -> dict:
    normalized = {
        key: value.strip() if isinstance(value, str) else value
        for key, value in state.get("draft", {}).items()
        if value not in (None, "")
    }
    return {"draft": normalized}


def _evaluate_stage(state: BookingFlowState) -> dict:
    draft = state.get("draft", {})
    missing = [field for field in REQUIRED_DRAFT_FIELDS if not draft.get(field)]
    if draft.get("status") == "cancelled":
        stage = "cancelled"
    elif draft.get("status") == "confirmed" and draft.get("booking_code"):
        stage = "booked"
    elif missing:
        stage = "collecting"
    else:
        stage = "awaiting_confirmation"
    return {
        "stage": stage,
        "missing_fields": missing,
        "ready_for_confirmation": stage == "awaiting_confirmation",
    }


BOOKING_FLOW = (
    StateGraph(BookingFlowState)
    .add_node("normalize_draft", _normalize_draft)
    .add_node("evaluate_stage", _evaluate_stage)
    .add_edge(START, "normalize_draft")
    .add_edge("normalize_draft", "evaluate_stage")
    .add_edge("evaluate_stage", END)
    .compile()
)


def evaluate_booking_flow(draft: dict[str, Any]) -> dict:
    """Evaluate the deterministic business stage independently of LLM wording."""
    result = BOOKING_FLOW.invoke({"draft": dict(draft)})
    return {
        "stage": result["stage"],
        "missing_fields": result["missing_fields"],
        "ready_for_confirmation": result["ready_for_confirmation"],
    }
