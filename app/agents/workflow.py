from __future__ import annotations

import re
from copy import deepcopy
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from app.agents.state import BookingAgentState
from app.core.exceptions import InvalidStateTransition

REQUIRED_BOOKING_FIELDS = [
    "service_id", "pet_name", "pet_type", "appointment_date",
    "appointment_time", "customer_name", "phone",
]
FIELD_QUESTIONS = {
    "service_id": "请问想预约哪项服务？",
    "pet_name": "请问宠物叫什么名字？",
    "pet_type": "请问宠物是猫、狗，还是其他类型？",
    "appointment_date": "请问想预约哪一天？",
    "appointment_time": "请问想预约哪个时间？",
    "customer_name": "请问怎么称呼您？",
    "phone": "请提供用于确认预约的11位手机号。",
}
CONFIRM_PHRASES = {
    "确认", "确认预约", "是的", "是的确认", "没问题确认", "yes", "confirm", "yes confirm",
    "yes confirm it", "yes cancel it", "yes reschedule it",
}
REJECT_PHRASES = {"不确认", "取消", "不要了", "no", "reject"}


def _normalized_text(text: str) -> str:
    return re.sub(r"[\s，。！？,.!?]", "", text).lower()


def classify_intent(text: str) -> str:
    value = _normalized_text(text)
    if re.search(r"取消|撤销|cancel", value):
        return "cancel"
    if re.search(r"改期|改时间|换时间|reschedule|change.*appointment|move.*appointment", value):
        return "reschedule"
    if re.search(r"查询|查一下|查预约|我的预约|lookup|status", value):
        return "lookup"
    if re.search(r"营业|地址|几点开门|几点关门|business", value):
        return "business_info"
    if re.search(r"多少钱|价格|服务项目|有哪些服务|price", value):
        return "service_info"
    if re.search(r"预约|预订|订一个|book|appointment|grooming", value):
        return "create"
    return "unknown"


def classify_confirmation(text: str) -> str:
    value = _normalized_text(text)
    if value in {_normalized_text(item) for item in CONFIRM_PHRASES}:
        return "confirmed"
    if re.fullmatch(r"(?:yes|是的)(?:confirm|确认|cancel|取消|reschedule|改期)(?:it|预约)?", value):
        return "confirmed"
    if value in {_normalized_text(item) for item in REJECT_PHRASES}:
        return "rejected"
    if re.search(r"改|换|调整|不是|修改", value):
        return "modifying"
    return "pending"


def _latest_text(state: BookingAgentState) -> str:
    if not state.get("messages"):
        return ""
    message = state["messages"][-1]
    if isinstance(message, dict):
        return str(message.get("content", ""))
    return str(getattr(message, "content", message))


def _event(state: BookingAgentState, node: str, **details: Any) -> list[dict[str, Any]]:
    return [*state.get("trace", []), {"node": node, **details}]


def _understand_request(state: BookingAgentState) -> dict[str, Any]:
    text = _latest_text(state)
    detected = classify_intent(text)
    intent = detected if detected != "unknown" or not state.get("intent") else state["intent"]
    confirmation = classify_confirmation(text) if intent in {"create", "reschedule", "cancel"} else "pending"
    return {
        "intent": intent,
        "confirmation_status": confirmation,
        "error": None,
        "trace": _event(state, "understand_request", intent=intent, confirmation=confirmation),
    }


def _collect_booking_info(state: BookingAgentState) -> dict[str, Any]:
    original = deepcopy(state.get("booking_draft", {}))
    updates = {
        key: value for key, value in state.get("draft_updates", {}).items() if value not in (None, "")
    }
    draft = {**original, **updates}
    changed = {key for key, value in updates.items() if original.get(key) != value}
    availability = list(state.get("availability", []))
    selected = state.get("selected_slot")
    confirmation = state.get("confirmation_status", "pending")
    if changed & {"appointment_date", "appointment_time"}:
        availability = []
        selected = None
        confirmation = "pending"
    if "service_id" in changed:
        for field in ("service_price", "service_duration"):
            draft.pop(field, None)
        availability = []
        selected = None
        confirmation = "pending"
    missing = [field for field in REQUIRED_BOOKING_FIELDS if not draft.get(field)]
    return {
        "booking_draft": draft,
        "draft_updates": {},
        "missing_fields": missing,
        "availability": availability,
        "selected_slot": selected,
        "confirmation_status": confirmation,
        "trace": _event(state, "collect_booking_info", changed=sorted(changed), missing=missing),
    }


