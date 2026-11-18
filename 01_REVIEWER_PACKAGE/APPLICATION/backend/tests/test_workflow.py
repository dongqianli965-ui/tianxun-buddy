"""TX-020 JobTask / 工作流 / 人工确认测试。"""
import pytest
from tests.authenticated_client import TestClient, synthetic_professional_registry
from txbuddy.api import app
from txbuddy.security.tokens import encode_token
from txbuddy.security.acl import ProjectRole

client = TestClient(app)

@pytest.fixture(autouse=True)
def synthetic_registry(tmp_path, monkeypatch):
    synthetic_professional_registry(tmp_path, monkeypatch, ["u1"])


def mkjob():
    body = {"project_id": "P1", "name": "L1 测试项目", "business_line": "L1",
            "basis_pack": {"basis_pack_id": "B1", "region_code": "410100", "region_level": "CITY",
                           "major": "BUILDING", "effective_date": "2026-01-01",
                           "measurement_standard_version": "GB/T 50854-2024",
                           "pricing_standard_version": "GB/T 50500-2024",
                           "rule_version_scope": "HENAN_2025", "precision_rule_version": "P-V1",
                           "status": "DRAFT", "test_only": True}, "created_by": "tester"}
    return client.post("/v1/job", json=body).json()["job_id"]


def mktask(job_id, node="REVIEW_BOQ"):
    return client.post(f"/v1/job/{job_id}/tasks", json={"node": node, "payload": {"x": 1}}).json()["task_id"]


def test_job_create_and_get():
    jid = mkjob()
    job = client.get(f"/v1/job/{jid}").json()
    assert job["business_line"] == "L1" and job["status"] == "CREATED"
    assert job["documents"] == [] and job["tasks"] == []


def test_task_lifecycle_with_approval():
    jid = mkjob(); tid = mktask(jid)
    assert client.post(f"/v1/task/{tid}/start", json={"actor": "u1", "role": "EDITOR"}).json()["status"] == "RUNNING"
    r = client.post(f"/v1/task/{tid}/complete", json={"actor": "u1", "role": "EDITOR", "result": {"ok": True}})
    assert r.json()["status"] == "WAITING_APPROVAL"
    # 无权限角色确认 → 拒绝
    app.state.security.acl.grant("P1", "u2", ProjectRole.EDITOR, granted_by="test")
    editor_token = encode_token(subject="u2", settings=app.state.security.settings, roles=())
    bad = client.post(f"/v1/task/{tid}/approve", headers={"Authorization": "Bearer " + editor_token}, json={"actor": "u2", "role": "EDITOR"})
    assert bad.status_code == 403 and "ACL_FORBIDDEN" in bad.text
    # 复核员确认 → DONE
    ok = client.post(f"/v1/task/{tid}/approve", headers={"X-Actor": "u3"}, json={"actor": "u3", "role": "REVIEWER", "comment": "ok"})
    assert ok.json()["status"] == "DONE" and ok.json()["approver"] == "u3"


def test_approve_node_requires_licensed_engineer():
    jid = mkjob(); tid = mktask(jid, "APPROVE")
    client.post(f"/v1/task/{tid}/start", json={"actor": "u1", "role": "EDITOR"})
    client.post(f"/v1/task/{tid}/complete", json={"actor": "u1", "role": "EDITOR"})
    bad = client.post(f"/v1/task/{tid}/approve", headers={"X-Actor": "u1"}, json={"actor": "u1", "role": "PRINCIPAL"})
    assert bad.status_code == 403 and "PROFESSIONAL_LICENSE_MISMATCH" in bad.text
    ok = client.post(f"/v1/task/{tid}/approve", headers={"X-Actor": "u1"}, json={"actor": "u1", "role": "PRINCIPAL",
                                                    "license": "REGISTERED_COST_ENGINEER"})
    assert ok.json()["status"] == "DONE"


def test_fail_and_retry_and_history():
    jid = mkjob(); tid = mktask(jid)
    client.post(f"/v1/task/{tid}/start", json={"actor": "u1", "role": "EDITOR"})
    assert client.post(f"/v1/task/{tid}/fail", json={"actor": "u1", "reason": "资料不全"}).json()["status"] == "FAILED"
    assert client.post(f"/v1/task/{tid}/retry", json={"actor": "u1"}).json()["status"] == "READY"
    hist = client.get(f"/v1/task/{tid}").json()["history"]
    assert len(hist) >= 3 and hist[-1]["to"] == "READY"


def test_illegal_transition_rejected():
    jid = mkjob(); tid = mktask(jid)
    r = client.post(f"/v1/task/{tid}/approve", headers={"X-Actor": "u1"}, json={"actor": "u1", "role": "REVIEWER"})
    assert r.status_code == 422 and "SUMMARY_REVIEW_FAILED" in r.text
