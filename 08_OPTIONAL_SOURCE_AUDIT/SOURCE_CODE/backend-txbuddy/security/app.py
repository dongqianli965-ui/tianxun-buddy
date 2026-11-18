"""P1-11-AUTH: 认证子应用装配。

本模块提供两种装配方式：

* ``create_security_app(...)`` —— 独立的安全子应用（登录/令牌/文件/内部接口）；
* ``install_security(app, context, prefix="/security")`` —— 把安全子应用 Mount 到任意既有
  FastAPI 应用（含 ``txbuddy.api.app``），不修改业务入口文件、不污染父应用路由清单。

生产环境必须通过 ``TXBUDDY_JWT_SECRET`` / ``TXBUDDY_INTERNAL_TOKEN`` 注入密钥；
下面的默认用户与默认密钥只用于本地开发与测试。
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from .acl import ProjectAcl, ProjectRole
from .acl_routes import projects_router
from .context import AuditLog, LoginThrottle, SecurityContext
from .files import FileAccessPolicy
from .routes import auth_router, files_router, internal_router
from .tokens import SecuritySettings
from .users import UserDirectory

__all__ = ["create_security_app", "install_security", "build_context", "demo_acl", "demo_directory", "app"]


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """给安全子应用的每个响应补上基础安全响应头。"""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response


# 本地开发/演示账号。生产环境应改为外部身份源，禁止沿用这些口令。
DEMO_USERS: dict[str, dict[str, object]] = {
    "estimator": {
        "password": "Estimator#2026",
        "roles": ("estimator",),
        "projects": ("P-001",),
        "display_name": "预算员",
    },
    "auditor": {
        "password": "Auditor#2026",
        "roles": ("auditor",),
        "projects": ("P-001", "P-002"),
        "display_name": "审核员",
    },
    "admin": {
        "password": "Admin#2026",
        "roles": ("admin",),
        "projects": (),
        "display_name": "系统管理员",
    },
    "disabled": {
        "password": "Disabled#2026",
        "roles": ("estimator",),
        "projects": ("P-001",),
        "active": False,
        "display_name": "已停用账号",
    },
}


def demo_directory() -> UserDirectory:
    return UserDirectory.from_plaintext(DEMO_USERS)


# 演示项目的成员关系（显式成员优先于令牌里的 projects 历史声明）。
DEMO_MEMBERSHIPS: dict[str, dict[str, ProjectRole]] = {
    "P-001": {
        "estimator": ProjectRole.EDITOR,
        "auditor": ProjectRole.REVIEWER,
        "admin": ProjectRole.OWNER,
    },
    "P-002": {
        "auditor": ProjectRole.VIEWER,
        "admin": ProjectRole.OWNER,
    },
}


def demo_acl() -> ProjectAcl:
    return ProjectAcl.from_mapping(DEMO_MEMBERSHIPS, granted_by="bootstrap")


def build_context(
    *,
    settings: SecuritySettings | None = None,
    directory: UserDirectory | None = None,
    file_root: str | Path | None = None,
    max_failures: int = 5,
    acl: ProjectAcl | None = None,
) -> SecurityContext:
    settings = settings or SecuritySettings.from_env()
    resolved_root = file_root if file_root is not None else (settings.file_root or Path.cwd() / "data" / "files")
    resolved_acl = acl if acl is not None else ProjectAcl()
    return SecurityContext(
        settings=settings,
        directory=directory if directory is not None else UserDirectory(),
        files=FileAccessPolicy(Path(resolved_root), acl=resolved_acl),
        acl=resolved_acl,
        audit=AuditLog(),
        throttle=LoginThrottle(max_failures=max_failures),
    )


def _normalize_prefix(prefix: str) -> str:
    if not isinstance(prefix, str) or not prefix.startswith("/"):
        raise ValueError("安全子应用挂载前缀必须以 / 开头")
    cleaned = prefix.rstrip("/")
    if not cleaned:
        raise ValueError("安全子应用不能挂载在根路径 /")
    return cleaned


def install_security(app: FastAPI, context: SecurityContext, *, prefix: str = "/security") -> SecurityContext:
    """把认证子应用挂到既有业务应用（如 ``txbuddy.api.app``）的 ``prefix`` 下。

    用 ``Mount`` 而不是平铺路由，原因有两条：

    1. 既有回归用例遍历 ``app.routes`` 并断言每个 POST 入口都已登记
       （``test_v423_numeric_all_engines.test_entry_inventory_covers_every_post_route``）；
       平铺会在父应用里凭空多出 ``/auth/login`` 等 POST 入口，属于对既有治理清单的破坏。
    2. 挂载后父应用路由表只多一个无 ``methods`` 的 Mount，不影响任何既有断言。

    挂载后的地址形如 ``/security/auth/login``、``/security/files/...``、
    ``/security/internal/health``。独立部署仍用 ``create_security_app()``（路径不带前缀）。
    """
    mounted = _normalize_prefix(prefix)
    sub_app = create_security_app(
        settings=context.settings,
        directory=context.directory,
        file_root=context.files.root if context.files is not None else None,
        context=context,
    )
    app.mount(mounted, sub_app)
    return context


def create_security_app(
    *,
    settings: SecuritySettings | None = None,
    directory: UserDirectory | None = None,
    file_root: str | Path | None = None,
    context: SecurityContext | None = None,
    acl: ProjectAcl | None = None,
) -> FastAPI:
    app = FastAPI(title="Tianxun Buddy Security", version="0.4.3")
    app.add_middleware(SecurityHeadersMiddleware)
    resolved = context or build_context(settings=settings, directory=directory, file_root=file_root, acl=acl)
    return _install_routes(app, resolved)


def _install_routes(app: FastAPI, context: SecurityContext) -> FastAPI:
    app.state.security = context
    for router in (auth_router, files_router, internal_router, projects_router):
        app.include_router(router)
    return app


app = create_security_app(
    settings=SecuritySettings.from_env(),
    file_root=os.environ.get("TXBUDDY_FILE_ROOT") or Path(__file__).resolve().parents[2] / "data" / "files",
    directory=UserDirectory(),
    acl=ProjectAcl(),
)
