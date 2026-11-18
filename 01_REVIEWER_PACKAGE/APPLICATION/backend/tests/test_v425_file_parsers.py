"""V425 archive trust boundaries and offline conversion failure semantics."""
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import sys
import zipfile
import zlib

import pytest

from txbuddy.parsing import parse_file, supported_extensions
from txbuddy.parsing.base import ParseStatus
from txbuddy.parsing.archive_adapter import ArchiveLimits, ArchiveRejected, parse_archive, safe_member_name, _zip_name
from txbuddy.parsing import doc_adapter, archive_adapter
from txbuddy.parsing.process_limits import bounded_run, ProcessLimitError, ProcessResult


def make_zip(path, entries, compression=zipfile.ZIP_STORED):
    with zipfile.ZipFile(path, 'w', compression=compression) as archive:
        for name, value in entries:
            archive.writestr(name, value)
    return path


def derived(tmp_path):
    return tmp_path / 'separate' / 'derived'


def test_doc_zip_rar_are_dispatched():
    assert {'doc', 'zip', 'rar'} <= set(supported_extensions())


def test_zip_real_member_parsing_and_repeat_attempt_provenance(tmp_path):
    path = make_zip(tmp_path / 'valid.zip', [('中文说明.txt', '真实输入示例 TEST_ONLY')])
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    first = parse_file(path, derived_root=derived(tmp_path))
    second = parse_file(path, derived_root=derived(tmp_path))
    assert first.status is ParseStatus.PARSED
    assert '真实输入示例' in first.text
    assert first.metadata['archive_entries'][0]['parse_result']['format'] == 'txt'
    entry = first.metadata['archive_entries'][0]
    assert entry['sha256'] == hashlib.sha256(Path(entry['derived_path']).read_bytes()).hexdigest()
    assert entry['archive_sha256'] == before
    assert first.metadata['manifest_path'] != second.metadata['manifest_path']
    assert Path(first.metadata['manifest_path']).is_file()
    assert first.sha256 == second.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert first.metadata['formal_usable'] is False


@pytest.mark.parametrize('name', ['../escape.txt', '/absolute.txt', 'C:/escape.txt',
    r'..\escape.txt', 'folder/../../escape.txt', 'file:stream.txt', 'NUL.txt',
    'folder/COM1', 'name. ', 'folder//a.txt', 'folder/./a.txt', 'a\x00b.txt',
    'a\x1fb.txt', 'a\u202eb.txt', 'foo?.txt'])
def test_member_names_reject_traversal_aliases_and_controls(name):
    with pytest.raises(ArchiveRejected):
        safe_member_name(name)


@pytest.mark.parametrize('entries', [
    [('Readme.txt', 'a'), ('README.TXT', 'b')],
    [('caf\u00e9.txt', 'a'), ('cafe\u0301.txt', 'b')],
    [('file', 'a'), ('file/child.txt', 'b')],
    [('file/child.txt', 'a'), ('file', 'b')],
])
def test_duplicate_and_file_directory_collisions_have_no_extracted_members(tmp_path, entries):
    path = make_zip(tmp_path / 'dup.zip', entries)
    result = parse_archive(path, derived_root=derived(tmp_path))
    assert result.status is ParseStatus.FAILED
    assert 'COLLISION' in result.error or 'DUPLICATE' in result.error
    assert result.metadata['archive_entries'] == []
    assert not list(derived(tmp_path).rglob('*.txt'))


def test_zip_symlink_is_rejected_before_writing(tmp_path):
    info = zipfile.ZipInfo('link.txt')
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    path = make_zip(tmp_path / 'link.zip', [(info, '../escape')])
    result = parse_archive(path, derived_root=derived(tmp_path))
    assert result.status is ParseStatus.FAILED
    assert result.error == 'ARCHIVE_LINK_OR_SPECIAL_FILE'


