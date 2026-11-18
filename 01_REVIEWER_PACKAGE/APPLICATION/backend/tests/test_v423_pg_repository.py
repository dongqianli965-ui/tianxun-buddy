"""P1-03-PG-REPO：PostgreSQL 仓储对真实实例的逐方法实测（禁止 skip）。

与 ``test_v423_sqlite_repository.py`` 同场景、同断言粒度，直接连 ``TXBUDDY_PG_DSN``：

1. 真实连通：psycopg2 打开连接、``PgDatabase.health/server_info``、9 个仓储探活；
2. 迁移：``initialize()`` 实际执行 001/002（core schema）与 003（runtime schema），
   迁移账本记录 sha256，重复调用幂等；缺表/缺脚本必须抛 ``PgMigrationError``；
3. 九类仓储：Evidence / CalcRun / Audit / Idem / Job / Document / Task / Gate / Record
   的 save/get/list/count、None 与空集合、覆盖写、CAS 失败等边界；
4. 幂等：数据库唯一约束裁定的认领、同键异指纹冲突、finish/release、陈旧认领 CAS 接管，
   以及多线程并发认领只有 1 个赢家；
5. JSONB 往返：嵌套对象/数组/中文/布尔/数字/null 原样取回，列类型确为 jsonb；
6. 与 SQLite 实现做同一场景对比（逐字段相等）。

环境缺失（没有 DSN / 没有驱动）时直接 ``pytest.fail``，绝不 skip 混过验收。

运行：cd backend; .venv/Scripts/python.exe -m pytest tests/test_v423_pg_repository.py -q
"""
from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

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
    TaskRepository,
    build_sqlite_repositories,
)
from txbuddy.repository.base import build_idem_scope_key
from txbuddy.repository.pg import (
    CORE_SCRIPTS,
    CORE_TABLES,
    DSN_ENV,
    PgConfigurationError,
    PgConnectionError,
    PgDatabase,
    PgMigrationError,
    PgRepositorySet,
    RUNTIME_SCRIPTS,
    RUNTIME_TABLES,
    build_pg_repositories,
)
from txbuddy.repository.schema import now_iso

PACK = {
    "basis_pack_id": "B-PG", "region_code": "410100", "region_level": "CITY",
    "major": "BUILDING", "effective_date": "2026-01-01",
    "measurement_standard_version": "GB/T 50854-2024",
    "pricing_standard_version": "GB/T 50500-2024",
    "rule_version_scope": "HENAN_2025", "precision_rule_version": "P-V1",
    "status": "LOCKED", "test_only": False,
    "sources": [{"source_id": "S1", "title": "河南定额", "source_type": "QUOTA",
                 "license_status": "LICENSED", "verified": True}],
}

JOB_ID = "job-pg-001"
RUNTIME_SCHEMA = "txbuddy_pytest_runtime"
CORE_SCHEMA = "txbuddy_pytest_core"
SQL_DIR = Path(__file__).resolve().parents[2] / "sql"

PG_TABLES = ("evidence", "calc_run", "audit_log", "idem", "job", "job_document",
             "job_task", "job_record", "gate_record")


# --------------------------------------------------------------------- helpers
def _require_dsn() -> str:
    dsn = (os.environ.get(DSN_ENV) or os.environ.get("TX_PG_DSN") or "").strip()
    if not dsn:
        pytest.fail(
            f"未设置 {DSN_ENV}：本任务要求连真实 PostgreSQL 执行，不允许 skip。"
            "请先导出 TXBUDDY_PG_DSN=postgresql://user@host:port/db 再重跑。")
    return dsn


def _psycopg2():
    try:
        import psycopg2  # noqa: F401
        import psycopg2.extensions  # noqa: F401
    except ImportError as exc:  # pragma: no cover - 环境缺驱动时明确失败
        pytest.fail(f"未安装 psycopg2，无法连真实 PostgreSQL：{exc}")
    return psycopg2


