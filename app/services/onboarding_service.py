from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Callable

from app.core.exceptions import OnboardingValidationError
from app.repositories.onboarding_repository import OnboardingRepository, onboarding_response
from business_config import save_business_config

ACCEPTANCE_CHECKLIST_KEYS = (
    "business_profile_confirmed", "services_confirmed", "hours_confirmed",
    "agent_config_generated", "text_test_passed", "voice_test_passed", "customer_accepted",
)


class OnboardingService:
    def __init__(
        self,
        repository: OnboardingRepository,
        config_path_provider: Callable[[], Path],
    ):
        self.repository = repository
        self.config_path_provider = config_path_provider

    def get(self, application_id: int) -> dict:
        row = self.repository.get(application_id)
        if row is None:
            raise OnboardingValidationError("申请不存在")
        return onboarding_response(row)

    def list(self) -> list[dict]:
        return [onboarding_response(row) for row in self.repository.list()]

    def submit(self, payload: dict) -> dict:
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
        application_id = self.repository.insert(f"APP-{uuid.uuid4().hex[:8].upper()}", payload)
        return self.get(application_id)

    def _set(self, application_id: int, status: str, **json_fields) -> dict:
        if not self.repository.update(application_id, status, **json_fields):
            raise OnboardingValidationError("申请不存在")
        return self.get(application_id)

    def advance(self, application_id: int, action: str) -> dict:
        current = self.get(application_id)
        status = {
            ("submitted", "collect"): "collecting",
            ("collecting", "review"): "awaiting_review",
            ("config_generated", "test"): "testing",
        }.get((current["status"], action))
        if status is None:
            raise OnboardingValidationError("当前状态不能执行该操作")
        if action == "collect":
            return self._set(
                application_id,
                status,
                collected_json={
                    "source": "客户授权的公开网站 + 演示稳定数据",
                    "website_reachable": True,
                    "summary": current["application"]["services_and_prices"],
                    "human_fallback": True,
                },
            )
        return self._set(application_id, status)

    def generate_config(self, application_id: int, config: dict) -> dict:
        current = self.get(application_id)
        if current["status"] not in {"submitted", "collecting", "awaiting_review", "config_generated"}:
            raise OnboardingValidationError("当前状态不能生成配置")
        preview = Path(str(self.config_path_provider()) + ".preview")
        normalized = save_business_config(config, preview)
        preview.unlink(missing_ok=True)
        normalized.pop("config_version", None)
        return self._set(application_id, "config_generated", config_json=normalized)

    def accept(self, application_id: int, checklist: dict) -> dict:
        current = self.get(application_id)
        if current["config"] is None:
            raise OnboardingValidationError("请先生成门店配置")
        if current["status"] != "testing":
            raise OnboardingValidationError("请先进入测试阶段")
        if not all(checklist.get(key) is True for key in ACCEPTANCE_CHECKLIST_KEYS):
            raise OnboardingValidationError("所有测试与验收检查项必须通过")
        return self._set(application_id, "accepted", checklist_json=checklist)

    def activate(self, application_id: int) -> dict:
        current = self.get(application_id)
        if current["status"] != "accepted" or current["config"] is None:
            raise OnboardingValidationError("只有验收通过的配置才能激活")
        save_business_config(current["config"], self.config_path_provider())
        return self._set(application_id, "activated")
