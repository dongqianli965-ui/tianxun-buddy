"""P0 缺陷复现测试：先红后绿。修复前应全部失败（xfail 不适用，直接断言正确行为）。"""
import io
import math
from datetime import date

import pytest
from tests.authenticated_client import TestClient
from openpyxl import Workbook

from txbuddy.api import app
from txbuddy.models import BasisPack

client = TestClient(app)


def locked_pack(**kw):
    base = dict(basis_pack_id="B-LOCKED", region_code="410100", region_level="CITY", major="BUILDING",
                effective_date="2026-01-01", measurement_standard_version="GB/T 50854-2024",
                pricing_standard_version="GB/T 50500-2024", rule_version_scope="HENAN_2025",
                precision_rule_version="P-V1", status="LOCKED", test_only=False,
                sources=[{"source_id": "S1", "title": "定额", "source_type": "QUOTA",
                          "license_status": "LICENSED", "verified": True}])
    base.update(kw)
    return base


def test_p0_01_test_only_rule_cannot_get_formal_admission():
    """TEST_ONLY 规则即便配置 LOCKED 依据包，也不得返回 formal_ready=true。"""
    r = client.post("/v1/quantity/rule-calc", json={
        "basis_pack": locked_pack(), "rule_code": "QTY.BOQ.MASONRY.WALL.001",
        "params": {"L": "6", "H": "3", "T": "0.24", "opening_area": "2.1"}})
    assert r.status_code == 200
    body = r.json()
    assert body["test_only"] is True
    assert body["formal_ready"] is False, "TEST_ONLY 规则不得获得正式准入"
    assert body.get("admission_reasons"), "必须给出拒绝原因"


def test_p0_02_idem_same_key_different_payload_conflict():
    """同键不同参数必须冲突，不得返回旧结果。"""
    pack = locked_pack()
    a = {"basis_pack": pack, "rule_code": "QTY.BOQ.CONC.COLUMN.001", "params": {"b": "0.5", "h": "0.5", "H": "3"}}
    b = {"basis_pack": pack, "rule_code": "QTY.BOQ.CONC.COLUMN.001", "params": {"b": "1", "h": "1", "H": "3"}}
    r1 = client.post("/v1/quantity/rule-calc", json=a, headers={"X-Idem-Key": "same-key"})
    assert r1.status_code == 200 and r1.json()["result"] == "0.750"
    r2 = client.post("/v1/quantity/rule-calc", json=b, headers={"X-Idem-Key": "same-key"})
    assert r2.status_code == 409, f"不同参数同键应 409，实际 {r2.status_code} {r2.text[:200]}"
    assert "IDEM" in r2.text.upper()


def _boq_bytes(rows):
    wb = Workbook(); ws = wb.active
    ws.append(["项目编码", "项目名称", "项目特征", "计量单位", "工程量", "分部"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def test_p0_03_boq_hash_covers_all_business_fields():
    """项目名称/特征/分部任一变化，content_sha256 必须变化。"""
    base = [["A1", "现浇混凝土柱", "C30", "m3", 1, "混凝土工程"]]
    variants = [
        [["A1", "现浇混凝土柱(改名)", "C30", "m3", 1, "混凝土工程"]],
        [["A1", "现浇混凝土柱", "C35", "m3", 1, "混凝土工程"]],
        [["A1", "现浇混凝土柱", "C30", "m3", 1, "另一分部"]],
    ]
    h0 = client.post("/v1/boq/import", files={"file": ("a.xlsx", _boq_bytes(base), "x")}).json()["content_sha256"]
    for v in variants:
        h = client.post("/v1/boq/import", files={"file": ("a.xlsx", _boq_bytes(v), "x")}).json()["content_sha256"]
        assert h != h0, f"业务字段变化后哈希未变：{v}"


def test_p0_04_non_finite_rejected():
    """NaN / Infinity 必须被拒绝，且返回业务错误码而不是 500。"""
    for bad in ["NaN", "Infinity", "-Infinity"]:
        r = client.post("/v1/quantity/calc", json={
            "basis_pack": locked_pack(), "operation": "RECTANGULAR_VOLUME",
            "params": {"length": bad, "width": "1", "height": "1"}, "unit": "m3",
            "rule_code": "R", "rule_version": "1", "standard_ref": "TEST"})
        assert r.status_code == 422, f"{bad} 应被拒绝"
        assert "CALC" in r.text.upper() or "非法" in r.text or "NaN" in r.text or "finite" in r.text.lower()


def test_p0_05_gate_cannot_be_forged_by_context():
    """调用方自报'成果已生成/证据覆盖率100%'不得让 G4 通过。"""
    body = {"business_line": "L1", "gate_code": "G4", "basis_pack": locked_pack(),
            "available_documents": [], "context": {"report": {"generated": True, "layout_checked": True},
                                                   "evidence_coverage": 1}}
    r = client.post("/v1/gate/check", json=body)
    assert r.status_code in (200, 422)
    data = r.json()
    payload = data.get("detail", data)
    assert payload.get("result") != "PASS", "伪造 context 不得 PASS"
