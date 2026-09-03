from __future__ import annotations

import json
import logging
import mimetypes
import re
import time
import uuid
from datetime import timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from app.core.logging import log_event

logger = logging.getLogger("pawpilot.http")


class AppHandler(SimpleHTTPRequestHandler):
    server_version = "PawPilotAI/3.0"

    def _start_request(self) -> None:
        self.request_id = self.headers.get("X-Request-ID", "").strip() or uuid.uuid4().hex[:12]
        self.request_started = time.perf_counter()

    def log_message(self, fmt: str, *args: object) -> None:
        log_event(
            logger,
            "http_request",
            request_id=getattr(self, "request_id", None),
            method=self.command,
            path=self.path,
            message=fmt % args,
            duration_ms=round((time.perf_counter() - getattr(self, "request_started", time.perf_counter())) * 1000),
        )

    def send_json(self, payload: object, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-ID", getattr(self, "request_id", ""))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求数据必须是 JSON 对象")
        return value

    def do_GET(self) -> None:  # noqa: N802
        import server

        self._start_request()
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self.send_json({"status": "ok", "service": "pawpilot", "version": "3.0"})
            return
        if parsed.path == "/api/config":
            config = server.current_business_config()
            today = server.business_now(config).date()
            self.send_json({
                "business": {
                    "name": config["business_name"],
                    "hours": server.format_business_hours(config),
                    "address": config["address"],
                },
                "services": config["services"], "welcomeMessage": config["welcome_message"],
                "configVersion": config["config_version"], "timezone": config["timezone"],
                "schedule": {
                    "openingTime": config["opening_time"], "closingTime": config["closing_time"],
                    "closedWeekdays": config["closed_weekdays"],
                    "bookingWindowDays": config["booking_window_days"],
                    "appointmentSlots": config["appointment_slots"],
                },
                "today": today.isoformat(),
                "maxDate": (today + timedelta(days=config["booking_window_days"])).isoformat(),
            })
            return
        if parsed.path == "/api/onboarding/applications":
            self.send_json({"applications": server.list_onboarding_applications()})
            return
        if parsed.path == "/api/agent/status":
            self.send_json(server.agent_status())
            return
        if parsed.path == "/api/stt/status":
            self.send_json(server.stt_status())
            return
        if parsed.path == "/api/slots":
            query = parse_qs(parsed.query)
            day = query.get("date", [""])[0]
            service_id = query.get("service_id", [None])[0]
            self.send_json({"date": day, "slots": server.available_slots(day, service_id)})
            return
        if parsed.path == "/api/bookings":
            self.send_json({"bookings": server.list_booking_records(mask_sensitive=True)})
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802
        self._start_request()
        path = urlparse(self.path).path
        if path == "/api/chat":
            self.handle_agent_chat()
        elif path == "/api/agent/interpret":
            self.handle_agent_interpret()
        elif path == "/api/transcribe":
            self.handle_transcribe()
        elif path == "/api/onboarding/applications":
            self.handle_onboarding_submit()
        elif path in {"/api/bookings/query", "/api/bookings/reschedule", "/api/bookings/cancel"}:
            self.handle_booking_operation(path.rsplit("/", 1)[-1])
        elif match := re.fullmatch(
            r"/api/onboarding/applications/(\d+)/(collect|review|config|test|accept|activate)", path
        ):
            self.handle_onboarding_action(int(match.group(1)), match.group(2))
        elif path == "/api/bookings":
            self.handle_booking_create()
        else:
            self.send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def handle_booking_create(self) -> None:
        import server
        try:
            payload = self.read_json()
            if key := self.headers.get("Idempotency-Key", "").strip():
                payload["idempotency_key"] = key
            booking = server.create_booking_record(payload)
            self.send_json({**booking, "message": "预约成功"}, HTTPStatus.CREATED)
        except (server.BookingValidationError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except server.BookingConflictError as error:
            self.send_json({"error": str(error)}, HTTPStatus.CONFLICT)

    def handle_booking_operation(self, operation: str) -> None:
        import server
        try:
            payload = self.read_json()
            if operation == "query":
                result = {"bookings": server.find_booking_records(
                    booking_code=str(payload.get("booking_code", "")), phone=str(payload.get("phone", ""))
                )}
            elif operation == "reschedule":
                result = server.reschedule_booking_record(
                    str(payload.get("booking_code", "")), str(payload.get("phone", "")),
                    str(payload.get("appointment_date", "")), str(payload.get("appointment_time", "")),
                    customer_confirmed=payload.get("customer_confirmed") is True,
                )
            else:
                result = server.cancel_booking_record(
                    str(payload.get("booking_code", "")), str(payload.get("phone", "")),
                    customer_confirmed=payload.get("customer_confirmed") is True,
                )
            self.send_json(result)
        except (server.BookingValidationError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except server.BookingConflictError as error:
            self.send_json({"error": str(error)}, HTTPStatus.CONFLICT)

    def handle_onboarding_submit(self) -> None:
        import server
        try:
            self.send_json(server.submit_onboarding_application(self.read_json()), HTTPStatus.CREATED)
        except (server.OnboardingValidationError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def handle_onboarding_action(self, application_id: int, action: str) -> None:
        import server
        try:
            payload = self.read_json()
            if action in {"collect", "review", "test"}:
                result = server.advance_onboarding_application(application_id, action)
            elif action == "config":
                result = server.generate_onboarding_config(application_id, payload.get("config", payload))
            elif action == "accept":
                result = server.accept_onboarding_application(application_id, payload.get("checklist", payload))
            else:
                result = server.activate_onboarding_application(application_id)
            self.send_json(result)
        except (server.OnboardingValidationError, ValueError, json.JSONDecodeError, UnicodeDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

    def handle_agent_chat(self) -> None:
        import server
        try:
            payload = self.read_json()
            session_id = str(payload.get("session_id", "")).strip()
            message = str(payload.get("message", "")).strip()
            if not session_id or not message:
                self.send_json({"error": "session_id 和 message 不能为空"}, HTTPStatus.BAD_REQUEST)
                return
            if not server.agent_status()["configured"]:
                self.send_json({"error": "真实 Agent 未配置，请设置模型 API 环境变量"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            from booking_agent import get_booking_agent, run_agent_turn
            result = run_agent_turn(get_booking_agent(), session_id, message)
            log_event(logger, "agent_turn", request_id=self.request_id, thread_id=session_id,
                      stage=result.get("workflow", {}).get("stage"), duration_ms=result.get("latency_ms"))
            self.send_json(result)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self.send_json({"error": "请求数据格式错误"}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            log_event(logger, "agent_error", request_id=self.request_id, error=type(error).__name__)
            self.send_json({"error": "Agent 服务暂时不可用，请稍后重试"}, HTTPStatus.BAD_GATEWAY)

    def handle_agent_interpret(self) -> None:
        import server
        try:
            payload = self.read_json()
            text = str(payload.get("text", "")).strip()
            step = str(payload.get("step", "")).strip()
            if not text or step not in {"service", "date", "time", "confirm"}:
                self.send_json({"error": "缺少有效的 text 或 step"}, HTTPStatus.BAD_REQUEST)
                return
            self.send_json({"result": server.interpret_text(text, step, payload.get("context", {}))})
        except RuntimeError as error:
            self.send_json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
        except Exception as error:
            log_event(logger, "interpret_error", request_id=self.request_id, error=type(error).__name__)
            self.send_json({"error": "模型响应暂时无法解析"}, HTTPStatus.BAD_GATEWAY)

    def handle_transcribe(self) -> None:
        import server
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            self.send_json({"error": "录音内容为空"}, HTTPStatus.BAD_REQUEST)
            return
        if length > 15 * 1024 * 1024:
            self.send_json({"error": "录音文件不能超过 15MB"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        try:
            text = server.transcribe_audio(
                self.rfile.read(length), self.headers.get("Content-Type", "application/octet-stream")
            )
            self.send_json({"text": text})
        except RuntimeError as error:
            self.send_json({"error": str(error)}, HTTPStatus.SERVICE_UNAVAILABLE)
        except Exception as error:
            log_event(logger, "stt_error", request_id=self.request_id, error=type(error).__name__)
            self.send_json({"error": "语音识别暂时不可用"}, HTTPStatus.BAD_GATEWAY)

    def serve_static(self, request_path: str) -> None:
        import server
        relative = request_path.lstrip("/") or "index.html"
        candidate = (server.STATIC_DIR / relative).resolve()
        if server.STATIC_DIR.resolve() not in candidate.parents and candidate != server.STATIC_DIR.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            candidate = server.STATIC_DIR / "index.html"
        data = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
