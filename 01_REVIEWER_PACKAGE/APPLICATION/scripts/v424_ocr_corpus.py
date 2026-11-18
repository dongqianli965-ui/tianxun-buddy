"""Offline OCR of all scanned/mixed PDFs and corpus images, retaining all review flags."""
from pathlib import Path
import json,os,logging
from datetime import datetime
from txbuddy.pilot.database import PilotDatabase,dumps
from txbuddy.pilot.importer import verify_corpus
from txbuddy.pilot.extraction import save_fields
from txbuddy.parsing.ocr import offline_ocr

ROOT=Path(__file__).resolve().parents[1]
CORPUS=Path(os.environ.get('TXBUDDY_CORPUS_ROOT',str(ROOT.parent/'REAL_DATA_CORPUS/HN_L1_V1_20261116')))
OUT=ROOT/'reports/v424'
if __name__=='__main__':
    logging.basicConfig(filename=OUT/'ocr_engine.log',level=logging.WARNING,encoding='utf-8')
    db=PilotDatabase(os.environ['TXBUDDY_SAMPLE_DSN'])
    base=json.loads((OUT/'CORPUS_SAMPLE_PILOT_RESULTS.json').read_text(encoding='utf-8'))
    with db.transaction() as c:job=c.one("SELECT job_id FROM data_import_job WHERE status='SUCCEEDED'")['job_id']
    records=[]
    for record in base['parsing_files']:
        suffix=record['format'];pages=record.get('empty_pages') or []
        if suffix=='pdf' and not pages:continue
        if suffix not in ('pdf','jpg','jpeg','png'):continue
        requested=pages if suffix=='pdf' and record['status']=='PARSED' else None
        print('OCR',record['path'],'pages',len(pages) if pages else 1,flush=True)
        result=offline_ocr(CORPUS/record['path'],OUT/'ocr',pages=requested)
        if result.blocks:save_fields(db,record['path'],result,job)
        item={'source_file':record['path'],'sha256':result.sha256,'status':result.status.value,
            'pages':result.metadata.get('pages',[]),'characters':len(result.text),'tokens':len(result.blocks),
            'requested_pages':requested,'error':result.blocked_reason,'review_status':'NEEDS_EXPERT_REVIEW','formal_usable':False}
        records.append(item)
        (OUT/'OCR_CORPUS_RESULTS.json').write_text(dumps({'updated':datetime.now().isoformat(),'files':records,'formal_usable':False}),encoding='utf-8')
    verify_corpus(CORPUS)
    print('OCR_FILES',len(records),'PARSED',sum(r['status']=='PARSED' for r in records),flush=True)