@pytest.mark.parametrize('setting, value, expected', [
    ('max_archive_bytes', 8, 'INPUT_TOO_LARGE'),
    ('max_member_bytes', 8, 'MEMBER_TOO_LARGE'),
    ('max_total_bytes', 15, 'TOTAL_TOO_LARGE'),
    ('max_members', 1, 'MEMBER_COUNT'),
])
def test_archive_size_and_entry_limits_are_enforced(tmp_path, setting, value, expected):
    path = make_zip(tmp_path / 'limits.zip', [('a.txt', 'a' * 10), ('b.txt', 'b' * 10)])
    result = parse_archive(path, derived_root=derived(tmp_path), archive_limits=replace(ArchiveLimits(), **{setting: value}))
    assert result.status is ParseStatus.FAILED
    assert expected in result.error
    assert result.metadata['archive_entries'] == []


def test_zip_bomb_ratio_is_rejected(tmp_path):
    path = make_zip(tmp_path / 'bomb.zip', [('bomb.txt', 'A' * 500000)], zipfile.ZIP_DEFLATED)
    result = parse_archive(path, derived_root=derived(tmp_path))
    assert result.status is ParseStatus.FAILED
    assert result.error == 'ARCHIVE_COMPRESSION_RATIO_LIMIT'
    assert result.blocks == []


def test_zip_decoder_allowlist_rejects_unbounded_dictionary_methods(tmp_path):
    path = make_zip(tmp_path / 'lzma.zip', [('payload.txt', 'TEST_ONLY')], zipfile.ZIP_LZMA)
    result = parse_archive(path, derived_root=derived(tmp_path))
    assert result.status is ParseStatus.BLOCKED
    assert result.blocked_reason.startswith('UNSUPPORTED: ZIP compression method')
    assert result.metadata['archive_entries'] == []


def test_nested_and_disguised_archives_are_registered_without_recursion(tmp_path):
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as archive:
        archive.writestr('payload.txt', 'TEST_ONLY')
    path = make_zip(tmp_path / 'outer.zip', [('renamed.txt', content.getvalue())])
    result = parse_archive(path, derived_root=derived(tmp_path))
    assert result.status is ParseStatus.BLOCKED
    entry = result.metadata['archive_entries'][0]
    assert entry['reason'] == 'NESTED_ARCHIVE_UNSUPPORTED'
    assert entry['sha256'] == hashlib.sha256(content.getvalue()).hexdigest()
    assert 'derived_path' not in entry


def test_malformed_utf8_filename_fails_closed(tmp_path):
    path = make_zip(tmp_path / 'badname.zip', [('abc.txt', 'TEST_ONLY')])
    raw = bytearray(path.read_bytes())
    local = raw.index(b'PK\x03\x04')
    central = raw.index(b'PK\x01\x02')
    struct.pack_into('<H', raw, local + 6, 0x800)
    struct.pack_into('<H', raw, central + 8, 0x800)
    raw[local + 30] = 0xff
    raw[central + 46] = 0xff
    path.write_bytes(raw)
    result = parse_archive(path, derived_root=derived(tmp_path))
    assert result.status is ParseStatus.FAILED
    assert result.metadata['archive_entries'] == []


def test_unicode_path_extra_requires_crc_and_safe_paths():
    info = zipfile.ZipInfo('safe.txt')
    payload = b'\x01' + struct.pack('<L', zlib.crc32(b'safe.txt')) + '../escape.txt'.encode()
    info.extra = struct.pack('<HH', 0x7075, len(payload)) + payload
    with pytest.raises(ArchiveRejected, match='TRAVERSAL'):
        _zip_name(info)
    info.extra = struct.pack('<HH', 0x7075, len(payload)) + payload[:1] + b'\x00' * 4 + payload[5:]
    with pytest.raises(ArchiveRejected, match='CRC_INVALID'):
        _zip_name(info)


def test_zip_crc_corruption_cannot_report_parsed(tmp_path):
    path = make_zip(tmp_path / 'badcrc.zip', [('a.txt', b'TEST_ONLY_PAYLOAD')])
    raw = bytearray(path.read_bytes())
    raw[raw.index(b'TEST_ONLY_PAYLOAD')] ^= 1
    path.write_bytes(raw)
    result = parse_archive(path, derived_root=derived(tmp_path))
    assert result.status is ParseStatus.FAILED
    assert result.blocks == []
    assert 'CRC' in result.error


