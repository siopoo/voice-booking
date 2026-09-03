from __future__ import annotations

import json
from datetime import datetime

from app.db.connection import Database


def onboarding_response(row) -> dict:
    return {
        "id": row["id"], "application_code": row["application_code"], "status": row["status"],
        "application": json.loads(row["application_json"]),
        "collected": json.loads(row["collected_json"] or "{}"),
        "config": json.loads(row["config_json"]) if row["config_json"] else None,
        "checklist": json.loads(row["checklist_json"] or "{}"),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


class OnboardingRepository:
    def __init__(self, database: Database):
        self.database = database

    def init_schema(self) -> None:
        with self.database.connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS onboarding_applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_code TEXT UNIQUE NOT NULL, status TEXT NOT NULL,
                    application_json TEXT NOT NULL, collected_json TEXT NOT NULL DEFAULT '{}',
                    config_json TEXT, checklist_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                )"""
            )

    def get(self, application_id: int):
        with self.database.connect() as db:
            return db.execute(
                "SELECT * FROM onboarding_applications WHERE id=?", (application_id,)
            ).fetchone()

    def list(self):
        with self.database.connect() as db:
            return db.execute("SELECT * FROM onboarding_applications ORDER BY id DESC").fetchall()

    def insert(self, code: str, payload: dict) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self.database.connect() as db:
            return db.execute(
                "INSERT INTO onboarding_applications "
                "(application_code,status,application_json,created_at,updated_at) VALUES (?,?,?,?,?)",
                (code, "submitted", json.dumps(payload, ensure_ascii=False), now, now),
            ).lastrowid

    def update(self, application_id: int, status: str, **json_fields) -> bool:
        assignments = ["status=?", "updated_at=?"]
        values = [status, datetime.now().isoformat(timespec="seconds")]
        for column, value in json_fields.items():
            assignments.append(f"{column}=?")
            values.append(json.dumps(value, ensure_ascii=False))
        values.append(application_id)
        with self.database.connect() as db:
            cursor = db.execute(
                f"UPDATE onboarding_applications SET {', '.join(assignments)} WHERE id=?", values
            )
        return bool(cursor.rowcount)
