"""版本化迁移与回滚执行器（V4.2.3 / P1-04-MIGRATION）。

设计目标：**同一套语义在 SQLite 与 PostgreSQL 上都成立**。

* 正向脚本 ``V{版本}__{名称}.sql``，回滚脚本 ``R{版本}__{名称}.sql``；版本号连续、
  一一配对（PostgreSQL 的正向脚本保留在 ``sql/00x_*``，回滚脚本在
  ``sql/migrations/postgresql/``，避免复制两份 DDL）。
* 账本表 ``schema_version`` 记录 version / name / checksum / rollback_checksum /
  applied_at / execution_ms；checksum 是脚本规范化（UTF-8、CRLF→LF）后的 sha256。
* 已登记版本脚本内容变化 → :class:`MigrationChecksumError`（历史脚本只读，必须新增版本）；
* 每个版本一个事务：DDL 与账本行同进同退，失败整体回滚，不留半套表；
* ``upgrade()`` 重复调用返回 ``[]``（幂等）；``downgrade()`` 逆序执行回滚脚本，
  全量回滚后只剩账本表，无业务表/索引残留。

用法::

    from txbuddy.repository.migrations import SqliteMigrationRunner
    SqliteMigrationRunner("data/txbuddy-dev.sqlite3").upgrade()

    from txbuddy.repository.migrations import PostgresMigrationRunner
    PostgresMigrationRunner(pg_database).downgrade()   # R003→R002→R001
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SQL_DIR_ENV = "TXBUDDY_SQL_DIR"
SCHEMA_VERSION_TABLE = "schema_version"

_UP_FILE_RE = re.compile(r"^V(\d{3,})__([a-z][a-z0-9_]*)\.sql$")
_DOWN_FILE_RE = re.compile(r"^R(\d{3,})__([a-z][a-z0-9_]*)\.sql$")
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

# PostgreSQL 事务级咨询锁的键（同一时刻只允许一个迁移者进入 DDL 段）。
_PG_MIGRATION_LOCK = "txbuddy:schema_version"


class MigrationError(RuntimeError):
    """迁移框架错误的基类。"""


class MigrationNotFoundError(MigrationError):
    """迁移目录 / 正向脚本 / 回滚脚本缺失或无法配对。"""


class MigrationChecksumError(MigrationError):
    """已登记版本的正向或回滚脚本内容发生变化（拒绝静默漂移）。"""


class MigrationStateError(MigrationError):
    """版本清单或账本状态非法（重复版本、未知版本、目标版本越界等）。"""


# --------------------------------------------------------------------- 脚本模型
def default_sql_dir() -> Path:
    """项目根 ``sql/``：backend/txbuddy/repository/migrations.py -> parents[3]。"""
    override = os.environ.get(SQL_DIR_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "sql"


def normalize_sql(text: str) -> str:
    """规范化脚本后再算校验和：去 BOM、CRLF→LF、统一 UTF-8 文本。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def sha256_text(text: str) -> str:
    return hashlib.sha256(normalize_sql(text).encode("utf-8")).hexdigest()


