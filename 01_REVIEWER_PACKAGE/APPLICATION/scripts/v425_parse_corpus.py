"""Audit all legacy DOC/ZIP/RAR inputs without writing into the real corpus."""
from __future__ import annotations

import argparse
from collections import Counter
import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import shutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from txbuddy.parsing import parse_file
from txbuddy.parsing.archive_adapter import ArchiveLimits, probe_rar_tool
from txbuddy.parsing.doc_adapter import probe_doc_converter


def sha256(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def compact_member(entry):
    result = {key: value for key, value in entry.items() if key != 'parse_result'}
    parsed = entry.get('parse_result')
    if parsed:
        metadata = parsed.get('metadata', {})
        pages = metadata.get('pages')
        result['parse_summary'] = {'status': parsed['status'], 'format': parsed['format'],
            'blocks': parsed['block_count'], 'error': parsed.get('error'),
            'blocked_reason': parsed.get('blocked_reason'), 'pages': len(pages) if isinstance(pages, list) else pages,
            'empty_pages': metadata.get('empty_pages'), 'has_text_layer': metadata.get('has_text_layer'),
            'engine': metadata.get('engine'), 'review_status': metadata.get('review_status'),
            'formal_usable': False}
    return result


def run(corpus: Path, out: Path, derived: Path, ocr_archives=False, ocr_cache=None):
    corpus = corpus.resolve()
    derived = derived.resolve()
    if derived == corpus or corpus in derived.parents:
        raise ValueError('Derived output cannot be inside the corpus')
    os.environ['TXBUDDY_CORPUS_ROOT'] = str(corpus)
    paths = sorted((p for p in corpus.rglob('*') if p.is_file() and p.suffix.lower() in ('.doc', '.zip', '.rar')),
                   key=lambda p: p.relative_to(corpus).as_posix())
    before = {p: sha256(p) for p in paths}
    records = []
    for path in paths:
        parsed = parse_file(path, derived_root=derived, archive_ocr=ocr_archives, archive_ocr_cache=ocr_cache)
        metadata = parsed.metadata
        row = {'path': path.relative_to(corpus).as_posix(), 'sha256': before[path],
            'source_file_sha256': parsed.sha256, 'format': parsed.format,
            'size_bytes': path.stat().st_size, 'status': parsed.status.value,
            'blocks': parsed.block_count(), 'error': parsed.error, 'blocked_reason': parsed.blocked_reason,
            'converter': parsed.converter, 'original_preserved': metadata.get('original_preserved'),
            'formal_usable': False, 'derived_manifest': metadata.get('manifest_path') or metadata.get('attempt_record')}
        if parsed.format == 'doc':
            row.update(engine=metadata.get('engine'), converted_file=metadata.get('converted_file'),
                       converted_sha256=metadata.get('converted_sha256'), locator_scope=metadata.get('locator_scope'),
                       conversion_input_read_only=metadata.get('conversion_input_read_only'), cfb_structure=metadata.get('cfb_structure'))
        else:
            row.update(extraction_status=metadata.get('extraction_status'), content_parsed=metadata.get('content_parsed'),
                extracted_files=metadata.get('extracted_files', 0), parsed_files=metadata.get('parsed_files', 0),
                members=[compact_member(item) for item in metadata.get('archive_entries', [])])
        records.append(row)
        print(json.dumps({'path': row['path'], 'status': row['status'], 'blocks': row['blocks']}, ensure_ascii=False), flush=True)
    mismatches = [p.relative_to(corpus).as_posix() for p in paths if sha256(p) != before[p]]
    summary = {ext: dict(Counter(row['status'] for row in records if row['format'] == ext)) for ext in ('doc', 'zip', 'rar')}
    result = {'version': 'V4.2.5', 'generated_at': datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),
        'corpus': str(corpus), 'derived_root': str(derived), 'ocr_archives_explicitly_enabled': ocr_archives,
        'production_accepted': False, 'professional_review_performed': False,
        'doc_engine': probe_doc_converter(), 'rar_tool': probe_rar_tool(),
        'archive_limits': ArchiveLimits().__dict__, 'files': len(records), 'format_summary': summary,
        'original_hash_mismatches': mismatches, 'records': records}
    out.mkdir(parents=True, exist_ok=True)
    suffix = '_OCR' if ocr_archives else '_NATIVE'
    report = out / ('PARSING_RESULTS' + suffix + '.json')
    markdown = out / ('PARSING_REPORT' + suffix + '.md')
    if report.exists() or markdown.exists():
        history = out / 'history' / datetime.datetime.now().strftime('%Y%m%dT%H%M%S%f')
        history.mkdir(parents=True)
        for previous in (report, markdown):
            if previous.exists():
                shutil.copy2(previous, history / previous.name)
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# V4.2.5 离线文件解析验收', '',
        f'模式：{"显式启用归档扫描件 OCR 草稿" if ocr_archives else "原生文本解析（不自动 OCR）"}。生产验收 False；未执行专业签认。', '',
        f'覆盖 {len(records)} 个原件，原件 SHA256 不一致 {len(mismatches)}；摘要 `{json.dumps(summary, ensure_ascii=False)}`。', '',
        'DOC 使用本机 Git for Windows 随附 Antiword 0.37（工具自述 GNU GPL）；原件只读，UTF-8 派生文本、工具 SHA256、版本和每次转换记录独立留存。表格布局和原始分页不保真，定位为派生文本行号，须人工核对。', '',
        'RAR 仅在核验系统 bsdtar 的 Microsoft Windows 有效签名、libarchive 版本和 BSD 许可后启用；缺少工具或 rarfile 元数据依赖时明确 UNSUPPORTED。加密与多卷 RAR 不处理，不尝试破解或密码恢复。', '',
        'ZIP/RAR 禁止路径穿越、绝对路径、设备名、ADS、大小写/Unicode 重名、符号链接和特殊文件；限制原件/成员/总字节数、目录条目、压缩比、超时、进程内存、CPU与输出。嵌套压缩独立登记为 BLOCKED；每个提取文件均独立登记哈希、格式和解析状态。', '',
        '默认解析不运行 OCR。显式 OCR 只产生 NEEDS_EXPERT_REVIEW 草稿，不能成为正式费率或替代真实专业人员复核。', '',
        '| 文件 | 状态 | 文本块 | 原件SHA256 |', '|---|---|---:|---|']
    for row in records:
        lines.append(f"| {row['path']} | {row['status']} | {row['blocks']} | {row['sha256']} |")
        for member in row.get('members', []):
            if not member.get('directory'):
                lines.append(f"| ↳ {member['name']} | {member['status']} | {member.get('parse_summary', {}).get('blocks', 0)} | {member['sha256']} |")
    lines.extend(['', '归档提取成功与正文解析成功分别统计；扫描 PDF 在默认模式下仍需 OCR。真实资料的来源、授权和适用性另按治理报告核验。'])
    markdown.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps({'report': str(report), 'summary': summary, 'hash_mismatches': len(mismatches)}, ensure_ascii=False), flush=True)
    return 0 if not mismatches else 2


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--corpus', type=Path, default=ROOT.parent / 'REAL_DATA_CORPUS/HN_L1_V1_20261116')
    parser.add_argument('--out', type=Path, default=ROOT / 'reports/v425')
    parser.add_argument('--derived', type=Path, default=ROOT.parent / '_CODEX_V425/derived')
    parser.add_argument('--ocr-archives', action='store_true')
    parser.add_argument('--ocr-cache', type=Path, default=ROOT / 'reports/v424/ocr')
    args = parser.parse_args()
    raise SystemExit(run(args.corpus, args.out, args.derived, args.ocr_archives, args.ocr_cache if args.ocr_archives else None))
