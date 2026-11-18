"""A-00-ACCEPT 端到端合成项目：一条真实业务链路跑到底。

链路：建项目 → 传资料 → 导入清单 → 算量(规则) → 套定额/附注 → 组价 →
       G0/G1/G2/G3a/G3b/G3c → 生成十类成果 → 报审成果记录 → G4 →
       归档 → G5 → 证据反查/校验 → 历史复算 → 审计留痕。

原则：全部走 FastAPI 路由与服务端 SQLite 状态，不直接伪造门禁结果；
      门禁缺前置即 FAIL、证据不可验证即失败，不允许 skip。
"""
from __future__ import annotations

import hashlib
import io
import uuid
from decimal import Decimal
from pathlib import Path

from tests.authenticated_client import TestClient, synthetic_professional_registry
from openpyxl import Workbook, load_workbook

from txbuddy.api import app, store
from txbuddy.deliverables import SPECS, DeliverableDataset, DeliverableService

client = TestClient(app)
TAG = uuid.uuid4().hex[:10]


def _ok(response, code: int = 200):
    assert response.status_code == code, response.text
    return response.json()


def _gate(jid: str, line: str, code: str, pack: dict) -> dict:
    return _ok(client.post("/v1/gate/check", json={
        "job_id": jid, "business_line": line, "gate_code": code, "basis_pack": pack}))


