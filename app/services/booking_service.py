from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

from app.core.exceptions import BookingConflictError, BookingValidationError
from app.repositories.booking_repository import BookingRepository, IntegrityError


REQUIRED_FIELDS = (
    "service_id", "pet_name", "pet_type", "customer_name", "phone",
    "appointment_date", "appointment_time",
)


def booking_response(row, *, idempotent_replay: bool = False) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "id", "booking_code", "status", "service_id", "service_name", "pet_name",
            "pet_type", "customer_name", "phone", "appointment_date", "appointment_time",
        )
    } | {"idempotent_replay": idempotent_replay}


class BookingService:
    def __init__(
        self,
        repository: BookingRepository,
        config_provider: Callable[[], dict[str, Any]],
        *,
        clock: Callable[[dict[str, Any]], datetime],
    ):
        self.repository = repository
        self.config_provider = config_provider
        self.clock = clock

    def _service(self, config: dict[str, Any], service_id: str):
        return next((item for item in config["services"] if item["id"] == service_id), None)

    def validate_schedule(self, day: str, slot: str, service: dict, config: dict, now: datetime):
        try:
            target = date.fromisoformat(str(day).strip())
            slot_time = time.fromisoformat(str(slot).strip())
        except ValueError as error:
            raise BookingValidationError("日期或时间格式无效") from error
        if target < now.date():
            raise BookingValidationError("不能预约过去的日期")
        if target > now.date() + timedelta(days=config["booking_window_days"]):
            raise BookingValidationError(f"只能预约当天至未来{config['booking_window_days']}天")
        if target.weekday() in config["closed_weekdays"]:
            raise BookingValidationError("门店当天休息，不能预约")
        normalized = slot_time.strftime("%H:%M")
        if normalized not in config["appointment_slots"]:
            raise BookingValidationError("预约时间不属于门店允许时段")
        start = datetime.combine(target, slot_time, tzinfo=now.tzinfo)
        if start <= now:
            raise BookingValidationError("该时段已经过去")
        closing = datetime.combine(target, time.fromisoformat(config["closing_time"]), tzinfo=now.tzinfo)
        if start + timedelta(minutes=int(service["duration"])) > closing:
            raise BookingValidationError("该服务将在关门后结束，请选择更早时段")
        return target.isoformat(), normalized

    def available_slots(self, day: str, service_id: str | None = None) -> list[str]:
        config = self.config_provider()
        service = self._service(config, service_id or config["services"][0]["id"])
        if service is None:
            return []
        now = self.clock(config)
        candidates = []
        for slot in config["appointment_slots"]:
            try:
                self.validate_schedule(day, slot, service, config, now)
                candidates.append(slot)
            except BookingValidationError:
                pass
        occupied = self.repository.occupied_slots(day) if candidates else set()
        return [slot for slot in candidates if slot not in occupied]

    def create(self, payload: dict[str, Any], *, customer_confirmed: bool) -> dict[str, Any]:
        if not customer_confirmed:
            raise BookingValidationError("创建预约前必须获得客户明确确认")
        config = self.config_provider()
        service = self._service(config, str(payload.get("service_id", "")))
        if any(not str(payload.get(key, "")).strip() for key in REQUIRED_FIELDS) or service is None:
            raise BookingValidationError("预约信息不完整或服务项目无效")
        phone = str(payload["phone"]).strip()
        if not re.fullmatch(r"1[3-9]\d{9}", phone):
            raise BookingValidationError("手机号必须是11位中国大陆手机号")
        key = str(payload.get("idempotency_key", "")).strip()
        if key and (existing := self.repository.find_idempotency(key)):
            return {**existing, "idempotent_replay": True}
        now = self.clock(config)
        day, slot = self.validate_schedule(
            str(payload["appointment_date"]), str(payload["appointment_time"]), service, config, now
        )
        if slot not in self.available_slots(day, service["id"]):
            raise BookingConflictError("该时段刚刚已被预约或不可用，请重新选择")
        code = f"PP{now:%m%d}{uuid.uuid4().hex[:10].upper()}"
        def response(booking_id: int):
            return {
                "id": booking_id, "booking_code": code, "status": "confirmed",
                "service_id": service["id"], "service_name": service["name"],
                "pet_name": str(payload["pet_name"]).strip(),
                "pet_type": str(payload["pet_type"]).strip(),
                "customer_name": str(payload["customer_name"]).strip(), "phone": phone,
                "appointment_date": day, "appointment_time": slot, "idempotent_replay": False,
            }
        values = (
            code, service["id"], service["name"], response(0)["pet_name"], response(0)["pet_type"],
            response(0)["customer_name"], phone, day, slot, str(payload.get("notes", "")).strip(),
            now.isoformat(timespec="seconds"), key or None,
        )
        try:
            return self.repository.insert(values, response)
        except IntegrityError as error:
            if key and (existing := self.repository.find_idempotency(key)):
                return {**existing, "idempotent_replay": True}
            raise BookingConflictError("该时段刚刚已被预约，请重新选择") from error

    def find(self, *, booking_code: str = "", phone: str = "", include_cancelled: bool = True):
        if not booking_code.strip() and not phone.strip():
            raise BookingValidationError("预约编号或手机号至少提供一项")
        return [
            booking_response(row)
            for row in self.repository.find(
                booking_code=booking_code, phone=phone, include_cancelled=include_cancelled
            )
        ]

    def reschedule(self, booking_code: str, phone: str, day: str, slot: str, *, customer_confirmed: bool):
        if not customer_confirmed:
            raise BookingValidationError("改期前必须获得客户明确确认")
        current = self.repository.get_owned(booking_code, phone)
        if current is None:
            raise BookingValidationError("未找到匹配的预约，请核对预约编号和手机号")
        if current["status"] != "confirmed":
            raise BookingValidationError("只有已确认预约可以改期")
        config = self.config_provider()
        service = self._service(config, current["service_id"])
        if service is None:
            raise BookingValidationError("原预约服务已不在当前配置中，请联系门店")
        normalized_day, normalized_slot = self.validate_schedule(day, slot, service, config, self.clock(config))
        if current["appointment_date"] == normalized_day and current["appointment_time"] == normalized_slot:
            return booking_response(current, idempotent_replay=True)
        if normalized_slot not in self.available_slots(normalized_day, service["id"]):
            raise BookingConflictError("新的预约时段不可用，请重新选择")
        try:
            return booking_response(self.repository.update_slot(current["id"], normalized_day, normalized_slot))
        except IntegrityError as error:
            raise BookingConflictError("新的预约时段刚刚被占用，请重新选择") from error

    def cancel(self, booking_code: str, phone: str, *, customer_confirmed: bool):
        if not customer_confirmed:
            raise BookingValidationError("取消前必须获得客户明确确认")
        current = self.repository.get_owned(booking_code, phone)
        if current is None:
            raise BookingValidationError("未找到匹配的预约，请核对预约编号和手机号")
        if current["status"] == "cancelled":
            return booking_response(current, idempotent_replay=True)
        return booking_response(self.repository.cancel(current["id"]))
