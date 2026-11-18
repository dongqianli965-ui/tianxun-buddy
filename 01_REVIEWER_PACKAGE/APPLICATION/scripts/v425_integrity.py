"""Read-only controls over originals, historic tests, sample prices and reference BOQ."""
from pathlib import Path
import hashlib,json,os,sqlite3
from collections import Counter

ROOT=Path(__file__).resolve().parents[1]
PRIVATE=ROOT.parent/'_CODEX_V425'

def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def run():
    import psycopg2
    takeover=json.loads((PRIVATE/'ACTIVE_TAKEOVER.json').read_text(encoding='utf-8'))
    snapshot=Path(takeover['snapshot'])
    corpus=ROOT.parent/'REAL_DATA_CORPUS/HN_L1_V1_20261116'
    raw=json.loads((snapshot/'CORPUS_SHA256.json').read_text(encoding='utf-8'))
    bad=[r['path'] for r in raw if not (corpus/r['path']).is_file() or sha(corpus/r['path'])!=r['sha256']]
    actual={p.relative_to(corpus).as_posix() for p in corpus.rglob('*') if p.is_file()}
    originals=json.loads((snapshot/'WORKTREE_SHA256.json').read_text(encoding='utf-8'))
    tests=[r for r in originals if r['path'].startswith('backend/tests/') and r['path'].endswith('.py')]
    changed_tests=[r['path'] for r in tests if not (ROOT/r['path']).is_file() or sha(ROOT/r['path'])!=r['sha256']]
    dev=[r for r in originals if r['path']=='backend/data/txbuddy-dev.sqlite3']
    cfg=json.loads((PRIVATE/'local-pilot/config.private.json').read_text(encoding='utf-8'))
    conn=psycopg2.connect(cfg['TXBUDDY_SAMPLE_DSN']);conn.set_session(readonly=True)
    with conn:
        with conn.cursor() as c:
            c.execute('SELECT period,count(*) FROM material_price GROUP BY period ORDER BY period');counts=dict(c.fetchall())
            c.execute('SELECT count(*) FROM material_price_correction');corrections=c.fetchone()[0]
            c.execute('SELECT record_id,price_incl_tax,price_excl_tax FROM material_price_correction ORDER BY record_id');errata=c.fetchall()
            c.execute('SELECT count(*) FROM material_price WHERE region<>%s',('410100-INNER-4RING',));outside=c.fetchone()[0]
            formal={}
            for table in ('quota_item','fee_rate_matrix','basis_pack'):
                c.execute('SELECT count(*) FROM '+table);formal[table]=c.fetchone()[0]
    conn.close()
    runtime=Path(cfg['TXBUDDY_RUNTIME_ROOT'])
    with sqlite3.connect((runtime/'business.sqlite3').resolve().as_uri()+'?mode=ro',uri=True) as c:
        payloads=[json.loads(r[0]) for r in c.execute("SELECT payload FROM job_record WHERE kind='sample_boq'")]
    items=[i for p in payloads for i in p.get('items',[])]
    unique={(i['file_sha256'],i['sheet'],i['row']):i for i in items}
    locators=all(i.get('cells') and i.get('sheet') and i.get('row') and i['data_role']=='B_REFERENCE' and i.get('independently_calculated') is False for i in unique.values())
    source_hashes=[]
    for suffix in ('土建_工程量清单.xlsx','安装_工程量清单.xlsx'):
        p=next((corpus/'05_REAL_PROJECTS/L1_BUDGET/L1_001/B_REFERENCE').glob('*'+suffix));source_hashes.append(sha(p))
    from txbuddy.governance.ocr_review import ReviewStore
    store=ReviewStore(runtime/'governance.sqlite3');docs=store.documents()
    with store.connect() as c:
        events=c.execute('SELECT count(*) FROM ocr_review_event').fetchone()[0]
        pages=c.execute('SELECT count(*) FROM ocr_page').fetchone()[0]
    checks={'corpus_files_unchanged':not bad and actual=={r['path'] for r in raw},
      'all_existing_test_files_byte_preserved':not changed_tests,
      'development_sqlite_unchanged':all(sha(ROOT/r['path'])==r['sha256'] for r in dev),
      'original_prices_preserved':counts=={'2026-Q2':5026,'2026-Q3':5024,'2026-09':939},
      'three_errata_preserved':corrections==3,'region_restriction_preserved':outside==0,
      'no_formal_quota_fee_or_basis_data':all(n==0 for n in formal.values()),
      'reference_boq_148_rows_with_locators':len(unique)==148 and locators and {i['file_sha256'] for i in unique.values()}==set(source_hashes),
      'forty_ocr_drafts_without_automated_reviews':len(docs)==40 and pages==366 and events==0 and all(d['review_status']=='NEEDS_EXPERT_REVIEW' for d in docs)}
    result={'checks':checks,'passed':all(checks.values()),'corpus_files':len(raw),'corpus_mismatches':bad,
      'old_test_files':len(tests),'changed_old_tests':changed_tests,'price_counts':counts,'raw_price_total':sum(counts.values()),
      'errata':errata,'corrections':corrections,'formal_tables':formal,'reference_boq_rows':len(unique),'boq_data_role':'B_REFERENCE',
      'independent_goldenset':False,'ocr_files':len(docs),'ocr_pages':pages,'review_events':events,
      'production_accepted':False,'accuracy_E':'BLOCKED'}
    out=ROOT/'reports/v425/DATA_INTEGRITY.json';out.write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    (ROOT/'DATA_INTEGRITY_REPORT_V425.md').write_text('# V4.2.5 数据完整性报告\n\n'+
      '\n'.join('- '+k+'：'+('PASS' if v else 'FAIL') for k,v in checks.items())+
      f'\n\n原仓{len(raw)}份全部字节核对；旧测试{len(tests)}个文件逐字节保留。价格原始10989行，勘误3条；148行参考清单保持B_REFERENCE及文件/表/行/单元格定位。40份366页OCR，真实人工复核记录0。\n\n数据操作接口的导入、查询、结构化、导出与地区/缺期拒绝另由独立Worker和全量回归执行。完整性通过不构成造价精度或专业签认。生产验收False；E级BLOCKED。\n',encoding='utf-8')
    print(json.dumps({'passed':result['passed'],'checks':checks},ensure_ascii=False));return 0 if result['passed'] else 1

if __name__=='__main__':raise SystemExit(run())
