"""Bounded, read-only archive extraction with per-member provenance.

Names are validated before any member bytes are written. Extracted files use
numeric paths chosen by the adapter, never an archive-supplied filesystem path.
No recursive archives, links, encrypted files or overwrite operations are used.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import tempfile
import time
import unicodedata
import zipfile
import zlib

from .base import Locator, ParsedDocument, ParseStatus, TextBlock, source_facts, sha256_file
from .doc_adapter import derived_directory
from .process_limits import bounded_run, ProcessLimitError


@dataclass(frozen=True)
class ArchiveLimits:
    max_archive_bytes: int = 32 * 1024 * 1024
    max_members: int = 128
    max_member_bytes: int = 32 * 1024 * 1024
    max_total_bytes: int = 128 * 1024 * 1024
    max_ratio: int = 100
    timeout_seconds: float = 60
    member_parse_seconds: float = 30
    memory_bytes: int = 1024 * 1024 * 1024


class ArchiveRejected(ValueError):
    pass


_NESTED_SUFFIXES = {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.tgz', '.cab', '.iso', '.zst'}
_PACKAGE_SUFFIXES = {'.docx', '.xlsx', '.xlsm'}
_DEVICES = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))}


def safe_member_name(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > 512:
        raise ArchiveRejected('ARCHIVE_NAME_INVALID')
    # Preserve and reject characters that Windows normalizes or interprets as
    # alternate streams, devices and trailing-dot/space aliases.
    if '\\' in name or '\x00' in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ArchiveRejected('ARCHIVE_PATH_INVALID: control or backslash')
    if any(c in name for c in ':*?"<>|') or name.startswith('/'):
        raise ArchiveRejected('ARCHIVE_PATH_INVALID: absolute, stream or wildcard')
    clean = name[:-1] if name.endswith('/') else name
    parts = clean.split('/')
    if not clean or len(parts) > 16:
        raise ArchiveRejected('ARCHIVE_PATH_INVALID: depth')
    for part in parts:
        if part in ('', '.', '..') or part.rstrip(' .') != part:
            raise ArchiveRejected('ARCHIVE_PATH_TRAVERSAL_OR_ALIAS')
        if part.split('.')[0].upper() in _DEVICES:
            raise ArchiveRejected('ARCHIVE_DEVICE_NAME')
    normalized = unicodedata.normalize('NFC', clean)
    if any(unicodedata.category(c) in ('Cs', 'Cc', 'Cf') for c in normalized):
        raise ArchiveRejected('ARCHIVE_NAME_ENCODING_INVALID')
    return normalized


def _budget(size, compressed, total, limits):
    if size < 0 or compressed < 0 or size > limits.max_member_bytes:
        raise ArchiveRejected('ARCHIVE_MEMBER_TOO_LARGE')
    if total + size > limits.max_total_bytes:
        raise ArchiveRejected('ARCHIVE_TOTAL_TOO_LARGE')
    if size > max(1, compressed) * limits.max_ratio:
        raise ArchiveRejected('ARCHIVE_COMPRESSION_RATIO_LIMIT')


def _zip_preflight(path: Path, limits: ArchiveLimits):
    # Validate the actual central-directory record count before ZipFile allocates
    # ZipInfo objects. ZIP64/multipart are deliberately unsupported here.
    with path.open('rb') as handle:
        handle.seek(max(0, path.stat().st_size - 65557))
        tail = handle.read(65557)
        pos = tail.rfind(b'PK\x05\x06')
        if pos < 0 or len(tail) - pos < 22:
            raise ArchiveRejected('ZIP_EOCD_INVALID')
        end = struct.unpack_from('<4s4H2LH', tail, pos)
        _, disk, cddisk, diskcount, count, cdsize, offset, comment = end
        if disk or cddisk or diskcount != count or count == 65535:
            raise ArchiveRejected('ZIP_MULTIPART_OR_ZIP64_UNSUPPORTED')
        if count > limits.max_members or len(tail) - pos != 22 + comment:
            raise ArchiveRejected('ARCHIVE_MEMBER_COUNT_OR_TRAILING_DATA')
        if offset + cdsize > path.stat().st_size:
            raise ArchiveRejected('ZIP_CENTRAL_DIRECTORY_INVALID')
        handle.seek(offset)
        consumed = 0
        actual = 0
        while consumed < cdsize:
            fixed = handle.read(46)
            if len(fixed) != 46 or fixed[:4] != b'PK\x01\x02':
                raise ArchiveRejected('ZIP_CENTRAL_DIRECTORY_INVALID')
            namesize, extrasize, commentsize = struct.unpack_from('<HHH', fixed, 28)
            length = namesize + extrasize + commentsize
            if length > cdsize - consumed - 46:
                raise ArchiveRejected('ZIP_CENTRAL_DIRECTORY_INVALID')
            handle.seek(length, 1)
            consumed += 46 + length
            actual += 1
            if actual > limits.max_members:
                raise ArchiveRejected('ARCHIVE_MEMBER_COUNT_LIMIT')
        if actual != count or consumed != cdsize:
            raise ArchiveRejected('ZIP_CENTRAL_DIRECTORY_COUNT_MISMATCH')


def _zip_name(info):
    raw = info.orig_filename.encode('utf-8' if info.flag_bits & 0x800 else 'cp437', errors='strict')
    # Both the raw spelling and an Info-ZIP Unicode Path extra field must pass
    # safety checks. CRC ties that extra field to the central-directory name.
    name = raw.decode('utf-8' if info.flag_bits & 0x800 else 'cp437', errors='strict')
    safe_member_name(name)
    encoding = 'utf-8' if info.flag_bits & 0x800 else 'cp437'
    extra = info.extra
    unicode_path_seen = False
    while extra:
        if len(extra) < 4:
            raise ArchiveRejected('ZIP_EXTRA_INVALID')
        kind, size = struct.unpack_from('<HH', extra)
        if size > len(extra) - 4:
            raise ArchiveRejected('ZIP_EXTRA_INVALID')
        payload = extra[4:4 + size]
        if kind == 0x7075:
            if unicode_path_seen:
                raise ArchiveRejected('ZIP_DUPLICATE_UNICODE_NAME_EXTRA')
            unicode_path_seen = True
            if len(payload) < 5 or payload[0] != 1 or struct.unpack_from('<L', payload, 1)[0] != zlib.crc32(raw):
                raise ArchiveRejected('ZIP_UNICODE_NAME_CRC_INVALID')
            unicode_name = payload[5:].decode('utf-8', errors='strict')
            safe_member_name(unicode_name)
            name = unicode_name
            encoding = 'infozip-unicode-path-utf8-crc32'
        extra = extra[4 + size:]
    return safe_member_name(name), raw.hex(), encoding


def _nested(raw: bytes, suffix: str) -> bool:
    if suffix in _NESTED_SUFFIXES:
        return True
    iszip = raw[:4] in (b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08')
    if iszip and suffix in _PACKAGE_SUFFIXES:
        return False
    return iszip or raw.startswith((b'Rar!\x1a\x07', b"7z\xbc\xaf\x27\x1c", b'\x1f\x8b', b'BZh', b'\xfd7zXZ\x00')) or raw[257:262] == b'ustar'


def _parse_member(path: Path, target: Path, limits: ArchiveLimits, archive_ocr=False, ocr_cache=None) -> dict:
    env = os.environ.copy()
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2])
    env['PYTHONIOENCODING'] = 'utf-8'
    env['TXBUDDY_DERIVED_ROOT'] = str(target / 'member-derived')
    env['TXBUDDY_ARCHIVE_OCR'] = '1' if archive_ocr else '0'
    env.pop('TXBUDDY_ARCHIVE_OCR_CACHE', None)
    if ocr_cache is not None:
        env['TXBUDDY_ARCHIVE_OCR_CACHE'] = str(Path(ocr_cache).resolve())
    run = bounded_run([sys.executable, '-m', 'txbuddy.parsing.member_worker', str(path)],
        timeout=limits.member_parse_seconds, cpu_seconds=limits.member_parse_seconds,
        memory_bytes=limits.memory_bytes, max_stdout=16 * 1024 * 1024, env=env)
    if run.returncode != 0:
        return {'status': 'FAILED', 'error': 'MEMBER_PARSER_EXIT_' + str(run.returncode),
                'stderr': run.stderr.decode('utf-8', 'replace')[:1000], 'blocks': []}
    return json.loads(run.stdout.decode('utf-8', errors='strict'))


def _register(raw: bytes, name: str, ordinal: int, target: Path, limits: ArchiveLimits,
              archive_sha: str, name_info: dict, archive_ocr=False, ocr_cache=None):
    digest = hashlib.sha256(raw).hexdigest()
    suffix = PurePosixPath(name).suffix.lower()
    record = {'member_id': f'{archive_sha}:{ordinal}', 'archive_sha256': archive_sha,
              'name': name, 'sha256': digest, 'size_bytes': len(raw), 'format': suffix.lstrip('.'),
              'source_role': 'DERIVED_FROM_ARCHIVE', 'formal_usable': False, **name_info}
    detected = ('pdf' if raw.startswith(b'%PDF-') else 'ole-compound' if raw.startswith(bytes.fromhex('d0cf11e0a1b11ae1'))
                else 'zip-container' if raw.startswith(b'PK\x03\x04') else 'png' if raw.startswith(b'\x89PNG\r\n\x1a\n')
                else 'jpeg' if raw.startswith(b'\xff\xd8\xff') else 'text-or-unknown')
    record['detected_format'] = detected
    if detected == 'pdf' and suffix != '.pdf':
        record.update(status='BLOCKED', reason='MEMBER_FORMAT_EXTENSION_MISMATCH')
        return record, []
    # Detect disguised archives before putting them into a general parser.
    if _nested(raw, suffix):
        record.update(status='BLOCKED', reason='NESTED_ARCHIVE_UNSUPPORTED')
        return record, []
    path = target / 'members' / f'{ordinal:04}_{digest}{suffix}'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as handle:
        handle.write(raw)
    record['derived_path'] = str(path)
    if suffix in _PACKAGE_SUFFIXES:
        _zip_preflight(path, limits)
        with zipfile.ZipFile(path) as package:
            total = 0
            names = set()
            for entry in package.infolist():
                child_name, _, _ = _zip_name(entry)
                key = child_name.casefold()
                if key in names:
                    raise ArchiveRejected('PACKAGE_DUPLICATE_MEMBER')
                names.add(key)
                _budget(entry.file_size, entry.compress_size, total, limits)
                total += entry.file_size
    # Text, PDF, OLE, OOXML and remaining formats retain their own parser result.
    parsed = _parse_member(path, target, limits, archive_ocr, ocr_cache)
    record['parse_result'] = parsed
    record['status'] = parsed['status']
    if parsed['status'] != 'PARSED' and suffix in ('.pdf', '.png', '.jpg', '.jpeg'):
        record['reason'] = 'OCR_REQUIRED_OR_PARSER_FAILURE: inspect native parser result'
    if parsed.get('metadata', {}).get('review_status') == 'NEEDS_EXPERT_REVIEW':
        record['review_status'] = 'NEEDS_EXPERT_REVIEW'
    blocks = []
    for block in parsed.get('blocks', []):
        locator = Locator(**block['locator'])
        blocks.append(TextBlock(block['text'], locator, f'archive_member:{ordinal}:{block.get("kind", "text")}'))
    return record, blocks


def probe_rar_tool() -> dict:
    if os.name != 'nt':
        return {'available': False, 'reason': 'UNSUPPORTED: no independently verified RAR tool'}
    tool = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/tar.exe'
    if not tool.is_file():
        return {'available': False, 'reason': 'UNSUPPORTED: Windows bsdtar unavailable'}
    try:
        env = os.environ.copy()
        env['TXBUDDY_RAR_TOOL_PROBE'] = str(tool)
        script = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security/Microsoft.PowerShell.Security.psd1'); $s=Get-AuthenticodeSignature -LiteralPath $env:TXBUDDY_RAR_TOOL_PROBE; [pscustomobject]@{status=[string]$s.Status;signer=$s.SignerCertificate.Subject}|ConvertTo-Json -Compress"
        sig = bounded_run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', script],
                          timeout=12, memory_bytes=512 * 1024 * 1024, max_stdout=16384, max_stderr=16384, env=env)
        if sig.returncode:
            raise ValueError('signature probe failed')
        signer = json.loads(sig.stdout.decode('utf-8-sig', 'strict'))
        version = bounded_run([str(tool), '--version'], timeout=5, max_stdout=16384, max_stderr=16384)
        text = version.stdout.decode('ascii', errors='strict').strip()
        if signer.get('status') != 'Valid' or 'Microsoft Windows' not in signer.get('signer', '') or 'libarchive' not in text:
            raise ValueError('signature or library not identified')
        return {'available': True, 'executable': str(tool), 'executable_sha256': sha256_file(tool),
                'version': text, 'license': 'BSD-2-Clause (libarchive); installed Windows component',
                'license_source': 'https://github.com/libarchive/libarchive/blob/master/COPYING',
                'signature': signer, 'offline': True}
    except (ValueError, OSError, UnicodeError, ProcessLimitError) as exc:
        return {'available': False, 'reason': 'UNSUPPORTED: RAR tool verification failed: ' + str(exc)}


def _rar_metadata(path: Path, limits: ArchiveLimits):
    env = os.environ.copy()
    # Keep a caller-supplied dependency path (e.g. an isolated vendor directory).
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[2]) + os.pathsep + env.get('PYTHONPATH', '')
    env['PYTHONIOENCODING'] = 'utf-8'
    run = bounded_run([sys.executable, '-m', 'txbuddy.parsing.rar_metadata_worker', str(path), str(limits.max_members)],
                      timeout=10, memory_bytes=limits.memory_bytes, max_stdout=1024 * 1024, env=env)
    if run.returncode:
        raise ArchiveRejected('RAR_METADATA_FAILED: ' + run.stderr.decode('utf-8', 'replace')[:1000])
    data = json.loads(run.stdout.decode('utf-8', 'strict'))
    if data.get('unsupported'):
        raise ArchiveRejected('UNSUPPORTED: ' + data['unsupported'])
    return data


def parse_archive(path: str | Path, *, derived_root=None, archive_limits: ArchiveLimits | None = None,
                  archive_ocr: bool = False, archive_ocr_cache=None, **_kwargs) -> ParsedDocument:
    source = Path(path).resolve()
    digest, size = source_facts(source)
    suffix = source.suffix.lower().lstrip('.')
    limits = archive_limits or ArchiveLimits()
    result = ParsedDocument(str(source), suffix, ParseStatus.BLOCKED, sha256=digest, size_bytes=size,
        metadata={'source_file_sha256': digest, 'original_preserved': True, 'content_parsed': False,
                  'formal_usable': False, 'limits': asdict(limits), 'archive_ocr_enabled': archive_ocr,
                  'archive_entries': []})
    target = derived_directory(source, derived_root) / suffix / digest
    target.mkdir(parents=True, exist_ok=True)
    # Each attempt gets an immutable directory; repeat parsing cannot overwrite
    # prior member provenance or partial failed extraction evidence.
    attempt = Path(tempfile.mkdtemp(prefix='attempt_', dir=target))
    deadline = time.monotonic() + limits.timeout_seconds
    def check_time():
        if time.monotonic() > deadline:
            raise ArchiveRejected('ARCHIVE_TIMEOUT')
    try:
        if size > limits.max_archive_bytes:
            raise ArchiveRejected('ARCHIVE_INPUT_TOO_LARGE')
        names, total = set(), 0
        records = result.metadata['archive_entries']
        if suffix == 'zip':
            _zip_preflight(source, limits)
            with zipfile.ZipFile(source) as archive:
                validated = []
                for info in archive.infolist():
                    name, rawhex, encoding = _zip_name(info)
                    key = name.casefold()
                    if key in names:
                        raise ArchiveRejected('ARCHIVE_DUPLICATE_NAME')
                    # A file cannot also be an ancestor directory of another entry.
                    if any(key.startswith(existing + '/') and kind == 'file' for existing, kind in validated):
                        raise ArchiveRejected('ARCHIVE_FILE_DIRECTORY_COLLISION')
                    if not info.is_dir() and any(existing.startswith(key + '/') for existing, _ in validated):
                        raise ArchiveRejected('ARCHIVE_FILE_DIRECTORY_COLLISION')
                    names.add(key)
                    validated.append((key, 'dir' if info.is_dir() else 'file'))
                    mode = info.external_attr >> 16
                    if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                        raise ArchiveRejected('ARCHIVE_LINK_OR_SPECIAL_FILE')
                    if info.flag_bits & 1:
                        raise ArchiveRejected('ENCRYPTED_ARCHIVE_UNSUPPORTED')
                    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                        raise ArchiveRejected('UNSUPPORTED: ZIP compression method is outside the bounded decoder allowlist')
                    _budget(info.file_size, info.compress_size, total, limits)
                    total += info.file_size
                for ordinal, info in enumerate(archive.infolist(), 1):
                    check_time()
                    name, rawhex, encoding = _zip_name(info)
                    if info.is_dir():
                        records.append({'name': name, 'directory': True})
                        continue
                    raw = bytearray()
                    with archive.open(info, 'r') as member:
                        while True:
                            check_time()
                            chunk = member.read(min(65536, limits.max_member_bytes + 1 - len(raw)))
                            if not chunk:
                                break
                            raw.extend(chunk)
                            if len(raw) > limits.max_member_bytes or len(raw) > info.file_size:
                                raise ArchiveRejected('ARCHIVE_ACTUAL_SIZE_LIMIT')
                    if len(raw) != info.file_size:
                        raise ArchiveRejected('ARCHIVE_MEMBER_SIZE_MISMATCH')
                    rec, blocks = _register(bytes(raw), name, ordinal, attempt, limits, digest,
                        {'name_raw_hex': rawhex, 'name_encoding': encoding, 'compressed_bytes': info.compress_size}, archive_ocr, archive_ocr_cache)
                    records.append(rec)
                    result.blocks.extend(blocks)
            result.converter = 'python-stdlib-zipfile/' + '.'.join(map(str, sys.version_info[:3]))
        elif suffix == 'rar':
            tool = probe_rar_tool()
            result.metadata['engine'] = tool
            if not tool['available']:
                raise ArchiveRejected(tool['reason'])
            metadata = _rar_metadata(source, limits)
            if metadata.get('encrypted') or metadata.get('multivolume'):
                raise ArchiveRejected('UNSUPPORTED: encrypted or multi-volume RAR')
            entries = metadata['entries']
            if len(entries) > limits.max_members:
                raise ArchiveRejected('ARCHIVE_MEMBER_COUNT_LIMIT')
            for info in entries:
                name = safe_member_name(info['name'])
                if '[' in name or ']' in name:
                    raise ArchiveRejected('RAR_NAME_PATTERN_UNSUPPORTED')
                if name.casefold() in names:
                    raise ArchiveRejected('ARCHIVE_DUPLICATE_NAME')
                names.add(name.casefold())
                if info['link'] or info['special']:
                    raise ArchiveRejected('ARCHIVE_LINK_OR_SPECIAL_FILE')
                _budget(info['size'], info['compressed'], total, limits)
                total += info['size']
            for ordinal, info in enumerate(entries, 1):
                check_time()
                name = safe_member_name(info['name'])
                if info['directory']:
                    records.append({'name': name, 'directory': True})
                    continue
                # stdout extraction never passes an archive path to a filesystem
                # destination. --no-recursion forbids ancestor expansion.
                run = bounded_run([tool['executable'], '-xOf', str(source), '--no-recursion', '--', info['name']],
                    timeout=min(30, max(0.1, deadline - time.monotonic())), memory_bytes=limits.memory_bytes,
                    max_stdout=min(limits.max_member_bytes, info['size']) + 1)
                if run.returncode:
                    raise ArchiveRejected('RAR_EXTRACTION_FAILED: ' + run.stderr.decode('utf-8', 'replace')[:1000])
                if len(run.stdout) != info['size']:
                    raise ArchiveRejected('RAR_MEMBER_SIZE_MISMATCH')
                if info.get('crc32') is not None and zlib.crc32(run.stdout) != info['crc32']:
                    raise ArchiveRejected('RAR_CRC_MISMATCH')
                rec, blocks = _register(run.stdout, name, ordinal, attempt, limits, digest,
                    {'name_encoding': 'rarfile-header-decoder', 'compressed_bytes': info['compressed']}, archive_ocr, archive_ocr_cache)
                records.append(rec)
                result.blocks.extend(blocks)
            result.converter = tool['version']
        else:
            raise ArchiveRejected('UNSUPPORTED: archive format')
        files = [r for r in records if not r.get('directory')]
        parsed = [r for r in files if r['status'] == 'PARSED']
        result.metadata.update(extracted_files=len(files), parsed_files=len(parsed), total_uncompressed_bytes=total,
                               extraction_status='EXTRACTED', content_parsed=len(parsed) == len(files) and bool(files))
        result.status = ParseStatus.PARSED if result.metadata['content_parsed'] else ParseStatus.BLOCKED
        if result.status is ParseStatus.BLOCKED:
            result.blocked_reason = 'ARCHIVE_MEMBER_REVIEW_OR_PARSE_REQUIRED: extracted members have unresolved parsing'
    except (ValueError, OSError, UnicodeError, zipfile.BadZipFile, ProcessLimitError, NotImplementedError) as exc:
        result.status = ParseStatus.BLOCKED if str(exc).startswith('UNSUPPORTED:') else ParseStatus.FAILED
        result.blocked_reason = str(exc) if result.status is ParseStatus.BLOCKED else None
        result.error = str(exc) if result.status is ParseStatus.FAILED else None
        result.metadata.update(extraction_status='UNSUPPORTED' if result.status is ParseStatus.BLOCKED else 'REJECTED', content_parsed=False)
        result.blocks = []
    if sha256_file(source) != digest:
        result.status = ParseStatus.FAILED
        result.blocks = []
        result.error = 'SOURCE_HASH_CHANGED'
        result.metadata['original_preserved'] = False
    manifest = attempt / 'archive_manifest.json'
    manifest.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding='utf-8')
    result.metadata['manifest_path'] = str(manifest)
    return result
