"""Real HTTP acceptance against the active, verified TEST_ONLY runtime."""
from pathlib import Path
from io import BytesIO
from decimal import Decimal
import hashlib, json, sys, time, shutil, urllib.request, urllib.error
from urllib.parse import urlencode, quote
import v426_manual_runtime as runtime
from package_v426 import source_manifest

ROOT=runtime.ROOT
OUT=ROOT/'reports/v426_manual'

def run():
    session=runtime.load_session()
    state=runtime.read_json(session/'state.private.json')
    config=runtime.read_json(session/'config.private.json')
    if state['status']!='RUNNING' or config['TXBUDDY_ENV']!='TEST_ONLY':
        raise runtime.Blocked('需要正在运行的 TEST_ONLY 会话')
    _,source,_=runtime.source_settings()
    runtime.verify_clone_identity(config['TXBUDDY_SAMPLE_DSN'],state['postgres']['database'],source)
    OUT.mkdir(parents=True,exist_ok=True)
    stamp=__import__('datetime').datetime.now().strftime('%Y%m%dT%H%M%S%f')
    previous=[OUT/name for name in ('API_REQUESTS.json','API_RESULTS.json','TEST_ONLY_API_SAMPLE.xlsx') if (OUT/name).exists()]
    if previous:
        attempt=OUT/('attempt_api_'+stamp);attempt.mkdir()
        for path in previous:shutil.move(str(path),str(attempt/path.name))
    tested_source=source_manifest(ROOT);started=runtime.now()
    fixture=runtime.read_json(session/'TEST_ACCOUNTS.private.json')
    tokens={};requests=[];cases=[]
    def call(method,path,body=None,role='OWNER',expected=200,raw=None,headers=None,token=None):
        h={} if role is None else {'Authorization':'Bearer '+(token if token is not None else tokens[role])}
        if headers:h.update(headers)
        data=raw
        if body is not None:data=json.dumps(body).encode();h['Content-Type']='application/json'
        req=urllib.request.Request('http://127.0.0.1:8010'+quote(path,safe='/?=&%'),data=data,method=method,headers=h)
        try:
            response=urllib.request.urlopen(req,timeout=120)
        except urllib.error.HTTPError as error:response=error
        content=response.read();status=response.code
        try:result=json.loads(content)
        except (ValueError,UnicodeDecodeError):result=None
        requests.append({'method':method,'path':path,'role':role,'expected_status':expected,'actual_status':status,'request':runtime.redact(body),'response':runtime.redact(result) if result is not None else {'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()}})
        assert status==expected,(method,path,status,runtime.redact(result))
        return result if result is not None else (content,response.headers)
    def check(name,fn):
        try:
            detail=fn();cases.append({'name':name,'status':'PASS','detail':detail})
        except Exception as error:
            cases.append({'name':name,'status':'FAIL','error':str(error)})
    for role,account in fixture['accounts'].items():
        result=call('POST','/auth/login',account,role=None)
        tokens[role]=result['access_token']
    def auth():
        call('POST','/auth/login',{'username':'owner','password':'TEST_ONLY_WRONG_PASSWORD'},role=None,expected=401)
        for role in tokens:assert call('GET','/auth/me',role=role)['subject']==role.lower()
        sys.path.insert(0,str(ROOT/'backend'))
        from txbuddy.security.tokens import SecuritySettings,encode_token
        expired=encode_token(subject='owner',settings=SecuritySettings.from_env(config),now=int(time.time())-100,ttl_seconds=1)
        result=call('GET','/auth/me',token=expired,expected=401)
        assert result['detail']['code']=='AUTH_TOKEN_EXPIRED'
        return {'roles':5,'wrong_password':401,'valid_signature_expired_token':401}
    check('身份/五种角色/真实签名过期Token',auth)
    def ocr():
        documents=call('GET','/v1/governance/ocr?q=173&limit=200')['items']
        assert documents
        doc=call('GET','/v1/governance/ocr/documents/'+documents[0]['document_id'])
        assert len(doc['pages'])>=2
        pid=doc['pages'][0]['page_id'];path='/v1/governance/ocr/pages/'+pid
        page=call('GET',path);raw,headers=call('GET',path+'/image')
        assert hashlib.sha256(raw).hexdigest()==headers['X-Content-SHA256']==page['image_sha256']
        assert page['tokens'] and page['history_chain_valid']
        call('GET',path,role='UNAUTHORIZED',expected=403)
        draft={'expected_version':page['version'],'corrected_text':page['corrected_text'],'structured_fields':page['structured_fields'],'reason':'TEST_ONLY API权限负例'}
        call('POST',path+'/drafts',draft,role='VIEWER',expected=403)
        decision={'expected_version':page['version'],'submitted_version':max(page['version'],1),'expected_content_sha256':page['content_sha256'],'decision':'APPROVE','reason':'TEST_ONLY 拒绝EDITOR审批','human_confirmed':True}
        call('POST',path+'/decisions',decision,role='EDITOR',expected=403)
        signoff={**draft,'human_confirmed':True}
        denied=call('POST',path+'/signoff',signoff,role='REVIEWER',expected=403)
        assert denied['detail']['code']=='PROFESSIONAL_IDENTITY_REQUIRED'
        assert call('GET',path)['version']==page['version']
        return {'pages':len(doc['pages']),'image_sha256':page['image_sha256'],'history_chain_valid':True,'negative_cases':4,'human_review_count':0}
    check('OCR真实页图/定位框/拒绝越权/专业资格门禁',ocr)
    def prices():
        common={'region':'410100-INNER-4RING'}
        totals={}
        for period in ('2026-Q2','2026-Q3','2026-09'):
            data=call('GET','/v1/material-prices?'+urlencode({**common,'period':period}))
            assert data['total']>0 and data['formal_usable'] is False
            assert all(row['period']==period and row['region']==common['region'] for row in data['items'])
            totals[period]=data['total']
        for params,code in [({**common,'period':'2026-12'},'PRICE_PERIOD_MISSING'),({'region':'NOT-EXIST','period':'2026-Q2'},'REGION_MISMATCH')]:
            assert call('GET','/v1/material-prices?'+urlencode(params),expected=422)['detail']['code']==code
        import psycopg2
        with psycopg2.connect(config['TXBUDDY_SAMPLE_DSN']) as connection:
            connection.set_session(readonly=True)
            with connection.cursor() as cursor:
                cursor.execute("SELECT material_name,spec_model,unit,period FROM material_price WHERE region=%s AND scope_status<>'ERRATA_FOR_Q2' GROUP BY material_name,spec_model,unit,period HAVING count(*)>1 ORDER BY material_name,spec_model",(common['region'],))
                duplicate=None
                # The verifier reads PostgreSQL for provenance, while the
                # running pilot API may expose a normalized SQLite mirror.
                # Select the first duplicate that is actually visible through
                # the API, rather than assuming row ordering is identical.
                for candidate in cursor.fetchall():
                    probe=dict(zip(('material_name','spec_model','unit','period'),candidate))
                    visible=call('GET','/v1/material-prices?'+urlencode({**common,'period':probe['period'],'q':probe['material_name'],'spec':probe['spec_model'],'unit':probe['unit'],'limit':200}))
                    if visible.get('total',0) > 1:
                        duplicate=probe
                        break
                assert duplicate is not None, 'no duplicate material is visible through the running API'
                cursor.execute("SELECT record_id,material_name,spec_model,unit,period FROM material_price WHERE record_id IN ('ZZ-20262-1782','ZZ-20262-1783','ZZ-20262-1784') ORDER BY record_id")
                errata=cursor.fetchall()
        assert call('GET','/v1/material-prices/resolve?'+urlencode({**common,**duplicate}),expected=422)['detail']['code']=='MATERIAL_AMBIGUOUS'
        assert call('GET','/v1/material-prices/resolve?'+urlencode({**common,'period':'2026-Q2','material_name':'钢'}),expected=422)['detail']['code']=='MATERIAL_CONDITIONS_REQUIRED'
        expected=[('492.34','435.70'),('470.95','416.77'),('439.83','389.23')]
        for row,values in zip(errata,expected):
            rid,name,spec,unit,period=row
            data=call('GET','/v1/material-prices?'+urlencode({**common,'period':period,'q':name,'spec':spec,'unit':unit,'limit':200}))
            item=next(item for item in data['items'] if item['record_id']==rid)
            assert item['corrected'] and len(item['version_history'])==2
            assert Decimal(item['price_incl_tax'])==Decimal(values[0]) and Decimal(item['price_excl_tax'])==Decimal(values[1])
            assert item['version_history'][1]['source_page']==137
            source_info=call('GET',item['source_url'])
            assert source_info['original_read_only'] is True
        assert len(errata)==3
        return {'period_query_counts':totals,'three_errata_page':137,'exact_duplicate_rejected':True,'missing_region_period_rejected':True,'historical_prices_preserved':True}
    check('材料期次/地域/勘误历史/重复消歧/来源追溯',prices)
    def boq():
        job='TEST_ONLY-JOB-A';project='TEST_ONLY-MANUAL-A';expected={}
        for suffix,count in [('土建_工程量清单.xlsx',50),('安装_工程量清单.xlsx',98)]:
            path=next(Path(fixture['boq_folder']).glob('*'+suffix));content=path.read_bytes();sha=hashlib.sha256(content).hexdigest();key=project+'/'+path.name
            uploaded=call('PUT','/files/'+key+'?job_id='+job,raw=content,headers={'X-Content-SHA256':sha})
            assert uploaded['file_sha256']==sha
            parsed=call('POST','/v1/parse',{'job_id':job,'path':key});assert parsed
            result=call('POST','/v1/pilot/boq-import',{'job_id':job,'path':key})
            assert result['count']==count and result['independently_calculated'] is False
            for item in result['items']:expected[(item['file_sha256'],item['sheet'],item['row'])]=item
        manifest=call('POST','/v1/deliverables/'+job+'/generate',{'kind':'sample_report'})
        assert manifest['formal_usable'] is False and manifest['accuracy_verification']=='BLOCKED'
        content,headers=call('GET',manifest['download_url'])
        from openpyxl import load_workbook
        book=load_workbook(BytesIO(content),read_only=True,data_only=True)
        try:
            rows=list(book.active.iter_rows(values_only=True));columns={name:i for i,name in enumerate(rows[1])}
            assert len(rows)-2==len(expected)==148
            for row in rows[2:]:
                item=expected[(row[columns['SHA256']],row[columns['Sheet']],row[columns['行号']])]
                for label,field in [('编码','code'),('名称','name'),('项目特征','feature'),('单位','unit')]:assert (row[columns[label]] or '')==item[field]
                assert Decimal(str(row[columns['原清单工程量']]))==Decimal(item['quantity'])
        finally:book.close()
        (OUT/'TEST_ONLY_API_SAMPLE.xlsx').write_bytes(content)
        call('GET','/v1/job/'+job,role='UNAUTHORIZED',expected=403)
        return {'reference_rows':148,'civil_rows':50,'installation_rows':98,'every_exported_field_compared':True,'independently_calculated':False}
    check('真实清单上传解析/148行逐字段导出往返/未授权项目',boq)
    def formal_gate():
        health=call('GET','/health',role=None)
        assert health['status']=='ok' and health['version']=='4.2.6'
        assert health['environment']=='TEST_ONLY' and health['test_only'] is True
        assert health['production_accepted'] is False and health['accuracy_E']=='BLOCKED'
        job='TEST_ONLY-JOB-A'
        before=call('GET','/v1/deliverables/'+job)
        assert before['formal_status']=='BLOCKED'
        denied=call('POST','/v1/deliverables/'+job+'/generate',{'kind':'quantity_calc'},expected=422)
        assert denied['detail']['code']=='FORMAL_DELIVERABLE_BLOCKED'
        after=call('GET','/v1/deliverables/'+job)
        assert before==after and all(item['formal_usable'] is False for item in after['items'])
        return {'health_environment':'TEST_ONLY','formal_deliverable_status':422,'formal_status':'BLOCKED','no_formal_artifact_created':True}
    check('实际环境健康/正式成果后端门禁/禁止前端绕过',formal_gate)
    def logout():
        assert call('POST','/auth/logout')['revoked'] is True
        call('GET','/auth/me',expected=401)
        return '已撤销令牌不能复用'
    check('退出登录撤销Token',logout)
    runtime.write_json(OUT/'API_REQUESTS.json',requests)
    report={'cases':cases,'passed':sum(c['status']=='PASS' for c in cases),'failures':sum(c['status']=='FAIL' for c in cases),'request_count':len(requests),'environment':'TEST_ONLY','production_accepted':False,'accuracy_E':'BLOCKED','session':str(session),'started_at':started,'finished_at':runtime.now(),'tested_source_sha256':tested_source,'source_unchanged':tested_source==source_manifest(ROOT),'requests_sha256':runtime.digest(OUT/'API_REQUESTS.json')}
    runtime.write_json(OUT/'API_RESULTS.json',report)
    print(json.dumps({k:v for k,v in report.items() if k!='tested_source_sha256'},ensure_ascii=False,indent=2))
    return 0 if report['failures']==0 else 1

if __name__=='__main__':raise SystemExit(run())
