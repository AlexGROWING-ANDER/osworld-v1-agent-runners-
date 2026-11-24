from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import json
import math
import msvcrt
import os
import re
import socket
import subprocess
import time
from pathlib import Path

HOME = Path(__file__).resolve().parent
ROOT = HOME.parents[1]
SOURCE = ROOT / "os_review/review_batches/rerun_queue_confirmed.json"
RESULTS = ROOT / "osworld_v1"
OUTPUT = RESULTS / HOME.name
PYTHON = Path("H:/GUI_AGENT_OSWorld/venv-v1/Scripts/python.exe")
WORKER = Path("C:/Users/ASUS/Documents/Codex/2026-09-19/new-chat/osworld_uipath/mobile_qwen_worker.py")
VMRUN = Path("C:/Program Files/VMware/VMware Workstation/vmrun.exe")
VMX = Path("H:/GUI_AGENT_OSWorld/Ubuntu_overnight_20261008/Ubuntu0.vmx")
PATCH = ROOT / "hotkey_patch"
PHYSICAL_MB, GUEST_MB, COMMIT_RESERVE_MB = 700, 4096, 1024
PROXY_PORT = 18081
MAX_ATTEMPTS, WATCHDOG_SECONDS = 2, 2850


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def valid_result(path: Path, task: str) -> bool:
    try:
        obj = read_json(path)
    except (OSError, ValueError):
        return False
    score = obj.get("score")
    return (obj.get("task") == task and obj.get("status") == "completed"
            and type(score) in (int, float) and math.isfinite(score))


def case_key(task: str) -> str:
    if re.fullmatch(r"[a-z_]+/[0-9a-f-]{36}", task) is None:
        raise ValueError(f"Invalid task ID: {task}")
    return task.replace("/", "_", 1)


def historical_result(task: str) -> bool:
    key = case_key(task)
    return any(valid_result(path, task)
               for folder in RESULTS.glob("rerun_*") if folder != OUTPUT
               for path in folder.glob(f"{key}/attempt_*/task_result.json"))


def confirmed_rows() -> list[dict]:
    confirmed = [{**row, "task": row["task"] if "/" in row["task"] else
                  f'{row["app"]}/{row["task"]}'} for row in read_json(SOURCE)]
    if len(confirmed) != 51 or len({item["task"] for item in confirmed}) != 51:
        raise ValueError("Expected 51 distinct confirmed task IDs")
    return confirmed


def make_plan() -> list[dict]:
    return [item for item in confirmed_rows() if not historical_result(item["task"])]


