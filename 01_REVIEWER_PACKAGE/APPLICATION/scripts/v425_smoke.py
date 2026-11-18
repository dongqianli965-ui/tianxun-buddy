"""Read-only local workbench smoke. Credentials and tokens never enter stdout or evidence."""
from pathlib import Path
import hashlib,json,re,time
import httpx

ROOT=Path(__file__).resolve().parents[1]
access=ROOT.parent/'_CODEX_V424/local-pilot/LOCAL_PILOT_ACCESS.private.txt'
password=next(re.search(r'密码\s*[:：]\s*(\S+)',line).group(1) for line in access.read_text(encoding='utf-8').splitlines() if re.search(r'密码\s*[:：]\s*(\S+)',line))
with httpx.Client(base_url='http://127.0.0.1:4185',timeout=30) as client:
    health=client.get('/health');health.raise_for_status();assert health.json()['version']=='0.4.5.1'
    r=client.post('/auth/login',json={'username':'pilot','password':password});r.raise_for_status()
    client.headers['Authorization']='Bearer '+r.json()['access_token']
    docs=client.get('/v1/governance/ocr?limit=200').json();assert docs['total']==40
    assert all(d['review_status']=='NEEDS_EXPERT_REVIEW' for d in docs['items'])
    document=client.get('/v1/governance/ocr/documents/'+docs['items'][0]['document_id']).json()
    page=client.get('/v1/governance/ocr/pages/'+document['pages'][0]['page_id']).json()
    image=client.get(page['image_url']);image.raise_for_status();assert hashlib.sha256(image.content).hexdigest()==page['image_sha256']
    projects=client.get('/v1/governance/projects').json();sources=client.get('/v1/governance/sources?limit=250').json()
    assert projects['total']==18 and sources['total']==220
    assert len([s for s in sources['items'] if s.get('source_url')])==217
    assert page['change_history']==[]
    result={'passed':True,'url':'http://127.0.0.1:4185','version':health.json()['version'],'login':True,
        'ocr_documents':docs['total'],'ocr_pages':sum(d['page_count'] for d in docs['items']),'review_events_created':0,
        'original_image_hash_verified':True,'projects':projects['total'],'sources':sources['total'],'source_direct_urls':217,
        'production_accepted':False,'accuracy_E':'BLOCKED'}
    (ROOT/'reports/v425/LOCAL_WORKBENCH_SMOKE.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