def test_encrypted_zip_flag_cannot_be_bypassed(tmp_path):
    path = make_zip(tmp_path / 'encrypted.zip', [('a.txt', 'TEST_ONLY')])
    raw = bytearray(path.read_bytes())
    for magic, flagpos in [(b'PK\x03\x04', 6), (b'PK\x01\x02', 8)]:
        struct.pack_into('<H', raw, raw.index(magic) + flagpos, 1)
    path.write_bytes(raw)
    result = parse_archive(path, derived_root=derived(tmp_path))
    assert result.status is ParseStatus.FAILED
    assert result.error == 'ENCRYPTED_ARCHIVE_UNSUPPORTED'


def test_central_directory_count_is_checked_before_member_objects(tmp_path):
    path = make_zip(tmp_path / 'fakecount.zip', [('a.txt', 'a'), ('b.txt', 'b')])
    raw = bytearray(path.read_bytes())
    end = raw.index(b'PK\x05\x06')
    struct.pack_into('<HH', raw, end + 8, 1, 1)
    path.write_bytes(raw)
    result = parse_archive(path, derived_root=derived(tmp_path))
    assert result.status is ParseStatus.FAILED
    assert result.error == 'ZIP_CENTRAL_DIRECTORY_COUNT_MISMATCH'


def test_doc_missing_engine_remains_blocked_and_records_original_hash(tmp_path, monkeypatch):
    path = tmp_path / 'input.doc'
    path.write_bytes(bytes.fromhex('d0cf11e0a1b11ae1') + b'TEST_ONLY')
    before = path.read_bytes()
    monkeypatch.setattr(doc_adapter, 'probe_doc_converter', lambda: {'available': False, 'reason': 'DOC_LEGACY_CONVERTER_REQUIRED'})
    result = parse_file(path, derived_root=derived(tmp_path))
    assert result.status is ParseStatus.BLOCKED
    assert result.blocks == []
    assert result.sha256 == hashlib.sha256(before).hexdigest()
    assert path.read_bytes() == before
    assert Path(result.metadata['attempt_record']).is_file()


def test_doc_timeout_is_failure_with_audit_and_original_unchanged(tmp_path, monkeypatch):
    path = tmp_path / 'input.doc'
    path.write_bytes(bytes.fromhex('d0cf11e0a1b11ae1') + b'TEST_ONLY')
    monkeypatch.setattr(doc_adapter, 'probe_doc_converter', lambda: {'available': True, 'version': 'TEST_ONLY',
        'executable': 'TEST_ONLY', 'mapping_directory': None})
    def fail(*args, **kwargs):
        raise ProcessLimitError('PROCESS_TIMEOUT')
    monkeypatch.setattr(doc_adapter, 'bounded_run', fail)
    result = parse_file(path, derived_root=derived(tmp_path))
    assert result.status is ParseStatus.FAILED
    assert result.error == 'PROCESS_TIMEOUT'
    assert result.metadata['original_preserved'] is True


def test_doc_invalid_magic_is_not_success(tmp_path):
    path = tmp_path / 'fake.doc'
    path.write_text('TEST_ONLY', encoding='utf-8')
    result = parse_file(path, derived_root=derived(tmp_path))
    assert result.status is ParseStatus.FAILED
    assert result.error.startswith('DOC_MAGIC_INVALID')


def test_doc_success_keeps_original_and_append_only_derived_attempts(tmp_path, monkeypatch):
    path = tmp_path / 'input.doc'
    path.write_bytes(bytes.fromhex('d0cf11e0a1b11ae1') + b'TEST_ONLY')
    monkeypatch.setattr(doc_adapter, 'probe_doc_converter', lambda: {'available': True, 'version': 'TEST_ONLY_FIXTURE',
        'executable': 'TEST_ONLY', 'mapping_directory': None})
    monkeypatch.setattr(doc_adapter, 'bounded_run', lambda *args, **kwargs: ProcessResult(0, '复核文本 TEST_ONLY\n第二行'.encode(), b''))
    first = parse_file(path, derived_root=derived(tmp_path))
    second = parse_file(path, derived_root=derived(tmp_path))
    assert first.status is ParseStatus.PARSED
    assert first.text == '复核文本 TEST_ONLY\n第二行'
    assert first.metadata['converted_sha256'] == hashlib.sha256(Path(first.metadata['converted_file']).read_bytes()).hexdigest()
    assert first.metadata['attempt_record'] != second.metadata['attempt_record']
    assert first.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert first.metadata['formal_usable'] is False


