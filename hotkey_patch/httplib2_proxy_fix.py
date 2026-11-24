# -*- coding: utf-8 -*-
"""Send PyDrive HTTP requests through the configured host proxy."""
import os

import httplib2
import requests

_PROXY = os.environ.get("OSWORLD_HTTP_PROXY_URL", "http://127.0.0.1:18081")


class _Resp(dict):
    """冒充 httplib2.Response：dict 存的响应头 + status 属性。"""

    def __init__(self, r):
        super().__init__(r.headers)
        self.status = r.status_code
        self.reason = r.reason
        self.version = 11
        self.previous = None
        self._url = r.url

    def __getitem__(self, k):
        if k == "status":
            return str(self.status)
        if k == "content-location":
            return self._url
        return super().__getitem__(k)

    def get(self, k, default=None):
        if k == "status":
            return str(self.status)
        return super().get(k, default)


class ProxiedHttp(httplib2.Http):
    def __init__(self, *a, **kw):
        self.timeout = kw.get("timeout") or 90
        self.proxy_info = kw.get("proxy_info")
        self.force_exception_to_status_code = False

    def request(self, uri, method="GET", body=None, headers=None, **kw):
        h = dict(headers or {})
        # httplib2 会把 Authorization 放 headers；requests 照用
        r = requests.request(
            method, uri, data=body, headers=h, timeout=self.timeout,
            proxies={"http": _PROXY, "https": _PROXY},
            allow_redirects=True,
        )
        return _Resp(r), r.content

    # 少数调用方会显式调这两个
    def clear_credentials(self):
        return None


def install():
    httplib2.Http = ProxiedHttp
    try:
        import apiclient.discovery as _d  # noqa: F401
    except Exception:
        pass
    return True
