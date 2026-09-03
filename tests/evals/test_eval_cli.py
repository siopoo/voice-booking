from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_eval_script_runs_directly_from_repository_root():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "evals/run_evals.py"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["scenario_count"] == 30
    assert report["failed"] == 0
