"""V4.2.1 反向测试：8 类问题 + BOQ 哈希补充 + 精度 + SQL 静态检查。"""
import io, os, re, sqlite3, threading
from datetime import date
from decimal import Decimal

import pytest
from tests.authenticated_client import TestClient
from openpyxl import Workbook

from txbuddy.api import app, store
from txbuddy.core.admission import formal_admission
from txbuddy.core.precision import compare_sum, round_money, round_quantity, decimals_for
from txbuddy.models import BasisPack

client = TestClient(app)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL_DIR = os.path.join(HERE, "..", "sql")


def basis(**kw):
    b = dict(basis_pack_id="B-OK", region_code="410100", region_level="CITY", major="BUILDING",
             effective_date="2026-01-01", measurement_standard_version="GB/T 50854-2024 [已核]",
             pricing_standard_version="GB/T 50500-2024 [已核]", rule_version_scope="HENAN_2025",
             precision_rule_version="P-V1", status="LOCKED", test_only=False,
             sources=[{"source_id": "S1", "title": "河南定额", "source_type": "QUOTA",
                       "license_status": "LICENSED", "verified": True}])
    b.update(kw)
    return BasisPack(**b)


def rule(**kw):
    r = dict(rule_code="R1", version="1", unit="m3", expression="1", variables={},
             standard_ref="GB/T 50854-2024 §5.1 [已核]", license_status="LICENSED",
             source_verified=True, status="ACTIVE", region_code="410100", major="BUILDING",
             source_ref="S1")
    r.update(kw)
    return r


# ---------------- 问题 1：准入来源绑定 ----------------
@pytest.mark.parametrize("mutate,needle", [
    ({"source_ref": None}, "source_ref"),
    ({"source_ref": "S-NOT-IN-PACK"}, "不属于当前依据包"),
    ({"region_code": None}, "region_code"),
    ({"major": None}, "major"),
    ({"standard_ref": "GB/T 50854-2024 待核"}, "standard_ref"),
    ({"license_status": "TEST_ONLY"}, "授权状态"),
    ({"source_verified": False}, "来源未经核验"),
])
def test_v421_admission_rejects_missing_bindings(mutate, needle):
    out = formal_admission(rule(**mutate), basis())
    assert out["formal_ready"] is False
    assert any(needle in r["detail"] for r in out["admission_reasons"]), out


def test_v421_admission_date_mismatch_and_valid_pack():
    out = formal_admission(rule(eff_from="2027-01-01"), basis())
    assert out["formal_ready"] is False and any("生效日" in r["detail"] for r in out["admission_reasons"])
    ok = formal_admission(rule(), basis())
    assert ok["formal_ready"] is True and ok["admission_reasons"] == []


def test_v421_admission_source_unverified_or_pending():
    b = basis(sources=[{"source_id": "S1", "title": "x", "source_type": "QUOTA",
                        "license_status": "PENDING", "verified": False}])
    out = formal_admission(rule(), b)
    assert out["formal_ready"] is False


# ---------------- 问题 2：Price 入口统一准入 ----------------
def test_v421_project_total_rejects_unlocked_basis_for_formal():
    r = client.post("/v1/price/project-total", json={
        "basis_pack": basis(status="DRAFT", test_only=True).model_dump(mode="json"),
        "boq_total": "100", "measure_total": "10", "other_total": "0",
        "fee_items": [], "safety_production_fee": None, "tax_rate": "0.09",
        "formal_calculation": True})
    assert r.status_code == 422 and "BASIS_PACK_MISSING" in r.text


def test_v421_project_total_allows_test_calc_when_not_formal():
    r = client.post("/v1/price/project-total", json={
        "basis_pack": basis(status="DRAFT", test_only=True).model_dump(mode="json"),
        "boq_total": "100", "measure_total": "10", "other_total": "0",
        "fee_items": [], "safety_production_fee": None, "tax_rate": "0.09",
        "formal_calculation": False})
    assert r.status_code == 200


