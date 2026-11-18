"""SQLite schema 定义与旧库迁移（V4.2.3 / P1-02、P1-04）。

DDL 的**权威来源**是 ``sql/migrations/sqlite/V00x__*.sql``（版本化 + 对应回滚脚本），
建表由 :class:`txbuddy.repository.migrations.SqliteMigrationRunner` 执行并记账到
``schema_version``；本文件的 ``SCHEMA_SQL`` 保留为等价对照 DDL（测试逐列校验）。
旧库补列/重建逻辑仍在 :meth:`SqliteDatabase.migrate_legacy` /
:meth:`SqliteDatabase.ensure_job_columns`：迁移原则是不改动已有列序（旧库有位置式写入），
只补列 / 重建不安全表，且幂等可重跑。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..models import JobStatusEvent, JobValidationError, parse_job_status

# P1-01-JOBMODEL：job 表增量列。旧库靠 ensure_job_columns 自动补齐，不改列序。
JOB_EXTRA_COLUMNS: dict[str, str] = {
    "basis_pack_id": "TEXT",
    "updated_by": "TEXT",
    "updated_at": "TEXT",
    "revision": "INTEGER DEFAULT 1",
    "status_history": "TEXT DEFAULT '[]'",
}

CALC_RUN_COLUMNS: list[str] = [
    "calc_run_id", "job_id", "rule_code", "rule_version", "basis_pack_id",
    "snapshot_sha256", "snapshot", "result", "unit", "evidence_id", "created_at",
]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS evidence(evidence_id TEXT PRIMARY KEY, sha256 TEXT, payload TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS calc_run(
    calc_run_id TEXT PRIMARY KEY, job_id TEXT, rule_code TEXT, rule_version TEXT, basis_pack_id TEXT,
    snapshot_sha256 TEXT, snapshot TEXT, result TEXT, unit TEXT, evidence_id TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, action TEXT, object_type TEXT,
    object_id TEXT, detail TEXT, trace_id TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS idem(
    scope_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'DONE', response TEXT,
    tenant TEXT, actor TEXT, claimed_at TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS job(
    job_id TEXT PRIMARY KEY, project_id TEXT, name TEXT, business_line TEXT,
    basis_pack TEXT, status TEXT, created_by TEXT, created_at TEXT,
    basis_pack_id TEXT, updated_by TEXT, updated_at TEXT,
    revision INTEGER DEFAULT 1, status_history TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS job_document(
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, doc_key TEXT, version TEXT,
    file_sha256 TEXT, verified INTEGER DEFAULT 0, added_at TEXT);
CREATE TABLE IF NOT EXISTS job_task(
    task_id TEXT PRIMARY KEY, job_id TEXT, node TEXT, status TEXT, payload TEXT,
    result TEXT, assignee TEXT, approver TEXT, approved_at TEXT, history TEXT,
    created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS job_record(
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, kind TEXT, payload TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS gate_record(
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, gate_code TEXT, result TEXT,
    failed_rules TEXT, judged_by TEXT, judged_at TEXT);
"""


def now_iso() -> str:
    """统一的 UTC ISO8601 时间戳（仓储层与业务层共用）。"""
    return datetime.now(timezone.utc).isoformat()


