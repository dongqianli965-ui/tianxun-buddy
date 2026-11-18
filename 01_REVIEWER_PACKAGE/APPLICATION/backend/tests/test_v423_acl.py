"""P1-07-ACL 验收测试：项目-成员-角色访问控制。

覆盖 TASK.md 三条硬要求：
1. ``project_member`` 角色 OWNER/EDITOR/REVIEWER/VIEWER 与判定函数；
2. 无权限访问被拒绝（403）；
3. 越权跨项目访问被拒绝，且 API 与文件访问走同一判定点。

测试只用内存 ACL 与 tmp_path 文件根，不依赖网络/外部服务。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from txbuddy.security import (
    AclError,
    FileAccessError,
    FileAccessPolicy,
    Identity,
    Membership,
    PERMISSION_MATRIX,
    PROJECT_ROLES,
    Permission,
    ProjectAcl,
    ProjectMember,
    ProjectRole,
    SecurityContext,
    SecuritySettings,
    build_context,
    create_security_app,
    required_roles,
)

SECRET = "unit-test-acl-secret-4f7c2a"
INTERNAL = "unit-test-acl-internal-8d31"
ISSUER = "txbuddy.acl"

ALL_ROLES = (ProjectRole.VIEWER, ProjectRole.EDITOR, ProjectRole.REVIEWER, ProjectRole.OWNER)
ROLE_PERMISSIONS = {
    ProjectRole.VIEWER: {Permission.READ},
    ProjectRole.EDITOR: {Permission.READ, Permission.WRITE},
    ProjectRole.REVIEWER: {Permission.READ, Permission.REVIEW},
    ProjectRole.OWNER: {Permission.READ, Permission.WRITE, Permission.REVIEW, Permission.ADMIN},
}

PASSWORDS = {
    "owner": "Own#2026",
    "editor": "Edit#2026",
    "reviewer": "Rev#2026",
    "viewer": "View#2026",
    "outsider": "Out#2026",
    "legacy": "Leg#2026",
    "admin": "Adm#2026",
}


def make_directory():
    from txbuddy.security import UserDirectory

    entries = {
        name: {"password": password, "roles": ("estimator",), "projects": ()}
        for name, password in PASSWORDS.items()
    }
    entries["admin"]["roles"] = ("admin",)
    entries["legacy"]["projects"] = ("P-001",)  # 历史令牌授权模型
    return UserDirectory.from_plaintext(entries, iterations=10_000)


def make_acl() -> ProjectAcl:
    return ProjectAcl.from_mapping(
        {
            "P-001": {
                "owner": ProjectRole.OWNER,
                "editor": ProjectRole.EDITOR,
                "reviewer": ProjectRole.REVIEWER,
                "viewer": ProjectRole.VIEWER,
            },
            "P-002": {
                "owner": ProjectRole.OWNER,
                "viewer": ProjectRole.VIEWER,
            },
        },
        granted_by="bootstrap",
        now=1_700_000_000.0,
    )


@pytest.fixture()
def settings() -> SecuritySettings:
    return SecuritySettings(secret=SECRET, internal_token=INTERNAL, issuer=ISSUER, ttl_seconds=600)


@pytest.fixture()
def file_root(tmp_path: Path) -> Path:
    root = tmp_path / "files"
    (root / "P-001" / "sub").mkdir(parents=True)
    (root / "P-002").mkdir(parents=True)
    (root / "P-001" / "boq.json").write_bytes(b'{"project":"P-001"}')
    (root / "P-001" / "sub" / "deep.txt").write_text("deep", encoding="utf-8")
    (root / "P-002" / "secret.txt").write_bytes(b"P-002 confidential")
    (tmp_path / "outside.txt").write_bytes(b"outside root")
    return root


@pytest.fixture()
def acl() -> ProjectAcl:
    return make_acl()


@pytest.fixture()
def context(settings: SecuritySettings, file_root: Path, acl: ProjectAcl) -> SecurityContext:
    return build_context(settings=settings, directory=make_directory(), file_root=file_root, acl=acl)


@pytest.fixture()
def app(context: SecurityContext) -> FastAPI:
    return create_security_app(context=context)


@pytest.fixture()
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def login(client: TestClient, username: str, prefix: str = "") -> str:
    response = client.post(f"{prefix}/auth/login", json={"username": username, "password": PASSWORDS[username]})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth(client: TestClient, username: str) -> dict[str, str]:
    return bearer(login(client, username))


def check_code(response) -> str:
    body = response.json()
    detail = body.get("detail")
    if isinstance(detail, dict):
        return str(detail.get("code", ""))
    return str(body.get("code", ""))


class TestRoleModel:
    def test_project_member_alias_and_role_order(self) -> None:
        assert ProjectMember is Membership
        assert PROJECT_ROLES == ALL_ROLES
        assert tuple(ProjectRole) == ALL_ROLES
        assert set(PERMISSION_MATRIX) == set(Permission)

    def test_role_parse_is_case_insensitive(self) -> None:
        for item in ALL_ROLES:
            assert ProjectRole.parse(item.value) is item
            assert ProjectRole.parse(item.value.lower()) is item
            assert ProjectRole.parse(f"  {item.value}  ") is item
        assert ProjectRole.parse("owner") is ProjectRole.OWNER

    def test_invalid_role_is_rejected(self) -> None:
        for bad in ["", "  ", "SUPERUSER", None, 3]:
            with pytest.raises(AclError) as exc:
                ProjectRole.parse(bad)  # type: ignore[arg-type]
            assert exc.value.code == "ACL_ROLE_INVALID"
            assert exc.value.status_code == 400

    @pytest.mark.parametrize("role", ALL_ROLES, ids=[role.value for role in ALL_ROLES])
    def test_permission_matrix_is_explicit(self, role: ProjectRole) -> None:
        granted = {permission for permission in Permission if role.allows(permission)}
        assert granted == ROLE_PERMISSIONS[role]
        assert role.permissions == tuple(sorted(granted, key=lambda item: list(Permission).index(item)))

    def test_reviewer_cannot_write_and_editor_cannot_review(self) -> None:
        assert ProjectRole.REVIEWER.can_review() is True
        assert ProjectRole.REVIEWER.can_write() is False
        assert ProjectRole.EDITOR.can_write() is True
        assert ProjectRole.EDITOR.can_review() is False
        assert ProjectRole.VIEWER.can_read() is True
        assert ProjectRole.VIEWER.can_write() is False
        assert ProjectRole.OWNER.can_administer() is True

    def test_required_roles_sorted_by_level(self) -> None:
        assert required_roles(Permission.WRITE) == (ProjectRole.EDITOR, ProjectRole.OWNER)
        assert required_roles("admin") == (ProjectRole.OWNER,)
        assert required_roles(Permission.READ) == ALL_ROLES

    def test_permission_aliases(self) -> None:
        assert Permission.parse("view") is Permission.READ
        assert Permission.parse("download") is Permission.READ
        assert Permission.parse("upload") is Permission.WRITE
        assert Permission.parse("approve") is Permission.REVIEW
        assert Permission.parse("manage") is Permission.ADMIN
        with pytest.raises(AclError) as exc:
            Permission.parse("publish")
        assert exc.value.code == "ACL_PERMISSION_INVALID"
        assert exc.value.status_code == 400


class TestProjectAclStore:
    def test_seed_and_lookup(self, acl: ProjectAcl) -> None:
        assert len(acl) == 6
        assert acl.role_of("P-001", "owner") is ProjectRole.OWNER
        assert acl.role_of("P-001", "viewer") is ProjectRole.VIEWER
        assert acl.role_of("P-002", "editor") is None
        assert acl.role_of("P-001", "ghost") is None
        assert acl.projects_of("viewer") == ("P-001", "P-002")

    def test_members_are_ordered_by_role_then_subject(self, acl: ProjectAcl) -> None:
        members = acl.members("P-001")
        assert [item.subject for item in members] == ["owner", "reviewer", "editor", "viewer"]
        assert [item.role for item in members] == [
            ProjectRole.OWNER, ProjectRole.REVIEWER, ProjectRole.EDITOR, ProjectRole.VIEWER,
        ]
        assert all(item.as_dict()["project_id"] == "P-001" for item in members)

    def test_grant_is_upsert_and_keeps_granted_at(self, acl: ProjectAcl) -> None:
        original = acl.membership("P-001", "viewer")
        assert original is not None
        updated = acl.grant("P-001", "viewer", "EDITOR", granted_by="owner", now=1_700_000_100.0)
        assert updated.role is ProjectRole.EDITOR
        assert updated.granted_at == original.granted_at
        assert updated.updated_at == 1_700_000_100.0
        assert updated.granted_by == "owner"
        assert acl.role_of("P-001", "viewer") is ProjectRole.EDITOR
        assert len(acl) == 6

    def test_grant_new_member_and_revoke(self, acl: ProjectAcl) -> None:
        acl.grant("P-002", "outsider", "VIEWER", granted_by="owner")
        assert acl.role_of("P-002", "outsider") is ProjectRole.VIEWER
        assert acl.revoke("P-002", "outsider") is True
        assert acl.revoke("P-002", "outsider") is False
        assert acl.role_of("P-002", "outsider") is None

    def test_blank_or_path_like_ids_are_rejected(self, acl: ProjectAcl) -> None:
        for bad_project, code in [("", "ACL_PROJECT_INVALID"), ("../P-001", "ACL_PROJECT_INVALID")]:
            with pytest.raises(AclError) as exc:
                acl.grant(bad_project, "viewer", "VIEWER")
            assert exc.value.code == code
        with pytest.raises(AclError) as exc:
            acl.grant("P-001", " ", "VIEWER")
        assert exc.value.code == "ACL_SUBJECT_INVALID"
        with pytest.raises(AclError) as exc:
            acl.grant("P-001", "viewer", "SUPERUSER")
        assert exc.value.code == "ACL_ROLE_INVALID"

    def test_membership_record_validates_its_fields(self) -> None:
        good = Membership(project_id="P-001", subject="viewer", role="viewer")
        assert good.role is ProjectRole.VIEWER
        with pytest.raises(AclError):
            Membership(project_id="", subject="viewer", role="VIEWER")
        with pytest.raises(AclError):
            Membership(project_id="P-001", subject="viewer", role="ROOT")

    def test_clear_empties_store(self, acl: ProjectAcl) -> None:
        acl.clear()
        assert len(acl) == 0
        assert acl.decide("owner", "P-001", "READ").allowed is False


class TestDecideFunction:
    def test_member_allowed_with_role_evidence(self, acl: ProjectAcl) -> None:
        decision = acl.decide("editor", "P-001", Permission.WRITE)
        assert decision.allowed is True
        assert bool(decision) is True
        assert decision.code == "ACL_ALLOWED"
        assert decision.role is ProjectRole.EDITOR
        assert decision.reason == "member"
        assert decision.status_code == 200

    def test_non_member_denied_403(self, acl: ProjectAcl) -> None:
        decision = acl.decide("outsider", "P-001", Permission.READ)
        assert decision.allowed is False
        assert decision.code == "ACL_NOT_MEMBER"
        assert decision.status_code == 403
        assert decision.role is None
        assert decision.reason == "not-member"

    def test_known_member_without_permission_denied_403(self, acl: ProjectAcl) -> None:
        decision = acl.decide("viewer", "P-001", Permission.WRITE)
        assert decision.allowed is False
        assert decision.code == "ACL_ROLE_FORBIDDEN"
        assert decision.status_code == 403
        assert decision.role is ProjectRole.VIEWER
        assert "EDITOR" in decision.message and "OWNER" in decision.message

    def test_cross_project_access_is_denied(self, acl: ProjectAcl) -> None:
        assert acl.decide("editor", "P-002", Permission.READ).code == "ACL_NOT_MEMBER"
        assert acl.decide("editor", "P-002", Permission.WRITE).allowed is False
        assert acl.decide("viewer", "P-002", Permission.READ).allowed is True

    def test_different_projects_are_independent(self, acl: ProjectAcl) -> None:
        acl.grant("P-002", "viewer", "OWNER")
        assert acl.decide("viewer", "P-002", Permission.ADMIN).allowed is True
        assert acl.decide("viewer", "P-001", Permission.ADMIN).allowed is False

    def test_global_admin_bypasses_membership(self, acl: ProjectAcl) -> None:
        decision = acl.decide("admin", "P-902", Permission.ADMIN, roles=("admin",))
        assert decision.allowed is True
        assert decision.code == "ACL_ALLOWED_ADMIN"
        assert decision.reason == "global-admin"
        assert decision.role is ProjectRole.OWNER

    def test_legacy_projects_claim_is_fallback_only(self, acl: ProjectAcl) -> None:
        legacy = acl.decide("legacy", "P-001", Permission.WRITE, legacy_projects=("P-001",))
        assert legacy.allowed is True
        assert legacy.code == "ACL_ALLOWED_LEGACY"
        assert legacy.role is ProjectRole.EDITOR
        denied = acl.decide("legacy", "P-001", Permission.ADMIN, legacy_projects=("P-001",))
        assert denied.allowed is False
        assert denied.code == "ACL_ROLE_FORBIDDEN"

    def test_explicit_membership_overrides_legacy_claim(self, acl: ProjectAcl) -> None:
        acl.grant("P-001", "legacy", "VIEWER")
        decision = acl.decide("legacy", "P-001", Permission.WRITE, legacy_projects=("P-001",))
        assert decision.allowed is False
        assert decision.role is ProjectRole.VIEWER
        assert decision.reason == "role-forbidden"

    def test_legacy_can_be_disabled(self, acl: ProjectAcl) -> None:
        denied = acl.decide("legacy", "P-001", "READ", legacy_projects=("P-001",), use_legacy_projects=False)
        assert denied.allowed is False
        assert denied.code == "ACL_NOT_MEMBER"

    def test_require_raises_acl_error(self, acl: ProjectAcl) -> None:
        assert acl.require("owner", "P-001", "ADMIN").allowed is True
        with pytest.raises(AclError) as exc:
            acl.require("outsider", "P-001", "READ")
        assert exc.value.code == "ACL_NOT_MEMBER"
        assert exc.value.status_code == 403
        with pytest.raises(AclError):
            acl.decide("owner", "P-001", "PUBLISH")

    def test_identity_helpers(self, acl: ProjectAcl) -> None:
        identity = Identity(subject="reviewer", roles=(), projects=())
        assert acl.decide_identity(identity, "P-001", "REVIEW").role is ProjectRole.REVIEWER
        with pytest.raises(AclError):
            acl.require_identity(identity, "P-001", "WRITE")
        with pytest.raises(AclError) as exc:
            acl.decide_identity(Identity(subject=""), "P-001", "READ")
        assert exc.value.status_code == 401


class TestProjectApiAuthorization:
    def test_missing_token_is_401(self, client: TestClient) -> None:
        response = client.get("/projects/P-001")
        assert response.status_code == 401
        assert check_code(response) == "AUTH_TOKEN_MISSING"

    def test_member_reads_project_with_role_evidence(self, client: TestClient) -> None:
        response = client.get("/projects/P-001", headers=auth(client, "viewer"))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["project_id"] == "P-001"
        assert body["subject"] == "viewer"
        assert body["role"] == "VIEWER"
        assert body["permission"] == "READ"
        assert body["decision"]["reason"] == "member"
        assert body["member_count"] == 4
        assert "OWNER" in body["permissions"]["ADMIN"]

    def test_cross_project_read_is_403(self, client: TestClient) -> None:
        response = client.get("/projects/P-002", headers=auth(client, "editor"))
        assert response.status_code == 403
        assert check_code(response) == "ACL_NOT_MEMBER"
        assert "members" not in response.text

    def test_non_member_read_is_403(self, client: TestClient) -> None:
        response = client.get("/projects/P-001", headers=auth(client, "outsider"))
        assert response.status_code == 403
        assert check_code(response) == "ACL_NOT_MEMBER"

    def test_unknown_project_does_not_leak_existence(self, client: TestClient) -> None:
        ghost = client.get("/projects/P-999", headers=auth(client, "owner"))
        assert ghost.status_code == 403
        assert check_code(ghost) == "ACL_NOT_MEMBER"

    def test_global_admin_reads_any_project(self, client: TestClient) -> None:
        response = client.get("/projects/P-999", headers=auth(client, "admin"))
        assert response.status_code == 200
        body = response.json()
        assert body["role"] == "OWNER"
        assert body["decision"]["reason"] == "global-admin"

    def test_check_endpoint_reports_denial_without_5xx(self, client: TestClient) -> None:
        headers = auth(client, "viewer")
        allowed = client.post("/projects/P-001/check", json={"permission": "READ"}, headers=headers)
        assert allowed.status_code == 200
        assert allowed.json()["allowed"] is True
        denied = client.post("/projects/P-001/check", json={"permission": "WRITE"}, headers=headers)
        assert denied.status_code == 200
        body = denied.json()
        assert body["allowed"] is False
        assert body["code"] == "ACL_ROLE_FORBIDDEN"
        assert body["role"] == "VIEWER"
        cross = client.post("/projects/P-002/check", json={"permission": "READ"}, headers=auth(client, "editor"))
        assert cross.json()["code"] == "ACL_NOT_MEMBER"

    def test_check_endpoint_rejects_unknown_permission(self, client: TestClient) -> None:
        response = client.post("/projects/P-001/check", json={"permission": "PUBLISH"}, headers=auth(client, "owner"))
        assert response.status_code == 400
        assert check_code(response) == "ACL_PERMISSION_INVALID"

    def test_viewer_and_editor_cannot_list_members(self, client: TestClient) -> None:
        for username, expected in [("viewer", "ACL_ROLE_FORBIDDEN"), ("editor", "ACL_ROLE_FORBIDDEN")]:
            response = client.get("/projects/P-001/members", headers=auth(client, username))
            assert response.status_code == 403
            assert check_code(response) == expected

    def test_reviewer_can_list_members(self, client: TestClient) -> None:
        response = client.get("/projects/P-001/members", headers=auth(client, "reviewer"))
        assert response.status_code == 200
        body = response.json()
        assert body["viewer_role"] == "REVIEWER"
        assert body["count"] == 4
        assert {row["subject"] for row in body["members"]} == {"owner", "editor", "reviewer", "viewer"}

    def test_cross_project_member_list_is_403(self, client: TestClient) -> None:
        response = client.get("/projects/P-002/members", headers=auth(client, "editor"))
        assert response.status_code == 403
        assert check_code(response) == "ACL_NOT_MEMBER"

    def test_grant_requires_owner_role(self, client: TestClient) -> None:
        for username in ("viewer", "editor", "reviewer"):
            response = client.post(
                "/projects/P-001/members",
                json={"subject": "outsider", "role": "VIEWER"},
                headers=auth(client, username),
            )
            assert response.status_code == 403, username
            assert check_code(response) == "ACL_ROLE_FORBIDDEN"

    def test_owner_can_grant_and_new_member_reads(self, client: TestClient, acl: ProjectAcl) -> None:
        owner = auth(client, "owner")
        granted = client.post(
            "/projects/P-001/members",
            json={"subject": "outsider", "role": "VIEWER"},
            headers=owner,
        )
        assert granted.status_code == 200, granted.text
        assert granted.json()["membership"]["role"] == "VIEWER"
        assert acl.role_of("P-001", "outsider") is ProjectRole.VIEWER

        member = client.get("/projects/P-001", headers=auth(client, "outsider"))
        assert member.status_code == 200
        assert member.json()["role"] == "VIEWER"

    def test_grant_unknown_subject_is_404(self, client: TestClient) -> None:
        response = client.post(
            "/projects/P-001/members",
            json={"subject": "ghost", "role": "VIEWER"},
            headers=auth(client, "owner"),
        )
        assert response.status_code == 404
        assert check_code(response) == "ACL_SUBJECT_UNKNOWN"

    def test_grant_invalid_role_is_400(self, client: TestClient) -> None:
        response = client.post(
            "/projects/P-001/members",
            json={"subject": "outsider", "role": "SUPERUSER"},
            headers=auth(client, "owner"),
        )
        assert response.status_code == 400
        assert check_code(response) == "ACL_ROLE_INVALID"

    def test_grant_cross_project_by_non_owner_is_403(self, client: TestClient) -> None:
        response = client.post(
            "/projects/P-002/members",
            json={"subject": "outsider", "role": "VIEWER"},
            headers=auth(client, "editor"),
        )
        assert response.status_code == 403
        assert check_code(response) == "ACL_NOT_MEMBER"

    def test_revoke_member_takes_effect(self, client: TestClient) -> None:
        owner = auth(client, "owner")
        granted = client.post("/projects/P-001/members", json={"subject": "outsider", "role": "VIEWER"}, headers=owner)
        assert granted.status_code == 200
        assert client.get("/projects/P-001", headers=auth(client, "outsider")).status_code == 200
        removed = client.delete("/projects/P-001/members/outsider", headers=owner)
        assert removed.status_code == 200
        assert removed.json()["revoked"] is True
        denied = client.get("/projects/P-001", headers=auth(client, "outsider"))
        assert denied.status_code == 403
        assert check_code(denied) == "ACL_NOT_MEMBER"

    def test_cannot_revoke_last_owner(self, client: TestClient) -> None:
        response = client.delete("/projects/P-001/members/owner", headers=auth(client, "owner"))
        assert response.status_code == 409
        assert check_code(response) == "ACL_LAST_OWNER"

    def test_denials_are_audited(self, client: TestClient, context: SecurityContext) -> None:
        client.get("/projects/P-002", headers=auth(client, "editor"))
        rows = context.audit.entries("PROJECT_ACCESS")
        assert rows, "项目访问必须留审计"
        assert rows[-1]["result"] == "denied"
        assert rows[-1]["detail"] == "ACL_NOT_MEMBER"
        assert rows[-1]["actor"] == "editor"


class TestFileAccessGoesThroughAcl:
    def test_viewer_reads_member_file_with_role_header(self, client: TestClient) -> None:
        response = client.get("/files/P-001/boq.json", headers=auth(client, "viewer"))
        assert response.status_code == 200
        assert response.content == b'{"project":"P-001"}'
        assert response.headers["X-Acl-Role"] == "VIEWER"
        assert response.headers["X-Project-Id"] == "P-001"

    def test_cross_project_read_is_403(self, client: TestClient) -> None:
        response = client.get("/files/P-002/secret.txt", headers=auth(client, "editor"))
        assert response.status_code == 403
        assert check_code(response) == "AUTH_FILE_FORBIDDEN"

    def test_non_member_read_is_403(self, client: TestClient) -> None:
        response = client.get("/files/P-001/boq.json", headers=auth(client, "outsider"))
        assert response.status_code == 403
        assert check_code(response) == "AUTH_FILE_FORBIDDEN"

    def test_viewer_cannot_write(self, client: TestClient, file_root: Path) -> None:
        response = client.put("/files/P-001/viewer.json", content=b"nope", headers=auth(client, "viewer"))
        assert response.status_code == 403
        assert check_code(response) == "AUTH_FILE_FORBIDDEN"
        assert not (file_root / "P-001" / "viewer.json").exists()

    def test_reviewer_cannot_write(self, client: TestClient) -> None:
        response = client.put("/files/P-001/reviewer.json", content=b"nope", headers=auth(client, "reviewer"))
        assert response.status_code == 403
        assert check_code(response) == "AUTH_FILE_FORBIDDEN"

    def test_editor_can_write_then_read_back(self, client: TestClient, file_root: Path) -> None:
        payload = b'{"draft":true,"project":"P-001"}'
        written = client.put("/files/P-001/draft.json", content=payload, headers=auth(client, "editor"))
        assert written.status_code == 200, written.text
        body = written.json()
        assert body["stored"] is True
        assert body["role"] == "EDITOR"
        assert body["size"] == len(payload)
        assert (file_root / "P-001" / "draft.json").read_bytes() == payload
        read = client.get("/files/P-001/draft.json", headers=auth(client, "viewer"))
        assert read.status_code == 200
        assert read.content == payload

    def test_editor_writes_existing_directory(self, client: TestClient, file_root: Path) -> None:
        payload = b'{"deep":1}'
        written = client.put("/files/P-001/sub/added.json", content=payload, headers=auth(client, "editor"))
        assert written.status_code == 200, written.text
        assert (file_root / "P-001" / "sub" / "added.json").read_bytes() == payload
        read = client.get("/files/P-001/sub/added.json", headers=auth(client, "viewer"))
        assert read.status_code == 200
        assert read.content == payload
        assert not list((file_root / "P-001" / "sub").glob(".upload-*")), "不得残留临时文件"

    def test_owner_can_write(self, client: TestClient) -> None:
        response = client.put("/files/P-001/owner.json", content=b"{}", headers=auth(client, "owner"))
        assert response.status_code == 200
        assert response.json()["role"] == "OWNER"

    def test_cross_project_write_is_403(self, client: TestClient) -> None:
        response = client.put("/files/P-002/intruder.json", content=b"{}", headers=auth(client, "editor"))
        assert response.status_code == 403
        assert check_code(response) == "AUTH_FILE_FORBIDDEN"

    def test_write_traversal_is_blocked(self, client: TestClient, file_root: Path) -> None:
        response = client.put(
            "/files/P-001/%2e%2e/outside.json", content=b"{}", headers=auth(client, "owner")
        )
        assert response.status_code == 403
        assert check_code(response) == "AUTH_FILE_PATH_FORBIDDEN"
        assert (file_root.parent / "outside.txt").read_bytes() == b"outside root"
        assert not (file_root.parent / "outside.json").exists()

    def test_write_suffix_whitelist(self, client: TestClient) -> None:
        response = client.put("/files/P-001/tool.exe", content=b"MZ", headers=auth(client, "owner"))
        assert response.status_code == 403
        assert check_code(response) == "AUTH_FILE_TYPE_FORBIDDEN"

    def test_write_to_missing_directory_is_404(self, client: TestClient) -> None:
        response = client.put("/files/P-001/nodir/x.json", content=b"{}", headers=auth(client, "owner"))
        assert response.status_code == 404
        assert check_code(response) == "AUTH_FILE_DIR_NOT_FOUND"

    def test_write_without_token_is_401(self, client: TestClient) -> None:
        assert client.put("/files/P-001/x.json", content=b"{}").status_code == 401

    def test_legacy_projects_claim_is_editor_fallback(self, client: TestClient) -> None:
        headers = auth(client, "legacy")
        project = client.get("/projects/P-001", headers=headers)
        assert project.status_code == 200
        assert project.json()["role"] == "EDITOR"
        assert project.json()["decision"]["reason"] == "legacy-project-grant"
        written = client.put("/files/P-001/legacy.json", content=b"{}", headers=headers)
        assert written.status_code == 200
        assert client.get("/files/P-002/secret.txt", headers=headers).status_code == 403

    def test_explicit_membership_overrides_legacy_claim(self, client: TestClient) -> None:
        owner = auth(client, "owner")
        granted = client.post(
            "/projects/P-001/members",
            json={"subject": "legacy", "role": "VIEWER"},
            headers=owner,
        )
        assert granted.status_code == 200
        headers = auth(client, "legacy")
        assert client.get("/files/P-001/boq.json", headers=headers).status_code == 200
        denied = client.put("/files/P-001/legacy2.json", content=b"{}", headers=headers)
        assert denied.status_code == 403, "显式 VIEWER 必须覆盖令牌里的历史 projects 声明"
        assert check_code(denied) == "AUTH_FILE_FORBIDDEN"

    def test_revocation_invalidates_existing_token_for_api_and_files(self, client: TestClient) -> None:
        owner = auth(client, "owner")
        assert client.post(
            "/projects/P-001/members",
            json={"subject": "outsider", "role": "VIEWER"},
            headers=owner,
        ).status_code == 200
        outsider = auth(client, "outsider")  # 令牌在授权之后签发，但没有 projects 声明
        assert client.get("/projects/P-001", headers=outsider).status_code == 200
        assert client.get("/files/P-001/boq.json", headers=outsider).status_code == 200
        assert client.delete("/projects/P-001/members/outsider", headers=owner).status_code == 200
        # 同一个令牌：服务端吊销后 API 与文件同时失效
        assert client.get("/projects/P-001", headers=outsider).status_code == 403
        assert client.get("/files/P-001/boq.json", headers=outsider).status_code == 403

    @pytest.mark.parametrize(
        "username,project,expected",
        [
            ("viewer", "P-001", 200),
            ("viewer", "P-002", 200),
            ("editor", "P-001", 200),
            ("editor", "P-002", 403),
            ("reviewer", "P-001", 200),
            ("outsider", "P-001", 403),
            ("admin", "P-002", 200),
        ],
        ids=["viewer-own", "viewer-other-member", "editor-own", "editor-cross",
             "reviewer-own", "outsider", "admin"],
    )
    def test_read_decisions_agree_between_api_and_files(
        self, client: TestClient, username: str, project: str, expected: int
    ) -> None:
        headers = auth(client, username)
        api = client.get(f"/projects/{project}", headers=headers)
        file_path = "boq.json" if project == "P-001" else "secret.txt"
        files = client.get(f"/files/{project}/{file_path}", headers=headers)
        assert api.status_code == expected, f"API {username}/{project}: {api.text}"
        assert files.status_code == expected, f"FILE {username}/{project}: {files.text}"

    @pytest.mark.parametrize(
        "username,expected",
        [("viewer", 403), ("editor", 200), ("reviewer", 403), ("owner", 200), ("outsider", 403)],
    )
    def test_write_decisions_follow_write_role(self, client: TestClient, username: str, expected: int) -> None:
        response = client.put(
            f"/files/P-001/write_{username}.json", content=b"{}", headers=auth(client, username)
        )
        assert response.status_code == expected, response.text

    def test_file_write_audit(self, client: TestClient, context: SecurityContext) -> None:
        client.put("/files/P-001/audit.json", content=b"{}", headers=auth(client, "viewer"))
        denied = context.audit.entries("FILE_WRITE")
        assert denied and denied[-1]["result"] == "denied" and denied[-1]["detail"] == "AUTH_FILE_FORBIDDEN"
        client.put("/files/P-001/audit.json", content=b"{}", headers=auth(client, "editor"))
        ok = context.audit.entries("FILE_WRITE")
        assert ok[-1]["result"] == "ok"


class TestUnifiedAuthorization:
    def test_context_and_files_share_one_acl(self, context: SecurityContext) -> None:
        assert context.acl is not None
        assert context.files.acl is context.acl

    def test_app_state_exposes_same_acl(self, app: FastAPI, acl: ProjectAcl) -> None:
        assert app.state.security.acl is acl
        assert app.state.security.files.acl is acl

    def test_context_adopts_policy_acl(self, file_root: Path, acl: ProjectAcl) -> None:
        policy = FileAccessPolicy(file_root, acl=acl)
        context = SecurityContext(files=policy)
        assert context.acl is acl
        assert context.files.acl is acl

    def test_context_rebinds_policy_to_its_acl(self, file_root: Path, acl: ProjectAcl) -> None:
        policy = FileAccessPolicy(file_root)  # 自带默认 ACL
        assert policy.acl is not acl
        context = SecurityContext(files=policy, acl=acl)
        assert context.acl is acl
        assert context.files.acl is acl

    def test_default_policy_keeps_legacy_fallback(self, file_root: Path) -> None:
        policy = FileAccessPolicy(file_root)
        identity = Identity(subject="legacy-user", roles=(), projects=("P-001",))
        grant = policy.resolve(identity, "P-001/boq.json")
        assert grant.role is ProjectRole.EDITOR
        stranger = Identity(subject="stranger", roles=(), projects=())
        with pytest.raises(FileAccessError) as exc:
            policy.resolve(stranger, "P-001/boq.json")
        assert exc.value.code == "AUTH_FILE_FORBIDDEN"

    def test_policy_write_is_regulated_by_same_acl(self, file_root: Path, acl: ProjectAcl) -> None:
        policy = FileAccessPolicy(file_root, acl=acl)
        viewer = Identity(subject="viewer", roles=(), projects=())
        editor = Identity(subject="editor", roles=(), projects=())
        with pytest.raises(FileAccessError) as exc:
            policy.write(viewer, "P-001/sub/x.json", b"{}")
        assert exc.value.code == "AUTH_FILE_FORBIDDEN"
        assert exc.value.status_code == 403
        grant = policy.write(editor, "P-001/sub/x.json", b"{}")
        assert grant.role is ProjectRole.EDITOR
        assert (file_root / "P-001" / "sub" / "x.json").read_bytes() == b"{}"

    def test_policy_write_limits_and_payload_type(self, file_root: Path, acl: ProjectAcl) -> None:
        policy = FileAccessPolicy(file_root, acl=acl, max_write_bytes=4)
        editor = Identity(subject="editor", roles=(), projects=())
        with pytest.raises(FileAccessError) as exc:
            policy.write(editor, "P-001/sub/big.json", b"12345")
        assert exc.value.code == "AUTH_FILE_TOO_LARGE"
        assert exc.value.status_code == 413
        with pytest.raises(FileAccessError) as exc:
            policy.write(editor, "P-001/sub/x.json", "text")  # type: ignore[arg-type]
        assert exc.value.code == "AUTH_FILE_PAYLOAD_INVALID"
        assert not (file_root / "P-001" / "sub" / "big.json").exists()

    def test_policy_write_overwrites_atomically(self, file_root: Path, acl: ProjectAcl) -> None:
        policy = FileAccessPolicy(file_root, acl=acl)
        editor = Identity(subject="editor", roles=(), projects=())
        policy.write(editor, "P-001/boq.json", b"first")
        policy.write(editor, "P-001/boq.json", b"second")
        assert (file_root / "P-001" / "boq.json").read_bytes() == b"second"
        assert not list((file_root / "P-001").glob(".upload-*"))

    def test_install_security_mounts_project_routes(self, settings: SecuritySettings, file_root: Path) -> None:
        main_app = FastAPI(title="isolated ACL installer harness")
        from txbuddy.security import install_security

        context = build_context(
            settings=settings, directory=make_directory(), file_root=file_root, acl=make_acl()
        )
        install_security(main_app, context, prefix="/acl-mount")
        with TestClient(main_app) as mounted:
            token = login(mounted, "editor", prefix="/acl-mount")
            assert mounted.get("/acl-mount/projects/P-001", headers=bearer(token)).status_code == 200
            cross = mounted.get("/acl-mount/projects/P-002", headers=bearer(token))
            assert cross.status_code == 403
            assert check_code(cross) == "ACL_NOT_MEMBER"
            assert mounted.get("/acl-mount/files/P-002/secret.txt", headers=bearer(token)).status_code == 403
