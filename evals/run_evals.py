from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.workflow import build_booking_workflow, classify_confirmation

FULL_DRAFT = {
    "service_id": "basic", "service_name": "基础洗护", "service_price": 88,
    "service_duration": 60, "pet_name": "可乐", "pet_type": "狗",
    "appointment_date": "2030-01-09", "appointment_time": "10:00",
    "customer_name": "陈女士", "phone": "13800138000",
}


def load_scenarios(path: Path | None = None) -> list[dict[str, Any]]:
    source = path or Path(__file__).with_name("booking_scenarios.json")
    return json.loads(source.read_text(encoding="utf-8"))


def run_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    writes: list[dict[str, Any]] = []
    unsafe_writes = 0

    def availability(day: str, _service_id: str) -> list[str]:
        return scenario.get("availability", {}).get(day, ["10:00", "11:30", "14:00"])

    active_text = ""

    def create(draft: dict[str, Any]) -> dict[str, Any]:
        nonlocal unsafe_writes
        writes.append(deepcopy(draft))
        if classify_confirmation(active_text) != "confirmed":
            unsafe_writes += 1
        if scenario.get("creator_error"):
            return {"status": "error", "error": scenario["creator_error"]}
        return {"booking_code": f"EVAL-{scenario['id']}", "status": "confirmed"}

    graph = build_booking_workflow(availability, create)
    state: dict[str, Any] = {
        "thread_id": scenario["id"],
        "booking_draft": deepcopy(FULL_DRAFT) if scenario.get("initial") == "full" else {},
        **({"intent": "create"} if scenario.get("initial") == "full" else {}),
    }
    traces = []
    for turn in scenario["turns"]:
        active_text = turn["text"]
        updates = turn.get("updates", turn.get("draft", {}))
        state = graph.invoke({
            **state,
            "messages": [{"role": "user", "content": active_text}],
            "draft_updates": updates,
        })
        traces.append({"user": active_text, "stage": state.get("stage"), "trace": state.get("trace", [])})

    hallucinations = 0
    facts = scenario.get("facts", {})
    service_id = state.get("booking_draft", {}).get("service_id")
    if service_id in facts:
        authoritative = facts[service_id]
        for key, value in authoritative.items():
            if state["booking_draft"].get(key) != value:
                hallucinations += 1
    return {
        "id": scenario["id"], "state": state, "writes": writes,
        "unsafe_writes": unsafe_writes, "hallucinations": hallucinations, "conversation_trace": traces,
    }


def assert_scenario(scenario: dict[str, Any], result: dict[str, Any]) -> list[str]:
    expected, state = scenario["expected"], result["state"]
    failures = []
    checks = {
        "stage": state.get("stage"), "intent": state.get("intent"),
        "writes": len(result["writes"]), "availability": state.get("availability"),
        "selected_slot": state.get("selected_slot"), "hallucinations": result["hallucinations"],
    }
    for key, expected_value in expected.items():
        if key in checks and checks[key] != expected_value:
            failures.append(f"{key}: expected {expected_value!r}, got {checks[key]!r}")
    if expected.get("first_missing") and state.get("missing_fields", [None])[0] != expected["first_missing"]:
        failures.append(f"first_missing: {state.get('missing_fields')}")
    for field in expected.get("cleared", []):
        if field in state.get("booking_draft", {}):
            failures.append(f"{field} should have been invalidated")
    if expected.get("error_contains"):
        error = json.dumps(state.get("booking_result", {}), ensure_ascii=False)
        if expected["error_contains"] not in error:
            failures.append(f"error missing {expected['error_contains']!r}")
    return failures


def run_eval_suite() -> dict[str, Any]:
    results, failures = [], []
    for scenario in load_scenarios():
        result = run_scenario(scenario)
        results.append(result)
        for failure in assert_scenario(scenario, result):
            failures.append({"id": scenario["id"], "failure": failure, "trace": result["conversation_trace"]})
    total_writes = sum(len(item["writes"]) for item in results)
    unsafe = sum(item["unsafe_writes"] for item in results)
    duplicate_writes = sum(max(0, len(item["writes"]) - 1) for item in results)
    hallucinations = sum(item["hallucinations"] for item in results)
    return {
        "scenario_count": len(results), "passed": len(results) - len({item["id"] for item in failures}),
        "failed": len({item["id"] for item in failures}),
        "metrics": {
            "confirmation_safety_rate": 1.0 if not total_writes else (total_writes - unsafe) / total_writes,
            "duplicate_writes": duplicate_writes, "hallucinations": hallucinations,
        },
        "failures": failures,
    }


if __name__ == "__main__":
    report = run_eval_suite()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(1 if report["failed"] else 0)
