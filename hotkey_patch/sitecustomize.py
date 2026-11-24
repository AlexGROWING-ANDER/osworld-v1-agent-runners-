# 自动加载 hotkey 适配补丁（只影响当前进程，不改上游仓库）
import os, sys
from pathlib import Path
if os.environ.get("OSWORLD_HOTKEY_FIX", "1") == "1":
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        sys.path.insert(0, os.environ.get("OSWORLD_V1_ROOT", r"H:/GUI_AGENT_OSWorld/OSWorld-v1"))
        import hotkey_adapter_fix
        hotkey_adapter_fix.install()
    except Exception as e:
        sys.stderr.write(f"[hotkey_fix] skipped: {e}\n")

# 素材下载：OSWorld 原生 _download_setup 只有 3 次重试且不支持续传，
# 走代理下 20MB 素材必挂。换成支持续传+按进展重试的版本。
if os.environ.get("OSWORLD_DOWNLOAD_FIX", "1") == "1":
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import download_fix
        download_fix.install()
    except Exception as e:
        sys.stderr.write(f"[download_fix] skipped: {e}\n")

# httplib2（PyDrive/GoogleAuth 底层）不认环境变量代理，这里换成 requests 实现。
# 只在跑带 googledrive setup 的任务时才有用，其它任务不受影响。
if os.environ.get("OSWORLD_HTTPLIB2_PROXY", "1") == "1":
    try:
        import httplib2_proxy_fix
        httplib2_proxy_fix.install()
    except Exception as e:
        sys.stderr.write(f"[httplib2_proxy] skipped: {e}\n")