def _admin_execute(dsn: str, sql: str, params: tuple = ()) -> None:
    psycopg2 = _psycopg2()
    conn = psycopg2.connect(dsn, connect_timeout=10)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql, params or None)
    finally:
        conn.close()


def _drop_test_schemas(dsn: str) -> None:
    for schema in (RUNTIME_SCHEMA, CORE_SCHEMA):
        _admin_execute(dsn, f"DROP SCHEMA IF EXISTS {schema} CASCADE")


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    return _require_dsn()


@pytest.fixture(scope="session")
def pg_database(pg_dsn) -> PgDatabase:
    """真实实例上建独立测试 schema，结束后整体 DROP，不在 txbuddy 里留脏数据。"""
    _drop_test_schemas(pg_dsn)
    database, _repos = build_pg_repositories(
        pg_dsn, runtime_schema=RUNTIME_SCHEMA, core_schema=CORE_SCHEMA)
    try:
        yield database
    finally:
        _drop_test_schemas(pg_dsn)


@pytest.fixture()
def database(pg_database: PgDatabase) -> PgDatabase:
    _truncate_runtime(pg_database)
    return pg_database


@pytest.fixture()
def repos(pg_database: PgDatabase) -> PgRepositorySet:
    _truncate_runtime(pg_database)
    return PgRepositorySet(pg_database)


def _truncate_runtime(database: PgDatabase) -> None:
    conn = database.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE " + ",".join(PG_TABLES) + " RESTART IDENTITY")
    finally:
        conn.close()


def make_job(repos: RepositorySet, job_id: str = JOB_ID) -> dict:
    repos.jobs.create(
        job_id=job_id, project_id="P-PG", name="PG 仓储用例", business_line="L1",
        basis_pack=PACK, status="CREATED", created_by="tester",
        created_at="2026-01-01T00:00:00+00:00", basis_pack_id=PACK["basis_pack_id"],
        updated_by="tester", updated_at="2026-01-01T00:00:00+00:00", revision=1,
        status_history=[{"to_status": "CREATED", "by": "tester",
                         "at": "2026-01-01T00:00:00+00:00", "reason": "JOB_CREATE"}],
    )
    return {"job_id": job_id}


def calc_run(calc_run_id: str = "run-1") -> dict:
    return {"calc_run_id": calc_run_id, "job_id": JOB_ID, "rule_code": "AREA_RECT",
            "rule_version": "1.0", "basis_pack_id": PACK["basis_pack_id"],
            "snapshot_sha256": "snap-sha", "snapshot": {"length": "3.0", "width": "4.0"},
            "result": "12.00", "unit": "m2", "evidence_id": "ev-1",
            "created_at": "2026-01-01T00:00:01+00:00"}


# ------------------------------------------------------- 1. 真实实例与迁移生命周期
def test_dsn_comes_from_environment_and_is_not_empty(pg_dsn):
    assert pg_dsn.startswith("postgres://") or pg_dsn.startswith("postgresql://") \
        or "host=" in pg_dsn, pg_dsn


def test_real_connection_and_server_info(pg_dsn):
    database = PgDatabase(pg_dsn, runtime_schema=RUNTIME_SCHEMA, core_schema=CORE_SCHEMA)
    assert database.health() is True
    info = database.server_info()
    assert info["version"].startswith("PostgreSQL "), info["version"]
    assert info["timezone"] == "UTC"
    assert info["runtime_schema"] == RUNTIME_SCHEMA
    assert info["core_schema"] == CORE_SCHEMA
    parts = urlsplit(pg_dsn) if "://" in pg_dsn else None
    if parts is not None:
        if parts.path.strip("/"):
            assert info["database"] == parts.path.strip("/").split("/")[0]
        if parts.username:
            assert info["user"] == parts.username
        if parts.port:
            assert int(info["port"]) == parts.port
    assert isinstance(info["port"], int)
    # repr 不得泄露 DSN（可能带密码）
    assert pg_dsn not in repr(database)


