import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import server
from business_config import business_timezone


class BookingRulesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(server, "DB_PATH", Path(self.temp_dir.name) / "rules.db")
        self.db_patch.start()
        server.init_db()
        self.tz = business_timezone("Asia/Shanghai")
        self.now = datetime(2030, 1, 8, 10, 30, tzinfo=self.tz)  # Tuesday

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def payload(self, **changes):
        value = {
            "service_id": "basic",
            "pet_name": "可乐",
            "pet_type": "狗",
            "customer_name": "陈女士",
            "phone": "13800138000",
            "appointment_date": "2030-01-09",
            "appointment_time": "10:00",
            "idempotency_key": "request-one",
        }
        value.update(changes)
        return value

    def create(self, **changes):
        with patch.object(server, "business_now", return_value=self.now):
            return server.create_booking_record(self.payload(**changes))

    def test_invalid_phone_is_rejected(self):
        with self.assertRaisesRegex(server.BookingValidationError, "手机号"):
            self.create(phone="123")

    def test_date_outside_window_and_closed_weekday_are_rejected(self):
        with self.assertRaisesRegex(server.BookingValidationError, "14"):
            self.create(appointment_date="2030-01-23")
        with self.assertRaisesRegex(server.BookingValidationError, "休息"):
            self.create(appointment_date="2030-01-14")

    def test_elapsed_same_day_slot_is_rejected(self):
        with self.assertRaisesRegex(server.BookingValidationError, "过去"):
            self.create(appointment_date="2030-01-08", appointment_time="10:00")

    def test_service_must_finish_before_closing(self):
        with self.assertRaisesRegex(server.BookingValidationError, "关门"):
            self.create(service_id="grooming", appointment_time="17:00")

    def test_concurrent_slot_claim_has_one_winner(self):
        barrier = threading.Barrier(6)
        results = []

        def worker(index):
            try:
                barrier.wait()
                booking = server.create_booking_record(
                    self.payload(idempotency_key=f"request-{index}")
                )
                results.append(("ok", booking["booking_code"]))
            except server.BookingConflictError:
                results.append(("conflict", None))

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
        with patch.object(server, "business_now", return_value=self.now):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(sum(kind == "ok" for kind, _ in results), 1)
        self.assertEqual(sum(kind == "conflict" for kind, _ in results), 5)

    def test_idempotency_is_explicit_and_replays_immutable_snapshot(self):
        first = self.create()
        replay = self.create()
        self.assertEqual(replay["booking_code"], first["booking_code"])
        self.assertTrue(replay["idempotent_replay"])

        with patch.object(server, "business_now", return_value=self.now):
            server.reschedule_booking_record(
                first["booking_code"], "13800138000", "2030-01-10", "11:30",
                customer_confirmed=True,
            )
        old_request = self.create()
        self.assertEqual(old_request["appointment_date"], "2030-01-09")
        self.assertEqual(old_request["appointment_time"], "10:00")

    def test_cancel_then_new_key_can_rebook_same_details(self):
        first = self.create()
        server.cancel_booking_record(first["booking_code"], "13800138000", customer_confirmed=True)
        second = self.create(idempotency_key="request-two")
        self.assertNotEqual(first["booking_code"], second["booking_code"])

    def test_operations_view_masks_phone_and_shows_cancelled_status(self):
        created = self.create()
        server.cancel_booking_record(created["booking_code"], "13800138000", customer_confirmed=True)
        rows = server.list_booking_records(mask_sensitive=True)
        self.assertEqual(rows[0]["phone"], "138****8000")
        self.assertEqual(rows[0]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