def _route_after_collection(state: BookingAgentState) -> str:
    if state.get("booking_result"):
        return "completed"
    if state.get("intent") in {"reschedule", "cancel"}:
        return "find_booking"
    if state.get("intent") != "create":
        return "route_non_create"
    return "ask_for_missing_info" if state.get("missing_fields") else "check_availability"


def _ask_for_missing_info(state: BookingAgentState) -> dict[str, Any]:
    field = state["missing_fields"][0]
    return {
        "stage": "collecting",
        "next_question": FIELD_QUESTIONS[field],
        "trace": _event(state, "ask_for_missing_info", field=field),
    }


def _route_non_create(state: BookingAgentState) -> dict[str, Any]:
    intent = state.get("intent", "unknown")
    stage_by_intent = {
        "lookup": "lookup",
        "reschedule": "reschedule",
        "cancel": "cancel",
        "business_info": "business_info",
        "service_info": "service_info",
        "unknown": "unknown",
    }
    return {
        "stage": stage_by_intent.get(intent, "unknown"),
        "trace": _event(state, "route_non_create", intent=intent),
    }


def _suggest_alternatives(state: BookingAgentState) -> dict[str, Any]:
    slots = state.get("availability", [])
    text = "、".join(slots[:3]) if slots else "其他日期"
    return {
        "stage": "suggesting_alternatives",
        "next_question": f"原时段不可用，可选 {text}，您希望哪个时段？",
        "confirmation_status": "pending",
        "trace": _event(state, "suggest_alternatives", slots=slots),
    }


def _await_confirmation(state: BookingAgentState) -> dict[str, Any]:
    status = state.get("confirmation_status", "pending")
    if status == "rejected":
        stage, question = "cancelled_by_customer", "好的，本次不会创建预约。"
    elif status == "modifying":
        stage, question = "collecting", "好的，请告诉我需要修改哪项信息。"
    else:
        stage, question = "awaiting_confirmation", "信息已齐全，请明确回复“确认预约”后我再提交。"
    return {
        "stage": stage,
        "next_question": question,
        "trace": _event(state, "await_confirmation", confirmation=status),
    }


def _route_confirmation(state: BookingAgentState) -> str:
    return "create_booking" if can_create_booking(state) else END


def can_create_booking(state: BookingAgentState) -> bool:
    return (
        state.get("stage") == "awaiting_confirmation"
        and state.get("confirmation_status") == "confirmed"
        and not state.get("missing_fields")
        and state.get("selected_slot") is not None
        and not state.get("booking_result")
    )


def can_reschedule_booking(state: BookingAgentState) -> bool:
    return (
        state.get("stage") == "awaiting_reschedule_confirmation"
        and state.get("confirmation_status") == "confirmed"
        and state.get("target_booking") is not None
        and state.get("selected_slot") is not None
        and not state.get("booking_result")
    )


def can_cancel_booking(state: BookingAgentState) -> bool:
    return (
        state.get("stage") == "awaiting_cancel_confirmation"
        and state.get("confirmation_status") == "confirmed"
        and state.get("target_booking") is not None
        and not state.get("booking_result")
    )


def _completed(state: BookingAgentState) -> dict[str, Any]:
    return {"stage": "completed", "trace": _event(state, "completed")}


