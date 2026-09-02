import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import booking_agent
import server
from booking_agent import (
    check_availability,
    create_booking,
    get_business_profile,
    get_services,
    run_agent_turn,
)


class BookingToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "agent-test.db"
        self.patch = patch.object(server, "DB_PATH", self.db_path)
        self.patch.start()
        server.init_db()
        target = date.today() + timedelta(days=1)
        while target.weekday() == 0:
            target += timedelta(days=1)
        self.day = target.isoformat()

    def tearDown(self) -> None:
        self.patch.stop()
        self.temp_dir.cleanup()

    def test_business_tools_return_authoritative_facts(self) -> None:
        profile = json.loads(get_business_profile.invoke({}))
        services = json.loads(get_services.invoke({}))
        self.assertEqual(profile["name"], "PawPilot 宠物护理中心")
        self.assertEqual([item["id"] for item in services], ["basic", "grooming", "spa"])

    def test_availability_tool_reads_real_database_state(self) -> None:
        result = json.loads(check_availability.invoke({"appointment_date": self.day}))
        self.assertEqual(result["slots"], ["10:00", "11:30", "14:00", "15:30", "17:00"])

    def test_create_booking_tool_persists_and_removes_slot(self) -> None:
        payload = {
            "service_id": "grooming",
            "pet_name": "可乐",
            "pet_type": "狗",
            "customer_name": "陈女士",
            "phone": "13800138000",
            "appointment_date": self.day,
            "appointment_time": "10:00",
            "customer_confirmed": True,
        }
        created = json.loads(create_booking.invoke(payload))
        remaining = json.loads(check_availability.invoke({"appointment_date": self.day}))
        self.assertEqual(created["status"], "confirmed")
        self.assertTrue(created["booking_code"].startswith("PP"))
        self.assertNotIn("10:00", remaining["slots"])

    def test_create_booking_tool_rejects_unconfirmed_write(self) -> None:
        result = json.loads(
            create_booking.invoke(
                {
                    "service_id": "basic",
                    "pet_name": "布丁",
                    "pet_type": "猫",
                    "customer_name": "林先生",
                    "phone": "13900139000",
                    "appointment_date": self.day,
                    "appointment_time": "11:30",
                    "customer_confirmed": False,
                }
            )
        )
        self.assertEqual(result["error"], "必须先向客户复述完整预约信息并获得明确确认")

    def test_booking_lifecycle_tools_are_available_to_the_agent(self) -> None:
        tool_names = {item.name for item in booking_agent.TOOLS}
        self.assertTrue(
            {"update_booking_draft", "find_bookings", "reschedule_booking", "cancel_booking"}
            <= tool_names
        )

    def test_draft_tool_normalizes_an_authoritative_service_name_to_its_id(self) -> None:
        result = json.loads(
            booking_agent.update_booking_draft.invoke({"service_name": "精致美容"})
        )
        self.assertEqual(
            result,
            {"service_id": "grooming", "service_name": "精致美容"},
        )


class AgentRuntimeTests(unittest.TestCase):
    def test_chat_model_survives_three_transient_connection_failures(self) -> None:
        attempts = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 4:
                raise httpx.ConnectError("temporary network failure")
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "deepseek-chat",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "OK"},
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

        builder = getattr(booking_agent, "build_chat_model", None)
        self.assertIsNotNone(builder, "需要独立的、可验证的模型连接构造函数")
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            model = builder(
                {
                    "PAWPILOT_LLM_API_KEY": "test-key",
                    "PAWPILOT_LLM_MODEL": "deepseek-chat",
                    "PAWPILOT_LLM_BASE_URL": "https://api.deepseek.com",
                },
                http_client=client,
            )
            response = model.invoke("只回复 OK")

        self.assertEqual(response.content, "OK")
        self.assertEqual(attempts, 4)

    def test_run_agent_turn_uses_session_memory_and_returns_tool_trace(self) -> None:
        class FakeAgent:
            def invoke(self, payload, config):
                thread_id = config["configurable"]["thread_id"]
                user_text = payload["messages"][0]["content"]
                return {
                    "messages": [
                        HumanMessage(content=user_text),
                        ToolMessage(
                            content='{"slots":["10:00"]}',
                            tool_call_id="call-1",
                            name="check_availability",
                        ),
                        AIMessage(content=f"session={thread_id}，10点可以预约"),
                    ]
                }

        timestamps = iter([10.0, 10.42])
        try:
            result = run_agent_turn(
                FakeAgent(),
                "session-42",
                "明天几点有空？",
                clock=lambda: next(timestamps),
            )
        except TypeError:
            result = {"reply": None, "tool_calls": [], "latency_ms": None}
        self.assertEqual(result["reply"], "session=session-42，10点可以预约")
        self.assertEqual(
            result["tool_calls"],
            [{"name": "check_availability", "result": '{"slots":["10:00"]}'}],
        )
        self.assertEqual(result["latency_ms"], 420)

    def test_run_agent_turn_returns_structured_draft_and_deterministic_stage(self) -> None:
        class FakeAgent:
            def invoke(self, payload, config):
                return {
                    "messages": [
                        HumanMessage(content=payload["messages"][0]["content"]),
                        ToolMessage(
                            content=json.dumps(
                                {
                                    "service_id": "grooming",
                                    "service_name": "精致美容",
                                    "pet_name": "可乐",
                                    "pet_type": "狗",
                                    "appointment_date": self_day,
                                    "appointment_time": "14:00",
                                    "customer_name": "陈女士",
                                    "phone": "13800138000",
                                },
                                ensure_ascii=False,
                            ),
                            tool_call_id="draft-1",
                            name="update_booking_draft",
                        ),
                        AIMessage(content="信息齐全，请确认预约。"),
                    ]
                }

        self_day = (date.today() + timedelta(days=1)).isoformat()
        result = run_agent_turn(FakeAgent(), "draft-session", "确认一下信息")

        self.assertEqual(result.get("draft", {}).get("pet_name"), "可乐")
        self.assertEqual(result.get("flow", {}).get("stage"), "awaiting_confirmation")
        self.assertEqual(result.get("flow", {}).get("missing_fields"), [])

    def test_querying_one_existing_booking_hydrates_the_visible_draft(self) -> None:
        class FakeAgent:
            def invoke(self, payload, config):
                return {
                    "messages": [
                        HumanMessage(content=payload["messages"][0]["content"]),
                        ToolMessage(
                            content=json.dumps(
                                [
                                    {
                                        "booking_code": "PPTEST001",
                                        "status": "confirmed",
                                        "service_id": "basic",
                                        "service_name": "基础洗护",
                                        "pet_name": "豆包",
                                        "pet_type": "狗",
                                        "customer_name": "王女士",
                                        "phone": "13700137000",
                                        "appointment_date": "2026-09-05",
                                        "appointment_time": "10:00",
                                    }
                                ],
                                ensure_ascii=False,
                            ),
                            tool_call_id="find-1",
                            name="find_bookings",
                        ),
                        AIMessage(content="已找到预约，请问需要改期还是取消？"),
                    ]
                }

        result = run_agent_turn(FakeAgent(), "lookup-session", "查询我的预约")

        self.assertEqual(result.get("draft", {}).get("booking_code"), "PPTEST001")
        self.assertEqual(result.get("flow", {}).get("stage"), "booked")


if __name__ == "__main__":
    unittest.main()
