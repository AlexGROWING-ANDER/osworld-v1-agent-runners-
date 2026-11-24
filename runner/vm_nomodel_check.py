"""Second-host VM smoke check. Makes zero model/API calls."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values


ROOT = Path(os.environ.get("OSWORLD_DELIVERY_ROOT", Path(__file__).resolve().parents[1]))
CODE = ROOT / "code" / "OSWorld-v1"
VMX = Path(os.environ.get("OSWORLD_VMX", ROOT / "vm" / "Ubuntu0" / "Ubuntu0.vmx"))
VMRUN = Path(os.environ.get("VMRUN", r"C:\Program Files\VMware\VMware Workstation\vmrun.exe"))
OUT = ROOT / "reports" / f"vm_nomodel_check_{datetime.now():%Y%m%d_%H%M%S}"


def main() -> int:
    if not VMRUN.is_file() or not VMX.is_file():
        print("VMware vmrun.exe or VMX is missing", file=sys.stderr)
        return 2
    credential = dotenv_values(ROOT / ".vm_guest_credential.local")
    password = credential.get("VM_GUEST_PASSWORD")
    if not password:
        print("VM_GUEST_PASSWORD is missing from .vm_guest_credential.local", file=sys.stderr)
        return 3

    OUT.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    subprocess.run([str(VMRUN), "start", str(VMX), "nogui"], check=True, timeout=180)
    ip = subprocess.run(
        [str(VMRUN), "getGuestIPAddress", str(VMX), "-wait"],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    ).stdout.strip()

    sys.path.insert(0, str(CODE))
    from desktop_env.desktop_env import DesktopEnv

    env = DesktopEnv(
        provider_name="vmware",
        path_to_vm=str(VMX),
        headless=True,
        client_password=password,
        require_a11y_tree=False,
        cache_dir=str(OUT / "cache"),
    )
    before = env.controller.get_screenshot()
    if not before:
        raise RuntimeError("Guest screenshot was empty")
    (OUT / "before.png").write_bytes(before)
    # Balanced modifier-key action: observable controller call with no lasting GUI state.
    env.controller.execute_python_command("pyautogui.keyDown('shift'); pyautogui.keyUp('shift')")
    after = env.controller.get_screenshot()
    if not after:
        raise RuntimeError("Post-action screenshot was empty")
    (OUT / "after.png").write_bytes(after)
    report = {
        "status": "passed",
        "model_calls": 0,
        "vmx": str(VMX),
        "guest_ip": ip,
        "vmware_tools_ready": True,
        "action": "balanced shift key down/up",
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    (OUT / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
