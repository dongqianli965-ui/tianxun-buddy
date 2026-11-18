"""P1-04-MIGRATION：版本化迁移 / 回滚的实测（SQLite + 真实 PostgreSQL）。

覆盖 TASK.md 的三条硬要求，并加上工程化约束：

1. **空库全量迁移 → 校验表结构**：V001→V003 全量执行后，9 张业务表与
   ``repository/schema.py`` 的对照 DDL（``SCHEMA_SQL``）逐列、逐索引一致；
   账本 ``schema_version`` 记录版本、名称、正/反向脚本 sha256、时间与耗时。
2. **重复迁移幂等**：第二次 ``upgrade()`` 返回 ``[]``，账本（含 applied_at）与
   schema 完全不变，已有业务数据不丢。
3. **回滚后表消失且无残留**：``downgrade()`` 逆序执行 R003→R002→R001，业务表、
   索引、``sqlite_sequence`` 记录全部清除，只剩账本表；重复回滚是 no-op；
   回滚后可再次全量迁移。
4. 部分升级/部分回滚（target）、目标版本越界、脚本漂移（改历史 V/R 必须报错）、
   缺回滚脚本 / 版本重复 / 名称不配对必须报错、SQL 切分器边界。
5. PostgreSQL：同一套 runner 在真实实例上把 V001/V002 落到 core schema、
   V003 落到 runtime schema；重复迁移幂等；全量回滚后两个 schema 都只剩账本表。
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from txbuddy.repository import (
    SCHEMA_VERSION_TABLE,
    MigrationChecksumError,
    MigrationNotFoundError,
    MigrationStateError,
    PostgresMigrationRunner,
    SqliteDatabase,
    SqliteMigrationRunner,
    build_sqlite_repositories,
    discover_postgres_migrations,
    discover_sqlite_migrations,
    split_sql_statements,
)
from txbuddy.repository.migrations import sha256_text
from txbuddy.repository.pg import (
    CORE_TABLES,
    DSN_ENV,
    RUNTIME_TABLES,
    PgDatabase,
    PgRepositorySet,
)
from txbuddy.repository.schema import SCHEMA_SQL

SQL_DIR = Path(__file__).resolve().parents[2] / "sql"
SQLITE_MIGRATION_DIR = SQL_DIR / "migrations" / "sqlite"
PG_MIGRATION_DIR = SQL_DIR / "migrations" / "postgresql"

SQLITE_TABLES = (
    "evidence", "calc_run", "audit_log", "idem", "job", "job_document",
    "job_task", "job_record", "gate_record",
)

CREATE_RE = re.compile(
    r"^[ \t]*CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+([a-z_][a-z0-9_]*)",
    re.IGNORECASE | re.MULTILINE)
DROP_RE = re.compile(
    r"^[ \t]*DROP\s+TABLE(?:\s+IF\s+EXISTS)?\s+([a-z_][a-z0-9_]*)",
    re.IGNORECASE | re.MULTILINE)

PG_RUNTIME_SCHEMA = "txbuddy_p104_runtime"
PG_CORE_SCHEMA = "txbuddy_p104_core"


# --------------------------------------------------------------------- helpers
def sqlite_tables(path: str | Path) -> set[str]:
    """业务表 + 账本表（排除 SQLite 内部 sqlite_* 表）。"""
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        connection.close()
    return {row[0] for row in rows}


def sqlite_columns(path: str | Path, table: str) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return [tuple(row) for row in connection.execute(f"PRAGMA table_info({table})")]
    finally:
        connection.close()


def sqlite_column_names(path: str | Path, table: str) -> list[str]:
    return [row[1] for row in sqlite_columns(path, table)]


def sqlite_indexes(path: str | Path, table: str) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        indexes = []
        for row in connection.execute(f"PRAGMA index_list({table})"):
            name = row[1]
            columns = tuple(info[2] for info in connection.execute(f"PRAGMA index_info({name})"))
            indexes.append((name, row[2], row[3], columns))
        return sorted(indexes)
    finally:
        connection.close()


def ledger_rows(path: str | Path) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT version,name,checksum,rollback_checksum,applied_at,execution_ms "
            "FROM schema_version ORDER BY version").fetchall()
    finally:
        connection.close()


def make_reference_schema(path: str | Path) -> None:
    """用 schema.py 的对照 DDL 建一份参考库（不经过迁移器）。"""
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMA_SQL)
        connection.commit()
    finally:
        connection.close()


def _dsn() -> str:
    dsn = (os.environ.get(DSN_ENV) or os.environ.get("TX_PG_DSN") or "").strip()
    if not dsn:
        candidate = Path(__file__).resolve().parents[1] / ".pg-dsn"
        if candidate.is_file():
            dsn = candidate.read_text(encoding="utf-8").strip()
    if not dsn:
        pytest.fail(f"未设置 {DSN_ENV} 且找不到 backend/.pg-dsn：本任务要求连真实 PostgreSQL，"
                    "不允许 skip。")
    return dsn


def _pg_drop_schemas(database: PgDatabase) -> None:
    connection = database.connect_schema("public")
    try:
        with connection.cursor() as cursor:
            for schema in (PG_RUNTIME_SCHEMA, PG_CORE_SCHEMA):
                cursor.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    finally:
        connection.close()


def _pg_create_schemas(database: PgDatabase) -> None:
    connection = database.connect_schema("public")
    try:
        with connection.cursor() as cursor:
            for schema in (PG_RUNTIME_SCHEMA, PG_CORE_SCHEMA):
                cursor.execute(f"CREATE SCHEMA {schema}")
    finally:
        connection.close()


def _pg_columns(database: PgDatabase, schema: str) -> dict[str, list[str]]:
    connection = database.connect_schema(schema)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema=%s ORDER BY table_name, ordinal_position", (schema,))
            columns: dict[str, list[str]] = {}
            for table, column in cursor.fetchall():
                columns.setdefault(table, []).append(column)
            return columns
    finally:
        connection.close()


def _pg_indexes(database: PgDatabase, schema: str) -> set[str]:
    connection = database.connect_schema(schema)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT indexname FROM pg_indexes WHERE schemaname=%s", (schema,))
            return {row[0] for row in cursor.fetchall()}
    finally:
        connection.close()


# --------------------------------------------------------------------- fixtures
@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "txbuddy-migrate.sqlite3"


@pytest.fixture()
def runner(db_path: Path) -> SqliteMigrationRunner:
    return SqliteMigrationRunner(db_path)


@pytest.fixture()
def migration_copy(tmp_path: Path) -> Path:
    """SQLite 迁移目录的可篡改副本，用于漂移/缺脚本用例（绝不改仓库内脚本）。"""
    target = tmp_path / "migrations" / "sqlite"
    shutil.copytree(SQLITE_MIGRATION_DIR, target)
    return target


@pytest.fixture(scope="module")
def pg_database() -> PgDatabase:
    database = PgDatabase(_dsn(), runtime_schema=PG_RUNTIME_SCHEMA, core_schema=PG_CORE_SCHEMA)
    _pg_drop_schemas(database)
    _pg_create_schemas(database)
    try:
        yield database
    finally:
        _pg_drop_schemas(database)


@pytest.fixture()
def pg_runner(pg_database: PgDatabase) -> PostgresMigrationRunner:
    _pg_drop_schemas(pg_database)
    _pg_create_schemas(pg_database)
    return PostgresMigrationRunner(pg_database)


# ------------------------------------------------- 1. 脚本清单与静态完整性
def test_sqlite_discovery_returns_paired_contiguous_versions():
    migrations = discover_sqlite_migrations()
    assert [migration.version for migration in migrations] == [1, 2, 3]
    assert [migration.name for migration in migrations] == [
        "core_tables", "workflow_tables", "record_tables"]
    for migration in migrations:
        assert migration.up.parent == SQLITE_MIGRATION_DIR
        assert migration.down.parent == SQLITE_MIGRATION_DIR
        assert migration.up.is_file() and migration.down.is_file()
        assert migration.up.suffix == migration.down.suffix == ".sql"
        assert migration.checksum == sha256_text(migration.read_up())
        assert migration.rollback_checksum == sha256_text(migration.read_down())


def test_sqlite_scripts_create_and_drop_exactly_the_reference_tables():
    migrations = discover_sqlite_migrations()
    created: set[str] = set()
    dropped: set[str] = set()
    for migration in migrations:
        up_tables = set(CREATE_RE.findall(migration.read_up()))
        down_tables = set(DROP_RE.findall(migration.read_down()))
        assert up_tables, f"{migration.label} 没有建表语句"
        assert up_tables == down_tables, f"{migration.label} 回滚脚本与正向脚本表集合不一致"
        assert not (created & up_tables), f"{migration.label} 重复创建了 {created & up_tables}"
        assert SCHEMA_VERSION_TABLE not in down_tables, "回滚脚本不得删除账本表"
        created |= up_tables
        dropped |= down_tables
    reference_tables = set(CREATE_RE.findall(SCHEMA_SQL))
    assert created == set(SQLITE_TABLES) == reference_tables
    assert dropped == created


def test_postgres_rollback_scripts_mirror_forward_scripts():
    migrations = discover_postgres_migrations(
        core_schema="txbuddy_core_probe", runtime_schema="txbuddy_runtime_probe")
    assert [migration.version for migration in migrations] == [1, 2, 3]
    assert [migration.schema for migration in migrations] == [
        "txbuddy_core_probe", "txbuddy_core_probe", "txbuddy_runtime_probe"]
    assert migrations[2].up.name == "003_repository_runtime_postgresql.sql"
    assert migrations[0].down.parent == PG_MIGRATION_DIR
    seen_by_schema: dict[str, set[str]] = {}
    for migration in migrations:
        up_tables = set(CREATE_RE.findall(migration.read_up()))
        down_tables = set(DROP_RE.findall(migration.read_down()))
        assert up_tables == down_tables, f"{migration.label} 的回滚脚本表集合不一致"
        assert SCHEMA_VERSION_TABLE not in down_tables
        seen = seen_by_schema.setdefault(migration.schema, set())
        assert not (seen & up_tables), f"{migration.label} 在同一 schema 重复建表"
        seen |= up_tables
    core_tables = seen_by_schema["txbuddy_core_probe"]
    runtime_tables = seen_by_schema["txbuddy_runtime_probe"]
    assert len(core_tables) == 23  # 001 的 19 张 + 002 的 4 张
    assert runtime_tables == set(RUNTIME_TABLES) == set(SQLITE_TABLES)
    assert set(CORE_TABLES) <= core_tables


# ------------------------------------------------- 2. 空库全量迁移 → 表结构
def test_empty_database_full_migration_matches_reference_schema(runner, db_path, tmp_path):
    assert sqlite_tables(db_path) == set()  # 空库（连接后只建了空文件）
    assert runner.applied() == []
    assert runner.current_version() == 0
    assert runner.pending() == [1, 2, 3]

    assert runner.upgrade() == [1, 2, 3]
    assert sqlite_tables(db_path) == set(SQLITE_TABLES) | {SCHEMA_VERSION_TABLE}
    assert runner.pending() == []
    assert runner.current_version() == 3

    reference = tmp_path / "reference.sqlite3"
    make_reference_schema(reference)
    for table in SQLITE_TABLES:
        assert sqlite_columns(db_path, table) == sqlite_columns(reference, table), table
        assert sqlite_indexes(db_path, table) == sqlite_indexes(reference, table), table
        assert sqlite_columns(db_path, table), table  # 表真的存在且非空列


def test_ledger_records_version_checksums_and_timestamps(runner, db_path):
    runner.upgrade()
    rows = ledger_rows(db_path)
    migrations = runner.available()
    assert [row[0] for row in rows] == [1, 2, 3]
    for row, migration in zip(rows, migrations):
        version, name, checksum, rollback_checksum, applied_at, execution_ms = row
        assert version == migration.version
        assert name == migration.name
        assert checksum == migration.checksum == sha256_text(migration.read_up())
        assert rollback_checksum == migration.rollback_checksum
        applied = datetime.fromisoformat(applied_at)
        assert applied.tzinfo is not None
        assert isinstance(execution_ms, int) and execution_ms >= 0
    status = runner.status()
    assert status["backend"] == "sqlite"
    assert status["ledger"] == SCHEMA_VERSION_TABLE
    assert status["latest_version"] == 3
    assert status["current_version"] == 3
    assert [row["version"] for row in status["applied"]] == [1, 2, 3]
    assert status["pending"] == []


# ------------------------------------------------- 3. 重复迁移幂等
def test_repeated_upgrade_is_idempotent_and_keeps_data(runner, db_path):
    runner.upgrade()
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO evidence(evidence_id,sha256,payload,created_at) VALUES(?,?,?,?)",
            ("ev-idem", "sha-idem", '{"v":1}', "2026-01-01T00:00:00+00:00"))
        connection.commit()
    finally:
        connection.close()

    ledger_before = ledger_rows(db_path)
    schema_before = {table: sqlite_columns(db_path, table) for table in SQLITE_TABLES}
    assert runner.upgrade() == []
    assert runner.upgrade(target=3) == []
    assert ledger_rows(db_path) == ledger_before  # applied_at 也不变，未重写账本
    assert {table: sqlite_columns(db_path, table) for table in SQLITE_TABLES} == schema_before

    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute(
            "SELECT sha256 FROM evidence WHERE evidence_id='ev-idem'").fetchone()[0] == "sha-idem"
    finally:
        connection.close()


# ------------------------------------------------- 4. 回滚后无残留
def test_full_rollback_removes_tables_indexes_and_sequence_rows(runner, db_path):
    runner.upgrade()
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO evidence(evidence_id,sha256,payload,created_at) VALUES(?,?,?,?)",
            ("ev-rollback", "sha", '{}', "2026-01-01T00:00:00+00:00"))
        connection.execute("INSERT INTO audit_log(actor,action,at) VALUES('tester','MIGRATE',?)",
                           ("2026-01-01T00:00:00+00:00",))
        connection.commit()
    finally:
        connection.close()

    assert runner.downgrade() == [3, 2, 1]
    assert sqlite_tables(db_path) == {SCHEMA_VERSION_TABLE}
    assert ledger_rows(db_path) == []
    assert runner.applied() == [] and runner.current_version() == 0
    for table in SQLITE_TABLES:
        assert sqlite_columns(db_path, table) == [], table

    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        ).fetchall() == []
        # AUTOINCREMENT 的 sqlite_sequence 行必须随 DROP TABLE 一起清掉
        assert connection.execute("SELECT name FROM sqlite_sequence").fetchall() == []
    finally:
        connection.close()

    assert runner.downgrade() == []  # 重复回滚是 no-op
    assert sqlite_tables(db_path) == {SCHEMA_VERSION_TABLE}

    assert runner.upgrade() == [1, 2, 3]  # 回滚后可再次全量迁移
    assert sqlite_tables(db_path) == set(SQLITE_TABLES) | {SCHEMA_VERSION_TABLE}
    assert [row[0] for row in ledger_rows(db_path)] == [1, 2, 3]


def test_partial_upgrade_and_downgrade_targets(runner, db_path):
    assert runner.upgrade(target=1) == [1]
    assert sqlite_tables(db_path) == {"evidence", "calc_run", "audit_log", SCHEMA_VERSION_TABLE}
    assert runner.pending() == [2, 3]
    assert runner.upgrade(target=2) == [2]
    assert "job" in sqlite_tables(db_path)
    assert "job_record" not in sqlite_tables(db_path)
    assert runner.upgrade() == [3]

    assert runner.downgrade(target=2) == [3]
    assert "job_record" not in sqlite_tables(db_path)
    assert "job" in sqlite_tables(db_path)
    assert runner.downgrade(target=2) == []
    assert runner.downgrade() == [2, 1]
    assert sqlite_tables(db_path) == {SCHEMA_VERSION_TABLE}


def test_invalid_target_versions_are_rejected(runner):
    with pytest.raises(MigrationStateError):
        runner.upgrade(target=4)
    with pytest.raises(MigrationStateError):
        runner.upgrade(target=-1)
    runner.upgrade()
    with pytest.raises(MigrationStateError):
        runner.upgrade(target=1)  # 反向目标必须走 downgrade
    with pytest.raises(MigrationStateError):
        runner.downgrade(target=4)


# ------------------------------------------------- 5. 脚本漂移 / 缺脚本必须报错
def test_changed_history_up_script_is_refused(db_path, migration_copy):
    SqliteMigrationRunner(db_path, directory=migration_copy).upgrade()
    target = migration_copy / "V002__workflow_tables.sql"
    target.write_text(target.read_text(encoding="utf-8") + "\n-- tampered\n", encoding="utf-8")
    with pytest.raises(MigrationChecksumError) as excinfo:
        SqliteMigrationRunner(db_path, directory=migration_copy).upgrade()
    message = str(excinfo.value)
    assert "V002__workflow_tables" in message
    assert "新增迁移" in message
    assert [row[0] for row in ledger_rows(db_path)] == [1, 2, 3]
    assert "job" in sqlite_tables(db_path)


def test_changed_history_rollback_script_is_refused(db_path, migration_copy):
    runner = SqliteMigrationRunner(db_path, directory=migration_copy)
    runner.upgrade()
    target = migration_copy / "R003__record_tables.sql"
    target.write_text(target.read_text(encoding="utf-8") + "\n-- tampered\n", encoding="utf-8")
    with pytest.raises(MigrationChecksumError) as excinfo:
        runner.downgrade()
    assert "R003__record_tables" in str(excinfo.value)
    assert [row[0] for row in ledger_rows(db_path)] == [1, 2, 3]
    assert "gate_record" in sqlite_tables(db_path)


def test_missing_rollback_script_is_reported(db_path, migration_copy):
    (migration_copy / "R002__workflow_tables.sql").unlink()
    with pytest.raises(MigrationNotFoundError) as excinfo:
        SqliteMigrationRunner(db_path, directory=migration_copy)
    assert "002" in str(excinfo.value)


def test_duplicate_version_is_rejected(db_path, migration_copy):
    shutil.copy(migration_copy / "V001__core_tables.sql", migration_copy / "V001__duplicate.sql")
    with pytest.raises(MigrationStateError) as excinfo:
        SqliteMigrationRunner(db_path, directory=migration_copy)
    assert "重复" in str(excinfo.value)


def test_mismatched_rollback_name_is_rejected(db_path, migration_copy):
    (migration_copy / "R002__workflow_tables.sql").rename(migration_copy / "R002__other_name.sql")
    with pytest.raises(MigrationNotFoundError) as excinfo:
        SqliteMigrationRunner(db_path, directory=migration_copy)
    assert "名称" in str(excinfo.value)


def test_split_sql_statements_handles_quotes_and_comments():
    script = """
    -- 行注释里的分号 ; 不算切分点
    CREATE TABLE demo(a TEXT DEFAULT 'x;y');  /* 块注释 ; 也不算 */
    INSERT INTO demo(a) VALUES('it''s;ok');
    SELECT "a;b" FROM demo;
    """
    statements = split_sql_statements(script)
    assert len(statements) == 3, statements
    assert statements[0].startswith("-- ") and "CREATE TABLE demo" in statements[0]
    assert "it''s;ok" in statements[1]
    assert statements[2].startswith("SELECT")
    assert split_sql_statements("-- only comment\n/* block */\n") == []


# ------------------------------------------------- 6. 与仓储层/服务启动的集成
def test_migrated_database_is_usable_by_repositories(runner, db_path):
    runner.upgrade()
    database, repos = build_sqlite_repositories(str(db_path), initialize=False)
    assert all(repos.health().values())

    evidence = {"evidence_id": "ev-1", "sha256": "abc123", "job_id": "job-1",
                "created_at": "2026-01-01T00:00:01+00:00"}
    repos.evidence.save(evidence)
    assert repos.evidence.get("ev-1") == evidence

    assert repos.idem.claim("scope|key", "fp-1", tenant="t", actor="a") == ("claimed", None)
    assert repos.idem.finish("scope|key", {"ok": True}) is True
    assert repos.idem.lookup("scope|key", "fp-1") == ("hit", {"ok": True})

    repos.audit.add("tester", "MIGRATE", "schema", "v3")
    assert [row["action"] for row in repos.audit.list()] == ["MIGRATE"]

    ledger_before = ledger_rows(db_path)
    database.initialize()  # 服务启动路径：重复初始化不得重跑迁移或丢数据
    assert ledger_rows(db_path) == ledger_before
    assert repos.evidence.get("ev-1") == evidence


def test_sqlite_database_initialize_uses_versioned_migrations(db_path):
    database = SqliteDatabase(db_path)
    database.initialize()
    assert [row[0] for row in ledger_rows(db_path)] == [1, 2, 3]
    assert set(SQLITE_TABLES) <= sqlite_tables(db_path)
    assert {"version", "name", "checksum", "rollback_checksum", "applied_at",
            "execution_ms"} <= database.columns(SCHEMA_VERSION_TABLE)
    before = ledger_rows(db_path)
    database.initialize()
    assert ledger_rows(db_path) == before


def test_legacy_database_is_adopted_without_data_loss(db_path):
    """旧库（无账本、表已存在）升级：只补记版本，不重建表、不丢数据。"""
    connection = sqlite3.connect(db_path)
    try:
        connection.executescript(SCHEMA_SQL)
        connection.execute(
            "INSERT INTO evidence(evidence_id,sha256,payload,created_at) VALUES(?,?,?,?)",
            ("ev-legacy", "legacy-sha", '{"legacy":true}', "2026-01-01T00:00:00+00:00"))
        connection.commit()
    finally:
        connection.close()

    SqliteDatabase(db_path).initialize()
    assert [row[0] for row in ledger_rows(db_path)] == [1, 2, 3]
    connection = sqlite3.connect(db_path)
    try:
        assert connection.execute(
            "SELECT sha256 FROM evidence WHERE evidence_id='ev-legacy'").fetchone()[0] == "legacy-sha"
    finally:
        connection.close()


# ------------------------------------------------- 7. PostgreSQL 真实实例
def test_postgres_full_migration_matches_runtime_contract(pg_runner, pg_database, tmp_path):
    assert pg_runner.applied() == []
    assert pg_runner.upgrade() == [1, 2, 3]
    assert pg_runner.pending() == []
    assert pg_runner.current_version() == 3

    runtime_tables = pg_database.tables(PG_RUNTIME_SCHEMA)
    assert runtime_tables == set(RUNTIME_TABLES) | {SCHEMA_VERSION_TABLE}
    core_tables = pg_database.tables(PG_CORE_SCHEMA)
    assert set(CORE_TABLES) <= core_tables
    assert len(core_tables) == 24  # 001 的 19 张 + 002 的 4 张 + 账本

    rows = pg_runner.applied()
    assert [(row["version"], row["schema"]) for row in rows] == [
        (1, PG_CORE_SCHEMA), (2, PG_CORE_SCHEMA), (3, PG_RUNTIME_SCHEMA)]
    migrations = {migration.version: migration for migration in pg_runner.available()}
    for row in rows:
        assert row["checksum"] == migrations[row["version"]].checksum
        assert row["rollback_checksum"] == migrations[row["version"]].rollback_checksum
        assert datetime.fromisoformat(row["applied_at"]).tzinfo is not None

    assert {"idx_calc_run_job", "idx_idem_claim", "idx_document_job", "idx_task_job",
            "idx_record_job_kind", "idx_gate_job"} <= _pg_indexes(pg_database, PG_RUNTIME_SCHEMA)

    # 与 SQLite 迁移结果逐列对齐（003 头部承诺两套运行时表同构）
    sqlite_path = tmp_path / "parity.sqlite3"
    SqliteMigrationRunner(sqlite_path).upgrade()
    pg_columns = _pg_columns(pg_database, PG_RUNTIME_SCHEMA)
    for table in RUNTIME_TABLES:
        assert pg_columns[table] == sqlite_column_names(sqlite_path, table), table


def test_postgres_repeated_upgrade_is_idempotent(pg_runner, pg_database):
    pg_runner.upgrade()
    repos = PgRepositorySet(pg_database)
    repos.evidence.save({"evidence_id": "ev-pg-1", "sha256": "pg-sha", "job_id": "job-pg",
                         "created_at": "2026-01-01T00:00:01+00:00"})
    before = pg_runner.applied()
    assert pg_runner.upgrade() == []
    assert pg_runner.applied() == before
    assert repos.evidence.get("ev-pg-1")["sha256"] == "pg-sha"


def test_postgres_full_rollback_leaves_no_residue(pg_runner, pg_database):
    pg_runner.upgrade()
    assert pg_runner.downgrade() == [3, 2, 1]
    assert pg_database.tables(PG_RUNTIME_SCHEMA) == {SCHEMA_VERSION_TABLE}
    assert pg_database.tables(PG_CORE_SCHEMA) == {SCHEMA_VERSION_TABLE}
    assert pg_runner.applied() == []
    # 业务表与索引全部消失；只剩账本自身的 PK/UNIQUE 索引
    assert {name for name in _pg_indexes(pg_database, PG_RUNTIME_SCHEMA)
            if not name.startswith(SCHEMA_VERSION_TABLE)} == set()
    assert {name for name in _pg_indexes(pg_database, PG_CORE_SCHEMA)
            if not name.startswith(SCHEMA_VERSION_TABLE)} == set()
    assert pg_runner.downgrade() == []

    assert pg_runner.upgrade() == [1, 2, 3]
    assert set(CORE_TABLES) <= pg_database.tables(PG_CORE_SCHEMA)
    assert set(RUNTIME_TABLES) <= pg_database.tables(PG_RUNTIME_SCHEMA)


def test_postgres_partial_downgrade_is_scoped_by_version(pg_runner, pg_database):
    pg_runner.upgrade()
    assert pg_runner.downgrade(target=2) == [3]
    assert pg_database.tables(PG_RUNTIME_SCHEMA) == {SCHEMA_VERSION_TABLE}
    assert set(CORE_TABLES) <= pg_database.tables(PG_CORE_SCHEMA)
    assert pg_runner.current_version() == 2
    assert pg_runner.pending() == [3]
    assert pg_runner.upgrade() == [3]
    assert set(RUNTIME_TABLES) <= pg_database.tables(PG_RUNTIME_SCHEMA)
