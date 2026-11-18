"""Release safety tests operate only on synthetic files in pytest tmp_path."""
from pathlib import Path
import sys,zipfile,hashlib
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from package_v425 import scan_payload,write_archive

@pytest.mark.parametrize('name',['../escape.py','/absolute.py','C:/secret.py','dir\\escape.py','backend/.pg-dsn','config.private.json','state.sqlite3','backup.dump'])
def test_rejects_unsafe_or_private_archive_paths(name):
    with pytest.raises(ValueError):scan_payload(name,b'TEST_ONLY fixture')

def test_actual_private_value_is_rejected_even_inside_test_labeled_content():
    secret='TEST_ONLY-ephemeral-secret-should-never-be-shipped'
    with pytest.raises(ValueError,match='PRIVATE_VALUE_FOUND'):
        scan_payload('backend/test_fixture.py',('TEST_ONLY '+secret).encode(),[secret])

def test_rejects_private_key_material():
    with pytest.raises(ValueError,match='PRIVATE_KEY_CONTENT'):scan_payload('notes.txt',b'-----BEGIN ' + b'PRIVATE KEY-----\nTEST_ONLY\n')

def test_archive_has_exact_payload_and_hash_manifest(tmp_path):
    target=tmp_path/'safe.zip';files={'backend/a.py':b'TEST_ONLY = True\n','docs/说明.md':'TEST_ONLY 交付'.encode()}
    manifest=write_archive(files,target)
    with zipfile.ZipFile(target) as z:
        assert set(z.namelist())==set(files)|{'SHA256SUMS'}
        assert z.read('SHA256SUMS').decode()==manifest
        for path,raw in files.items():assert hashlib.sha256(z.read(path)).digest()==hashlib.sha256(raw).digest()
        assert z.testzip() is None

def test_existing_release_never_overwritten_and_rejected_stage_leaves_no_zip(tmp_path):
    target=tmp_path/'safe.zip';target.write_bytes(b'KEEP')
    with pytest.raises(FileExistsError):write_archive({'a.txt':b'new'},target)
    assert target.read_bytes()==b'KEEP'
    missing=tmp_path/'rejected.zip'
    with pytest.raises(ValueError):write_archive({'config.private.json':b'secret'},missing)
    assert not missing.exists() and not missing.with_suffix('.zip.tmp').exists()
