from __future__ import annotations

import json
import threading
from datetime import timedelta
from urllib.request import Request, urlopen

import server


def _open_day(start):
    value = start
    while value.weekday() in server.current_business_config()["closed_weekdays"]:
        value += timedelta(days=1)
    return value


def _request(base_url: str, path: str, payload: dict | None = None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{base_url}{path}", data=data,
        method="GET" if payload is None else "POST",
        headers={"Content-Type": "application/json", "Idempotency-Key": "integration-flow"},
    )
    with urlopen(request, timeout=3) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_health_and_full_booking_http_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB_PATH", tmp_path / "integration.db")
    server.init_db()
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.AppHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        status, health = _request(base, "/api/health")
        assert status == 200 and health["status"] == "ok"
        first_day = _open_day(server.business_now().date() + timedelta(days=1))
        second_day = _open_day(first_day + timedelta(days=1))
        _, created = _request(base, "/api/bookings", {
            "service_id": "basic", "pet_name": "豆包", "pet_type": "狗",
            "customer_name": "王女士", "phone": "13700137000",
            "appointment_date": first_day.isoformat(), "appointment_time": "10:00",
        })
        code = created["booking_code"]
        _, queried = _request(base, "/api/bookings/query", {"booking_code": code, "phone": "13700137000"})
        assert queried["bookings"][0]["booking_code"] == code
        _, moved = _request(base, "/api/bookings/reschedule", {
            "booking_code": code, "phone": "13700137000",
            "appointment_date": second_day.isoformat(), "appointment_time": "11:30",
            "customer_confirmed": True,
        })
        assert moved["appointment_date"] == second_day.isoformat()
        _, cancelled = _request(base, "/api/bookings/cancel", {
            "booking_code": code, "phone": "13700137000", "customer_confirmed": True,
        })
        assert cancelled["status"] == "cancelled"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)
