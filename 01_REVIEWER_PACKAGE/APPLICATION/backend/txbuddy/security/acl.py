"""P1-07-ACL: 项目-成员-角色访问控制（API 与文件访问的唯一判定点）。

设计要点
--------
* 角色集合固定为 ``OWNER / EDITOR / REVIEWER / VIEWER``，权限矩阵显式声明，
  不依赖"级别越大权限越多"的线性假设（REVIEWER 可审核但不可改写）。
* 判定权威在服务端 ``ProjectAcl``：令牌里的 ``projects`` 仅作为**历史遗留授权**
  的回退来源，显式成员关系优先；吊销成员后旧令牌立即失效。
* ``decide`` 只做判定、不抛业务异常（返回 ``AccessDecision``）；``require`` 在拒绝时
  抛 ``AclError(status_code=403)``。文件策略与 API 守卫都调用同一个 ``decide``。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping

__all__ = [
    "AclError",
    "AccessDecision",
    "Membership",
    "PERMISSION_MATRIX",
    "PROJECT_ROLES",
    "Permission",
    "ProjectAcl",
    "ProjectMember",
    "ProjectRole",
    "required_roles",
]


class AclError(Exception):
    """访问控制异常。``status_code`` 直接用于 HTTP 响应（默认 403）。"""

    def __init__(self, code: str, message: str, status_code: int = 403) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        return f"{self.code}: {self.message}"


class ProjectRole(str, Enum):
    """项目成员角色。视图层可直接序列化为 ``str``。"""

    VIEWER = "VIEWER"
    EDITOR = "EDITOR"
    REVIEWER = "REVIEWER"
    OWNER = "OWNER"

    @classmethod
    def parse(cls, value: "ProjectRole | str") -> "ProjectRole":
        if isinstance(value, cls):
            return value
        if not isinstance(value, str) or not value.strip():
            raise AclError("ACL_ROLE_INVALID", "项目角色不能为空", status_code=400)
        try:
            return cls(value.strip().upper())
        except ValueError:
            allowed = "/".join(role.value for role in cls)
            raise AclError("ACL_ROLE_INVALID", f"未知项目角色，仅支持 {allowed}", status_code=400) from None

    @property
    def level(self) -> int:
        return _ROLE_LEVELS[self]

    @property
    def permissions(self) -> tuple["Permission", ...]:
        return tuple(permission for permission in Permission if self.allows(permission))

    def allows(self, permission: "Permission | str") -> bool:
        return self in PERMISSION_MATRIX[Permission.parse(permission)]

    def can_read(self) -> bool:
        return self.allows(Permission.READ)

    def can_write(self) -> bool:
        return self.allows(Permission.WRITE)

    def can_review(self) -> bool:
        return self.allows(Permission.REVIEW)

    def can_administer(self) -> bool:
        return self.allows(Permission.ADMIN)


class Permission(str, Enum):
    """受控动作。文件下载 = READ，文件写入 = WRITE，签批 = REVIEW，成员管理 = ADMIN。"""

    READ = "READ"
    WRITE = "WRITE"
    REVIEW = "REVIEW"
    ADMIN = "ADMIN"

    @classmethod
    def parse(cls, value: "Permission | str") -> "Permission":
        if isinstance(value, cls):
            return value
        if not isinstance(value, str) or not value.strip():
            raise AclError("ACL_PERMISSION_INVALID", "权限动作不能为空", status_code=400)
        text = value.strip().upper()
        text = _ACTION_ALIASES.get(text, text)
        try:
            return cls(text)
        except ValueError:
            allowed = "/".join(item.value for item in cls)
            raise AclError("ACL_PERMISSION_INVALID", f"未知权限动作，仅支持 {allowed}", status_code=400) from None


_ROLE_LEVELS: dict[ProjectRole, int] = {
    ProjectRole.VIEWER: 10,
    ProjectRole.EDITOR: 20,
    ProjectRole.REVIEWER: 30,
    ProjectRole.OWNER: 40,
}

PERMISSION_MATRIX: dict[Permission, frozenset[ProjectRole]] = {
    Permission.READ: frozenset({ProjectRole.VIEWER, ProjectRole.EDITOR, ProjectRole.REVIEWER, ProjectRole.OWNER}),
    Permission.WRITE: frozenset({ProjectRole.EDITOR, ProjectRole.OWNER}),
    Permission.REVIEW: frozenset({ProjectRole.REVIEWER, ProjectRole.OWNER}),
    Permission.ADMIN: frozenset({ProjectRole.OWNER}),
}

_ACTION_ALIASES: dict[str, str] = {
    "READ": "READ",
    "VIEW": "READ",
    "DOWNLOAD": "READ",
    "WRITE": "WRITE",
    "EDIT": "WRITE",
    "UPLOAD": "WRITE",
    "REVIEW": "REVIEW",
    "APPROVE": "REVIEW",
    "ADMIN": "ADMIN",
    "MANAGE": "ADMIN",
    "DELETE": "ADMIN",
    "OWNER": "ADMIN",
}

_MAX_ID_LENGTH = 128


def required_roles(permission: Permission | str) -> tuple[ProjectRole, ...]:
    """返回允许该动作的角色（按级别升序，便于错误信息展示）。"""
    allowed = PERMISSION_MATRIX[Permission.parse(permission)]
    return tuple(sorted(allowed, key=lambda role: role.level))


def _clean_id(value: object, *, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AclError("ACL_PROJECT_INVALID" if what == "项目" else "ACL_SUBJECT_INVALID",
                       f"{what}标识不能为空", status_code=400)
    text = value.strip()
    if len(text) > _MAX_ID_LENGTH:
        raise AclError("ACL_PROJECT_INVALID" if what == "项目" else "ACL_SUBJECT_INVALID",
                       f"{what}标识超长", status_code=400)
    if any(ch in text for ch in ("/", "\\", "\x00")) or text in (".", ".."):
        # 项目标识同时用于拼文件路径，禁止路径片段以杜绝 ACL 绕过。
        raise AclError("ACL_PROJECT_INVALID" if what == "项目" else "ACL_SUBJECT_INVALID",
                       f"{what}标识含非法字符", status_code=400)
    return text


@dataclass(frozen=True)
class Membership:
    """一条项目成员关系。``role`` 为 ``ProjectRole``，构造时统一规范化。"""

    project_id: str
    subject: str
    role: ProjectRole
    granted_by: str = ""
    granted_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", _clean_id(self.project_id, what="项目"))
        object.__setattr__(self, "subject", _clean_id(self.subject, what="主体"))
        object.__setattr__(self, "role", ProjectRole.parse(self.role))

    @property
    def key(self) -> tuple[str, str]:
        return (self.project_id, self.subject)

    def as_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "subject": self.subject,
            "role": self.role.value,
            "granted_by": self.granted_by,
            "granted_at": self.granted_at,
            "updated_at": self.updated_at,
        }


# 任务命名对齐：project_member 即"项目成员关系"。
ProjectMember = Membership
PROJECT_ROLES: tuple[ProjectRole, ...] = tuple(ProjectRole)


@dataclass(frozen=True)
class AccessDecision:
    """一次授权判定的全部证据（allowed 与 code/reason 一一对应）。"""

    allowed: bool
    code: str
    message: str
    project_id: str
    subject: str
    permission: Permission
    role: ProjectRole | None = None
    reason: str = ""
    status_code: int = 200

    def __bool__(self) -> bool:
        return self.allowed

    def raise_for_denied(self) -> "AccessDecision":
        if not self.allowed:
            raise AclError(self.code, self.message, self.status_code)
        return self

    def as_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "message": self.message,
            "project_id": self.project_id,
            "subject": self.subject,
            "permission": self.permission.value,
            "role": self.role.value if self.role is not None else None,
            "reason": self.reason,
            "status_code": self.status_code,
        }


@dataclass
class ProjectAcl:
    """项目成员关系存储 + 唯一授权判定器（线程安全、显式成员优先）。

    ``legacy_project_role`` 只为兼容既有 ``identity.projects`` 授权模型而存在：
    当某项目没有显式成员记录、但令牌 ``projects`` 声明包含它时，按该角色放行
    （默认 ``EDITOR``）。显式成员记录永远优先，因此吊销成员后旧令牌立即失效。
    """

    legacy_project_role: ProjectRole | None = ProjectRole.EDITOR
    admin_roles: tuple[str, ...] = ("admin",)
    memberships: dict[tuple[str, str], Membership] = field(default_factory=dict, repr=False)
    _lock: object = field(default_factory=threading.RLock, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.legacy_project_role is not None:
            self.legacy_project_role = ProjectRole.parse(self.legacy_project_role)
        self.admin_roles = tuple(str(role).strip().lower() for role in self.admin_roles if str(role).strip())
        self.memberships = {membership.key: membership for membership in self.memberships.values()}

    # ---------------------------------------------------------------- 成员管理
    @classmethod
    def from_mapping(
        cls,
        mapping: Mapping[str, Mapping[str, "ProjectRole | str"]],
        *,
        granted_by: str = "bootstrap",
        now: float | None = None,
        **kwargs: object,
    ) -> "ProjectAcl":
        acl = cls(**kwargs)  # type: ignore[arg-type]
        stamp = time.time() if now is None else float(now)
        for project_id, members in mapping.items():
            for subject, role in members.items():
                acl.grant(project_id, subject, role, granted_by=granted_by, now=stamp)
        return acl

    def grant(
        self,
        project_id: str,
        subject: str,
        role: ProjectRole | str,
        *,
        granted_by: str = "",
        now: float | None = None,
    ) -> Membership:
        """新增或改派成员角色（幂等 upsert）。"""
        stamp = time.time() if now is None else float(now)
        project_key = _clean_id(project_id, what="项目")
        subject_key = _clean_id(subject, what="主体")
        parsed = ProjectRole.parse(role)
        with self._lock:  # type: ignore[attr-defined]
            existing = self.memberships.get((project_key, subject_key))
            membership = Membership(
                project_id=project_key,
                subject=subject_key,
                role=parsed,
                granted_by=granted_by or (existing.granted_by if existing else ""),
                granted_at=existing.granted_at if existing else stamp,
                updated_at=stamp,
            )
            self.memberships[membership.key] = membership
            return membership

    def revoke(self, project_id: str, subject: str) -> bool:
        """删除成员关系；返回是否真的删除了记录。"""
        key = (_clean_id(project_id, what="项目"), _clean_id(subject, what="主体"))
        with self._lock:  # type: ignore[attr-defined]
            return self.memberships.pop(key, None) is not None

    def membership(self, project_id: str, subject: str) -> Membership | None:
        try:
            key = (_clean_id(project_id, what="项目"), _clean_id(subject, what="主体"))
        except AclError:
            return None
        with self._lock:  # type: ignore[attr-defined]
            return self.memberships.get(key)

    def role_of(self, project_id: str, subject: str) -> ProjectRole | None:
        found = self.membership(project_id, subject)
        return found.role if found is not None else None

    def members(self, project_id: str) -> tuple[Membership, ...]:
        project_key = _clean_id(project_id, what="项目")
        with self._lock:  # type: ignore[attr-defined]
            rows = [item for item in self.memberships.values() if item.project_id == project_key]
        return tuple(sorted(rows, key=lambda item: (-item.role.level, item.subject)))

    def memberships_of(self, subject: str) -> tuple[Membership, ...]:
        subject_key = _clean_id(subject, what="主体")
        with self._lock:  # type: ignore[attr-defined]
            rows = [item for item in self.memberships.values() if item.subject == subject_key]
        return tuple(sorted(rows, key=lambda item: item.project_id))

    def projects_of(self, subject: str) -> tuple[str, ...]:
        return tuple(item.project_id for item in self.memberships_of(subject))

    def clear(self) -> None:
        with self._lock:  # type: ignore[attr-defined]
            self.memberships.clear()

    def __len__(self) -> int:
        with self._lock:  # type: ignore[attr-defined]
            return len(self.memberships)

    def __contains__(self, key: object) -> bool:
        return key in self.memberships

    # ---------------------------------------------------------------- 判定
    def decide(
        self,
        subject: str,
        project_id: str,
        permission: Permission | str,
        *,
        roles: Iterable[str] = (),
        legacy_projects: Iterable[str] = (),
        use_legacy_projects: bool = True,
    ) -> AccessDecision:
        """单点判定：返回 ``AccessDecision``。仅标识非法时抛 ``AclError``（400）。"""
        project_key = _clean_id(project_id, what="项目")
        subject_key = _clean_id(subject, what="主体")
        parsed = Permission.parse(permission)
        granted_roles = {str(role).strip().lower() for role in roles if str(role).strip()}

        def deny(code: str, message: str, role: ProjectRole | None = None) -> AccessDecision:
            return AccessDecision(
                allowed=False, code=code, message=message, project_id=project_key,
                subject=subject_key, permission=parsed, role=role,
                reason="not-member" if code == "ACL_NOT_MEMBER" else "role-forbidden",
                status_code=403,
            )

        if any(admin in granted_roles for admin in self.admin_roles):
            return AccessDecision(
                allowed=True, code="ACL_ALLOWED_ADMIN", message="全局管理员放行",
                project_id=project_key, subject=subject_key, permission=parsed,
                role=ProjectRole.OWNER, reason="global-admin",
            )

        found = self.membership(project_key, subject_key)
        if found is not None:
            if found.role.allows(parsed):
                return AccessDecision(
                    allowed=True, code="ACL_ALLOWED", message=f"项目成员 {found.role.value} 放行",
                    project_id=project_key, subject=subject_key, permission=parsed,
                    role=found.role, reason="member",
                )
            needed = "/".join(role.value for role in required_roles(parsed))
            return deny("ACL_ROLE_FORBIDDEN", f"角色 {found.role.value} 无权执行 {parsed.value}，需 {needed}", found.role)

        legacy = {str(item) for item in legacy_projects}
        if use_legacy_projects and self.legacy_project_role is not None and project_key in legacy:
            role = self.legacy_project_role
            if role.allows(parsed):
                return AccessDecision(
                    allowed=True, code="ACL_ALLOWED_LEGACY", message=f"历史项目授权 {role.value} 放行",
                    project_id=project_key, subject=subject_key, permission=parsed,
                    role=role, reason="legacy-project-grant",
                )
            needed = "/".join(item.value for item in required_roles(parsed))
            return deny("ACL_ROLE_FORBIDDEN", f"历史授权 {role.value} 无权执行 {parsed.value}，需 {needed}", role)

        return deny("ACL_NOT_MEMBER", f"{subject_key} 不是项目 {project_key} 的成员")

    def decide_identity(
        self,
        identity: object,
        project_id: str,
        permission: Permission | str,
        *,
        use_legacy_projects: bool = True,
    ) -> AccessDecision:
        subject = getattr(identity, "subject", "")
        if not isinstance(subject, str) or not subject.strip():
            raise AclError("ACL_SUBJECT_INVALID", "身份主体缺失", status_code=401)
        return self.decide(
            subject,
            project_id,
            permission,
            roles=tuple(getattr(identity, "roles", ()) or ()),
            legacy_projects=tuple(getattr(identity, "projects", ()) or ()),
            use_legacy_projects=use_legacy_projects,
        )

    def require(
        self,
        subject: str,
        project_id: str,
        permission: Permission | str,
        *,
        roles: Iterable[str] = (),
        legacy_projects: Iterable[str] = (),
        use_legacy_projects: bool = True,
    ) -> AccessDecision:
        return self.decide(
            subject, project_id, permission, roles=roles,
            legacy_projects=legacy_projects, use_legacy_projects=use_legacy_projects,
        ).raise_for_denied()

    def require_identity(
        self,
        identity: object,
        project_id: str,
        permission: Permission | str,
        *,
        use_legacy_projects: bool = True,
    ) -> AccessDecision:
        return self.decide_identity(
            identity, project_id, permission, use_legacy_projects=use_legacy_projects
        ).raise_for_denied()
