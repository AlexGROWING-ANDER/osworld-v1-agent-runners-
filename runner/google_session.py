"""Google-only login restoration; encrypted local storage, no credential logging."""
import ctypes
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import requests
import websocket

COOKIE_FILE = Path('H:/GUI_AGENT_OSWorld/private_auth/google_cookies.dpapi')

class Blob(ctypes.Structure):
    _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_ubyte))]

def crypt(data: bytes, decrypt: bool = False) -> bytes:
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    if decrypt:
        ok = ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        ok = ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), 'OSWorld Google login', None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)

class CDP:
    def __init__(self, address: str):
        s = requests.Session(); s.trust_env = False
        version = s.get(address+'/json/version',timeout=10).json()
        parts = urlsplit(version['webSocketDebuggerUrl'])
        endpoint = urlunsplit((parts.scheme,urlsplit(address).netloc,parts.path,parts.query,parts.fragment))
        self.ws = websocket.create_connection(endpoint,timeout=65,suppress_origin=True,http_no_proxy=[urlsplit(address).hostname])
        self.seq = 0

    def call(self, method: str, params: dict | None = None, session: str | None = None):
        self.seq += 1
        request = dict(id=self.seq,method=method,params=params or {})
        if session:
            request['sessionId'] = session
        self.ws.send(json.dumps(request))
        deadline = time.monotonic()+65
        while time.monotonic()<deadline:
            response = json.loads(self.ws.recv())
            if response.get('id') == self.seq:
                if 'error' in response:
                    raise RuntimeError('CDP operation failed: '+method)
                return response.get('result',{})
        raise TimeoutError('CDP operation timed out: '+method)

    def close(self):
        self.ws.close()

def persist_verified_context(cdp: CDP, context: str | None = None) -> None:
    """After authenticated Drive DOM validation, checkpoint rotated cookies."""
    fields = {'name', 'value', 'domain', 'path', 'expires', 'httpOnly', 'secure', 'sameSite'}
    params = {'browserContextId': context} if context else {}
    cookies = [{k: v for k, v in item.items() if k in fields}
               for item in cdp.call('Storage.getCookies', params)['cookies']
               if item['domain'].lstrip('.') in {'google.com', 'accounts.google.com', 'drive.google.com'}]
    names = {item['name'] for item in cookies}
    # Drive can rotate away the global SID while retaining its own authenticated
    # OSID pair. The caller has already verified the signed-in Drive DOM.
    if 'SID' not in names and not {'OSID', '__Secure-OSID'}.issubset(names):
        raise RuntimeError('Authenticated session has no Google/Drive auth cookies')
    encrypted = crypt(json.dumps(cookies).encode())
    assert json.loads(crypt(encrypted, decrypt=True)) == cookies
    stamp = str(time.time_ns())
    if COOKIE_FILE.exists():
        with COOKIE_FILE.with_name('google_cookies.before_refresh_' + stamp + '.dpapi').open('xb') as stream:
            stream.write(COOKIE_FILE.read_bytes())
    temporary = COOKIE_FILE.with_name('google_cookies.refresh_' + stamp + '.tmp')
    with temporary.open('xb') as stream:
        stream.write(encrypted)
    os.replace(temporary, COOKIE_FILE)

def restore_and_verify(address: str, isolated: bool = False, inject_cookies: bool = True) -> None:
    cdp = CDP(address)
    context = None
    target = None
    try:
        cookies = json.loads(crypt(COOKIE_FILE.read_bytes(),decrypt=True)) if inject_cookies else []
        extra = {}
        if isolated:
            context = cdp.call('Target.createBrowserContext', {'proxyServer': 'http://192.168.92.1:18888'})['browserContextId']
            extra['browserContextId'] = context
        if inject_cookies:
            cdp.call('Storage.setCookies',dict(cookies=cookies,**extra))
        target = cdp.call('Target.createTarget',dict(url='about:blank',**extra))['targetId']
        session = cdp.call('Target.attachToTarget',dict(targetId=target,flatten=True))['sessionId']
        cdp.call('Page.navigate',dict(url='https://drive.google.com/drive/my-drive'),session)
        deadline = time.monotonic()+60
        while time.monotonic()<deadline:
            result = cdp.call('Runtime.evaluate',dict(expression="location.origin === 'https://drive.google.com' && location.pathname.startsWith('/drive/') && !!document.querySelector('[role=main]') && !!document.querySelector('a[href*=SignOutOptions]')",returnByValue=True),session)
            if result.get('result',{}).get('value') is True:
                if not isolated:
                    persist_verified_context(cdp)
                return
            time.sleep(1)
        raise RuntimeError('Authenticated Google Drive UI did not become ready')
    finally:
        if isolated and context:
            cdp.call('Target.disposeBrowserContext',dict(browserContextId=context))
        cdp.close()

