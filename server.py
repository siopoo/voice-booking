from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import threading
import uuid
from collections.abc import MutableMapping
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen as default_urlopen


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DB_PATH = ROOT / "appointments.db"
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

SERVICES = [
    {"id": "basic", "name": "基础洗护", "duration": 60, "price": 88},
    {"id": "grooming", "name": "精致美容", "duration": 90, "price": 168},
    {"id": "spa", "name": "深度护理", "duration": 90, "price": 238},
]
SLOTS = ["10:00", "11:30", "14:00", "15:30", "17:00"]


class BookingValidationError(ValueError):
    pass


class BookingConflictError(RuntimeError):
    pass


def agent_status(env: dict[str, str] | None = None) -> dict:
    source = os.environ if env is None else env
    key = source.get("PAWPILOT_LLM_API_KEY", "").strip()
    model = source.get("PAWPILOT_LLM_MODEL", "").strip()
    return {"configured": bool(key and model), "model": model or None}


def stt_status(env: dict[str, str] | None = None) -> dict:
    source = os.environ if env is None else env
    if source.get("PAWPILOT_STT_PROVIDER", "api").strip().lower() == "sensevoice":
        model = source.get("PAWPILOT_SENSEVOICE_MODEL", "iic/SenseVoiceSmall").strip()
        return {"configured": bool(model), "model": model or None}
    key = source.get("PAWPILOT_STT_API_KEY", "").strip()
    model = source.get("PAWPILOT_STT_MODEL", "").strip()
    return {"configured": bool(key and model), "model": model or None}


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
    prompt = (
        "你是宠物护理预约系统的意图解析器。只返回 JSON，不要解释。"
        "格式为 {\"value\": 任意JSON值, \"confidence\": 0到1的数字}。"
        "service 步骤的 value 只能是 basic、grooming、spa 或 null；"
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


def available_slots(day: str) -> list[str]:
    try:
        target = date.fromisoformat(day)
    except ValueError:
        return []
    if target < date.today() or target.weekday() == 0:
        return []
    with get_db() as db:
        rows = db.execute(
            "SELECT appointment_time FROM appointments "
            "WHERE appointment_date = ? AND status = 'confirmed'",
            (day,),
        ).fetchall()
    occupied = {row["appointment_time"] for row in rows}
    return [slot for slot in SLOTS if slot not in occupied]


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
    service = next(
        (item for item in SERVICES if item["id"] == payload.get("service_id")), None
    )
    if missing or service is None:
        raise BookingValidationError("预约信息不完整或服务项目无效")
    day = str(payload["appointment_date"])
    time = str(payload["appointment_time"])
    fingerprint_fields = {
        key: str(payload.get(key, "")).strip()
        for key in required
    }
    idempotency_key = str(payload.get("idempotency_key", "")).strip() or hashlib.sha256(
        json.dumps(fingerprint_fields, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    with get_db() as db:
        existing = db.execute(
            "SELECT * FROM appointments WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
    if existing is not None:
        return _booking_response(existing, idempotent_replay=True)
    if time not in available_slots(day):
        raise BookingConflictError("该时段刚刚已被预约或不可用，请重新选择")
    now = datetime.now()
    code = f"PP{now:%m%d%H%M%S%f}"[:-3]
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
                    str(payload["phone"]).strip(),
                    day,
                    time,
                    str(payload.get("notes", "")).strip(),
                    now.isoformat(timespec="seconds"),
                    idempotency_key,
                ),
            )
            booking_id = cursor.lastrowid
    except sqlite3.IntegrityError as error:
        with get_db() as db:
            existing = db.execute(
                "SELECT * FROM appointments WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if existing is not None:
            return _booking_response(existing, idempotent_replay=True)
        raise BookingConflictError("该时段刚刚已被预约，请重新选择") from error
    return {
        "id": booking_id,
        "booking_code": code,
        "status": "confirmed",
        "service_id": service["id"],
        "service_name": service["name"],
        "pet_name": str(payload["pet_name"]).strip(),
        "pet_type": str(payload["pet_type"]).strip(),
        "customer_name": str(payload["customer_name"]).strip(),
        "phone": str(payload["phone"]).strip(),
        "appointment_date": day,
        "appointment_time": time,
        "idempotent_replay": False,
    }


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
    day = appointment_date.strip()
    slot = appointment_time.strip()
    with get_db() as db:
        current = _find_owned_booking(db, booking_code, phone)
    if current["status"] != "confirmed":
        raise BookingValidationError("只有已确认预约可以改期")
    if current["appointment_date"] == day and current["appointment_time"] == slot:
        return _booking_response(current, idempotent_replay=True)
    if slot not in available_slots(day):
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
    server_version = "PawPilotDemo/1.0"

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
            today = date.today()
            self.send_json(
                {
                    "business": {
                        "name": "PawPilot 宠物护理中心",
                        "hours": "周二至周日 10:00–18:00，周一休息",
                        "address": "上海市静安区示范路 88 号",
                    },
                    "services": SERVICES,
                    "today": today.isoformat(),
                    "maxDate": (today + timedelta(days=14)).isoformat(),
                }
            )
            return
        if parsed.path == "/api/agent/status":
            self.send_json(agent_status())
            return
        if parsed.path == "/api/stt/status":
            self.send_json(stt_status())
            return
        if parsed.path == "/api/slots":
            day = parse_qs(parsed.query).get("date", [""])[0]
            self.send_json({"date": day, "slots": available_slots(day)})
            return
        if parsed.path == "/api/bookings":
            with get_db() as db:
                rows = db.execute(
                    "SELECT * FROM appointments ORDER BY appointment_date, appointment_time"
                ).fetchall()
            self.send_json({"bookings": [dict(row) for row in rows]})
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
        if path != "/api/bookings":
            self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self.read_json()
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
    print(f"PawPilot Demo 已启动：http://{HOST}:{PORT}")
    print("按 Ctrl+C 停止服务")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