# ---------------- 问题 3：幂等并发 50 ----------------
def _calc_body(k=1.0, **kw):
    b = basis().model_dump(mode="json"); b.update(kw)
    return {"basis_pack": b, "rule_code": "QTY.BOQ.CONC.COLUMN.001",
            "params": {"b": str(k), "h": "0.5", "H": "3"}}


def test_v421_idem_50_same_request_single_execution():
    before = store.evidence_count()
    results, errors = [], []
    def worker():
        try:
            r = client.post("/v1/quantity/rule-calc", json=_calc_body(), headers={"X-Idem-Key": "vk-1"})
            results.append((r.status_code, r.json().get("result")))
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
    ts = [threading.Thread(target=worker) for _ in range(50)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errors
    oks = [r for r in results if r[0] == 200]
    assert oks, results[:5]
    assert len({r[1] for r in oks}) == 1, "同键同参应返回同一结果"
    assert store.evidence_count() - before <= 1, "重复请求不得重复生成证据"


def test_v421_idem_conflict_under_concurrency():
    codes = []
    def worker(i):
        r = client.post("/v1/quantity/rule-calc", json=_calc_body(1.0 + i / 100),
                        headers={"X-Idem-Key": "vk-conflict"})
        codes.append(r.status_code)
    ts = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert 409 in codes, f"应出现幂等冲突，实际 {set(codes)}"
    assert codes.count(200) <= 1, "只允许一个请求成功"


def test_v421_idem_isolated_by_actor():
    a = client.post("/v1/quantity/rule-calc", json=_calc_body(), headers={"X-Idem-Key": "shared", "X-Actor": "u1"})
    b = client.post("/v1/quantity/rule-calc", json=_calc_body(2.0), headers={"X-Idem-Key": "shared", "X-Actor": "u2"})
    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["result"] != b.json()["result"]


# ---------------- 问题 4：全引擎非法数值 ----------------
@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_v421_numeric_rejected_across_engines(bad):
    r1 = client.post("/v1/quantity/calc", json={
        "basis_pack": basis().model_dump(mode="json"), "operation": "RECTANGULAR_VOLUME",
        "params": {"length": bad, "width": "1", "height": "1"}, "unit": "m3",
        "rule_code": "R", "rule_version": "1", "standard_ref": "T"})
    assert r1.status_code == 422 and r1.status_code != 500
    r2 = client.post("/v1/price/unit", json={
        "resources": [{"resource_code": "M1", "resource_type": "MATERIAL",
                       "consumption": bad, "unit_price": "10", "price_ref": "T"}],
        "management_rate": "0.1", "profit_rate": "0.05",
        "management_source_ref": "T", "profit_source_ref": "T"})
    assert r2.status_code == 422
    r3 = client.post("/v1/quantity/rule-calc", json={
        "basis_pack": basis().model_dump(mode="json"),
        "rule_code": "QTY.BOQ.CONC.COLUMN.001", "params": {"b": bad, "h": "1", "H": "1"}})
    assert r3.status_code == 422


# ---------------- 问题 7：精度 Golden ----------------
def test_v421_precision_rules_and_tolerance():
    assert decimals_for("m3") == 3 and decimals_for("m2") == 2
    assert str(round_quantity(Decimal("1") / Decimal("3"), "m3")) == "0.333"
    assert str(round_money(Decimal("10") / Decimal("3"))) == "3.33"
    assert compare_sum(Decimal("100.00"), Decimal("100.01"))["consistent"] is True
    assert compare_sum(Decimal("100.00"), Decimal("100.02"))["consistent"] is False


# ---------------- 问题 6：SQL 结构与迁移静态检查 ----------------
def _tables(sql_text):
    return re.findall(r"CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)\s*\(([^;]+?)\);", sql_text, re.S | re.I)


def test_v421_sql_no_conflicting_definitions_or_dangling_index():
    sql1 = io.open(os.path.join(SQL_DIR, "001_core_schema_postgresql.sql"), encoding="utf-8").read()
    sql2 = io.open(os.path.join(SQL_DIR, "002_workflow_and_idem_postgresql.sql"), encoding="utf-8").read()
    defs = {}
    for name, body in _tables(sql1) + _tables(sql2):
        defs.setdefault(name.lower(), []).append(body)
    dup = {k: v for k, v in defs.items() if len(v) > 1}
    assert not dup, f"重复定义表：{list(dup)}"
    cols = {}
    for name, body in _tables(sql1) + _tables(sql2):
        names = set()
        for piece in body.split(","):
            head = piece.strip().split()
            if len(head) >= 2 and re.match(r"^[A-Za-z_]\w*$", head[0]):
                if head[0].upper() not in {"PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT"}:
                    names.add(head[0].lower())
        cols[name.lower()] = names
    all_text = sql1 + sql2
    for m in re.finditer(r"CREATE (?:UNIQUE )?INDEX(?: IF NOT EXISTS)?\s+\w+\s+ON\s+(\w+)\s*\(([^)]+)\)", all_text, re.I):
        table, raw = m.group(1).lower(), m.group(2)
        for col in [c.strip().split()[0].lower() for c in raw.split(",")]:
            assert table in cols, f"索引指向不存在的表 {table}"
            assert col in cols[table] or col.endswith("_id"), f"索引列 {col} 不在 {table}"


def test_v421_calc_run_has_snapshot_sha256_in_sql():
    t1 = io.open(os.path.join(SQL_DIR, "001_core_schema_postgresql.sql"), encoding="utf-8").read()
    t2 = io.open(os.path.join(SQL_DIR, "002_workflow_and_idem_postgresql.sql"), encoding="utf-8").read()
    assert "snapshot_sha256" in (t1 + t2)


# ---------------- BOQ 哈希补充 ----------------
def _boq(rows, headers=None):
    wb = Workbook(); ws = wb.active
    ws.append(headers or ["项目编码", "项目名称", "项目特征", "计量单位", "工程量", "分部"])
    for r in rows: ws.append(r)
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


BASE = [["A1", "柱", "C30", "m3", 1, "混凝土"]]
@pytest.mark.parametrize("rows,headers", [
    ([["A2", "柱", "C30", "m3", 1, "混凝土"]], None),          # 编码
    ([["A1", "柱改", "C30", "m3", 1, "混凝土"]], None),        # 名称
    ([["A1", "柱", "C35", "m3", 1, "混凝土"]], None),          # 特征
    ([["A1", "柱", "C30", "m2", 1, "混凝土"]], None),          # 单位
    ([["A1", "柱", "C30", "m3", 2, "混凝土"]], None),          # 数量
    ([["A1", "柱", "C30", "m3", 1, "砌筑"]], None),            # 分部
    (BASE, ["项目编码", "项目名称", "项目特征", "计量单位", "工程量", "章节"]),  # 表头映射
])
def test_v421_boq_hash_covers_all_fields(rows, headers):
    h0 = client.post("/v1/boq/import", files={"file": ("a.xlsx", _boq(BASE), "x")}).json()["content_sha256"]
    h1 = client.post("/v1/boq/import", files={"file": ("a.xlsx", _boq(rows, headers), "x")}).json()["content_sha256"]
    assert h1 != h0


def test_v421_boq_hash_zero_vs_empty_and_row_order():
    zero = client.post("/v1/boq/import", files={"file": ("a.xlsx", _boq([["A1", "柱", "C30", "m3", 0, ""]]), "x")}).json()
    empty = client.post("/v1/boq/import", files={"file": ("a.xlsx", _boq([["A1", "柱", "C30", "m3", "", ""]]), "x")}).json()
    assert empty["stats"]["invalid"] == 1 and zero["content_sha256"] is not None
    two = [["A1", "柱", "C30", "m3", 1, ""], ["A2", "梁", "C30", "m3", 2, ""]]
    h_a = client.post("/v1/boq/import", files={"file": ("a.xlsx", _boq(two), "x")}).json()["content_sha256"]
    h_b = client.post("/v1/boq/import", files={"file": ("a.xlsx", _boq(list(reversed(two))), "x")}).json()["content_sha256"]
    assert h_a != h_b, "行顺序影响业务哈希（当前规范：行号计入哈希）"
