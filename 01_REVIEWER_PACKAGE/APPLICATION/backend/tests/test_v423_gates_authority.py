"""P0-04-GATES：G0–G5 门禁真实性（服务端权威状态，客户端不可伪造）。

覆盖四条硬要求：
1. 每道门都能指出它读了哪些服务端记录（authority_sources / failure_rule_catalog）；
2. 伪造请求体（context / available_documents / 无 job_id 自述）→ 仍 FAIL；
3. 真实服务端记录补齐 → PASS；
4. 缺证据绑定（绑定量/证据/指纹/卷号）→ FAIL，且补齐绑定后转 PASS。

测试同时校验 FAIL/PASS 都被写入服务端 gate_record，顺序判定只认服务端历史。
"""
from __future__ import annotations

import json
import pytest
from tests.authenticated_client import TestClient

from txbuddy.api import app, store as api_store
from txbuddy.core.gates import (
    ADVISORY_GATES,
    AUTHORITATIVE_GATES,
    GATE_FAILURE_RULES,
    GATE_SEQUENCE,
    GATE_SERVER_SOURCES,
    REQUIRED,
    check_gate,
    gate_authority,
)
from txbuddy.core.gates_service import GateError, evaluate as gate_evaluate

client = TestClient(app)


@pytest.fixture(autouse=True)
def test_only_professional_registry(tmp_path, monkeypatch):
    """Synthetic credentials support workflow tests without granting real qualifications."""
    registry = tmp_path / "professionals_TEST_ONLY.json"
    registry.write_text(json.dumps({subject: {
        "subject": subject, "identity_verified_at": "TEST_ONLY_FIXTURE",
        "license_number": f"TEST_ONLY-{subject}",
        "license_type": "REGISTERED_COST_ENGINEER", "name": f"TEST_ONLY {subject}",
    } for subject in ("anonymous", "u", "tester")}), encoding="utf-8")
    monkeypatch.setenv("TXBUDDY_PROFESSIONALS_FILE", str(registry))

PACK = {
    "basis_pack_id": "B-GATES", "region_code": "410100", "region_level": "CITY",
    "major": "BUILDING", "effective_date": "2026-01-01",
    "measurement_standard_version": "GB/T 50854-2024",
    "pricing_standard_version": "GB/T 50500-2024",
    "rule_version_scope": "HENAN_2025", "precision_rule_version": "P-V1",
    "status": "LOCKED", "test_only": False,
    "sources": [{"source_id": "S1", "title": "定额", "source_type": "QUOTA",
                 "license_status": "LICENSED", "verified": True}],
}
GATES = ["G0", "G1", "G2", "G3a", "G3b", "G3c", "G4", "G5"]
TOKEN = {"X-Internal-Token": "v424-legacy-fixture-token"}


# --------------------------------------------------------------------- helpers
def new_job(pack: dict | None = None, docs: bool = True, line: str = "L1") -> str:
    body = {"project_id": "P-GATES", "name": "门禁真实性", "business_line": line,
            "basis_pack": pack or PACK, "created_by": "tester"}
    jid = client.post("/v1/job", json=body).json()["job_id"]
    if docs:
        for d in sorted(REQUIRED[line]):
            client.post(f"/v1/job/{jid}/documents",
                        json={"doc_key": d, "version": "v1", "file_sha256": "a" * 64,
                              "verified": True})
    return jid


def add_calc(jid: str, params: dict | None = None) -> dict:
    out = client.post("/v1/quantity/rule-calc",
                      json={"job_id": jid, "basis_pack": PACK,
                            "rule_code": "QTY.BOQ.CONC.COLUMN.001",
                            "params": params or {"b": "0.5", "h": "0.5", "H": "8"}}).json()
    assert "calc_run_id" in out, out
    return out


def put_server(jid: str, kind: str, payload: dict) -> dict:
    r = client.post(f"/internal/job/{jid}/records",
                    json={"kind": kind, "payload": payload}, headers=TOKEN)
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "SERVER"
    return r.json()


