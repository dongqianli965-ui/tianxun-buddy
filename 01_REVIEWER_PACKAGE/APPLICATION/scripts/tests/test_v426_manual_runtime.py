import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import v426_manual_runtime as runtime


def test_redact_removes_nested_credentials_and_keeps_business_tokens():
    value = {
        'password': 'secret',
        'nested': {'Authorization': 'Bearer token', 'items': [{'access_token': 'x', 'value': 7}]},
        'tokens': [{'text': 'OCR text'}],
    }
    cleaned = runtime.redact(value)
    assert cleaned['password'] == '[REDACTED]'
    assert cleaned['nested']['Authorization'] == '[REDACTED]'
    assert cleaned['nested']['items'][0]['access_token'] == '[REDACTED]'
    assert cleaned['tokens'][0]['text'] == 'OCR text'


def test_clean_environment_removes_inherited_production_values(monkeypatch):
    monkeypatch.setenv('TXBUDDY_SAMPLE_DSN', 'prod-dsn')
    monkeypatch.setenv('TX_PG_DSN', 'prod-dsn')
    monkeypatch.setenv('TX_INTERNAL_TOKEN', 'prod-token')
    monkeypatch.setenv('PGPASSWORD', 'prod-password')
    monkeypatch.setenv('SAFE_TEST_SETTING', 'kept')
    env = runtime.clean_environment({'TXBUDDY_ENV': 'TEST_ONLY'})
    assert env['SAFE_TEST_SETTING'] == 'kept'
    assert env['TXBUDDY_ENV'] == 'TEST_ONLY'
    for key in ('TXBUDDY_SAMPLE_DSN', 'TX_PG_DSN', 'TX_INTERNAL_TOKEN', 'PGPASSWORD'):
        assert key not in env


def test_assert_session_rejects_outside_and_non_test_paths(tmp_path):
    private = tmp_path / '_CODEX_V426' / 'manual'
    private.mkdir(parents=True)
    with patch.object(runtime, 'PRIVATE', private):
        good = private / 'TEST_ONLY-abc'
        good.mkdir()
        assert runtime.assert_session(good) == good.resolve()
        with pytest.raises(runtime.Blocked):
            runtime.assert_session(private / 'OTHER-abc')
        outside = tmp_path / 'TEST_ONLY-outside'
        outside.mkdir()
        with pytest.raises(runtime.Blocked):
            runtime.assert_session(outside)


def test_matching_process_rejects_pid_reuse_and_command_mismatch():
    record = {'pid': 123, 'create_time': 10.0, 'executable': 'C:/Python/python.exe',
              'command': ['C:/Python/python.exe', 'serve-backend', '--session', 'TEST_ONLY-x']}

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid
        def create_time(self):
            return 10.0
        def exe(self):
            return 'C:/Python/python.exe'
        def cmdline(self):
            return list(record['command'])

    assert runtime.matching_process(record, FakeProcess) is not None
    record['create_time'] = 11.0
    assert runtime.matching_process(record, FakeProcess) is None


def test_snapshot_columns_json_roundtrip_are_lists():
    value = {'columns': [list(('id', 'integer', 'int4', 'NO', None))]}
    assert json.loads(json.dumps(value)) == value


def test_stop_owned_terminates_recorded_orphan_only(monkeypatch):
    child_record = {'pid': 22}
    root_record = {'pid': 11, 'children': [child_record]}
    alive = {22: True}

    class Child:
        def terminate(self):
            alive[22] = False
        def wait(self, timeout):
            return 0

    monkeypatch.setattr(runtime, 'matching_process',
                        lambda record: Child() if alive.get(record['pid']) else None)
    result = runtime.stop_owned(root_record)
    assert result['stopped'] is True
    assert result['stopped_pids'] == [22]
    assert result['remaining_pids'] == []


