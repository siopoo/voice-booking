from __future__ import annotations

import json
import mimetypes
import os
import re
import sqlite3
import threading
import uuid
from collections.abc import MutableMapping
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen as default_urlopen

from business_config import (
    DEFAULT_CONFIG_PATH,
    business_timezone,
    format_business_hours,
    load_business_config,
    save_business_config,
)


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DB_PATH = ROOT / "appointments.db"
BUSINESS_CONFIG_PATH = DEFAULT_CONFIG_PATH
HOST = "127.0.0.1"
PORT = 8000


def load_env_file(
    path: Path = ROOT / ".env",
    target: MutableMapping[str, str] | None = None,
) -> None:
    """Load simple KEY=VALUE settings without overriding process variables."""
    destination = os.environ if target is None else target
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        destination.setdefault(key, value)


load_env_file()

def current_business_config() -> dict:
    return load_business_config(BUSINESS_CONFIG_PATH)


def business_now(config: dict | None = None) -> datetime:
    active = current_business_config() if config is None else config
    return datetime.now(business_timezone(active["timezone"]))


# Backward-compatible constants for existing callers; new code reads current_business_config().
_INITIAL_CONFIG = current_business_config()
SERVICES = _INITIAL_CONFIG["services"]
SLOTS = _INITIAL_CONFIG["appointment_slots"]


class BookingValidationError(ValueError):
    pass


class BookingConflictError(RuntimeError):
    pass


class OnboardingValidationError(ValueError):
    pass


ACCEPTANCE_CHECKLIST_KEYS = (
    "business_profile_confirmed",
    "services_confirmed",
    "hours_confirmed",
    "agent_config_generated",
    "text_test_passed",
    "voice_test_passed",
    "customer_accepted",
)


def agent_status(env: dict[str, str] | None = None) -> dict:
    source = os.environ if env is None else env
    key = source.get("PAWPILOT_LLM_API_KEY", "").strip()
    model = source.get("PAWPILOT_LLM_MODEL", "").strip()
    return {"configured": bool(key and model), "model": model or None}


def stt_status(env: dict[str, str] | None = None) -> dict:
    source = os.environ if env is None else env
    if source.get("PAWPILOT_STT_PROVIDER", "api").strip().lower() == "sensevoice":
        model = source.get("PAWPILOT_SENSEVOICE_MODEL", "iic/SenseVoiceSmall").strip()
        basic = {"configured": bool(model), "model": model or None}
        if env is not None:
            return basic
        try:
            from sensevoice_stt import sensevoice_runtime_status

            runtime = sensevoice_runtime_status()
        except Exception as error:
            runtime = {"state": "error", "ready": False, "error": str(error)[:300]}
        return {**basic, "provider": "sensevoice", **runtime}
    key = source.get("PAWPILOT_STT_API_KEY", "").strip()
    model = source.get("PAWPILOT_STT_MODEL", "").strip()
    basic = {"configured": bool(key and model), "model": model or None}
    return basic


