# -*- coding: utf-8 -*-
"""Resume setup downloads and validate file sizes before upload."""
from __future__ import annotations

import glob
import os
import shutil
import time
import uuid

import requests

LOG_PREFIX = "[download_fix]"


def _fetch_robust(url: str, cache_path: str, logger, max_attempts: int = 40) -> int:
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    stalled = 0
    last_size = -1
    total_size = 0
    for attempt in range(1, max_attempts + 1):
        try:
            have = os.path.getsize(cache_path) if os.path.exists(cache_path) else 0
            headers = {"Range": f"bytes={have}-"} if have else {}
            with requests.get(url, stream=True, timeout=(20, 300), headers=headers) as resp:
                if resp.status_code == 416:                     # 已经下完
                    return have
                resp.raise_for_status()
                if have and resp.status_code != 206:            # 服务端不支持续传
                    os.remove(cache_path)
                    have = 0
                if not total_size:
                    cl = resp.headers.get("content-length")
                    if cl and not resp.headers.get("content-encoding"):
                        total_size = int(cl) + (have if resp.status_code == 206 else 0)
                        logger.info(f"{LOG_PREFIX} total {total_size / 1048576:.2f} MB, "
                                    f"resume from {have / 1048576:.2f} MB")
                mode = "ab" if have else "wb"
                got = 0
                with open(cache_path, mode) as fh:
                    for chunk in resp.iter_content(chunk_size=262144):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        got += len(chunk)
                        if total_size and (have + got) % (4 * 1048576) < 262144:
                            logger.info(f"{LOG_PREFIX} progress "
                                        f"{(have + got) / total_size * 100:.1f}%")
            size = os.path.getsize(cache_path)
            if total_size and size != total_size:
                raise RuntimeError(f"incomplete {size}/{total_size}")
            logger.info(f"{LOG_PREFIX} done {size / 1048576:.2f} MB in {attempt} attempt(s)")
            return size
        except Exception as exc:                                # noqa: BLE001
            now = os.path.getsize(cache_path) if os.path.exists(cache_path) else 0
            if now > last_size:
                stalled = 0
                last_size = now
            else:
                stalled += 1
            logger.warning(f"{LOG_PREFIX} attempt {attempt} failed at "
                           f"{now / 1048576:.2f} MB: {type(exc).__name__}: {exc}")
            if stalled >= 6:
                raise RuntimeError(f"{LOG_PREFIX} giving up after {attempt} tries "
                                   f"({now} bytes): {exc}") from exc
            time.sleep(min(2 * stalled + 2, 20))
    raise RuntimeError(f"{LOG_PREFIX} exhausted {max_attempts} attempts for {url}")


def install() -> bool:
    from desktop_env.controllers.setup import SetupController
    import logging

    logger = logging.getLogger("desktopenv.setup")

    def _download_setup(self, files):
        try:
            from requests_toolbelt import MultipartEncoder
        except Exception:                                        # noqa: BLE001
            MultipartEncoder = None

        for f in files:
            url = f["url"]
            path = f["path"]
            if not url or not path:
                raise Exception(f"Setup Download - Invalid URL ({url}) or path ({path}).")
            cache_path = os.path.join(
                self.cache_dir,
                "{:}_{:}".format(uuid.uuid5(uuid.NAMESPACE_URL, url), os.path.basename(path)),
            )

            if not os.path.exists(cache_path):
                # 先看共享缓存：同一 URL 的 basename 在不同调用方可能差一个下划线
                # （13_0.pptx vs 13_0_.pptx），按 uuid 前缀匹配就能命中，省掉整次下载。
                shared = os.environ.get("OSWORLD_SHARED_CACHE",
                                        r"H:/GUI_AGENT_OSWorld/setup_assets_verified")
                uid = str(uuid.uuid5(uuid.NAMESPACE_URL, url))
                seeded = None
                for cand in glob.glob(os.path.join(shared, uid + "_*")):
                    if cand.endswith((".part", ".sha256")):
                        continue
                    if os.path.isfile(cand) and os.path.getsize(cand) > 0:
                        seeded = cand
                        break
                if seeded:
                    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                    shutil.copyfile(seeded, cache_path)
                    logger.info(f"{LOG_PREFIX} seeded from shared cache: "
                                f"{os.path.basename(seeded)} -> {os.path.basename(cache_path)}")
                else:
                    logger.info(f"Cache file not found, downloading from {url} to {cache_path}")
                    _fetch_robust(url, cache_path, logger)
            else:
                logger.info(f"Cache hit: {cache_path}")

            if MultipartEncoder is None:
                raise RuntimeError("requests_toolbelt missing, cannot upload to VM")
            with open(cache_path, "rb") as fh:
                form = MultipartEncoder({
                    "file_path": path,
                    "file_data": (os.path.basename(path), fh),
                })
                resp = requests.post(self.http_server + "/setup/upload",
                                     headers={"Content-Type": form.content_type},
                                     data=form, timeout=600)
            if resp.status_code != 200:
                raise requests.RequestException(
                    f"Upload failed with status {resp.status_code}: {resp.text}")
            logger.info(f"File uploaded successfully: {path}")

    SetupController._download_setup = _download_setup
    print("[download_fix] patched: _download_setup (resume + progress-based retries)")
    return True
