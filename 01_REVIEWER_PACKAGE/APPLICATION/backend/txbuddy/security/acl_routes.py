"""P1-07-ACL: 项目成员与访问控制 API（全部经 ``ProjectAcl`` 判定）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .acl import AccessDecision, AclError, Permission, ProjectRole
from .context import Identity, SecurityContext
from .deps import get_context, require_identity, require_project_permission
from .users import UserDirectory

__all__ = ["projects_router"]

projects_router = APIRouter(prefix="/projects", tags=["acl"])


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=128)
    role: str = Field(min_length=1, max_length=32)


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    permission: str = Field(default="READ", max_length=32)


def _decision(request: Request) -> AccessDecision:
    found = getattr(request.state, "acl_decision", None)
    if found is None:  # pragma: no cover - 路由与守卫装配错误
        raise HTTPException(status_code=500, detail={"code": "ACL_DECISION_MISSING", "message": "缺少判定结果"})
    return found


def _permission_view() -> dict[str, list[str]]:
    return {
        permission.value: sorted(role.value for role in ProjectRole if role.allows(permission))
        for permission in Permission
    }


def _bad_request(exc: AclError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message})


@projects_router.get("/{project_id}")
def get_project(
    project_id: str,
    request: Request,
    identity: Identity = Depends(require_project_permission(Permission.READ)),
) -> dict[str, object]:
    """项目读取入口：非成员/无权一律 403（不暴露项目是否存在）。"""
    decision = _decision(request)
    context = get_context(request)
    assert context.acl is not None
    return {
        "project_id": decision.project_id,
        "subject": identity.subject,
        "role": decision.role.value if decision.role is not None else None,
        "permission": decision.permission.value,
        "decision": decision.as_dict(),
        "member_count": len(context.acl.members(project_id)),
        "permissions": _permission_view(),
    }


@projects_router.post("/{project_id}/check")
def check_permission(
    project_id: str,
    payload: CheckRequest,
    request: Request,
    identity: Identity = Depends(require_identity),
) -> dict[str, object]:
    """显式判定函数入口：返回 allowed/role/reason，便于前端灰化与审计。"""
    context: SecurityContext = get_context(request)
    try:
        decision = context.decide(identity, project_id, payload.permission)
    except AclError as exc:
        raise _bad_request(exc) from None
    context.audit.record(identity.subject, "ACL_CHECK", target=project_id,
                         result="ok" if decision.allowed else "denied", detail=decision.code)
    return decision.as_dict()


@projects_router.get("/{project_id}/members")
def list_members(
    project_id: str,
    request: Request,
    identity: Identity = Depends(require_project_permission(Permission.REVIEW)),
) -> dict[str, object]:
    """成员清单：VIEWER 不可见，需 REVIEW 及以上的项目内部治理权限。"""
    context = get_context(request)
    assert context.acl is not None
    decision = _decision(request)
    rows = [item.as_dict() for item in context.acl.members(project_id)]
    return {
        "project_id": project_id,
        "viewer": identity.subject,
        "viewer_role": decision.role.value if decision.role is not None else None,
        "count": len(rows),
        "members": rows,
    }


@projects_router.post("/{project_id}/members")
def grant_member(
    project_id: str,
    payload: GrantRequest,
    request: Request,
    identity: Identity = Depends(require_project_permission(Permission.ADMIN)),
) -> dict[str, object]:
    """成员授权：仅 OWNER（或全局 admin）可授予/改派角色。"""
    context: SecurityContext = get_context(request)
    assert context.acl is not None
    try:
        role = ProjectRole.parse(payload.role)
    except AclError as exc:
        raise _bad_request(exc) from None
    directory: UserDirectory = context.directory
    if directory.get(payload.subject) is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "ACL_SUBJECT_UNKNOWN", "message": "目标用户不存在"},
        )
    membership = context.acl.grant(project_id, payload.subject, role, granted_by=identity.subject)
    context.audit.record(identity.subject, "ACL_GRANT", target=f"{project_id}/{payload.subject}",
                         result="ok", detail=role.value)
    return {"granted": True, "membership": membership.as_dict()}


@projects_router.delete("/{project_id}/members/{subject}")
def revoke_member(
    project_id: str,
    subject: str,
    request: Request,
    identity: Identity = Depends(require_project_permission(Permission.ADMIN)),
) -> dict[str, object]:
    """吊销成员：不允许吊销最后一个 OWNER（避免项目失去治理人）。"""
    context: SecurityContext = get_context(request)
    assert context.acl is not None
    current = context.acl.membership(project_id, subject)
    if current is not None and current.role is ProjectRole.OWNER:
        owners = [item for item in context.acl.members(project_id) if item.role is ProjectRole.OWNER]
        if len(owners) <= 1:
            raise HTTPException(
                status_code=409,
                detail={"code": "ACL_LAST_OWNER", "message": "不能吊销项目唯一的 OWNER"},
            )
    removed = context.acl.revoke(project_id, subject)
    context.audit.record(identity.subject, "ACL_REVOKE", target=f"{project_id}/{subject}",
                         result="ok" if removed else "noop")
    return {"revoked": removed, "project_id": project_id, "subject": subject}
