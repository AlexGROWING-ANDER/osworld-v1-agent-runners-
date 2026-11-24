# -*- coding: utf-8 -*-
"""按 hotkey_rerun_queue.json 顺序重跑 OSWorld 失败 case（一次一个，可断点续跑）。

依赖：
  - H:/GUI_AGENT_OSWorld/venv-v1/Scripts/python.exe
  - C:/Users/ASUS/Documents/Codex/2026-09-19/new-chat/osworld_uipath/mobile_qwen_worker.py
  - 该机器可启动 VMware Ubuntu0（或配置 --native-v2 的 QEMU）
  - 模型密钥（worker 内部读取）

用法：
  python rerun_hotkey_queue.py --dry-run
  python rerun_hotkey_queue.py --limit 2
  python rerun_hotkey_queue.py
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(r"H:/GUI_AGENT_runs")
QUEUE = ROOT / "os_review" / "hotkey_rerun_queue.json"
PATCH_DIR = ROOT / "hotkey_patch"
PY = Path(r"H:/GUI_AGENT_OSWorld/venv-v1/Scripts/python.exe")
WORKER = Path(r"C:/Users/ASUS/Documents/Codex/2026-09-19/new-chat/osworld_uipath/mobile_qwen_worker.py")
OUT_ROOT = ROOT / "osworld_v1" / "rerun_hotkey_20261102"
LOG = OUT_ROOT / "queue_status.jsonl"
VMRUN = Path(r"C:/Program Files/VMware/VMware Workstation/vmrun.exe")
VMX = Path(r"H:/GUI_AGENT_OSWorld/Ubuntu_overnight_20261008/Ubuntu0.vmx")




def _free_mem():
    import ctypes
    class M(ctypes.Structure):
        _fields_ = [('l', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
            (n, ctypes.c_ulonglong) for n in ('tp','fp','tc','fc','tv','fv','e')]
    m = M(); m.l = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.fp // 1024**2, m.fc // 1024**2


def wait_memory(need_phys, need_commit, tries=8, interval=20):
    for _ in range(tries):
        p, c = _free_mem()
        if p >= need_phys and c >= need_commit:
            return True
        time.sleep(interval)
    return False

def stop_vm() -> None:
    """优雅关闭残留 VM，释放 3.5GB 内存；失败不阻断队列。"""
    try:
        if VMRUN.exists() and VMX.exists():
            subprocess.run([str(VMRUN), "-T", "ws", "stop", str(VMX)],
                           capture_output=True, timeout=90)
    except Exception:
        pass


def out_dir(task: str) -> Path:
    app, uid = task.split("/", 1)
    return OUT_ROOT / f"{app}_{uid}" / "attempt_1"


PID_FILE = Path(r"H:/GUI_AGENT_runs/os_review/queue.pid")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--steps", type=int, default=30)
    ap.add_argument("--physical-min-mb", type=int, default=1000)
    ap.add_argument("--timeout", type=int, default=1000)
    args = ap.parse_args()
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    queue = json.loads(QUEUE.read_text(encoding="utf-8"))
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PATCH_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    env["OSWORLD_HOTKEY_FIX"] = "1"
    # 本机代理：沙箱代理(51659)会对公网返回 502，必须四个变量一起覆盖
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env[k] = "http://127.0.0.1:18081"
    for k in ("no_proxy", "NO_PROXY"):
        env[k] = "localhost,127.0.0.1"
    env["OSWORLD_COMMIT_HEADROOM_MB"] = "0"
    env["OSWORLD_GUEST_MB"] = "2048"

    done = 0
    for item in queue:
        out = out_dir(item["task"])
        if (out / "task_result.json").exists():
            continue
        cmd = [str(PY), "-u", "-X", "utf8", str(WORKER),
               "--version", "v1", "--task", item["task"], "--output", str(out),
               "--model-profile", "gpt", "--steps", str(args.steps),
               "--physical-min-mb", str(args.physical_min_mb)]
        print(f"[{item['idx']}] {item['task']} -> {out}", flush=True)
        if args.dry_run:
            print("   ", " ".join(cmd), flush=True)
        else:
            stop_vm()
            # stale_dir: 没有 task_result 的旧目录会让 worker 的 mkdir(exist_ok=False) 失败
            if out.exists() and not (out / "task_result.json").exists():
                try:
                    out.rename(out.with_name(out.name + f".stale_{int(time.time())}"))
                except Exception:
                    pass
            if not wait_memory(args.physical_min_mb - 200, 2048):
                rec = {"idx": item["idx"], "task": item["task"], "rc": 99,
                       "seconds": 0, "out": str(out), "note": "memory_insufficient",
                       "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
                with LOG.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                continue
            t0 = time.time()
            try:
                rc = subprocess.run(cmd, env=env, timeout=args.timeout).returncode
            except subprocess.TimeoutExpired:
                rc = 124
            rec = {"idx": item["idx"], "task": item["task"], "rc": rc,
                   "seconds": round(time.time() - t0), "out": str(out),
                   "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
            stop_vm()
            with LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        done += 1
        if args.limit and done >= args.limit:
            break
    print(f"finished {done} task(s)", flush=True)
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
