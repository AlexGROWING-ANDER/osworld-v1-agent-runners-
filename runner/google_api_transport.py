"""Bounded proxied transport for the existing PyDrive OAuth credentials."""
from urllib.parse import urlsplit

import httplib2
import requests


class GoogleApiHttp:
    def __init__(self, timeout: float = 25, proxy: str = 'http://127.0.0.1:18081'):
        self.timeout = min(timeout or 25, 25)
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.proxies.update(http=proxy, https=proxy)
        self.cache = None
        self.connections = {}
        self.redirect_codes = {301, 302, 303, 307, 308}

    def request(self, uri: str, method: str = 'GET', body=None, headers=None,
                redirections: int = 5, connection_type=None):
        host = (urlsplit(uri).hostname or '').lower()
        if not any(host == domain or host.endswith('.' + domain)
                   for domain in ('google.com', 'googleapis.com')):
            raise ValueError('Google transport received an unexpected host')
        self.session.max_redirects = redirections
        # Only read requests may retry. Never replay uploads/deletes on uncertainty.
        attempts = 3 if method.upper() in ('GET', 'HEAD') else 1
        for attempt in range(attempts):
            try:
                response = self.session.request(method, uri, data=body, headers=headers,
                                                timeout=(10, self.timeout))
                content = response.content
                metadata = {k.lower(): v for k, v in response.headers.items()
                            if k.lower() not in ('content-encoding', 'content-length')}
                metadata.update(status=str(response.status_code),
                                **{'content-length': str(len(content))})
                result = httplib2.Response(metadata)
                result.reason = response.reason
                return result, content
            except (requests.ConnectionError, requests.Timeout):
                if attempt + 1 == attempts:
                    raise
        raise AssertionError('Unreachable request loop')

    def close(self) -> None:
        self.session.close()


def install() -> None:
    """Cover setup, protection inventory, and evaluator GoogleAuth instances."""
    from pydrive.auth import GoogleAuth
    if getattr(GoogleAuth, '_osworld_bounded_transport', False):
        return
    original_init = GoogleAuth.__init__

    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.http_timeout = min(self.http_timeout or 25, 25)
        self.http = GoogleApiHttp(self.http_timeout)

    def thread_http(self):
        return self.credentials.authorize(GoogleApiHttp(self.http_timeout))

    GoogleAuth.__init__ = initialize
    GoogleAuth.Get_Http_Object = thread_http
    GoogleAuth._osworld_bounded_transport = True
