"""P1-11-AUTH: 认证/受保护/内部接口路由。"""
from __future__ import annotations

import os
import threading
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .context import Identity, InternalIdentity, SecurityContext
from .deps import get_context, require_identity, require_internal, require_roles
from .files import FileAccessError, FileGrant
from .tokens import TokenError, constant_time_equals, decode_token, encode_token
from .users import AuthError, User

__all__ = ["auth_router", "files_router", "internal_router"]

_TEST_AUTOLOGIN_USED: set[str] = set()
_TEST_AUTOLOGIN_LOCK = threading.Lock()


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=512)


auth_router = APIRouter(prefix="/auth", tags=["auth"])
files_router = APIRouter(prefix="/files", tags=["files"])
internal_router = APIRouter(prefix="/internal", tags=["internal"])


def _client_key(request: Request) -> str:
    client = request.client
    return client.host if client is not None else "unknown"


def _identity_view(identity: Identity) -> dict[str, object]:
    return {
        "subject": identity.subject,
        "roles": list(identity.roles),
        "projects": list(identity.projects),
        "token_id": identity.token_id,
        "expires_at": identity.expires_at,
        "method": identity.method,
    }


@auth_router.post("/login")
def login(payload: LoginRequest, request: Request) -> dict[str, object]:
    """用户名 + 口令换取 HS256 访问令牌。"""
    context = get_context(request)
    client = _client_key(request)
    remaining = context.throttle.check(payload.username, client)
    if remaining > 0:
        context.audit.record(payload.username, "AUTH_LOGIN", result="locked", detail=f"{remaining:.0f}s")
        raise HTTPException(
            status_code=429,
            detail={"code": "AUTH_LOGIN_LOCKED", "message": "登录失败次数过多，请稍后再试"},
            headers={"Retry-After": str(max(1, int(remaining)))},
        )

    try:
        user: User = context.directory.authenticate(payload.username, payload.password)
    except AuthError as exc:
        context.throttle.record_failure(payload.username, client)
        context.audit.record(payload.username, "AUTH_LOGIN", result="denied", detail=exc.code)
        # 账号停用是“凭据正确但禁止登录”，用 403 与口令错误（401）区分；
        # 未知用户与口令错误共用 401，避免用户名枚举。
        status_code = 403 if exc.code == "AUTH_USER_DISABLED" else 401
        raise HTTPException(status_code=status_code, detail={"code": exc.code, "message": exc.message}) from None

    context.throttle.reset(payload.username, client)
    try:
        token = encode_token(
            subject=user.username,
            settings=context.settings,
            roles=user.roles,
            projects=user.projects,
        )
        payload_checked = decode_token(token, settings=context.settings)
    except TokenError as exc:  # pragma: no cover - 配置错误
        raise HTTPException(status_code=500, detail={"code": exc.code, "message": exc.message}) from None

    context.audit.record(user.username, "AUTH_LOGIN", result="ok")
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": payload_checked.expires_at - payload_checked.issued_at,
        "expires_at": payload_checked.expires_at,
        "subject": user.username,
        "roles": list(user.roles),
        "projects": list(user.projects),
    }

@auth_router.get("/test-auto-login")
def test_auto_login(username: str, bootstrap: str, request: Request) -> dict[str, object]:
    """One-click login for the isolated local TEST_ONLY workbench.

    This route is deliberately unavailable outside TEST_ONLY or off localhost.
    The launcher supplies a random, single-use bootstrap value; no password is
    stored in the frontend, URL after consumption, source tree, or delivery zip.
    """
    if os.environ.get("TXBUDDY_ENV") != "TEST_ONLY":
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "接口不存在"})
    client = request.client.host if request.client is not None else ""
    if client not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail={"code": "LOCAL_ONLY", "message": "仅允许本机测试入口"})
    expected = os.environ.get("TXBUDDY_TEST_AUTOLOGIN_TOKEN", "")
    if not expected or not constant_time_equals(bootstrap, expected):
        raise HTTPException(status_code=401, detail={"code": "AUTOLOGIN_INVALID", "message": "本机测试入口已失效"})
    with _TEST_AUTOLOGIN_LOCK:
        if bootstrap in _TEST_AUTOLOGIN_USED:
            raise HTTPException(status_code=401, detail={"code": "AUTOLOGIN_USED", "message": "本机测试入口已使用"})
        _TEST_AUTOLOGIN_USED.add(bootstrap)
    user = get_context(request).directory.get(username)
    if user is None or not user.active:
        raise HTTPException(status_code=401, detail={"code": "AUTH_USER_INVALID", "message": "测试账号不可用"})
    context = get_context(request)
    try:
        token = encode_token(subject=user.username, settings=context.settings, roles=user.roles, projects=user.projects)
        payload_checked = decode_token(token, settings=context.settings)
    except TokenError as exc:
        raise HTTPException(status_code=500, detail={"code": exc.code, "message": exc.message}) from None
    context.audit.record(user.username, "AUTH_TEST_AUTO_LOGIN", result="ok")
    return {
        "access_token": token, "token_type": "bearer",
        "expires_in": payload_checked.expires_at - payload_checked.issued_at,
        "expires_at": payload_checked.expires_at, "subject": user.username,
        "roles": list(user.roles), "projects": list(user.projects),
    }


