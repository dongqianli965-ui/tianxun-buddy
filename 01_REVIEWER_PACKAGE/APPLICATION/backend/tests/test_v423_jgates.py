"""P0-05-JGATE：鉴定线 J0–J5 六门影子运行 + 人工签署才可出件。

覆盖三条硬要求：
1. J0–J5 六道鉴定门全部实现、可从 API 查询结果、且不可绕过
   （无 job_id 的自述一律拒绝；鉴定门只能套在 L6；顺序不可跳）。
2. 鉴定线正式成果默认不可签发：六门全通过后仍然只是 SHADOW，
   必须显式人工签署（自然人 + 执业资格 + 签署声明 + 门禁快照）才可出件。
3. 鉴定口径与编制口径不得混用：L6 不得写 report，通用 /records 接口不得伪造
   j_opinion / j_signoff / j_deliverable，客户端不得自述鉴定事实。
"""
from __future__ import annotations

import pytest
import json
from tests.authenticated_client import TestClient

from txbuddy.api import app, store as api_store
from txbuddy.core.gates import J_GATES, J_LINE, j_shadow_policy

client = TestClient(app)


@pytest.fixture(autouse=True)
def test_only_professional_registry(tmp_path, monkeypatch):
    """Use synthetic, test-only credentials; never grant a real qualification."""
    registry = tmp_path / "professionals_TEST_ONLY.json"
    registry.write_text(json.dumps({
        subject: {
            "subject": subject, "identity_verified_at": "TEST_ONLY_FIXTURE",
            "license_number": f"TEST_ONLY-{subject}", "license_type": "APPRAISER",
            "name": f"TEST_ONLY {subject}",
        } for subject in ("zhao-gong", "tester", "system")
    }), encoding="utf-8")
    monkeypatch.setenv("TXBUDDY_PROFESSIONALS_FILE", str(registry))

PACK = {
    "basis_pack_id": "B-JGATE", "region_code": "410100", "region_level": "CITY",
    "major": "BUILDING", "effective_date": "2026-01-01",
    "measurement_standard_version": "GB/T 50854-2024",
    "pricing_standard_version": "GB/T 50500-2024",
    "rule_version_scope": "HENAN_2025", "precision_rule_version": "P-V1",
    "status": "LOCKED", "test_only": False,
    "sources": [{"source_id": "S1", "title": "河南定额", "source_type": "QUOTA",
                 "license_status": "LICENSED", "verified": True}],
}
TOKEN = {"X-Internal-Token": "v424-legacy-fixture-token"}

J_DOCS = {
    "J0": ["authorized_entrustment", "conflict_declaration", "qualification"],
    "J1": ["material_ledger", "cross_examination_record"],
    "J2": ["survey_notice", "survey_record", "photo_index"],
    "J3": ["dispute_decomposition", "method_selection"],
    "J4": ["calc_book", "review_record"],
    "J5": ["opinion_draft", "citation_list"],
}
ALL_J_DOCS = [doc for docs in J_DOCS.values() for doc in docs]

DEFAULT_FACTS = {
    "conflict": {"exists": False},
    "material": {"unverified_used": False},
    "dispute": {"undisputed_mixed": False},
    "calc_support": {"unsupported": False},
    "opinion": {"form": "QUALIFIED", "evidence_level": "MEDIUM",
                "illegal_legal_judgement": False},
}


# --------------------------------------------------------------------- helpers
def new_job(line: str = J_LINE, docs: list[str] | None = None) -> str:
    body = {"project_id": "P-JGATE", "name": "鉴定线影子运行", "business_line": line,
            "basis_pack": PACK, "created_by": "tester"}
    r = client.post("/v1/job", json=body)
    assert r.status_code == 200, r.text
    jid = r.json()["job_id"]
    for doc in (ALL_J_DOCS if docs is None else docs):
        add_doc(jid, doc)
    return jid


def add_doc(jid: str, doc_key: str, verified: bool = True) -> None:
    r = client.post(f"/v1/job/{jid}/documents",
                    json={"doc_key": doc_key, "version": "v1",
                          "file_sha256": "a" * 64, "verified": verified})
    assert r.status_code == 200, r.text


def seed_facts(jid: str, **overrides: dict) -> None:
    facts = {kind: dict(payload) for kind, payload in DEFAULT_FACTS.items()}
    facts.update({kind: payload for kind, payload in overrides.items()})
    for kind, payload in facts.items():
        put_server(jid, kind, payload)