def _read_script(path: Path) -> str:
    if not path.is_file():
        raise MigrationNotFoundError(f"迁移脚本不存在: {path}")
    return normalize_sql(path.read_text(encoding="utf-8-sig"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Migration:
    """一个版本的正向 + 回滚脚本。

    ``schema`` 为空表示"当前连接默认 schema"（SQLite 无 schema 概念）；
    PostgreSQL 用它区分 core schema（V001/V002）与 runtime schema（V003）。
    """

    version: int
    name: str
    up: Path
    down: Path
    schema: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "up", Path(self.up))
        object.__setattr__(self, "down", Path(self.down))
        if not isinstance(self.version, int) or self.version <= 0:
            raise MigrationStateError(f"非法迁移版本号: {self.version!r}")
        if not _UP_FILE_RE.match(f"V{self.version:03d}__{self.name}.sql"):
            raise MigrationStateError(f"非法迁移名: {self.name!r}")

    @property
    def checksum(self) -> str:
        """正向脚本当前内容的 sha256（每次都重新读，才能发现事后篡改）。"""
        return sha256_text(_read_script(self.up))

    @property
    def rollback_checksum(self) -> str:
        return sha256_text(_read_script(self.down))

    def read_up(self) -> str:
        return _read_script(self.up)

    def read_down(self) -> str:
        return _read_script(self.down)

    @property
    def label(self) -> str:
        return f"V{self.version:03d}__{self.name}"


# --------------------------------------------------------------------- SQL 切分
def split_sql_statements(sql: str) -> list[str]:
    """按分号切分 SQL，忽略字符串/标识符/行注释/块注释里的分号。

    SQLite 的 ``executescript`` 会隐式提交，做不到"DDL 与账本行同事务"；
    因此这里先把脚本切成单条语句，再用 ``execute`` 逐条执行。
    """
    statements: list[str] = []
    buffer: list[str] = []
    in_single = in_double = in_line_comment = in_block_comment = False
    index, length = 0, len(sql)
    while index < length:
        char = sql[index]
        nxt = sql[index + 1] if index + 1 < length else ""
        if in_line_comment:
            buffer.append(char)
            if char == "\n":
                in_line_comment = False
        elif in_block_comment:
            buffer.append(char)
            if char == "*" and nxt == "/":
                buffer.append(nxt)
                index += 1
                in_block_comment = False
        elif in_single:
            buffer.append(char)
            if char == "'":
                if nxt == "'":  # SQL 里的 '' 转义
                    buffer.append(nxt)
                    index += 1
                else:
                    in_single = False
        elif in_double:
            buffer.append(char)
            if char == '"':
                if nxt == '"':
                    buffer.append(nxt)
                    index += 1
                else:
                    in_double = False
        elif char == "-" and nxt == "-":
            buffer.append(char)
            buffer.append(nxt)
            index += 1
            in_line_comment = True
        elif char == "/" and nxt == "*":
            buffer.append(char)
            buffer.append(nxt)
            index += 1
            in_block_comment = True
        elif char == "'":
            buffer.append(char)
            in_single = True
        elif char == '"':
            buffer.append(char)
            in_double = True
        elif char == ";":
            statement = "".join(buffer)
            if _has_sql(statement):
                statements.append(statement.strip())
            buffer = []
        else:
            buffer.append(char)
        index += 1
    tail = "".join(buffer)
    if _has_sql(tail):
        statements.append(tail.strip())
    return statements


def _has_sql(statement: str) -> bool:
    """去掉注释与空白后是否还有真正的 SQL 文本。"""
    stripped = re.sub(r"/\*.*?\*/", "", statement, flags=re.S)
    stripped = re.sub(r"--[^\n]*", "", stripped)
    return bool(stripped.strip())


# --------------------------------------------------------------------- 脚本发现
def discover_sqlite_migrations(directory: str | Path | None = None) -> list[Migration]:
    """扫描 SQLite 迁移目录，返回按版本升序、正/反向脚本严格配对的清单。"""
    base = Path(directory) if directory is not None else default_sql_dir() / "migrations" / "sqlite"
    if not base.is_dir():
        raise MigrationNotFoundError(f"SQLite 迁移目录不存在: {base}")
    ups: dict[int, tuple[str, Path]] = {}
    downs: dict[int, tuple[str, Path]] = {}
    for path in sorted(base.glob("*.sql")):
        up_match = _UP_FILE_RE.match(path.name)
        down_match = _DOWN_FILE_RE.match(path.name)
        if up_match:
            version, name = int(up_match.group(1)), up_match.group(2)
            if version in ups:
                raise MigrationStateError(f"SQLite 迁移版本重复: {version}（{ups[version][1].name} / {path.name}）")
            ups[version] = (name, path)
        elif down_match:
            version, name = int(down_match.group(1)), down_match.group(2)
            if version in downs:
                raise MigrationStateError(f"SQLite 回滚版本重复: {version}（{downs[version][1].name} / {path.name}）")
            downs[version] = (name, path)
    if not ups:
        raise MigrationNotFoundError(f"SQLite 迁移目录没有任何 V*.sql: {base}")
    _check_pairing("SQLite", ups, downs)
    return [Migration(version=version, name=ups[version][0], up=ups[version][1], down=downs[version][1])
            for version in sorted(ups)]


def discover_postgres_migrations(
    sql_dir: str | Path | None = None, *, core_schema: str, runtime_schema: str,
) -> list[Migration]:
    """PostgreSQL 版本化清单：正向脚本沿用 ``sql/00x_*``，回滚脚本在 migrations/postgresql。

    V001/V002 属于领域核心 schema，V003 属于应用运行时 schema；两个 schema 各自记账。
    """
    root = Path(sql_dir) if sql_dir is not None else default_sql_dir()
    for schema in (core_schema, runtime_schema):
        if not _IDENT_RE.match(schema):
            raise MigrationStateError(f"非法 PostgreSQL schema 名: {schema!r}")
    specs = (
        (1, "core_schema", root / "001_core_schema_postgresql.sql",
         root / "migrations" / "postgresql" / "R001__core_schema.sql", core_schema),
        (2, "workflow_and_idem", root / "002_workflow_and_idem_postgresql.sql",
         root / "migrations" / "postgresql" / "R002__workflow_and_idem.sql", core_schema),
        (3, "repository_runtime", root / "003_repository_runtime_postgresql.sql",
         root / "migrations" / "postgresql" / "R003__repository_runtime.sql", runtime_schema),
    )
    migrations: list[Migration] = []
    for version, name, up, down, schema in specs:
        for path in (up, down):
            if not path.is_file():
                raise MigrationNotFoundError(f"PostgreSQL 迁移脚本不存在: {path}")
        migrations.append(Migration(version=version, name=name, up=up, down=down, schema=schema))
    return migrations


def _check_pairing(kind: str, ups: dict[int, tuple[str, Path]],
                   downs: dict[int, tuple[str, Path]]) -> None:
    missing_down = sorted(set(ups) - set(downs))
    missing_up = sorted(set(downs) - set(ups))
    problems: list[str] = []
    if missing_down:
        problems.append("缺回滚脚本的版本: " + ", ".join(f"{v:03d}" for v in missing_down))
    if missing_up:
        problems.append("缺正向脚本的版本: " + ", ".join(f"{v:03d}" for v in missing_up))
    for version in sorted(set(ups) & set(downs)):
        if ups[version][0] != downs[version][0]:
            problems.append(
                f"V{version:03d} 名称 {ups[version][0]!r} 与 R{version:03d} 名称 "
                f"{downs[version][0]!r} 不一致")
    if problems:
        raise MigrationNotFoundError(f"{kind} 迁移脚本配对失败: " + "；".join(problems))


# --------------------------------------------------------------------- 执行器基类
class _MigrationRunner:
    """正/反向流程的公共实现，方言差异由子类钩子承担。"""

    backend = "abstract"

    def __init__(self, migrations: Iterable[Migration], *, ledger: str = SCHEMA_VERSION_TABLE) -> None:
        if not _IDENT_RE.match(ledger):
            raise MigrationStateError(f"非法账本表名: {ledger!r}")
        self.ledger = ledger
        ordered = sorted(migrations, key=lambda item: item.version)
        seen: set[int] = set()
        for migration in ordered:
            if migration.version in seen:
                raise MigrationStateError(f"迁移版本重复: {migration.version}")
            seen.add(migration.version)
            for path in (migration.up, migration.down):
                if not path.is_file():
                    raise MigrationNotFoundError(f"迁移脚本不存在: {path}")
        self._migrations = tuple(ordered)

    # ---------------------------------------------------------------- 查询
    def available(self) -> tuple[Migration, ...]:
        return self._migrations

    def versions(self) -> list[int]:
        return [migration.version for migration in self._migrations]

    def latest_version(self) -> int:
        return self._migrations[-1].version if self._migrations else 0

    def applied(self) -> list[dict[str, Any]]:
        return sorted(self._applied_rows(), key=lambda row: row["version"])

    def current_version(self) -> int:
        rows = self._applied_rows()
        return max((row["version"] for row in rows), default=0)

    def pending(self) -> list[int]:
        applied = {row["version"] for row in self._applied_rows()}
        return [m.version for m in self._migrations if m.version not in applied]

    def status(self) -> dict[str, Any]:
        rows = self._applied_rows()
        applied = {row["version"] for row in rows}
        return {
            "backend": self.backend,
            "ledger": self.ledger,
            "latest_version": self.latest_version(),
            "current_version": max(applied, default=0),
            "applied": sorted(rows, key=lambda row: row["version"]),
            "pending": [m.version for m in self._migrations if m.version not in applied],
        }

    # ---------------------------------------------------------------- 正向
    def upgrade(self, target: int | None = None) -> list[int]:
        """升级到 target（默认最新）。返回本次真正执行的版本号列表。"""
        latest = self.latest_version()
        resolved = latest if target is None else int(target)
        if resolved < 0 or resolved > latest:
            raise MigrationStateError(f"目标版本越界: {resolved}（可用范围 0..{latest}）")
        rows = self._applied_rows()
        self._validate_applied(rows)
        current = max((row["version"] for row in rows), default=0)
        if resolved < current:
            raise MigrationStateError(
                f"目标版本 {resolved} 低于当前版本 {current}；降级请用 downgrade(target=...)")
        applied_versions = {row["version"] for row in rows}
        executed: list[int] = []
        for migration in self._migrations:
            if migration.version > resolved or migration.version in applied_versions:
                continue
            if self._apply(migration):
                executed.append(migration.version)
                applied_versions.add(migration.version)
        return executed

    # ---------------------------------------------------------------- 反向
    def downgrade(self, target: int = 0) -> list[int]:
        """回滚到 target（默认 0 = 全部）。返回本次真正回滚的版本号列表。"""
        resolved = int(target)
        if resolved < 0:
            raise MigrationStateError(f"目标版本非法: {resolved}")
        rows = self._applied_rows()
        if not rows:
            return []
        self._validate_applied(rows)
        current = max(row["version"] for row in rows)
        if resolved > current:
            raise MigrationStateError(
                f"目标版本 {resolved} 高于当前版本 {current}；升级请用 upgrade(target=...)")
        by_version = {migration.version: migration for migration in self._migrations}
        rolled_back: list[int] = []
        for row in sorted(rows, key=lambda item: item["version"], reverse=True):
            if row["version"] <= resolved:
                continue
            migration = by_version[row["version"]]
            if self._revert(migration):
                rolled_back.append(migration.version)
        return rolled_back

    # ---------------------------------------------------------------- 校验
    def _validate_applied(self, rows: Iterable[dict[str, Any]]) -> None:
        by_version = {migration.version: migration for migration in self._migrations}
        for row in rows:
            version = row["version"]
            migration = by_version.get(version)
            if migration is None:
                raise MigrationStateError(
                    f"账本登记了不存在的版本 {version}（当前脚本最高 {self.latest_version()}）")
            if row["name"] != migration.name:
                raise MigrationStateError(
                    f"V{version:03d} 账本名称 {row['name']!r} 与脚本名称 {migration.name!r} 不一致")
            if row["checksum"] != migration.checksum:
                raise MigrationChecksumError(
                    f"{migration.label} 已登记但正向脚本内容变化"
                    f"（登记 {row['checksum'][:12]}…，当前 {migration.checksum[:12]}…）；"
                    "历史脚本只读，请新增迁移版本而不是改已发布脚本")
            if row["rollback_checksum"] != migration.rollback_checksum:
                raise MigrationChecksumError(
                    f"R{version:03d}__{migration.name} 已登记但回滚脚本内容变化"
                    f"（登记 {row['rollback_checksum'][:12]}…，当前 "
                    f"{migration.rollback_checksum[:12]}…）；请新增迁移版本")

    # ---------------------------------------------------------------- 方言钩子
    def _applied_rows(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _apply(self, migration: Migration) -> bool:
        raise NotImplementedError

    def _revert(self, migration: Migration) -> bool:
        raise NotImplementedError


# --------------------------------------------------------------------- SQLite
class SqliteMigrationRunner(_MigrationRunner):
    """SQLite 版本化迁移：单库单账本，每个版本 ``BEGIN IMMEDIATE`` 事务。"""

    backend = "sqlite"

    def __init__(self, path: str | Path, *, directory: str | Path | None = None,
                 migrations: Iterable[Migration] | None = None,
                 ledger: str = SCHEMA_VERSION_TABLE) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if migrations is None:
            migrations = discover_sqlite_migrations(directory)
        super().__init__(migrations, ledger=ledger)

    # ---------------------------------------------------------------- 连接
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _ledger_exists(self, connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (self.ledger,)).fetchone()
        return row is not None

    def _ensure_ledger(self, connection: sqlite3.Connection) -> None:
        connection.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.ledger}(
                version           INTEGER PRIMARY KEY,
                name              TEXT    NOT NULL UNIQUE,
                checksum          TEXT    NOT NULL,
                rollback_checksum TEXT    NOT NULL,
                applied_at        TEXT    NOT NULL,
                execution_ms      INTEGER NOT NULL)""")

    # ---------------------------------------------------------------- 查询
    def _applied_rows(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            if not self._ledger_exists(connection):
                return []
            rows = connection.execute(
                f"SELECT version,name,checksum,rollback_checksum,applied_at,execution_ms "
                f"FROM {self.ledger} ORDER BY version").fetchall()
        finally:
            connection.close()
        keys = ("version", "name", "checksum", "rollback_checksum", "applied_at", "execution_ms")
        return [dict(zip(keys, row)) for row in rows]

    # ---------------------------------------------------------------- 执行
    def _apply(self, migration: Migration) -> bool:
        connection = self._connect()
        try:
            self._ensure_ledger(connection)
            started = time.perf_counter()
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    f"SELECT 1 FROM {self.ledger} WHERE version=?", (migration.version,)).fetchone()
                if existing is not None:  # 并发迁移者已抢先登记
                    connection.execute("COMMIT")
                    return False
                self._run_script(connection, migration.read_up())
                elapsed = max(0, int((time.perf_counter() - started) * 1000))
                connection.execute(
                    f"INSERT INTO {self.ledger}"
                    "(version,name,checksum,rollback_checksum,applied_at,execution_ms) "
                    "VALUES(?,?,?,?,?,?)",
                    (migration.version, migration.name, migration.checksum,
                     migration.rollback_checksum, now_iso(), elapsed))
                connection.execute("COMMIT")
            except Exception as exc:  # noqa: BLE001 - 统一转成迁移错误
                connection.execute("ROLLBACK")
                raise MigrationError(
                    f"执行迁移 {migration.label} 失败: {exc}") from exc
        finally:
            connection.close()
        return True

    def _revert(self, migration: Migration) -> bool:
        connection = self._connect()
        try:
            self._ensure_ledger(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    f"SELECT 1 FROM {self.ledger} WHERE version=?", (migration.version,)).fetchone()
                if existing is None:  # 已被其它进程回滚
                    connection.execute("COMMIT")
                    return False
                self._run_script(connection, migration.read_down())
                connection.execute(
                    f"DELETE FROM {self.ledger} WHERE version=?", (migration.version,))
                connection.execute("COMMIT")
            except Exception as exc:  # noqa: BLE001
                connection.execute("ROLLBACK")
                raise MigrationError(
                    f"回滚 {migration.label} 失败: {exc}") from exc
        finally:
            connection.close()
        return True

    @staticmethod
    def _run_script(connection: sqlite3.Connection, sql: str) -> None:
        statements = split_sql_statements(sql)
        if not statements:
            raise MigrationError("迁移脚本没有任何可执行语句")
        for statement in statements:
            connection.execute(statement)


# --------------------------------------------------------------------- PostgreSQL
class PostgresMigrationRunner(_MigrationRunner):
    """PostgreSQL 版本化迁移：core / runtime 两个 schema 各一张 ``schema_version``。"""

    backend = "postgresql"

    def __init__(self, database: Any, *, migrations: Iterable[Migration] | None = None,
                 sql_dir: str | Path | None = None,
                 ledger: str = SCHEMA_VERSION_TABLE) -> None:
        self.database = database
        if migrations is None:
            migrations = discover_postgres_migrations(
                sql_dir, core_schema=database.core_schema, runtime_schema=database.runtime_schema)
        super().__init__(migrations, ledger=ledger)
        self._schemas = tuple(dict.fromkeys(
            migration.schema for migration in self._migrations if migration.schema))
        if len(self._schemas) != len({migration.schema for migration in self._migrations}):
            raise MigrationStateError("PostgreSQL 迁移缺少目标 schema")
        for schema in self._schemas:
            if not _IDENT_RE.match(schema):
                raise MigrationStateError(f"非法 PostgreSQL schema 名: {schema!r}")

    # ---------------------------------------------------------------- 查询
    def _applied_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for schema in self._schemas:
            connection = self.database.connect_schema(schema)
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT to_regclass(%s)", (f"{schema}.{self.ledger}",))
                    if cursor.fetchone()[0] is None:
                        continue
                    cursor.execute(
                        f"SELECT version,name,checksum,rollback_checksum,applied_at,execution_ms "
                        f"FROM {schema}.{self.ledger} ORDER BY version")
                    for version, name, checksum, rollback_checksum, applied_at, elapsed in cursor.fetchall():
                        rows.append({"version": version, "name": name, "checksum": checksum,
                                     "rollback_checksum": rollback_checksum,
                                     "applied_at": _iso(applied_at), "execution_ms": elapsed,
                                     "schema": schema})
            finally:
                connection.close()
        return rows

    # ---------------------------------------------------------------- 执行
    def _ledger_ddl(self, schema: str) -> str:
        return (f"CREATE TABLE IF NOT EXISTS {schema}.{self.ledger}("
                "version INTEGER PRIMARY KEY,"
                "name TEXT NOT NULL UNIQUE,"
                "checksum TEXT NOT NULL,"
                "rollback_checksum TEXT NOT NULL,"
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
                "execution_ms INTEGER NOT NULL)")

    def _apply(self, migration: Migration) -> bool:
        schema = migration.schema or self._schemas[0]
        connection = self.database.connect_schema(schema)
        try:
            connection.autocommit = False
            with connection.cursor() as cursor:
                try:
                    cursor.execute(self._ledger_ddl(schema))
                    cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                                   (_PG_MIGRATION_LOCK,))
                    cursor.execute(
                        f"SELECT 1 FROM {schema}.{self.ledger} WHERE version=%s",
                        (migration.version,))
                    if cursor.fetchone() is not None:  # 并发迁移者已抢先登记
                        connection.commit()
                        return False
                    started = time.perf_counter()
                    cursor.execute(migration.read_up())
                    elapsed = max(0, int((time.perf_counter() - started) * 1000))
                    cursor.execute(
                        f"INSERT INTO {schema}.{self.ledger}"
                        "(version,name,checksum,rollback_checksum,applied_at,execution_ms) "
                        "VALUES(%s,%s,%s,%s,now(),%s)",
                        (migration.version, migration.name, migration.checksum,
                         migration.rollback_checksum, elapsed))
                    connection.commit()
                except Exception as exc:  # noqa: BLE001
                    connection.rollback()
                    raise MigrationError(
                        f"执行迁移 {migration.label}（schema={schema}）失败: {exc}") from exc
        finally:
            connection.close()
        return True

    def _revert(self, migration: Migration) -> bool:
        schema = migration.schema or self._schemas[0]
        connection = self.database.connect_schema(schema)
        try:
            connection.autocommit = False
            with connection.cursor() as cursor:
                try:
                    cursor.execute(self._ledger_ddl(schema))
                    cursor.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                                   (_PG_MIGRATION_LOCK,))
                    cursor.execute(
                        f"SELECT 1 FROM {schema}.{self.ledger} WHERE version=%s",
                        (migration.version,))
                    if cursor.fetchone() is None:  # 已被其它进程回滚
                        connection.commit()
                        return False
                    cursor.execute(migration.read_down())
                    cursor.execute(
                        f"DELETE FROM {schema}.{self.ledger} WHERE version=%s",
                        (migration.version,))
                    connection.commit()
                except Exception as exc:  # noqa: BLE001
                    connection.rollback()
                    raise MigrationError(
                        f"回滚 {migration.label}（schema={schema}）失败: {exc}") from exc
        finally:
            connection.close()
        return True


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)
