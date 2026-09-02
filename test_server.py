import json
import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server


class EnvironmentFileTests(unittest.TestCase):
    def test_load_env_file_reads_values_without_overriding_process_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "# PawPilot model settings\n"
                "PAWPILOT_LLM_API_KEY=key-from-file\n"
                "PAWPILOT_LLM_MODEL=\"deepseek-chat\"\n"
                "PAWPILOT_LLM_BASE_URL='https://api.deepseek.com'\n",
                encoding="utf-8",
            )
            target = {"PAWPILOT_LLM_API_KEY": "key-from-process"}
            load_env_file = getattr(server, "load_env_file", lambda *_args, **_kwargs: None)

            load_env_file(env_path, target)

        self.assertEqual(target["PAWPILOT_LLM_API_KEY"], "key-from-process")
        self.assertEqual(target.get("PAWPILOT_LLM_MODEL"), "deepseek-chat")
        self.assertEqual(target.get("PAWPILOT_LLM_BASE_URL"), "https://api.deepseek.com")


class SpeechApiRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def test_stt_status_route_reports_configuration(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PAWPILOT_STT_PROVIDER": "api",
                "PAWPILOT_STT_API_KEY": "audio-key",
                "PAWPILOT_STT_MODEL": "whisper-1",
            },
        ):
            with urlopen(f"{self.base_url}/api/stt/status", timeout=2) as response:
                content_type = response.headers.get_content_type()
                body = response.read().decode("utf-8")
        self.assertEqual(content_type, "application/json")
        payload = json.loads(body)
        self.assertEqual(payload, {"configured": True, "model": "whisper-1"})

    def test_transcribe_route_returns_recognized_text(self) -> None:
        request = Request(
            f"{self.base_url}/api/transcribe",
            data=b"recorded-audio",
            method="POST",
            headers={"Content-Type": "audio/webm"},
        )
        with patch.object(server, "transcribe_audio", return_value="我想预约基础洗护"):
            try:
                response = urlopen(request, timeout=2)
            except HTTPError as error:
                response = error
            with response:
                status = response.status
                content_type = response.headers.get_content_type()
                body = response.read().decode("utf-8")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "application/json")
        self.assertEqual(json.loads(body), {"text": "我想预约基础洗护"})


class BookingDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.patch = patch.object(server, "DB_PATH", self.db_path)
        self.patch.start()
        server.init_db()

    def tearDown(self) -> None:
        self.patch.stop()
        self.temp_dir.cleanup()

    def test_future_open_day_has_slots(self) -> None:
        target = date.today() + timedelta(days=1)
        while target.weekday() == 0:
            target += timedelta(days=1)
        self.assertEqual(server.available_slots(target.isoformat()), server.SLOTS)

    def test_monday_is_closed(self) -> None:
        target = date.today()
        while target.weekday() != 0:
            target += timedelta(days=1)
        self.assertEqual(server.available_slots(target.isoformat()), [])

    def test_booked_slot_is_removed(self) -> None:
        target = date.today() + timedelta(days=1)
        while target.weekday() == 0:
            target += timedelta(days=1)
        day = target.isoformat()
        with server.get_db() as db:
            db.execute(
                """INSERT INTO appointments (
                    booking_code, service_id, service_name, pet_name, pet_type,
                    customer_name, phone, appointment_date, appointment_time,
                    notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "TEST001", "basic", "基础洗护", "可乐", "狗", "陈女士",
                    "13800138000", day, "10:00", "", "2026-01-01T00:00:00"
                ),
            )
        self.assertNotIn("10:00", server.available_slots(day))
        self.assertIn("11:30", server.available_slots(day))

    def test_database_context_releases_file_handle(self) -> None:
        with server.get_db() as db:
            db.execute("SELECT 1").fetchone()
        self.db_path.unlink()
        self.assertFalse(self.db_path.exists())

    def test_repeated_booking_confirmation_returns_original_record(self) -> None:
        target = date.today() + timedelta(days=1)
        while target.weekday() == 0:
            target += timedelta(days=1)
        payload = {
            "service_id": "basic",
            "pet_name": "豆包",
            "pet_type": "狗",
            "customer_name": "王女士",
            "phone": "13700137000",
            "appointment_date": target.isoformat(),
            "appointment_time": "14:00",
            "idempotency_key": "session-123-confirmation-1",
        }

        first = server.create_booking_record(payload)
        second = server.create_booking_record(payload)
        with server.get_db() as db:
            count = db.execute("SELECT COUNT(*) AS total FROM appointments").fetchone()["total"]

        self.assertEqual(second["booking_code"], first["booking_code"])
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(count, 1)

    def test_init_db_migrates_existing_appointments_table(self) -> None:
        self.db_path.unlink()
        legacy_db = sqlite3.connect(self.db_path)
        try:
            db = legacy_db
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
                    UNIQUE(appointment_date, appointment_time)
                )
                """
            )
            db.commit()
        finally:
            legacy_db.close()

        server.init_db()
        with server.get_db() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(appointments)")}
        self.assertIn("idempotency_key", columns)

    def _create_sample_booking(self, time: str = "10:00") -> dict:
        target = date.today() + timedelta(days=1)
        while target.weekday() == 0:
            target += timedelta(days=1)
        return server.create_booking_record(
            {
                "service_id": "grooming",
                "pet_name": "可乐",
                "pet_type": "狗",
                "customer_name": "陈女士",
                "phone": "13800138000",
                "appointment_date": target.isoformat(),
                "appointment_time": time,
            }
        )

    def test_find_bookings_returns_only_matching_customer_records(self) -> None:
        created = self._create_sample_booking()
        finder = getattr(server, "find_booking_records", None)
        self.assertIsNotNone(finder, "需要按预约编号或手机号查询预约")

        matched = finder(phone="13800138000")
        missing = finder(phone="13900139000")

        self.assertEqual([item["booking_code"] for item in matched], [created["booking_code"]])
        self.assertEqual(missing, [])

    def test_reschedule_requires_confirmation_and_moves_the_reserved_slot(self) -> None:
        created = self._create_sample_booking("10:00")
        target = date.fromisoformat(created["appointment_date"])
        while True:
            target += timedelta(days=1)
            if target.weekday() != 0:
                break
        reschedule = getattr(server, "reschedule_booking_record", None)
        self.assertIsNotNone(reschedule, "需要可审计的改期业务操作")

        with self.assertRaisesRegex(server.BookingValidationError, "明确确认"):
            reschedule(
                created["booking_code"],
                "13800138000",
                target.isoformat(),
                "11:30",
                customer_confirmed=False,
            )

        updated = reschedule(
            created["booking_code"],
            "13800138000",
            target.isoformat(),
            "11:30",
            customer_confirmed=True,
        )

        self.assertEqual(updated["appointment_date"], target.isoformat())
        self.assertEqual(updated["appointment_time"], "11:30")
        self.assertIn("10:00", server.available_slots(created["appointment_date"]))
        self.assertNotIn("11:30", server.available_slots(target.isoformat()))

    def test_cancel_requires_confirmation_and_releases_the_slot(self) -> None:
        created = self._create_sample_booking("14:00")
        cancel = getattr(server, "cancel_booking_record", None)
        self.assertIsNotNone(cancel, "需要可审计的取消预约业务操作")

        with self.assertRaisesRegex(server.BookingValidationError, "明确确认"):
            cancel(created["booking_code"], "13800138000", customer_confirmed=False)

        cancelled = cancel(
            created["booking_code"],
            "13800138000",
            customer_confirmed=True,
        )

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIn("14:00", server.available_slots(created["appointment_date"]))
        replacement = server.create_booking_record(
            {
                "service_id": "basic",
                "pet_name": "豆包",
                "pet_type": "狗",
                "customer_name": "王女士",
                "phone": "13700137000",
                "appointment_date": created["appointment_date"],
                "appointment_time": "14:00",
            }
        )
        self.assertEqual(replacement["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()


class AgentApiTests(unittest.TestCase):
    def test_agent_status_is_disabled_without_credentials(self) -> None:
        status = server.agent_status({})
        self.assertEqual(status, {"configured": False, "model": None})

    def test_agent_status_never_exposes_api_key(self) -> None:
        status = server.agent_status(
            {
                "PAWPILOT_LLM_API_KEY": "secret-value",
                "PAWPILOT_LLM_MODEL": "deepseek-chat",
                "PAWPILOT_LLM_BASE_URL": "https://api.deepseek.com",
            }
        )
        self.assertEqual(status, {"configured": True, "model": "deepseek-chat"})
        self.assertNotIn("secret-value", json.dumps(status))

    def test_stt_status_requires_separate_audio_model_credentials(self) -> None:
        status_function = getattr(server, "stt_status", lambda _env: {})
        status = status_function(
            {
                "PAWPILOT_STT_API_KEY": "audio-secret",
                "PAWPILOT_STT_MODEL": "whisper-1",
                "PAWPILOT_STT_BASE_URL": "https://api.openai.com/v1",
            }
        )
        self.assertEqual(status, {"configured": True, "model": "whisper-1"})
        self.assertNotIn("audio-secret", json.dumps(status))

    def test_stt_status_enables_local_sensevoice_without_api_key(self) -> None:
        status = server.stt_status(
            {
                "PAWPILOT_STT_PROVIDER": "sensevoice",
                "PAWPILOT_SENSEVOICE_MODEL": "iic/SenseVoiceSmall",
            }
        )
        self.assertEqual(status, {"configured": True, "model": "iic/SenseVoiceSmall"})

    def test_transcribe_audio_dispatches_to_local_sensevoice(self) -> None:
        received = []

        def fake_local_transcriber(audio, content_type, env):
            received.append((audio, content_type, env["PAWPILOT_SENSEVOICE_DEVICE"]))
            return "我想预约基础洗护"

        try:
            result = server.transcribe_audio(
                b"webm-audio",
                "audio/webm",
                env={
                    "PAWPILOT_STT_PROVIDER": "sensevoice",
                    "PAWPILOT_SENSEVOICE_MODEL": "iic/SenseVoiceSmall",
                    "PAWPILOT_SENSEVOICE_DEVICE": "cuda:0",
                },
                local_transcriber=fake_local_transcriber,
            )
        except TypeError:
            result = None
        self.assertEqual(result, "我想预约基础洗护")
        self.assertEqual(received, [(b"webm-audio", "audio/webm", "cuda:0")])

    def test_transcribe_audio_calls_openai_compatible_audio_endpoint(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return json.dumps({"text": "我想预约基础洗护"}).encode("utf-8")

        def fake_urlopen(request, timeout):
            self.assertEqual(request.full_url, "https://api.openai.com/v1/audio/transcriptions")
            self.assertEqual(timeout, 45)
            self.assertEqual(request.headers["Authorization"], "Bearer audio-key")
            self.assertTrue(request.headers["Content-type"].startswith("multipart/form-data; boundary="))
            self.assertIn(b"whisper-1", request.data)
            self.assertIn(b"audio-bytes", request.data)
            return FakeResponse()

        transcribe = getattr(server, "transcribe_audio", lambda *_args, **_kwargs: None)
        result = transcribe(
            b"audio-bytes",
            "audio/webm",
            env={
                "PAWPILOT_STT_API_KEY": "audio-key",
                "PAWPILOT_STT_MODEL": "whisper-1",
                "PAWPILOT_STT_BASE_URL": "https://api.openai.com/v1",
            },
            urlopen=fake_urlopen,
        )
        self.assertEqual(result, "我想预约基础洗护")

    def test_interpret_text_returns_structured_value(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "content": '```json\n{"value":"grooming","confidence":0.97}\n```'
                                }
                            }
                        ]
                    }
                ).encode("utf-8")

        def fake_urlopen(request, timeout):
            self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
            self.assertEqual(timeout, 15)
            self.assertEqual(request.headers["Authorization"], "Bearer test-key")
            return FakeResponse()

        result = server.interpret_text(
            text="我想给狗狗做个造型",
            step="service",
            context={"services": server.SERVICES},
            env={
                "PAWPILOT_LLM_API_KEY": "test-key",
                "PAWPILOT_LLM_MODEL": "deepseek-chat",
                "PAWPILOT_LLM_BASE_URL": "https://api.deepseek.com",
            },
            urlopen=fake_urlopen,
        )
        self.assertEqual(result, {"value": "grooming", "confidence": 0.97})
