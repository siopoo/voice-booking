from __future__ import annotations

import copy
import hashlib
import json
import sys
from datetime import time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT / "config" / "business.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "business_name": "PawPilot 宠物护理中心",
    "business_type": "pet_groomer",
    "address": "上海市静安区示范路88号",
    "timezone": "Asia/Shanghai",
    "opening_time": "10:00",
    "closing_time": "18:00",
    "closed_weekdays": [0],
    "booking_window_days": 14,
    "services": [
        {"id": "basic", "name": "基础洗护", "duration": 60, "price": 88},
        {"id": "grooming", "name": "精致美容", "duration": 90, "price": 168},
        {"id": "spa", "name": "深度护理", "duration": 90, "price": 238},
    ],
    "appointment_slots": ["10:00", "11:30", "14:00", "15:30", "17:00"],
    "agent_language": "zh",
    "welcome_message": "您好，我是 PawPilot AI 前台。请问想为您的宠物预约什么服务？",
}


class BusinessConfigError(ValueError):
    pass


def business_timezone(name: str) -> tzinfo:
    """Return a usable timezone even on Windows installs without the tzdata wheel."""
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        if name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name="Asia/Shanghai")
        raise BusinessConfigError("timezone 不是有效 IANA 时区") from error


def _parse_time(value: str, field: str) -> time:
    try:
        return time.fromisoformat(str(value))
    except ValueError as error:
        raise BusinessConfigError(f"{field} 必须是 HH:MM 格式") from error


def validate_business_config(config: dict[str, Any]) -> dict[str, Any]:
    required = set(DEFAULT_CONFIG)
    missing = sorted(required - set(config))
    if missing:
        raise BusinessConfigError(f"缺少字段：{', '.join(missing)}")
    value = copy.deepcopy(config)
    if not str(value["business_name"]).strip():
        raise BusinessConfigError("business_name 不能为空")
    business_timezone(str(value["timezone"]))
    opening = _parse_time(value["opening_time"], "opening_time")
    closing = _parse_time(value["closing_time"], "closing_time")
    if opening >= closing:
        raise BusinessConfigError("closing_time 必须晚于 opening_time")
    if not isinstance(value["closed_weekdays"], list) or any(
        not isinstance(day, int) or day < 0 or day > 6 for day in value["closed_weekdays"]
    ):
        raise BusinessConfigError("closed_weekdays 必须是 0 到 6 的数组")
    if not isinstance(value["booking_window_days"], int) or value["booking_window_days"] < 0:
        raise BusinessConfigError("booking_window_days 必须是非负整数")
    services = value["services"]
    if not isinstance(services, list) or not services:
        raise BusinessConfigError("services 至少包含一个服务")
    service_ids = set()
    for service in services:
        if not isinstance(service, dict) or not all(
            key in service for key in ("id", "name", "duration", "price")
        ):
            raise BusinessConfigError("每项服务必须包含 id、name、duration、price")
        service_id = str(service["id"]).strip()
        if not service_id or service_id in service_ids:
            raise BusinessConfigError("服务 id 不能为空或重复")
        service_ids.add(service_id)
        if int(service["duration"]) <= 0 or float(service["price"]) < 0:
            raise BusinessConfigError("服务时长必须大于0且价格不能为负数")
        service["id"] = service_id
        service["name"] = str(service["name"]).strip()
        service["duration"] = int(service["duration"])
    slots = value["appointment_slots"]
    if not isinstance(slots, list) or not slots:
        raise BusinessConfigError("appointment_slots 至少包含一个时段")
    normalized_slots = []
    for slot in slots:
        parsed = _parse_time(slot, "appointment_slots")
        if parsed < opening or parsed >= closing:
            raise BusinessConfigError("预约时段必须位于营业时间内")
        normalized_slots.append(parsed.strftime("%H:%M"))
    value["appointment_slots"] = list(dict.fromkeys(normalized_slots))
    value["business_name"] = str(value["business_name"]).strip()
    value["address"] = str(value["address"]).strip()
    value["agent_language"] = str(value["agent_language"]).strip() or "zh"
    value["welcome_message"] = str(value["welcome_message"]).strip()
    value.pop("config_version", None)
    return value


def config_version(config: dict[str, Any]) -> str:
    payload = {key: value for key, value in config.items() if key != "config_version"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]


def _with_version(config: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(config)
    value["config_version"] = config_version(value)
    return value


def _default_reporter(message: str) -> None:
    print(message, file=sys.stderr)


def load_business_config(
    path: Path = DEFAULT_CONFIG_PATH,
    *,
    reporter: Callable[[str], None] = _default_reporter,
) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise BusinessConfigError("配置根节点必须是 JSON 对象")
        return _with_version(validate_business_config(raw))
    except (OSError, json.JSONDecodeError, BusinessConfigError, TypeError, ValueError) as error:
        reporter(f"门店配置读取失败，已使用安全默认配置：{error}")
        return _with_version(validate_business_config(DEFAULT_CONFIG))


def save_business_config(config: dict[str, Any], path: Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    normalized = validate_business_config(config)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return _with_version(normalized)


def format_business_hours(config: dict[str, Any]) -> str:
    closed = "、".join(f"周{['一','二','三','四','五','六','日'][day]}" for day in config["closed_weekdays"])
    suffix = f"，{closed}休息" if closed else ""
    return f"{config['opening_time']}–{config['closing_time']}{suffix}"
