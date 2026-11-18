"""P1-11-AUTH: 用户目录与口令校验（PBKDF2-HMAC-SHA256，标准库实现）。"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from typing import Iterable, Mapping

__all__ = [
    "PasswordHashError",
    "AuthError",
    "hash_password",
    "verify_password",
    "User",
    "UserDirectory",
]

_SCHEME = "pbkdf2_sha256"
_DEFAULT_ITERATIONS = 120_000
_DUMMY_HASH = (
    "pbkdf2_sha256$120000$"
    "00000000000000000000000000000000$"
    "0000000000000000000000000000000000000000000000000000000000000000"
)


class PasswordHashError(Exception):
    """口令哈希串格式非法。"""


class AuthError(Exception):
    """认证失败。对外只暴露统一错误码，避免用户名枚举。"""

    def __init__(self, code: str = "AUTH_CREDENTIALS_INVALID", message: str = "用户名或口令错误") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def hash_password(password: str, *, salt: str | None = None, iterations: int = _DEFAULT_ITERATIONS) -> str:
    if not isinstance(password, str) or not password:
        raise PasswordHashError("口令不能为空")
    if iterations < 10_000:
        raise PasswordHashError("PBKDF2 迭代次数过低")
    salt_hex = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), iterations)
    return f"{_SCHEME}${iterations}${salt_hex}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """常量时间校验；格式非法返回 False（不抛异常，避免泄漏内部结构）。"""
    if not isinstance(password, str) or not isinstance(encoded, str):
        return False
    try:
        scheme, iterations_text, salt_hex, digest_hex = encoded.split("$")
        iterations = int(iterations_text)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    if scheme != _SCHEME or iterations < 1 or not salt or not expected:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


@dataclass(frozen=True)
class User:
    username: str
    password_hash: str
    roles: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    active: bool = True
    display_name: str = ""

    def has_role(self, *roles: str) -> bool:
        return any(role in self.roles for role in roles)


@dataclass
class UserDirectory:
    """内存用户目录。测试与本地部署用；生产应替换为外部身份源。"""

    users: dict[str, User] = field(default_factory=dict)

    @classmethod
    def from_plaintext(cls, entries: Mapping[str, Mapping[str, object]], *, iterations: int = _DEFAULT_ITERATIONS) -> "UserDirectory":
        """用明文口令构造目录（仅初始化用，落盘一律存哈希）。"""
        users: dict[str, User] = {}
        for username, spec in entries.items():
            password = spec.get("password")
            if not isinstance(password, str) or not password:
                raise PasswordHashError(f"用户 {username} 缺少口令")
            users[username] = User(
                username=username,
                password_hash=hash_password(password, iterations=iterations),
                roles=tuple(spec.get("roles", ()) or ()),
                projects=tuple(spec.get("projects", ()) or ()),
                active=bool(spec.get("active", True)),
                display_name=str(spec.get("display_name", username)),
            )
        return cls(users=users)

    def get(self, username: str) -> User | None:
        return self.users.get(username)

    def add(self, user: User) -> None:
        self.users[user.username] = user

    def usernames(self) -> Iterable[str]:
        return tuple(self.users)

    def authenticate(self, username: str, password: str) -> User:
        """校验凭据。未知用户也走一次 PBKDF2，避免时间侧信道。"""
        user = self.users.get(username) if isinstance(username, str) else None
        stored = user.password_hash if user is not None else _DUMMY_HASH
        ok = verify_password(password, stored)
        if user is None or not ok:
            raise AuthError()
        if not user.active:
            raise AuthError("AUTH_USER_DISABLED", "账号已停用")
        return user