def test_v423_synthetic_project_full_chain(tmp_path: Path, monkeypatch):
    synthetic_professional_registry(tmp_path, monkeypatch, ["approve"])
    pack = {
        "basis_pack_id": "B-E2E-" + TAG,
        "region_code": "410100",
        "region_level": "CITY",
        "major": "BUILDING",
        "effective_date": "2026-01-01",
        "measurement_standard_version": "GB/T 50854-2024",
        "pricing_standard_version": "GB/T 50500-2024",
        "rule_version_scope": "HENAN_2025",
        "precision_rule_version": "P-V1",
        "status": "LOCKED",
        "test_only": False,
        "sources": [{
            "source_id": "S-E2E", "title": "河南省 2025 计价依据", "source_type": "QUOTA",
            "license_status": "LICENSED", "verified": True,
        }],
    }

    # 1 建项目
    job = _ok(client.post("/v1/job", json={
        "project_id": "P-E2E-" + TAG, "name": "端到端合成项目 " + TAG,
        "business_line": "L1", "basis_pack": pack, "created_by": "e2e-pm"}))
    jid = job["job_id"]
    assert job["business_line"] == "L1" and job["basis_pack_id"] == pack["basis_pack_id"]

    # 2 传资料：G1 必备四类，全部带 SHA-256 且已校验
    for doc_key in ("contract_or_entrustment", "drawing_set", "design_description",
                    "finish_schedule"):
        docs = _ok(client.post(f"/v1/job/{jid}/documents", json={
            "doc_key": doc_key, "version": "v1",
            "file_sha256": hashlib.sha256((jid + doc_key).encode()).hexdigest(),
            "verified": True}))
        assert any(d["doc_key"] == doc_key and d["verified"] for d in docs["documents"])

    # 3 导入清单（真实 XLSX 上传）
    wb = Workbook()
    ws = wb.active
    ws.append(["项目编码", "项目名称", "项目特征", "计量单位", "工程量", "分部"])
    ws.append(["010502001001", "现浇混凝土矩形柱", "C30", "m³", 2, "混凝土工程"])
    ws.append(["010401003001", "实心砖墙", "MU10", "m3", 10, "砌筑工程"])
    buf = io.BytesIO()
    wb.save(buf)
    boq = _ok(client.post("/v1/boq/import", files={
        "file": ("boq.xlsx", buf.getvalue(),
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}))
    assert boq["stats"]["valid"] == 2 and boq["stats"]["invalid"] == 0
    assert len(boq["content_sha256"]) == 64

    prices = {"010502001001": "620.50", "010401003001": "480.00"}
    boq_rows = [{
        "item_code": row["code"], "name": row["name"], "unit": row["unit"],
        "quantity": row["quantity"], "unit_price": prices[row["code"]],
        "section": row["section"],
    } for row in boq["items"]]
    _ok(client.post(f"/v1/job/{jid}/records", json={"kind": "boq", "payload": {
        "content_sha256": boq["content_sha256"], "file_sha256": boq["file_sha256"],
        "items": boq_rows}}))

    # 4 G0 / G1：依据包与资料门禁由服务端真实记录判定
    g0 = _gate(jid, "L1", "G0", pack)
    assert g0["result"] == "PASS", g0
    assert g0["authoritative"] is True and g0["context_ignored"] is True
    g1 = _gate(jid, "L1", "G1", pack)
    assert g1["result"] == "PASS", g1

    # 5 算量：两条规则、两个 calc_run，均产生可追溯证据
    column = _ok(client.post("/v1/quantity/rule-calc", json={
        "job_id": jid, "basis_pack": pack, "rule_code": "QTY.BOQ.CONC.COLUMN.001",
        "params": {"b": "0.5", "h": "0.5", "H": "8"}}))
    wall = _ok(client.post("/v1/quantity/rule-calc", json={
        "job_id": jid, "basis_pack": pack, "rule_code": "QTY.BOQ.MASONRY.WALL.001",
        "params": {"L": "6", "H": "3", "T": "0.24", "opening_area": "2.1"}}))
    assert column["result"] == "2.000" and wall["result"] == "3.816"
    assert column["evidence_sha256"] and wall["evidence_sha256"]
    run_ids = [column["calc_run_id"], wall["calc_run_id"]]
    _ok(client.post(f"/v1/job/{jid}/records", json={"kind": "quantity_calc",
                    "payload": {"items": [
                        {"item_code": "010502001001", "name": "现浇混凝土矩形柱",
                         "formula": column["formula"], "unit": "m3",
                         "quantity": column["result"],
                         "rule_code": "QTY.BOQ.CONC.COLUMN.001", "rule_version": "2026.1",
                         "standard_ref": column["standard_ref"], "note": "b×h×H"},
                        {"item_code": "010401003001", "name": "实心砖墙",
                         "formula": wall["formula"], "unit": "m3",
                         "quantity": wall["result"],
                         "rule_code": "QTY.BOQ.MASONRY.WALL.001", "rule_version": "2026.1",
                         "standard_ref": wall["standard_ref"], "note": "扣门窗洞口"}]}}))

    _ok(client.post(f"/v1/job/{jid}/records", json={"kind": "reconcile", "payload": {
        "reconciled": True, "calc_run_ids": run_ids}}))
    _ok(client.post(f"/v1/job/{jid}/records", json={"kind": "diff", "payload": {
        "unexplained": 0, "explained": 2}}))
    g2 = _gate(jid, "L1", "G2", pack)
    assert g2["result"] == "PASS", g2

    # 6 套定额：精确映射 → 人工确认后才可套用；附注按册→章→子目顺序执行并留痕
    match = _ok(client.post("/v1/quota/match", json={
        "boq_item": {"code": "010502001001", "name": "现浇混凝土矩形柱", "unit": "m3",
                     "major": "BUILDING", "tags": ["混凝土", "柱"]},
        "quota_items": [{
            "id": "Q-CONC-COL", "item_code": "5-2-8", "name": "现浇混凝土矩形柱",
            "unit": "m3", "major": "BUILDING", "work_tags": ["混凝土", "柱"],
            "source_ref": "河南省2025定额 第五章", "license_status": "LICENSED",
            "status": "ACTIVE", "mapped_boq_code": "010502001001"}],
        "exact_mappings": {"010502001001": "Q-CONC-COL"}}))
    assert match["recommended_id"] == "Q-CONC-COL", match
    assert match["candidates"][0]["excluded"] is False
    assert match["need_human"] is True

    notes = _ok(client.post("/v1/quota/apply-notes", json={
        "consumption": [
            {"resource_code": "R-LAB-01", "consumption": "12.5"},
            {"resource_code": "R-MAT-01", "consumption": "1.02"},
        ],
        "notes": [{"id": "N-1", "scope_level": "ITEM", "priority": 1,
                   "effect_type": "FACTOR", "factor": "1.1",
                   "target_resource_code": "R-LAB-01",
                   "source_ref": "河南省2025定额 第五章 附注3"}],
        "conversion": {"allowed": True}}))
    assert Decimal(notes["consumption"][0]["consumption"]) == Decimal("13.750")
    assert notes["execution_trace"][0]["source_ref"].startswith("河南省2025定额")

    # 7 组价：资源消耗 × 单价 → 直接费 → 管理费/利润，费率来源必须显式
    unit_price = _ok(client.post("/v1/price/unit", json={
        "resources": [
            {"resource_type": "LABOR", "resource_code": "R-LAB-01", "consumption": "13.750",
             "unit_price": "180", "price_ref": "信息价2026-01"},
            {"resource_type": "MATERIAL", "resource_code": "R-MAT-01", "consumption": "1.05",
             "unit_price": "520", "price_ref": "信息价2026-01"},
            {"resource_type": "MACHINE", "resource_code": "R-MAC-01", "consumption": "0.5",
             "unit_price": "1200", "price_ref": "租赁合同2026"},
        ],
        "management_rate": "0.05", "profit_rate": "0.03",
        "management_source_ref": "依据包-管理费", "profit_source_ref": "依据包-利润"}))
    assert Decimal(unit_price["unit_price"]) > Decimal("3000")
    assert Decimal(unit_price["unit_price"]) == (
        Decimal(unit_price["labor"]) + Decimal(unit_price["material"])
        + Decimal(unit_price["machine"]) + Decimal(unit_price["management"])
        + Decimal(unit_price["profit"]) + Decimal(unit_price["risk_fee"]))

    adjust = _ok(client.post("/v1/price/index-adjust", json={
        "basis_pack": pack, "base_amount": "10000", "base_index": "100",
        "current_index": "106", "risk_rate": "0.05", "mode": "WITHIN_BAND_ONLY",
        "contract_clause_ref": "合同 13.2 条"}))
    assert adjust["delta_amount"] == "100.00" and adjust["change_ratio"] == "0.06"

    total = _ok(client.post("/v1/price/project-total", json={
        "basis_pack": pack, "boq_total": "100000", "measure_total": "5000",
        "other_total": "0", "tax_rate": "0.09", "formal_calculation": False,
        "fee_items": [{"fee_item": "安全文明施工费", "base_def": "BOQ_TOTAL",
                       "amount": "3200", "source_ref": "豫建价[2025]1号"}]}))
    assert Decimal(total["project_total"]) > Decimal("100000")

    evidence_id = column["evidence_id"]
    _ok(client.post(f"/v1/job/{jid}/records", json={"kind": "price", "payload": {
        "item": "010502001001", "source_ref": "河南省2025定额 第五章",
        "evidence_id": evidence_id, "unit_price": unit_price["unit_price"]}}))
    _ok(client.post(f"/v1/job/{jid}/records", json={"kind": "unit_price", "payload": {"items": [
        {"item_code": "010502001001", "name": "现浇混凝土矩形柱", "unit": "m3",
         "quantity": "2", "labor": unit_price["labor"], "material": unit_price["material"],
         "machine": unit_price["machine"], "management": unit_price["management"],
         "profit": unit_price["profit"], "risk_fee": unit_price["risk_fee"],
         "management_source_ref": "依据包-管理费", "profit_source_ref": "依据包-利润",
         "resource_codes": ["R-LAB-01", "R-MAT-01", "R-MAC-01"]}]}}))
    _ok(client.post(f"/v1/job/{jid}/records", json={"kind": "quota_match", "payload": {"items": [
        {"item_code": "010502001001", "name": "现浇混凝土矩形柱", "quota_code": "5-2-8",
         "quota_name": "现浇混凝土矩形柱", "unit": "m3", "quantity": "2",
         "labor": "2475.00", "material": "546.00", "machine": "600.00"}]}}))
    _ok(client.post(f"/v1/job/{jid}/records", json={"kind": "resources", "payload": {"items": [
        {"resource_type": "LABOR", "resource_code": "R-LAB-01", "name": "综合工日",
         "unit": "工日", "quantity": "13.75", "unit_price": "180",
         "price_ref": "信息价2026-01", "item_code": "010502001001"},
        {"resource_type": "MATERIAL", "resource_code": "R-MAT-01", "name": "C30 混凝土",
         "unit": "m3", "quantity": "1.05", "unit_price": "520",
         "price_ref": "信息价2026-01", "item_code": "010502001001"}]}}))
    _ok(client.post(f"/v1/job/{jid}/records", json={"kind": "fees", "payload": {
        "measure_total": "5000", "other_total": "0", "tax_rate": "0.09",
        "remark": "依据包费率", "items": [{
            "fee_item": "安全文明施工费", "base_def": "BOQ_TOTAL",
            "base_amount": "100000", "rate": "0.032", "amount": "3200",
            "source_ref": "豫建价[2025]1号", "noncompetitive": True}]}}))
    _ok(client.post(f"/v1/job/{jid}/records", json={"kind": "index_adjustment",
                    "payload": {"items": [{
                        "resource_code": "R-MAT-01", "name": "C30 混凝土",
                        "base_amount": "10000", "base_index": "100", "current_index": "106",
                        "risk_rate": "0.05", "mode": "WITHIN_BAND_ONLY",
                        "delta_amount": adjust["delta_amount"],
                        "contract_clause_ref": "合同 13.2 条"}]}}))
    _ok(client.post(f"/v1/job/{jid}/records", json={"kind": "audit_diff", "payload": {"items": [{
        "item_code": "010502001001", "name": "现浇混凝土矩形柱", "unit": "m3",
        "submitted_qty": "2", "audited_qty": "2", "submitted_amount": "1241.00",
        "audited_amount": "1241.00", "reason": "工程量按实核定", "evidence_id": evidence_id}]}}))

    g3a = _gate(jid, "L1", "G3a", pack)
    assert g3a["result"] == "PASS", g3a

    # 8 三级复核：SELF_REVIEW → REVIEW → APPROVE，签署人须有执业资格
    for node, approve_role, license_no in (("SELF_REVIEW", "REVIEWER", None),
                                            ("REVIEW", "REVIEWER", None),
                                            ("APPROVE", "PRINCIPAL", "REGISTERED_COST_ENGINEER")):
        task = _ok(client.post(f"/v1/job/{jid}/tasks", json={
            "node": node, "payload": {"job_id": jid}, "assignee": node.lower()}))
        tid = task["task_id"]
        _ok(client.post(f"/v1/task/{tid}/start", json={"actor": node.lower(), "role": "EDITOR"}))
        _ok(client.post(f"/v1/task/{tid}/complete", json={
            "actor": node.lower(), "role": "EDITOR", "result": {"node": node}}))
        approved = _ok(client.post(f"/v1/task/{tid}/approve", headers={"X-Actor": node.lower()}, json={
            "actor": node.lower(), "role": approve_role, "license": license_no,
            "comment": "端到端复核通过"}))
        assert approved["status"] == "DONE", approved

    g3b = _gate(jid, "L1", "G3b", pack)
    assert g3b["result"] == "PASS", g3b
    g3c = _gate(jid, "L1", "G3c", pack)
    assert g3c["result"] == "PASS", g3c

    # 9 成果：十类文件真实落盘，XLSX/PDF 可重新打开
    dataset = DeliverableDataset.from_store(store, jid, prepared_by="e2e-worker")
    assert len(dataset.boq) == 2
    assert len(dataset.quantities) == 2
    assert len(dataset.unit_prices) == 1 and len(dataset.quotas) == 1
    service = DeliverableService(tmp_path / "deliverables", store.get)
    manifest = service.generate_all(dataset)
    assert len(SPECS) == 10 and len(manifest.files) == 10
    for item in manifest.files:
        assert item.path.is_file() and item.path.stat().st_size > 0
        assert item.sha256 == hashlib.sha256(item.path.read_bytes()).hexdigest()
    budget = load_workbook(manifest.by_kind("budget_xlsx").path)
    assert budget.sheetnames == ["封面", "总预算表", "单位工程预算表"]
    raw_pdf = manifest.by_kind("budget_pdf").path.read_bytes()
    assert raw_pdf.startswith(b"%PDF-1.4") and raw_pdf.rstrip().endswith(b"%%EOF")

    # 10 成果门 G4：先验证缺成果必 FAIL，再写入带指纹与证据绑定的成果记录
    pre_g4 = _gate(jid, "L1", "G4", pack)
    assert pre_g4["result"] == "FAIL", pre_g4
    assert any(f["error_code"] == "REPORT_GATE_NOT_READY" for f in pre_g4["failed_rules"])
    _ok(client.post(f"/v1/job/{jid}/records", json={"kind": "report", "payload": {
        "generated": True, "layout_checked": True,
        "report_sha256": manifest.manifest_sha256,
        "evidence_ids": [column["evidence_id"], wall["evidence_id"]],
        "ai_direct_numbers": 0, "manifest_path": str(manifest.manifest_path)}}))
    g4 = _gate(jid, "L1", "G4", pack)
    assert g4["result"] == "PASS", g4

    # 11 归档 G5
    _ok(client.post(f"/v1/job/{jid}/records", json={"kind": "archive", "payload": {
        "volume_complete": True, "package_sha256": manifest.manifest_sha256,
        "volume_no": "V-E2E-001", "indicators_stored": True}}))
    g5 = _gate(jid, "L1", "G5", pack)
    assert g5["result"] == "PASS", g5

    # 12 证据链：逐条反查 + 摘要校验 + 历史复算
    evidence = _ok(client.get("/v1/evidence/" + evidence_id))
    assert evidence["evidence_id"] == evidence_id and evidence["sha256"]
    verified = _ok(client.post("/v1/evidence/verify", json=evidence))
    assert verified["valid"] is True, verified
    run = _ok(client.get("/v1/calc/" + column["calc_run_id"]))
    replay = _ok(client.post("/v1/calc/recalc", json={
        "basis_pack": pack, "snapshot": run["snapshot"], "stored_value": column["result"]}))
    assert replay["reproducible"] is True, replay

    # 13 审计留痕 + 任务/门禁/清单全景
    audit = _ok(client.get("/v1/audit?limit=200"))["items"]
    actions = {row["action"] for row in audit}
    assert {"JOB_CREATE", "BOQ_IMPORT", "JOB_RECORD_CLIENT"} <= actions, actions
    assert any(row.get("action") == "TASK_APPROVE" for row in audit), actions
    final = _ok(client.get("/v1/job/" + jid))
    assert len(final["documents"]) == 4
    assert len(final["tasks"]) == 3
    sequence = [r["gate_code"] for r in final["gate_records"]]
    assert sequence == ["G0", "G1", "G2", "G3a", "G3b", "G3c", "G4", "G4", "G5"], sequence
    latest = {}
    for row in final["gate_records"]:
        latest[row["gate_code"]] = row["result"]
    assert latest == {"G0": "PASS", "G1": "PASS", "G2": "PASS", "G3a": "PASS",
                      "G3b": "PASS", "G3c": "PASS", "G4": "PASS", "G5": "PASS"}
    assert len(final["calc_runs"]) == 2
