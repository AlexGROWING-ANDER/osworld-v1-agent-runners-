"""Small dependency-free safeguards; no credentials or raw HTTP bodies logged."""
import ctypes
import json
import os
import time
from pathlib import Path
from typing import Callable


class ResourceBlocked(RuntimeError):
    pass


class InfrastructureFailure(RuntimeError):
    def __init__(self, category: str, module: str):
        super().__init__(category)
        self.category, self.module = category, module


def append_event(path: Path, event: dict) -> None:
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(event, ensure_ascii=False, default=str) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def classify_error(exc: Exception) -> str:
    if getattr(exc, 'category', None):
        return exc.category
    status = getattr(exc, 'status_code', None)
    if status in (401, 403):
        return 'authentication_or_permission'
    if status == 402:
        return 'billing_or_payment'
    if status == 429:
        return 'rate_or_quota_limit'
    name = type(exc).__name__
    if 'Timeout' in name:
        return 'api_timeout'
    if 'Connection' in name:
        return 'api_connection'
    if status and status >= 500:
        return 'api_server_error'
    return 'api_other_error'


def error_chain(exc: Exception) -> list[str]:
    names, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        names.append(type(exc).__name__)
        names.extend(getattr(exc, 'remote_chain', []))
        exc = exc.__cause__ or exc.__context__
    return names


def memory_snapshot() -> dict:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [(name, ctypes.c_ulonglong) for name in ('total_physical','free_physical','total_commit','free_commit','total_virtual','free_virtual','extended')]
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ResourceBlocked('Cannot verify Windows memory availability')
    return {name + '_mb': int(getattr(status, name) // 1024**2) for name in ('free_physical', 'free_commit')}


def check_memory(snapshot: dict, guest_mb: int, physical_min_mb: int | None = None) -> None:
    # Reserve approximately 1.8 GB for Python and 0.7 GB of physical headroom.
    # The commit headroom above the guest allocation is env-tunable so a busy host
    # can run a smaller guest without editing this guard; default stays 3500 MB.
    required_physical = guest_mb + 2500 if physical_min_mb is None else physical_min_mb
    if required_physical <= 0:
        raise ValueError('Physical memory threshold must be positive')
    commit_headroom = int(os.environ.get('OSWORLD_COMMIT_HEADROOM_MB', '3500'))
    if snapshot['free_physical_mb'] < required_physical or snapshot['free_commit_mb'] < guest_mb + commit_headroom:
        raise ResourceBlocked('Insufficient physical/commit headroom for guest and Python runtime')


class RuntimeGuard:
    def __init__(self, path: Path):
        self.path, self.stream = path, None

    def acquire(self) -> None:
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        self.stream.seek(0, 2)
        if self.stream.tell() == 0:
            self.stream.write(b'0')
            self.stream.flush()
        self.stream.seek(0)
        try:
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise ResourceBlocked('Another protected experiment owns runtime lock') from exc

    def close(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None


class ApiAudit:
    def __init__(self, path: Path, attempts: int = 3, sleep: Callable = time.sleep):
        self.path, self.attempts, self.sleep = path, attempts, sleep
        self.module = 'unknown'
        self.step = 0
        self.last_failure = None

    def call(self, operation: Callable, metadata: dict):
        self.last_failure = None
        for attempt in range(1, self.attempts + 1):
            event = dict(metadata, module=self.module, step=self.step, attempt=attempt, phase='request_started', time=time.time())
            append_event(self.path, event)
            started = time.monotonic()
            try:
                response = operation()
            except Exception as exc:
                category = classify_error(exc)
                append_event(self.path, dict(event, phase='request_failed', elapsed_seconds=time.monotonic()-started, category=category, exception_chain=error_chain(exc), http_status=getattr(exc,'status_code',None)))
                if category in ('api_timeout','api_connection','api_server_error') and attempt < self.attempts:
                    self.sleep(min(2**attempt, 8))
                    continue
                self.last_failure = InfrastructureFailure(category, self.module)
                raise self.last_failure from exc
            append_event(self.path, dict(event, phase='request_completed', elapsed_seconds=time.monotonic()-started))
            return response


def normalized_to_source(x: float, y: float) -> list[float]:
    if not (0 <= x <= 1000 and 0 <= y <= 1000):
        raise ValueError('Normalized grounding coordinates out of range')
    return [x * 1932 / 1000, y * 1092 / 1000]


def journal_action(folder: Path, events: Path, step: int, action, screenshot: Callable, execute: Callable):
    """Persist executed action before any reflector call; never retry execution."""
    folder.mkdir(exist_ok=True)
    event = {'step':step,'action':action,'phase':'before_execution','time':time.time()}
    (folder / 'before_action.png').write_bytes(screenshot())
    append_event(events, event)
    try:
        response = execute()
    except Exception as exc:
        append_event(events,dict(event,phase='execution_unknown',error_type=type(exc).__name__))
        raise
    append_event(events,dict(event,phase='execution_completed',time=time.time()))
    (folder / 'after_action.png').write_bytes(response[0]['screenshot'])
    return response
