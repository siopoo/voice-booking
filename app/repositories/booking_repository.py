from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.db.connection import Database


class BookingRepository:
    def __init__(self, database: Database):
        self.database = database

    def occupied_slots(self, day: str) -> set[str]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT appointment_time FROM appointments WHERE appointment_date=? AND status='confirmed'",
                (day,),
            ).fetchall()
        return {row["appointment_time"] for row in rows}

    def find_idempotency(self, key: str) -> dict[str, Any] | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT response_json FROM idempotency_records WHERE idempotency_key=?", (key,)
            ).fetchone()
        return json.loads(row["response_json"]) if row else None

    def insert(self, values: tuple, response_factory) -> dict[str, Any]:
        with self.database.connect() as db:
            cursor = db.execute(
                """INSERT INTO appointments (
                    booking_code, service_id, service_name, pet_name, pet_type,
                    customer_name, phone, appointment_date, appointment_time,
                    notes, created_at, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
            response = response_factory(cursor.lastrowid)
            if values[-1]:
                db.execute(
                    "INSERT INTO idempotency_records VALUES (?, ?, ?, ?)",
                    (values[-1], "create_booking", json.dumps(response, ensure_ascii=False), values[-2]),
                )
        return response

    def find(self, *, booking_code: str = "", phone: str = "", include_cancelled: bool = True):
        clauses, values = [], []
        if booking_code:
            clauses.append("booking_code=?")
            values.append(booking_code.strip().upper())
        if phone:
            clauses.append("phone=?")
            values.append(phone.strip())
        if not clauses:
            return []
        status = "" if include_cancelled else " AND status='confirmed'"
        with self.database.connect() as db:
            return db.execute(
                f"SELECT * FROM appointments WHERE {' AND '.join(clauses)}{status} "
                "ORDER BY appointment_date, appointment_time",
                values,
            ).fetchall()

    def get_owned(self, booking_code: str, phone: str):
        rows = self.find(booking_code=booking_code, phone=phone)
        return rows[0] if rows else None

    def update_slot(self, booking_id: int, day: str, slot: str):
        with self.database.connect() as db:
            db.execute(
                "UPDATE appointments SET appointment_date=?, appointment_time=? "
                "WHERE id=? AND status='confirmed'",
                (day, slot, booking_id),
            )
            return db.execute("SELECT * FROM appointments WHERE id=?", (booking_id,)).fetchone()

    def cancel(self, booking_id: int):
        with self.database.connect() as db:
            db.execute("UPDATE appointments SET status='cancelled' WHERE id=?", (booking_id,))
            return db.execute("SELECT * FROM appointments WHERE id=?", (booking_id,)).fetchone()

    def list_all(self):
        with self.database.connect() as db:
            return db.execute(
                "SELECT * FROM appointments ORDER BY appointment_date, appointment_time"
            ).fetchall()

    def count(self) -> int:
        with self.database.connect() as db:
            return db.execute("SELECT COUNT(*) AS total FROM appointments").fetchone()["total"]


IntegrityError = sqlite3.IntegrityError
