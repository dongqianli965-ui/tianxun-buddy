"""P0-04 门禁记录真实性测试：客户端伪造 vs 服务端事实。"""
import os

import pytest
from tests.authenticated_client import TestClient, synthetic_professional_registry
from txbuddy.api import app

client = TestClient(app)

@pytest.fixture(autouse=True)
def synthetic_registry(tmp_path, monkeypatch):
    synthetic_professional_registry(tmp_path, monkeypatch, ["u"])
PACK = {"basis_pack_id": "B-G", "region_code": "410100", "region_level": "CITY", "major": "BUILDING",
        "effective_date": "2026-01-01", "measurement_standard_version": "GB/T 50854-2024",
        "pricing_standard_version": "GB/T 50500-2024", "rule_version_scope": "HENAN_2025",
        "precision_rule_version": "P-V1", "status": "LOCKED", "test_only": False,
        "sources": [{"source_id": "S1", "title": "定额", "source_type": "QUOTA",
                     "license_status": "LICENSED", "verified": True}]}


def mkjob():
    r = client.post("/v1/job", json={"project_id": "P-G", "name": "门禁测试", "business_line": "L1",
                                     "basis_pack": PACK, "created_by": "tester"})
    jid = r.json()["job_id"]
    for d in ["contract_or_entrustment", "drawing_set", "design_description", "finish_schedule"]:
        client.post(f"/v1/job/{jid}/documents", json={"doc_key": d, "version": "v1",
                                                      "file_sha256": "0" * 64, "verified": True})
    return jid


def gate(jid, code):
    return client.post("/v1/gate/check", json={"job_id": jid, "business_line": "L1",
                                               "gate_code": code, "basis_pack": PACK}).json()


def advance_to_g4(jid, server=False):
    """推进到 G4 之前：strict 模式下 reconcile/price 必须是服务端记录。"""
    tok = {"X-Internal-Token": "v424-legacy-fixture-token"}
    client.post(f"/v1/job/{jid}/records", json={"kind": "boq", "payload": {"content_sha256": "x"}})
    calc = client.post("/v1/quantity/rule-calc", json={"job_id": jid, "basis_pack": PACK,
                "rule_code": "QTY.BOQ.CONC.COLUMN.001",
                "params": {"b": "0.5", "h": "0.5", "H": "8"}}).json()
    if server:
        client.post(f"/internal/job/{jid}/records", headers=tok,
                    json={"kind": "reconcile",
                          "payload": {"reconciled": True, "calc_run_ids": [calc["calc_run_id"]]}})
        client.post(f"/internal/job/{jid}/records", headers=tok,
                    json={"kind": "price",
                          "payload": {"source_ref": "Q-1", "evidence_id": calc["evidence_id"]}})
    else:
        client.post(f"/v1/job/{jid}/records", json={"kind": "reconcile", "payload": {"reconciled": True}})
        client.post(f"/v1/job/{jid}/records", json={"kind": "price", "payload": {"source_ref": "Q-1"}})
    for node, role, lic in [("SELF_REVIEW", "REVIEWER", None), ("REVIEW", "REVIEWER", None),
                            ("APPROVE", "PRINCIPAL", "REGISTERED_COST_ENGINEER")]:
        tid = client.post(f"/v1/job/{jid}/tasks", json={"node": node, "payload": {}}).json()["task_id"]
        client.post(f"/v1/task/{tid}/start", json={"actor": "u", "role": "EDITOR"})
        client.post(f"/v1/task/{tid}/complete", json={"actor": "u", "role": "EDITOR", "result": {}})
        client.post(f"/v1/task/{tid}/approve", headers={"X-Actor": "u"}, json={"actor": "u", "role": role, "license": lic})
    for code in ["G0", "G1", "G2", "G3a", "G3b", "G3c"]:
        gate(jid, code)


def test_client_report_rejected_in_strict_mode(monkeypatch):
    monkeypatch.setenv("TX_STRICT_GATES", "1")
    monkeypatch.setenv("TX_INTERNAL_TOKEN", "v424-legacy-fixture-token")
    jid = mkjob()
    advance_to_g4(jid, server=True)   # 前置门禁的勾稽/组价必须是服务端事实
    client.post(f"/v1/job/{jid}/records", json={"kind": "report",
                "payload": {"generated": True, "layout_checked": True}})
    out = gate(jid, "G4")
    assert out["result"] == "FAIL"
    assert any(f["rule_id"] == "G4.SOURCE" for f in out["failed_rules"]), out


def test_server_report_with_binding_passes_strict(monkeypatch):
    monkeypatch.setenv("TX_STRICT_GATES", "1")
    monkeypatch.setenv("TX_INTERNAL_TOKEN", "v424-legacy-fixture-token")
    jid = mkjob()
    advance_to_g4(jid, server=True)
    r = client.post(f"/internal/job/{jid}/records",
                    json={"kind": "report", "payload": {"generated": True, "layout_checked": True,
                                                        "report_sha256": "a" * 64,
                                                        "evidence_ids": ["e1"]}},
                    headers={"X-Internal-Token": "v424-legacy-fixture-token"})
    assert r.status_code == 200 and r.json()["source"] == "SERVER"
    assert gate(jid, "G4")["result"] == "PASS"


def test_internal_endpoint_requires_token():
    jid = mkjob()
    r = client.post(f"/internal/job/{jid}/records",
                    json={"kind": "report", "payload": {"generated": True}})
    assert r.status_code == 403 and "SEAL_NOT_ALLOWED" in r.text


def test_server_record_requires_binding_fields(monkeypatch):
    monkeypatch.setenv("TX_INTERNAL_TOKEN", "v424-legacy-fixture-token")
    jid = mkjob()
    r = client.post(f"/internal/job/{jid}/records",
                    json={"kind": "report", "payload": {"generated": True}},
                    headers={"X-Internal-Token": "v424-legacy-fixture-token"})
    assert r.status_code == 422 and "report_sha256" in r.text


def test_reconcile_binding_calc_run_must_exist(monkeypatch):
    monkeypatch.setenv("TX_INTERNAL_TOKEN", "v424-legacy-fixture-token")
    jid = mkjob()
    r = client.post(f"/internal/job/{jid}/records",
                    json={"kind": "reconcile", "payload": {"reconciled": True,
                                                           "calc_run_ids": ["not-exist"]}},
                    headers={"X-Internal-Token": "v424-legacy-fixture-token"})
    assert r.status_code == 422 and "GATE_QTY_RECONCILE_FAILED" in r.text


def test_compat_mode_keeps_legacy_records(monkeypatch):
    monkeypatch.delenv("TX_STRICT_GATES", raising=False)
    jid = mkjob()
    advance_to_g4(jid)
    client.post(f"/v1/job/{jid}/records", json={"kind": "report",
                "payload": {"generated": True, "layout_checked": True}})
    assert gate(jid, "G4")["result"] == "PASS"
