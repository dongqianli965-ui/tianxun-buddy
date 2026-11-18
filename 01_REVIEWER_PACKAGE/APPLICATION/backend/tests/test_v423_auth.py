"""P1-11-AUTH 验收测试：身份认证、内部接口鉴权、文件访问控制。

覆盖 TASK.md 四条硬要求：
1. 无令牌 -> 401；
2. 错误令牌（伪造/篡改/过期/吊销）-> 401；
3. 合法令牌 -> 200 且身份由依赖注入；
4. 内部接口无 token -> 403（用户令牌也不得代替内部 token）。

另覆盖：口令哈希、登录限速、角色守卫、路径穿越与项目级文件 ACL。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from txbuddy.security import (
    SecuritySettings,
    TokenError,
    UserDirectory,
    build_context,
    constant_time_equals,
    create_security_app,
    decode_token,
    encode_token,
    hash_password,
    install_security,
    verify_password,
)

SECRET = "unit-test-secret-2c1f0d7a9b4e"
INTERNAL = "unit-test-internal-token-91ab"
ISSUER = "txbuddy.test"


def make_directory() -> UserDirectory:
    return UserDirectory.from_plaintext(
        {
            "estimator": {"password": "Est#2026", "roles": ("estimator",), "projects": ("P-001",)},
            "auditor": {"password": "Aud#2026", "roles": ("auditor",), "projects": ("P-001", "P-002")},
            "admin": {"password": "Adm#2026", "roles": ("admin",), "projects": ()},
            "disabled": {"password": "Dis#2026", "roles": ("estimator",), "projects": ("P-001",), "active": False},
        },
        iterations=10_000,
    )


@pytest.fixture()
def settings() -> SecuritySettings:
    return SecuritySettings(secret=SECRET, internal_token=INTERNAL, issuer=ISSUER, ttl_seconds=600)


@pytest.fixture()
def file_root(tmp_path: Path) -> Path:
    root = tmp_path / "files"
    (root / "P-001").mkdir(parents=True)
    (root / "P-002").mkdir(parents=True)
    (root / "P-001" / "boq.txt").write_bytes(b"P-001 BOQ payload \x00\x01\xff")
    (root / "P-001" / ".hidden.txt").write_bytes(b"hidden")
    (root / "P-001" / "payload.exe").write_bytes(b"MZ")
    (root / "P-002" / "secret.txt").write_bytes(b"P-002 confidential")
    (tmp_path / "outside.txt").write_bytes(b"outside root")
    return root


@pytest.fixture()
def context(settings: SecuritySettings, file_root: Path):
    return build_context(
        settings=settings,
        directory=make_directory(),
        file_root=file_root,
        max_failures=3,
    )


@pytest.fixture()
def app(context) -> FastAPI:
    return create_security_app(context=context)


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def login(client: TestClient, username: str = "estimator", password: str = "Est#2026", prefix: str = "") -> str:
    response = client.post(f"{prefix}/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def mint(settings: SecuritySettings, **overrides: object) -> str:
    kwargs: dict[str, object] = {
        "subject": "estimator",
        "settings": settings,
        "roles": ("estimator",),
        "projects": ("P-001",),
    }
    kwargs.update(overrides)
    return encode_token(**kwargs)  # type: ignore[arg-type]


def b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class TestTokenCore:
    def test_roundtrip_carries_identity_claims(self, settings: SecuritySettings) -> None:
        token = mint(settings, ttl_seconds=120, now=1_700_000_000)
        payload = decode_token(token, settings=settings, now=1_700_000_010)
        assert payload.subject == "estimator"
        assert payload.roles == ("estimator",)
        assert payload.projects == ("P-001",)
        assert payload.expires_at == 1_700_000_120
        assert payload.token_id

    def test_signature_is_standard_hs256(self, settings: SecuritySettings) -> None:
        token = mint(settings)
        header_part, payload_part, signature = token.split(".")
        expected = hmac.new(SECRET.encode(), f"{header_part}.{payload_part}".encode(), hashlib.sha256).digest()
        assert signature == b64e(expected)
        assert json.loads(base64.urlsafe_b64decode(header_part + "=="))["alg"] == "HS256"

    def test_tampered_payload_is_rejected(self, settings: SecuritySettings) -> None:
        header_part, payload_part, signature = mint(settings).split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload_part + "=="))
        claims["roles"] = ["admin"]
        forged = b64e(json.dumps(claims, separators=(",", ":")).encode())
        with pytest.raises(TokenError) as exc:
            decode_token(f"{header_part}.{forged}.{signature}", settings=settings)
        assert exc.value.code == "AUTH_TOKEN_SIGNATURE_INVALID"

    def test_alg_none_is_rejected(self, settings: SecuritySettings) -> None:
        header = b64e(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        payload = b64e(json.dumps({"sub": "admin", "iss": ISSUER, "exp": int(time.time()) + 600}).encode())
        with pytest.raises(TokenError) as exc:
            decode_token(f"{header}.{payload}.", settings=settings)
        assert exc.value.code in {"AUTH_TOKEN_INVALID", "AUTH_TOKEN_ALG_REJECTED"}

    def test_foreign_secret_is_rejected(self, settings: SecuritySettings) -> None:
        other = SecuritySettings(secret="attacker-secret", internal_token=INTERNAL, issuer=ISSUER)
        with pytest.raises(TokenError) as exc:
            decode_token(mint(other), settings=settings)
        assert exc.value.code == "AUTH_TOKEN_SIGNATURE_INVALID"

    def test_expired_token_is_rejected(self, settings: SecuritySettings) -> None:
        token = mint(settings, now=int(time.time()) - 7200, ttl_seconds=60)
        with pytest.raises(TokenError) as exc:
            decode_token(token, settings=settings)
        assert exc.value.code == "AUTH_TOKEN_EXPIRED"

    def test_wrong_issuer_is_rejected(self, settings: SecuritySettings) -> None:
        wrong = SecuritySettings(secret=SECRET, internal_token=INTERNAL, issuer="evil.issuer")
        with pytest.raises(TokenError) as exc:
            decode_token(mint(wrong), settings=settings)
        assert exc.value.code == "AUTH_TOKEN_ISSUER_MISMATCH"

    def test_malformed_tokens_are_rejected(self, settings: SecuritySettings) -> None:
        for bad in ["", "abc", "a.b", "a.b.c.d", "!!!.???.###"]:
            with pytest.raises(TokenError):
                decode_token(bad, settings=settings)

    def test_non_positive_ttl_is_rejected(self, settings: SecuritySettings) -> None:
        with pytest.raises(TokenError):
            encode_token(subject="estimator", settings=settings, ttl_seconds=0)

    def test_constant_time_equals(self) -> None:
        assert constant_time_equals("abc", "abc") is True
        assert constant_time_equals("abc", "abd") is False
        assert constant_time_equals("", "x") is False
        assert constant_time_equals("", "") is True

    def test_password_hash_roundtrip(self) -> None:
        encoded = hash_password("Pass#1234", iterations=10_000)
        assert encoded.startswith("pbkdf2_sha256$10000$")
        assert verify_password("Pass#1234", encoded) is True
        assert verify_password("Pass#12345", encoded) is False
        assert verify_password("Pass#1234", "garbage") is False
        assert verify_password("Pass#1234", hash_password("Pass#1234", iterations=10_000)) is True


class TestLogin:
    def test_login_returns_token_without_secrets(self, client: TestClient) -> None:
        response = client.post("/auth/login", json={"username": "estimator", "password": "Est#2026"})
        assert response.status_code == 200
        body = response.json()
        assert body["access_token"]
        assert body["token_type"] == "bearer"
        assert body["expires_in"] == 600
        assert body["roles"] == ["estimator"]
        assert body["projects"] == ["P-001"]
        serialized = json.dumps(body)
        assert "Est#2026" not in serialized
        assert "password" not in body

    def test_wrong_password_is_401(self, client: TestClient) -> None:
        response = client.post("/auth/login", json={"username": "estimator", "password": "wrong"})
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "AUTH_CREDENTIALS_INVALID"

    def test_unknown_user_is_indistinguishable_from_wrong_password(self, client: TestClient) -> None:
        unknown = client.post("/auth/login", json={"username": "nobody", "password": "wrong"})
        wrong = client.post("/auth/login", json={"username": "estimator", "password": "wrong"})
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["detail"] == wrong.json()["detail"]

    def test_disabled_user_is_rejected(self, client: TestClient) -> None:
        response = client.post("/auth/login", json={"username": "disabled", "password": "Dis#2026"})
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "AUTH_USER_DISABLED"

    def test_login_requires_both_fields(self, client: TestClient) -> None:
        assert client.post("/auth/login", json={"username": "estimator"}).status_code == 422
        assert client.post("/auth/login", json={"username": "", "password": "x"}).status_code == 422

    def test_bruteforce_is_throttled(self, client: TestClient) -> None:
        for _ in range(3):
            assert client.post("/auth/login", json={"username": "estimator", "password": "bad"}).status_code == 401
        locked = client.post("/auth/login", json={"username": "estimator", "password": "Est#2026"})
        assert locked.status_code == 429
        assert locked.json()["detail"]["code"] == "AUTH_LOGIN_LOCKED"
        assert int(locked.headers["Retry-After"]) >= 1

    def test_success_resets_failure_counter(self, client: TestClient) -> None:
        for _ in range(2):
            assert client.post("/auth/login", json={"username": "auditor", "password": "bad"}).status_code == 401
        assert client.post("/auth/login", json={"username": "auditor", "password": "Aud#2026"}).status_code == 200
        for _ in range(2):
            assert client.post("/auth/login", json={"username": "auditor", "password": "bad"}).status_code == 401
        assert client.post("/auth/login", json={"username": "auditor", "password": "Aud#2026"}).status_code == 200


class TestProtectedRoutesRequireToken:
    def test_missing_token_is_401(self, client: TestClient) -> None:
        response = client.get("/auth/me")
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "AUTH_TOKEN_MISSING"
        assert response.headers["WWW-Authenticate"].startswith("Bearer")

    def test_empty_and_non_bearer_scheme_are_401(self, client: TestClient) -> None:
        assert client.get("/auth/me", headers={"Authorization": ""}).status_code == 401
        basic = client.get("/auth/me", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert basic.status_code == 401
        assert basic.json()["detail"]["code"] == "AUTH_SCHEME_UNSUPPORTED"

    @pytest.mark.parametrize(
        "token",
        ["garbage", "a.b.c", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbiJ9.deadbeef"],
        ids=["garbage", "three-parts", "bad-signature"],
    )
    def test_invalid_tokens_are_401(self, client: TestClient, token: str) -> None:
        response = client.get("/auth/me", headers=bearer(token))
        assert response.status_code == 401
        assert response.json()["detail"]["code"].startswith("AUTH_")

    def test_token_signed_with_other_secret_is_401(self, client: TestClient) -> None:
        other = SecuritySettings(secret="not-the-real-secret", internal_token=INTERNAL, issuer=ISSUER)
        response = client.get("/auth/me", headers=bearer(mint(other)))
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "AUTH_TOKEN_SIGNATURE_INVALID"

    def test_expired_token_is_401(self, client: TestClient, settings: SecuritySettings) -> None:
        token = mint(settings, now=int(time.time()) - 3600, ttl_seconds=60)
        response = client.get("/auth/me", headers=bearer(token))
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "AUTH_TOKEN_EXPIRED"

    def test_valid_token_is_200_and_injected(self, client: TestClient) -> None:
        token = login(client, "estimator", "Est#2026")
        response = client.get("/auth/me", headers=bearer(token))
        assert response.status_code == 200
        body = response.json()
        assert body["subject"] == "estimator"
        assert body["roles"] == ["estimator"]
        assert body["projects"] == ["P-001"]
        assert body["method"] == "bearer"
        assert body["token_id"]

    def test_actor_header_cannot_spoof_identity(self, client: TestClient) -> None:
        token = login(client, "estimator", "Est#2026")
        headers = {**bearer(token), "X-Actor": "admin"}
        body = client.get("/auth/me", headers=headers).json()
        assert body["subject"] == "estimator"
        assert "admin" not in body["roles"]

    def test_logout_revokes_token(self, client: TestClient) -> None:
        token = login(client, "estimator", "Est#2026")
        assert client.post("/auth/logout", headers=bearer(token)).status_code == 200
        replay = client.get("/auth/me", headers=bearer(token))
        assert replay.status_code == 401
        assert replay.json()["detail"]["code"] == "AUTH_TOKEN_REVOKED"

    def test_logout_itself_requires_token(self, client: TestClient) -> None:
        assert client.post("/auth/logout").status_code == 401

    def test_role_guard_blocks_estimator_and_allows_admin(self, client: TestClient) -> None:
        estimator = client.get("/auth/audit", headers=bearer(login(client, "estimator", "Est#2026")))
        assert estimator.status_code == 403
        assert estimator.json()["detail"]["code"] == "AUTH_ROLE_FORBIDDEN"

        admin = client.get("/auth/audit", headers=bearer(login(client, "admin", "Adm#2026")))
        assert admin.status_code == 200
        actions = {row["action"] for row in admin.json()["entries"]}
        assert "AUTH_LOGIN" in actions

    def test_role_guard_without_token_is_401(self, client: TestClient) -> None:
        assert client.get("/auth/audit").status_code == 401


class TestInternalTokenGate:
    def test_internal_route_without_token_is_403(self, client: TestClient) -> None:
        response = client.get("/internal/health")
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "AUTH_INTERNAL_TOKEN_MISSING"

    def test_internal_route_with_wrong_token_is_403(self, client: TestClient) -> None:
        response = client.get("/internal/health", headers={"X-Internal-Token": "nope"})
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "AUTH_INTERNAL_TOKEN_INVALID"

    def test_user_bearer_token_cannot_open_internal_route(self, client: TestClient) -> None:
        token = login(client, "admin", "Adm#2026")
        response = client.get("/internal/health", headers=bearer(token))
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "AUTH_INTERNAL_TOKEN_MISSING"

    def test_internal_token_is_accepted(self, client: TestClient) -> None:
        response = client.get("/internal/health", headers={"X-Internal-Token": INTERNAL})
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["scope"] == "internal"

    def test_internal_token_cannot_authenticate_user_route(self, client: TestClient) -> None:
        response = client.get("/auth/me", headers={"Authorization": f"Bearer {INTERNAL}"})
        assert response.status_code == 401

    def test_internal_user_lookup_hides_password_hash(self, client: TestClient) -> None:
        response = client.get("/internal/users/estimator", headers={"X-Internal-Token": INTERNAL})
        assert response.status_code == 200
        body = response.json()
        assert body["username"] == "estimator"
        assert body["projects"] == ["P-001"]
        assert "password_hash" not in body
        assert "password" not in json.dumps(body)

    def test_internal_user_lookup_404_for_unknown(self, client: TestClient) -> None:
        response = client.get("/internal/users/ghost", headers={"X-Internal-Token": INTERNAL})
        assert response.status_code == 404

    def test_denied_internal_calls_are_audited(self, client: TestClient, context) -> None:
        client.get("/internal/health", headers={"X-Internal-Token": "nope"})
        rows = context.audit.entries("INTERNAL_ACCESS")
        assert rows and rows[-1]["result"] == "denied"
        assert rows[-1]["detail"] == "AUTH_INTERNAL_TOKEN_INVALID"


class TestFileAccessControl:
    def test_file_without_token_is_401(self, client: TestClient) -> None:
        assert client.get("/files/P-001/boq.txt").status_code == 401

    def test_owner_can_read_own_project_file(self, client: TestClient) -> None:
        response = client.get("/files/P-001/boq.txt", headers=bearer(login(client, "estimator", "Est#2026")))
        assert response.status_code == 200
        assert response.content == b"P-001 BOQ payload \x00\x01\xff"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Project-Id"] == "P-001"

    def test_cross_project_read_is_403(self, client: TestClient) -> None:
        response = client.get("/files/P-002/secret.txt", headers=bearer(login(client, "estimator", "Est#2026")))
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "AUTH_FILE_FORBIDDEN"

    def test_multi_project_identity_can_read_both(self, client: TestClient) -> None:
        auditor = bearer(login(client, "auditor", "Aud#2026"))
        assert client.get("/files/P-001/boq.txt", headers=auditor).status_code == 200
        assert client.get("/files/P-002/secret.txt", headers=auditor).status_code == 200

    def test_admin_can_read_any_project(self, client: TestClient) -> None:
        admin = bearer(login(client, "admin", "Adm#2026"))
        assert client.get("/files/P-002/secret.txt", headers=admin).status_code == 200

    @pytest.mark.parametrize(
        "path",
        [
            "/files/P-001/%2e%2e/outside.txt",
            "/files/P-001/%2e%2e%2foutside.txt",
            "/files/%2e%2e/outside.txt",
            "/files/%2Fetc%2Fpasswd",
        ],
        ids=["dotdot-segment", "dotdot-encoded", "top-dotdot", "absolute"],
    )
    def test_path_traversal_is_blocked(self, client: TestClient, path: str) -> None:
        response = client.get(path, headers=bearer(login(client, "admin", "Adm#2026")))
        assert response.status_code == 403, response.text
        assert response.json()["detail"]["code"] == "AUTH_FILE_PATH_FORBIDDEN"

    def test_hidden_file_is_403(self, client: TestClient) -> None:
        response = client.get("/files/P-001/.hidden.txt", headers=bearer(login(client, "admin", "Adm#2026")))
        assert response.status_code == 403

    def test_non_whitelisted_suffix_is_403(self, client: TestClient) -> None:
        response = client.get("/files/P-001/payload.exe", headers=bearer(login(client, "admin", "Adm#2026")))
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "AUTH_FILE_TYPE_FORBIDDEN"

    def test_project_root_directory_is_not_listable(self, client: TestClient) -> None:
        response = client.get("/files/P-001", headers=bearer(login(client, "admin", "Adm#2026")))
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "AUTH_FILE_PATH_FORBIDDEN"
        assert "boq.txt" not in response.text

    def test_missing_file_is_404(self, client: TestClient) -> None:
        response = client.get("/files/P-001/none.txt", headers=bearer(login(client, "admin", "Adm#2026")))
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "AUTH_FILE_NOT_FOUND"

    def test_file_access_is_audited(self, client: TestClient, context) -> None:
        client.get("/files/P-001/boq.txt", headers=bearer(login(client, "estimator", "Est#2026")))
        client.get("/files/P-002/secret.txt", headers=bearer(login(client, "estimator", "Est#2026")))
        rows = context.audit.entries("FILE_ACCESS")
        results = [row["result"] for row in rows]
        assert "ok" in results and "denied" in results

    def test_policy_rejects_windows_absolute_path(self, file_root: Path) -> None:
        from txbuddy.security import FileAccessError, FileAccessPolicy

        policy = FileAccessPolicy(file_root)
        for bad in ["C:/Windows/win.ini", "\\\\server\\share\\x.txt", "/etc/passwd", "P-001/../x.txt"]:
            with pytest.raises(FileAccessError) as exc:
                policy.normalize(bad)
            assert exc.value.code in {"AUTH_FILE_PATH_FORBIDDEN", "AUTH_FILE_PATH_INVALID"}


class TestAppIntegration:
    def test_factory_exposes_configurable_context(self, settings: SecuritySettings, file_root: Path) -> None:
        app = create_security_app(settings=settings, directory=make_directory(), file_root=file_root)
        assert app.state.security.settings is settings
        with TestClient(app) as client:
            assert client.get("/auth/me").status_code == 401
            token = login(client, "estimator", "Est#2026")
            assert client.get("/auth/me", headers=bearer(token)).status_code == 200

    def test_install_onto_existing_main_app(self, settings: SecuritySettings, file_root: Path) -> None:
        """不修改 api.py 即可把鉴权挂到既有业务应用（写范围限制下的集成证明）。"""
        main_app = FastAPI(title="isolated installer harness")

        context = build_context(settings=settings, directory=make_directory(), file_root=file_root)
        returns = install_security(main_app, context, prefix="/security")
        assert returns is context
        paths = [getattr(route, "path", None) for route in main_app.routes]
        assert None not in paths, "父应用路由表中出现无 path 的包装对象"
        post_paths = {
            route.path for route in main_app.routes
            if "POST" in (getattr(route, "methods", None) or set())
        }
        assert "/security/auth/login" not in post_paths, "鉴权端点不得污染父应用的 POST 入口清单"

        with TestClient(main_app) as client:
            assert client.get("/security/auth/me").status_code == 401
            assert client.get("/auth/me").status_code == 404
            token = login(client, "estimator", "Est#2026", prefix="/security")
            me = client.get("/security/auth/me", headers=bearer(token))
            assert me.status_code == 200
            assert me.json()["subject"] == "estimator"
            assert client.get("/openapi.json").status_code == 200
            assert client.get("/security/internal/health").status_code == 403

