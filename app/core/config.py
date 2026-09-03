from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping

ROOT = Path(__file__).resolve().parents[2]


def load_env_file(path: Path, target: MutableMapping[str, str]) -> None:
    """Load a small dotenv subset, preserving values already in the environment."""
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
        target.setdefault(key, value)


def _integer(source: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(source.get(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    database_path: Path = ROOT / "appointments.db"
    business_config_path: Path = ROOT / "config" / "business.json"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_timeout: int = 45
    llm_max_retries: int = 3
    stt_provider: str = "api"
    log_level: str = "INFO"

    @classmethod
    def load(
        cls,
        *,
        env_file: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "Settings":
        source = dict(os.environ if environ is None else environ)
        load_env_file(ROOT / ".env" if env_file is None else env_file, source)
        return cls(
            host=source.get("PAWPILOT_HOST", "127.0.0.1"),
            port=_integer(source, "PAWPILOT_PORT", 8000),
            database_path=Path(source.get("PAWPILOT_DATABASE_PATH", ROOT / "appointments.db")),
            business_config_path=Path(
                source.get("PAWPILOT_BUSINESS_CONFIG_PATH", ROOT / "config" / "business.json")
            ),
            llm_api_key=source.get("PAWPILOT_LLM_API_KEY", "").strip(),
            llm_model=source.get("PAWPILOT_LLM_MODEL", "").strip(),
            llm_base_url=source.get("PAWPILOT_LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            llm_timeout=_integer(source, "PAWPILOT_LLM_TIMEOUT", 45),
            llm_max_retries=_integer(source, "PAWPILOT_LLM_MAX_RETRIES", 3),
            stt_provider=source.get("PAWPILOT_STT_PROVIDER", "api").strip().lower(),
            log_level=source.get("PAWPILOT_LOG_LEVEL", "INFO").upper(),
        )