def install_login_restore(controller_class: type) -> None:
    """Use a preauthenticated session instead of storing the user's password."""
    from google_api_transport import install
    install()
    original_launch = controller_class._launch_setup
    def launch(self, command=None, shell=False, **config):
        # SetupController.launch() calls self._launch_setup(command, shell=shell)
        # positionally, so this hook must accept the positional form as well as
        # the config-dict keyword form used by _setup_config().
        config=dict(config)
        if command is not None:
            config['command']=command
        if shell:
            config['shell']=shell
        command = config.get('command')
        if (isinstance(command,list) and command and command[0]=='google-chrome'
                and '--proxy-server=http://192.168.92.1:18888' in command):
            from google_profile import verified_available, restore
            if verified_available():
                profile=restore(self.http_server)
                config={**config,'command':[*command,'--user-data-dir='+profile]}
                self._osworld_profile_path=profile
        return original_launch(self,**config)
    controller_class._launch_setup=launch
    original = controller_class._login_setup
    def login(self, **config):
        if config.get('platform') != 'googledrive':
            return original(self, **config)
        restore_and_verify(f'http://{self.vm_ip}:{self.chromium_port}',
                           inject_cookies=not bool(getattr(self,'_osworld_profile_path',None)))
    controller_class._login_setup = login
    from drive_setup_compat import fixed_upload_path_method
    original_drive = fixed_upload_path_method(controller_class._googledrive_setup)
    def drive_setup(self, **config):
        # Validate deletion targets against the test fixture allowlist.
        if 'delete' in config.get('operation', []):
            from pydrive.auth import GoogleAuth
            from pydrive.drive import GoogleDrive
            auth = GoogleAuth(settings_file=config.get('settings_file','evaluation_examples/settings/googledrive/settings.yml'))
            auth.http_timeout = 20
            auth.LoadCredentialsFile('evaluation_examples/settings/googledrive/credentials.json')
            if auth.access_token_expired: auth.Refresh()
            auth.Authorize()
            drive = GoogleDrive(auth)
            for operation, params in zip(config['operation'],config['args']):
                if operation != 'delete': continue
                query = params.get('query','')
                if not query.strip():
                    raise RuntimeError('Google Drive initialization target requires personal-data review; no deletion performed')
                from google_fixture_guard import allowed_existing_fixture
                matches = drive.ListFile({'q':query,'maxResults':100}).GetList()
                if any(not allowed_existing_fixture(item, query) for item in matches):
                    raise RuntimeError('Google Drive initialization target requires personal-data review; no deletion performed')
        return original_drive(self, **config)
    controller_class._googledrive_setup = drive_setup

def configure_google_task(task: dict) -> bool:
    if not any(step.get('type') == 'googledrive' for step in task.get('config', [])):
        return False
    for step in task['config']:
        command = step.get('parameters', {}).get('command')
        if step.get('type') == 'launch' and isinstance(command,list) and command and command[0] == 'google-chrome':
            command.extend(['--no-first-run','--no-default-browser-check',
                            '--proxy-server=http://192.168.92.1:18888','--disable-quic',
                            '--remote-debugging-address=127.0.0.1'])
        if step.get('type') == 'launch' and isinstance(command,list) and command and command[0] == 'socat':
            for index,value in enumerate(command):
                if value == 'tcp-listen:9222,fork':
                    command[index]='tcp-listen:9222,bind=192.168.92.129,range=192.168.92.1/32,fork'
    return True
