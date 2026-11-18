"""P1-11-AUTH: HMAC-SHA256 (HS256) JWT issuing / verification.

纯标准库实现，避免新增第三方依赖（本机 .venv 无 PyJWT）。
令牌格式为紧凑 JWT：``base64url(header).base64url(payload).base64url(sig)``。

安全要点：
- 只接受 alg=HS256；拒绝 ``none``/RS256 等任何其他算法（防算法混淆）；
- 签名用 ``hmac.compare_digest`` 常量时间比较；
- exp/nbf 强校验并带小 leeway；iss 必须匹配；
- 载荷必须是 JSON 对象，sub 必须为非空字符串；
- 任何解析/校验失败统一抛 ``TokenError``，绝不回显原始令牌。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "ALGORITHM",
    "TokenError",
    "TokenPayload",
    "SecuritySettings",
    "encode_token",
    "decode_token",
    "constant_time_equals",
]


ALGORITHM = "HS256"


class TokenError(Exception):
    """令牌无法签发/校验。``code`` 为稳定的业务错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        return f"{self.code}: {self.message}"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (ValueError, TypeError) as exc:  # base64 非法
        raise TokenError("AUTH_TOKEN_INVALID", "令牌编码非法") from exc


def constant_time_equals(left: str, right: str) -> bool:
    """字符串常量时间比较（先各自 SHA-256 归一化长度）。"""
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    digest_left = hashlib.sha256(left.encode("utf-8")).digest()
    digest_right = hashlib.sha256(right.encode("utf-8")).digest()
    return hmac.compare_digest(digest_left, digest_right)


@dataclass(frozen=True)
class SecuritySettings:
    """认证配置。生产环境必须通过环境变量注入，不得使用默认开发密钥。"""

    secret: str = field(default_factory=lambda: secrets.token_urlsafe(48))
    internal_token: str = ""
    issuer: str = "txbuddy.security"
    ttl_seconds: int = 3600
    leeway_seconds: int = 0
    file_root: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SecuritySettings":
        import os

        source = os.environ if env is None else env
        return cls(
            secret=source.get("TXBUDDY_JWT_SECRET") or secrets.token_urlsafe(48),
            internal_token=source.get("TXBUDDY_INTERNAL_TOKEN", cls.internal_token),
            issuer=source.get("TXBUDDY_JWT_ISSUER", cls.issuer),
            ttl_seconds=int(source.get("TXBUDDY_JWT_TTL", cls.ttl_seconds)),
            leeway_seconds=int(source.get("TXBUDDY_JWT_LEEWAY", cls.leeway_seconds)),
            file_root=source.get("TXBUDDY_FILE_ROOT", cls.file_root),
        )


@dataclass(frozen=True)
class TokenPayload:
    subject: str
    roles: tuple[str, ...]
    projects: tuple[str, ...]
    issued_at: int
    expires_at: int
    issuer: str
    token_id: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def has_role(self, *roles: str) -> bool:
        return any(role in self.roles for role in roles)

    def can_access_project(self, project_id: str) -> bool:
        if "admin" in self.roles:
            return True
        return project_id in self.projects


