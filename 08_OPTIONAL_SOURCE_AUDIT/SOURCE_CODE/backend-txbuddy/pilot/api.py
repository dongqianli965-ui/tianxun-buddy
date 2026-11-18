from __future__ import annotations
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib, json, mimetypes, os, uuid
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import Response, JSONResponse
from .database import PilotDatabase, PilotError, dumps
from .service import query_prices, resolve_price, source_detail, quota_candidates, import_test_quota
from .boq import workbook_boq
from ..security.routes import auth_router
from ..security.acl_routes import projects_router
from ..security.runtime import check_project, deny
from ..security.acl import Permission
from ..storage.filestore import FileStore, FileStoreError, ChecksumMismatch
from ..parsing import parse_file
from ..parsing.ocr import offline_ocr

ALLOWED={'.xlsx','.xlsm','.pdf','.docx','.doc','.dxf','.dwg','.html','.htm','.csv','.txt','.tsv','.md','.jpg','.jpeg','.png','.zip','.rar'}
MAX_FILE_BYTES=32*1024*1024


def create_pilot_api(parent,store_getter,root):
    app=FastAPI(title='天寻 Buddy 样本试点 API',version='4.2.6')
    app.state.security=parent.state.security
    app.include_router(auth_router); app.include_router(projects_router)
    files=FileStore(Path(root)/'blobs'); parent.state.filestore=files
    db=PilotDatabase(os.environ.get('TXBUDDY_SAMPLE_DSN') or str(Path(root)/'sample-pilot.sqlite3'))
    db.migrate(); parent.state.pilot_db=db

    @app.exception_handler(PilotError)
    async def pilot_error(request,exc):
        return JSONResponse(status_code=422,content={'detail':{'code':exc.code,'message':exc.message}})

    def who(request): return request.state.identity
    def job_for(request,job_id,permission=Permission.READ):
        job=store_getter().get_job(job_id)
        if not job: raise deny()
        check_project(app.state.security,who(request),job['project_id'],permission); return job
    def file_for(request,path,permission):
        try: pure=app.state.security.files.normalize(path)
        except Exception: raise HTTPException(403,detail={'code':'FILE_PATH_INVALID','message':'路径非法'}) from None
        if len(pure.parts)<2 or any(p.startswith('.') or ':' in p for p in pure.parts): raise deny()
        check_project(app.state.security,who(request),pure.parts[0],permission)
        return pure

    @app.put('/files/{file_path:path}')
    async def put_file(file_path:str,request:Request,job_id:str|None=None):
        pure=file_for(request,file_path,Permission.WRITE)
        if pure.suffix.lower() not in ALLOWED: raise HTTPException(415,detail={'code':'FILE_TYPE_UNSUPPORTED','message':'不支持此文件类型'})
        if job_id and job_for(request,job_id,Permission.WRITE)['project_id']!=pure.parts[0]: raise deny()
        body=bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>MAX_FILE_BYTES: raise HTTPException(413,detail={'code':'FILE_TOO_LARGE','message':'文件超过 32 MiB'})
        if not body: raise HTTPException(422,detail={'code':'FILE_EMPTY','message':'文件为空'})
        try:
            ref=files.put(bytes(body),str(pure),project_id=pure.parts[0],filename=pure.name,
                actor=who(request).subject,expected_sha256=request.headers.get('X-Content-SHA256'))
        except ChecksumMismatch: raise HTTPException(422,detail={'code':'FILE_HASH_MISMATCH','message':'SHA256 校验不一致'}) from None
        if job_id:
            existing=store_getter().list_documents(job_id)
            if not any(d['doc_key']==str(pure) and d['file_sha256']==ref.sha256 for d in existing):
                store_getter().add_document(job_id,str(pure),str(ref.version),ref.sha256,False)
        return {**ref.to_dict(),'file_sha256':ref.sha256,'stored':True,'storage_path':None,
            'download_url':'/files/'+str(pure)}

    @app.get('/files/{file_path:path}')
    def get_file(file_path:str,request:Request,version:int|None=None):
        pure=file_for(request,file_path,Permission.READ)
        try: body=files.get(str(pure),version); ref=files.get_version(str(pure),version)
        except FileStoreError: raise HTTPException(404,detail={'code':'FILE_UNAVAILABLE','message':'文件不可用'}) from None
        from urllib.parse import quote
        return Response(body,media_type=mimetypes.guess_type(pure.name)[0] or 'application/octet-stream',headers={
            'Content-Disposition':"attachment; filename*=UTF-8''"+quote(pure.name),
            'X-Content-SHA256':ref.sha256,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

    @app.get('/v1/jobs')
    def jobs(request:Request,offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=200),project_id:str='',status:str='',q:str=''):
        store=store_getter()
        # Repository rows remain authoritative; permission filtering precedes pagination.
        with store._conn() as c:
            ids=[r[0] for r in c.execute('SELECT job_id FROM job ORDER BY created_at DESC,job_id')]
        rows=[store.get_job(j) for j in ids]
        rows=[r for r in rows if app.state.security.decide(who(request),r['project_id'],Permission.READ).allowed
            and (not project_id or r['project_id']==project_id) and (not status or r['status']==status) and (not q or q.lower() in r['name'].lower())]
        return {'items':rows[offset:offset+limit],'total':len(rows),'offset':offset,'limit':limit}

    def uploaded(request,body,permission=Permission.READ):
        if not isinstance(body.get('path'),str) or not body.get('job_id'): raise PilotError('PARSE_INPUT_REQUIRED','需要 job_id 与已上传文件 path')
        job=job_for(request,body['job_id'],permission)
        pure=file_for(request,body['path'],permission)
        if job['project_id']!=pure.parts[0]: raise deny()
        try: ref=files.get_version(str(pure),body.get('version')); raw=files.get(str(pure),body.get('version'))
        except FileStoreError: raise PilotError('FILE_UNAVAILABLE','已上传原件不存在或校验失败') from None
        return pure,ref,raw

    @app.post('/v1/parse')
    def parse(body:dict,request:Request):
        pure,ref,raw=uploaded(request,body,Permission.WRITE)
        with TemporaryDirectory(dir=root,prefix='parse-') as td:
            path=Path(td)/pure.name; path.write_bytes(raw)
            parsed=offline_ocr(path,Path(root)/'ocr') if body.get('ocr') else parse_file(path)
            parsed.source=str(pure); result=parsed.to_dict()
        result['job_id']=body['job_id']; result['file_version']=ref.version
        result['metadata']['source_url']=body.get('source_url') or result['metadata'].get('source_url')
        store_getter().put_record(body['job_id'],'parsed_document',result)
        return result

    @app.post('/v1/pilot/boq-import')
    def boq_import(body:dict,request:Request):
        pure,ref,raw=uploaded(request,body,Permission.WRITE)
        # Provenance roles are fixed by this reference-import endpoint, never claimed as independent calculation.
        with TemporaryDirectory(dir=root,prefix='boq-') as td:
            p=Path(td)/pure.name; p.write_bytes(raw); result=workbook_boq(p,data_role='B_REFERENCE')
        result['source_file']=str(pure); result['job_id']=body['job_id']
        existing=store_getter().get_records(body['job_id'],'sample_boq')
        if not any(r.get('sha256')==result['sha256'] and r.get('items')==result['items'] for r in existing): store_getter().put_record(body['job_id'],'sample_boq',result)
        return result

    @app.get('/v1/material-prices')
    def prices(region:str,period:str,q:str='',spec:str='',unit:str='',offset:int=0,limit:int=50,usage:str='SAMPLE_REFERENCE'):
        return query_prices(parent.state.pilot_db,region=region,period=period,q=q,spec=spec,unit=unit,offset=offset,limit=limit,usage=usage)

    @app.get('/v1/material-prices/resolve')
    def price_resolve(region:str,period:str,material_name:str='',spec_model:str|None=None,unit:str='',usage:str='SAMPLE_REFERENCE'):
        return resolve_price(parent.state.pilot_db,region=region,period=period,material_name=material_name,spec_model=spec_model,unit=unit,usage=usage)

    @app.get('/v1/sources/{document_id}')
    def source(document_id:str,request:Request):
        result=source_detail(parent.state.pilot_db,document_id)
        path=result['document'].get('local_path','')
        if path.startswith('05_REAL_PROJECTS/'):
            from ..governance.catalog import project_id_for_path
            check_project(app.state.security,who(request),project_id_for_path(path),Permission.READ)
        return result

    @app.post('/v1/quota-library/import')
    def quota_import(body:dict,request:Request):
        if not who(request).has_role('admin'): raise deny()
        return import_test_quota(db,body)

    @app.post('/v1/quota-library/match')
    def quota_match(body:dict): return quota_candidates(db,body)

    @app.get('/v1/deliverables/{job_id}')
    def deliverables(job_id:str,request:Request):
        job_for(request,job_id)
        return {'job_id':job_id,'items':store_getter().get_records(job_id,'sample_deliverable'),
            'formal_status':'BLOCKED','g4_required':True,'available':['sample_report'],
            'reason':'正式成果需通过 G4、正版依据及真实专业签认；当前只能导出样本登记/清单结果'}

    @app.post('/v1/deliverables/{job_id}/generate')
    def generate(job_id:str,body:dict,request:Request):
        job=job_for(request,job_id,Permission.WRITE)
        if body.get('kind')!='sample_report':
            from ..core.gates_service import evaluate
            from ..core.record_writer import require_server_record
            from ..core.admission import require_formal
            from ..models import BasisPack
            from ..deliverables import DeliverableDataset,DeliverableService,DeliverableError
            gate=evaluate(store_getter(),job_id,'G4',who(request).subject)
            pack_id=job['basis_pack']['basis_pack_id']
            with parent.state.pilot_db.transaction() as c:
                registered=c.one('SELECT * FROM basis_pack WHERE basis_pack_id=? AND formal_usable=1 AND license_status=?',(pack_id,'LICENSED_VERIFIED'))
            signoffs=require_server_record(store_getter(),job_id,'professional_signoff')
            valid_signoff=any(s.get('verified_professional') is True and s.get('person_name') and s.get('license_number') and s.get('signed_at') for s in signoffs)
            admission=require_formal(None,BasisPack.model_validate(job['basis_pack']),True)
            if gate['result']!='PASS' or not registered or not valid_signoff or admission['rejected']:
                raise HTTPException(422,detail={'code':'FORMAL_DELIVERABLE_BLOCKED','message':'正式预算/工程量成果尚缺正版依据及专业签认','g4':gate})
            try:
                dataset=DeliverableDataset.from_store(store_getter(),job_id,prepared_by=who(request).subject)
                if not dataset.boq or not dataset.unit_prices or (body['kind']=='quantity_calc' and not dataset.quantities):
                    raise PilotError('DELIVERABLE_DATA_MISSING','正式成果缺少实际清单或独立算量结果')
                service=DeliverableService(Path(root)/'formal-deliverables',store_getter().get)
                service._preflight(dataset)
                artifact=service.generate(dataset,body['kind'])
                content=artifact.path.read_bytes();path=job['project_id']+'/'+artifact.file_name
                ref=files.put(content,path,project_id=job['project_id'],filename=artifact.file_name,actor=who(request).subject)
                manifest={'kind':body['kind'],'job_id':job_id,'download_url':'/files/'+path,'file_sha256':ref.sha256,
                    'version':ref.version,'formal_usable':True,'g4':gate,'basis_pack_id':pack_id}
                store_getter().put_record(job_id,'formal_deliverable',manifest)
                return manifest
            except DeliverableError as exc:
                raise PilotError(exc.code,exc.message) from None
        records=store_getter().get_records(job_id,'sample_boq')
        if not records or not any(r.get('items') for r in records): raise PilotError('SAMPLE_BOQ_REQUIRED','须先完成样本清单导入')
        from openpyxl import Workbook,load_workbook
        from io import BytesIO
        wb=Workbook(); ws=wb.active; ws.title='样本清单导入结果'
        ws.append(['样本验证导出：B_REFERENCE；未独立算量；计算精度 BLOCKED'])
        ws.append(['编码','名称','项目特征','单位','原清单工程量','来源文件','SHA256','Sheet','行号','单元格','数据角色','校验错误','原始单元格'])
        seen=set(); seen_files=set(); count=0
        for result in reversed(records):
            if result['sha256'] in seen_files: continue
            seen_files.add(result['sha256'])
            for item in result.get('items',[]):
                key=(result['sha256'],item['sheet'],item['row'])
                if key in seen: continue
                seen.add(key); count+=1
                ws.append([item['code'],item['name'],item.get('feature',''),item['unit'],item['quantity'],result['source_file'],result['sha256'],item['sheet'],item['row'],dumps(item['cells']),item['data_role'],dumps(item.get('validation_errors',[])),dumps(item.get('raw_cells',[]))])
                # Keep numeric quantity/row fields numeric while preventing
                # provenance strings from becoming executable spreadsheet
                # formulas on export.
                for cell in ws[ws.max_row]:
                    if isinstance(cell.value,str) and cell.value[:1] in ('=','+','-','@'):
                        cell.value="'"+cell.value
        ws.freeze_panes='A3'; ws.auto_filter.ref=f'A2:M{ws.max_row}'
        for col in ws.columns: ws.column_dimensions[col[0].column_letter].width=min(48,max(12,max(len(str(c.value or '')) for c in col)+2))
        bio=BytesIO(); wb.save(bio); raw=bio.getvalue(); assert load_workbook(BytesIO(raw),read_only=True).active.max_row==count+2
        path=f"{job['project_id']}/sample-result-{job_id}.xlsx"
        ref=files.put(raw,path,project_id=job['project_id'],filename=Path(path).name,actor=who(request).subject)
        manifest={'kind':'sample_report','job_id':job_id,'download_url':'/files/'+path,'file_sha256':ref.sha256,
            'version':ref.version,'rows':count,'formal_usable':False,'accuracy_verification':'BLOCKED','data_role':'B_REFERENCE'}
        store_getter().put_record(job_id,'sample_deliverable',manifest)
        return manifest

    return app
