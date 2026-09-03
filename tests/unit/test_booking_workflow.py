from __future__ import annotations

from copy import deepcopy

import pytest

from app.agents.state import BookingAgentState
from app.agents.workflow import REQUIRED_BOOKING_FIELDS, build_booking_workflow, classify_intent

FULL_DRAFT = {
    "service_id": "basic",
    "service_name": "基础洗护",
    "service_price": 88,
    "service_duration": 60,
    "pet_name": "可乐",
    "pet_type": "狗",
    "appointment_date": "2030-01-09",
    "appointment_time": "10:00",
    "customer_name": "陈女士",
    "phone": "13800138000",
}


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("我想预约洗护", "create"),
        ("查一下我的预约", "lookup"),
        ("我要改期", "reschedule"),
        ("取消预约", "cancel"),
        ("你们几点营业", "business_info"),
        ("洗护多少钱", "service_info"),
        ("今天天气如何", "unknown"),
    ],
)
def test_intent_routing(text, intent):
    assert classify_intent(text) == intent


def test_state_contract_contains_auditable_single_source_fields():
    fields = BookingAgentState.__annotations__
    assert {
        "messages", "thread_id", "intent", "booking_draft", "missing_fields",
        "availability", "selected_slot", "confirmation_status", "booking_result",
        "stage", "error",
    } <= set(fields)


def test_missing_information_stops_at_one_question_without_checking_availability():
    checks = []
    graph = build_booking_workflow(lambda *_: checks.append(True) or ["10:00"], lambda _: {})

    result = graph.invoke(
        {"thread_id": "t1", "messages": [{"role": "user", "content": "我要预约"}]}
    )

    assert result["stage"] == "collecting"
    assert result["missing_fields"] == REQUIRED_BOOKING_FIELDS
    assert result["next_question"]
    assert checks == []


def test_ambiguous_confirmation_never_creates_booking():
    writes = []
    graph = build_booking_workflow(lambda *_: ["10:00", "11:30"], lambda draft: writes.append(draft))

    result = graph.invoke(
        {
            "thread_id": "t2",
            "messages": [{"role": "user", "content": "好吧"}],
            "intent": "create",
            "booking_draft": deepcopy(FULL_DRAFT),
        }
    )

    assert result["stage"] == "awaiting_confirmation"
    assert result["confirmation_status"] == "pending"
    assert writes == []


def test_explicit_confirmation_creates_exactly_once():
    writes = []

    def create(draft):
        writes.append(deepcopy(draft))
        return {"booking_code": "PP001", "status": "confirmed"}

    graph = build_booking_workflow(lambda *_: ["10:00"], create)
    initial = {
        "thread_id": "t3",
        "messages": [{"role": "user", "content": "确认预约"}],
        "intent": "create",
        "booking_draft": deepcopy(FULL_DRAFT),
    }
    first = graph.invoke(initial)
    second = graph.invoke({**first, "messages": [{"role": "user", "content": "确认预约"}]})

    assert first["stage"] == "completed"
    assert second["stage"] == "completed"
    assert len(writes) == 1


def test_date_change_invalidates_availability_and_selected_slot():
    graph = build_booking_workflow(lambda day, _service: ["11:30"] if day.endswith("10") else ["10:00"], lambda _: {})
    result = graph.invoke(
        {
            "thread_id": "t4",
            "messages": [{"role": "user", "content": "改到10号"}],
            "intent": "create",
            "booking_draft": deepcopy(FULL_DRAFT),
            "availability": ["10:00"],
            "selected_slot": "10:00",
            "draft_updates": {"appointment_date": "2030-01-10", "appointment_time": "11:30"},
        }
    )

    assert result["availability"] == ["11:30"]
    assert result["selected_slot"] == "11:30"
    assert result["stage"] == "awaiting_confirmation"


def test_service_change_invalidates_derived_facts_and_availability():
    draft = deepcopy(FULL_DRAFT)
    graph = build_booking_workflow(lambda *_: ["10:00"], lambda _: {})

    result = graph.invoke(
        {
            "thread_id": "t5",
            "messages": [{"role": "user", "content": "换成美容"}],
            "intent": "create",
            "booking_draft": draft,
            "availability": ["10:00"],
            "draft_updates": {"service_id": "grooming", "service_name": "精致美容"},
        }
    )

    assert "service_price" not in result["booking_draft"]
    assert "service_duration" not in result["booking_draft"]
    assert result["confirmation_status"] == "pending"


def test_unavailable_selected_slot_suggests_real_alternatives():
    graph = build_booking_workflow(lambda *_: ["11:30", "14:00"], lambda _: pytest.fail("must not write"))

    result = graph.invoke(
        {
            "thread_id": "t6",
            "messages": [{"role": "user", "content": "预约"}],
            "intent": "create",
            "booking_draft": deepcopy(FULL_DRAFT),
        }
    )

    assert result["stage"] == "suggesting_alternatives"
    assert result["availability"] == ["11:30", "14:00"]
    assert "11:30" in result["next_question"]