class SqliteDatabase:
    """SQLite 连接工厂 + schema 生命周期。

    并发安全：WAL + busy timeout（50 线程并发实测会触发 database is locked）。
    """

    backend = "sqlite"

    def __init__(self, path: str | Path = "data/txbuddy-dev.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- 连接
    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def connect_scoped(self):
        """仓储用连接工厂：事务体内复用同一条连接，事务外每次开新连接。

        P1-05：``SqliteRepositorySet`` 全部仓储都拿到这个工厂，因此任意一段仓储写入
        只要被 ``with repos.unit_of_work():`` 包住就自动成为同一事务，无需改调用方。
        """
        from .unit_of_work import active_transaction

        state = active_transaction(str(self.path.resolve()))
        return state.proxy if state is not None else self.connect()

    def unit_of_work(self, *, immediate: bool = True, label: str = "unit-of-work"):
        """返回一个事务边界（见 ``repository/unit_of_work.py``）。"""
        from .unit_of_work import SqliteUnitOfWork

        return SqliteUnitOfWork(self, immediate=immediate, label=label)

    # ------------------------------------------------------------- schema
    def columns(self, table: str) -> set[str]:
        conn = self.connect()
        try:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        except sqlite3.DatabaseError:
            return set()
        finally:
            conn.close()

    def migrate_legacy(self) -> None:
        """旧库兼容：idem 缺 fingerprint 列则重建；calc_run 缺 job_id 或列序不同则重建。"""
        idem_cols = self.columns("idem")
        if idem_cols and "fingerprint" not in idem_cols:
            conn = self.connect()
            try:
                conn.execute("DROP TABLE idem")
            finally:
                conn.close()
        # V4.2.3：幂等改为数据库级原子认领，需要 state / claimed_at 两列。
        # 旧表只有 (scope_key,fingerprint,response,tenant,actor,at)，已完成记录语义等于 DONE，直接搬迁。
        idem_cols = self.columns("idem")
        if idem_cols and "state" not in idem_cols:
            conn = self.connect()
            try:
                conn.execute("ALTER TABLE idem RENAME TO idem_legacy")
                conn.execute("""CREATE TABLE idem(
                    scope_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'DONE', response TEXT,
                    tenant TEXT, actor TEXT, claimed_at TEXT, at TEXT)""")
                conn.execute("""INSERT INTO idem(scope_key,fingerprint,state,response,tenant,actor,claimed_at,at)
                                SELECT scope_key,fingerprint,'DONE',response,tenant,actor,NULL,at FROM idem_legacy""")
                conn.execute("DROP TABLE idem_legacy")
            finally:
                conn.close()
        order = self._calc_run_order()
        if order and "job_id" in order and order != CALC_RUN_COLUMNS:
            self._rebuild_calc_run(order)

    def _calc_run_order(self) -> list[str]:
        conn = self.connect()
        try:
            return [row[1] for row in conn.execute("PRAGMA table_info(calc_run)").fetchall()]
        finally:
            conn.close()

    def _rebuild_calc_run(self, order: list[str]) -> None:
        """旧库列序不同 → 重建，避免位置式写入错列。"""
        conn = self.connect()
        try:
            conn.executescript("""
            ALTER TABLE calc_run RENAME TO calc_run_legacy;
            CREATE TABLE calc_run(
                calc_run_id TEXT PRIMARY KEY, job_id TEXT, rule_code TEXT, rule_version TEXT,
                basis_pack_id TEXT, snapshot_sha256 TEXT, snapshot TEXT, result TEXT,
                unit TEXT, evidence_id TEXT, created_at TEXT);
            """)
            common = [name for name in CALC_RUN_COLUMNS if name in order]
            cols_sql = ",".join(common)
            conn.execute(f"INSERT INTO calc_run({cols_sql}) SELECT {cols_sql} FROM calc_run_legacy")
            conn.execute("DROP TABLE calc_run_legacy")
        finally:
            conn.close()

    def create_schema(self) -> None:
        """执行 ``sql/migrations/sqlite`` 的版本化迁移（V001→V003）。

        P1-04 之前这里直接 ``executescript(SCHEMA_SQL)``；现在建表统一走迁移器，
        账本 ``schema_version`` 记录已应用的版本与脚本校验和。``SCHEMA_SQL`` 保留为
        对照 DDL，由 ``tests/test_v423_migration.py`` 验证两者逐列等价。
        重复调用幂等：已登记版本不会再次执行。
        """
        from .migrations import SqliteMigrationRunner

        SqliteMigrationRunner(self.path).upgrade()

    def ensure_job_columns(self) -> None:
        """补齐 job 增量列并回填 basis_pack_id / status_history（旧库升级不丢数据、不改列序）。"""
        cols = self.columns("job")
        if not cols:
            return
        conn = self.connect()
        try:
            for name, ddl in JOB_EXTRA_COLUMNS.items():
                if name not in cols:
                    conn.execute(f"ALTER TABLE job ADD COLUMN {name} {ddl}")
            conn.execute("UPDATE job SET revision=COALESCE(revision,1), "
                         "status_history=COALESCE(status_history,'[]')")
            rows = conn.execute("SELECT job_id,basis_pack FROM job "
                               "WHERE basis_pack_id IS NULL OR basis_pack_id=''").fetchall()
            for job_id, raw in rows:
                try:
                    pack = json.loads(raw or "{}")
                except json.JSONDecodeError:
                    continue
                if isinstance(pack, dict) and pack.get("basis_pack_id"):
                    conn.execute("UPDATE job SET basis_pack_id=? WHERE job_id=?",
                                 (str(pack["basis_pack_id"]), job_id))
            # 旧库没有状态迁移审计：补一条"历史建单"事件，保证审计链不断头。
            legacy = conn.execute("SELECT job_id,created_by,created_at,status FROM job "
                                  "WHERE status_history IS NULL OR status_history IN ('','[]')").fetchall()
            for job_id, created_by, created_at, status in legacy:
                try:
                    event = JobStatusEvent(to_status=parse_job_status(status),
                                           by=created_by or "system",
                                           at=created_at or now_iso(),
                                           reason="JOB_CREATE_LEGACY").model_dump(mode="json")
                except (ValueError, JobValidationError):
                    continue
                conn.execute("UPDATE job SET status_history=? WHERE job_id=?",
                             (json.dumps([event], ensure_ascii=False), job_id))
        finally:
            conn.close()

    def initialize(self) -> None:
        """完整初始化：旧库迁移 → 建表 → 补列回填（幂等）。"""
        self.migrate_legacy()
        self.create_schema()
        self.ensure_job_columns()
