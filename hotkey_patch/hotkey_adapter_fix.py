# -*- coding: utf-8 -*-
"""适配层补丁：修复 GroundingAgent.hotkey 的 keys 参数被字符串化后逐字符下发。

不修改上游仓库文件；通过 monkey-patch 在进程内替换类方法。
用法（在我们的 runner 入口最早处）：
    import hotkey_adapter_fix; hotkey_adapter_fix.install()
"""
from __future__ import annotations
import ast
import json
import re

_ALIAS = {
    "control": "ctrl", "ctl": "ctrl",
    "cmd": "command", "option": "alt",
    "escape": "esc", "return": "enter",
    # Linux 侧叫 super / meta，Windows 的 pyautogui 只有 win。
    # 不映射会让 normalize 抛 ValueError，再被调用方吞掉 → 静默退回拆字符。
    "super": "win", "meta": "win", "win": "win",
}

# pyautogui 可用键名；优先用真实列表，导入失败时退回常用集合
try:
    import pyautogui  # type: ignore
    _KEYS = set(pyautogui.KEYBOARD_KEYS)
except Exception:  # pragma: no cover
    _KEYS = set("abcdefghijklmnopqrstuvwxyz0123456789") | {
        "ctrl", "alt", "shift", "command", "win", "esc", "enter", "tab", "space",
        "backspace", "delete", "insert", "home", "end", "pageup", "pagedown",
        "up", "down", "left", "right", "capslock", "numlock",
    } | {f"f{i}" for i in range(1, 13)}


def normalize_hotkey_keys(keys):
    """把 list / tuple / 字符串形式的 ['ctrl','c'] / 'ctrl+c' 统一成合法 list[str]。"""
    if isinstance(keys, str):
        s = keys.strip()
        parsed = None
        try:
            parsed = ast.literal_eval(s)
        except (ValueError, SyntaxError):
            parsed = None
        if isinstance(parsed, (list, tuple)):
            keys = list(parsed)
        else:
            keys = [p for p in re.split(r"[+,]|\s+", s) if p]
    elif isinstance(keys, (tuple, set)):
        keys = list(keys)

    if not isinstance(keys, list) or not keys:
        raise ValueError(f"hotkey keys must be a non-empty list, got {keys!r}")

    out = []
    for k in keys:
        if not isinstance(k, str):
            raise ValueError(f"invalid key type: {k!r}")
        k = k.strip().strip("'\"").lower()
        k = _ALIAS.get(k, k)
        if k not in _KEYS:
            raise ValueError(f"invalid key: {k!r}")
        out.append(k)
    return out


def make_hotkey_code(keys, note: str | None = None) -> str:
    keys = normalize_hotkey_keys(keys)
    key_str = ", ".join(keys).replace("'", "\\'")
    suffix = note or f"Press Hotkey: {key_str}"
    return f"import pyautogui; pyautogui.hotkey({', '.join(map(repr, keys))}); print(f'{suffix}')"


def _patch_class(cls):
    original = cls.__dict__.get("hotkey")
    if original is None:
        return False
    if getattr(original.__func__, "_hotkey_fixed", False):
        return False

    def hotkey(cls_, keys):
        return make_hotkey_code(keys)

    hotkey._hotkey_fixed = True
    hotkey.is_agent_action = True
    setattr(cls, "hotkey", classmethod(hotkey))
    return True


def _fix_dummy_action(dummy_action):
    """把 {\"action\":\"hotkey\",\"keys\":\"['ctrl','c']\"} 规范成真正的 list。"""
    if not isinstance(dummy_action, str):
        return dummy_action
    try:
        obj = json.loads(dummy_action)
    except Exception:
        return dummy_action
    if isinstance(obj, dict) and obj.get("action") == "hotkey":
        try:
            obj["keys"] = normalize_hotkey_keys(obj.get("keys"))
        except Exception:
            return dummy_action
        return json.dumps(obj, ensure_ascii=False)
    return dummy_action


def _patch_mobileagent_v3() -> bool:
    """mobileagent_v3 的两个动作转换函数：入口处规范化 keys。"""
    import json as _json
    try:
        mod = __import__("mm_agents.mobileagent_v3.mobile_agent", fromlist=["*"])
    except Exception:
        return False
    changed = False
    for name in ("convert_fc_action_to_json_action_grounding", "convert_fc_action_to_json_action"):
        fn = getattr(mod, name, None)
        if fn is None or getattr(fn, "_hotkey_fixed", False):
            continue

        def make(orig):
            def wrapper(dummy_action, *args, **kwargs):
                return orig(_fix_dummy_action(dummy_action), *args, **kwargs)
            wrapper._hotkey_fixed = True
            return wrapper

        setattr(mod, name, make(fn))
        changed = True
    return changed


def _patch_image_url() -> bool:
    """没有 OSS 凭据时，把截图改成 data URL，避免 push_oss 抛 KeyError。"""
    try:
        mod = __import__("mm_agents.mobileagent_v3.mobile_agent_modules", fromlist=["*"])
    except Exception:
        return False
    if getattr(mod.get_image_url, "_data_url", False):
        return False

    def get_image_url(image):
        if isinstance(image, bytes):
            import base64 as _b64
            image = _b64.b64encode(image).decode("ascii")
        return "data:image/png;base64," + image

    get_image_url._data_url = True
    mod.get_image_url = get_image_url
    return True


def install() -> list[str]:
    """安装补丁：mobileagent_v3（实际路径）+ autoglm/autoglm_v（兜底）。"""
    patched = []
    try:
        if _patch_mobileagent_v3():
            patched.append("mm_agents.mobileagent_v3.mobile_agent")
        if _patch_image_url():
            patched.append("mm_agents.mobileagent_v3.mobile_agent_modules")
    except Exception:
        pass

    candidates = [
        ("mm_agents.autoglm.prompt.grounding_agent", "GroundingAgent"),
        ("mm_agents.autoglm_v.prompt.grounding_agent", "GroundingAgent"),
    ]
    for mod_name, cls_name in candidates:
        try:
            mod = __import__(mod_name, fromlist=[cls_name])
            cls = getattr(mod, cls_name)
            if _patch_class(cls):
                patched.append(mod_name)
        except Exception:
            continue
    return patched


if __name__ == "__main__":
    import sys
    sys.path.insert(0, r"H:/GUI_AGENT_OSWorld/OSWorld-v1")
    print("patched:", install())
    from mm_agents.autoglm.prompt.grounding_agent import GroundingAgent as G
    tests = [
        ["ctrl", "c"],
        "['ctrl', 'shift', 's']",
        "ctrl+shift+s",
        ["CTRL", "C"],
        "ctrl",
    ]
    for t in tests:
        try:
            print(repr(t), "->", G.hotkey(t))
        except Exception as e:
            print(repr(t), "-> ERROR", type(e).__name__, e)
