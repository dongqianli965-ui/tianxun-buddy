"""L1 端到端集成：项目 → 资料 → BOQ → 算量 → 证据 → 门禁 → 成果 → 归档 → 复算。

严格走服务端状态：门禁必须绑定 job_id，context 不被采信。
"""
import io
import pytest
from tests.authenticated_client import TestClient, synthetic_professional_registry
from openpyxl import Workbook
from txbuddy.api import app

client = TestClient(app)

@pytest.fixture(autouse=True)
def synthetic_registry(tmp_path, monkeypatch):
    synthetic_professional_registry(tmp_path, monkeypatch, ["approve"])
PACK = {"basis_pack_id": "B-L1", "region_code": "410100", "region_level": "CITY", "major": "BUILDING",
        "effective_date": "2026-01-01", "measurement_standard_version": "GB/T 50854-2024",
        "pricing_standard_version": "GB/T 50500-2024", "rule_version_scope": "HENAN_2025",
        "precision_rule_version": "P-V1", "status": "LOCKED", "test_only": False,
        "sources": [{"source_id": "S1", "title": "定额", "source_type": "QUOTA",
                     "license_status": "LICENSED", "verified": True}]}


def test_l1_end_to_end_pipeline():
    job = client.post("/v1/job", json={"project_id": "P-L1", "name": "L1 住宅预算", "business_line": "L1",
                                       "basis_pack": PACK, "created_by": "pm"}).json()
    jid = job["job_id"]

    # 1 资料登记（必备 4 类）
    for doc in ["contract_or_entrustment", "drawing_set", "design_description", "finish_schedule"]:
        client.post(f"/v1/job/{jid}/documents", json={"doc_key": doc, "version": "v1",
                                                      "file_sha256": "0"*64, "verified": True})

    # 2 BOQ 导入
    wb = Workbook(); ws = wb.active
    ws.append(["项目编码", "项目名称", "项目特征", "计量单位", "工程量", "分部"])
    ws.append(["010502001001", "现浇混凝土矩形柱", "C30", "m³", 2, "混凝土工程"])
    ws.append(["010401003001", "实心砖墙", "MU10", "m3", 10, "砌筑工程"])
    buf = io.BytesIO(); wb.save(buf)
    boq = client.post("/v1/boq/import", files={"file": ("boq.xlsx", buf.getvalue(), "x")}).json()
    assert boq["stats"]["valid"] == 2
    client.post(f"/v1/job/{jid}/records", json={"kind": "boq", "payload": {"content_sha256": boq["content_sha256"]}})

    # 3 G0 / G1
    g0 = client.post("/v1/gate/check", json={"job_id": jid, "business_line": "L1", "gate_code": "G0",
                                             "basis_pack": PACK}).json()
    assert g0["result"] == "PASS", g0
    g1 = client.post("/v1/gate/check", json={"job_id": jid, "business_line": "L1", "gate_code": "G1",
                                             "basis_pack": PACK}).json()
    assert g1["result"] == "PASS", g1

    # 4 算量（规则）+ 勾稽记录
    calc = client.post("/v1/quantity/rule-calc", json={"job_id": jid, "basis_pack": PACK,
                                                       "rule_code": "QTY.BOQ.CONC.COLUMN.001",
                                                       "params": {"b": "0.5", "h": "0.5", "H": "8"}}).json()
    assert calc["result"] == "2.000"
    client.post(f"/v1/job/{jid}/records", json={"kind": "reconcile", "payload": {"reconciled": True}})
    client.post(f"/v1/job/{jid}/records", json={"kind": "diff", "payload": {"unexplained": 0}})
    g2 = client.post("/v1/gate/check", json={"job_id": jid, "business_line": "L1", "gate_code": "G2",
                                             "basis_pack": PACK}).json()
    assert g2["result"] == "PASS", g2

    # 5 组价记录 + G3a
    client.post(f"/v1/job/{jid}/records", json={"kind": "price",
                "payload": {"item": "010502001001", "source_ref": "QUOTA-TEST"}})
    assert client.post("/v1/gate/check", json={"job_id": jid, "business_line": "L1", "gate_code": "G3a",
                                               "basis_pack": PACK}).json()["result"] == "PASS"

    # 6 三级复核任务 + G3b / G3c
    for node, role, lic in [("SELF_REVIEW", "EDITOR", None), ("REVIEW", "REVIEWER", None),
                            ("APPROVE", "PRINCIPAL", "REGISTERED_COST_ENGINEER")]:
        tid = client.post(f"/v1/job/{jid}/tasks", json={"node": node, "payload": {}}).json()["task_id"]
        client.post(f"/v1/task/{tid}/start", json={"actor": node.lower(), "role": "EDITOR"})
        client.post(f"/v1/task/{tid}/complete", json={"actor": node.lower(), "role": "EDITOR", "result": {}})
        if node != "SELF_REVIEW":
            r = client.post(f"/v1/task/{tid}/approve", headers={"X-Actor": node.lower()}, json={"actor": node.lower(), "role": role, "license": lic})
            assert r.json()["status"] == "DONE", r.text
        else:
            r = client.post(f"/v1/task/{tid}/approve", headers={"X-Actor": "self"}, json={"actor": "self", "role": "REVIEWER"})
            assert r.json()["status"] == "DONE", r.text
    g3b = client.post("/v1/gate/check", json={"job_id": jid, "business_line": "L1", "gate_code": "G3b",
                                              "basis_pack": PACK}).json()
    assert g3b["result"] == "PASS", g3b
    g3c = client.post("/v1/gate/check", json={"job_id": jid, "business_line": "L1", "gate_code": "G3c",
                                              "basis_pack": PACK}).json()
    assert g3c["result"] == "PASS", g3c

    # 7 成果 + G4
    client.post(f"/v1/job/{jid}/records", json={"kind": "report",
                "payload": {"generated": True, "layout_checked": True}})
    g4 = client.post("/v1/gate/check", json={"job_id": jid, "business_line": "L1", "gate_code": "G4",
                                             "basis_pack": PACK}).json()
    assert g4["result"] == "PASS", g4

    # 8 归档 + G5
    client.post(f"/v1/job/{jid}/records", json={"kind": "archive",
                "payload": {"volume_complete": True, "package_sha256": "f"*64, "indicators_stored": True}})
    g5 = client.post("/v1/gate/check", json={"job_id": jid, "business_line": "L1", "gate_code": "G5",
                                             "basis_pack": PACK}).json()
    assert g5["result"] == "PASS", g5

    # 9 历史复算
    run = client.get(f"/v1/calc/{calc['calc_run_id']}").json()
    rec = client.post("/v1/calc/recalc", json={"basis_pack": PACK, "snapshot": run["snapshot"],
                                               "stored_value": calc["result"]}).json()
    assert rec["reproducible"] is True

    # 10 顺序与越权校验
    skip = client.post("/v1/gate/check", json={"job_id": jid, "business_line": "L1", "gate_code": "G0",
                                               "basis_pack": PACK}).json()
    assert skip["result"] == "PASS"  # 已通过的门可复检
    wrong = client.post("/v1/gate/check", json={"job_id": jid, "business_line": "L2", "gate_code": "G0",
                                                "basis_pack": PACK}).json()
    assert wrong.get("result") != "PASS" or wrong.get("detail")  # 业务线不匹配