@auth_router.get("/me")
def me(identity: Identity = Depends(require_identity)) -> dict[str, object]:
    """受保护路由：身份由依赖注入，路由体内不做任何令牌解析。"""
    return _identity_view(identity)


@auth_router.post("/logout")
def logout(request: Request, identity: Identity = Depends(require_identity)) -> dict[str, object]:
    context = get_context(request)
    context.revocations.revoke(identity.token_id, identity.expires_at)
    context.audit.record(identity.subject, "AUTH_LOGOUT", target=identity.token_id)
    return {"revoked": True, "token_id": identity.token_id}


@auth_router.get("/audit")
def audit_trail(
    request: Request,
    limit: int = 50,
    identity: Identity = Depends(require_roles("admin")),
) -> dict[str, object]:
    context = get_context(request)
    limit = max(1, min(int(limit), 500))
    rows = context.audit.entries()[-limit:]
    return {"count": len(rows), "entries": rows, "viewer": identity.subject}


@files_router.get("/{file_path:path}")
def download_file(
    request: Request,
    file_path: str,
    identity: Identity = Depends(require_identity),
) -> FileResponse:
    """受保护的文件下载：身份 + 项目 ACL + 路径规范化。"""
    context: SecurityContext = get_context(request)
    assert context.files is not None
    try:
        grant: FileGrant = context.files.resolve(identity, file_path)
    except FileAccessError as exc:
        context.audit.record(identity.subject, "FILE_ACCESS", target=file_path, result="denied", detail=exc.code)
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
            headers={"Cache-Control": "no-store"},
        ) from None
    context.audit.record(identity.subject, "FILE_ACCESS", target=file_path, result="ok", detail=grant.project_id)
    return FileResponse(
        grant.path,
        media_type=grant.media_type,
        filename=grant.path.name,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
            "X-Project-Id": grant.project_id,
            "X-Acl-Role": grant.role.value if grant.role is not None else "",
        },
    )


@files_router.put("/{file_path:path}")
async def upload_file(
    request: Request,
    file_path: str,
    identity: Identity = Depends(require_identity),
) -> dict[str, object]:
    """受控写入：读取需 VIEWER+，写入需 EDITOR/OWNER；权限由项目 ACL 判定。"""
    context: SecurityContext = get_context(request)
    assert context.files is not None
    body = await request.body()
    try:
        grant: FileGrant = context.files.write(identity, file_path, body)
    except FileAccessError as exc:
        context.audit.record(identity.subject, "FILE_WRITE", target=file_path, result="denied", detail=exc.code)
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
            headers={"Cache-Control": "no-store"},
        ) from None
    context.audit.record(identity.subject, "FILE_WRITE", target=file_path, result="ok", detail=grant.project_id)
    return {
        "stored": True,
        "project_id": grant.project_id,
        "size": grant.size,
        "media_type": grant.media_type,
        "role": grant.role.value if grant.role is not None else None,
    }


@internal_router.get("/health")
def internal_health(_: InternalIdentity = Depends(require_internal)) -> dict[str, object]:
    return {"status": "ok", "service": "txbuddy-security", "scope": "internal"}


@internal_router.get("/users/{username}")
def internal_user(
    username: str,
    request: Request,
    _: InternalIdentity = Depends(require_internal),
) -> dict[str, object]:
    context = get_context(request)
    user = context.directory.get(username)
    if user is None:
        raise HTTPException(status_code=404, detail={"code": "AUTH_USER_NOT_FOUND", "message": "用户不存在"})
    return {
        "username": user.username,
        "display_name": user.display_name,
        "roles": list(user.roles),
        "projects": list(user.projects),
        "active": user.active,
    }