def put_server(jid: str, kind: str, payload: dict) -> dict:
    r = client.post(f"/internal/job/{jid}/records",
                    json={"kind": kind, "payload": payload}, headers=TOKEN)
    assert r.status_code == 200, r.text
    return r.json()


def put_client(jid: str, kind: str, payload: dict):
    return client.post(f"/v1/job/{jid}/records", json={"kind": kind, "payload": payload})


def run_gates(jid: str, actor: str = "judge") -> dict:
    r = client.post(f"/v1/jgate/{jid}/run", json={"actor": actor})
    assert r.status_code == 200, r.text
    return r.json()


def view(jid: str) -> dict:
    r = client.get(f"/v1/jgate/{jid}")
    assert r.status_code == 200, r.text
    return r.json()


def complete_job(**overrides: dict) -> str:
    """六门可直接全绿的鉴定任务（仍为 SHADOW，未签发）。"""
    jid = new_job()
    seed_facts(jid, **overrides)
    out = run_gates(jid)
    assert out["all_gates_passed"] is True, out
    return jid


def draft(jid: str) -> dict:
    r = client.post(f"/v1/jgate/{jid}/draft", json={"title": "造价鉴定意见书"})
    assert r.status_code == 200, r.text
    return r.json()


def signoff(jid: str, actor: str = "zhao-gong", license_: str = "APPRAISER",
            statement: str = "本人已逐项复核，确认鉴定意见与依据一致，同意签署。"):
    return client.post(f"/v1/jgate/{jid}/signoff",
                       headers={"X-Actor": actor or "tester"},
                       json={"actor": actor, "license": license_, "statement": statement})


def issue(jid: str, actor: str = "tester"):
    return client.post(f"/v1/jgate/{jid}/issue", headers={"X-Actor": actor},
                       json={"actor": actor})


# ---------------------------------------------------------- 1 契约可查询
def test_jgate_policy_declares_six_shadow_gates():
    r = client.get("/v1/jgate")
    assert r.status_code == 200, r.text
    policy = r.json()
    assert policy["gate_sequence"] == list(J_GATES) == ["J0", "J1", "J2", "J3", "J4", "J5"]
    assert policy["business_line"] == J_LINE
    assert policy["shadow_run"] is True
    assert policy["auto_issue"] is False
    assert policy["requires_manual_signoff"] is True
    assert policy["default_deliverable_state"] == "SHADOW"
    assert "j_signoff" in policy["reserved_record_kinds"]
    assert "APPRAISER" in policy["signoff_licenses"]

    seq = client.get("/v1/gate/sequence/L6").json()
    assert seq["gates"] == list(J_GATES)
    # 纯函数契约与 API 契约同源
    assert j_shadow_policy()["gate_sequence"] == list(J_GATES)


# ------------------------------------------- 2 无 job_id / 错业务线不可绕过
@pytest.mark.parametrize("code", J_GATES)
def test_j_gate_without_job_id_is_rejected(code):
    """请求体写满"已受理/已质证"也不能在无 job_id 时拿到任何 J 门结论。"""
    body = {
        "business_line": J_LINE, "gate_code": code, "basis_pack": PACK,
        "available_documents": ALL_J_DOCS,
        "context": {"conflict_of_interest": False, "unverified_materials_used": False,
                    "undisputed_mixed": False, "unsupported_calc": False,
                    "opinion_form": "QUALIFIED", "evidence_level": "MEDIUM"},
    }
    r = client.post("/v1/gate/check", json=body)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "JD_GATE_NOT_READY", r.text


@pytest.mark.parametrize("code", J_GATES)
def test_j_gate_refuses_compilation_line(code):
    r = client.post("/v1/gate/check",
                    json={"business_line": "L1", "gate_code": code, "basis_pack": PACK,
                          "available_documents": ALL_J_DOCS})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "JD_LINE_MIXED", r.text


