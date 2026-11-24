"""User-authorized test-VM Chrome profile, encrypted on the Windows host."""
import base64
import hashlib
import json
import time
from pathlib import Path

import requests

from google_session import crypt

PROFILE_FILE = Path('H:/GUI_AGENT_OSWorld/private_auth/google_profile.dpapi')


def execute(address: str, code: str) -> str:
    session = requests.Session()
    session.trust_env = False
    response = session.post(address + '/execute', json={'command': ['python3', '-c', code], 'shell': False}, timeout=(10, 90))
    response.raise_for_status()
    result = response.json()
    if result.get('returncode') != 0:
        raise RuntimeError('VM Google profile operation failed; sensitive output suppressed')
    return result['output'].strip()


def capture(address: str, profile_path: str = '/home/user/.config/google-chrome') -> dict:
    if '..' in profile_path.split('/') or (profile_path != '/home/user/.config/google-chrome' and not (profile_path.startswith('/home/user/.config/osworld-google-') and profile_path.endswith('/profile'))):
        raise ValueError('Profile capture must stay inside the authorized test-VM Chrome directory')
    code = """
import base64,io,os,signal,tarfile
from pathlib import Path
root=Path(PROFILE_PATH)
assert (root/'Local State').is_file()
skip={'Cache','Code Cache','GPUCache','GrShaderCache','ShaderCache','CacheStorage','Crash Reports','BrowserMetrics','History','History-journal','Top Sites','Top Sites-journal','Visited Links'}
processes=[]
buffer=io.BytesIO()
try:
 for p in Path('/proc').iterdir():
  if not p.name.isdigit():continue
  try:
   if p.stat().st_uid==os.getuid() and (p/'comm').read_text().strip()=='chrome':
    pid=int(p.name);os.kill(pid,signal.SIGSTOP);processes.append(pid)
  except (FileNotFoundError,ProcessLookupError,PermissionError):pass
 with tarfile.open(fileobj=buffer,mode='w:gz') as archive:
  def include(info):
   if not (info.isfile() or info.isdir()) or any(x in skip or x.startswith('Singleton') for x in Path(info.name).parts):return None
   return info
  archive.add(root,arcname='profile',filter=include)
finally:
 for pid in processes:
  try:os.kill(pid,signal.SIGCONT)
  except ProcessLookupError:pass
print(base64.b64encode(buffer.getvalue()).decode())
""".replace('PROFILE_PATH', repr(profile_path))
    content = base64.b64decode(execute(address, code), validate=True)
    if not 1000 < len(content) < 100_000_000:
        raise RuntimeError('Unexpected Google profile archive size')
    encrypted = crypt(content)
    if PROFILE_FILE.exists():
        backup = PROFILE_FILE.with_name('google_profile.before_' + str(time.time_ns()) + '.dpapi')
        with backup.open('xb') as stream:
            stream.write(PROFILE_FILE.read_bytes())
    temporary = PROFILE_FILE.with_name('google_profile.updated_' + str(time.time_ns()) + '.tmp')
    with temporary.open('xb') as stream:
        stream.write(encrypted)
    temporary.replace(PROFILE_FILE)
    return {'encrypted_profile_saved': True, 'bytes': len(content),
            'sha256': hashlib.sha256(content).hexdigest()}


def restore(address: str) -> str:
    content = crypt(PROFILE_FILE.read_bytes(), decrypt=True)
    digest = hashlib.sha256(content).hexdigest()
    target = '/tmp/osworld_google_profile_' + digest + '.tar.gz'
    session = requests.Session()
    session.trust_env = False
    response = session.post(address + '/setup/upload', data={'file_path': target},
                            files={'file_data': ('profile.tar.gz', content)}, timeout=(10, 90))
    response.raise_for_status()
    code = f"""
from pathlib import Path
import hashlib,json,tarfile,tempfile
source=Path({target!r})
assert hashlib.sha256(source.read_bytes()).hexdigest()=={digest!r}
root=Path(tempfile.mkdtemp(prefix='osworld-google-',dir=str(Path.home()/'.config'))).resolve()
with tarfile.open(source) as archive:
 for item in archive.getmembers():
  dest=(root/item.name).resolve()
  assert root in dest.parents or dest==root
  assert item.isfile() or item.isdir()
  archive.extract(item,path=root)
preferences=root/'profile/Default/Preferences'
if preferences.is_file():
 data=json.loads(preferences.read_text())
 data.setdefault('profile',{{}}).update(exit_type='Normal',exited_cleanly=True)
 data.setdefault('session',{{}})['restore_on_startup']=5
 preferences.write_text(json.dumps(data))
source.unlink()
print(str(root/'profile'))
"""
    profile = execute(address, code)
    if not profile.startswith('/home/user/.config/osworld-google-') or not profile.endswith('/profile'):
        raise RuntimeError('Unexpected restored profile path')
    return profile


def verified_available() -> bool:
    """Formal setup may only use the exact archive that passed cold validation."""
    proof_path = Path(__file__).resolve().parents[1] / 'osworld_repair_20261023_remaining7/profile_cold_proof.json'
    try:
        proof = json.loads(proof_path.read_text(encoding='utf-8'))
        if not all(proof.get(key) is True for key in ('cold_restore_verified', 'reload_verified', 'account_match')):
            return False
        return hashlib.sha256(crypt(PROFILE_FILE.read_bytes(), decrypt=True)).hexdigest() == proof['profile_sha256']
    except (OSError, ValueError, KeyError):
        return False
