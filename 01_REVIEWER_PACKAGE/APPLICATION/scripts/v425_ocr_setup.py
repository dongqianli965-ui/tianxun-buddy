"""Offline, explicit import of existing OCR drafts; never creates human review events."""
from pathlib import Path
import argparse, json, os
from txbuddy.governance.ocr_review import ReviewStore, sha_file

ROOT=Path(__file__).resolve().parents[1]

def ingest_existing(store, corpus, manifest, image_root, projects=None):
    corpus=Path(corpus).resolve();image_root=Path(image_root).resolve()
    report=json.loads(Path(manifest).read_text(encoding='utf-8'))
    projects=projects or []
    results=[]
    for f in report['files']:
        cache=image_root/f['sha256']/'ocr.json'
        original=json.loads(cache.read_text(encoding='utf-8'))
        # Rebase cached absolute image names only in memory; immutable old OCR JSON remains untouched.
        payload=json.loads(json.dumps(original))
        for p in payload['pages']:
            p['image']=str(image_root/f['sha256']/Path(p['image']).name)
        for t in payload['tokens']:
            t['image']=str(image_root/f['sha256']/Path(t['image']).name)
        matching=[p for p in projects if f['source_file'].startswith(p['corpus_project_path'].rstrip('/')+'/')]
        project=max(matching,key=lambda p:len(p['corpus_project_path']))['project_id'] if matching else 'CORPUS_GOVERNANCE'
        r=store.ingest(payload,project,f['source_file'],corpus,image_root)
        results.append({**r,'source_path':f['source_file'],'sha256':f['sha256'],'project_id':project,
                        'original_ocr_json_sha256':sha_file(cache),'review_status':'NEEDS_EXPERT_REVIEW'})
    return {'files':len(results),'pages':sum(r['pages'] for r in results),'items':results,
            'human_reviews_created':0,'production_accepted':False,'formal_usable':False}

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--runtime',type=Path,required=True)
    p.add_argument('--corpus',type=Path,default=ROOT.parent/'REAL_DATA_CORPUS/HN_L1_V1_20261116')
    p.add_argument('--manifest',type=Path,default=ROOT/'reports/v424/OCR_CORPUS_RESULTS.json')
    p.add_argument('--images',type=Path,default=ROOT/'reports/v424/ocr')
    p.add_argument('--reports',type=Path,default=ROOT/'reports/v425')
    a=p.parse_args();store=ReviewStore(a.runtime/'governance.sqlite3')
    project_report=json.loads((a.reports/'PROJECT_READINESS.json').read_text(encoding='utf-8'))
    result=ingest_existing(store,a.corpus,a.manifest,a.images,project_report['projects'])
    for kind,filename in (('projects','PROJECT_READINESS.json'),('sources','SOURCE_STATUS.json')):
        store.import_catalog(kind,json.loads((a.reports/filename).read_text(encoding='utf-8')))
    (a.reports/'OCR_WORKBENCH_IMPORT.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='items'},ensure_ascii=False))