def build_booking_workflow(
    availability_checker: Callable[[str, str], list[str]],
    booking_creator: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    booking_finder: Callable[[str, str], list[dict[str, Any]]] | None = None,
    booking_rescheduler: Callable[[str, str, str, str], dict[str, Any]] | None = None,
    booking_canceller: Callable[[str, str], dict[str, Any]] | None = None,
):
    """Compile the deterministic booking control plane around injected business services."""

    def check_availability(state: BookingAgentState) -> dict[str, Any]:
        draft = state["booking_draft"]
        slots = availability_checker(draft["appointment_date"], draft["service_id"])
        selected = draft.get("appointment_time") if draft.get("appointment_time") in slots else None
        return {
            "availability": slots,
            "selected_slot": selected,
            "trace": _event(state, "check_availability", slots=slots, selected=selected),
        }

    def route_availability(state: BookingAgentState) -> str:
        return "await_confirmation" if state.get("selected_slot") else "suggest_alternatives"

    def create_booking(state: BookingAgentState) -> dict[str, Any]:
        if state.get("booking_result"):
            return {"trace": _event(state, "create_booking", replay=True)}
        if not can_create_booking(state):
            raise InvalidStateTransition("create booking requires explicit graph confirmation")
        try:
            result = booking_creator(deepcopy(state["booking_draft"]))
            return {
                "booking_result": result,
                "error": None,
                "trace": _event(state, "create_booking", booking_code=result.get("booking_code")),
            }
        except Exception as error:  # service exceptions become auditable graph state
            return {
                "stage": "failed",
                "error": str(error),
                "trace": _event(state, "create_booking", error=type(error).__name__),
            }

    def find_booking(state: BookingAgentState) -> dict[str, Any]:
        draft = state.get("booking_draft", {})
        code = str(draft.get("booking_code", ""))
        phone = str(draft.get("phone", ""))
        if booking_finder is None or (not code and not phone):
            return {
                "found_bookings": [],
                "target_booking": None,
                "stage": "awaiting_booking_lookup",
                "next_question": "请提供预约编号或预约手机号。",
                "trace": _event(state, "find_booking", count=0),
            }
        found = booking_finder(code, phone)
        if len(found) != 1:
            stage = "awaiting_booking_selection" if found else "booking_not_found"
            return {
                "found_bookings": found,
                "target_booking": None,
                "stage": stage,
                "next_question": "请明确选择要操作的预约。" if found else "没有找到匹配的预约。",
                "trace": _event(state, "find_booking", count=len(found)),
            }
        target = found[0]
        hydrated = dict(draft)
        for key in (
            "booking_code", "phone", "service_id", "service_name", "pet_name",
            "pet_type", "customer_name",
        ):
            if target.get(key) not in (None, ""):
                hydrated.setdefault(key, target[key])
        return {
            "found_bookings": found,
            "target_booking": target,
            "booking_draft": hydrated,
            "trace": _event(state, "find_booking", count=1, booking_code=target.get("booking_code")),
        }

    def route_found_booking(state: BookingAgentState) -> str:
        if state.get("target_booking") is None:
            return END
        return "await_cancel_confirmation" if state.get("intent") == "cancel" else "check_reschedule_availability"

    def check_reschedule_availability(state: BookingAgentState) -> dict[str, Any]:
        draft = state["booking_draft"]
        if not draft.get("appointment_date") or not draft.get("appointment_time"):
            return {
                "stage": "collecting_reschedule_slot",
                "selected_slot": None,
                "availability": [],
                "trace": _event(state, "check_reschedule_availability", missing=True),
            }
        slots = availability_checker(draft["appointment_date"], draft["service_id"])
        selected = draft["appointment_time"] if draft["appointment_time"] in slots else None
        return {
            "availability": slots,
            "selected_slot": selected,
            "trace": _event(state, "check_reschedule_availability", slots=slots, selected=selected),
        }

    def route_reschedule_availability(state: BookingAgentState) -> str:
        if state.get("stage") == "collecting_reschedule_slot":
            return END
        return "await_reschedule_confirmation" if state.get("selected_slot") else "suggest_alternatives"

    def await_reschedule_confirmation(state: BookingAgentState) -> dict[str, Any]:
        draft = state["booking_draft"]
        return {
            "stage": "awaiting_reschedule_confirmation",
            "next_question": (
                f"是否将预约改到 {draft['appointment_date']} "
                f"{draft['appointment_time']}？请明确回复“确认改期”。"
            ),
            "trace": _event(state, "await_reschedule_confirmation"),
        }

    def route_reschedule_confirmation(state: BookingAgentState) -> str:
        return "reschedule_booking" if can_reschedule_booking(state) else END

    def reschedule_booking(state: BookingAgentState) -> dict[str, Any]:
        if not can_reschedule_booking(state) or booking_rescheduler is None:
            raise InvalidStateTransition("reschedule requires explicit graph confirmation")
        target, draft = state["target_booking"], state["booking_draft"]
        result = booking_rescheduler(
            target["booking_code"], target["phone"], draft["appointment_date"], draft["appointment_time"]
        )
        return {
            "booking_result": result,
            "error": None,
            "trace": _event(state, "reschedule_booking", booking_code=result.get("booking_code")),
        }

    def await_cancel_confirmation(state: BookingAgentState) -> dict[str, Any]:
        target = state["target_booking"]
        return {
            "stage": "awaiting_cancel_confirmation",
            "next_question": (
                f"是否取消 {target['appointment_date']} {target['appointment_time']} 的"
                f"{target['service_name']}预约？请明确回复“确认取消”。"
            ),
            "trace": _event(state, "await_cancel_confirmation"),
        }

    def route_cancel_confirmation(state: BookingAgentState) -> str:
        return "cancel_booking" if can_cancel_booking(state) else END

    def cancel_booking(state: BookingAgentState) -> dict[str, Any]:
        if not can_cancel_booking(state) or booking_canceller is None:
            raise InvalidStateTransition("cancel requires explicit graph confirmation")
        target = state["target_booking"]
        result = booking_canceller(target["booking_code"], target["phone"])
        return {
            "booking_result": result,
            "error": None,
            "trace": _event(state, "cancel_booking", booking_code=result.get("booking_code")),
        }

    builder = StateGraph(BookingAgentState)
    builder.add_node("understand_request", _understand_request)
    builder.add_node("collect_booking_info", _collect_booking_info)
    builder.add_node("ask_for_missing_info", _ask_for_missing_info)
    builder.add_node("route_non_create", _route_non_create)
    builder.add_node("check_availability", check_availability)
    builder.add_node("suggest_alternatives", _suggest_alternatives)
    builder.add_node("await_confirmation", _await_confirmation)
    builder.add_node("create_booking", create_booking)
    builder.add_node("find_booking", find_booking)
    builder.add_node("check_reschedule_availability", check_reschedule_availability)
    builder.add_node("await_reschedule_confirmation", await_reschedule_confirmation)
    builder.add_node("reschedule_booking", reschedule_booking)
    builder.add_node("await_cancel_confirmation", await_cancel_confirmation)
    builder.add_node("cancel_booking", cancel_booking)
    builder.add_node("completed", _completed)
    builder.add_edge(START, "understand_request")
    builder.add_edge("understand_request", "collect_booking_info")
    builder.add_conditional_edges(
        "collect_booking_info",
        _route_after_collection,
        {
            "ask_for_missing_info": "ask_for_missing_info",
            "check_availability": "check_availability",
            "route_non_create": "route_non_create",
            "find_booking": "find_booking",
            "completed": "completed",
        },
    )
    builder.add_edge("ask_for_missing_info", END)
    builder.add_edge("route_non_create", END)
    builder.add_conditional_edges(
        "find_booking",
        route_found_booking,
        {
            "await_cancel_confirmation": "await_cancel_confirmation",
            "check_reschedule_availability": "check_reschedule_availability",
            END: END,
        },
    )
    builder.add_conditional_edges(
        "check_reschedule_availability",
        route_reschedule_availability,
        {
            "await_reschedule_confirmation": "await_reschedule_confirmation",
            "suggest_alternatives": "suggest_alternatives",
            END: END,
        },
    )
    builder.add_conditional_edges(
        "await_reschedule_confirmation",
        route_reschedule_confirmation,
        {"reschedule_booking": "reschedule_booking", END: END},
    )
    builder.add_edge("reschedule_booking", "completed")
    builder.add_conditional_edges(
        "await_cancel_confirmation",
        route_cancel_confirmation,
        {"cancel_booking": "cancel_booking", END: END},
    )
    builder.add_edge("cancel_booking", "completed")
    builder.add_conditional_edges(
        "check_availability",
        route_availability,
        {"await_confirmation": "await_confirmation", "suggest_alternatives": "suggest_alternatives"},
    )
    builder.add_edge("suggest_alternatives", END)
    builder.add_conditional_edges(
        "await_confirmation", _route_confirmation, {"create_booking": "create_booking", END: END}
    )
    builder.add_edge("create_booking", "completed")
    builder.add_edge("completed", END)
    return builder.compile()


def evaluate_booking_flow(draft: dict[str, Any]) -> dict[str, Any]:
    """Compatibility projection used by the existing UI and API."""
    missing = [field for field in REQUIRED_BOOKING_FIELDS if not draft.get(field)]
    if draft.get("status") == "cancelled":
        stage = "cancelled"
    elif draft.get("status") == "confirmed" and draft.get("booking_code"):
        stage = "booked"
    elif missing:
        stage = "collecting"
    else:
        stage = "awaiting_confirmation"
    return {"stage": stage, "missing_fields": missing, "ready_for_confirmation": not missing}
