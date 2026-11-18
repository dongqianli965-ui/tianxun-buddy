"""P1-11-AUTH: FastAPI 依赖注入——身份解析、内部 token 校验、角色守卫。"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request

from .acl import AclError, Permission
from .context import Identity, InternalIdentity, SecurityContext
from .tokens import TokenError, constant_time_equals, decode_token

__all__ = [
    "bearer_realm",
    "get_context",
    "extract_bearer",
    "require_identity",
    "require_internal",
    "require_project_permission",
    "require_roles",
    "optional_identity",
]

bearer_realm = 'Bearer realm="txbuddy"'


def get_context(request: Request) -> SecurityContext:
    context = getattr(request.app.state, "security", None)
    if context is None:  # pragma: no cover - 装配错误属于编程错误
        raise HTTPException(status_code=500, detail={"code": "AUTH_CONTEXT_MISSING", "message": "认证上下文未装配"})
    return context


def _denied(status_code: int, code: str, message: str) -> HTTPException:
    headers = {"WWW-Authenticate": bearer_realm} if status_code == 401 else None
    return HTTPException(status_code=status_code, detail={"code": code, "message": message}, headers=headers)


def extract_bearer(authorization: str | None) -> str:
    if authorization is None or not authorization.strip():
        raise _denied(401, "AUTH_TOKEN_MISSING", "缺少访问令牌")
    scheme, _, credentials = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not credentials.strip():
        raise _denied(401, "AUTH_SCHEME_UNSUPPORTED", "仅支持 Bearer 令牌")
    return credentials.strip()


def require_identity(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Identity:
    """受保护路由的统一入口：无/错/过期/已登出令牌一律 401。"""
    context = get_context(request)
    try:
        token = extract_bearer(authorization)
        payload = decode_token(token, settings=context.settings)
    except TokenError as exc:
        context.audit.record("anonymous", "AUTH_VERIFY", result="denied", detail=exc.code)
        raise _denied(401, exc.code, exc.message) from None
    if context.revocations.is_revoked(payload.token_id):
        context.audit.record(payload.subject, "AUTH_VERIFY", result="denied", detail="AUTH_TOKEN_REVOKED")
        raise _denied(401, "AUTH_TOKEN_REVOKED", "令牌已注销")
    return Identity(
        subject=payload.subject,
        roles=payload.roles,
        projects=payload.projects,
        token_id=payload.token_id,
        expires_at=payload.expires_at,
        method="bearer",
        claims=dict(payload.raw),
    )


def optional_identity(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Identity | None:
    if not authorization:
        return None
    try:
        return require_identity(request, authorization)
    except HTTPException:
        return None


def require_internal(
    request: Request,
    x_internal_token: str | None = Header(default=None),
) -> InternalIdentity:
    """内部接口闸门：缺失或不匹配一律 403（错误码区分缺失/错误，但不回显期望值）。"""
    context = get_context(request)
    if not x_internal_token:
        context.audit.record("anonymous", "INTERNAL_ACCESS", result="denied", detail="AUTH_INTERNAL_TOKEN_MISSING")
        raise _denied(403, "AUTH_INTERNAL_TOKEN_MISSING", "内部接口需要内部 token")
    if context.settings.internal_token in ('','dev-internal','txbuddy-dev-internal-token-change-me') or not constant_time_equals(x_internal_token, context.settings.internal_token):
        context.audit.record("anonymous", "INTERNAL_ACCESS", result="denied", detail="AUTH_INTERNAL_TOKEN_INVALID")
        raise _denied(403, "AUTH_INTERNAL_TOKEN_INVALID", "内部 token 无效")
    return InternalIdentity()


def require_roles(*roles: str):
    """角色守卫工厂：``Depends(require_roles("admin"))`` -> 403。"""
    wanted = tuple(roles)

    def _guard(identity: Identity = Depends(require_identity)) -> Identity:
        if wanted and not identity.has_role(*wanted):
            raise _denied(403, "AUTH_ROLE_FORBIDDEN", "角色不足")
        return identity

    return _guard


def require_project_permission(permission: Permission | str, *, param: str = "project_id"):
    """项目级守卫工厂：按路径参数 ``project_id`` 走 ``ProjectAcl`` 判定。

    判定证据写入 ``request.state.acl_decision``，供路由回显调用者角色；
    拒绝时 403 且错误码来自 ACL（``ACL_NOT_MEMBER`` / ``ACL_ROLE_FORBIDDEN``）。
    """
    wanted = Permission.parse(permission)

    def _guard(request: Request, identity: Identity = Depends(require_identity)) -> Identity:
        context = get_context(request)
        project_id = request.path_params.get(param) or request.query_params.get(param, "")
        try:
            decision = context.decide(identity, str(project_id), wanted)
        except AclError as exc:
            context.audit.record(identity.subject, "PROJECT_ACCESS", target=str(project_id),
                                 result="denied", detail=exc.code)
            raise _denied(exc.status_code, exc.code, exc.message) from None
        request.state.acl_decision = decision
        if not decision.allowed:
            context.audit.record(identity.subject, "PROJECT_ACCESS", target=decision.project_id,
                                 result="denied", detail=decision.code)
            raise _denied(decision.status_code, decision.code, decision.message)
        context.audit.record(identity.subject, "PROJECT_ACCESS", target=decision.project_id,
                             result="ok", detail=decision.role.value if decision.role is not None else "")
        return identity

    return _guard
