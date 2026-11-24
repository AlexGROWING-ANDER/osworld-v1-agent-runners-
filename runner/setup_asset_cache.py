"""Cache official setup inputs atomically; never cache task-mutated history."""
import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path

import requests

CACHE = Path('H:/GUI_AGENT_OSWorld/setup_assets_verified')
HISTORY_URL = ('https://huggingface.co/datasets/xlangai/ubuntu_osworld_file_cache/'
               'resolve/main/chrome/44ee5668-ecd5-4366-a6ce-c1c9b8d4e938/history_empty.sqlite?download=true')


def fetch(url: str, basename: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    target = CACHE / f'{uuid.uuid5(uuid.NAMESPACE_URL, url)}_{Path(basename).name}'
    proof = target.with_name(target.name + '.sha256')
    if target.exists() and proof.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() == proof.read_text().strip():
            return target
    # 同一个 URL 不同调用方给出的 basename 可能差一个下划线（13_0_.pptx vs 13_0.pptx），
    # 缓存键里带了 basename 就会误判为未命中、白下一个 20MB。这里按 uuid 前缀兜底复用。
    _uid = uuid.uuid5(uuid.NAMESPACE_URL, url).hex
    for cand in CACHE.glob(f'{_uid}_*'):
        if cand.name.endswith('.part') or cand.name.endswith('.sha256'):
            continue
        cproof = cand.with_name(cand.name + '.sha256')
        if not (cand.is_file() and cproof.exists()):
            continue
        if hashlib.sha256(cand.read_bytes()).hexdigest() != cproof.read_text().strip():
            continue
        if cand.name != target.name:
            shutil.copyfile(cand, target)
            proof.write_text(cproof.read_text().strip(), encoding='ascii')
        return target
    temporary = target.with_name(target.name + '.part')

    # 走代理下 HuggingFace 会频繁断流（ChunkedEncodingError / IncompleteRead），
    # 而且速度会从 200KB/s 掉到 10KB/s。策略：
    #   · Range 断点续传，断了从断点接着下
    #   · 只要有进展就不算失败（连续 6 次零进展才放弃）
    #   · 校验用"已下载字节 == 服务端声明的总长度"，不再用累加值双重计数
    last = None
    stalled = 0
    last_size = -1
    for attempt in range(1, 61):
        try:
            resumed = temporary.stat().st_size if temporary.exists() else 0
            headers = {'Range': f'bytes={resumed}-'} if resumed else {}
            with requests.get(url, stream=True, timeout=(20, 120), headers=headers) as response:
                if response.status_code == 416:          # 断点已到文件末尾
                    break
                response.raise_for_status()
                if resumed and response.status_code != 206:
                    temporary.unlink(missing_ok=True)     # 服务端不支持续传，重头来
                    resumed = 0
                added = 0
                with temporary.open('ab' if resumed else 'wb') as stream:
                    for chunk in response.iter_content(1024 * 128):
                        if not chunk:
                            continue
                        stream.write(chunk)
                        added += len(chunk)
                cl = response.headers.get('content-length')
                if cl and not response.headers.get('content-encoding'):
                    expected = int(cl) + (resumed if response.status_code == 206 else 0)
                    if resumed + added != expected:
                        raise RuntimeError(
                            f'incomplete: {resumed + added}/{expected} bytes')
            break
        except Exception as exc:                          # noqa: BLE001
            last = exc
            now = temporary.stat().st_size if temporary.exists() else 0
            if now > last_size:
                stalled = 0
                last_size = now
            else:
                stalled += 1
            if stalled >= 6:
                raise RuntimeError(
                    f'Setup asset download failed after {attempt} tries '
                    f'({now} bytes on disk): {last}') from last
            time.sleep(min(2 * stalled + 2, 20))
    else:
        raise RuntimeError(f'Setup asset download failed: {last}')

    total = temporary.stat().st_size
    digest = hashlib.sha256()
    with temporary.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 512), b''):
            digest.update(chunk)
    with temporary.open('rb') as stream:
        signature = stream.read(16)
    if basename.endswith('.xlsx') and not signature.startswith(b'PK'):
        raise RuntimeError('Setup spreadsheet has invalid signature')
    if basename.endswith('.sqlite') and signature != b'SQLite format 3\x00':
        raise RuntimeError('Setup database has invalid signature')
    if not total:
        raise RuntimeError('Setup asset download produced empty file')
    os.replace(temporary, target)
    proof.write_text(digest.hexdigest(), encoding='ascii')
    return target


def prepare(task: dict, local_cache: Path) -> None:
    local_cache.mkdir(parents=True, exist_ok=True)
    for step in task.get('config', []):
        if step.get('type') == 'download':
            for asset in step.get('parameters', {}).get('files', []):
                name = Path(asset['path']).name
                source = fetch(asset['url'], name)
                shutil.copyfile(source, local_cache / source.name)
        elif step.get('type') == 'update_browse_history':
            shutil.copyfile(fetch(HISTORY_URL, 'history_empty.sqlite'), local_cache / 'history_new.sqlite')


def missing_credentials(task: dict, repo: Path) -> list[str]:
    if any(s.get('type') == 'googledrive' for s in task.get('config', [])):
        required = ['evaluation_examples/settings/googledrive/client_secrets.json',
                    'evaluation_examples/settings/googledrive/credentials.json']
        missing = [p for p in required if not (repo / p).is_file()]
        for local in ('H:/GUI_AGENT_OSWorld/private_auth/google_cookies.dpapi',
                      'H:/GUI_AGENT_OSWorld/private_auth/restore_verified.json'):
            if not Path(local).is_file(): missing.append(local)
        return missing
    return []
