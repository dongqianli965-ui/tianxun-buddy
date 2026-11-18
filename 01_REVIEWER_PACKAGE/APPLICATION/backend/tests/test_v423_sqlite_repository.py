"""P1-02-SQLITE-REPO：Repository 抽象接口 + SQLite 实现的逐方法覆盖。

覆盖范围（与 TASK.md 的八类仓储一一对应）：
1. 抽象接口：8 类仓储 + RepositorySet 的抽象方法齐备，直接实例化必须失败；
2. SQLite 实现：Evidence / CalcRun / Audit / Idem / Job / Document / Task / Gate / Record
   的每个方法（含 None、空集合、覆盖写、CAS 失败等边界）；
3. 幂等语义：认领唯一裁判 = 数据库唯一约束（changes()==1）、冲突、释放、陈旧认领接管；
4. store.py 委托：EvidenceStore 只做领域校验，SQL 已全部下沉（源码不得再出现 SQL 语句）。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import txbuddy
from txbuddy.repository import (
    AuditRepository,
    CalcRunRepository,
    DocumentRepository,
    EvidenceRepository,
    GateRepository,
    IdemRepository,
    JobRepository,
    RecordRepository,
    RepositorySet,
    SqliteRepositorySet,
    TaskRepository,
    build_sqlite_repositories,
)
from txbuddy.repository.base import Repository, build_idem_scope_key
from txbuddy.repository.schema import now_iso
from txbuddy.store import EvidenceStore

PACK = {
    "basis_pack_id": "B-REPO", "region_code": "410100", "region_level": "CITY",
    "major": "BUILDING", "effective_date": "2026-01-01",
    "measurement_standard_version": "GB/T 50854-2024",
    "pricing_standard_version": "GB/T 50500-2024",
    "rule_version_scope": "HENAN_2025", "precision_rule_version": "P-V1",
    "status": "LOCKED", "test_only": False,
    "sources": [{"source_id": "S1", "title": "河南定额", "source_type": "QUOTA",
                 "license_status": "LICENSED", "verified": True}],
}

JOB_ID = "job-repo-001"
PACKAGE_ROOT = Path(txbuddy.__file__).resolve().parent


# --------------------------------------------------------------------- fixtures
@pytest.fixture()
def db_path(tmp_path) -> Path:
    return tmp_path / "repository.sqlite3"


@pytest.fixture()
def repos(db_path) -> SqliteRepositorySet:
    _database, repository_set = build_sqlite_repositories(str(db_path))
    return repository_set


def make_job(repos: RepositorySet, job_id: str = JOB_ID) -> dict:
    repos.jobs.create(
        job_id=job_id, project_id="P-REPO", name="Repository 用例", business_line="L1",
        basis_pack=PACK, status="CREATED", created_by="tester",
        created_at="2026-01-01T00:00:00+00:00", basis_pack_id=PACK["basis_pack_id"],
        updated_by="tester", updated_at="2026-01-01T00:00:00+00:00", revision=1,
        status_history=[{"to_status": "CREATED", "by": "tester",
                         "at": "2026-01-01T00:00:00+00:00", "reason": "JOB_CREATE"}],
    )
    return {"job_id": job_id}


# ------------------------------------------------------------ 1. 抽象接口契约
def test_all_repository_interfaces_are_abstract_and_cover_task_scope():
    """TASK 要求的 8 类仓储都在抽象层声明了方法；直接实例化必须失败。"""
    expected = {
        EvidenceRepository: {"save", "get", "count", "list_by_job"},
        CalcRunRepository: {"save", "get", "list_by_job"},
        AuditRepository: {"add", "list"},
        IdemRepository: {"lookup", "claim", "finish", "release", "store"},
        JobRepository: {"create", "get", "update_status"},
        DocumentRepository: {"add", "list_by_job"},
        TaskRepository: {"create", "get", "update", "list_by_job"},
        GateRepository: {"add", "list_by_job"},
        RecordRepository: {"put", "list_by_job"},
    }
    for interface, methods in expected.items():
        assert issubclass(interface, Repository)
        assert interface.__abstractmethods__ == frozenset(methods), interface.__name__
        with pytest.raises(TypeError):
            interface()  # type: ignore[abstract]


def test_repository_set_exposes_all_nine_repositories(repos):
    pairs = {
        "evidence": EvidenceRepository, "calc_run": CalcRunRepository,
        "audit": AuditRepository, "idem": IdemRepository, "jobs": JobRepository,
        "documents": DocumentRepository, "tasks": TaskRepository,
        "gates": GateRepository, "records": RecordRepository,
    }
    for attribute, interface in pairs.items():
        repo = getattr(repos, attribute)
        assert isinstance(repo, interface), attribute
        assert repo.backend == "sqlite"
    assert isinstance(repos, RepositorySet)
    assert repos.health() == {name: True for name in pairs}
    assert repos.backend == "sqlite"


def test_build_sqlite_repositories_creates_schema_file(db_path):
    database, repository_set = build_sqlite_repositories(str(db_path))
    assert database.path == db_path and db_path.exists()
    names = {row[0] for row in database.connect().execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"evidence", "calc_run", "audit_log", "idem", "job", "job_document",
            "job_task", "job_record", "gate_record"} <= names
    assert isinstance(repository_set, SqliteRepositorySet)


# ------------------------------------------------------------------ 2. Evidence
def test_evidence_save_get_roundtrip(repos):
    evidence = {"evidence_id": "ev-1", "sha256": "abc123",
                "job_id": JOB_ID, "rule_code": "R-1",
                "created_at": "2026-01-01T00:00:01+00:00"}
    repos.evidence.save(evidence)
    stored = repos.evidence.get("ev-1")
    assert stored == evidence


def test_evidence_get_missing_returns_none(repos):
    assert repos.evidence.get("nope") is None


def test_evidence_save_overwrites_same_id(repos):
    base = {"evidence_id": "ev-1", "sha256": "old", "job_id": JOB_ID,
            "created_at": "2026-01-01T00:00:01+00:00"}
    repos.evidence.save(base)
    repos.evidence.save({**base, "sha256": "new"})
    assert repos.evidence.get("ev-1")["sha256"] == "new"
    assert repos.evidence.count() == 1


def test_evidence_count_filters_by_job(repos):
    repos.evidence.save({"evidence_id": "ev-a", "sha256": "x", "job_id": "job-alpha",
                         "created_at": "2026-01-01T00:00:01+00:00"})
    repos.evidence.save({"evidence_id": "ev-b", "sha256": "y", "job_id": "job-beta",
                         "created_at": "2026-01-01T00:00:02+00:00"})
    assert repos.evidence.count() == 2
    assert repos.evidence.count("job-alpha") == 1
    assert repos.evidence.count("job-missing") == 0


def test_evidence_list_by_job_orders_and_isolates(repos):
    for eid, job, at in [("ev-2", "job-alpha", "2026-01-01T00:00:02+00:00"),
                         ("ev-1", "job-alpha", "2026-01-01T00:00:01+00:00"),
                         ("ev-3", "job-beta", "2026-01-01T00:00:03+00:00")]:
        repos.evidence.save({"evidence_id": eid, "sha256": eid.upper(), "job_id": job,
                             "created_at": at})
    rows = repos.evidence.list_by_job("job-alpha")
    assert [row["evidence_id"] for row in rows] == ["ev-1", "ev-2"]
    assert rows[0] == {"evidence_id": "ev-1", "sha256": "EV-1",
                       "created_at": "2026-01-01T00:00:01+00:00",
                       "payload": {"created_at": "2026-01-01T00:00:01+00:00",
                                   "evidence_id": "ev-1", "job_id": "job-alpha",
                                   "sha256": "EV-1"}}
    assert repos.evidence.list_by_job("job-unknown") == []


def test_evidence_list_skips_corrupted_payload(repos, db_path):
    """历史脏数据（非法 JSON）不得让整个列表接口炸掉。"""
    repos.evidence.save({"evidence_id": "ev-ok", "sha256": "ok", "job_id": "job-alpha",
                         "created_at": "2026-01-01T00:00:01+00:00"})
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO evidence VALUES(?,?,?,?)",
                 ("ev-bad", "bad", "{not-json", "2026-01-01T00:00:00+00:00"))
    conn.commit()
    conn.close()
    assert [row["evidence_id"] for row in repos.evidence.list_by_job("job-alpha")] == ["ev-ok"]


# ------------------------------------------------------------------ 3. CalcRun
def calc_run() -> dict:
    return {"calc_run_id": "run-1", "job_id": JOB_ID, "rule_code": "AREA_RECT",
            "rule_version": "1.0", "basis_pack_id": PACK["basis_pack_id"],
            "snapshot_sha256": "snap-sha", "snapshot": {"length": "3.0", "width": "4.0"},
            "result": "12.00", "unit": "m2", "evidence_id": "ev-1",
            "created_at": "2026-01-01T00:00:01+00:00"}


def test_calc_run_save_get_roundtrip(repos):
    repos.calc_run.save(calc_run())
    stored = repos.calc_run.get("run-1")
    assert stored["calc_run_id"] == "run-1"
    assert stored["snapshot"] == {"length": "3.0", "width": "4.0"}
    assert stored["result"] == "12.00" and stored["unit"] == "m2"
    assert stored["evidence_id"] == "ev-1" and stored["job_id"] == JOB_ID


def test_calc_run_get_missing_returns_none(repos):
    assert repos.calc_run.get("run-missing") is None


def test_calc_run_list_by_job_returns_summary(repos):
    repos.calc_run.save(calc_run())
    repos.calc_run.save({**calc_run(), "calc_run_id": "run-2",
                         "created_at": "2026-01-01T00:00:02+00:00",
                         "rule_code": "VOL_RECT"})
    repos.calc_run.save({**calc_run(), "calc_run_id": "run-other", "job_id": "job-beta"})
    rows = repos.calc_run.list_by_job(JOB_ID)
    assert [row["calc_run_id"] for row in rows] == ["run-1", "run-2"]
    assert rows[0] == {"calc_run_id": "run-1", "rule_code": "AREA_RECT",
                       "result": "12.00", "unit": "m2", "snapshot_sha256": "snap-sha"}
    assert "snapshot" not in rows[0]


# -------------------------------------------------------------------- 4. Audit
def test_audit_add_and_list_desc_with_limit(repos):
    for index in range(3):
        repos.audit.add(f"actor-{index}", "JOB_CREATE", "JOB", f"job-{index}",
                        detail=f"d{index}", trace_id=f"t{index}")
    rows = repos.audit.list()
    assert [row["actor"] for row in rows] == ["actor-2", "actor-1", "actor-0"]
    assert rows[0] == {"actor": "actor-2", "action": "JOB_CREATE", "object_type": "JOB",
                       "object_id": "job-2", "detail": "d2", "trace_id": "t2",
                       "at": rows[0]["at"]}
    assert datetime.fromisoformat(rows[0]["at"]).tzinfo is not None
    assert len(repos.audit.list(limit=2)) == 2


# --------------------------------------------------------------------- 5. Idem
def test_idem_scope_key_format_and_store_alias(repos):
    assert build_idem_scope_key("t", "a", "post", "/v1/x", "k") == "t|a|POST|/v1/x|k"
    assert IdemRepository.scope_key("", "", "get", "/p", "k") == "-|-|GET|/p|k"
    assert repos.idem.scope_key("t", "a", "post", "/v1/x", "k") == "t|a|POST|/v1/x|k"


def test_idem_lookup_miss(repos):
    assert repos.idem.lookup("scope-miss", "fp") == ("miss", None)


def test_idem_claim_wins_once_then_reports_in_progress(repos):
    assert repos.idem.claim("scope-1", "fp-1", "t", "a") == ("claimed", None)
    # 同键同指纹的第二次请求拿不到执行权
    assert repos.idem.claim("scope-1", "fp-1", "t", "a") == ("in_progress", None)
    # 认领中的行不算 hit
    assert repos.idem.lookup("scope-1", "fp-1") == ("in_progress", None)


def test_idem_conflict_on_different_fingerprint(repos):
    repos.idem.claim("scope-1", "fp-1")
    assert repos.idem.claim("scope-1", "fp-2") == ("conflict", None)
    assert repos.idem.lookup("scope-1", "fp-2") == ("conflict", None)


def test_idem_finish_requires_claim_and_then_hits_with_replay(repos):
    assert repos.idem.finish("scope-never-claimed", {"ok": True}) is False
    repos.idem.claim("scope-1", "fp-1")
    assert repos.idem.finish("scope-1", {"result": 42}) is True
    state, cached = repos.idem.claim("scope-1", "fp-1")
    assert state == "hit" and cached == {"result": 42, "idem_replay": True}
    assert repos.idem.lookup("scope-1", "fp-1") == ("hit", {"result": 42})


def test_idem_release_allows_retry(repos):
    repos.idem.claim("scope-1", "fp-1")
    assert repos.idem.release("scope-1") is True
    assert repos.idem.release("scope-1") is False  # 已释放，不重复计数
    assert repos.idem.lookup("scope-1", "fp-1") == ("miss", None)
    assert repos.idem.claim("scope-1", "fp-1") == ("claimed", None)


def test_idem_stale_claim_is_taken_over(repos, db_path):
    repos.idem.claim("scope-1", "fp-1")
    stale = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE idem SET claimed_at=? WHERE scope_key=?", (stale, "scope-1"))
    conn.commit()
    conn.close()
    assert repos.idem.claim("scope-1", "fp-1", ttl_seconds=300) == ("claimed", None)
    # 未过期的认领不可被接管
    assert repos.idem.claim("scope-1", "fp-1", ttl_seconds=300) == ("in_progress", None)


def test_idem_store_compat_is_claim_plus_finish(repos):
    assert repos.idem.store("scope-1", "fp-1", {"v": 1}) == {"v": 1}
    assert repos.idem.lookup("scope-1", "fp-1") == ("hit", {"v": 1})
    assert repos.idem.store("scope-1", "fp-1", {"v": 2}) == {"v": 1}


# ---------------------------------------------------------------------- 6. Job
def test_job_create_and_get_roundtrip(repos):
    make_job(repos)
    row = repos.jobs.get(JOB_ID)
    assert row["job_id"] == JOB_ID and row["status"] == "CREATED"
    assert row["business_line"] == "L1" and row["revision"] == 1
    assert row["basis_pack"]["basis_pack_id"] == "B-REPO"
    assert row["basis_pack_id"] == "B-REPO"
    assert [event["reason"] for event in row["status_history"]] == ["JOB_CREATE"]


def test_job_get_missing_returns_none(repos):
    assert repos.jobs.get("job-missing") is None


def test_job_update_status_cas_success_and_conflict(repos):
    make_job(repos)
    history = [{"to_status": "INTAKE", "by": "pm", "at": now_iso()}]
    assert repos.jobs.update_status(JOB_ID, status="INTAKE", expected_status="CREATED",
                                    by="pm", at=now_iso(), status_history=history) is True
    assert repos.jobs.get(JOB_ID)["status"] == "INTAKE"
    assert repos.jobs.get(JOB_ID)["revision"] == 2
    # 期望状态已过期 → CAS 未命中，库里状态不变
    assert repos.jobs.update_status(JOB_ID, status="REVIEW", expected_status="CREATED",
                                    by="pm", at=now_iso(), status_history=history) is False
    assert repos.jobs.get(JOB_ID)["status"] == "INTAKE"
    assert repos.jobs.update_status("job-missing", status="INTAKE", expected_status="CREATED",
                                    by="pm", at=now_iso(), status_history=[]) is False


# ----------------------------------------------------------------- 7. Document
def test_document_add_and_list_by_job(repos):
    make_job(repos)
    repos.documents.add(JOB_ID, "BOQ_XLSX", "v1", "sha-1", False, "2026-01-01T00:00:01+00:00")
    repos.documents.add(JOB_ID, "DRAWING", "v2", "sha-2", True, "2026-01-01T00:00:02+00:00")
    repos.documents.add("job-other", "BOQ_XLSX", "v9", "sha-9", True,
                        "2026-01-01T00:00:03+00:00")
    rows = repos.documents.list_by_job(JOB_ID)
    assert [row["doc_key"] for row in rows] == ["BOQ_XLSX", "DRAWING"]
    assert rows[0] == {"doc_key": "BOQ_XLSX", "version": "v1", "file_sha256": "sha-1",
                       "verified": 0, "added_at": "2026-01-01T00:00:01+00:00"}
    assert rows[1]["verified"] == 1
    assert repos.documents.list_by_job("job-unknown") == []


# --------------------------------------------------------------------- 8. Task
def test_task_create_and_get_roundtrip(repos):
    make_job(repos)
    repos.tasks.create(task_id="task-1", job_id=JOB_ID, node="INTAKE",
                       status="READY", payload={"doc": "BOQ"}, assignee="alice",
                       history=[{"to": "READY", "at": "2026-01-01T00:00:01+00:00",
                                 "by": "alice"}],
                       created_at="2026-01-01T00:00:01+00:00",
                       updated_at="2026-01-01T00:00:01+00:00")
    task = repos.tasks.get("task-1")
    assert task["status"] == "READY" and task["payload"] == {"doc": "BOQ"}
    assert task["assignee"] == "alice" and task["result"] is None
    assert task["approver"] is None and task["approved_at"] is None
    assert [event["to"] for event in task["history"]] == ["READY"]


def test_task_get_missing_returns_none(repos):
    assert repos.tasks.get("task-missing") is None


def test_task_update_sets_result_and_history(repos):
    make_job(repos)
    repos.tasks.create(task_id="task-1", job_id=JOB_ID, node="INTAKE", status="READY",
                       payload={}, assignee="alice", history=[],
                       created_at="2026-01-01T00:00:01+00:00",
                       updated_at="2026-01-01T00:00:01+00:00")
    history = [{"from": "READY", "to": "DONE", "by": "alice",
                "at": "2026-01-01T00:00:02+00:00"}]
    assert repos.tasks.update("task-1", status="DONE", result={"rows": 3},
                              approver="bob", approved_at="2026-01-01T00:00:02+00:00",
                              history=history,
                              updated_at="2026-01-01T00:00:02+00:00") is True
    task = repos.tasks.get("task-1")
    assert task["status"] == "DONE" and task["result"] == {"rows": 3}
    assert task["approver"] == "bob" and task["approved_at"] == "2026-01-01T00:00:02+00:00"
    assert task["history"] == history
    # None 表示保留原值（COALESCE 语义）
    assert repos.tasks.update("task-1", status="DONE", result=None, approver=None,
                              approved_at=None, history=history,
                              updated_at="2026-01-01T00:00:03+00:00") is True
    kept = repos.tasks.get("task-1")
    assert kept["result"] == {"rows": 3} and kept["approver"] == "bob"
    assert repos.tasks.update("task-missing", status="DONE", result=None, approver=None,
                              approved_at=None, history=[],
                              updated_at="2026-01-01T00:00:03+00:00") is False


def test_task_list_by_job(repos):
    make_job(repos)
    for task_id, at in [("task-1", "2026-01-01T00:00:01+00:00"),
                        ("task-2", "2026-01-01T00:00:02+00:00")]:
        repos.tasks.create(task_id=task_id, job_id=JOB_ID, node="INTAKE", status="READY",
                           payload={}, assignee="alice", history=[], created_at=at,
                           updated_at=at)
    repos.tasks.create(task_id="task-other", job_id="job-other", node="INTAKE",
                       status="READY", payload={}, assignee="alice", history=[],
                       created_at="2026-01-01T00:00:03+00:00",
                       updated_at="2026-01-01T00:00:03+00:00")
    assert [t["task_id"] for t in repos.tasks.list_by_job(JOB_ID)] == ["task-1", "task-2"]
    assert repos.tasks.list_by_job("job-unknown") == []


# --------------------------------------------------------------------- 9. Gate
def test_gate_add_and_list_by_job(repos):
    make_job(repos)
    repos.gates.add(JOB_ID, "G1_INTAKE", "PASS", [], "judge-1",
                    "2026-01-01T00:00:01+00:00")
    repos.gates.add(JOB_ID, "G2_BASIS", "FAIL",
                    [{"rule": "BASIS_REQUIRED", "detail": "缺 basis_pack"}], "judge-2",
                    "2026-01-01T00:00:02+00:00")
    rows = repos.gates.list_by_job(JOB_ID)
    assert [row["gate_code"] for row in rows] == ["G1_INTAKE", "G2_BASIS"]
    assert rows[0]["failed_rules"] == [] and rows[0]["result"] == "PASS"
    assert rows[1]["failed_rules"] == [{"rule": "BASIS_REQUIRED", "detail": "缺 basis_pack"}]
    assert rows[1]["judged_by"] == "judge-2" and rows[1]["judged_at"] == "2026-01-01T00:00:02+00:00"
    assert repos.gates.list_by_job("job-unknown") == []


# ------------------------------------------------------------------- 10. Record
def test_record_put_and_list_by_job_and_kind(repos):
    make_job(repos)
    repos.records.put(JOB_ID, "BOQ", {"item": "A"}, "2026-01-01T00:00:01+00:00")
    repos.records.put(JOB_ID, "BOQ", {"item": "B"}, "2026-01-01T00:00:02+00:00")
    repos.records.put(JOB_ID, "PRICE", {"item": "C"}, "2026-01-01T00:00:03+00:00")
    assert repos.records.list_by_job(JOB_ID, "BOQ") == [{"item": "A"}, {"item": "B"}]
    assert repos.records.list_by_job(JOB_ID, "PRICE") == [{"item": "C"}]
    assert repos.records.list_by_job(JOB_ID, "MISSING") == []
    assert repos.records.list_by_job("job-unknown", "BOQ") == []


# --------------------------------------------------------- 11. store.py 委托关系
def test_store_uses_repository_set_and_shares_same_database(tmp_path):
    store = EvidenceStore(str(tmp_path / "store.sqlite3"))
    assert isinstance(store.repositories, SqliteRepositorySet)
    assert store.repositories is store.repositories  # 单实例，多次访问同一对象
    assert store.schema_version == "sqlite"
    assert store.path.exists()


def test_store_delegates_every_repository_family(tmp_path):
    """业务门面写的每一类数据，仓储都能直接读到（证明委托真实发生，不是重写一遍）。"""
    path = tmp_path / "delegation.sqlite3"
    store = EvidenceStore(str(path))
    repos = store.repositories

    store.create_job(JOB_ID, "P-REPO", "委托用例", "L1", PACK, "tester")
    assert repos.jobs.get(JOB_ID)["name"] == "委托用例"

    store.add_document(JOB_ID, "BOQ_XLSX", "v1", "sha-1")
    assert [d["doc_key"] for d in repos.documents.list_by_job(JOB_ID)] == ["BOQ_XLSX"]

    store.create_task("task-1", JOB_ID, "INTAKE", {"doc": "BOQ"}, "alice")
    assert repos.tasks.get("task-1")["node"] == "INTAKE"

    store.put_record(JOB_ID, "BOQ", {"item": "A"})
    assert repos.records.list_by_job(JOB_ID, "BOQ") == [{"item": "A"}]

    store.add_gate_record(JOB_ID, "G1_INTAKE", "PASS", [], "judge")
    assert repos.gates.list_by_job(JOB_ID)[0]["gate_code"] == "G1_INTAKE"

    store.add_audit("tester", "JOB_CREATE", "JOB", JOB_ID)
    assert repos.audit.list(limit=1)[0]["action"] == "JOB_CREATE"

    store.save({"evidence_id": "ev-1", "sha256": "sha", "job_id": JOB_ID,
                "created_at": now_iso()})
    assert repos.evidence.get("ev-1")["job_id"] == JOB_ID

    store.save_calc_run({"calc_run_id": "run-1", "job_id": JOB_ID, "rule_code": "R",
                         "rule_version": "1", "basis_pack_id": "B-REPO",
                         "snapshot_sha256": "sha", "snapshot": {"a": "1"},
                         "result": "1", "unit": "m", "evidence_id": "ev-1",
                         "created_at": now_iso()})
    assert repos.calc_run.get("run-1")["snapshot"] == {"a": "1"}

    scope = store.idem_scope("t", "a", "post", "/v1/x", "k")
    assert store.idem_claim(scope, "fp") == ("claimed", None)
    assert repos.idem.lookup(scope, "fp") == ("in_progress", None)
    assert store.idem_finish(scope, {"ok": True}) is True
    assert repos.idem.lookup(scope, "fp") == ("hit", {"ok": True})
    assert store.idem_release(store.idem_scope("t", "a", "post", "/v1/x", "k2")) is False


SQL_STATEMENT = re.compile(
    r"\b(?:SELECT\s+.+\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|"
    r"CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE|PRAGMA\s+\w+)\b",
    re.IGNORECASE)


def test_store_module_has_no_embedded_sql():
    """P1-02 的核心指标：store.py 里不再有散落的 SQL 语句。"""
    source = (PACKAGE_ROOT / "store.py").read_text(encoding="utf-8")
    assert "import sqlite3" not in source
    assert SQL_STATEMENT.findall(source) == []
    # 委托必须真的指向仓储层
    assert "SqliteRepositorySet" in source and "repositories." in source


def test_repository_layer_owns_all_sql():
    root = PACKAGE_ROOT / "repository"
    sql_files = {path.name for path in root.glob("*.py")
                 if SQL_STATEMENT.search(path.read_text(encoding="utf-8"))}
    # P1-03 新增 PostgreSQL 实现 pg.py；P1-04 新增版本化迁移执行器 migrations.py
    # （账本 DDL / 查询 SQL）。SQL 仍全部收敛在 repository 层，store.py 依旧零 SQL。
    assert sql_files == {"schema.py", "sqlite.py", "pg.py", "migrations.py"}


# ------------------------------------------------- 12. 旧库迁移（schema 层，P1-02 迁移过来）
def test_legacy_idem_table_migrates_to_state_machine_schema(db_path):
    """旧 idem 只有 (scope_key,fingerprint,response,...)：已完成记录搬迁为 DONE 且可命中。"""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE idem(scope_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                          response TEXT, tenant TEXT, actor TEXT, at TEXT);
        INSERT INTO idem VALUES('t|a|POST|/v1/x|k','fp-legacy','{"v":7}','t','a',
                                '2026-01-01T00:00:00+00:00');
    """)
    conn.commit()
    conn.close()

    database, repos = build_sqlite_repositories(str(db_path))
    assert {"scope_key", "fingerprint", "state", "claimed_at", "response"} <= database.columns("idem")
    assert repos.idem.lookup("t|a|POST|/v1/x|k", "fp-legacy") == ("hit", {"v": 7})
    assert repos.idem.claim("t|a|POST|/v1/x|k", "fp-other") == ("conflict", None)


