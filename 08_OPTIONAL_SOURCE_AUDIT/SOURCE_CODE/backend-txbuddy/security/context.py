"""P1-11-AUTH: 认证上下文（身份、审计、登录限速、令牌吊销）。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .acl import AccessDecision, Permission, ProjectAcl
from .files import FileAccessPolicy
from .tokens import SecuritySettings
from .users import UserDirectory

__all__ = [
    "Identity",
    "InternalIdentity",
    "AuditLog",
    "LoginThrottle",
    "TokenRevocations",
    "SecurityContext",
]


@dataclass(frozen=True)
class Identity:
    """受保护路由注入的身份对象。"""

    subject: str
    roles: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    token_id: str = ""
    expires_at: int = 0
    method: str = "bearer"
    claims: dict[str, Any] = field(default_factory=dict, repr=False)

    def has_role(self, *roles: str) -> bool:
        return any(role in self.roles for role in roles)

    def can_access_project(self, project_id: str) -> bool:
        return "admin" in self.roles or project_id in self.projects


@dataclass(frozen=True)
class InternalIdentity:
    """内部接口身份；与用户身份严格区分，用户令牌不得用于内部接口。"""

    subject: str = "internal-service"
    roles: tuple[str, ...] = ("internal",)
    projects: tuple[str, ...] = ()
    method: str = "internal-token"


class AuditLog:
    """线程安全的进程内审计日志（认证/授权事件）。"""

    def __init__(self, max_entries: int = 2000) -> None:
        self._entries: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._max_entries = max(1, int(max_entries))
        self._seq = 0

    def record(self, actor: str, action: str, *, target: str = "", result: str = "ok", detail: str = "") -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            entry = {
                "seq": self._seq,
                "actor": actor or "anonymous",
                "action": action,
                "target": target,
                "result": result,
                "detail": detail,
                "at": time.time(),
            }
            self._entries.append(entry)
            if len(self._entries) > self._max_entries:
                del self._entries[: len(self._entries) - self._max_entries]
            return dict(entry)

    def entries(self, action: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = [dict(row) for row in self._entries]
        if action is None:
            return rows
        return [row for row in rows if row["action"] == action]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class LoginThrottle:
    """按用户名 + 客户端键的失败计数锁定，抑制在线口令爆破。"""

    def __init__(self, *, max_failures: int = 5, window_seconds: float = 300.0, lockout_seconds: float = 60.0) -> None:
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self.max_failures = max(1, int(max_failures))
        self.window_seconds = float(window_seconds)
        self.lockout_seconds = float(lockout_seconds)

    def _key(self, username: str, client: str) -> str:
        return f"{username.strip().lower()}@{client}"

    def check(self, username: str, client: str = "", *, now: float | None = None) -> float:
        """返回剩余锁定秒数；0 表示未锁定。"""
        current = time.monotonic() if now is None else now
        key = self._key(username, client)
        with self._lock:
            until = self._locked_until.get(key)
            if until is None:
                return 0.0
            if current >= until:
                self._locked_until.pop(key, None)
                self._failures.pop(key, None)
                return 0.0
            return until - current

    def record_failure(self, username: str, client: str = "", *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        key = self._key(username, client)
        with self._lock:
            stamps = [stamp for stamp in self._failures.get(key, []) if current - stamp <= self.window_seconds]
            stamps.append(current)
            if len(stamps) >= self.max_failures:
                self._locked_until[key] = current + self.lockout_seconds
                self._failures[key] = []
            else:
                self._failures[key] = stamps

    def reset(self, username: str, client: str = "") -> None:
        key = self._key(username, client)
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)


class TokenRevocations:
    """已登出令牌的 jti 集合（进程内，按 exp 清理）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._revoked: dict[str, int] = {}

    def revoke(self, token_id: str, expires_at: int) -> None:
        if not token_id:
            return
        with self._lock:
            self._revoked[token_id] = int(expires_at)

    def is_revoked(self, token_id: str, *, now: int | None = None) -> bool:
        if not token_id:
            return False
        current = int(time.time()) if now is None else int(now)
        with self._lock:
            expired = [key for key, exp in self._revoked.items() if exp < current - 60]
            for key in expired:
                self._revoked.pop(key, None)
            return token_id in self._revoked

    def __len__(self) -> int:
        with self._lock:
            return len(self._revoked)


@dataclass
class SecurityContext:
    settings: SecuritySettings = field(default_factory=SecuritySettings)
    directory: UserDirectory = field(default_factory=UserDirectory)
    files: FileAccessPolicy | None = None
    acl: ProjectAcl | None = None
    audit: AuditLog = field(default_factory=AuditLog)
    throttle: LoginThrottle = field(default_factory=LoginThrottle)
    revocations: TokenRevocations = field(default_factory=TokenRevocations)

    def __post_init__(self) -> None:
        # 统一授权：上下文、文件策略与 API 守卫共享同一个 ProjectAcl 实例。
        if self.acl is None:
            self.acl = self.files.acl if self.files is not None else ProjectAcl()
        if self.files is None:
            root = Path(self.settings.file_root or Path.cwd() / "data" / "files")
            self.files = FileAccessPolicy(root, acl=self.acl)
        elif self.files.acl is not self.acl:
            self.files = replace(self.files, acl=self.acl)

    def decide(self, identity: object, project_id: str, permission: Permission | str) -> AccessDecision:
        """API 与文件共用的判定入口；拒绝时返回 decision，不抛异常。"""
        assert self.acl is not None  # __post_init__ 保证
        return self.acl.decide_identity(identity, project_id, permission)

    def require(self, identity: object, project_id: str, permission: Permission | str) -> AccessDecision:
        assert self.acl is not None  # __post_init__ 保证
        return self.acl.require_identity(identity, project_id, permission)
