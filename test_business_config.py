import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

try:
    import business_config
except ImportError:
    business_config = None


CUSTOM_CONFIG = {
    "business_name": "星球宠物美容",
    "business_type": "pet_groomer",
    "address": "上海市静安区测试路1号",
    "timezone": "Asia/Shanghai",
    "opening_time": "09:00",
    "closing_time": "19:00",
    "closed_weekdays": [0],
    "booking_window_days": 14,
    "services": [
        {"id": "wash", "name": "清爽洗护", "duration": 60, "price": 99}
    ],
    "appointment_slots": ["09:00", "10:30", "14:00"],
    "agent_language": "zh",
    "welcome_message": "您好，欢迎致电星球宠物美容。",
}


class BusinessConfigTests(unittest.TestCase):
    def test_valid_config_is_loaded_with_a_stable_version(self) -> None:
        self.assertIsNotNone(business_config, "需要独立的门店配置模块")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "business.json"
            path.write_text(json.dumps(CUSTOM_CONFIG, ensure_ascii=False), encoding="utf-8")

            loaded = business_config.load_business_config(path)

        self.assertEqual(loaded["business_name"], "星球宠物美容")
        self.assertEqual(loaded["services"][0]["price"], 99)
        self.assertRegex(loaded["config_version"], r"^[0-9a-f]{12}$")

    def test_invalid_config_uses_safe_default_and_reports_the_error(self) -> None:
        self.assertIsNotNone(business_config, "需要独立的门店配置模块")
        errors = []
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "business.json"
            path.write_text('{"business_name":"坏配置","services":[]}', encoding="utf-8")

            loaded = business_config.load_business_config(path, reporter=errors.append)

        self.assertEqual(loaded["business_name"], "PawPilot 宠物护理中心")
        self.assertEqual([item["id"] for item in loaded["services"]], ["basic", "grooming", "spa"])
        self.assertTrue(errors)
        self.assertIn("门店配置读取失败", errors[0])

    def test_saving_a_changed_config_changes_its_version(self) -> None:
        self.assertIsNotNone(business_config, "需要独立的门店配置模块")
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "business.json"
            first = business_config.save_business_config(CUSTOM_CONFIG, path)
            changed = {**CUSTOM_CONFIG, "business_name": "更新后的门店"}
            second = business_config.save_business_config(changed, path)

        self.assertNotEqual(first["config_version"], second["config_version"])
        self.assertEqual(second["business_name"], "更新后的门店")

    def test_agent_prompt_uses_current_config_and_runtime_date(self) -> None:
        import booking_agent

        builder = getattr(booking_agent, "build_system_prompt", None)
        self.assertIsNotNone(builder, "系统提示词必须在构建 Agent 时动态生成")

        prompt = builder(CUSTOM_CONFIG, today=date(2030, 1, 2))

        self.assertIn("星球宠物美容", prompt)
        self.assertIn("2030-01-02", prompt)
        self.assertIn("未来14天", prompt)

    def test_agent_cache_rebuilds_when_config_version_changes(self) -> None:
        import booking_agent

        first_agent, second_agent = object(), object()
        booking_agent._agent = None
        booking_agent._agent_signature = None
        with patch.object(
            booking_agent.server,
            "current_business_config",
            side_effect=[{"config_version": "v1"}, {"config_version": "v1"}, {"config_version": "v2"}],
        ), patch.object(
            booking_agent, "build_booking_agent", side_effect=[first_agent, second_agent]
        ) as builder:
            self.assertIs(booking_agent.get_booking_agent(), first_agent)
            self.assertIs(booking_agent.get_booking_agent(), first_agent)
            self.assertIs(booking_agent.get_booking_agent(), second_agent)
        self.assertEqual(builder.call_count, 2)
        booking_agent._agent = None
        booking_agent._agent_signature = None


if __name__ == "__main__":
    unittest.main()
