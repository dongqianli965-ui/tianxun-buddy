"""Authenticated project-scoped review routes. No HTTP import or identity verification shortcut."""
from pathlib import Path
import json, os
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictBool
from .ocr_review import ReviewStore, ReviewError, sha_file
from ..security.acl import Permission
from ..security.runtime import check_project, deny

class ReviewInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_version: StrictInt=Field(ge=0)
    corrected_text: str=Field(max_length=200000)
    structured_fields: dict=Field(default_factory=dict)
    review_status: str='IN_REVIEW'
    reason: str=Field(default='',max_length=4000)
    human_confirmed: StrictBool=False
    expected_content_sha256: str|None=Field(default=None,pattern=r'^[0-9a-f]{64}$')
    submitted_version: StrictInt|None=Field(default=None,ge=1)

class DraftInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_version: StrictInt=Field(ge=0)
    corrected_text: str=Field(max_length=200000)
    structured_fields: dict=Field(default_factory=dict)
    reason: str=Field(default='',max_length=4000)

class SubmitInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_version: StrictInt=Field(ge=0)
    expected_content_sha256: str=Field(pattern=r'^[0-9a-f]{64}$')
    reason: str=Field(default='',max_length=4000)

class DecisionInput(SubmitInput):
    submitted_version: StrictInt=Field(ge=1)
    decision: str=Field(pattern=r'^(APPROVE|RETURN)$')
    human_confirmed: StrictBool=False

def create_governance_api(parent,root):
    app=FastAPI(title='天寻 Buddy 数据治理 API',version='4.2.6')
    app.state.security=parent.state.security
    install_governance(app,parent,root)
    return app

