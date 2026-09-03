from __future__ import annotations

import logging
import os
import threading
from collections.abc import MutableMapping
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen as default_urlopen

from app.core.config import ROOT, Settings
from app.core.config import load_env_file as _load_env_file
from app.core.exceptions import (
    BookingConflictError,
    BookingValidationError,
    OnboardingValidationError,
)
from app.core.logging import configure_logging, mask_phone
from app.db.connection import Database
from app.repositories.booking_repository import BookingRepository
from app.repositories.onboarding_repository import OnboardingRepository
from app.services.booking_service import BookingService, booking_response
from app.services.onboarding_service import ACCEPTANCE_CHECKLIST_KEYS, OnboardingService
from app.services.speech_service import (
    agent_status as _agent_status,
)
from app.services.speech_service import (
    interpret_text as _interpret_text,
)
from app.services.speech_service import (
    stt_status as _stt_status,
)
from app.services.speech_service import (
    transcribe_audio as _transcribe_audio,
)
from business_config import business_timezone, format_business_hours, load_business_config

__all__ = [
    "ACCEPTANCE_CHECKLIST_KEYS", "AppHandler", "BookingConflictError",
    "BookingValidationError", "OnboardingValidationError", "ThreadingHTTPServer",
    "format_business_hours",
]


def load_env_file(
    path: Path = ROOT / ".env",
    target: MutableMapping[str, str] | None = None,
) -> None:
    _load_env_file(path, os.environ if target is None else target)


load_env_file()
SETTINGS = Settings.load(environ=os.environ)
configure_logging(SETTINGS.log_level)
logger = logging.getLogger("pawpilot")

STATIC_DIR = ROOT / "static"
DB_PATH = SETTINGS.database_path
BUSINESS_CONFIG_PATH = SETTINGS.business_config_path
HOST = SETTINGS.host
PORT = SETTINGS.port


def current_business_config() -> dict:
    return load_business_config(BUSINESS_CONFIG_PATH)


def business_now(config: dict | None = None) -> datetime:
    active = current_business_config() if config is None else config
    return datetime.now(business_timezone(active["timezone"]))


_INITIAL_CONFIG = current_business_config()
SERVICES = _INITIAL_CONFIG["services"]
SLOTS = _INITIAL_CONFIG["appointment_slots"]


def _database() -> Database:
    return Database(DB_PATH)


def _booking_service() -> BookingService:
    return BookingService(
        BookingRepository(_database()),
        current_business_config,
        clock=lambda config: business_now(config),
    )


def _onboarding_service() -> OnboardingService:
    return OnboardingService(
        OnboardingRepository(_database()),
        lambda: Path(BUSINESS_CONFIG_PATH),
    )


def get_db():
    """Compatibility context manager; new code uses repositories."""
    return _database().connect()


def init_db() -> None:
    database = _database()
    database.init_schema()
    OnboardingRepository(database).init_schema()


def agent_status(env: dict[str, str] | None = None) -> dict:
    return _agent_status(os.environ if env is None else env)


def stt_status(env: dict[str, str] | None = None) -> dict:
    source = os.environ if env is None else env
    runtime_status = None
    if env is None and source.get("PAWPILOT_STT_PROVIDER", "api").lower() == "sensevoice":
        try:
            from sensevoice_stt import sensevoice_runtime_status
            runtime_status = sensevoice_runtime_status
        except Exception as error:
            message = str(error)[:300]

            def runtime_status():
                return {"state": "error", "ready": False, "error": message}
    return _stt_status(source, runtime_status)


def transcribe_audio(
    audio: bytes,
    content_type: str,
    env: dict[str, str] | None = None,
    urlopen=default_urlopen,
    local_transcriber=None,
) -> str:
    return _transcribe_audio(
        audio,
        content_type,
        os.environ if env is None else env,
        urlopen=urlopen,
        local_transcriber=local_transcriber,
    )


def interpret_text(
    text: str,
    step: str,
    context: dict,
    env: dict[str, str] | None = None,
    urlopen=default_urlopen,
) -> dict:
    source = os.environ if env is None else env
    return _interpret_text(
        text,
        step,
        context,
        source,
        [item["id"] for item in current_business_config()["services"]],
        urlopen=urlopen,
    )


def available_slots(day: str, service_id: str | None = None, *, now: datetime | None = None) -> list[str]:
    service = _booking_service()
    if now is not None:
        service.clock = lambda _config: now
    return service.available_slots(day, service_id)


def create_booking_record(payload: dict, *, customer_confirmed: bool = True) -> dict:
    return _booking_service().create(payload, customer_confirmed=customer_confirmed)


def find_booking_records(
    *, booking_code: str = "", phone: str = "", include_cancelled: bool = True
) -> list[dict]:
    return _booking_service().find(
        booking_code=booking_code, phone=phone, include_cancelled=include_cancelled
    )


def list_booking_records(*, mask_sensitive: bool = True) -> list[dict]:
    values = [booking_response(row) for row in BookingRepository(_database()).list_all()]
    if mask_sensitive:
        for value in values:
            value["phone"] = mask_phone(value["phone"])
    return values


def reschedule_booking_record(
    booking_code: str,
    phone: str,
    appointment_date: str,
    appointment_time: str,
    *,
    customer_confirmed: bool,
) -> dict:
    return _booking_service().reschedule(
        booking_code,
        phone,
        appointment_date,
        appointment_time,
        customer_confirmed=customer_confirmed,
    )


def cancel_booking_record(
    booking_code: str,
    phone: str,
    *,
    customer_confirmed: bool,
) -> dict:
    return _booking_service().cancel(
        booking_code, phone, customer_confirmed=customer_confirmed
    )


def get_onboarding_application(application_id: int) -> dict:
    return _onboarding_service().get(application_id)


def list_onboarding_applications() -> list[dict]:
    return _onboarding_service().list()


def submit_onboarding_application(payload: dict) -> dict:
    return _onboarding_service().submit(payload)


def advance_onboarding_application(application_id: int, action: str) -> dict:
    return _onboarding_service().advance(application_id, action)


def generate_onboarding_config(application_id: int, config: dict) -> dict:
    return _onboarding_service().generate_config(application_id, config)


def accept_onboarding_application(application_id: int, checklist: dict) -> dict:
    return _onboarding_service().accept(application_id, checklist)


def activate_onboarding_application(application_id: int) -> dict:
    return _onboarding_service().activate(application_id)


# Imported late so the transport can call this compatibility facade without a cycle.
from app.api.http import AppHandler  # noqa: E402


def run() -> None:
    init_db()
    if os.getenv("PAWPILOT_STT_PROVIDER", "api").strip().lower() == "sensevoice":
        def preload_sensevoice() -> None:
            try:
                from sensevoice_stt import warmup_sensevoice
                status = warmup_sensevoice()
                logger.info("SenseVoice ready: %s (%s)", status["model"], status["device"])
            except Exception as error:
                logger.warning("SenseVoice warmup failed: %s", type(error).__name__)

        threading.Thread(target=preload_sensevoice, name="sensevoice-warmup", daemon=True).start()
    httpd = ThreadingHTTPServer((HOST, PORT), AppHandler)
    logger.info("PawPilot started at http://%s:%s", HOST, PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("PawPilot stopped")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    run()
