"""Resolve private-VM Chrome discovery without host HTTP proxy inheritance."""
import ipaddress
from urllib.parse import urlsplit, urlunsplit
import requests

def resolve_private_cdp(endpoint: str) -> str:
    address=urlsplit(endpoint)
    if address.scheme not in ('http','https'):return endpoint
    try:private=ipaddress.ip_address(address.hostname).is_private
    except ValueError:return endpoint
    if not private:return endpoint
    with requests.Session() as session:
        session.trust_env=False
        result=session.get(endpoint.rstrip('/')+'/json/version',timeout=10)
        result.raise_for_status()
        socket=urlsplit(result.json()['webSocketDebuggerUrl'])
    if socket.scheme not in ('ws','wss') or not socket.path.startswith('/devtools/browser/'):
        raise RuntimeError('Invalid VM Chrome browser websocket')
    return urlunsplit((socket.scheme,address.netloc,socket.path,socket.query,socket.fragment))

def install() -> None:
    from playwright.sync_api import BrowserType
    original=BrowserType.connect_over_cdp
    if getattr(original,'_osworld_direct',False):return
    def connect(self,endpoint_url,**kwargs):
        kwargs.setdefault('timeout',60000)
        return original(self,resolve_private_cdp(endpoint_url),**kwargs)
    connect._osworld_direct=True
    BrowserType.connect_over_cdp=connect