def test_sqlite_snapshot_is_readonly_and_detects_logical_change(tmp_path):
    import sqlite3
    path = tmp_path / 'source.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE item (id INTEGER PRIMARY KEY, text TEXT)')
        connection.execute('INSERT INTO item VALUES (1, ?)', ('原始资料',))
    before = path.read_bytes()
    original = runtime.sqlite_snapshot(path)
    assert path.read_bytes() == before
    assert original['integrity'] == ['ok']
    assert original['tables']['item']['rows'] == 1
    with sqlite3.connect(path) as connection:
        connection.execute('UPDATE item SET text=? WHERE id=1', ('changed',))
    assert runtime.sqlite_snapshot(path) != original


def test_sqlite_snapshot_includes_uncheckpointed_wal_changes(tmp_path):
    import sqlite3
    path = tmp_path / 'source.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('CREATE TABLE item (id INTEGER PRIMARY KEY, text TEXT)')
        connection.execute('INSERT INTO item VALUES (1, ?)', ('before',))
        connection.commit()
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        before = runtime.sqlite_snapshot(path)
        connection.execute('UPDATE item SET text=? WHERE id=1', ('after',))
        connection.commit()
        main_file_before_snapshot = path.read_bytes()
        after = runtime.sqlite_snapshot(path)
        assert path.read_bytes() == main_file_before_snapshot
        assert after['integrity'] == ['ok']
        assert after['file_sha256'] == before['file_sha256']
        assert after['tables']['item']['logical_sha256'] != before['tables']['item']['logical_sha256']


def test_sqlite_snapshot_hash_is_repeatable_without_changes(tmp_path):
    import sqlite3
    path = tmp_path / 'source.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE item (text TEXT)')
        connection.execute('INSERT INTO item VALUES (?)', ('长 OCR 文本' * 10000,))
    assert runtime.sqlite_snapshot(path) == runtime.sqlite_snapshot(path)


def test_stop_without_original_snapshot_remains_blocked(tmp_path, monkeypatch):
    session=tmp_path/'TEST_ONLY-no-snapshot';session.mkdir()
    runtime.write_json(session/'state.private.json',{'status':'PREPARING','processes':{}})
    monkeypatch.setattr(runtime,'load_session',lambda _:session)
    assert runtime.stop()==2
    result=runtime.read_json(session/'STOP_RESULT.json')
    assert result['status']=='BLOCKED' and result['passed'] is False
    assert 'original_data_unchanged' not in result


def test_busy_port_never_terminates_existing_process(monkeypatch):
    monkeypatch.setattr(runtime,'port_available',lambda port:port!=8010)
    with pytest.raises(runtime.Blocked,match='8010'):
        runtime.require_free_ports()


def test_dependencies_preserves_node_and_npm_executable_paths(monkeypatch, tmp_path):
    node = tmp_path / 'node.exe'; node.write_text('node')
    npm = tmp_path / 'npm.cmd'; npm.write_text('npm')
    chrome = tmp_path / 'chrome.exe'; chrome.write_text('chrome')
    monkeypatch.setattr(runtime, 'executable', lambda name, candidates=():
                        str(node if name.startswith('node') else npm if name.startswith('npm') else chrome))
    class Completed:
        returncode = 0
        stdout = 'v-test\n'
    monkeypatch.setattr(runtime.subprocess, 'run', lambda *args, **kwargs: Completed())
    monkeypatch.setattr(runtime.importlib.util, 'find_spec', lambda _: object())
    monkeypatch.setattr(runtime, 'ROOT', tmp_path)
    (tmp_path / 'frontend/node_modules/vite/bin').mkdir(parents=True)
    (tmp_path / 'frontend/node_modules/vite/bin/vite.js').write_text('')
    (tmp_path / 'frontend/node_modules/@vitejs/plugin-vue').mkdir(parents=True)
    (tmp_path / 'frontend/node_modules/@vitejs/plugin-vue/package.json').write_text('{}')
    (tmp_path / 'frontend/node_modules/vue').mkdir(parents=True)
    (tmp_path / 'frontend/node_modules/vue/package.json').write_text('{}')
    found = runtime.dependencies()
    assert found['node'] == str(node.resolve()) and found['node_version'] == 'v-test'
    assert found['npm'] == str(npm.resolve()) and found['npm_version'] == 'v-test'
