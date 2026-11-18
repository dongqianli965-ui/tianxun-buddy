from pathlib import Path
import importlib.util
import pytest

def scanner():
    path=Path(__file__).resolve().parents[2]/'scripts/package_v424.py'
    spec=importlib.util.spec_from_file_location('v424_package',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def test_sensitive_file_names_refused():
    m=scanner()
    for path in ('backend/.pg-dsn','backend/.env','config.private.json','data/business.sqlite3','private/backup.dump','server.pem'):
        with pytest.raises(ValueError,match='SENSITIVE_PATH'):m.scan_payload(path,b'not-safe')

def test_secret_content_in_ordinary_file_is_refused():
    m=scanner();key_header='-----BEGIN '+'RSA'+' PRIVATE KEY-----'
    with pytest.raises(ValueError,match='PRIVATE_KEY_CONTENT'):m.scan_payload('docs/ordinary.md',key_header.encode())
    probe=b''.join([b'postgresql://', b'operator:', b'ProdCredential923', b'@localhost/database'])
    with pytest.raises(ValueError,match='CREDENTIAL_DSN'):m.scan_payload('docs/ordinary.md',probe)

def test_source_gather_keeps_runtime_out(tmp_path):
    m=scanner()
    for d in ('backend','frontend','docs','schemas','scripts','sql','workflows','reports/v424/screenshots'):(tmp_path/d).mkdir(parents=True,exist_ok=True)
    (tmp_path/'backend/.pg-dsn').write_text('private');(tmp_path/'backend/main.py').write_text('print(1)')
    (tmp_path/'backend/data/v424-runtime').mkdir(parents=True);(tmp_path/'backend/data/v424-runtime/auth.sqlite3').write_bytes(b'private')
    result=m.gather(tmp_path)
    assert 'backend/main.py' in result and not any(m.sensitive_name(p) for p in result)