def _sign(secret: str, signing_input: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    return _b64e(digest)


def encode_token(
    *,
    subject: str,
    settings: SecuritySettings,
    roles: tuple[str, ...] = (),
    projects: tuple[str, ...] = (),
    ttl_seconds: int | None = None,
    now: int | None = None,
    extra_claims: Mapping[str, Any] | None = None,
) -> str:
    """签发 HS256 令牌。``ttl_seconds`` 为负时应由调用方拒绝（这里统一校验）。"""
    if not isinstance(subject, str) or not subject.strip():
        raise TokenError("AUTH_TOKEN_SUBJECT_INVALID", "令牌主体不能为空")
    ttl = settings.ttl_seconds if ttl_seconds is None else ttl_seconds
    if not isinstance(ttl, int) or ttl <= 0:
        raise TokenError("AUTH_TOKEN_TTL_INVALID", "令牌有效期必须为正整数秒")

    issued_at = int(time.time()) if now is None else int(now)
    payload: dict[str, Any] = {
        "sub": subject,
        "iss": settings.issuer,
        "iat": issued_at,
        "nbf": issued_at,
        "exp": issued_at + ttl,
        "jti": secrets.token_hex(8),
        "roles": list(roles),
        "projects": list(projects),
    }
    if extra_claims:
        reserved = {"sub", "iss", "iat", "nbf", "exp", "jti", "roles", "projects"}
        for key, value in extra_claims.items():
            if key in reserved:
                raise TokenError("AUTH_TOKEN_CLAIM_RESERVED", f"保留声明不可覆盖: {key}")
            payload[key] = value

    header = {"alg": ALGORITHM, "typ": "JWT"}
    header_part = _b64e(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    payload_part = _b64e(
        json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode("utf-8")
    )
    signing_input = f"{header_part}.{payload_part}"
    return f"{signing_input}.{_sign(settings.secret, signing_input)}"


def _decode_json(segment: str, what: str) -> dict[str, Any]:
    raw = _b64d(segment)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TokenError("AUTH_TOKEN_INVALID", f"{what}不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise TokenError("AUTH_TOKEN_INVALID", f"{what}必须是 JSON 对象")
    return data


def decode_token(
    token: str,
    *,
    settings: SecuritySettings,
    now: int | None = None,
    expected_issuer: str | None = None,
) -> TokenPayload:
    """校验并解析令牌；失败抛 ``TokenError``（过期码为 ``AUTH_TOKEN_EXPIRED``）。"""
    if not isinstance(token, str) or not token.strip():
        raise TokenError("AUTH_TOKEN_MISSING", "缺少访问令牌")
    parts = token.strip().split(".")
    if len(parts) != 3 or not all(parts):
        raise TokenError("AUTH_TOKEN_INVALID", "令牌结构非法")
    header_part, payload_part, signature_part = parts

    header = _decode_json(header_part, "令牌头")
    if header.get("alg") != ALGORITHM:
        raise TokenError("AUTH_TOKEN_ALG_REJECTED", "仅接受 HS256 算法")
    if header.get("typ") not in (None, "JWT"):
        raise TokenError("AUTH_TOKEN_ALG_REJECTED", "令牌类型非法")

    signing_input = f"{header_part}.{payload_part}"
    expected = _sign(settings.secret, signing_input)
    if not hmac.compare_digest(expected, signature_part):
        raise TokenError("AUTH_TOKEN_SIGNATURE_INVALID", "令牌签名不匹配")

    payload = _decode_json(payload_part, "令牌载荷")
    now_ts = int(time.time()) if now is None else int(now)
    leeway = settings.leeway_seconds

    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise TokenError("AUTH_TOKEN_INVALID", "缺少 exp 声明")
    if now_ts > exp + leeway:
        raise TokenError("AUTH_TOKEN_EXPIRED", "令牌已过期")

    nbf = payload.get("nbf")
    if nbf is not None:
        if not isinstance(nbf, int):
            raise TokenError("AUTH_TOKEN_INVALID", "nbf 声明非法")
        if now_ts + leeway < nbf:
            raise TokenError("AUTH_TOKEN_NOT_YET_VALID", "令牌尚未生效")

    issuer = payload.get("iss")
    required_issuer = settings.issuer if expected_issuer is None else expected_issuer
    if not isinstance(issuer, str) or issuer != required_issuer:
        raise TokenError("AUTH_TOKEN_ISSUER_MISMATCH", "令牌签发方不匹配")

    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject.strip():
        raise TokenError("AUTH_TOKEN_INVALID", "令牌缺少 sub")

    def _str_tuple(value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise TokenError("AUTH_TOKEN_INVALID", "roles/projects 声明必须是字符串数组")
        return tuple(value)

    issued_at = payload.get("iat")
    if not isinstance(issued_at, int):
        raise TokenError("AUTH_TOKEN_INVALID", "缺少 iat 声明")

    return TokenPayload(
        subject=subject,
        roles=_str_tuple(payload.get("roles")),
        projects=_str_tuple(payload.get("projects")),
        issued_at=issued_at,
        expires_at=exp,
        issuer=issuer,
        token_id=str(payload.get("jti", "")),
        raw=payload,
    )