def test_j_line_endpoints_reject_compilation_job():
    jid = new_job(line="L1", docs=[])
    r = client.get(f"/v1/jgate/{jid}")
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "JD_LINE_MIXED"
    r = client.post(f"/v1/jgate/{jid}/run", json={"actor": "judge"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "JD_LINE_MIXED"
    r = client.post(f"/v1/jgate/{jid}/draft", json={})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "JD_LINE_MIXED"


# ------------------------------------------------ 3 六门服务端判定（全绿）
def test_six_j_gates_pass_in_order_and_stay_shadow():
    jid = complete_job()
    out = run_gates(jid)
    assert [item["gate"] for item in out["results"]] == list(J_GATES)
    for item in out["results"]:
        assert item["result"] == "PASS", item
        assert item["shadow_run"] is True
        assert item["auto_issue"] is False
        assert item["requires_manual_signoff"] is True
        assert item["authority_sources"], item
        assert item["job_id"] == jid

    state = view(jid)
    assert state["all_gates_passed"] is True
    assert state["gate_results"] == {gate: "PASS" for gate in J_GATES}
    assert [g["gate"] for g in state["gates"]] == list(J_GATES)
    assert all(g["passed"] for g in state["gates"])
    assert all(g["judged_at"] for g in state["gates"])
    # 全绿仍不可签发：影子运行，未自动生成成果
    assert state["auto_issue"] is False
    assert state["deliverable_state"] == "SHADOW"
    assert state["deliverable"]["issued"] is False
    assert state["issuable"] is False
    assert api_store.get_records(jid, "j_deliverable") == []
    assert api_store.get_records(jid, "j_signoff") == []


def test_single_j_gate_check_is_server_authoritative_and_shadow():
    """带 job_id 的 /v1/gate/check：忽略自述 context，只认服务端事实，且带影子标记。"""
    jid = complete_job()
    body = {"job_id": jid, "business_line": J_LINE, "gate_code": "J3", "basis_pack": PACK,
            "available_documents": [], "context": {"undisputed_mixed": True}}
    r = client.post("/v1/gate/check", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["result"] == "PASS", out
    assert out["authoritative"] is True and out["advisory"] is False
    assert out["context_ignored"] is True
    assert out["shadow_run"] is True and out["auto_issue"] is False
    assert out["authority_sources"]
    assert out["job_id"] == jid


# ---------------------------------------- 4 六门逐门失败（每门都有独立判据）
GATE_FAILURE_CASES = [
    ("J0", {"drop": "qualification"}, {}, "J0.DOC", "JD_ACCEPT_FAILED"),
    ("J0", {"drop": None}, {"conflict": {"exists": True}}, "J0.CONFLICT", "JD_ACCEPT_FAILED"),
    ("J1", {"drop": "cross_examination_record"}, {}, "J1.DOC", "JD_MATERIAL_UNVERIFIED"),
    ("J1", {"drop": None}, {"material": {"unverified_used": True}},
     "J1.UNVERIFIED", "JD_MATERIAL_UNVERIFIED"),
    ("J2", {"drop": "photo_index"}, {}, "J2.DOC", "JD_ISSUE_NOT_DECOMPOSED"),
    ("J3", {"drop": "method_selection"}, {}, "J3.DOC", "JD_ISSUE_NOT_DECOMPOSED"),
    ("J3", {"drop": None}, {"dispute": {"undisputed_mixed": True}},
     "J3.SCOPE", "JD_ISSUE_NOT_DECOMPOSED"),
    ("J4", {"drop": None}, {"calc_support": {"unsupported": True}},
     "J4.SUPPORT", "JD_CALC_UNSUPPORTED"),
    ("J4", {"drop": None}, {"opinion": {"form": "DEFINITE", "evidence_level": "LOW"}},
     "J4.FORM", "JD_OPINION_INCOMPLETE"),
    ("J5", {"drop": None}, {"opinion": {"form": "LEGAL_JUDGEMENT",
                                       "evidence_level": "MEDIUM"}},
     "J5.LEGAL", "JD_OPINION_INCOMPLETE"),
    ("J5", {"drop": "citation_list"}, {}, "J5.DOC", "JD_OPINION_INCOMPLETE"),
]


@pytest.mark.parametrize("target,doc_case,fact_case,rule_id,error_code", GATE_FAILURE_CASES)
def test_each_j_gate_blocks_on_its_own_rule(target, doc_case, fact_case, rule_id, error_code):
    docs = [doc for doc in ALL_J_DOCS if doc != doc_case.get("drop")]
    jid = new_job(docs=docs)
    seed_facts(jid, **fact_case)
    out = run_gates(jid)

    item = next(r for r in out["results"] if r["gate"] == target)
    assert item["result"] == "FAIL", item
    assert any(rule["rule_id"] == rule_id and rule["error_code"] == error_code
               for rule in item["failed_rules"]), item["failed_rules"]
    # 失败必须落 gate_record，且后续门被顺序挡住（禁止跳门）
    stored = [r for r in api_store.list_gate_records(jid) if r["gate_code"] == target]
    assert stored and stored[-1]["result"] == "FAIL"
    assert out["all_gates_passed"] is False
    assert view(jid)["deliverable_state"] == "SHADOW"


def test_unverified_document_blocks_j0():
    jid = new_job(docs=[])
    for doc in ALL_J_DOCS:
        add_doc(jid, doc, verified=doc != "qualification")
    seed_facts(jid)
    out = run_gates(jid)
    j0 = next(r for r in out["results"] if r["gate"] == "J0")
    assert j0["result"] == "FAIL"
    assert any(rule["rule_id"] == "J0.UNVERIFIED" for rule in j0["failed_rules"]), j0["failed_rules"]


# ------------------------------- 5 默认不可签发：无签署 → 拒绝出件
def test_deliverable_is_not_issuable_without_manual_signoff():
    jid = complete_job()
    opinion = draft(jid)
    assert opinion["state"] == "SHADOW"
    assert opinion["issued"] is False
    assert opinion["auto_issue"] is False
    assert opinion["requires_manual_signoff"] is True

    state = view(jid)
    assert state["deliverable_state"] == "SHADOW"
    assert state["signoff"] is None
    assert state["signoff_valid"] is False
    assert state["issuable"] is False

    r = issue(jid)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "JD_SIGNOFF_REQUIRED", detail
    assert detail["issued"] is False and detail["auto_issue"] is False
    assert any(rule["rule_id"] == "J5.SIGNOFF" for rule in detail["failed_rules"]), detail
    assert api_store.get_records(jid, "j_deliverable") == []
    assert view(jid)["deliverable_state"] == "SHADOW"


def test_signoff_requires_completed_six_gates_and_draft():
    # 未跑门禁：不得签署
    jid = new_job()
    seed_facts(jid)
    draft(jid)
    r = signoff(jid)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "JD_GATE_NOT_READY"
    # 六门全过但无草稿：不得签署
    jid2 = complete_job()
    r2 = signoff(jid2)
    assert r2.status_code == 422, r2.text
    assert r2.json()["detail"]["code"] == "JD_OPINION_INCOMPLETE"


@pytest.mark.parametrize("actor,license_,statement,status,code", [
    ("system", "APPRAISER", "本人已复核并同意签署。", 422, "JD_AUTO_ISSUE_FORBIDDEN"),
    ("", "APPRAISER", "本人已复核并同意签署。", 403, "SIGNOFF_ACTOR_MISMATCH"),
    ("zhao-gong", "NONE", "本人已复核并同意签署。", 403, "PROFESSIONAL_IDENTITY_REQUIRED"),
    ("zhao-gong", "APPRAISER", "", 422, "JD_SIGNOFF_REQUIRED"),
])
def test_signoff_rejects_auto_or_unqualified_signers(actor, license_, statement, status, code):
    jid = complete_job()
    draft(jid)
    r = signoff(jid, actor=actor, license_=license_, statement=statement)
    assert r.status_code == status, r.text
    assert r.json()["detail"]["code"] == code, r.text
    assert api_store.get_records(jid, "j_signoff") == []
    assert view(jid)["deliverable_state"] == "SHADOW"


# ------------------------------------- 6 人工签署 → 显式出件；签署失效拦截
def test_manual_signoff_then_explicit_issue():
    jid = complete_job()
    opinion = draft(jid)
    signed = signoff(jid)
    assert signed.status_code == 200, signed.text
    payload = signed.json()
    assert payload["manual"] is True
    assert payload["signed_by"] == "zhao-gong"
    assert payload["license"] == "APPRAISER"
    assert payload["issued"] is False
    assert payload["auto_issue"] is False
    assert payload["opinion_id"] == opinion["opinion_id"]
    assert payload["gate_snapshot_sha256"] == view(jid)["gate_snapshot_sha256"]

    after_sign = view(jid)
    assert after_sign["deliverable_state"] == "SIGNED"
    assert after_sign["signoff_valid"] is True
    assert after_sign["issuable"] is True
    assert after_sign["deliverable"]["issued"] is False
    assert api_store.get_records(jid, "j_deliverable") == []

    r = issue(jid)
    assert r.status_code == 200, r.text
    issued = r.json()
    assert issued["issued"] is True
    assert issued["state"] == "ISSUED"
    assert issued["auto_issue"] is False
    assert issued["manual_signoff"] is True
    assert issued["issued_by"] == "zhao-gong"
    assert issued["opinion_id"] == opinion["opinion_id"]

    final = view(jid)
    assert final["deliverable_state"] == "ISSUED"
    assert final["deliverable"]["issued"] is True
    assert final["deliverable"]["auto_issue"] is False
    assert final["issuable"] is False

    replay = issue(jid)
    assert replay.status_code == 200, replay.text
    assert replay.json()["idempotent_replay"] is True


def test_signoff_rejects_request_body_signer_spoofing():
    jid = complete_job()
    draft(jid)
    r = client.post(f"/v1/jgate/{jid}/signoff", headers={"X-Actor": "attacker"},
                    json={"actor": "zhao-gong", "license": "APPRAISER",
                          "statement": "TEST_ONLY forged identity"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "SIGNOFF_ACTOR_MISMATCH"
    assert api_store.get_records(jid, "j_signoff") == []


def test_signoff_requires_server_registered_matching_test_credential(monkeypatch):
    jid = complete_job()
    draft(jid)
    monkeypatch.delenv("TXBUDDY_PROFESSIONALS_FILE", raising=False)
    r = signoff(jid)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "PROFESSIONAL_IDENTITY_REQUIRED"
    assert api_store.get_records(jid, "j_signoff") == []


def test_issue_rejects_request_body_operator_spoofing():
    jid = complete_job()
    draft(jid)
    assert signoff(jid).status_code == 200
    r = client.post(f"/v1/jgate/{jid}/issue", headers={"X-Actor": "attacker"},
                    json={"actor": "tester"})
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "TASK_ACTOR_MISMATCH"
    assert api_store.get_records(jid, "j_deliverable") == []


def test_signoff_goes_stale_when_gate_result_changes():
    jid = complete_job()
    draft(jid)
    assert signoff(jid).status_code == 200
    # 签署后意见形态变成法律判断，J5 被改判为 FAIL → 原签署失效
    put_server(jid, "opinion", {"form": "LEGAL_JUDGEMENT", "evidence_level": "MEDIUM"})
    rerun = run_gates(jid)
    assert rerun["gate_results"]["J5"] == "FAIL"
    r = issue(jid)
    assert r.status_code == 422, r.text
    rules = r.json()["detail"]["failed_rules"]
    assert any(rule["rule_id"] == "J5.STALE" and rule["error_code"] == "JD_SIGNOFF_STALE"
               for rule in rules), rules
    assert any(rule["rule_id"] == "J5.GATE" for rule in rules), rules
    assert api_store.get_records(jid, "j_deliverable") == []


# --------------------------- 7 通用接口不得伪造签署 / 鉴定编制口径隔离
def test_generic_record_endpoints_cannot_forge_signoff():
    jid = complete_job()
    draft(jid)
    forged = {"signed_by": "zhao-gong", "license": "APPRAISER", "manual": True,
              "signed_at": "2026-01-01T00:00:00+00:00", "statement": "伪造签署"}
    r = put_client(jid, "j_signoff", forged)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "JD_AUTO_ISSUE_FORBIDDEN"
    r = client.post(f"/internal/job/{jid}/records",
                    json={"kind": "j_signoff", "payload": forged}, headers=TOKEN)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "JD_AUTO_ISSUE_FORBIDDEN"
    assert api_store.get_records(jid, "j_signoff") == []
    assert issue(jid).status_code == 422


def test_j_line_blocks_compilation_report_and_client_facts():
    jid = complete_job()
    r = put_client(jid, "report", {"generated": True, "layout_checked": True})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "JD_LINE_MIXED"
    r = put_client(jid, "opinion", {"form": "QUALIFIED", "evidence_level": "MEDIUM"})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "JD_GATE_NOT_READY"
    # 服务端写入同一事实是允许的（门禁唯一事实入口）
    assert put_server(jid, "opinion", {"form": "QUALIFIED", "evidence_level": "MEDIUM"})[
        "source"] == "SERVER"
