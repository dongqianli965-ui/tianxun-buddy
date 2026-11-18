"""P1-01-JOBMODEL：六业务线共用 Job 数据模型 + 状态机合法性校验。

覆盖：
1. Job 领域模型（job_id / project_id / business_line 枚举 / status 状态机 /
   basis_pack 引用 / documents / tasks / evidence / 审计字段）在 models.py 一次定义，
   六条业务线共用同一模型，无业务线专属字段；
2. 状态机迁移表完备，表外迁移（含同状态空转、跳级、终态出边）全部被拒绝，
   拒绝时数据库状态不变；
3. store 层落地：旧库自动补列、创建即写首条迁移事件、迁移写审计（谁/何时/原因）；
4. 聚合根把 documents / tasks / evidence 关联到同一个 Job；
5. API /v1/job 六条业务线均可创建，创建后状态为 CREATED。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from tests.authenticated_client import TestClient
from pydantic import ValidationError

from txbuddy.api import app, store as api_store
from txbuddy.api_models import JobCreateRequest, JobStatusChangeRequest, JobSummary
from txbuddy.core.gates import GATE_SEQUENCE, REQUIRED
from txbuddy.models import (
    BUSINESS_LINE_LABELS,
    JOB_INITIAL_STATUS,
    JOB_TERMINAL_STATUSES,
    JOB_TRANSITIONS,
    BasisPack,
    BusinessLine,
    Job,
    JobStatus,
    JobTransitionError,
    JobValidationError,
    allowed_job_transitions,
    can_transition_job,
    parse_business_line,
    parse_job_status,
    validate_job_transition,
)
from txbuddy.store import EvidenceStore

client = TestClient(app)

PACK = {
    "basis_pack_id": "B-JOBMODEL", "region_code": "410100", "region_level": "CITY",
    "major": "BUILDING", "effective_date": "2026-01-01",
    "measurement_standard_version": "GB/T 50854-2024",
    "pricing_standard_version": "GB/T 50500-2024",
    "rule_version_scope": "HENAN_2025", "precision_rule_version": "P-V1",
    "status": "LOCKED", "test_only": False,
    "sources": [{"source_id": "S1", "title": "河南定额", "source_type": "QUOTA",
                 "license_status": "LICENSED", "verified": True}],
}

# 期望的状态机（与 models.JOB_TRANSITIONS 独立写一份，防止实现被“顺手改宽”）
EXPECTED_TRANSITIONS = {
    "CREATED": {"BASIS_BOUND", "INTAKE", "CANCELLED"},
    "BASIS_BOUND": {"INTAKE", "CANCELLED"},
    "INTAKE": {"IN_PROGRESS", "CANCELLED"},
    "IN_PROGRESS": {"REVIEW", "CANCELLED"},
    "REVIEW": {"IN_PROGRESS", "SIGNED", "CANCELLED"},
    "SIGNED": {"DELIVERED", "ARCHIVED"},
    "DELIVERED": {"ARCHIVED"},
    "ARCHIVED": set(),
    "CANCELLED": set(),
}


# --------------------------------------------------------------------- fixtures
@pytest.fixture()
def store(tmp_path):
    return EvidenceStore(str(tmp_path / "jobmodel.sqlite3"))


def new_job(store: EvidenceStore, line: str = "L1", job_id: str | None = None,
            pack: dict | None = None) -> str:
    jid = job_id or f"job-{line}-{datetime.now(timezone.utc).timestamp()}"
    store.create_job(jid, f"P-{line}", f"{line} 项目", line, pack or PACK, "tester")
    return jid


# --------------------------------------------------------------- 领域模型定义
def test_business_line_enum_covers_exactly_six_lines():
    assert [b.value for b in BusinessLine] == ["L1", "L2", "L3", "L4", "L5", "L6"]
    assert set(BUSINESS_LINE_LABELS) == {b.value for b in BusinessLine}
    assert BUSINESS_LINE_LABELS["L1"] == "预算编制"
    assert BUSINESS_LINE_LABELS["L6"] == "造价鉴定"
    # 与门禁体系同一套业务线键，不允许两套字符串约定
    assert set(REQUIRED) == {b.value for b in BusinessLine}
    assert set(GATE_SEQUENCE) == {b.value for b in BusinessLine}


def test_business_line_parsing_accepts_alias_forms_and_rejects_others():
    assert parse_business_line("l1") is BusinessLine.L1
    assert parse_business_line(" L6 ") is BusinessLine.L6
    assert parse_business_line(BusinessLine.L3) is BusinessLine.L3
    for bad in ["", "L0", "L7", "预算编制", None, 3]:
        with pytest.raises(JobValidationError) as ei:
            parse_business_line(bad)
        assert ei.value.code == "JOB_BUSINESS_LINE_INVALID"


def test_job_status_machine_table_matches_contract():
    assert {s.value for s in JobStatus} == set(EXPECTED_TRANSITIONS)
    for status in JobStatus:
        targets = {t.value for t in JOB_TRANSITIONS[status]}
        assert targets == EXPECTED_TRANSITIONS[status.value], status.value
        assert all(isinstance(t, JobStatus) for t in JOB_TRANSITIONS[status])
    assert JOB_TERMINAL_STATUSES == {JobStatus.ARCHIVED, JobStatus.CANCELLED}
    assert JOB_INITIAL_STATUS is JobStatus.CREATED
    # 每条业务线都能从 CREATED 走到 ARCHIVED（不存在的状态无法交付）
    for target in (JobStatus.DELIVERED, JobStatus.ARCHIVED):
        assert any(target in JOB_TRANSITIONS[s] for s in JobStatus)


def test_every_illegal_transition_is_rejected():
    legal = {(s, t) for s in JobStatus for t in JOB_TRANSITIONS[s]}
    checked = 0
    for current in JobStatus:
        for target in JobStatus:
            if (current, target) in legal:
                continue
            checked += 1
            assert can_transition_job(current, target) is False
            with pytest.raises(JobTransitionError) as ei:
                validate_job_transition(current, target)
            assert ei.value.code == "JOB_STATUS_ILLEGAL"
            assert ei.value.current is current and ei.value.target is target
            assert ei.value.allowed == JOB_TRANSITIONS[current]
    # 9 状态两两组合 81 对，非法对必须全部覆盖（不是抽样）
    assert checked == len(list(JobStatus)) ** 2 - len(legal)


def test_legal_transitions_are_accepted():
    for current in JobStatus:
        for target in JOB_TRANSITIONS[current]:
            assert can_transition_job(current, target) is True
            assert validate_job_transition(current, target) is target
            assert allowed_job_transitions(current) == JOB_TRANSITIONS[current]


def test_unknown_status_rejected():
    for bad in ["", "DONE", "CREATEDD", 7, None]:
        with pytest.raises(JobValidationError) as ei:
            parse_job_status(bad)
        assert ei.value.code == "JOB_STATUS_UNKNOWN"


def test_status_parsing_only_tolerates_case():
    assert parse_job_status("created") is JobStatus.CREATED
    assert parse_job_status(JobStatus.REVIEW) is JobStatus.REVIEW
    with pytest.raises(JobValidationError) as ei:
        parse_job_status("CREATEDD")
    assert ei.value.code == "JOB_STATUS_UNKNOWN"


# ------------------------------------------------------------------ store 创建
@pytest.mark.parametrize("line", [b.value for b in BusinessLine])
def test_create_job_all_six_business_lines(store, line):
    jid = new_job(store, line)
    row = store.get_job(jid)
    assert row["business_line"] == line and row["status"] == "CREATED"
    assert row["basis_pack_id"] == PACK["basis_pack_id"]
    assert row["revision"] == 1
    job = store.get_job_model(jid)
    assert isinstance(job, Job)
    assert job.business_line is BusinessLine(line)
    assert job.business_line_label == BUSINESS_LINE_LABELS[line]
    assert job.basis_pack == BasisPack(**PACK)
    assert job.allowed_next_statuses() == [JobStatus.BASIS_BOUND, JobStatus.CANCELLED,
                                           JobStatus.INTAKE]
    assert [e.to_status for e in job.status_history] == [JobStatus.CREATED]
    assert job.status_history[0].by == "tester"


def test_create_job_rejects_unknown_business_line(store):
    with pytest.raises(JobValidationError) as ei:
        store.create_job("job-x", "P-X", "x", "L9", PACK, "tester")
    assert ei.value.code == "JOB_BUSINESS_LINE_INVALID"
    assert store.get_job("job-x") is None


def test_create_job_rejects_incomplete_basis_pack(store):
    with pytest.raises(JobValidationError) as ei:
        store.create_job("job-y", "P-Y", "y", "L1", {"basis_pack_id": "B-Y"}, "tester")
    assert ei.value.code == "JOB_BASIS_PACK_INVALID"
    assert store.get_job("job-y") is None

    extra = dict(PACK, not_a_field=1)  # BasisPack extra=forbid
    with pytest.raises(JobValidationError) as ei2:
        store.create_job("job-z", "P-Z", "z", "L1", extra, "tester")
    assert ei2.value.code == "JOB_BASIS_PACK_INVALID"


def test_create_job_requires_project_and_name(store):
    with pytest.raises(JobValidationError) as ei:
        store.create_job("job-a", "  ", "name", "L1", PACK, "tester")
    assert ei.value.code == "JOB_PROJECT_REQUIRED"
    with pytest.raises(JobValidationError) as ei2:
        store.create_job("job-b", "P", "", "L1", PACK, "tester")
    assert ei2.value.code == "JOB_NAME_REQUIRED"


# ---------------------------------------------------------------- store 迁移
def test_legal_path_created_to_archived(store):
    jid = new_job(store, "L6")
    path = ["BASIS_BOUND", "INTAKE", "IN_PROGRESS", "REVIEW", "SIGNED", "DELIVERED", "ARCHIVED"]
    for i, target in enumerate(path, start=1):
        row = store.set_job_status(jid, target, by="pm", reason=f"step{i}")
        assert row["status"] == target
        assert row["revision"] == 1 + i
    job = store.get_job_model(jid)
    assert job.status is JobStatus.ARCHIVED and job.is_terminal
    assert job.updated_by == "pm"
    # 审计链：创建 1 条 + 7 次迁移，首尾闭合
    assert len(job.status_history) == 8
    assert job.status_history[0].to_status is JobStatus.CREATED
    assert [e.to_status.value for e in job.status_history[1:]] == path
    assert [e.reason for e in job.status_history[1:]] == [f"step{i}" for i in range(1, 8)]
    assert job.status_history[1].from_status is JobStatus.CREATED
    assert all(e.by == "pm" for e in job.status_history[1:])


def test_review_can_return_to_in_progress(store):
    jid = new_job(store)
    for target in ["INTAKE", "IN_PROGRESS", "REVIEW"]:
        store.set_job_status(jid, target, by="pm")
    row = store.set_job_status(jid, "IN_PROGRESS", by="reviewer", reason="返工")
    assert row["status"] == "IN_PROGRESS"
    assert row["status_history"][-1]["reason"] == "返工"


def test_illegal_transition_rejected_and_db_unchanged(store):
    jid = new_job(store, "L6")
    before = store.get_job(jid)
    with pytest.raises(JobTransitionError) as ei:
        store.set_job_status(jid, "SIGNED", by="pm")  # CREATED -> SIGNED 跳级
    assert ei.value.code == "JOB_STATUS_ILLEGAL"
    assert ei.value.allowed == JOB_TRANSITIONS[JobStatus.CREATED]
    after = store.get_job(jid)
    assert after["status"] == before["status"] == "CREATED"
    assert after["revision"] == before["revision"] == 1
    assert after["status_history"] == before["status_history"]


def test_same_status_transition_rejected(store):
    jid = new_job(store)
    with pytest.raises(JobTransitionError):
        store.set_job_status(jid, "CREATED", by="pm")


def test_terminal_status_has_no_outgoing_transition(store):
    jid = new_job(store)
    store.set_job_status(jid, "CANCELLED", by="pm", reason="客户撤销")
    assert store.get_job_model(jid).is_terminal
    for target in JobStatus:
        with pytest.raises(JobTransitionError):
            store.set_job_status(jid, target.value, by="pm")
    assert store.get_job(jid)["status"] == "CANCELLED"


def test_unknown_target_status_rejected(store):
    jid = new_job(store)
    with pytest.raises(JobValidationError) as ei:
        store.set_job_status(jid, "FINISHED", by="pm")
    assert ei.value.code == "JOB_STATUS_UNKNOWN"
    assert store.get_job(jid)["status"] == "CREATED"


def test_missing_job_transition_raises_not_found(store):
    with pytest.raises(JobValidationError) as ei:
        store.set_job_status("no-such-job", "INTAKE", by="pm")
    assert ei.value.code == "JOB_NOT_FOUND"


def test_concurrent_status_change_detected(store, monkeypatch):
    """CAS 失败路径：读到旧状态但库里已变 → JOB_STATUS_CONFLICT，不覆盖别人的迁移。"""
    jid = new_job(store)
    store.set_job_status(jid, "INTAKE", by="pm")
    stale = dict(store.get_job(jid), status="CREATED", revision=1)
    monkeypatch.setattr(store, "get_job", lambda _jid: dict(stale))
    with pytest.raises(JobValidationError) as ei:
        store.set_job_status(jid, "BASIS_BOUND", by="other")
    assert ei.value.code == "JOB_STATUS_CONFLICT"
    monkeypatch.undo()
    assert store.get_job(jid)["status"] == "INTAKE"  # 真实状态未被陈旧读取覆盖


# ------------------------------------------------------------------ Job 聚合
def test_job_aggregate_links_documents_tasks_evidence(store):
    jid = new_job(store, "L3")
    store.add_document(jid, "contract", "v1", "a" * 64, verified=True)
    store.add_document(jid, "as_built_drawings", "v2", "b" * 64)
    store.create_task("task-1", jid, "REVIEW_BOQ", {"x": 1}, "u1")
    store.update_task("task-1", status="DONE", by="u1", result={"ok": True}, approver="u2")
    store.save({"evidence_id": "ev-1", "sha256": "c" * 64, "job_id": jid,
                "created_at": datetime.now(timezone.utc).isoformat(), "kind": "CALC"})
    store.save({"evidence_id": "ev-other", "sha256": "d" * 64, "job_id": "other-job",
                "created_at": datetime.now(timezone.utc).isoformat()})

    job = store.get_job_model(jid)
    assert [d.doc_key for d in job.documents] == ["contract", "as_built_drawings"]
    assert [d.verified for d in job.documents] == [True, False]
    assert [t.task_id for t in job.tasks] == ["task-1"]
    assert job.tasks[0].status == "DONE" and job.tasks[0].result == {"ok": True}
    assert job.tasks[0].approver == "u2"
    assert [e.evidence_id for e in job.evidence] == ["ev-1"]  # 只关联本 Job
    assert store.list_job_evidence_ids(jid) == ["ev-1"]

    summary = JobSummary.from_job(job)
    assert summary.document_count == 2 and summary.task_count == 1 and summary.evidence_count == 1
    assert summary.basis_pack_id == PACK["basis_pack_id"]
    assert summary.business_line is BusinessLine.L3
    assert store.get_job_summary(jid) == summary
    assert store.get_job_summary("nope") is None


def test_missing_job_returns_none(store):
    assert store.get_job("nope") is None
    assert store.get_job_model("nope") is None


def test_job_model_forbids_extra_fields():
    base = dict(job_id="j", project_id="p", name="n", business_line="L1", status="CREATED",
                basis_pack=PACK, basis_pack_id=PACK["basis_pack_id"], created_by="t",
                created_at=datetime.now(timezone.utc))
    Job(**base)  # 合法
    with pytest.raises(ValidationError):
        Job(**base, unknown_field=1)
    for missing in ["job_id", "business_line", "status", "basis_pack", "created_by"]:
        broken = {k: v for k, v in base.items() if k != missing}
        with pytest.raises(ValidationError):
            Job(**broken)


def test_job_model_rejects_unknown_status_or_business_line():
    base = dict(job_id="j", project_id="p", name="n", business_line="L1", status="CREATED",
                basis_pack=PACK, basis_pack_id=PACK["basis_pack_id"], created_by="t",
                created_at=datetime.now(timezone.utc))
    with pytest.raises(ValidationError):
        Job(**dict(base, status="DONE"))
    with pytest.raises(ValidationError):
        Job(**dict(base, business_line="预算编制"))


def test_job_model_basis_pack_reference_must_match_snapshot():
    base = dict(job_id="j", project_id="p", name="n", business_line="L1", status="CREATED",
                basis_pack=PACK, basis_pack_id=PACK["basis_pack_id"], created_by="t",
                created_at=datetime.now(timezone.utc))
    assert Job(**base).basis_pack_id == PACK["basis_pack_id"]
    with pytest.raises(ValidationError):
        Job(**dict(base, basis_pack_id="B-OTHER"))


# -------------------------------------------------------------------- API 契约
def test_api_models_job_create_request_contract():
    req = JobCreateRequest(project_id="P", name="N", business_line="l1", basis_pack=PACK)
    assert req.business_line is BusinessLine.L1
    assert req.basis_pack_payload()["basis_pack_id"] == PACK["basis_pack_id"]
    with pytest.raises(ValidationError):
        JobCreateRequest(project_id="P", name="N", business_line="L7", basis_pack=PACK)
    with pytest.raises(ValidationError):
        JobCreateRequest(project_id="P", name="N", business_line="L1",
                         basis_pack={"basis_pack_id": "only"})
    with pytest.raises(ValidationError):
        JobCreateRequest(project_id="", name="N", business_line="L1", basis_pack=PACK)


def test_api_models_status_change_request_contract():
    req = JobStatusChangeRequest(target_status="intake", by="pm", reason="受理")
    assert req.target_status is JobStatus.INTAKE and req.reason == "受理"
    with pytest.raises(ValidationError):
        JobStatusChangeRequest(target_status="SHIPPED")
    with pytest.raises(ValidationError):
        JobStatusChangeRequest(target_status="INTAKE", unexpected=1)


@pytest.mark.parametrize("line", [b.value for b in BusinessLine])
def test_api_job_create_all_business_lines(line):
    r = client.post("/v1/job", json={"project_id": f"P-{line}", "name": f"{line} 项目",
                                     "business_line": line, "basis_pack": PACK,
                                     "created_by": "tester"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["business_line"] == line and body["status"] == "CREATED"
    job = client.get(f"/v1/job/{body['job_id']}").json()
    assert job["business_line"] == line and job["status"] == "CREATED"
    assert job["basis_pack"]["basis_pack_id"] == PACK["basis_pack_id"]
    assert job["documents"] == [] and job["tasks"] == []
    model = api_store.get_job_model(body["job_id"])
    assert model.business_line is BusinessLine(line)
    assert model.revision == 1


def test_api_job_status_machine_applies_to_api_created_job():
    r = client.post("/v1/job", json={"project_id": "P-L6", "name": "鉴定项目",
                                     "business_line": "L6", "basis_pack": PACK,
                                     "created_by": "tester"})
    jid = r.json()["job_id"]
    assert api_store.set_job_status(jid, "INTAKE", by="pm")["status"] == "INTAKE"
    with pytest.raises(JobTransitionError):
        api_store.set_job_status(jid, "ARCHIVED", by="pm")  # 跳级
    assert api_store.get_job(jid)["status"] == "INTAKE"


# ------------------------------------------------------------- 旧库迁移兼容
LEGACY_DDL = """
CREATE TABLE job(
    job_id TEXT PRIMARY KEY, project_id TEXT, name TEXT, business_line TEXT,
    basis_pack TEXT, status TEXT, created_by TEXT, created_at TEXT);
