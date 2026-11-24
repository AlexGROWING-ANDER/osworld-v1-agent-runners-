"""Sequential OSWorld-1 runner for the frozen SZ-LXZL-288 allocation."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allocation", default=str(ROOT / "tasks" / "allocation_manifest_v1.json"))
    parser.add_argument("--run-id", default=datetime.now().strftime("%Y%m%d_%H%M%S"))
    parser.add_argument("--task", help="Run one allocated task ID only")
    args = parser.parse_args()

    allocation = read_json(Path(args.allocation))
    if not allocation or allocation.get("status") != "frozen":
        raise SystemExit("Allocation is not frozen; refresh host completed/running sets before launch")
    tasks = allocation.get("tasks") or []
    if args.task:
        tasks = [entry for entry in tasks if entry.get("task_id") == args.task]
        if not tasks:
            raise SystemExit("Requested task is not present in the frozen allocation")

    run_root = ROOT / "runs" / "SZ-LXZL-288" / args.run_id
    run_root.mkdir(parents=True, exist_ok=False)
    (run_root / "run_manifest.json").write_text(
        json.dumps(
            {
                "allocation_version": allocation["allocation_version"],
                "model_contract": allocation["model_contract"],
                "started": datetime.now().astimezone().isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    runner = ROOT / "runner" / "run_guarded_mobile.py"
    for entry in tasks:
        task = entry["task_id"]
        task_root = run_root / task.replace("/", "_")
        max_attempts = int(entry.get("max_attempts", 1 if not entry.get("allow_retry") else 2))
        for attempt in range(1, max_attempts + 1):
            output = task_root / f"attempt_{attempt}"
            command = [
                sys.executable,
                "-u",
                "-X",
                "utf8",
                str(runner),
                "--deadline-seconds",
                "2700",
                "--version",
                "v1",
                "--task",
                task,
                "--model-profile",
                "gpt",
                "--normalized-grounding",
                "--physical-min-mb",
                "3000",
                "--steps",
                "30",
                "--api-timeout-seconds",
                "240",
                "--output",
                str(output),
            ]
            log = task_root / f"attempt_{attempt}.log"
            task_root.mkdir(parents=True, exist_ok=True)
            with log.open("x", encoding="utf-8") as stream:
                code = subprocess.run(command, cwd=ROOT, stdout=stream, stderr=stream).returncode
            result = read_json(output / "task_result.json") or {}
            if result.get("status") == "completed" and result.get("score") is not None:
                break
            if code == 3 or result.get("failure_category") in {
                "authentication_or_permission",
                "billing_or_payment",
                "rate_or_quota_limit",
            }:
                return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