def test_health_and_connect_fail_cleanly_on_unreachable_instance():
    broken = PgDatabase("postgresql://postgres@127.0.0.1:1/txbuddy")
    assert broken.health() is False
    with pytest.raises(PgConnectionError):
        broken.connect()


def test_missing_dsn_is_configuration_error(monkeypatch):
    for name in (DSN_ENV, "TX_PG_DSN", "PGHOST", "PGDATABASE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(PgConfigurationError):
        PgDatabase()


def test_initialize_records_all_three_scripts_with_real_sha256(database):
    runtime_log = database.migration_log()
    core_log = database.migration_log(CORE_SCHEMA)
    assert [row["name"] for row in runtime_log] == list(RUNTIME_SCRIPTS)
    assert [row["name"] for row in core_log] == list(CORE_SCRIPTS)
    for row in runtime_log + core_log:
        expected = hashlib.sha256((database.sql_dir / row["name"]).read_text(
            encoding="utf-8").encode("utf-8")).hexdigest()
        assert row["sha256"] == expected, row["name"]
        assert len(row["sha256"]) == 64
        applied = datetime.fromisoformat(row["applied_at"])
        assert applied.tzinfo is not None


def test_initialize_is_idempotent_on_second_call(database):
    before_runtime = database.migration_log()
    before_core = database.migration_log(CORE_SCHEMA)
    assert database.initialize() == []
    assert database.migration_log() == before_runtime
    assert database.migration_log(CORE_SCHEMA) == before_core


def test_verify_schema_sees_runtime_and_core_tables(database):
    database.verify_schema()
    runtime_tables = database.tables()
    core_tables = database.tables(CORE_SCHEMA)
    assert set(RUNTIME_TABLES) <= runtime_tables
    assert set(CORE_TABLES) <= core_tables
    assert "schema_migration" in runtime_tables
    assert "schema_migration" in core_tables


def test_verify_schema_raises_on_missing_tables(pg_dsn):
    empty_runtime = "txbuddy_pytest_missing_runtime"
    empty_core = "txbuddy_pytest_missing_core"
    for schema in (empty_runtime, empty_core):
        _admin_execute(pg_dsn, f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    database = PgDatabase(pg_dsn, runtime_schema=empty_runtime, core_schema=empty_core)
    with pytest.raises(PgMigrationError) as excinfo:
        database.verify_schema()
    assert "缺表" in str(excinfo.value)
    assert "evidence" in str(excinfo.value)


def test_initialize_raises_when_sql_scripts_are_missing(pg_dsn, tmp_path):
    runtime = "txbuddy_pytest_bad_runtime"
    core = "txbuddy_pytest_bad_core"
    for schema in (runtime, core):
        _admin_execute(pg_dsn, f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    database = PgDatabase(pg_dsn, runtime_schema=runtime, core_schema=core,
                          sql_dir=tmp_path / "no-sql")
    try:
        with pytest.raises(PgMigrationError) as excinfo:
            database.initialize()
        assert "迁移脚本不存在" in str(excinfo.value)
    finally:
        for schema in (runtime, core):
            _admin_execute(pg_dsn, f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def test_repository_set_exposes_nine_postgres_repositories(repos):
    pairs = {
        "evidence": EvidenceRepository, "calc_run": CalcRunRepository,
        "audit": AuditRepository, "idem": IdemRepository, "jobs": JobRepository,
        "documents": DocumentRepository, "tasks": TaskRepository,
        "gates": GateRepository, "records": RecordRepository,
    }
    assert isinstance(repos, PgRepositorySet)
    assert isinstance(repos, RepositorySet)
    assert repos.backend == "postgresql"
    for attribute, interface in pairs.items():
        repo = getattr(repos, attribute)
        assert isinstance(repo, interface), attribute
        assert repo.backend == "postgresql"
    assert repos.health() == {name: True for name in pairs}


def test_runtime_columns_use_jsonb_and_timestamptz(database):
    conn = database.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name, data_type FROM information_schema.columns "
                "WHERE table_schema=%s", (RUNTIME_SCHEMA,))
            types = {(table, column): data_type for table, column, data_type in cur.fetchall()}
    finally:
        conn.close()
    jsonb_columns = {
        ("evidence", "payload"), ("calc_run", "snapshot"), ("job", "basis_pack"),
        ("job", "status_history"), ("job_task", "payload"), ("job_task", "result"),
        ("job_task", "history"), ("job_record", "payload"), ("gate_record", "failed_rules"),
        ("idem", "response"),
    }
    for key in jsonb_columns:
        assert types[key] == "jsonb", key
    for key in [("evidence", "created_at"), ("job", "updated_at"), ("audit_log", "at"),
                ("job_task", "approved_at"), ("idem", "claimed_at")]:
        assert types[key] == "timestamp with time zone", key
    # 与 SQLite 不同：结果与单位是文本（精度由业务层 NUMERIC 字符串承载）
    assert types[("calc_run", "result")] == "text"
    assert types[("job_document", "verified")] == "boolean"


# ------------------------------------------------------------------ 2. Evidence
def test_evidence_save_get_roundtrip(repos):
    evidence = {"evidence_id": "ev-1", "sha256": "abc123",
                "job_id": JOB_ID, "rule_code": "R-1",
                "created_at": "2026-01-01T00:00:01+00:00"}
    repos.evidence.save(evidence)
    assert repos.evidence.get("ev-1") == evidence


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


# ------------------------------------------------------------- 3. JSONB 往返
def test_jsonb_nested_payload_roundtrips_exactly(repos):
    payload = {
        "evidence_id": "ev-jsonb", "sha256": "sha-jsonb", "job_id": JOB_ID,
        "created_at": "2026-01-01T00:00:01+00:00",
        "text": "河南郑州·混凝土 C30", "number": 12.5, "integer": 7,
        "flag": True, "disabled": False, "nothing": None,
        "nested": {"b": [1, 2, 3], "a": {"deep": {"deeper": "值"}}},
    }
    repos.evidence.save(payload)
    stored = repos.evidence.get("ev-jsonb")
    assert stored == payload
    assert stored["nested"]["a"]["deep"]["deeper"] == "值"
    assert stored["nothing"] is None


def test_jsonb_calc_run_snapshot_and_task_result_roundtrip(repos):
    make_job(repos)
    snapshot = {"items": [{"code": "A", "qty": "3.0"}], "meta": {"source": "图纸", "ok": True}}
    run = {**calc_run("run-jsonb"), "snapshot": snapshot}
    repos.calc_run.save(run)
    assert repos.calc_run.get("run-jsonb")["snapshot"] == snapshot

    repos.tasks.create(task_id="task-jsonb", job_id=JOB_ID, node="INTAKE", status="READY",
                       payload={"doc": {"name": "清单.xlsx", "pages": 12}},
                       assignee="alice", history=[{"to": "READY", "n": 1}],
                       created_at="2026-01-01T00:00:01+00:00",
                       updated_at="2026-01-01T00:00:01+00:00")
    assert repos.tasks.update("task-jsonb", status="DONE",
                              result={"rows": [{"id": 1, "ok": True}], "total": 3.5},
                              approver="bob", approved_at="2026-01-01T00:00:02+00:00",
                              history=[{"to": "DONE"}],
                              updated_at="2026-01-01T00:00:02+00:00") is True
    task = repos.tasks.get("task-jsonb")
    assert task["result"] == {"rows": [{"id": 1, "ok": True}], "total": 3.5}
    assert task["payload"] == {"doc": {"name": "清单.xlsx", "pages": 12}}


# ------------------------------------------------------------------ 4. CalcRun
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
    repos.calc_run.save({**calc_run("run-2"), "created_at": "2026-01-01T00:00:02+00:00",
                         "rule_code": "VOL_RECT"})
    repos.calc_run.save({**calc_run("run-other"), "job_id": "job-beta"})
    rows = repos.calc_run.list_by_job(JOB_ID)
    assert [row["calc_run_id"] for row in rows] == ["run-1", "run-2"]
    assert rows[0] == {"calc_run_id": "run-1", "rule_code": "AREA_RECT",
                       "result": "12.00", "unit": "m2", "snapshot_sha256": "snap-sha"}
    assert "snapshot" not in rows[0]
    assert repos.calc_run.list_by_job("job-unknown") == []


# -------------------------------------------------------------------- 5. Audit
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
    assert [row["actor"] for row in repos.audit.list(limit=0)] == []


# --------------------------------------------------------------------- 6. Idem
def test_idem_scope_key_format_and_store_alias(repos):
    assert build_idem_scope_key("t", "a", "post", "/v1/x", "k") == "t|a|POST|/v1/x|k"
    assert IdemRepository.scope_key("", "", "get", "/p", "k") == "-|-|GET|/p|k"
    assert repos.idem.scope_key("t", "a", "post", "/v1/x", "k") == "t|a|POST|/v1/x|k"


def test_idem_lookup_miss(repos):
    assert repos.idem.lookup("scope-miss", "fp") == ("miss", None)


def test_idem_claim_wins_once_then_reports_in_progress(repos):
    assert repos.idem.claim("scope-1", "fp-1", "t", "a") == ("claimed", None)
    assert repos.idem.claim("scope-1", "fp-1", "t", "a") == ("in_progress", None)
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
    assert repos.idem.release("scope-1") is False
    assert repos.idem.lookup("scope-1", "fp-1") == ("miss", None)
    assert repos.idem.claim("scope-1", "fp-1") == ("claimed", None)


def test_idem_stale_claim_is_taken_over_by_cas(repos, database):
    repos.idem.claim("scope-1", "fp-1")
    stale = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    conn = database.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE idem SET claimed_at=%s::timestamptz WHERE scope_key=%s",
                        (stale, "scope-1"))
    finally:
        conn.close()
    assert repos.idem.claim("scope-1", "fp-1", ttl_seconds=300) == ("claimed", None)
    assert repos.idem.claim("scope-1", "fp-1", ttl_seconds=300) == ("in_progress", None)


def test_idem_store_compat_is_claim_plus_finish(repos):
    assert repos.idem.store("scope-1", "fp-1", {"v": 1}) == {"v": 1}
    assert repos.idem.lookup("scope-1", "fp-1") == ("hit", {"v": 1})
    assert repos.idem.store("scope-1", "fp-1", {"v": 2}) == {"v": 1}


def test_idem_concurrent_claims_have_exactly_one_winner(pg_database):
    _truncate_runtime(pg_database)
    repos = PgRepositorySet(pg_database)

    def claim(_index: int):
        return repos.idem.claim("scope-race", "fp-race", "t", "a")

    with ThreadPoolExecutor(max_workers=12) as pool:
        first_round = list(pool.map(claim, range(12)))
    states = [state for state, _cached in first_round]
    assert states.count("claimed") == 1, states
    assert states.count("in_progress") == 11, states
    assert repos.idem.finish("scope-race", {"winner": True}) is True

    with ThreadPoolExecutor(max_workers=12) as pool:
        replay = list(pool.map(claim, range(12)))
    assert all(state == "hit" for state, _cached in replay), replay
    assert all(cached == {"winner": True, "idem_replay": True} for _state, cached in replay)


# ---------------------------------------------------------------------- 7. Job
def test_job_create_and_get_roundtrip(repos):
    make_job(repos)
    row = repos.jobs.get(JOB_ID)
    assert row["job_id"] == JOB_ID and row["status"] == "CREATED"
    assert row["business_line"] == "L1" and row["revision"] == 1
    assert row["basis_pack"]["basis_pack_id"] == "B-PG"
    assert row["basis_pack_id"] == "B-PG"
    assert [event["reason"] for event in row["status_history"]] == ["JOB_CREATE"]
    assert row["created_at"] == "2026-01-01T00:00:00+00:00"


def test_job_get_missing_returns_none(repos):
    assert repos.jobs.get("job-missing") is None


def test_job_update_status_cas_success_and_conflict(repos):
    make_job(repos)
    history = [{"to_status": "INTAKE", "by": "pm", "at": now_iso()}]
    assert repos.jobs.update_status(JOB_ID, status="INTAKE", expected_status="CREATED",
                                    by="pm", at=now_iso(), status_history=history) is True
    assert repos.jobs.get(JOB_ID)["status"] == "INTAKE"
    assert repos.jobs.get(JOB_ID)["revision"] == 2
    assert repos.jobs.update_status(JOB_ID, status="REVIEW", expected_status="CREATED",
                                    by="pm", at=now_iso(), status_history=history) is False
    assert repos.jobs.get(JOB_ID)["status"] == "INTAKE"
    assert repos.jobs.update_status("job-missing", status="INTAKE", expected_status="CREATED",
                                    by="pm", at=now_iso(), status_history=[]) is False


# ----------------------------------------------------------------- 8. Document
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


# --------------------------------------------------------------------- 9. Task
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


# --------------------------------------------------------------------- 10. Gate
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
    assert rows[1]["judged_by"] == "judge-2"
    assert rows[1]["judged_at"] == "2026-01-01T00:00:02+00:00"
    assert repos.gates.list_by_job("job-unknown") == []


# ------------------------------------------------------------------- 11. Record
def test_record_put_and_list_by_job_and_kind(repos):
    make_job(repos)
    repos.records.put(JOB_ID, "BOQ", {"item": "A"}, "2026-01-01T00:00:01+00:00")
    repos.records.put(JOB_ID, "BOQ", {"item": "B"}, "2026-01-01T00:00:02+00:00")
    repos.records.put(JOB_ID, "PRICE", {"item": "C"}, "2026-01-01T00:00:03+00:00")
    assert repos.records.list_by_job(JOB_ID, "BOQ") == [{"item": "A"}, {"item": "B"}]
    assert repos.records.list_by_job(JOB_ID, "PRICE") == [{"item": "C"}]
    assert repos.records.list_by_job(JOB_ID, "MISSING") == []
    assert repos.records.list_by_job("job-unknown", "BOQ") == []


# ------------------------------------------------- 12. 与 SQLite 同场景逐字段对比
def _run_scenario(repos: RepositorySet):
    """同一套操作分别在 PG 与 SQLite 上执行，返回可逐字段比较的结果快照。"""
    make_job(repos)
    repos.evidence.save({"evidence_id": "ev-parity", "sha256": "sha-parity",
                         "job_id": JOB_ID, "rule_code": "AREA_RECT",
                         "payload": {"nested": [1, 2, {"x": "值"}]},
                         "created_at": "2026-01-01T00:00:01+00:00"})
    repos.calc_run.save(calc_run("run-parity"))
    repos.audit.add("tester", "JOB_CREATE", "JOB", JOB_ID, detail="parity", trace_id="tr-1")
    repos.documents.add(JOB_ID, "BOQ_XLSX", "v1", "sha-1", False,
                        "2026-01-01T00:00:01+00:00")
    repos.tasks.create(task_id="task-parity", job_id=JOB_ID, node="INTAKE", status="READY",
                       payload={"doc": "BOQ"}, assignee="alice",
                       history=[{"to": "READY"}],
                       created_at="2026-01-01T00:00:01+00:00",
                       updated_at="2026-01-01T00:00:01+00:00")
    repos.tasks.update("task-parity", status="DONE", result={"rows": 3}, approver="bob",
                       approved_at="2026-01-01T00:00:02+00:00", history=[{"to": "DONE"}],
                       updated_at="2026-01-01T00:00:02+00:00")
    repos.gates.add(JOB_ID, "G2_BASIS", "FAIL",
                    [{"rule": "BASIS_REQUIRED", "detail": "缺 basis_pack"}], "judge",
                    "2026-01-01T00:00:02+00:00")
    repos.records.put(JOB_ID, "BOQ", {"item": "A"}, "2026-01-01T00:00:01+00:00")

    idem_states = [repos.idem.claim("scope-parity", "fp-parity", "t", "a")[0],
                   repos.idem.claim("scope-parity", "fp-parity", "t", "a")[0],
                   repos.idem.claim("scope-parity", "fp-other", "t", "a")[0]]
    idem_lookup = repos.idem.lookup("scope-parity", "fp-parity")
    finished = repos.idem.finish("scope-parity", {"result": 42})
    idem_hit = repos.idem.claim("scope-parity", "fp-parity", "t", "a")
    released = repos.idem.release("scope-parity")

    cas_ok = repos.jobs.update_status(
        JOB_ID, status="INTAKE", expected_status="CREATED", by="pm",
        at="2026-01-01T00:10:00+00:00",
        status_history=[{"to_status": "INTAKE", "by": "pm",
                         "at": "2026-01-01T00:10:00+00:00"}])
    cas_stale = repos.jobs.update_status(
        JOB_ID, status="REVIEW", expected_status="CREATED", by="pm",
        at="2026-01-01T00:11:00+00:00", status_history=[])

    audit_rows = [{key: row[key] for key in
                   ("actor", "action", "object_type", "object_id", "detail", "trace_id")}
                  for row in repos.audit.list()]
    return {
        "evidence": repos.evidence.get("ev-parity"),
        "evidence_count": repos.evidence.count(JOB_ID),
        "evidence_by_job": repos.evidence.list_by_job(JOB_ID),
        "calc_run": repos.calc_run.get("run-parity"),
        "calc_run_list": repos.calc_run.list_by_job(JOB_ID),
        "job": repos.jobs.get(JOB_ID),
        "documents": repos.documents.list_by_job(JOB_ID),
        "tasks": repos.tasks.list_by_job(JOB_ID),
        "gates": repos.gates.list_by_job(JOB_ID),
        "records": repos.records.list_by_job(JOB_ID, "BOQ"),
        "audit": audit_rows,
        "idem_states": idem_states,
        "idem_lookup": idem_lookup,
        "idem_finished": finished,
        "idem_hit": idem_hit,
        "idem_released": released,
        "cas_ok": cas_ok,
        "cas_stale": cas_stale,
    }


def test_same_scenario_produces_identical_results_on_pg_and_sqlite(repos, tmp_path):
    sqlite_db_path = tmp_path / "parity.sqlite3"
    _sqlite_database, sqlite_repos = build_sqlite_repositories(str(sqlite_db_path))
    pg_result = _run_scenario(repos)
    sqlite_result = _run_scenario(sqlite_repos)
    for key in pg_result:
        assert pg_result[key] == sqlite_result[key], key
    assert pg_result["idem_states"] == ["claimed", "in_progress", "conflict"]
    assert pg_result["idem_hit"] == ("hit", {"result": 42, "idem_replay": True})
    assert pg_result["cas_ok"] is True and pg_result["cas_stale"] is False


def test_sqlite_and_pg_backends_are_reported_distinctly(repos, tmp_path):
    _sqlite_database, sqlite_repos = build_sqlite_repositories(str(tmp_path / "b.sqlite3"))
    assert repos.backend == "postgresql" and sqlite_repos.backend == "sqlite"
    assert repos.evidence.backend == "postgresql"
    assert sqlite_repos.evidence.backend == "sqlite"
