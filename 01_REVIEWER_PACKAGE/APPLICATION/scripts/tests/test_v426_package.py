from pathlib import Path
import hashlib, json, sys, zipfile
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from package_v426 import gather, scan_payload, source_manifest, validate_gate, write_archive


@pytest.mark.parametrize('name', ['../escape.py', '/absolute.py', 'C:/secret.py', 'backend/.pg-dsn', 'config.private.json', 'state.sqlite3', 'dump.zip', 'sample.pg-dsn', '.npmrc', 'store.sqlite3-wal', 'original.pdf', 'auth-state.json'])
def test_v426_rejects_unsafe_or_private_paths(name):
    with pytest.raises(ValueError):
        scan_payload(name, b'TEST_ONLY fixture')


def test_v426_rejects_private_value_and_dsn():
    with pytest.raises(ValueError, match='PRIVATE_VALUE_FOUND'):
        scan_payload('notes.md', b'TEST_ONLY secret-value', ['secret-value'])
    with pytest.raises(ValueError, match='UNREDACTED_DSN'):
        scan_payload('notes.md', b'postgresql://' + b'alice:real-password@host/db')


def test_v426_unrelated_test_word_does_not_bypass_dsn_scan():
    with pytest.raises(ValueError, match='UNREDACTED_DSN'):
        scan_payload('notes.md', b'Test results passed. postgresql://' + b'alice:real-password@host/db')
    scan_payload('example.md', b'postgresql://user:pass@localhost/db')


def test_v426_private_keys_are_rejected_even_under_tests():
    key = b'-----' + b'BEGIN PRIVATE KEY-----\n' + b'A' * 64 + b'\n-----END PRIVATE KEY-----'
    with pytest.raises(ValueError, match='PRIVATE_KEY_CONTENT'):
        scan_payload('backend/tests/fixture.py', key)


def test_v426_archive_manifest_crc_and_hash(tmp_path):
    target = tmp_path / 'safe.zip'
    files = {'backend/fix.py': b'TEST_ONLY = True\n', 'V426_DELIVERY_PROFILE.md': 'BLOCKED'.encode()}
    manifest = write_archive(files, target)
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert archive.read('SHA256SUMS').decode() == manifest
        for name, raw in files.items():
            assert hashlib.sha256(archive.read(name)).digest() == hashlib.sha256(raw).digest()


def test_v426_archive_never_overwrites_and_cleans_rejected_stage(tmp_path):
    target = tmp_path / 'existing.zip'
    target.write_bytes(b'KEEP')
    with pytest.raises(FileExistsError):
        write_archive({'a.txt': b'new'}, target)
    assert target.read_bytes() == b'KEEP'
    rejected = tmp_path / 'rejected.zip'
    with pytest.raises(ValueError):
        write_archive({'config.private.json': b'secret'}, rejected)
    assert not rejected.exists()
    assert not rejected.with_name(rejected.name + '.tmp').exists()


def test_v426_gather_includes_execution_state_and_excludes_private_data(tmp_path):
    for relative in ('reports/v426/TEST_SUMMARY.json', 'V426_EXECUTION_STATE.json', 'EXECUTION_STATE.md', 'scripts/V426_TASK_QUEUE.json', 'backend/fix.py', 'backend/.env', 'backend/state.sqlite3', 'backend/original.pdf', 'frontend/node_modules/private.json', 'backend/.venv/Lib/site-packages/dependency.py', 'backend/venv/private.json', 'backend/.pytest_cache/cache.json'):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('TEST_ONLY fixture', encoding='utf-8')
    files = gather(tmp_path)
    assert {'V426_EXECUTION_STATE.json', 'EXECUTION_STATE.md', 'scripts/V426_TASK_QUEUE.json', 'backend/fix.py', 'reports/v426/TEST_SUMMARY.json'} <= set(files)
    assert not any(name in files for name in ('backend/.env', 'backend/state.sqlite3', 'backend/original.pdf', 'frontend/node_modules/private.json', 'backend/.venv/Lib/site-packages/dependency.py', 'backend/venv/private.json', 'backend/.pytest_cache/cache.json'))


def test_v426_gather_preserves_runtime_and_test_dependencies(tmp_path):
    for relative in ('backend/requirements.txt', 'scripts/package_v424.py', 'scripts/package_v425.py', 'scripts/v425_e2e_seed.py', 'scripts/v425_ocr_setup.py'):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# TEST_ONLY fixture', encoding='utf-8')
    (tmp_path / 'reports/v426').mkdir(parents=True)
    files = gather(tmp_path)
    assert {'backend/requirements.txt', 'scripts/package_v424.py', 'scripts/package_v425.py', 'scripts/v425_e2e_seed.py', 'scripts/v425_ocr_setup.py'} <= set(files)


