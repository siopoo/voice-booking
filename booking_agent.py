from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date
from typing import Any

from langchain.agents import create_agent
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

import server
from booking_workflow import evaluate_booking_flow


SYSTEM_PROMPT = f"""
你是 PawPilot 宠物护理中心的 AI 语音前台。今天是 {date.today().isoformat()}。

你的目标不是闲聊，而是可靠完成一次预约。必须遵守以下业务规则：
1. 服务、价格、营业时间只能来自 get_business_profile 和 get_services 工具，禁止编造。
2. 用户给出日期后，必须调用 check_availability；只能向用户提供工具返回的时段。
3. 收集服务项目、宠物名字和类型、日期、时间、联系人姓名、11位手机号。
4. 信息齐全后，用一句话完整复述预约信息并询问“是否确认预约”。
5. 只有用户在复述之后明确表示确认，才能调用 create_booking，并把 customer_confirmed 设为 true。
6. 如果用户没有确认、表示修改或取消，不得调用 create_booking。
7. 如果创建时提示时段冲突，重新调用 check_availability 并提供其他时段。
8. 每次只问一个最必要的问题，回答简短、自然，适合语音播报。
9. 应主动说明自己是 AI 前台；不得承诺工具没有返回的优惠、医疗效果或服务。
10. 每当客户提供或修改预约字段，必须调用 update_booking_draft，把已知字段结构化记录；不要猜测未知字段。
11. 查询已有预约必须核对预约编号或手机号；改期和取消前必须完整复述变更并获得明确确认。
12. 改期必须先调用 check_availability，再调用 reschedule_booking；取消必须调用 cancel_booking。
""".strip()


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


@tool
def get_business_profile() -> str:
    """读取门店名称、地址、营业时间与预约规则。涉及门店事实时必须使用。"""
    return _json(
        {
            "name": "PawPilot 宠物护理中心",
            "address": "上海市静安区示范路 88 号",
            "hours": "周二至周日 10:00–18:00，周一休息",
            "booking_window": "仅接受未来14天内的预约",
        }
    )


@tool
def get_services() -> str:
    """读取可预约的宠物护理服务、价格和预计时长。禁止凭记忆回答价格。"""
    return _json(server.SERVICES)


@tool
def check_availability(appointment_date: str) -> str:
    """查询某一天真实可预约的时间。

    Args:
        appointment_date: ISO 日期，格式必须是 YYYY-MM-DD。
    """
    try:
        target = date.fromisoformat(appointment_date)
    except ValueError:
        return _json({"error": "日期格式必须是 YYYY-MM-DD", "slots": []})
    if target < date.today():
        return _json({"error": "不能预约过去的日期", "slots": []})
    if target > date.today().fromordinal(date.today().toordinal() + 14):
        return _json({"error": "只能预约未来14天内的日期", "slots": []})
    if target.weekday() == 0:
        return _json({"date": appointment_date, "closed": True, "slots": []})
    return _json(
        {
            "date": appointment_date,
            "closed": False,
            "slots": server.available_slots(appointment_date),
        }
    )


@tool
def create_booking(
    service_id: str,
    pet_name: str,
    pet_type: str,
    customer_name: str,
    phone: str,
    appointment_date: str,
    appointment_time: str,
    customer_confirmed: bool,
) -> str:
    """在客户听到完整预约复述并明确确认后创建预约。

    Args:
        service_id: get_services 返回的服务 ID。
        pet_name: 宠物名字。
        pet_type: 宠物类型，例如猫或狗。
        customer_name: 联系人称呼。
        phone: 11位中国大陆手机号。
        appointment_date: 已通过 check_availability 查询的 YYYY-MM-DD 日期。
        appointment_time: check_availability 返回的 HH:MM 时段。
        customer_confirmed: 客户是否在完整复述后明确确认；没有确认必须为 false。
    """
    if not customer_confirmed:
        return _json({"error": "必须先向客户复述完整预约信息并获得明确确认"})
    if not re.fullmatch(r"1\d{10}", phone):
        return _json({"error": "手机号必须是11位中国大陆手机号"})
    try:
        booking = server.create_booking_record(
            {
                "service_id": service_id,
                "pet_name": pet_name,
                "pet_type": pet_type,
                "customer_name": customer_name,
                "phone": phone,
                "appointment_date": appointment_date,
                "appointment_time": appointment_time,
                "notes": "LangChain Agent 预约",
            }
        )
        return _json(booking)
    except server.BookingValidationError as error:
        return _json({"error": str(error)})
    except server.BookingConflictError as error:
        return _json({"error": str(error), "conflict": True})


