"""Explicit user consent covers one verified, backed-up public invoice only."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'osworld_repair_20261023_remaining7'


def allowed_existing_fixture(item: dict, query: str) -> bool:
    from google_session import crypt, COOKIE_FILE
    try:
        consent = json.loads((ROOT / 'invoice_cleanup_authorization.json').read_text(encoding='utf-8'))
        proof = json.loads((ROOT / 'google_api_provenance.local.json').read_text(encoding='utf-8'))
        if (consent.get('permanent_delete_authorized') is not True
                or consent.get('task') != 'multi_apps/4e9f0faf-2ecc-4ae8-a804-28c9a75d1ddc'
                or query != consent.get('query') or query != "title = 'invoice.pdf'"
                or item.get('id') not in consent.get('file_ids', [])
                or item.get('mimeType') != 'application/pdf'
                or item.get('md5Checksum') != proof.get('fixture_md5')
                or int(item.get('fileSize', -1)) != proof.get('fixture_bytes')
                or proof.get('fixture_sha256') != consent.get('required_fixture_sha256')):
            return False
        for entry in proof.get('invoice_matches', []):
            if (entry.get('metadata', {}).get('id') != item['id']
                    or entry.get('exact_bytes_verified') is not True
                    or entry.get('matches_public_fixture') is not True):
                continue
            backup = Path(entry['encrypted_backup']).resolve()
            if backup != COOKIE_FILE.with_name('invoice_fixture_backup.dpapi').resolve():
                return False
            return hashlib.sha256(crypt(backup.read_bytes(), decrypt=True)).hexdigest() == proof['fixture_sha256']
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return False
