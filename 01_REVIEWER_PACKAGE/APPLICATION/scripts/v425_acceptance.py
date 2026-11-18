"""Independent acceptance over real artifacts and invariants; does not create professional approval."""
from pathlib import Path
import argparse,hashlib,json,sqlite3,xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'reports/v425'

def read(name):return json.loads((OUT/name).read_text(encoding='utf-8'))
def need(condition,message):
    if not condition:raise AssertionError(message)
def junit(path):
    tree=ET.parse(path).getroot();suites=list(tree.iter('testsuite'))
    return {k:sum(int(s.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}

def check(task):
    if task=='baseline':
        r=read('baseline/REGRESSION_RESULT.json');need(r['exit_code']==0 and r['original_development_db_unchanged'] and r['legacy_assertions_preserved'],'V424基线未通过')
        n=junit(Path(r['log']).with_suffix('.xml'));need(n=={'tests':869,'failures':0,'errors':0,'skipped':0},'V424基线应为869/0/0')
        t=read('TAKEOVER.json');need(t['files']==11465 and t['hash_mismatches']==0,'完整快照无有效证据')
    elif task=='ocr':
        r=read('OCR_WORKBENCH_IMPORT.json');need(r['files']==40 and r['pages']==366 and r['human_reviews_created']==0,'原40份OCR未守恒')
        integrity=read('DATA_INTEGRITY.json');need(integrity['review_events']==0,'自动化不应修改真实复核状态')
    elif task=='sources':
        r=read('SOURCE_STATUS.json');s=r['sources'];need(len(s)==220 and len({x['source_id'] for x in s})==220,'来源项数或主键不完整')
        need(all(x['hash_match'] and x['sha256']==x['original_sha256'] for x in s),'来源SHA未保留')
        unresolved=[x for x in s if not x.get('source_url')];need({x['source_id'] for x in unresolved}=={'SRC-0132','SRC-0137','SRC-0220'},'派生缺口状态变化需重审')
        need(all(x['verification_status']=='INCOMPLETE' and x['parent_sources'] for x in unresolved),'派生件伪造完成或丢失父来源')
        need(next(x for x in s if x['source_id']=='SRC-0155')['verification_status']=='OFFICIAL_BYTES_VERIFIED','官方字节验证未落盘')
        need(r['summary']['all_sources_verified'] is False,'不能把220项审计当全量核验通过')
    elif task=='parsers':
        r=read('PARSING_RESULTS_NATIVE.json');o=read('PARSING_RESULTS_OCR.json')
        need(r['files']==o['files']==14 and r['original_hash_mismatches']==o['original_hash_mismatches']==[],'14原件解析清单或哈希不全')
        need(r['professional_review_performed'] is False and o['professional_review_performed'] is False,'解析不能自动专业复核')
        records=r['records'];docs=[x for x in records if str(x.get('format','')).lower()=='doc']
        # The precise source failures are part of the acceptance, not hidden to inflate coverage.
        need(len(docs)==9 and sum(x.get('status')=='PARSED' for x in docs)==7,'DOC真实覆盖应为7成功、2失败')
        need(not o['production_accepted'],'OCR草稿不得成为生产依据')
    elif task=='matrix':
        r=read('PROJECT_READINESS.json');need(len(r['projects'])==18 and all(x['l1_quantity_status']=='BLOCKED' for x in r['projects']),'项目矩阵数量或算量边界错误')
        from openpyxl import load_workbook
        path=ROOT/'REAL_PROJECT_READINESS_MATRIX_V425.xlsx';wb=load_workbook(path,data_only=True,read_only=False)
        need(len(wb.worksheets)==2,'矩阵表结构缺失')
        need(all(ws.freeze_panes and (ws.auto_filter.ref or any(table.autoFilter is not None and table.autoFilter.ref==table.ref for table in ws.tables.values())) for ws in wb.worksheets),'矩阵缺冻结或原生表筛选')
        wb.close();a=read('CATALOG_ACCEPTANCE.json');need(a['status']=='PASS','矩阵视觉/数据验收未通过')
    elif task=='database':
        r=read('DATABASE_POC_RESULT.json');need(r['migration_poc_passed'] and all(v['passed'] for v in r['checks'].values()),'PoC检查未全部通过')
        need(not r['production_switch_ready'] and not r['live_connection_changed'] and not r['production_accepted'],'不能切换既有环境')
        need('v425_test' in r['database'],'PoC必须是独立测试库')
    elif task=='integrity':
        r=read('DATA_INTEGRITY.json');need(r['passed'] and all(r['checks'].values()),'真实数据完整性失败')
        need(r['raw_price_total']==10989 and r['corrections']==3 and r['reference_boq_rows']==148,'样本数量不守恒')
        need(not r['independent_goldenset'] and r['accuracy_E']=='BLOCKED','参考清单不得成为独立基准')
    elif task=='browser':
        r=read('playwright-results.json');s=r['stats'];need(s['expected']>=3 and s['unexpected']==0 and s['skipped']==0 and s.get('flaky',0)==0,'浏览器回归未全部通过')
        need(read('GOVERNANCE_E2E_EVIDENCE.json')['all_ten_scenarios'],'十场景证据缺失')
        need('built in' in (OUT/'FRONTEND_BUILD.log').read_text(encoding='utf-8'),'前端未构建')
        need(len(list((OUT/'screenshots').glob('*.png')))>=5,'截图证据不足')
    elif task=='regression':
        r=read('REGRESSION_RESULT.json');need(r['exit_code']==0 and r['tests']>=978 and not any(r[k] for k in ('failures','errors','skipped')),'全量测试失败或被跳过')
        need(r['all_existing_test_files_preserved'] and r['original_development_db_unchanged'] and r['source_code_unchanged_during_regression'],'旧成果或回归源发生变化')
    else:raise ValueError(task)
    return {'check':task,'accepted':True,'production_accepted':False}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--task',required=True);a=p.parse_args();print(json.dumps(check(a.task),ensure_ascii=False))