def test_rar_without_verified_tool_returns_unsupported(tmp_path, monkeypatch):
    path = tmp_path / 'archive.rar'
    path.write_bytes(b'Rar!\x1a\x07\x01\x00TEST_ONLY')
    monkeypatch.setattr(archive_adapter, 'probe_rar_tool', lambda: {'available': False, 'reason': 'UNSUPPORTED: tool not verified'})
    result = parse_file(path, derived_root=derived(tmp_path))
    assert result.status is ParseStatus.BLOCKED
    assert result.metadata['extraction_status'] == 'UNSUPPORTED'
    assert result.blocked_reason.startswith('UNSUPPORTED:')


def test_rar_member_path_preflight_precedes_extraction(tmp_path, monkeypatch):
    path = tmp_path / 'archive.rar'
    path.write_bytes(b'Rar!\x1a\x07\x01\x00TEST_ONLY')
    monkeypatch.setattr(archive_adapter, 'probe_rar_tool', lambda: {'available': True, 'version': 'TEST_ONLY', 'executable': 'TEST_ONLY'})
    monkeypatch.setattr(archive_adapter, '_rar_metadata', lambda *args: {'entries': [
        {'name': '../outside.txt', 'size': 1, 'compressed': 1, 'link': False, 'special': False}]})
    def forbid(*args, **kwargs):
        raise AssertionError('no extractor may execute before path preflight')
    monkeypatch.setattr(archive_adapter, 'bounded_run', forbid)
    result = parse_file(path, derived_root=derived(tmp_path))
    assert result.status is ParseStatus.FAILED
    assert 'TRAVERSAL' in result.error


@pytest.mark.parametrize('field', ['link', 'special'])
def test_rar_links_and_special_entries_never_reach_extractor(tmp_path, monkeypatch, field):
    path = tmp_path / 'archive.rar'
    path.write_bytes(b'Rar!\x1a\x07\x01\x00TEST_ONLY')
    monkeypatch.setattr(archive_adapter, 'probe_rar_tool', lambda: {'available': True, 'version': 'TEST_ONLY', 'executable': 'TEST_ONLY'})
    entry = {'name': 'safe.txt', 'size': 1, 'compressed': 1, 'link': False, 'special': False}
    entry[field] = True
    monkeypatch.setattr(archive_adapter, '_rar_metadata', lambda *args: {'entries': [entry]})
    result = parse_file(path, derived_root=derived(tmp_path))
    assert result.status is ParseStatus.FAILED
    assert result.error == 'ARCHIVE_LINK_OR_SPECIAL_FILE'
    assert result.metadata['archive_entries'] == []


@pytest.mark.parametrize('field', ['encrypted', 'multivolume'])
def test_rar_encrypted_or_multi_volume_is_unsupported_without_extraction(tmp_path, monkeypatch, field):
    path = tmp_path / 'archive.rar'
    path.write_bytes(b'Rar!\x1a\x07\x01\x00TEST_ONLY')
    monkeypatch.setattr(archive_adapter, 'probe_rar_tool', lambda: {'available': True, 'version': 'TEST_ONLY', 'executable': 'TEST_ONLY'})
    monkeypatch.setattr(archive_adapter, '_rar_metadata', lambda *args: {field: True, 'entries': []})
    result = parse_file(path, derived_root=derived(tmp_path))
    assert result.status is ParseStatus.BLOCKED
    assert result.metadata['extraction_status'] == 'UNSUPPORTED'
    assert result.metadata['archive_entries'] == []