def write_plan(plan: list[dict]) -> None:
    if len(plan) != 17:
        raise ValueError(f"Expected 17 pending confirmed tasks; found {len(plan)}")
    path = HOME / "queue.json"
    with path.open("x", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
        f.write("\n")


def event(kind: str, **fields) -> None:
    row = {"time": dt.datetime.now().isoformat(timespec="seconds"), "event": kind, **fields}
    with (HOME / "supervisor.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def memory() -> tuple[int, int]:
    class State(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in ("tp", "fp", "tc", "fc", "tv", "fv", "ev")
        ]
    state = State()
    state.size = ctypes.sizeof(state)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
        raise OSError("Cannot read available memory")
    return state.fp // (1024 ** 2), state.fc // (1024 ** 2)


def active_vm() -> bool:
    p = subprocess.run([str(VMRUN), "-T", "ws", "list"], capture_output=True, text=True,
                       timeout=25, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
    return any(os.path.normcase(os.path.normpath(line.strip())) == os.path.normcase(os.path.normpath(str(VMX)))
               for line in p.stdout.splitlines()[1:])


def gate() -> str | None:
    physical, commit = memory()
    if physical < PHYSICAL_MB or commit < GUEST_MB + COMMIT_RESERVE_MB:
        return (f"memory: physical {physical}/{PHYSICAL_MB} MB; "
                f"commit {commit}/{GUEST_MB + COMMIT_RESERVE_MB} MB")
    try:
        with socket.create_connection(("127.0.0.1", PROXY_PORT), timeout=2):
            pass
        if active_vm():
            return "shared OSWorld VM already active"
    except (OSError, subprocess.SubprocessError):
        return "proxy or VMware unavailable"
    return None


def stop_own_vm() -> None:
    try:
        if active_vm():
            p = subprocess.run([str(VMRUN), "-T", "ws", "stop", str(VMX), "soft"],
                               capture_output=True, timeout=90,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            if p.returncode:
                event("vm_cleanup_failed", returncode=p.returncode)
    except (OSError, subprocess.SubprocessError) as exc:
        event("vm_cleanup_failed", error=type(exc).__name__)


def launch(item: dict, folder: Path) -> str:
    task, idx = item["task"], item["idx"]
    folder.parent.mkdir(parents=True, exist_ok=True)
    logs = folder.parent / f"{folder.name}.log"
    cmd = [str(PYTHON), "-u", "-X", "utf8", str(WORKER), "--version", "v1",
           "--task", task, "--output", str(folder), "--model-profile", "gpt",
           "--steps", "30", "--runtime-budget-seconds", "2700",
           "--api-timeout-seconds", "240", "--physical-min-mb", str(PHYSICAL_MB)]
    env = dict(os.environ)
    env.update(PYTHONPATH=str(PATCH) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""),
               OSWORLD_HOTKEY_FIX="1", OSWORLD_GUEST_MB=str(GUEST_MB),
               OSWORLD_COMMIT_HEADROOM_MB=str(COMMIT_RESERVE_MB))
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env[name] = f"http://127.0.0.1:{PROXY_PORT}"
    p = None
    rc: int | str = "not_started"
    started = time.time()
    try:
        with logs.open("xb") as log:
            p = subprocess.Popen(cmd, cwd=WORKER.parent, env=env, stdout=log,
                                 stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            event("worker_start", idx=idx, task=task, pid=p.pid, attempt=folder.name)
            try:
                rc = p.wait(timeout=WATCHDOG_SECONDS)
            except subprocess.TimeoutExpired:
                rc = "watchdog_timeout"
                subprocess.run(["taskkill.exe", "/PID", str(p.pid), "/T", "/F"], timeout=30,
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                if p.poll() is None:
                    p.kill()
                p.wait(timeout=20)
    finally:
        if p is not None and p.poll() is not None:
            result_file = folder / "task_result.json"
            try:
                result = read_json(result_file)
            except (OSError, ValueError):
                result = {}
            if (result.get("status") not in {"blocked_resources", "blocked_configuration"}
                    and (folder / "api_events.jsonl").exists()):
                stop_own_vm()
    result_file = folder / "task_result.json"
    try:
        result = read_json(result_file)
    except (OSError, ValueError):
        result = {}
    status = result.get("status", "no_result")
    event("worker_exit", idx=idx, task=task, attempt=folder.name, rc=rc,
          status=status, score=result.get("score"),
          error_category=result.get("failure_category"), seconds=round(time.time() - started))
    return status


def run_queue() -> None:
    plan = read_json(HOME / "queue.json")
    if read_json(SOURCE) != read_json(HOME / "confirmed_snapshot.json"):
        raise ValueError("The confirmed queue changed; inspect before resuming")
    if len(plan) != 17 or len({row["task"] for row in plan}) != 17:
        raise ValueError("Pending plan was modified")
    if any(row not in confirmed_rows() for row in plan):
        raise ValueError("Pending plan contains tasks outside the confirmed list")
    if not all(p.exists() for p in (PYTHON, WORKER, PATCH, VMRUN, VMX)):
        raise FileNotFoundError("Missing existing OSWorld runner, Hotkey patch, or VMware")
    if int(re.search(r'^memsize\s*=\s*"(\d+)"', VMX.read_text(encoding="utf-8"),
                     re.M).group(1)) != GUEST_MB:
        raise ValueError("VMware memory does not match the 4 GB batch")
    event("supervisor_start", pid=os.getpid(), planned=len(plan), guest_mb=GUEST_MB,
          model="gpt-5.6-sol", reasoning="medium")
    for item in plan:
        task = item["task"]
        dest = OUTPUT / case_key(task)
        if historical_result(task) or any(valid_result(p, task) for p in dest.glob("attempt_*/task_result.json")):
            continue
        last_message = None
        last_log = 0.0
        while True:
            standby = gate()
            if standby is not None:
                if last_message is None or time.monotonic() - last_log >= 300:
                    event("waiting", idx=item["idx"], reason=standby)
                    last_message, last_log = standby, time.monotonic()
                time.sleep(30)
                continue
            attempts = list(dest.glob("attempt_*.log"))
            resource_blocks = 0
            for log in attempts:
                p = dest / log.stem / "task_result.json"
                try:
                    resource_blocks += read_json(p).get("status") == "blocked_resources"
                except (OSError, ValueError):
                    pass
            if len(attempts) - resource_blocks >= MAX_ATTEMPTS:
                event("attempt_limit", idx=item["idx"])
                break
            attempt_number = max((int(p.stem.split("_")[-1]) for p in attempts), default=0) + 1
            folder = dest / f"attempt_{attempt_number}"
            status = launch(item, folder)
            if status == "blocked_resources":
                time.sleep(60)
                continue
            if valid_result(folder / "task_result.json", task):
                break
            if status == "blocked_configuration":
                event("blocked_configuration", idx=item["idx"])
                break
            time.sleep(30)
    done = sum(historical_result(row["task"]) or any(
        valid_result(p, row["task"]) for p in (OUTPUT / case_key(row["task"])).glob("attempt_*/task_result.json"))
        for row in plan)
    event("queue_exhausted", scored=done, remaining=len(plan)-done)


def main() -> None:
    arg = argparse.ArgumentParser()
    arg.add_argument("--plan", action="store_true")
    args = arg.parse_args()
    if args.plan:
        plan = make_plan()
        write_plan(plan)
        with (HOME / "confirmed_snapshot.json").open("xb") as f:
            f.write(SOURCE.read_bytes())
        print("Pending:", ", ".join(str(row["idx"]) for row in plan))
        print("Resources:", memory())
        return
    with (HOME / "supervisor.lock").open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            run_queue()
        except Exception as exc:
            event("supervisor_error", error=type(exc).__name__, detail=str(exc))
            raise
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


if __name__ == "__main__":
    main()
