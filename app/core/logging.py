from __future__ import annotations

import json
import logging
import re
from typing import Any

SECRET_KEYS = {"authorization", "api_key", "apikey", "token", "secret", "password"}


def mask_phone(phone: str) -> str:
    return f"{phone[:3]}****{phone[-4:]}" if re.fullmatch(r"\d{11}", phone) else "已隐藏"


def mask_sensitive(value: Any, key: str = "") -> Any:
    lowered = key.lower()
    if lowered in SECRET_KEYS or any(secret in lowered for secret in ("api_key", "authorization")):
        return "***"
    if lowered == "phone" and isinstance(value, str):
        return mask_phone(value)
    if isinstance(value, dict):
        return {item_key: mask_sensitive(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [mask_sensitive(item) for item in value]
    return value


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), format="%(message)s")


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    logger.info(json.dumps(mask_sensitive({"event": event, **fields}), ensure_ascii=False, default=str))