def test_converter_process_timeout_is_enforced():
    with pytest.raises(ProcessLimitError, match='TIMEOUT'):
        bounded_run([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=.2)


def test_converter_output_limit_is_enforced():
    with pytest.raises(ProcessLimitError, match='STDOUT_LIMIT'):
        bounded_run([sys.executable, '-c', "import sys;sys.stdout.write('x'*100000)"], max_stdout=1000)


def test_converter_memory_limit_is_enforced():
    result = bounded_run([sys.executable, '-c', 'x=bytearray(512*1024*1024)'], memory_bytes=96*1024*1024)
    assert result.returncode != 0


def test_derived_outputs_never_enter_source_directory_or_corpus(tmp_path, monkeypatch):
    path = tmp_path / 'original' / 'file.doc'
    path.parent.mkdir()
    path.write_bytes(b'TEST_ONLY')
    monkeypatch.setenv('TXBUDDY_CORPUS_ROOT', str(path.parent))
    with pytest.raises(ValueError, match='DERIVED_DIRECTORY_INVALID'):
        parse_file(path, derived_root=path.parent)
    with pytest.raises(ValueError, match='DERIVED_DIRECTORY_INVALID'):
        parse_file(path, derived_root=path.parent/'derived')


def test_archive_scanned_pdf_default_does_not_run_ocr(tmp_path):
    from pypdf import PdfWriter
    buffer = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(buffer)
    path = make_zip(tmp_path / 'scanned.zip', [('page.pdf', buffer.getvalue())])
    result = parse_archive(path, derived_root=derived(tmp_path))
    assert result.status is ParseStatus.BLOCKED
    assert result.metadata['archive_ocr_enabled'] is False
    member = result.metadata['archive_entries'][0]
    assert member['parse_result']['format'] == 'pdf'
    assert 'OCR_REQUIRED' in member['reason']
    assert not list(derived(tmp_path).rglob('ocr.json'))


def cache_fixture(tmp_path, monkeypatch):
    from txbuddy.parsing.base import ParsedDocument
    source = tmp_path / 'page.pdf'
    source.write_bytes(b'TEST_ONLY_SOURCE')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    root = tmp_path / 'cache'
    folder = root / digest
    folder.mkdir(parents=True)
    image = folder / 'page_0001.png'
    image.write_bytes(b'TEST_ONLY_IMAGE')
    saved = {'sha256': digest, 'formal_usable': False, 'review_status': 'NEEDS_EXPERT_REVIEW',
             'requested_pages': None, 'tokens': [{'text': 'TEST_ONLY'}],
             'pages': [{'page': 1, 'image': str(image), 'image_sha256': hashlib.sha256(image.read_bytes()).hexdigest()}]}
    cache = folder / 'ocr.json'
    cache.write_text(json.dumps(saved), encoding='utf-8')
    monkeypatch.setenv('TXBUDDY_ARCHIVE_OCR_CACHE', str(root))
    native = ParsedDocument(str(source), 'pdf', ParseStatus.FAILED, metadata={'pages': 1})
    return source, root, cache, image, saved, native


def test_explicit_ocr_cache_requires_original_and_image_hash_and_full_coverage(tmp_path, monkeypatch):
    from txbuddy.parsing.member_worker import verified_ocr_cache
    source, root, cache, image, saved, native = cache_fixture(tmp_path, monkeypatch)
    original_cache = cache.read_bytes()
    assert verified_ocr_cache(source, native) == root.resolve()
    assert cache.read_bytes() == original_cache
    image.write_bytes(b'CHANGED_TEST_ONLY_IMAGE')
    assert verified_ocr_cache(source, native) is None


@pytest.mark.parametrize('field, value', [('sha256', 'different'), ('formal_usable', True),
    ('review_status', 'REVIEWED'), ('requested_pages', [1]), ('tokens', [])])
def test_ocr_cache_invalid_or_review_status_cannot_be_promoted(tmp_path, monkeypatch, field, value):
    from txbuddy.parsing.member_worker import verified_ocr_cache
    source, root, cache, image, saved, native = cache_fixture(tmp_path, monkeypatch)
    saved[field] = value
    cache.write_text(json.dumps(saved), encoding='utf-8')
    assert verified_ocr_cache(source, native) is None
