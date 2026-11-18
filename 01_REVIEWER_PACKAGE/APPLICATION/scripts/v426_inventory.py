"""Read-only inventory and real ten-page human review preparation; no approvals."""
from pathlib import Path
import argparse, collections, datetime, hashlib, html, json, re, sqlite3, subprocess
from v426_verify import ROOT, OUT, PRIVATE, digest

def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding='utf-8')

def sqlite_inventory(path):
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.execute('PRAGMA query_only=ON')
        tables = {}
        for name, schema in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall():
            quoted = '"' + name.replace('"', '""') + '"'
            rows = conn.execute('SELECT * FROM ' + quoted).fetchall()
            row_hashes = sorted(hashlib.sha256(repr(row).encode('utf-8')).hexdigest() for row in rows)
            tables[name] = {'rows': len(rows), 'schema': schema, 'logical_sha256': hashlib.sha256('\n'.join(row_hashes).encode()).hexdigest()}
        return {'path': str(path), 'sha256': digest(path), 'integrity': [r[0] for r in conn.execute('PRAGMA integrity_check')], 'foreign_key_violations': conn.execute('PRAGMA foreign_key_check').fetchall(), 'tables': tables}

def inventory(phase):
    corpus = ROOT.parent / 'REAL_DATA_CORPUS/HN_L1_V1_20261116'
    originals = [{'path': p.relative_to(corpus).as_posix(), 'bytes': p.stat().st_size, 'sha256': digest(p)} for p in sorted(corpus.rglob('*')) if p.is_file()]
    cfg = json.loads((ROOT.parent / '_CODEX_V425/local-pilot/config.private.json').read_text(encoding='utf-8'))
    runtime = Path(cfg['TXBUDDY_RUNTIME_ROOT'])
    databases = [sqlite_inventory(p) for p in sorted(runtime.glob('*.sqlite3'))]
    databases.append(sqlite_inventory(ROOT / 'backend/data/txbuddy-dev.sqlite3'))
    result = {'generated_at': datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(), 'phase': phase, 'project_root': str(ROOT), 'corpus_root': str(corpus), 'originals': originals, 'corpus_files': len(originals), 'databases': databases, 'runtime_database_type': 'SQLite business/auth/governance plus PostgreSQL sample price repository', 'git_head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(), 'git_status': subprocess.check_output(['git', 'status', '--short'], cwd=ROOT, text=True, encoding='utf-8'), 'module_roots': {name: sorted(p.name for p in (ROOT / name).iterdir() if p.is_dir()) for name in ('backend/txbuddy', 'frontend/src')}, 'production_accepted': False}
    if phase == 'final':
        before = json.loads((OUT / 'BASELINE_DATA_INVENTORY.json').read_text(encoding='utf-8'))
        original = {(r['path'], r['sha256'], r['bytes']) for r in before['originals']}
        result['original_files_unchanged'] = original == {(r['path'], r['sha256'], r['bytes']) for r in originals}
        old = {r['path']: r for r in before['databases']}
        result['live_databases_logically_unchanged'] = all(d['tables'] == old[d['path']]['tables'] for d in databases)
        result['sqlite_integrity_passed'] = all(d['integrity'] == ['ok'] and not d['foreign_key_violations'] for d in databases)
    write(OUT / (phase.upper() + '_DATA_INVENTORY.json'), result)
    print(json.dumps({k: result[k] for k in ('phase', 'corpus_files', 'original_files_unchanged', 'live_databases_logically_unchanged', 'sqlite_integrity_passed') if k in result}))
    return result

def readiness():
    imported = json.loads((ROOT / 'reports/v425/OCR_WORKBENCH_IMPORT.json').read_text(encoding='utf-8'))
    sources = json.loads((ROOT / 'reports/v425/SOURCE_STATUS.json').read_text(encoding='utf-8'))['sources']
    cfg = json.loads((ROOT.parent / '_CODEX_V425/local-pilot/config.private.json').read_text(encoding='utf-8'))
    database = Path(cfg['TXBUDDY_RUNTIME_ROOT']) / 'governance.sqlite3'
    corpus = ROOT.parent / 'REAL_DATA_CORPUS/HN_L1_V1_20261116'
    chosen = ['ocr-5b8a5d066b784ec575ee96ab53322a1d', 'ocr-a5f3452d26d1b9afaf3e2e4284c6a54d']
    tasks, documents, parts = [], [], []
    with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as conn:
        conn.row_factory = sqlite3.Row
        event_count = conn.execute('SELECT count(*) FROM ocr_review_event').fetchone()[0]
        for docid in chosen:
            document = dict(conn.execute('SELECT * FROM ocr_document WHERE document_id=?', (docid,)).fetchone())
            original = corpus / document['source_path']
            provenance = [s for s in sources if s['local_path'] == document['source_path']]
            pages = conn.execute('SELECT * FROM ocr_page WHERE document_id=? ORDER BY page_number', (docid,)).fetchall()
            documents.append({'document_id': docid, 'source_path': document['source_path'], 'source_file_sha256': document['source_file_sha256'], 'actual_source_sha256': digest(original), 'source_hash_valid': digest(original) == document['source_file_sha256'], 'pages': len(pages), 'source_records': provenance})
            parts.append('<h2>' + html.escape(document['source_path']) + '</h2>')
            for page_row in pages:
                page = dict(page_row)
                tokens = json.loads(page['tokens_json'])
                task = {'task_id': 'V426-HUMAN-' + page['page_id'], 'document_id': docid, 'page_id': page['page_id'], 'page_number': page['page_number'], 'image_sha256': page['image_sha256'], 'image_hash_valid': digest(page['image_path']) == page['image_sha256'], 'source_file_sha256': document['source_file_sha256'], 'project_id': document['project_id'], 'low_confidence_tokens': sum(t['confidence'] < .85 for t in tokens), 'numeric_or_unit_tokens': [t for t in tokens if re.search(r'[0-9%％㎡³²元吨米]', t['text'])], 'original_text_sha256': hashlib.sha256(page['original_text'].encode('utf-8')).hexdigest(), 'assigned_reviewer': None, 'reviewed_at': None, 'review_decision': None, 'modifications': [], 'status': 'NEEDS_EXPERT_REVIEW', 'formal_usable': False}
                tasks.append(task)
                parts.append('<section><h3>第 ' + str(page['page_number']) + ' 页 · 待人工复核</h3><div class=pair><img src="' + html.escape(Path(page['image_path']).as_uri(), quote=True) + '"><pre>' + html.escape(page['original_text']) + '</pre></div><p>数字/单位候选 ' + str(len(task['numeric_or_unit_tokens'])) + '；低置信度文本块 ' + str(task['low_confidence_tokens']) + '。正式校对请在项目工作台保存草稿、提交并由有复核权限的人确认。</p></section>')
    private = PRIVATE / 'human-review'
    private.mkdir(parents=True, exist_ok=True)
    workbench = private / '173_2026_page_comparison.html'
    workbench.write_text('<!doctype html><meta charset=utf-8><title>天寻 Buddy 十页人工复核任务</title><style>body{font:16px sans-serif;margin:32px;background:#f5f7fa;color:#142338}.pair{display:grid;grid-template-columns:1fr 1fr;gap:24px}.pair img{width:100%;background:white}pre{white-space:pre-wrap;line-height:1.7;padding:20px;background:white}section{border-top:1px solid #ccd;padding:20px 0}h1,h2{line-height:1.7}</style><h1>173号与2026指数通知 · 10页待人工校对</h1><p>这是已有真实原件与OCR草稿的对照。AI标注不构成人工复核通过；没有填写任何人员、时间或复核决定。</p>' + ''.join(parts), encoding='utf-8')
    write(private / 'OCR_PAGE_TASKS.private.json', tasks)
    public_tasks = [{k: v for k, v in t.items() if k != 'numeric_or_unit_tokens'} for t in tasks]
    result = {'documents': documents, 'tasks': public_tasks, 'task_count': len(tasks), 'live_review_events': event_count, 'human_reviews_created': 0, 'professional_signoffs_created': 0, 'workbench': str(workbench), 'workbench_sha256': digest(workbench), 'task_detail_private_path': str(private / 'OCR_PAGE_TASKS.private.json'), 'workflow_status': 'READY_FOR_HUMAN_REVIEW', 'human_review_status': 'BLOCKED', 'blocking_reason': '尚无真实人员逐页核对、独立复核决定及专业资质核验，不能标记人工复核通过', 'production_accepted': False}
    write(OUT / 'OCR_HUMAN_REVIEW_READINESS.json', result)
    lines = ['# OCR 人工复核准备实测', '', f'真实原件2份、{len(tasks)}页，逐页原件和页图 SHA256 已验证。既有真实复核事件{event_count}；本轮创建人工复核0、专业签认0。', '', f'本机逐页对照：`{workbench}`。详细文本/数字候选保存在仓外私有任务JSON；公开报告只留元数据和哈希。', '', '| 原件 | 页数 | 原件SHA256 |', '|---|---:|---|']
    lines += [f"| {d['source_path']} | {d['pages']} | {d['actual_source_sha256']} |" for d in documents]
    lines += ['', '任务保留页码、页图、原文哈希、原始来源与URL/授权、低置信度候选统计。人员、时间、决定均为空，修改记录为空。真实人员必须在OCR工作台完成草稿修改说明、提交准确版本、由REVIEWER或OWNER批准或填写原因退回；修改内容后须重新提交复核。专业签认还须服务器已核验资质登记。', '', '重点检查：173号费用条文及适用日期、费率小数/百分号、指数通知人工/机械/管理费表格对应行列、单位、脚注和适用期次。AI候选统计没有证明具体识别错误或正确。', '', '**状态：工作流准备可用；真实人工复核 BLOCKED；正式计价适用性 BLOCKED。** 没有真实人员操作时不能升级。']
    (OUT / 'OCR_HUMAN_REVIEW_READINESS.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps({'task_count': len(tasks), 'human_reviews_created': 0, 'all_hashes_valid': all(d['source_hash_valid'] for d in documents) and all(t['image_hash_valid'] for t in tasks), 'workbench': str(workbench)}, ensure_ascii=False))
    return result

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=('baseline', 'final', 'readiness'), required=True)
    phase = parser.parse_args().phase
    readiness() if phase == 'readiness' else inventory(phase)