def put_client(jid: str, kind: str, payload: dict) -> dict:
    r = client.post(f"/v1/job/{jid}/records", json={"kind": kind, "payload": payload})
    assert r.status_code == 200, r.text
    return r.json()


def gate(jid: str, code: str, line: str = "L1", **kw) -> dict:
    body = {"job_id": jid, "business_line": line, "gate_code": code, "basis_pack": PACK}
    body.update(kw)
    r = client.post("/v1/gate/check", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def seed_full(jid: str | None = None, params: dict | None = None,
              with_report: bool = True, with_archive: bool = True) -> tuple[str, dict]:
    """写入一套完整的服务端事实（strict 模式下可直接全绿）。"""
    jid = jid or new_job()
    put_client(jid, "boq", {"content_sha256": "x"})
    calc = add_calc(jid, params)
    put_server(jid, "reconcile", {"reconciled": True, "calc_run_ids": [calc["calc_run_id"]]})
    put_server(jid, "diff", {"unexplained": 0})
    put_server(jid, "price", {"item": "010502001001", "source_ref": "QUOTA-TEST",
                              "evidence_id": calc["evidence_id"]})
    for node, role, lic in [("SELF_REVIEW", "REVIEWER", None),
                            ("REVIEW", "REVIEWER", None),
                            ("APPROVE", "PRINCIPAL", "REGISTERED_COST_ENGINEER")]:
        identity = {"X-Actor": "u"}
        tid = client.post(f"/v1/job/{jid}/tasks", json={"node": node, "payload": {}}).json()["task_id"]
        client.post(f"/v1/task/{tid}/start", headers=identity, json={"actor": "u", "role": "EDITOR"})
        client.post(f"/v1/task/{tid}/complete", headers=identity, json={"actor": "u", "role": "EDITOR", "result": {}})
        client.post(f"/v1/task/{tid}/approve", headers=identity,
                    json={"actor": "u", "role": role, "license": lic})
    if with_report:
        put_server(jid, "report", {"generated": True, "layout_checked": True,
                                   "report_sha256": "b" * 64,
                                   "evidence_ids": [calc["evidence_id"]]})
    if with_archive:
        put_server(jid, "archive", {"volume_complete": True, "package_sha256": "c" * 64,
                                    "volume_no": "V-01", "indicators_stored": True})
    return jid, calc


def judge_all(jid: str, codes: list[str], line: str = "L1") -> None:
    for code in codes:
        out = gate(jid, code, line)
        assert out["result"] == "PASS", (code, out)


def gate_upto(jid: str, target: str, line: str = "L1") -> dict:
    """按业务线顺序判定 target 之前的门禁，要求全部 PASS。"""
    seq = GATE_SEQUENCE[line]
    judge_all(jid, seq[:seq.index(target)], line)
    return gate(jid, target, line)


@pytest.fixture()
def strict(monkeypatch):
    monkeypatch.setenv("TX_STRICT_GATES", "1")
    monkeypatch.setenv("TX_INTERNAL_TOKEN", "v424-legacy-fixture-token")
    return True


# ------------------------------------------------------- 1 权威性契约可自证
def test_every_gate_declares_server_sources_and_failure_rules():
    for code in GATES:
        assert GATE_SERVER_SOURCES.get(code), f"{code} 未声明服务端记录来源"
        rules = GATE_FAILURE_RULES.get(code) or []
        assert rules, f"{code} 缺少失败规则清单"
        for rule in rules:
            assert rule["rule_id"].startswith(code), rule
            assert rule["error_code"] and rule["condition"] and rule["source"], rule
        authority = gate_authority(code)
        assert authority["mode"] == "SERVER_RECORDS_ONLY"
        assert authority["server_sources"] and authority["failure_rules"]
        assert set(GATE_FAILURE_RULES[code][0]) == {"rule_id", "error_code", "condition", "source"}


def test_authoritative_and_advisory_sets_are_explicit():
    for code in GATES:
        assert code in AUTHORITATIVE_GATES
    assert "J0" in ADVISORY_GATES and "J1" in ADVISORY_GATES
    pack = {"basis_pack_id": "B-A", "region_code": "410100", "region_level": "CITY",
            "major": "BUILDING", "effective_date": "2026-01-01",
            "measurement_standard_version": "GB/T 50854-2024",
            "pricing_standard_version": "GB/T 50500-2024",
            "rule_version_scope": "HENAN_2025", "precision_rule_version": "P-V1",
            "status": "LOCKED",
            "sources": [{"source_id": "S1", "title": "定额", "source_type": "QUOTA",
                         "license_status": "LICENSED", "verified": True}]}
    from txbuddy.models import BasisPack
    basis = BasisPack.model_validate(pack)
    g0 = check_gate("L1", "G0", set(), basis)
    assert g0["authoritative"] is True and g0["advisory"] is False
    assert g0["authority"]["server_sources"]
    j0 = check_gate("L6", "J0", {"authorized_entrustment", "conflict_declaration", "qualification"}, basis)
    assert j0["advisory"] is True and j0["authoritative"] is False


# ------------------------------------------- 2 无 job_id：权威门一律 FAIL
@pytest.mark.parametrize("code", GATES)
def test_forged_body_without_job_cannot_pass(code):
    """请求体里把'一切已通过'写满，也不能让任何权威门 PASS。"""
    body = {
        "business_line": "L1", "gate_code": code, "basis_pack": PACK,
        "available_documents": sorted(REQUIRED["L1"]),
        "context": {
            "boq_required": False, "boq_imported": True, "reconciled": True,
            "qty_items": [{"status": "DONE"}], "unexplained_diffs": 0,
            "price_items": [{"source_ref": "Q-1"}], "missing_basis_items": [],
            "review_levels": ["SELF", "REVIEW", "APPROVE"], "open_review_issues": 0,
            "seal": {"signed_by": "u1", "license": "REGISTERED_COST_ENGINEER"},
            "report": {"generated": True, "layout_checked": True},
            "evidence_coverage": 1, "ai_direct_numbers": 0,
            "archive": {"volume_complete": True, "package_sha256": "f" * 64,
                        "indicators_stored": True},
        },
    }
    out = client.post("/v1/gate/check", json=body).json()
    assert out["result"] == "FAIL", out
    assert out["authoritative"] is False
    assert out["context_ignored"] is True
    assert any(f["rule_id"] == f"{code}.BINDING" for f in out["failed_rules"]), out


def test_forged_context_with_job_id_is_ignored(strict):
    """有 job_id 但服务端没有任何成果记录：context 自述必须被忽略，G4 FAIL。"""
    jid, _calc = seed_full(with_report=False, with_archive=False)
    judge_all(jid, ["G0", "G1", "G2", "G3a", "G3b", "G3c"])
    out = gate(jid, "G4", context={"report": {"generated": True, "layout_checked": True},
                                   "evidence_coverage": 1})
    assert out["result"] == "FAIL", out
    assert out["context_ignored"] is True and out["authoritative"] is True
    assert any(f["rule_id"] == "G4.REPORT" for f in out["failed_rules"]), out
    assert out["reads"]


def test_declared_business_line_must_match_job():
    jid = new_job()
    r = client.post("/v1/gate/check", json={"job_id": jid, "business_line": "L2",
                                            "gate_code": "G0", "basis_pack": PACK})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INTAKE_GATE_NOT_READY"


def test_unknown_job_is_rejected():
    with pytest.raises(GateError) as exc:
        gate_evaluate(api_store, "job-not-exist", "G0")
    assert exc.value.code == "INTAKE_GATE_NOT_READY"


def test_g0_reads_basis_pack_from_job_not_request():
    """任务里的依据包是 DRAFT：请求体塞 LOCKED 依据包也不得 PASS。"""
    draft = dict(PACK, status="DRAFT", test_only=True)
    jid = new_job(pack=draft)
    out = gate(jid, "G0")  # 请求体 PACK 是 LOCKED
    assert out["result"] == "FAIL", out
    assert any(f["rule_id"] == "G0.BASIS" for f in out["failed_rules"]), out
    assert "job.basis_pack" in out["reads"]


def test_g1_ignores_client_declared_documents():
    jid = new_job(docs=False)
    judge_all(jid, ["G0"])
    out = gate(jid, "G1", available_documents=sorted(REQUIRED["L1"]))
    assert out["result"] == "FAIL", out
    assert any(f["rule_id"] == "G1.DOCS" for f in out["failed_rules"]), out
    assert out["missing_documents"]
    assert "job_document" in out["reads"]


def test_g1_rejects_unverified_documents():
    jid = new_job(docs=False)
    for d in sorted(REQUIRED["L1"]):
        client.post(f"/v1/job/{jid}/documents",
                    json={"doc_key": d, "version": "v1", "file_sha256": "a" * 64,
                          "verified": False})
    put_client(jid, "boq", {"content_sha256": "x"})
    judge_all(jid, ["G0"])
    out = gate(jid, "G1")
    assert out["result"] == "FAIL", out
    assert any(f["rule_id"] == "G1.UNVERIFIED" for f in out["failed_rules"]), out


# ------------------------------------- 3 真实服务端记录补齐 → G0–G5 全 PASS
def test_full_server_records_pass_every_gate(strict):
    jid, calc = seed_full()
    counts = {}
    for code in GATES:
        out = gate(jid, code)
        assert out["result"] == "PASS", (code, out)
        assert out["authoritative"] is True and out["context_ignored"] is True
        assert out["authority_sources"], code
        assert out["failure_rule_catalog"], code
        for src in out["authority_sources"]:
            assert src["authoritative"] is True and isinstance(src["count"], int)
        counts[code] = {s["table"]: s["count"] for s in out["authority_sources"]}
    # 每道门都能指出读了哪些服务端记录，并且计数与真实数据一致
    assert counts["G0"]["job.basis_pack"] == 1
    assert counts["G1"]["job_document"] == len(REQUIRED["L1"])
    assert counts["G1"]["job_record:boq"] == 1
    assert counts["G2"]["calc_run"] == 1
    assert counts["G2"]["job_record:reconcile"] == 1
    assert counts["G3a"]["job_record:price"] == 1
    assert counts["G3b"]["job_task"] == 3
    assert counts["G3c"]["audit_log"] >= 3
    assert counts["G4"]["job_record:report"] == 1
    assert counts["G5"]["job_record:archive"] == 1
    # 判定结果本身也落在服务端 gate_record 上
    saved = {r["gate_code"]: r["result"] for r in api_store.list_gate_records(jid)}
    for code in GATES:
        assert saved[code] == "PASS", (code, saved)


def test_sequence_is_decided_by_server_gate_records(strict):
    jid, _calc = seed_full()
    early = gate(jid, "G2")
    assert early["result"] == "FAIL"
    assert any(f["rule_id"] == "G2.SEQUENCE" for f in early["failed_rules"]), early
    assert "gate_record" in early["reads"]
    assert gate(jid, "G0")["result"] == "PASS"
    assert gate(jid, "G1")["result"] == "PASS"
    assert gate(jid, "G2")["result"] == "PASS"


# ------------------------------------------- 4 缺证据绑定 → FAIL，补齐 → PASS
def test_g2_reconcile_binding_missing_fails(strict):
    jid, _calc = seed_full()
    gate_upto(jid, "G2")
    # 模拟历史/旁路写入的服务端勾稽记录：声明 reconciled 但没有绑定任何 calc_run
    api_store.put_record(jid, "reconcile", {"reconciled": True, "_source": "SERVER"})
    out = gate(jid, "G2")
    assert out["result"] == "FAIL", out
    assert any(f["rule_id"] == "G2.BINDING" for f in out["failed_rules"]), out
    # 补齐绑定后同一门禁转 PASS
    calc = add_calc(jid)
    put_server(jid, "reconcile", {"reconciled": True, "calc_run_ids": [calc["calc_run_id"]]})
    assert gate(jid, "G2")["result"] == "PASS"


def test_g2_reconcile_binding_foreign_calc_run_fails(strict):
    # 参数不同才会生成不同的 evidence_id / calc_run（证据 ID 是内容哈希）
    jid_a, calc_a = seed_full(params={"b": "0.4", "h": "0.4", "H": "7"})
    jid_b, calc_b = seed_full()
    assert calc_a["calc_run_id"] != calc_b["calc_run_id"]
    gate_upto(jid_b, "G2")
    api_store.put_record(jid_b, "reconcile",
                         {"reconciled": True, "calc_run_ids": [calc_a["calc_run_id"]],
                          "_source": "SERVER"})
    out = gate(jid_b, "G2")
    assert out["result"] == "FAIL", out
    assert any(f["rule_id"] == "G2.BINDING" for f in out["failed_rules"]), out
    assert any("foreign-job" in f["message"] for f in out["failed_rules"]), out


def test_g3a_price_evidence_binding_missing_fails(strict):
    jid, _calc = seed_full()
    gate_upto(jid, "G3a")
    api_store.put_record(jid, "price", {"source_ref": "Q-1", "_source": "SERVER"})
    out = gate(jid, "G3a")
    assert out["result"] == "FAIL", out
    assert any(f["rule_id"] == "G3a.BINDING" for f in out["failed_rules"]), out
    put_server(jid, "price", {"source_ref": "Q-1", "evidence_id": "ev-1"})
    assert gate(jid, "G3a")["result"] == "PASS"


def test_g4_report_binding_missing_fails_then_passes(strict):
    jid, calc = seed_full()
    gate_upto(jid, "G4")
    api_store.put_record(jid, "report",
                         {"generated": True, "layout_checked": True, "_source": "SERVER"})
    out = gate(jid, "G4")
    assert out["result"] == "FAIL", out
    assert any(f["rule_id"] == "G4.BINDING" for f in out["failed_rules"]), out
    put_server(jid, "report", {"generated": True, "layout_checked": True,
                               "report_sha256": "d" * 64,
                               "evidence_ids": [calc["evidence_id"]]})
    assert gate(jid, "G4")["result"] == "PASS"


def test_g5_archive_binding_missing_fails(strict):
    jid, _calc = seed_full()
    gate_upto(jid, "G5")
    api_store.put_record(jid, "archive",
                         {"volume_complete": True, "package_sha256": "e" * 64,
                          "indicators_stored": True, "_source": "SERVER"})
    out = gate(jid, "G5")
    assert out["result"] == "FAIL", out
    assert any(f["rule_id"] == "G5.BINDING" for f in out["failed_rules"]), out
    put_server(jid, "archive", {"volume_complete": True, "package_sha256": "e" * 64,
                                "volume_no": "V-02", "indicators_stored": True})
    assert gate(jid, "G5")["result"] == "PASS"


def test_client_records_never_authoritative_in_strict_mode(strict):
    jid = new_job()
    put_client(jid, "boq", {"content_sha256": "x"})
    calc = add_calc(jid)
    put_client(jid, "reconcile", {"reconciled": True, "calc_run_ids": [calc["calc_run_id"]]})
    put_client(jid, "diff", {"unexplained": 0})
    gate(jid, "G0")
    gate(jid, "G1")
    out = gate(jid, "G2")
    assert out["result"] == "FAIL", out
    assert any(f["rule_id"] == "G2.RECONCILE" for f in out["failed_rules"]), out
    assert out["server_mode"] == "STRICT" and out["binding_required"] is True


def test_compat_mode_reports_binding_warnings_instead_of_silence(monkeypatch):
    """非 strict 兼容模式容忍历史未绑定记录，但必须显式暴露 warning，不能静默放行。"""
    monkeypatch.delenv("TX_STRICT_GATES", raising=False)
    jid = new_job()
    put_client(jid, "boq", {"content_sha256": "x"})
    calc = add_calc(jid)
    put_client(jid, "reconcile", {"reconciled": True})
    put_client(jid, "diff", {"unexplained": 0})
    put_client(jid, "price", {"source_ref": "Q-1"})
    for node, role, lic in [("SELF_REVIEW", "REVIEWER", None),
                            ("REVIEW", "REVIEWER", None),
                            ("APPROVE", "PRINCIPAL", "REGISTERED_COST_ENGINEER")]:
        identity = {"X-Actor": "u"}
        tid = client.post(f"/v1/job/{jid}/tasks", json={"node": node, "payload": {}}).json()["task_id"]
        client.post(f"/v1/task/{tid}/start", headers=identity, json={"actor": "u", "role": "EDITOR"})
        client.post(f"/v1/task/{tid}/complete", headers=identity, json={"actor": "u", "role": "EDITOR", "result": {}})
        client.post(f"/v1/task/{tid}/approve", headers=identity, json={"actor": "u", "role": role, "license": lic})
    put_client(jid, "report", {"generated": True, "layout_checked": True})
    gate_upto(jid, "G4")
    out = gate(jid, "G4")
    assert out["result"] == "PASS", out
    assert out["server_mode"] == "COMPAT" and out["binding_required"] is False
    assert any(w["rule_id"] == "G4.BINDING" for w in out["binding_warnings"]), out
    assert calc["calc_run_id"]


def test_failed_gate_is_persisted_before_fix(strict):
    """FAIL 也落服务端 gate_record；修好后新增 PASS 记录，历史不可抹除。"""
    jid = new_job()
    gate(jid, "G0")
    first = gate(jid, "G1")
    assert first["result"] == "FAIL"
    for d in sorted(REQUIRED["L1"]):
        client.post(f"/v1/job/{jid}/documents",
                    json={"doc_key": d, "version": "v1", "file_sha256": "a" * 64,
                          "verified": True})
    put_client(jid, "boq", {"content_sha256": "x"})
    assert gate(jid, "G1")["result"] == "PASS"
    rows = [r for r in api_store.list_gate_records(jid) if r["gate_code"] == "G1"]
    assert [r["result"] for r in rows] == ["FAIL", "PASS"]


# ------------------------------------- 5 写入守卫与门禁守卫共用同一套绑定规则
def test_missing_bindings_helper_is_shared_by_writer_and_gate():
    from txbuddy.core.record_writer import BINDING_RULES, missing_bindings

    jid = new_job()
    assert set(BINDING_RULES) == {"reconcile", "price", "report", "archive"}
    assert set(missing_bindings(api_store, "report", {}, jid)) == {"report_sha256", "evidence_ids"}
    assert missing_bindings(api_store, "price", {"source_ref": "Q-1"}, jid) == ["evidence_id"]
    assert missing_bindings(api_store, "archive", {"package_sha256": "f" * 64}, jid) == ["volume_no"]
    unbound_run = missing_bindings(api_store, "reconcile", {"calc_run_ids": ["nope"]}, jid)
    assert unbound_run and unbound_run[0].startswith("calc_run_ids:")
    ok_run = missing_bindings(api_store, "reconcile", {"calc_run_ids": [add_calc(jid)["calc_run_id"]]}, jid)
    assert ok_run == []


def test_server_write_guard_rejects_missing_binding_before_gate():
    jid = new_job()
    r = client.post(f"/internal/job/{jid}/records",
                    json={"kind": "archive", "payload": {"volume_complete": True}},
                    headers=TOKEN)
    assert r.status_code == 422 and "package_sha256" in r.text
    r2 = client.post(f"/internal/job/{jid}/records",
                     json={"kind": "price", "payload": {"source_ref": "Q-1"}},
                     headers=TOKEN)
    assert r2.status_code == 422 and "evidence_id" in r2.text
