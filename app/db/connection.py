from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def init_schema(self) -> None:
        with self.connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS appointments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    booking_code TEXT UNIQUE NOT NULL,
                    service_id TEXT NOT NULL, service_name TEXT NOT NULL,
                    pet_name TEXT NOT NULL, pet_type TEXT NOT NULL,
                    customer_name TEXT NOT NULL, phone TEXT NOT NULL,
                    appointment_date TEXT NOT NULL, appointment_time TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'confirmed',
                    created_at TEXT NOT NULL, idempotency_key TEXT
                )"""
            )
            table_sql = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='appointments'"
            ).fetchone()["sql"]
            if re.search(r"UNIQUE\s*\(\s*appointment_date\s*,\s*appointment_time\s*\)", table_sql, re.I):
                self._migrate_legacy(db)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(appointments)")}
            if "idempotency_key" not in columns:
                db.execute("ALTER TABLE appointments ADD COLUMN idempotency_key TEXT")
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_appointments_idempotency "
                "ON appointments(idempotency_key) WHERE idempotency_key IS NOT NULL"
            )
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_appointments_confirmed_slot "
                "ON appointments(appointment_date, appointment_time) WHERE status='confirmed'"
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS idempotency_records (
                    idempotency_key TEXT PRIMARY KEY, operation TEXT NOT NULL,
                    response_json TEXT NOT NULL, created_at TEXT NOT NULL
                )"""
            )

    @staticmethod
    def _migrate_legacy(db) -> None:
        db.execute("ALTER TABLE appointments RENAME TO appointments_legacy")
        db.execute(
            """CREATE TABLE appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                booking_code TEXT UNIQUE NOT NULL, service_id TEXT NOT NULL,
                service_name TEXT NOT NULL, pet_name TEXT NOT NULL, pet_type TEXT NOT NULL,
                customer_name TEXT NOT NULL, phone TEXT NOT NULL,
                appointment_date TEXT NOT NULL, appointment_time TEXT NOT NULL,
                notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'confirmed',
                created_at TEXT NOT NULL, idempotency_key TEXT
            )"""
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(appointments_legacy)")}
        idempotency = "idempotency_key" if "idempotency_key" in columns else "NULL"
        db.execute(
            f"""INSERT INTO appointments
            SELECT id, booking_code, service_id, service_name, pet_name, pet_type,
                customer_name, phone, appointment_date, appointment_time,
                notes, status, created_at, {idempotency} FROM appointments_legacy"""
        )
        db.execute("DROP TABLE appointments_legacy")