def install_governance(app, parent, root):
    parent.state.review_store=ReviewStore(Path(root)/'governance.sqlite3')

    @app.exception_handler(ReviewError)
    async def review_error(request, exc):
        return JSONResponse(status_code=exc.status, content={'detail':{'code':exc.code,'message':exc.message}})

    def db():
        return parent.state.review_store

    def require(request, project, permission=Permission.READ):
        check_project(parent.state.security,request.state.identity,project,permission)

    def visible(request, project):
        return parent.state.security.decide(request.state.identity,project,Permission.READ).allowed

    def get_page(request,pageid,permission=Permission.READ):
        p=db().page(pageid)
        if not p:
            raise deny()
        require(request,p['project_id'],permission)
        return p

    def public_page(p,request=None):
        # Keep filesystem paths private; serve page images through the same project ACL.
        p.pop('image_path',None)
        for token in p['tokens']:
            token.pop('image',None)
        p['image_url']='/v1/governance/ocr/pages/'+p['page_id']+'/image'
        p['history_chain_valid']=db().verify_chain(p['page_id'])
        if request is not None:
            ctx=parent.state.security;identity=request.state.identity
            p['permissions']={
                'can_edit':ctx.decide(identity,p['project_id'],Permission.WRITE).allowed,
                'can_review':ctx.decide(identity,p['project_id'],Permission.REVIEW).allowed,
                'can_signoff':ctx.decide(identity,p['project_id'],Permission.REVIEW).allowed and identity.has_role('verified_professional')}
        return p

    @app.get('/ocr')
    def ocr_list(request:Request,project_id:str='',review_status:str='',q:str='',
                 offset:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=200)):
        if project_id:
            require(request,project_id)
        rows=[d for d in db().documents() if visible(request,d['project_id'])
              and (not project_id or d['project_id']==project_id)
              and (not review_status or d['review_status']==review_status)
              and (not q or q.lower() in (d['source_path']+' '+d['source_file_sha256']).lower())]
        return {'items':rows[offset:offset+limit], 'total':len(rows),'offset':offset,'limit':limit,
                'production_accepted':False,'accuracy_verification':'BLOCKED'}

    @app.get('/ocr/documents/{document_id}')
    def document(document_id:str,request:Request):
        d=db().document(document_id)
        if not d:
            raise deny()
        require(request,d['project_id'])
        d['formal_usable']=False
        return d

    @app.get('/ocr/pages/{page_id}')
    def page(page_id:str,request:Request):
        return public_page(get_page(request,page_id),request)

    @app.get('/ocr/pages/{page_id}/image')
    def page_image(page_id:str,request:Request):
        p=get_page(request,page_id)
        try:
            raw=Path(p['image_path']).read_bytes()
        except OSError:
            raise ReviewError('IMAGE_UNAVAILABLE','页图不可用') from None
        import hashlib
        if hashlib.sha256(raw).hexdigest()!=p['image_sha256']:
            raise ReviewError('IMAGE_HASH_MISMATCH','页图 SHA256 校验失败')
        return Response(raw,media_type='image/png',headers={'Cache-Control':'no-store',
          'X-Content-SHA256':p['image_sha256'],'X-Content-Type-Options':'nosniff'})

    @app.post('/ocr/pages/{page_id}/reviews')
    def save_review(page_id:str,body:ReviewInput,request:Request):
        decision=body.review_status in ('REVIEWED','RETURNED')
        current=get_page(request,page_id,Permission.REVIEW if decision else Permission.WRITE)
        if body.review_status=='PROFESSIONALLY_SIGNED':
            raise ReviewError('PROFESSIONAL_SIGNOFF_ENDPOINT_REQUIRED','正式签认须使用独立签认入口',403)
        if decision:
            if body.submitted_version is None or body.expected_content_sha256 is None:
                raise ReviewError('REVIEW_SUBMISSION_BINDING_REQUIRED','复核决定须提供已提交版本和内容 SHA256')
            if body.corrected_text!=current['corrected_text'] or body.structured_fields!=current['structured_fields']:
                raise ReviewError('REVIEW_CONTENT_CHANGE_FORBIDDEN','复核决定不得修改已提交的 OCR 内容',403)
        try:
            result=db().save(page_id,reviewer_id=request.state.identity.subject,
                action=('approve' if body.review_status=='REVIEWED' else 'return') if decision else 'edit',**body.model_dump())
        except (TypeError, ValueError) as exc:
            if isinstance(exc,ReviewError):
                raise
            raise ReviewError('REVIEW_FIELDS_INVALID','结构化字段包含非法值') from None
        return public_page(result,request)

    @app.post('/ocr/pages/{page_id}/drafts')
    def save_draft(page_id:str,body:DraftInput,request:Request):
        get_page(request,page_id,Permission.WRITE)
        return public_page(db().save(page_id,reviewer_id=request.state.identity.subject,
            action='edit',review_status='NEEDS_EXPERT_REVIEW',**body.model_dump()),request)

    @app.post('/ocr/pages/{page_id}/submit')
    def submit_review(page_id:str,body:SubmitInput,request:Request):
        current=get_page(request,page_id,Permission.WRITE)
        return public_page(db().save(page_id,reviewer_id=request.state.identity.subject,
            action='submit',review_status='IN_REVIEW',corrected_text=current['corrected_text'],
            structured_fields=current['structured_fields'],**body.model_dump()),request)

    @app.post('/ocr/pages/{page_id}/decisions')
    def decide_review(page_id:str,body:DecisionInput,request:Request):
        current=get_page(request,page_id,Permission.REVIEW)
        values=body.model_dump();decision=values.pop('decision')
        return public_page(db().save(page_id,reviewer_id=request.state.identity.subject,
            action='approve' if decision=='APPROVE' else 'return',
            review_status='REVIEWED' if decision=='APPROVE' else 'RETURNED',
            corrected_text=current['corrected_text'],structured_fields=current['structured_fields'],**values),request)

    @app.post('/ocr/pages/{page_id}/signoff')
    def professional_signoff(page_id:str,body:ReviewInput,request:Request):
        get_page(request,page_id,Permission.REVIEW)
        identity=request.state.identity
        if not identity.has_role('verified_professional'):
            raise ReviewError('PROFESSIONAL_IDENTITY_REQUIRED','专业签认须经真实专业人员身份及资质核验',403)
        registry={}
        path=os.environ.get('TXBUDDY_PROFESSIONALS_FILE')
        if path:
            try:
                registry=json.loads(Path(path).read_text(encoding='utf-8'))
            except (OSError,ValueError):
                pass
        verified=registry.get(identity.subject)
        if not isinstance(verified,dict) or verified.get('subject')!=identity.subject or not all(
                verified.get(key) for key in ('identity_verified_at','license_number','name')):
            raise ReviewError('PROFESSIONAL_IDENTITY_REQUIRED','尚无真实专业人员身份及资质核验登记',403)
        values=body.model_dump();values['review_status']='PROFESSIONALLY_SIGNED'
        if not body.expected_content_sha256:
            raise ReviewError('REVIEW_CONTENT_HASH_REQUIRED','正式签认须绑定已复核内容 SHA256')
        return public_page(db().save(page_id,reviewer_id=identity.subject,professional=verified,
            action='signoff',**values),request)

    @app.get('/projects')
    def projects(request:Request,project_id:str=''):
        if project_id:
            require(request,project_id)
        report=db().catalog('projects')
        rows=[p for p in report['projects'] if visible(request,p['project_id']) and (not project_id or p['project_id']==project_id)]
        return {'items':rows,'total':len(rows),'available':report['available'],
          'snapshot_sha256':report.get('snapshot_sha256'),'production_accepted':False,'accuracy_verification':'BLOCKED'}

    @app.get('/sources')
    def sources(request:Request,project_id:str='',q:str='',offset:int=Query(0,ge=0),limit:int=Query(100,ge=1,le=250)):
        if project_id:
            require(request,project_id)
        report=db().catalog('sources')
        rows=[s for s in report['sources'] if visible(request,s['project_id']) and (not project_id or s['project_id']==project_id)
              and (not q or q.lower() in json.dumps(s,ensure_ascii=False).lower())]
        return {'items':rows[offset:offset+limit],'total':len(rows),'available':report['available'],
          'snapshot_sha256':report.get('snapshot_sha256'),'production_accepted':False}

    @app.get('/sources/{source_id}')
    def source(source_id:str,request:Request):
        row=next((s for s in db().catalog('sources')['sources'] if s['source_id']==source_id),None)
        if not row:
            raise deny()
        require(request,row['project_id'])
        return row
