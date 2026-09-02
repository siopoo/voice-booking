import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(server, "DB_PATH", Path(self.temp.name) / "onboarding.db")
        self.config_patch = patch.object(
            server, "BUSINESS_CONFIG_PATH", Path(self.temp.name) / "business.json"
        )
        self.db_patch.start()
        self.config_patch.start()
        server.init_db()

    def tearDown(self):
        self.config_patch.stop()
        self.db_patch.stop()
        self.temp.cleanup()

    def application(self):
        return {
            "business_name": "星球宠物美容",
            "business_type": "pet_groomer",
            "website_url": "https://example.com",
            "social_profile": "",
            "services_and_prices": "洗护 99 元",
            "email": "owner@example.com",
            "phone": "13800138000",
            "preferred_language": "zh",
            "website_authorized": True,
            "contact_authorized": True,
            "representative_confirmed": True,
        }

    def test_required_authorizations_are_enforced(self):
        payload = self.application()
        payload["website_authorized"] = False
        with self.assertRaisesRegex(server.OnboardingValidationError, "授权"):
            server.submit_onboarding_application(payload)

    def test_application_state_persists_and_can_transition(self):
        submitted = server.submit_onboarding_application(self.application())
        self.assertEqual(submitted["status"], "submitted")
        collecting = server.advance_onboarding_application(submitted["id"], "collect")
        reviewed = server.advance_onboarding_application(submitted["id"], "review")
        self.assertEqual(collecting["status"], "collecting")
        self.assertEqual(reviewed["status"], "awaiting_review")
        self.assertEqual(server.get_onboarding_application(submitted["id"])["application"]["business_name"], "星球宠物美容")

    def test_generate_accept_and_activate_updates_current_config(self):
        item = server.submit_onboarding_application(self.application())
        server.advance_onboarding_application(item["id"], "collect")
        server.advance_onboarding_application(item["id"], "review")
        config = copy.deepcopy(server.current_business_config())
        config.pop("config_version", None)
        config["business_name"] = "星球宠物美容"
        generated = server.generate_onboarding_config(item["id"], config)
        self.assertEqual(generated["status"], "config_generated")
        testing = server.advance_onboarding_application(item["id"], "test")
        self.assertEqual(testing["status"], "testing")

        checklist = {key: True for key in server.ACCEPTANCE_CHECKLIST_KEYS}
        accepted = server.accept_onboarding_application(item["id"], checklist)
        self.assertEqual(accepted["status"], "accepted")
        active = server.activate_onboarding_application(item["id"])
        self.assertEqual(active["status"], "activated")
        self.assertEqual(server.current_business_config()["business_name"], "星球宠物美容")

    def test_incomplete_acceptance_checklist_cannot_activate(self):
        item = server.submit_onboarding_application(self.application())
        config = server.current_business_config()
        config.pop("config_version", None)
        server.generate_onboarding_config(item["id"], config)
        server.advance_onboarding_application(item["id"], "test")
        checklist = {key: True for key in server.ACCEPTANCE_CHECKLIST_KEYS}
        checklist["voice_test_passed"] = False
        with self.assertRaisesRegex(server.OnboardingValidationError, "检查项"):
            server.accept_onboarding_application(item["id"], checklist)


if __name__ == "__main__":
    unittest.main()