def transcribe_audio(
    audio: bytes,
    content_type: str,
    env: dict[str, str] | None = None,
    urlopen=default_urlopen,
    local_transcriber=None,
) -> str:
    source = os.environ if env is None else env
    if source.get("PAWPILOT_STT_PROVIDER", "api").strip().lower() == "sensevoice":
        if local_transcriber is None:
            from sensevoice_stt import transcribe_sensevoice

            local_transcriber = transcribe_sensevoice
        return local_transcriber(audio, content_type, source)
    status = stt_status(source)
    if not status["configured"]:
        raise RuntimeError("后端语音识别 API 尚未配置")
    if not audio:
        raise ValueError("录音内容为空")

    mime_type = content_type.split(";", 1)[0].strip().lower() or "application/octet-stream"
    extension = {
        "audio/webm": "webm",
        "audio/ogg": "ogg",
        "audio/mp4": "m4a",
        "audio/mpeg": "mp3",
        "audio/wav": "wav",
    }.get(mime_type, "webm")
    boundary = f"----PawPilot{uuid.uuid4().hex}"
    body = b"".join(
        [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{status['model']}\r\n".encode(),
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\nzh\r\n".encode(),
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"speech.{extension}\"\r\nContent-Type: {mime_type}\r\n\r\n"
            ).encode(),
            audio,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
    )
    base_url = source.get("PAWPILOT_STT_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    request = Request(
        f"{base_url}/audio/transcriptions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {source['PAWPILOT_STT_API_KEY']}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urlopen(request, timeout=45) as response:
        payload = json.loads(response.read().decode("utf-8"))
    text = str(payload.get("text", "")).strip()
    if not text:
        raise ValueError("语音识别服务未返回文字")
    return text


def _extract_json_object(content: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.I)
    match = re.search(r"\{.*\}", cleaned, flags=re.S)
    if not match:
        raise ValueError("模型未返回 JSON 对象")
    value = json.loads(match.group(0))
    if not isinstance(value, dict) or "value" not in value:
        raise ValueError("模型返回缺少 value 字段")
    return value


def interpret_text(
    text: str,
    step: str,
    context: dict,
    env: dict[str, str] | None = None,
    urlopen=default_urlopen,
) -> dict:
    source = os.environ if env is None else env
    status = agent_status(source)
    if not status["configured"]:
        raise RuntimeError("模型 API 尚未配置")
    base_url = source.get("PAWPILOT_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    endpoint = f"{base_url}/chat/completions"
    allowed_service_ids = "、".join(item["id"] for item in current_business_config()["services"])
    prompt = (
        "你是宠物护理预约系统的意图解析器。只返回 JSON，不要解释。"
        "格式为 {\"value\": 任意JSON值, \"confidence\": 0到1的数字}。"
        f"service 步骤的 value 只能是 {allowed_service_ids} 或 null；"
        "date 步骤返回 YYYY-MM-DD 或 null；time 步骤返回 HH:MM 或 null；"
        "confirm 步骤返回 yes、no、restart 或 null。"
    )
    body = json.dumps(
        {
            "model": status["model"],
            "temperature": 0,
            "messages": [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"step": step, "text": text, "context": context},
                        ensure_ascii=False,
                    ),
                },
            ],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {source['PAWPILOT_LLM_API_KEY']}",
            "Content-Type": "application/json",
        },
    )
    with urlopen(request, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    content = payload["choices"][0]["message"]["content"]
    return _extract_json_object(content)


@contextmanager
def get_db():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    with get_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                booking_code TEXT UNIQUE NOT NULL,
                service_id TEXT NOT NULL,
                service_name TEXT NOT NULL,
                pet_name TEXT NOT NULL,
                pet_type TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                appointment_date TEXT NOT NULL,
                appointment_time TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'confirmed',
                created_at TEXT NOT NULL,
                idempotency_key TEXT
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS onboarding_applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                application_code TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL,
                application_json TEXT NOT NULL,
                collected_json TEXT NOT NULL DEFAULT '{}',
                config_json TEXT,
                checklist_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        table_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'appointments'"
        ).fetchone()["sql"]
        if re.search(
            r"UNIQUE\s*\(\s*appointment_date\s*,\s*appointment_time\s*\)",
            table_sql,
            flags=re.I,
        ):
            db.execute("ALTER TABLE appointments RENAME TO appointments_legacy")
            db.execute(
                """
                CREATE TABLE appointments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    booking_code TEXT UNIQUE NOT NULL,
                    service_id TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    pet_name TEXT NOT NULL,
                    pet_type TEXT NOT NULL,
                    customer_name TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    appointment_date TEXT NOT NULL,
                    appointment_time TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'confirmed',
                    created_at TEXT NOT NULL,
                    idempotency_key TEXT
                )
                """
            )
            legacy_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(appointments_legacy)")
            }
            idempotency_value = "idempotency_key" if "idempotency_key" in legacy_columns else "NULL"
            db.execute(
                f"""
                INSERT INTO appointments (
                    id, booking_code, service_id, service_name, pet_name, pet_type,
                    customer_name, phone, appointment_date, appointment_time,
                    notes, status, created_at, idempotency_key
                )
                SELECT id, booking_code, service_id, service_name, pet_name, pet_type,
                    customer_name, phone, appointment_date, appointment_time,
                    notes, status, created_at, {idempotency_value}
                FROM appointments_legacy
                """
            )
            db.execute("DROP TABLE appointments_legacy")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(appointments)")}
        if "idempotency_key" not in columns:
            db.execute("ALTER TABLE appointments ADD COLUMN idempotency_key TEXT")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_appointments_idempotency "
            "ON appointments(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_appointments_confirmed_slot "
            "ON appointments(appointment_date, appointment_time) "
            "WHERE status = 'confirmed'"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency_records (
                idempotency_key TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


def _onboarding_response(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "application_code": row["application_code"],
        "status": row["status"],
        "application": json.loads(row["application_json"]),
        "collected": json.loads(row["collected_json"] or "{}"),
        "config": json.loads(row["config_json"]) if row["config_json"] else None,
        "checklist": json.loads(row["checklist_json"] or "{}"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_onboarding_application(application_id: int) -> dict:
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM onboarding_applications WHERE id = ?", (application_id,)
        ).fetchone()
    if row is None:
        raise OnboardingValidationError("申请不存在")
    return _onboarding_response(row)


def list_onboarding_applications() -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM onboarding_applications ORDER BY id DESC"
        ).fetchall()
    return [_onboarding_response(row) for row in rows]


def submit_onboarding_application(payload: dict) -> dict:
    required = (
        "business_name", "business_type", "website_url", "services_and_prices",
        "email", "phone", "preferred_language",
    )
    if any(not str(payload.get(key, "")).strip() for key in required):
        raise OnboardingValidationError("申请资料不完整")
    if not re.fullmatch(r"1[3-9]\d{9}", str(payload["phone"]).strip()):
        raise OnboardingValidationError("联系电话必须是11位中国大陆手机号")
    if not all(payload.get(key) is True for key in (
        "website_authorized", "contact_authorized", "representative_confirmed"
    )):
        raise OnboardingValidationError("必须勾选三项必要授权与身份确认")
    now = datetime.now().isoformat(timespec="seconds")
    code = f"APP-{uuid.uuid4().hex[:8].upper()}"
    with get_db() as db:
        cursor = db.execute(
            "INSERT INTO onboarding_applications "
            "(application_code,status,application_json,created_at,updated_at) VALUES (?,?,?,?,?)",
            (code, "submitted", json.dumps(payload, ensure_ascii=False), now, now),
        )
        application_id = cursor.lastrowid
    return get_onboarding_application(application_id)


def _set_onboarding_status(application_id: int, status: str, **json_fields) -> dict:
    assignments = ["status = ?", "updated_at = ?"]
    values: list[object] = [status, datetime.now().isoformat(timespec="seconds")]
    for column, value in json_fields.items():
        assignments.append(f"{column} = ?")
        values.append(json.dumps(value, ensure_ascii=False))
    values.append(application_id)
    with get_db() as db:
        cursor = db.execute(
            f"UPDATE onboarding_applications SET {', '.join(assignments)} WHERE id = ?",
            values,
        )
        if not cursor.rowcount:
            raise OnboardingValidationError("申请不存在")
    return get_onboarding_application(application_id)


def advance_onboarding_application(application_id: int, action: str) -> dict:
    current = get_onboarding_application(application_id)
    transitions = {
        ("submitted", "collect"): "collecting",
        ("collecting", "review"): "awaiting_review",
        ("config_generated", "test"): "testing",
    }
    status = transitions.get((current["status"], action))
    if status is None:
        raise OnboardingValidationError("当前状态不能执行该操作")
    if action == "collect":
        collected = {
            "source": "客户授权的公开网站 + 演示稳定数据",
            "website_reachable": True,
            "summary": current["application"]["services_and_prices"],
            "human_fallback": True,
        }
        return _set_onboarding_status(application_id, status, collected_json=collected)
    return _set_onboarding_status(application_id, status)


def generate_onboarding_config(application_id: int, config: dict) -> dict:
    current = get_onboarding_application(application_id)
    if current["status"] not in {"submitted", "collecting", "awaiting_review", "config_generated"}:
        raise OnboardingValidationError("当前状态不能生成配置")
    normalized = save_business_config(config, Path(str(BUSINESS_CONFIG_PATH) + ".preview"))
    Path(str(BUSINESS_CONFIG_PATH) + ".preview").unlink(missing_ok=True)
    normalized.pop("config_version", None)
    return _set_onboarding_status(application_id, "config_generated", config_json=normalized)


def accept_onboarding_application(application_id: int, checklist: dict) -> dict:
    current = get_onboarding_application(application_id)
    if current["config"] is None:
        raise OnboardingValidationError("请先生成门店配置")
    if current["status"] != "testing":
        raise OnboardingValidationError("请先进入测试阶段")
    if not all(checklist.get(key) is True for key in ACCEPTANCE_CHECKLIST_KEYS):
        raise OnboardingValidationError("所有测试与验收检查项必须通过")
    return _set_onboarding_status(application_id, "accepted", checklist_json=checklist)


def activate_onboarding_application(application_id: int) -> dict:
    current = get_onboarding_application(application_id)
    if current["status"] != "accepted" or current["config"] is None:
        raise OnboardingValidationError("只有验收通过的配置才能激活")
    save_business_config(current["config"], BUSINESS_CONFIG_PATH)
    return _set_onboarding_status(application_id, "activated")


def _service(config: dict, service_id: str) -> dict | None:
    return next((item for item in config["services"] if item["id"] == service_id), None)


def _validate_schedule(
    appointment_date: str,
    appointment_time: str,
    service: dict,
    *,
    config: dict,
    now: datetime,
) -> tuple[str, str]:
    try:
        target = date.fromisoformat(str(appointment_date).strip())
        slot_time = time.fromisoformat(str(appointment_time).strip())
    except ValueError as error:
        raise BookingValidationError("日期或时间格式无效") from error
    today = now.date()
    if target < today:
        raise BookingValidationError("不能预约过去的日期")
    if target > today + timedelta(days=config["booking_window_days"]):
        raise BookingValidationError(f"只能预约当天至未来{config['booking_window_days']}天")
    if target.weekday() in config["closed_weekdays"]:
        raise BookingValidationError("门店当天休息，不能预约")
    slot = slot_time.strftime("%H:%M")
    if slot not in config["appointment_slots"]:
        raise BookingValidationError("预约时间不属于门店允许时段")
    start = datetime.combine(target, slot_time, tzinfo=now.tzinfo)
    if start <= now:
        raise BookingValidationError("该时段已经过去")
    closing = datetime.combine(target, time.fromisoformat(config["closing_time"]), tzinfo=now.tzinfo)
    if start + timedelta(minutes=int(service["duration"])) > closing:
        raise BookingValidationError("该服务将在关门后结束，请选择更早时段")
    return target.isoformat(), slot


def available_slots(day: str, service_id: str | None = None, *, now: datetime | None = None) -> list[str]:
    config = current_business_config()
    current = business_now(config) if now is None else now
    service = _service(config, service_id or config["services"][0]["id"])
    if service is None:
        return []
    candidates = []
    for slot in config["appointment_slots"]:
        try:
            _validate_schedule(day, slot, service, config=config, now=current)
            candidates.append(slot)
        except BookingValidationError:
            continue
    if not candidates:
        return []
    with get_db() as db:
        rows = db.execute(
            "SELECT appointment_time FROM appointments "
            "WHERE appointment_date = ? AND status = 'confirmed'",
            (day,),
        ).fetchall()
    occupied = {row["appointment_time"] for row in rows}
    return [slot for slot in candidates if slot not in occupied]


def create_booking_record(payload: dict) -> dict:
    required = [
        "service_id",
        "pet_name",
        "pet_type",
        "customer_name",
        "phone",
        "appointment_date",
        "appointment_time",
    ]
    missing = [key for key in required if not str(payload.get(key, "")).strip()]
    config = current_business_config()
    service = _service(config, str(payload.get("service_id", "")))
    if missing or service is None:
        raise BookingValidationError("预约信息不完整或服务项目无效")
    phone = str(payload["phone"]).strip()
    if not re.fullmatch(r"1[3-9]\d{9}", phone):
        raise BookingValidationError("手机号必须是11位中国大陆手机号")
    idempotency_key = str(payload.get("idempotency_key", "")).strip()
    if idempotency_key:
        with get_db() as db:
            record = db.execute(
                "SELECT response_json FROM idempotency_records WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if record is not None:
            response = json.loads(record["response_json"])
            response["idempotent_replay"] = True
            return response
    day, appointment_time = _validate_schedule(
        str(payload["appointment_date"]), str(payload["appointment_time"]), service,
        config=config, now=business_now(config),
    )
    if appointment_time not in available_slots(day, service["id"]):
        raise BookingConflictError("该时段刚刚已被预约或不可用，请重新选择")
    now = business_now(config)
    code = f"PP{now:%m%d}{uuid.uuid4().hex[:10].upper()}"
    try:
        with get_db() as db:
            cursor = db.execute(
                """
                INSERT INTO appointments (
                    booking_code, service_id, service_name, pet_name, pet_type,
                    customer_name, phone, appointment_date, appointment_time,
                    notes, created_at, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    service["id"],
                    service["name"],
                    str(payload["pet_name"]).strip(),
                    str(payload["pet_type"]).strip(),
                    str(payload["customer_name"]).strip(),
                    phone,
                    day,
                    appointment_time,
                    str(payload.get("notes", "")).strip(),
                    now.isoformat(timespec="seconds"),
                    idempotency_key or None,
                ),
            )
            booking_id = cursor.lastrowid
            response = {
                "id": booking_id,
                "booking_code": code,
                "status": "confirmed",
                "service_id": service["id"],
                "service_name": service["name"],
                "pet_name": str(payload["pet_name"]).strip(),
                "pet_type": str(payload["pet_type"]).strip(),
                "customer_name": str(payload["customer_name"]).strip(),
                "phone": phone,
                "appointment_date": day,
                "appointment_time": appointment_time,
                "idempotent_replay": False,
            }
            if idempotency_key:
                db.execute(
                    "INSERT INTO idempotency_records VALUES (?, ?, ?, ?)",
                    (idempotency_key, "create_booking", json.dumps(response, ensure_ascii=False), now.isoformat()),
                )
    except sqlite3.IntegrityError as error:
        if idempotency_key:
            with get_db() as db:
                record = db.execute(
                    "SELECT response_json FROM idempotency_records WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
            if record is not None:
                response = json.loads(record["response_json"])
                response["idempotent_replay"] = True
                return response
        raise BookingConflictError("该时段刚刚已被预约，请重新选择") from error
    return response


def _booking_response(row: sqlite3.Row, idempotent_replay: bool) -> dict:
    return {
        "id": row["id"],
        "booking_code": row["booking_code"],
        "status": row["status"],
        "service_id": row["service_id"],
        "service_name": row["service_name"],
        "pet_name": row["pet_name"],
        "pet_type": row["pet_type"],
        "customer_name": row["customer_name"],
        "phone": row["phone"],
        "appointment_date": row["appointment_date"],
        "appointment_time": row["appointment_time"],
        "idempotent_replay": idempotent_replay,
    }


def find_booking_records(
    *, booking_code: str = "", phone: str = "", include_cancelled: bool = True
) -> list[dict]:
    """Find customer bookings by exact booking code or phone number."""
    code = booking_code.strip().upper()
    customer_phone = phone.strip()
    if not code and not customer_phone:
        raise BookingValidationError("预约编号或手机号至少提供一项")
    clauses = []
    values: list[str] = []
    if code:
        clauses.append("booking_code = ?")
        values.append(code)
    if customer_phone:
        clauses.append("phone = ?")
        values.append(customer_phone)
    status_clause = "" if include_cancelled else " AND status = 'confirmed'"
    with get_db() as db:
        rows = db.execute(
            f"SELECT * FROM appointments WHERE {' AND '.join(clauses)}{status_clause} "
            "ORDER BY appointment_date, appointment_time",
            values,
        ).fetchall()
    return [_booking_response(row, idempotent_replay=False) for row in rows]


def mask_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}" if re.fullmatch(r"\d{11}", phone) else "已隐藏"


def list_booking_records(*, mask_sensitive: bool = True) -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM appointments ORDER BY appointment_date, appointment_time"
        ).fetchall()
    values = [_booking_response(row, idempotent_replay=False) for row in rows]
    if mask_sensitive:
        for value in values:
            value["phone"] = mask_phone(value["phone"])
    return values


def _find_owned_booking(db, booking_code: str, phone: str) -> sqlite3.Row:
    row = db.execute(
        "SELECT * FROM appointments WHERE booking_code = ? AND phone = ?",
        (booking_code.strip().upper(), phone.strip()),
    ).fetchone()
    if row is None:
        raise BookingValidationError("未找到匹配的预约，请核对预约编号和手机号")
    return row


def reschedule_booking_record(
    booking_code: str,
    phone: str,
    appointment_date: str,
    appointment_time: str,
    *,
    customer_confirmed: bool,
) -> dict:
    """Move a confirmed booking after explicit customer confirmation."""
    if not customer_confirmed:
        raise BookingValidationError("改期前必须获得客户明确确认")
    with get_db() as db:
        current = _find_owned_booking(db, booking_code, phone)
    if current["status"] != "confirmed":
        raise BookingValidationError("只有已确认预约可以改期")
    config = current_business_config()
    service = _service(config, current["service_id"])
    if service is None:
        raise BookingValidationError("原预约服务已不在当前配置中，请联系门店")
    day, slot = _validate_schedule(
        appointment_date.strip(), appointment_time.strip(), service,
        config=config, now=business_now(config),
    )
    if current["appointment_date"] == day and current["appointment_time"] == slot:
        return _booking_response(current, idempotent_replay=True)
    if slot not in available_slots(day, service["id"]):
        raise BookingConflictError("新的预约时段不可用，请重新选择")
    try:
        with get_db() as db:
            db.execute(
                "UPDATE appointments SET appointment_date = ?, appointment_time = ? "
                "WHERE id = ? AND status = 'confirmed'",
                (day, slot, current["id"]),
            )
            updated = db.execute(
                "SELECT * FROM appointments WHERE id = ?", (current["id"],)
            ).fetchone()
    except sqlite3.IntegrityError as error:
        raise BookingConflictError("新的预约时段刚刚被占用，请重新选择") from error
    return _booking_response(updated, idempotent_replay=False)


def cancel_booking_record(
    booking_code: str,
    phone: str,
    *,
    customer_confirmed: bool,
) -> dict:
    """Cancel an owned booking after explicit customer confirmation."""
    if not customer_confirmed:
        raise BookingValidationError("取消前必须获得客户明确确认")
    with get_db() as db:
        current = _find_owned_booking(db, booking_code, phone)
        if current["status"] == "confirmed":
            db.execute(
                "UPDATE appointments SET status = 'cancelled' WHERE id = ?",
                (current["id"],),
            )
        updated = db.execute(
            "SELECT * FROM appointments WHERE id = ?", (current["id"],)
        ).fetchone()
    return _booking_response(updated, idempotent_replay=current["status"] == "cancelled")


class AppHandler(SimpleHTTPRequestHandler):
    # HTTP's Server header is encoded as Latin-1 by the standard library.
    server_version = "PawPilotAI/2.0"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, payload: object, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            config = current_business_config()
            today = business_now(config).date()
            self.send_json(
                {
                    "business": {
                        "name": config["business_name"],
                        "hours": format_business_hours(config),
                        "address": config["address"],
                    },
                    "services": config["services"],
                    "welcomeMessage": config["welcome_message"],
                    "configVersion": config["config_version"],
                    "timezone": config["timezone"],
                    "schedule": {
                        "openingTime": config["opening_time"],
                        "closingTime": config["closing_time"],
                        "closedWeekdays": config["closed_weekdays"],
                        "bookingWindowDays": config["booking_window_days"],
                        "appointmentSlots": config["appointment_slots"],
                    },
                    "today": today.isoformat(),
                    "maxDate": (today + timedelta(days=config["booking_window_days"])).isoformat(),
                }
            )
            return
        if parsed.path == "/api/onboarding/applications":
            self.send_json({"applications": list_onboarding_applications()})
            return
        if parsed.path == "/api/agent/status":
            self.send_json(agent_status())
            return
        if parsed.path == "/api/stt/status":
            self.send_json(stt_status())
            return
        if parsed.path == "/api/slots":
            query = parse_qs(parsed.query)
            day = query.get("date", [""])[0]
            service_id = query.get("service_id", [None])[0]
            self.send_json({"date": day, "slots": available_slots(day, service_id)})
            return
        if parsed.path == "/api/bookings":
            self.send_json({"bookings": list_booking_records(mask_sensitive=True)})
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/chat":
            self.handle_agent_chat()
            return
        if path == "/api/agent/interpret":
            self.handle_agent_interpret()
            return
        if path == "/api/transcribe":
            self.handle_transcribe()
            return
        if path == "/api/onboarding/applications":
            self.handle_onboarding_submit()
            return
        if path in {"/api/bookings/query", "/api/bookings/reschedule", "/api/bookings/cancel"}:
            self.handle_booking_operation(path.rsplit("/", 1)[-1])
            return
        match = re.fullmatch(r"/api/onboarding/applications/(\d+)/(collect|review|config|test|accept|activate)", path)
        if match:
            self.handle_onboarding_action(int(match.group(1)), match.group(2))
            return
        if path != "/api/bookings":
            self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self.read_json()
            header_key = self.headers.get("Idempotency-Key", "").strip()
            if header_key:
                payload["idempotency_key"] = header_key
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json({"error": "请求数据格式错误"}, HTTPStatus.BAD_REQUEST)
            return

        try:
            booking = create_booking_record(payload)
        except BookingValidationError as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        except BookingConflictError as error:
            self.send_json(
                {"error": str(error)}, HTTPStatus.CONFLICT
            )
            return
        self.send_json({**booking, "message": "预约成功"}, HTTPStatus.CREATED)

    def handle_booking_operation(self, operation: str) -> None:
        try:
            payload = self.read_json()
            if operation == "query":
                result = {"bookings": find_booking_records(
                    booking_code=str(payload.get("booking_code", "")),
                    phone=str(payload.get("phone", "")),
                )}
            elif operation == "reschedule":
                result = reschedule_booking_record(
                    str(payload.get("booking_code", "")), str(payload.get("phone", "")),
                    str(payload.get("appointment_date", "")), str(payload.get("appointment_time", "")),
                    customer_confirmed=payload.get("customer_confirmed") is True,
                )
            else:
                result = cancel_booking_record(
                    str(payload.get("booking_code", "")), str(payload.get("phone", "")),
                    customer_confirmed=payload.get("customer_confirmed") is True,
                )
            self.send_json(result)
        except (BookingValidationError, json.JSONDecodeError, UnicodeDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except BookingConflictError as error:
            self.send_json({"error": str(error)}, HTTPStatus.CONFLICT)

    def handle_onboarding_submit(self) -> None:
        try:
            self.send_json(submit_onboarding_application(self.read_json()), HTTPStatus.CREATED)
        except (OnboardingValidationError, json.JSONDecodeError, UnicodeDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def handle_onboarding_action(self, application_id: int, action: str) -> None:
        try:
            payload = self.read_json()
            if action in {"collect", "review", "test"}:
                result = advance_onboarding_application(application_id, action)
            elif action == "config":
                result = generate_onboarding_config(application_id, payload.get("config", payload))
            elif action == "accept":
                result = accept_onboarding_application(application_id, payload.get("checklist", payload))
            else:
                result = activate_onboarding_application(application_id)
            self.send_json(result)
        except (OnboardingValidationError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def handle_agent_chat(self) -> None:
        try:
            payload = self.read_json()
            session_id = str(payload.get("session_id", "")).strip()
            message = str(payload.get("message", "")).strip()
            if not session_id or not message:
                self.send_json(
                    {"error": "session_id 和 message 不能为空"},
                    HTTPStatus.BAD_REQUEST,
                )
                return
            if not agent_status()["configured"]:
                self.send_json(
                    {"error": "真实 Agent 未配置，请设置模型 API 环境变量"},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            from booking_agent import get_booking_agent, run_agent_turn

            result = run_agent_turn(get_booking_agent(), session_id, message)
            self.send_json(result)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_json({"error": "请求数据格式错误"}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            self.send_json({"error": f"Agent 调用失败：{error}"}, HTTPStatus.BAD_GATEWAY)

    def handle_agent_interpret(self) -> None:
        try:
            payload = self.read_json()
            text = str(payload.get("text", "")).strip()
            step = str(payload.get("step", "")).strip()
            context = payload.get("context", {})
            if not text or step not in {"service", "date", "time", "confirm"}:
                self.send_json({"error": "缺少有效的 text 或 step"}, HTTPStatus.BAD_REQUEST)
                return
            result = interpret_text(text, step, context)
            self.send_json({"result": result})
        except RuntimeError as error:
            self.send_json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
        except (KeyError, ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": f"模型响应无法解析：{error}"}, HTTPStatus.BAD_GATEWAY)
        except Exception as error:
            self.send_json({"error": f"模型 API 调用失败：{error}"}, HTTPStatus.BAD_GATEWAY)

    def handle_transcribe(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            self.send_json({"error": "录音内容为空"}, HTTPStatus.BAD_REQUEST)
            return
        if length > 15 * 1024 * 1024:
            self.send_json({"error": "录音文件不能超过 15MB"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        audio = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "application/octet-stream")
        try:
            text = transcribe_audio(audio, content_type)
            self.send_json({"text": text})
        except RuntimeError as error:
            self.send_json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
        except (ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)
        except Exception as error:
            self.send_json({"error": f"语音识别调用失败：{error}"}, HTTPStatus.BAD_GATEWAY)

    def serve_static(self, request_path: str) -> None:
        relative = request_path.lstrip("/") or "index.html"
        candidate = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in candidate.parents and candidate != STATIC_DIR.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            candidate = STATIC_DIR / "index.html"
        data = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run() -> None:
    init_db()
    if os.getenv("PAWPILOT_STT_PROVIDER", "api").strip().lower() == "sensevoice":
        def preload_sensevoice() -> None:
            try:
                from sensevoice_stt import warmup_sensevoice

                status = warmup_sensevoice()
                print(f"SenseVoice 预热完成：{status['model']} ({status['device']})")
            except Exception as error:
                print(f"SenseVoice 预热失败，将在首次录音时重试：{error}")

        threading.Thread(
            target=preload_sensevoice,
            name="sensevoice-warmup",
            daemon=True,
        ).start()
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"PawPilot AI语音预约系统已启动：http://{HOST}:{PORT}")
    print("按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