@tool
def update_booking_draft(
    service_id: str = "",
    service_name: str = "",
    pet_name: str = "",
    pet_type: str = "",
    appointment_date: str = "",
    appointment_time: str = "",
    customer_name: str = "",
    phone: str = "",
) -> str:
    """结构化记录客户刚刚提供或修改的预约字段，不会写入正式预约。

    只传入已经从客户原话或业务工具中确认的字段，未知字段保持为空。
    """
    values = {
        "service_id": service_id,
        "service_name": service_name,
        "pet_name": pet_name,
        "pet_type": pet_type,
        "appointment_date": appointment_date,
        "appointment_time": appointment_time,
        "customer_name": customer_name,
        "phone": phone,
    }
    if service_id and not service_name:
        service = next((item for item in server.SERVICES if item["id"] == service_id), None)
        if service:
            values["service_name"] = service["name"]
    elif service_name and not service_id:
        service = next((item for item in server.SERVICES if item["name"] == service_name), None)
        if service:
            values["service_id"] = service["id"]
    return _json({key: value.strip() for key, value in values.items() if value.strip()})


@tool
def find_bookings(booking_code: str = "", phone: str = "") -> str:
    """按预约编号或客户手机号查询已有预约，用于查询、改期或取消前核对身份。"""
    try:
        return _json(server.find_booking_records(booking_code=booking_code, phone=phone))
    except server.BookingValidationError as error:
        return _json({"error": str(error)})


@tool
def reschedule_booking(
    booking_code: str,
    phone: str,
    appointment_date: str,
    appointment_time: str,
    customer_confirmed: bool,
) -> str:
    """客户明确确认改期后，把已有预约移动到已查询为可用的新时段。"""
    try:
        return _json(
            server.reschedule_booking_record(
                booking_code,
                phone,
                appointment_date,
                appointment_time,
                customer_confirmed=customer_confirmed,
            )
        )
    except server.BookingValidationError as error:
        return _json({"error": str(error)})
    except server.BookingConflictError as error:
        return _json({"error": str(error), "conflict": True})


@tool
def cancel_booking(
    booking_code: str,
    phone: str,
    customer_confirmed: bool,
) -> str:
    """客户明确确认取消后，将匹配预约标记为已取消并释放时段。"""
    try:
        return _json(
            server.cancel_booking_record(
                booking_code,
                phone,
                customer_confirmed=customer_confirmed,
            )
        )
    except server.BookingValidationError as error:
        return _json({"error": str(error)})


TOOLS = [
    get_business_profile,
    get_services,
    update_booking_draft,
    check_availability,
    create_booking,
    find_bookings,
    reschedule_booking,
    cancel_booking,
]


def build_chat_model(env: dict[str, str] | None = None, http_client=None) -> ChatOpenAI:
    """Build the model client with request-level retries for transient network errors."""
    source = os.environ if env is None else env
    status = server.agent_status(source)
    if not status["configured"]:
        raise RuntimeError("请先配置 PAWPILOT_LLM_API_KEY 和 PAWPILOT_LLM_MODEL")
    options = {
        "model": status["model"],
        "api_key": source["PAWPILOT_LLM_API_KEY"],
        "base_url": source.get("PAWPILOT_LLM_BASE_URL", "https://api.openai.com/v1"),
        "temperature": 0,
        "timeout": float(source.get("PAWPILOT_LLM_TIMEOUT", "45")),
        "max_retries": int(source.get("PAWPILOT_LLM_MAX_RETRIES", "3")),
    }
    if http_client is not None:
        options["http_client"] = http_client
    return ChatOpenAI(**options)


