"""Read-only legacy DOC extraction using an installed, identified offline tool."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import tempfile

from .base import Locator, ParsedDocument, ParseStatus, TextBlock, source_facts, sha256_file
from .process_limits import bounded_run, ProcessLimitError

DOC_MAX_BYTES = 16 * 1024 * 1024
DOC_MAX_TEXT_BYTES = 16 * 1024 * 1024


def probe_doc_converter() -> dict:
    candidates = []
    if os.environ.get('TXBUDDY_ANTIWORD'):
        candidates.append(Path(os.environ['TXBUDDY_ANTIWORD']))
    found = shutil.which('antiword')
    if found:
        candidates.append(Path(found))
    if os.name == 'nt':
        for base in (os.environ.get('ProgramFiles', r'C:\Program Files'),
                     os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')):
            candidates.append(Path(base) / 'Git/mingw64/bin/antiword.exe')
    attempts = []
    for executable in dict.fromkeys(candidates):
        if not executable.is_file():
            continue
        try:
            p = bounded_run([str(executable), '-h'], timeout=5, max_stdout=16384, max_stderr=16384)
            help_text = (p.stdout + p.stderr).decode('utf-8', errors='replace')
            version = re.search(r'Version:\s*([^\r\n]+)', help_text)
            licensed = 'GNU General Public License' in help_text
            if version and licensed:
                mapping = executable.parent.parent / 'share/antiword'
                env_mapping = os.environ.get('ANTIWORDHOME')
                if env_mapping:
                    mapping = Path(env_mapping)
                return {'available': True, 'name': 'antiword', 'version': version.group(1).strip(),
                        'executable': str(executable.resolve()), 'executable_sha256': sha256_file(executable),
                        'license': 'GNU GPL (engine self-identification)',
                        'mapping_directory': str(mapping) if mapping.is_dir() else None,
                        'offline': True, 'attempts': attempts}
            attempts.append({'path': str(executable), 'reason': 'VERSION_OR_LICENSE_NOT_IDENTIFIED'})
        except (OSError, ProcessLimitError) as exc:
            attempts.append({'path': str(executable), 'reason': str(exc)})
    return {'available': False, 'reason': 'DOC_LEGACY_CONVERTER_REQUIRED: no identified offline antiword',
            'attempts': attempts, 'word_com_enabled': False,
            'word_reason': 'Installed Word is not used without verified conversion licensing'}


def derived_directory(source: Path, requested=None) -> Path:
    if requested is None:
        requested = os.environ.get('TXBUDDY_DERIVED_ROOT')
    if requested is None:
        requested = Path(os.environ.get('TXBUDDY_RUNTIME_ROOT', tempfile.gettempdir())) / 'derived'
    root = Path(requested).resolve()
    source = source.resolve()
    corpus = os.environ.get('TXBUDDY_CORPUS_ROOT')
    if root == source.parent or root == source or source in root.parents or root in source.parents:
        raise ValueError('DERIVED_DIRECTORY_INVALID: derived files must be separate from source')
    if corpus and (root == Path(corpus).resolve() or Path(corpus).resolve() in root.parents):
        raise ValueError('DERIVED_DIRECTORY_INVALID: never write inside the corpus')
    root.mkdir(parents=True, exist_ok=True)
    return root


def _record_attempt(root, result):
    from uuid import uuid4
    target = root / ('conversion_' + uuid4().hex + '.json')
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return str(target)


def parse_doc(path: str | Path, *, derived_root=None, doc_timeout: float = 30,
              doc_memory_bytes: int = 256 * 1024 * 1024, **_kwargs) -> ParsedDocument:
    source = Path(path).resolve()
    digest, size = source_facts(source)
    result = ParsedDocument(str(source), 'doc', ParseStatus.BLOCKED, sha256=digest, size_bytes=size,
        metadata={'source_file_sha256': digest, 'original_preserved': True, 'formal_usable': False,
                  'limits': {'timeout_seconds': doc_timeout, 'memory_bytes': doc_memory_bytes,
                             'max_input_bytes': DOC_MAX_BYTES, 'max_output_bytes': DOC_MAX_TEXT_BYTES}})
    root = derived_directory(source, derived_root) / 'doc' / digest
    root.mkdir(parents=True, exist_ok=True)
    probe = probe_doc_converter()
    result.metadata['engine'] = probe
    result.converter = ('antiword/' + probe['version']) if probe['available'] else None
    try:
        if size > DOC_MAX_BYTES:
            raise ProcessLimitError('DOC_INPUT_TOO_LARGE')
        with source.open('rb') as handle:
            header = handle.read(512)
            if header[:8] != bytes.fromhex('d0cf11e0a1b11ae1'):
                raise ValueError('DOC_MAGIC_INVALID: expected legacy OLE Compound File')
        if len(header) >= 32:
            shift = struct.unpack_from('<H', header, 30)[0]
            if shift in (9, 12):
                sector = 1 << shift
                result.metadata['cfb_structure'] = {'sector_size': sector, 'sector_aligned': size % sector == 0}
                if size % sector:
                    result.metadata['cfb_structure']['warning'] = 'Source size is not sector aligned; incomplete or nonstandard CFB is possible. No repair was attempted.'
        if not probe['available']:
            result.blocked_reason = probe['reason']
        else:
            env = os.environ.copy()
            if probe['mapping_directory']:
                env['ANTIWORDHOME'] = probe['mapping_directory']
            # The converter only receives an independently hashed read-only
            # derived copy. It never receives the original corpus file path.
            conversion_input = root / (digest + '.doc')
            if not conversion_input.exists():
                with source.open('rb') as incoming, conversion_input.open('xb') as copied:
                    shutil.copyfileobj(incoming, copied, 1024 * 1024)
                conversion_input.chmod(stat.S_IREAD)
            if sha256_file(conversion_input) != digest:
                raise ValueError('CONVERSION_INPUT_HASH_MISMATCH')
            result.metadata['conversion_input'] = str(conversion_input)
            result.metadata['conversion_input_read_only'] = not bool(conversion_input.stat().st_mode & stat.S_IWRITE)
            run = bounded_run([probe['executable'], '-m', 'UTF-8.txt', '-w', '0', str(conversion_input)],
                timeout=min(doc_timeout, 120), memory_bytes=doc_memory_bytes,
                cpu_seconds=min(doc_timeout, 60), max_stdout=DOC_MAX_TEXT_BYTES, env=env)
            if run.returncode != 0:
                raise ValueError('DOC_CONVERSION_FAILED: exit ' + str(run.returncode) + ': ' +
                                 run.stderr.decode('utf-8', 'replace')[:1500])
            text = run.stdout.decode('utf-8', errors='strict')
            if not text.strip():
                raise ValueError('DOC_EMPTY: converter returned no text')
            output_hash = hashlib.sha256(run.stdout).hexdigest()
            output = root / (output_hash + '.txt')
            if not output.exists():
                with output.open('xb') as handle:
                    handle.write(run.stdout)
            elif sha256_file(output) != output_hash:
                raise ValueError('DERIVED_HASH_MISMATCH')
            result.blocks = [TextBlock(line, Locator(line=n), 'converted_text')
                             for n, line in enumerate(text.splitlines(), 1) if line.strip()]
            result.status = ParseStatus.PARSED
            result.metadata.update(converted_file=str(output), converted_sha256=output_hash,
                encoding='utf-8', converted_bytes=len(run.stdout), content_parsed=True,
                locator_scope='derived text line; original pagination/layout not preserved',
                conversion_warning=run.stderr.decode('utf-8', 'replace')[:1500])
            result.warnings.append('Legacy DOC extracted as text; tables/pagination require human checking')
    except (ValueError, OSError, ProcessLimitError, UnicodeError) as exc:
        result.status = ParseStatus.FAILED
        result.error = str(exc)
    if sha256_file(source) != digest:
        result.status = ParseStatus.FAILED
        result.blocks = []
        result.error = 'SOURCE_HASH_CHANGED: original no longer matches input'
        result.metadata['original_preserved'] = False
    result.metadata['attempt_record'] = _record_attempt(root, result.to_dict())
    return result
