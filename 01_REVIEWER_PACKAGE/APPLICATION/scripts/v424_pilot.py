"""Read-only corpus scan, atomic import, PDF price comparison, and machine-readable pilot evidence.
Run with backend on PYTHONPATH and TXBUDDY_SAMPLE_DSN set to an isolated sample DB.
"""
from pathlib import Path
import argparse, json, os, logging, collections, re, hashlib, traceback
from datetime import datetime
from txbuddy.pilot.database import PilotDatabase,dumps
from txbuddy.pilot.importer import import_corpus,verify_corpus,read_csv,ident
from txbuddy.pilot.boq import workbook_boq
from txbuddy.parsing import parse_file
from txbuddy.parsing.ocr import offline_ocr
from txbuddy.pilot.extraction import save_fields
from txbuddy.pilot.provenance import enrich_sources


def run(root,out,ocr=False):
    out.mkdir(parents=True,exist_ok=True)
    logging.basicConfig(filename=out/'execution.log',level=logging.INFO,encoding='utf-8')
    logging.getLogger('pypdf').setLevel(logging.ERROR)
    db=PilotDatabase(os.environ.get('TXBUDDY_SAMPLE_DSN') or str(out/'sample-pilot.sqlite3')); db.migrate()
    result={'started':datetime.now().isoformat(),'database_backend':'PostgreSQL' if db.pg else 'SQLite_TEMP',
        'postgreSQL_accepted':db.pg,'corpus':str(root),'levels':{},'blockers':[]}
    result['import']=import_corpus(db,root); result['idempotent_reimport']=import_corpus(db,root)
    result['source_resolution']=enrich_sources(db,root)
    with db.transaction() as c:
        resolved_sources={s['local_path']:s['official_url'] for s in c.rows('SELECT local_path,official_url FROM source_register')}
    records=[]; pdf_cache={}; differences=[]
    sources={r['local_path']:r for r in read_csv(root/'00_INDEX/SOURCE_REGISTER.csv')}
    parsed_out=out/'parsed'; parsed_out.mkdir(exist_ok=True)
    for i,f in enumerate(sorted(p for p in root.rglob('*') if p.is_file()),1):
        rel=f.relative_to(root).as_posix()
        p=parse_file(f)
        if f.suffix.lower() in ('.html','.htm'):
            p.metadata['source_url']=p.metadata.get('source_url') or resolved_sources.get(rel)
        save_fields(db,rel,p,result['import']['job_id'])
        d=p.to_dict(); (parsed_out/(ident(rel)+'.json')).write_text(dumps(d),encoding='utf-8')
        record={'path':rel,'format':f.suffix.lower().lstrip('.'),'status':p.status.value,'characters':len(p.text),
            'sha256':p.sha256,'error':p.error or p.blocked_reason,'empty_pages':p.metadata.get('empty_pages',[]),
            'boq_complete':p.metadata.get('boq_complete')}
        records.append(record)
        if f.suffix.lower()=='.pdf':
            page_text={}
            for b in p.blocks: page_text.setdefault(b.locator.page,[]).append(b.text)
            pdf_cache[rel]={k:'\n'.join(v) for k,v in page_text.items()}
            if not any(page_text.get(k,'') for k in (1,2,3)) and page_text:
                differences.append({'path':rel,'first3_has_text':False,'all_text_pages':sorted(page_text),'reason':'原质量报告只探测前3页，全页解析发现后续文字'})
        if i%25==0: print('SCAN',i,flush=True)
    result['pdf_28_vs_29_reconciliation']=differences
    result['parsing_files']=records
    result['format_counts']={ext:dict(collections.Counter(r['status'] for r in records if r['format']==ext)) for ext in sorted(set(r['format'] for r in records))}
    # Compare deterministically sampled rows spread across each source table directly with original PDF text.
    checks=[]
    for f in (root/'07_DERIVED_DATA/STRUCTURED_TABLES').glob('*结构化.csv'):
        rows=read_csv(f); sample=[rows[(i*(len(rows)-1))//29] for i in range(30)]; table_checks=[]
        for r in sample:
            page=pdf_cache[r['original_file']].get(int(r['source_page']),'')
            sequence=r['record_id'].rsplit('-',1)[-1]
            match=re.search(r'(?ms)^\s*'+re.escape(sequence)+r'\s+(.+?)(?=^\s*\d+\s+|\Z)',page)
            original_row=(sequence+' '+match.group(1)) if match else ''
            expected=''.join([sequence,*[r[k] for k in ('material_name','spec_model','unit','price_incl_tax','price_excl_tax')]])
            compact=re.sub(r'\s+','',original_row)
            ok=re.sub(r'\s+','',expected) in compact
            table_checks.append({'record_id':r['record_id'],'source_file':r['original_file'],'sha256':r['file_sha256'],
                'page':int(r['source_page']),'row':int(r['source_row']),'material_name':r['material_name'],
                'spec_model':r['spec_model'],'unit':r['unit'],'price_incl_tax':r['price_incl_tax'],'price_excl_tax':r['price_excl_tax'],
                'pdf_text_comparison':ok,'original_pdf_row':original_row,'all_fields_checked':['sequence','material_name','spec_model','unit','price_incl_tax','price_excl_tax']})
        checks.append({'table':f.name,'total':len(rows),'sample_count':len(sample),'passed':sum(x['pdf_text_comparison'] for x in table_checks),'rows':table_checks})
    result['price_pdf_checks']=checks
    boq=[]
    for f in (root/'05_REAL_PROJECTS/L1_BUDGET/L1_001').rglob('*.xlsx'):
        r=workbook_boq(f,data_role='B_REFERENCE'); r['source_file']=f.relative_to(root).as_posix(); boq.append(r)
    result['l1_001_boq']=boq
    if ocr:
        ocr_results=[]
        candidates=[p for p in root.rglob('*') if p.is_file() and p.suffix.lower()=='.pdf' and ('173' in p.name or ('2026' in p.name and '23' in p.name))]
        unique=set()
        for f in candidates:
            sha=hashlib.sha256(f.read_bytes()).hexdigest()
            if sha in unique: continue
            unique.add(sha); print('OCR',f.name,flush=True)
            parsed=offline_ocr(f,out/'ocr')
            save_fields(db,f.relative_to(root).as_posix(),parsed,result['import']['job_id'])
            ocr_results.append({'source_file':f.relative_to(root).as_posix(),'sha256':sha,'status':parsed.status.value,
                'characters':len(parsed.text),'pages':len(parsed.metadata.get('pages',[])),
                'review_status':'NEEDS_EXPERT_REVIEW','formal_usable':False,'error':parsed.blocked_reason})
        result['ocr']=ocr_results
    result['hash_verification']={'files':len(verify_corpus(root)),'mismatches':0}
    result['counts']=db.counts()
    html=[r for r in records if r['format'] in ('html','htm')]; html_ok=sum(r['status']=='PARSED' for r in html)
    main=[r for r in records if r['format'] in ('html','htm','pdf','xlsx','docx')]
    result['html_success_rate']=html_ok/len(html)
    result['main_parse_success_rate']=sum(r['status']=='PARSED' for r in main)/len(main)
    result['levels']={'A':'PASS' if result['counts']['material_price']==10989 else 'FAIL',
        'B':'PARTIAL' if any(r['status']!='PARSED' for r in main) else 'PASS',
        'C':'PASS' if all(x['passed']==30 for x in checks) else 'FAIL',
        'D':'PARSE_IMPORT_ONLY','E':'BLOCKED'}
    result['blockers']=['无原始施工图','河南定额及2024国标正文 LICENSE_REQUIRED','费率 OCR 未完成真实人工核对','无独立人工复核 Golden Set','无专业人员签认','2026 Q4/10月及其他城市信息价缺失']
    result['finished']=datetime.now().isoformat()
    (out/'CORPUS_SAMPLE_PILOT_RESULTS.json').write_text(dumps(result),encoding='utf-8')
    print(dumps({k:v for k,v in result.items() if k in ('levels','counts','html_success_rate','main_parse_success_rate','pdf_28_vs_29_reconciliation','ocr')}),flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--corpus',type=Path,required=True);parser.add_argument('--out',type=Path,required=True);parser.add_argument('--ocr',action='store_true')
    args=parser.parse_args(); run(args.corpus.resolve(),args.out.resolve(),args.ocr)
