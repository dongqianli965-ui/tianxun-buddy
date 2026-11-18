"""P1-11-AUTH / P1-07-ACL: 身份认证、内部接口鉴权与项目级访问控制。

模块边界：
- ``tokens``      —— HS256 令牌签发/校验（标准库 HMAC，无第三方依赖）
- ``users``       —— PBKDF2 口令哈希与用户目录
- ``acl``         —— 项目-成员-角色（OWNER/EDITOR/REVIEWER/VIEWER）唯一判定点
- ``files``       —— 文件访问策略（防目录穿越 / 经 ACL 的读写授权 / 类型白名单）
- ``deps``        —— FastAPI 依赖：身份注入、内部 token、角色与项目权限守卫
- ``routes``      —— /auth/*、/files/*、/internal/* 路由
- ``acl_routes``  —— /projects/* 项目成员与权限判定路由
- ``app``         —— 子应用装配（也可挂到既有 app）
"""
from __future__ import annotations

from .acl import (AclError, AccessDecision, Membership, PERMISSION_MATRIX,
                  PROJECT_ROLES, Permission, ProjectAcl, ProjectMember,
                  ProjectRole, required_roles)
from .acl_routes import projects_router
from .app import (build_context, create_security_app, demo_acl, demo_directory,
                  install_security)
from .context import (AuditLog, Identity, InternalIdentity, LoginThrottle,
                      SecurityContext, TokenRevocations)
from .deps import (extract_bearer, get_context, optional_identity,
                   require_identity, require_internal,
                   require_project_permission, require_roles)
from .files import FileAccessError, FileAccessPolicy, FileGrant
from .tokens import (SecuritySettings, TokenError, TokenPayload, constant_time_equals,
                     decode_token, encode_token)
from .users import AuthError, PasswordHashError, User, UserDirectory, hash_password, verify_password

__all__ = [
    "AccessDecision",
    "AclError",
    "AuditLog",
    "AuthError",
    "FileAccessError",
    "FileAccessPolicy",
    "FileGrant",
    "Identity",
    "InternalIdentity",
    "LoginThrottle",
    "Membership",
    "PERMISSION_MATRIX",
    "PROJECT_ROLES",
    "PasswordHashError",
    "Permission",
    "ProjectAcl",
    "ProjectMember",
    "ProjectRole",
    "SecurityContext",
    "SecuritySettings",
    "TokenError",
    "TokenPayload",
    "TokenRevocations",
    "User",
    "UserDirectory",
    "build_context",
    "constant_time_equals",
    "create_security_app",
    "decode_token",
    "demo_acl",
    "demo_directory",
    "encode_token",
    "extract_bearer",
    "get_context",
    "hash_password",
    "install_security",
    "optional_identity",
    "projects_router",
    "require_identity",
    "require_internal",
    "require_project_permission",
    "require_roles",
    "required_roles",
    "verify_password",
]
