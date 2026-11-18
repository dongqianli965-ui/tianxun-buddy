"""Main application security: real JWT authentication and persistent project ACL/revocation."""
from __future__ import annotations
from pathlib import Path
from dataclasses import replace
import json, os, secrets, sqlite3, threading, logging
from fastapi import HTTPException, Request
from starlette.responses import JSONResponse
from .acl import ProjectAcl, ProjectRole, Membership, Permission, AclError
from .context import SecurityContext, TokenRevocations
from .tokens import SecuritySettings, constant_time_equals
from .users import User, UserDirectory
from .deps import require_identity


class PersistentAcl(ProjectAcl):
    def __init__(self,path):
        super().__init__(legacy_project_role=None)
        self.path=str(path); Path(path).parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS acl_member (project_id TEXT,subject TEXT,role TEXT,granted_by TEXT,granted_at REAL,updated_at REAL,PRIMARY KEY(project_id,subject))')
            c.execute('CREATE TABLE IF NOT EXISTS acl_project (project_id TEXT PRIMARY KEY,owner TEXT NOT NULL)')
            c.execute('CREATE TABLE IF NOT EXISTS auth_revoked (jti TEXT PRIMARY KEY,expires_at INTEGER NOT NULL)')
    def connect(self): return sqlite3.connect(self.path,timeout=30)
    def grant(self,*args,**kwargs):
        m=super().grant(*args,**kwargs)
        with self.connect() as c:
            c.execute('INSERT INTO acl_member VALUES (?,?,?,?,?,?) ON CONFLICT(project_id,subject) DO UPDATE SET role=excluded.role,updated_at=excluded.updated_at',
                (m.project_id,m.subject,m.role.value,m.granted_by,m.granted_at,m.updated_at))
        return m
    def membership(self,project_id,subject):
        with self.connect() as c: r=c.execute('SELECT * FROM acl_member WHERE project_id=? AND subject=?',(project_id,subject)).fetchone()
        return Membership(r[0],r[1],ProjectRole.parse(r[2]),*r[3:]) if r else None
    def members(self,project_id):
        with self.connect() as c: rows=c.execute('SELECT * FROM acl_member WHERE project_id=?',(project_id,)).fetchall()
        return tuple(Membership(r[0],r[1],ProjectRole.parse(r[2]),*r[3:]) for r in rows)
    def memberships_of(self,subject):
        with self.connect() as c: rows=c.execute('SELECT * FROM acl_member WHERE subject=?',(subject,)).fetchall()
        return tuple(Membership(r[0],r[1],ProjectRole.parse(r[2]),*r[3:]) for r in rows)
    def revoke(self,project_id,subject):
        super().revoke(project_id,subject)
        with self.connect() as c: return c.execute('DELETE FROM acl_member WHERE project_id=? AND subject=?',(project_id,subject)).rowcount>0
    def claim(self,project_id,identity):
        # Validate path IDs before persisting; DB uniqueness arbitrates concurrent claims.
        Membership(project_id,identity.subject,ProjectRole.OWNER)
        with self.connect() as c:
            c.execute('INSERT INTO acl_project VALUES (?,?) ON CONFLICT(project_id) DO NOTHING',(project_id,identity.subject))
            owner=c.execute('SELECT owner FROM acl_project WHERE project_id=?',(project_id,)).fetchone()[0]
        if owner!=identity.subject and not self.decide_identity(identity,project_id,Permission.WRITE).allowed:
            raise HTTPException(403,detail={'code':'ACL_FORBIDDEN','message':'无权访问该项目'})
        if owner==identity.subject and self.membership(project_id,identity.subject) is None:
            self.grant(project_id,identity.subject,ProjectRole.OWNER,granted_by=identity.subject)


class PersistentRevocations(TokenRevocations):
    def __init__(self,acl): super().__init__(); self.acl=acl
    def revoke(self,token_id,expires_at):
        with self.acl.connect() as c:
            c.execute('INSERT INTO auth_revoked VALUES (?,?) ON CONFLICT(jti) DO NOTHING',(token_id,expires_at))
    def is_revoked(self,token_id,*,now=None):
        with self.acl.connect() as c: return c.execute('SELECT 1 FROM auth_revoked WHERE jti=?',(token_id,)).fetchone() is not None