def test_v426_delivery_gate_rejects_post_test_source_and_summary_drift(tmp_path):
    out = tmp_path / 'reports/v426'
    out.mkdir(parents=True)
    code = tmp_path / 'backend/fix.py'
    code.parent.mkdir(parents=True)
    code.write_text('TEST_ONLY = True', encoding='utf-8')
    source = source_manifest(tmp_path)
    summary = {'backend_tests': 1, 'backend_failures': 0, 'backend_errors': 0, 'backend_skipped': 0, 'frontend_browser_tests': 4, 'frontend_browser_failures': 0, 'frontend_build_exit_code': 0, 'production_accepted': False, 'accuracy_E': 'BLOCKED'}
    reports = {
        'FINAL_RESULT.json': {'exit_code': 0, 'tests': 1, 'failures': 0, 'errors': 0, 'skipped': 0, 'source_unchanged_during_run': True, 'working_source_matches_tested_copy': True, 'tested_source_sha256': source},
        'FINAL_FRONTEND_RESULT.json': {'build_exit_code': 0, 'browser_exit_code': 0, 'source_unchanged_during_run': True, 'stats': {'expected': 4, 'unexpected': 0, 'skipped': 0, 'flaky': 0}, 'tested_source_sha256': source},
        'FINAL_DATA_INVENTORY.json': dict.fromkeys(('original_files_unchanged', 'live_databases_logically_unchanged', 'sqlite_integrity_passed'), True),
        'MATERIAL_PRICE_VALIDATION.json': {'total': 10989, 'unverified_source_count': 0},
        'BOQ_VALIDATION.json': {'total_rows': 148, 'independently_calculated': False, 'accuracy_verification': 'BLOCKED'},
        'OCR_HUMAN_REVIEW_READINESS.json': {'task_count': 10, 'human_reviews_created': 0, 'professional_signoffs_created': 0, 'human_review_status': 'BLOCKED'},
        'OCR_CONTENT_DEDUP.json': {'assets_verified_unchanged': True, 'human_reviews_created': 0},
        'TEST_SUMMARY.json': summary,
    }
    for name, data in reports.items():
        (out / name).write_text(json.dumps(data), encoding='utf-8')
    assert validate_gate(tmp_path)['backend']['tests'] == 1
    code.write_text('TEST_ONLY = False', encoding='utf-8')
    with pytest.raises(ValueError, match='SOURCE_CHANGED_SINCE_FINAL_TEST'):
        validate_gate(tmp_path)
    code.write_text('TEST_ONLY = True', encoding='utf-8')
    summary['backend_tests'] = 999
    (out / 'TEST_SUMMARY.json').write_text(json.dumps(summary), encoding='utf-8')
    with pytest.raises(ValueError, match='TEST_SUMMARY_MISMATCH'):
        validate_gate(tmp_path)


def test_v426_external_extraction_verifies_manifest_source_and_test_evidence(tmp_path):
    from v426_package_verify import verify
    root = tmp_path / 'workspace'
    out = root / 'reports/v426'
    out.mkdir(parents=True)
    source_file = root / 'backend/fix.py'
    source_file.parent.mkdir()
    source_file.write_text('TEST_ONLY = True', encoding='utf-8')
    source = source_manifest(root)
    reports = {
        'FINAL_RESULT.json': {'tested_source_sha256': source, 'tests': 1},
        'FINAL_FRONTEND_RESULT.json': {'tested_source_sha256': source, 'stats': {'expected': 4}},
        'TEST_SUMMARY.json': {'backend_tests': 1, 'frontend_browser_tests': 4, 'production_accepted': False, 'accuracy_E': 'BLOCKED'},
    }
    for name, data in reports.items():
        (out / name).write_text(json.dumps(data), encoding='utf-8')
    profile = 'V4.2.5.1 SecurityFix'
    files = gather(root, profile)
    target = tmp_path / 'delivery.zip'
    manifest = write_archive(files, target)
    report = {'archive': str(target), 'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'manifest_sha256': hashlib.sha256(manifest.encode()).hexdigest(), 'archive_entries': len(files) + 1, 'payload_files': len(files), 'profile': profile, 'backend_tests': 1, 'frontend_tests': 4, 'production_accepted': False}
    report_path = tmp_path / 'delivery.json'
    report_path.write_text(json.dumps(report), encoding='utf-8')
    result = verify(report_path, tmp_path / 'extracted', root)
    assert result['passed'] and result['sensitive_findings'] == 0
    target.write_bytes(target.read_bytes() + b'TAMPER')
    with pytest.raises(ValueError, match='ARCHIVE_SHA256_MISMATCH'):
        verify(report_path, tmp_path / 'rejected', root)
    assert not (tmp_path / 'rejected').exists()