def build_booking_agent(env: dict[str, str] | None = None):
    """Build the real LangChain tool-calling agent from environment configuration."""
    source = os.environ if env is None else env
    status = server.agent_status(source)
    if not status["configured"]:
        raise RuntimeError("请先配置 PAWPILOT_LLM_API_KEY 和 PAWPILOT_LLM_MODEL")
    model = build_chat_model(source)
    return create_agent(
        model=model,
        tools=TOOLS,
        system_prompt=SYSTEM_PROMPT,
        checkpointer=InMemorySaver(),
    )


def _message_text(message: AIMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    if isinstance(message.content, list):
        return "".join(
            block.get("text", "")
            for block in message.content
            if isinstance(block, dict) and block.get("type") in {"text", "output_text"}
        )
    return str(message.content)


def run_agent_turn(agent, session_id: str, user_text: str, clock=time.perf_counter) -> dict:
    """Run one conversation turn and return both speech text and auditable tool results."""
    started_at = clock()
    result = agent.invoke(
        {"messages": [{"role": "user", "content": user_text}]},
        config={
            "configurable": {"thread_id": session_id},
            "recursion_limit": 12,
        },
    )
    messages = result["messages"]
    last_human_index = max(
        (index for index, message in enumerate(messages) if isinstance(message, HumanMessage)),
        default=-1,
    )
    turn_messages = messages[last_human_index + 1 :]
    tool_calls = [
        {"name": message.name or "tool", "result": str(message.content)}
        for message in turn_messages
        if isinstance(message, ToolMessage)
    ]
    reply_message = next(
        (message for message in reversed(turn_messages) if isinstance(message, AIMessage)),
        None,
    )
    if reply_message is None:
        raise RuntimeError("Agent 没有返回回复")
    draft: dict[str, Any] = {}
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        try:
            value = json.loads(str(message.content))
        except (TypeError, json.JSONDecodeError):
            continue
        if message.name == "update_booking_draft" and isinstance(value, dict):
            draft.update({key: item for key, item in value.items() if item not in (None, "")})
        elif message.name == "find_bookings" and isinstance(value, list) and len(value) == 1:
            draft.update(
                {key: item for key, item in value[0].items() if item not in (None, "")}
            )
        elif message.name in {"create_booking", "reschedule_booking", "cancel_booking"}:
            if isinstance(value, dict) and not value.get("error"):
                draft.update(
                    {
                        key: value[key]
                        for key in (
                            "service_id", "service_name", "pet_name", "pet_type",
                            "customer_name", "phone", "appointment_date",
                            "appointment_time", "booking_code", "status",
                        )
                        if value.get(key) not in (None, "")
                    }
                )
    latency_ms = round((clock() - started_at) * 1000)
    return {
        "reply": _message_text(reply_message),
        "tool_calls": tool_calls,
        "latency_ms": latency_ms,
        "draft": draft,
        "flow": evaluate_booking_flow(draft),
    }


_agent = None
_agent_signature = None
_agent_lock = threading.Lock()


def get_booking_agent():
    global _agent, _agent_signature
    signature = (
        os.getenv("PAWPILOT_LLM_BASE_URL", "https://api.openai.com/v1"),
        os.getenv("PAWPILOT_LLM_MODEL", ""),
        bool(os.getenv("PAWPILOT_LLM_API_KEY")),
    )
    with _agent_lock:
        if _agent is None or signature != _agent_signature:
            _agent = build_booking_agent()
            _agent_signature = signature
    return _agent