def runtime_context(root):
    settings=SecuritySettings(secret=os.environ.get('TXBUDDY_JWT_SECRET') or secrets.token_urlsafe(48),
        internal_token=os.environ.get('TX_INTERNAL_TOKEN',''),ttl_seconds=int(os.environ.get('TXBUDDY_JWT_TTL','3600')),
        file_root=str(Path(root)/'files'))
    entries={}
    if os.environ.get('TXBUDDY_USERS_FILE'):
        entries=json.loads(Path(os.environ['TXBUDDY_USERS_FILE']).read_text(encoding='utf-8-sig'))
    directory=UserDirectory({name:User(username=name, password_hash=spec['password_hash'],
        roles=tuple(spec.get('roles',())),display_name=spec.get('display_name',name),active=spec.get('active',True)) for name,spec in entries.items()})
    acl=PersistentAcl(Path(root)/'auth.sqlite3')
    if not settings.internal_token:
        logging.getLogger(__name__).warning('内部写接口关闭：请通过 TX_INTERNAL_TOKEN 配置独立密钥；不提供默认密钥')
    return SecurityContext(settings=settings,directory=directory,acl=acl,revocations=PersistentRevocations(acl))


def deny(): return HTTPException(403,detail={'code':'ACL_FORBIDDEN','message':'无权访问该项目'})


def check_project(ctx,identity,project,permission):
    try:
        if not ctx.decide(identity,project,permission).allowed: raise deny()
    except AclError: raise deny() from None


def install_business_guard(app,store_getter):
    @app.middleware('http')
    async def guard(request:Request,call_next):
        path=request.url.path; ctx=app.state.security
        if path in ('/health','/auth/login','/auth/test-auto-login'):
            return await call_next(request)
        try:
            if path.startswith('/internal/'):
                expected=os.environ.get('TX_INTERNAL_TOKEN','')
                if not expected or expected in ('dev-internal','txbuddy-dev-internal-token-change-me') or not constant_time_equals(request.headers.get('X-Internal-Token',''),expected):
                    raise HTTPException(403,detail={'code':'SEAL_NOT_ALLOWED','message':'内部写入口令无效或未配置'})
                return await call_next(request)
            identity=require_identity(request,request.headers.get('Authorization'))
            request.state.identity=identity
            store=store_getter(); parts=path.strip('/').split('/')
            permission=Permission.READ if request.method in ('GET','HEAD') else Permission.WRITE
            if parts[-1] in ('approve','signoff','issue'): permission=Permission.REVIEW
            if parts[-1]=='signoff' and not identity.has_role('verified_professional'):
                raise HTTPException(403,detail={'code':'PROFESSIONAL_IDENTITY_REQUIRED','message':'专业签认须经真实专业人员身份及资质核验'})
            jobid=None; object_checked=False
            if len(parts)>=3 and parts[0]=='v1' and parts[1] in ('job','jgate','deliverables'):
                jobid=parts[2]; object_checked=True
            elif len(parts)>=3 and parts[0]=='v1' and parts[1] in ('task','calc','evidence') and parts[2] not in ('recalc','verify'):
                obj={'task':store.get_task,'calc':store.get_calc_run,'evidence':store.get}[parts[1]](parts[2]); object_checked=True
                jobid=(obj.get('job_id') or (obj.get('payload') or {}).get('job_id')) if obj else None
                if not jobid and not identity.has_role('admin'): raise deny()
            if request.headers.get('Content-Type','').startswith('application/json') and request.method in ('POST','PUT','PATCH'):
                try: body=await request.json()
                except (ValueError,UnicodeDecodeError): body={}
                if isinstance(body,dict):
                    # Check every supplied object reference before idempotency or service execution.
                    for field in ('job_id',):
                        if body.get(field):
                            job=store.get_job(body[field])
                            if not job and not identity.has_role('admin'): raise deny()
                            if job: check_project(ctx,identity,job['project_id'],permission)
                    if isinstance(body.get('context'),dict) and body['context'].get('job_id'):
                        job=store.get_job(body['context']['job_id'])
                        if not job and not identity.has_role('admin'): raise deny()
                        if job: check_project(ctx,identity,job['project_id'],permission)
                    if path=='/v1/job' and body.get('project_id'):
                        members=ctx.acl.members(body['project_id'])
                        if members: check_project(ctx,identity,body['project_id'],Permission.WRITE)
            if object_checked:
                job=store.get_job(jobid) if jobid else None
                if job: check_project(ctx,identity,job['project_id'],permission)
                elif not identity.has_role('admin'): raise deny()
            if path in ('/v1/audit','/auth/audit') and not identity.has_role('admin'): raise deny()
            response=await call_next(request)
            response.headers['X-Content-Type-Options']='nosniff'
            return response
        except (HTTPException,AclError) as exc:
            ctx.audit.record(getattr(getattr(request.state,'identity',None),'subject','anonymous'), 'ACCESS_DENIED',target=path,result='denied')
            if isinstance(exc,AclError): exc=deny()
            return JSONResponse(status_code=exc.status_code,content={'detail':exc.detail},headers=exc.headers)