"""


def test_legacy_job_table_migrates_without_data_loss(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    pack_json = __import__("json").dumps(PACK, ensure_ascii=False)
    con = sqlite3.connect(path)
    con.executescript(LEGACY_DDL)
    con.execute("INSERT INTO job VALUES(?,?,?,?,?,?,?,?)",
                ("job-legacy", "P-OLD", "旧库作业", "L1", pack_json, "CREATED", "old", 
                 "2026-01-01T00:00:00+00:00"))
    con.commit()
    con.close()

    store = EvidenceStore(str(path))
    cols = store._columns("job")
    assert {"basis_pack_id", "updated_by", "updated_at", "revision", "status_history"} <= cols
    row = store.get_job("job-legacy")
    assert row["business_line"] == "L1" and row["status"] == "CREATED"
    assert row["basis_pack_id"] == PACK["basis_pack_id"]  # 由 basis_pack 回填
    assert row["revision"] == 1
    job = store.get_job_model("job-legacy")
    assert job.status is JobStatus.CREATED and job.basis_pack_id == PACK["basis_pack_id"]
    # 旧库审计链补头：一条 JOB_CREATE_LEGACY，重复初始化不重复补
    assert [e.reason for e in job.status_history] == ["JOB_CREATE_LEGACY"]
    assert job.status_history[0].by == "old"
    EvidenceStore(str(path))
    assert len(store.get_job_model("job-legacy").status_history) == 1
    # 旧库行同样受状态机约束
    with pytest.raises(JobTransitionError):
        store.set_job_status("job-legacy", "ARCHIVED", by="pm")
    assert store.set_job_status("job-legacy", "INTAKE", by="pm")["status"] == "INTAKE"


def test_business_line_stored_as_plain_string_in_db(store):
    """API 返回值保持字符串（既有契约），枚举只做校验不改变线上格式。"""
    jid = new_job(store, "L4")
    row = store.get_job(jid)
    assert type(row["business_line"]) is str and type(row["status"]) is str
