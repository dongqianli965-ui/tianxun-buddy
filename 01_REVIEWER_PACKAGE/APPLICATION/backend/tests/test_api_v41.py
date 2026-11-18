import io
from datetime import date
from tests.authenticated_client import TestClient
from openpyxl import Workbook
from txbuddy.api import app

client = TestClient(app)

def pack(**kw):
    base = dict(basis_pack_id="B-API", region_code="410100", region_level="CITY", major="BUILDING",
                effective_date="2026-01-01", measurement_standard_version="GB/T 50854-2024",
                pricing_standard_version="GB/T 50500-2024", rule_version_scope="HENAN_2025",
                precision_rule_version="P-TEST", status="DRAFT", test_only=True)
    base.update(kw)
    return base

def test_health_and_rules():
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["rules_loaded"] == 6
    assert r.json()["version"] == "4.2.6"
    assert r.json()["production_accepted"] is False
    assert r.json()["accuracy_E"] == "BLOCKED"
    v = client.post("/v1/rules/validate").json()
    assert v["ok"] is True


def test_health_reports_actual_runtime_environment(monkeypatch):
    monkeypatch.setenv("TXBUDDY_ENV", "TEST_ONLY")
    assert client.get("/health").json()["test_only"] is True
    monkeypatch.delenv("TXBUDDY_ENV")
    result = client.get("/health").json()
    assert result["environment"] == "UNCONFIRMED"
    assert result["test_only"] is False

def test_rule_calc_and_idempotency():
    body = {"basis_pack": pack(), "rule_code": "QTY.BOQ.MASONRY.WALL.001", "params":
            {"L": "6", "H": "3", "T": "0.24", "opening_area": "2.1"}}
    r1 = client.post("/v1/quantity/rule-calc", json=body, headers={"X-Idem-Key": "k1"})
    assert r1.status_code == 200
    data = r1.json()
    assert data["result"] == "3.816" and data["unit"] == "m3"
    assert data["evidence_id"] and data["snapshot_sha256"]
    assert data["formal_ready"] is False
    r2 = client.post("/v1/quantity/rule-calc", json=body, headers={"X-Idem-Key": "k1"})
    assert r2.json().get("idem_replay") is True

def test_rule_calc_unknown_rule_returns_code():
    r = client.post("/v1/quantity/rule-calc", json={"basis_pack": pack(), "rule_code": "NOPE", "params": {}})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "CALC_RULE_NOT_FOUND"

def test_recalc_endpoint_roundtrip():
    body = {"basis_pack": pack(), "rule_code": "QTY.BOQ.CONC.COLUMN.001", "params": {"b": "0.5", "h": "0.5", "H": "3"}}
    calc = client.post("/v1/quantity/rule-calc", json=body).json()
    run = client.get(f"/v1/calc/{calc['calc_run_id']}").json()
    rec = client.post("/v1/calc/recalc", json={"basis_pack": pack(), "snapshot": run["snapshot"],
                                              "stored_value": calc["result"]}).json()
    assert rec["reproducible"] is True

def test_gate_endpoint_and_trace_header():
    body = {"business_line": "L1", "gate_code": "G2", "basis_pack": pack(),
            "available_documents": [], "context": {"qty_items": [], "reconciled": False}}
    r = client.post("/v1/gate/check", json=body, headers={"X-Trace-Id": "trace-test-1"})
    assert r.headers.get("X-Trace-Id") == "trace-test-1"
    data = r.json()
    assert data["result"] == "FAIL"
    assert any(f["error_code"] == "GATE_QTY_NOT_READY" for f in data["failed_rules"])

def test_boq_upload_endpoint():
    wb = Workbook(); ws = wb.active
    ws.append(["项目编码", "项目名称", "项目特征", "计量单位", "工程量"])
    ws.append(["A1", "现浇混凝土柱", "C30", "m³", 2])
    buf = io.BytesIO(); wb.save(buf)
    r = client.post("/v1/boq/import", files={"file": ("boq.xlsx", buf.getvalue(),
                 "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200
    data = r.json()
    assert data["stats"]["valid"] == 1 and data["items"][0]["unit"] == "m3"

def test_audit_recorded():
    items = client.get("/v1/audit?limit=20").json()["items"]
    assert isinstance(items, list) and len(items) >= 1
