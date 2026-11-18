import hashlib
from io import BytesIO
from openpyxl import load_workbook
from .test_v24_api_auth import secure_client,login,create
from .v24_fixtures import corpus,price_db

def test_real_l1_upload_parse_boq_price_source_workflow_export(secure_client,corpus,price_db,monkeypatch):
    from txbuddy import api
    monkeypatch.setattr(api.app.state,'pilot_db',price_db)
    a=login(secure_client);j=create(secure_client,a);total=0
    for suffix,count in [('土建_工程量清单.xlsx',50),('安装_工程量清单.xlsx',98)]:
        f=next((corpus/'05_REAL_PROJECTS/L1_BUDGET/L1_001/B_REFERENCE').glob('*'+suffix));raw=f.read_bytes();path=j['project_id']+'/'+f.name
        upload=secure_client.put('/files/'+path+'?job_id='+j['job_id'],content=raw,headers={**a,'X-Content-SHA256':hashlib.sha256(raw).hexdigest()})
        assert upload.status_code==200,upload.text
        body={'path':path,'job_id':j['job_id']}
        parsed=secure_client.post('/v1/parse',json=body,headers=a).json();assert parsed['status']=='PARSED' and parsed['sha256']==hashlib.sha256(raw).hexdigest()
        result=secure_client.post('/v1/pilot/boq-import',json=body,headers=a).json();assert result['count']==count;total+=count
        assert result['data_role']=='B_REFERENCE' and result['accuracy_verification']=='BLOCKED'
    prices=secure_client.get('/v1/material-prices?region=410100-INNER-4RING&period=2026-Q2&q=沥青混凝土',headers=a).json()
    row=next(x for x in prices['items'] if x['record_id']=='ZZ-20262-1782')
    assert row['price_incl_tax']=='492.34' and len(row['version_history'])==2
    detail=secure_client.get('/v1/sources/'+row['document_id'],headers=a).json();assert detail['document']['sha256']==row['sha256']
    t=secure_client.post('/v1/job/'+j['job_id']+'/tasks',headers=a,json={'node':'IMPORT','payload':{'sample_only':True}}).json()
    done=secure_client.post('/v1/task/'+t['task_id']+'/complete',headers=a,json={'result':{'sample_only':True}})
    assert done.json()['status']=='DONE',done.text
    export=secure_client.post('/v1/deliverables/'+j['job_id']+'/generate',headers=a,json={'kind':'sample_report'})
    assert export.status_code==200,export.text
    manifest=export.json();download=secure_client.get(manifest['download_url'],headers=a)
    assert hashlib.sha256(download.content).hexdigest()==manifest['file_sha256']
    wb=load_workbook(BytesIO(download.content),read_only=True);assert wb.active.max_row==total+2 and total==148;wb.close()
    assert manifest['accuracy_verification']=='BLOCKED' and manifest['formal_usable'] is False
