"""Rebuild a private, isolated content inventory from existing real OCR assets."""
from pathlib import Path
import hashlib, json, sys
from collections import defaultdict
from txbuddy.governance.ocr_review import ReviewStore, sha_file

ROOT=Path(__file__).resolve().parents[1]
CORPUS=ROOT.parent/'REAL_DATA_CORPUS/HN_L1_V1_20261116'
IMAGES=ROOT/'reports/v424/ocr'
DEST=ROOT.parent/'_CODEX_V426/ocr_dedup_private.sqlite3'
REPORT=ROOT/'reports/v426/OCR_CONTENT_DEDUP.json'

def main():
    if DEST.exists():
        raise SystemExit('Refusing to overwrite existing private inventory')
    DEST.parent.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((ROOT/'reports/v424/OCR_CORPUS_RESULTS.json').read_text(encoding='utf-8'))
    source_data=json.loads((ROOT/'reports/v425/SOURCE_STATUS.json').read_text(encoding='utf-8'))
    sources={x['local_path']:x for x in source_data['sources']}
    projects=json.loads((ROOT/'reports/v425/PROJECT_READINESS.json').read_text(encoding='utf-8'))['projects']
    store=ReviewStore(DEST)
    assets={}; results=[]
    for item in manifest['files']:
        path=item['source_file']; original=CORPUS/path
        cache=IMAGES/item['sha256']/'ocr.json'
        assets[str(original)]=sha_file(original); assets[str(cache)]=sha_file(cache)
        assert assets[str(original)]==item['sha256']
        payload=json.loads(cache.read_text(encoding='utf-8'))
        for obj in payload['pages']+payload['tokens']:
            if obj.get('image'):
                obj['image']=str(IMAGES/item['sha256']/Path(obj['image']).name)
        for page in payload['pages']:
            assets[page['image']]=sha_file(page['image'])
            assert assets[page['image']]==page['image_sha256']
        matches=[p for p in projects if path.startswith(p['corpus_project_path'].rstrip('/')+'/')]
        project=max(matches,key=lambda p:len(p['corpus_project_path']))['project_id'] if matches else 'CORPUS_GOVERNANCE'
        provenance=sources.get(path,{})
        provenance={**provenance,'source_name':provenance.get('title',original.name),
                    'authorization_status':provenance.get('authorization_status','UNKNOWN')}
        r=store.ingest(payload,project,path,CORPUS,IMAGES,provenance=provenance)
        results.append({**r,'source_path':path,'sha256':item['sha256'],'source_metadata_present':path in sources})
    groups=defaultdict(list)
    for r in results: groups[r['sha256']].append(r['source_path'])
    inventory=store.content_inventory()
    unchanged=all(sha_file(path)==digest for path,digest in assets.items())
    report={'private_database':str(DEST),'registrations':len(results),'unique_source_sha256':len(groups),
            'content_records':len(inventory['contents']),'content_versions':len(inventory['versions']),
            'source_records':len(inventory['sources']),'reused_ocr_results':sum(r['ocr_result_reused'] for r in results),
            'duplicate_groups':{k:v for k,v in groups.items() if len(v)>1},'assets_verified_unchanged':unchanged,
            'asset_sha256_before_and_after':assets,'human_reviews_created':0,'items':results}
    REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('items','asset_sha256_before_and_after','duplicate_groups')},ensure_ascii=False))

if __name__=='__main__': main()
