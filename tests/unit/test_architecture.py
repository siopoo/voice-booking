from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.core.config import Settings
from app.core.exceptions import BookingValidationError
from app.core.logging import mask_sensitive
from app.db.connection import Database
from app.repositories.booking_repository import BookingRepository
from app.services.booking_service import BookingService
from business_config import DEFAULT_CONFIG, business_timezone


def test_settings_loads_env_file_without_overriding_process_values(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PAWPILOT_LLM_MODEL=from-file\nPAWPILOT_PORT=9000\n",
        encoding="utf-8",
    )
    settings = Settings.load(
        env_file=env_file,
        environ={"PAWPILOT_LLM_MODEL": "from-process"},
    )

    assert settings.llm_model == "from-process"
    assert settings.port == 9000
    assert settings.llm_api_key == ""


def test_sensitive_logging_masks_nested_phone_and_secret():
    masked = mask_sensitive(
        {"phone": "13800138000", "headers": {"Authorization": "Bearer secret"}}
    )

    assert masked["phone"] == "138****8000"
    assert masked["headers"]["Authorization"] == "***"
    assert "secret" not in json.dumps(masked)


def test_booking_service_requires_confirmation_and_persists_once(tmp_path):
    database = Database(tmp_path / "bookings.db")
    database.init_schema()
    repository = BookingRepository(database)
    config = {**DEFAULT_CONFIG, "closed_weekdays": []}
    now = datetime(2030, 1, 8, 9, 0, tzinfo=business_timezone("Asia/Shanghai"))
    service = BookingService(repository, lambda: config, clock=lambda _config: now)
    payload = {
        "service_id": "basic",
        "pet_name": "可乐",
        "pet_type": "狗",
        "customer_name": "陈女士",
        "phone": "13800138000",
        "appointment_date": "2030-01-09",
        "appointment_time": "10:00",
        "idempotency_key": "same-confirmation",
    }

    with pytest.raises(BookingValidationError, match="明确确认"):
        service.create(payload, customer_confirmed=False)

    first = service.create(payload, customer_confirmed=True)
    replay = service.create(payload, customer_confirmed=True)

    assert first["booking_code"] == replay["booking_code"]
    assert replay["idempotent_replay"] is True
    assert repository.count() == 1