def test_legacy_idem_without_fingerprint_is_rebuilt(db_path):
    """缺 fingerprint 的远古表结构不安全，直接重建（不猜测旧语义）。"""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE idem(scope_key TEXT PRIMARY KEY, response TEXT, at TEXT);
        INSERT INTO idem VALUES('old|scope','{"v":1}','2026-01-01T00:00:00+00:00');
    """)
    conn.commit()
    conn.close()

    database, repos = build_sqlite_repositories(str(db_path))
    assert "fingerprint" in database.columns("idem")
    assert repos.idem.lookup("old|scope", "fp") == ("miss", None)
    assert repos.idem.claim("old|scope", "fp") == ("claimed", None)


def test_legacy_calc_run_column_order_is_rebuilt_not_copied_positionally(db_path):
    """旧 calc_run 列序不同（result 在 snapshot 前）：必须重建，避免位置式写入错列。"""
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE calc_run(
            calc_run_id TEXT PRIMARY KEY, job_id TEXT, rule_code TEXT, rule_version TEXT,
            basis_pack_id TEXT, snapshot_sha256 TEXT, result TEXT, unit TEXT,
            snapshot TEXT, evidence_id TEXT, created_at TEXT);
        INSERT INTO calc_run(calc_run_id,job_id,rule_code,rule_version,basis_pack_id,
                             snapshot_sha256,result,unit,snapshot,evidence_id,created_at)
        VALUES('run-legacy','job-1','R','1','B-1','sha','12.00','m2','{"a":"1"}','ev-1',
               '2026-01-01T00:00:00+00:00');
    """)
    conn.commit()
    conn.close()

    database, repos = build_sqlite_repositories(str(db_path))
    assert database.columns("calc_run") == set(
        ["calc_run_id", "job_id", "rule_code", "rule_version", "basis_pack_id",
         "snapshot_sha256", "snapshot", "result", "unit", "evidence_id", "created_at"])
    row = repos.calc_run.get("run-legacy")
    assert row["result"] == "12.00" and row["unit"] == "m2"
    assert row["snapshot"] == {"a": "1"}  # 列没被写错位
    assert row["evidence_id"] == "ev-1"
