import json
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from package_v426_manual import manual_private_values, validate, collect
from package_v426 import scan_payload

def test_manual_generated_password_excluded(tmp_path):
    private=tmp_path.parent/'_CODEX_V426/manual/TEST_ONLY-unit'
    private.mkdir(parents=True,exist_ok=True)
    file=private/'TEST_ACCOUNTS.private.json'
    file.write_text(json.dumps({'accounts':{'OWNER':{'password':'random-unit-password-123456'}}}))
    values=manual_private_values(tmp_path)
    with pytest.raises(ValueError,match='PRIVATE_VALUE_FOUND'):
        scan_payload('report.md',b'random-unit-password-123456',values)

def test_gate_cannot_accept_missing_evidence(tmp_path):
    with pytest.raises(FileNotFoundError):validate(tmp_path)

def test_collect_requires_one_click_launchers(tmp_path):
    (tmp_path/'reports/v426').mkdir(parents=True)
    with pytest.raises(ValueError,match='MISSING_DELIVERY_FILE'):collect(tmp_path)
