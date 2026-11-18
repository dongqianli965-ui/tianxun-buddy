import uuid
from .test_v24_api_auth import secure_client,login,create

def test_upload_html_parse_with_hash_and_locator(secure_client):
    a=login(secure_client);j=create(secure_client,a);path=j['project_id']+'/'+uuid.uuid4().hex+'.html'
    r=secure_client.put('/files/'+path+'?job_id='+j['job_id'],headers=a,content='<html><body><h1>材料依据</h1><p>中文原文</p></body></html>'.encode())
    assert r.status_code==200
    p=secure_client.post('/v1/parse',headers=a,json={'path':path,'job_id':j['job_id']})
    assert p.status_code==200,p.text
    d=p.json();assert d['sha256']==r.json()['sha256'] and d['blocks'][0]['locator']
    b=login(secure_client,'mallory');assert secure_client.post('/v1/parse',headers=b,json={'path':path,'job_id':j['job_id']}).status_code==403

def test_four_format_http_parsing_and_dwg_blocked(secure_client,tmp_path):
    from .test_v423_parsing import build_xlsx,build_pdf,build_docx,build_dxf
    a=login(secure_client);j=create(secure_client,a)
    paths=[build_xlsx(tmp_path/'input.xlsx'),build_pdf(tmp_path/'input.pdf',[['TEST_ONLY HTTP PDF']]),build_docx(tmp_path/'input.docx'),build_dxf(tmp_path/'input.dxf')]
    dwg=tmp_path/'input.dwg';dwg.write_bytes(b'AC1027 TEST_ONLY');paths.append(dwg)
    for p in paths:
        path=j['project_id']+'/'+p.name
        assert secure_client.put('/files/'+path,headers=a,content=p.read_bytes()).status_code==200
        r=secure_client.post('/v1/parse',headers=a,json={'path':path,'job_id':j['job_id']});assert r.status_code==200,r.text
        assert r.json()['status']==('BLOCKED' if p.suffix=='.dwg' else 'PARSED'),r.text
