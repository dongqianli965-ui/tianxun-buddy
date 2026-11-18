"""PostgreSQL Repository 实现（V4.2.3 / P1-03）。

与 ``sqlite.py`` **同接口、同语义**，由 ``base.py`` 的抽象类约束；业务层可整体替换后端。

数据库结构（三段脚本，见 ``sql/``，由 :meth:`PgDatabase.initialize` 真实执行）：

* ``001_core_schema_postgresql.sql`` —— 领域核心模型（BIGINT 代理键 + 外键 + NUMERIC），
  落在独立 schema（默认 ``txbuddy_core``），是生产目标的权威 DDL；
* ``002_workflow_and_idem_postgresql.sql`` —— 001 之上的工作流扩展（job/idem/
  job_document/job_record + calc_run 增列），与 001 同 schema；
* ``003_repository_runtime_postgresql.sql`` —— 应用运行时表（TEXT 业务键 + JSONB +
  TIMESTAMPTZ），列名/语义与 ``repository/schema.py`` 的 SQLite schema 一一对应，
  落在运行时 schema（默认 ``public``），是当前 API 直接读写的那一套。

为什么运行时表不能直接复用 001/002：001 的 evidence/calc_run 用 BIGINT 自增代理键，
而仓储接口用业务键（``evidence_id`` / ``calc_run_id`` / ``job_id``）寻址；002 的 job
又依赖 001 的 job_task(BIGINT)。两套模型并存会互相污染，所以 003 单独定义运行时表。
001/002 仍会被真实执行并登记在迁移账本里，保证领域核心 schema 与生产目标一致。

迁移账本（每个 schema 一张 ``schema_migration``）：
    文件名 + 内容 sha256 记账；重跑幂等；同名文件内容变化直接报错（禁止静默漂移）。
    ``001`` 不是幂等 DDL（没有 IF NOT EXISTS），账本正是它的重跑保护。

连接约定：
    * 每次方法调用开一条连接（连接不跨线程复用），``autocommit=True``；
    * 会话固定 ``TIME ZONE 'UTC'``，时间列读出后统一转成与 SQLite 完全相同的
      ISO8601 字符串（``+00:00``），因此同一套断言在两种后端上都成立；
    * JSON/JSONB 列用 ``%s::jsonb`` 传入 JSON 文本，读出由驱动反序列化为 dict/list。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .base import (
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
    build_idem_scope_key,
)
from .schema import now_iso

# ---------------------------------------------------------------- 常量与异常

DSN_ENV = "TXBUDDY_PG_DSN"
SQL_DIR_ENV = "TXBUDDY_SQL_DIR"
DEFAULT_RUNTIME_SCHEMA = "public"
DEFAULT_CORE_SCHEMA = "txbuddy_core"

CORE_SCRIPTS = ("001_core_schema_postgresql.sql", "002_workflow_and_idem_postgresql.sql")
RUNTIME_SCRIPTS = ("003_repository_runtime_postgresql.sql",)

# 003 必须产出的运行时表（与 schema.py 的 SQLite schema 一一对应）。
RUNTIME_TABLES = (
    "evidence",
    "calc_run",
    "audit_log",
    "idem",
    "job",
    "job_document",
    "job_task",
    "job_record",
    "gate_record",
)

# 001+002 必须产出的领域核心表（抽样校验关键表，避免把 24 张表硬编码成脆弱断言）。
CORE_TABLES = (
    "tenant",
    "project",
    "basis_pack",
    "calc_rule",
    "evidence",
    "project_job",
    "idem",
    "job",
    "job_document",
    "job_record",
)

_SCHEMA_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class PgError(RuntimeError):
    """PostgreSQL 仓储层错误的基类。"""


class PgConfigurationError(PgError):
    """DSN / schema 名 / 驱动缺失等配置错误。"""


class PgConnectionError(PgError):
    """连接失败（实例不可达、认证失败）。"""


class PgMigrationError(PgError):
    """迁移脚本缺失、执行失败或已登记内容发生变化。"""


# ---------------------------------------------------------------- 工具函数


def _iso(value: Any) -> str | None:
    """把驱动返回的 datetime 统一成 UTC ISO8601 字符串（与 SQLite 完全一致）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _dump(value: Any) -> str:
    """JSON 列的参数序列化（数据库端再 ::jsonb 解析，非法 JSON 会被 PG 拒绝）。"""
    return json.dumps(value, ensure_ascii=False, default=str)


def _default_sql_dir() -> Path:
    """``backend/txbuddy/repository/pg.py`` -> 项目根 ``sql/``。"""
    override = os.environ.get(SQL_DIR_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "sql"


def _dsn_from_environment() -> str | None:
    """优先 TXBUDDY_PG_DSN；退化时用 libpq 的 PG* 变量拼装（不读密码文件）。"""
    dsn = os.environ.get(DSN_ENV) or os.environ.get("TX_PG_DSN")
    if dsn:
        return dsn
    host = os.environ.get("PGHOST")
    database = os.environ.get("PGDATABASE")
    if not (host and database):
        return None
    user = os.environ.get("PGUSER") or "postgres"
    port = os.environ.get("PGPORT") or "5432"
    return f"postgresql://{user}@{host}:{port}/{database}"


def _ident(name: str, *, kind: str = "schema") -> str:
    """校验并返回可安全拼接的标识符（只允许小写字母/数字/下划线）。"""
    if not isinstance(name, str) or not _SCHEMA_RE.match(name):
        raise PgConfigurationError(
            f"非法 {kind} 名 {name!r}：只允许小写字母/数字/下划线，且以字母或下划线开头")
    return name


# ---------------------------------------------------------------- 数据库


class PgDatabase:
    """PostgreSQL 连接工厂 + schema 生命周期（对应 ``SqliteDatabase``）。"""

    backend = "postgresql"

    def __init__(
        self,
        dsn: str | None = None,
        *,
        runtime_schema: str = DEFAULT_RUNTIME_SCHEMA,
        core_schema: str = DEFAULT_CORE_SCHEMA,
        sql_dir: str | Path | None = None,
    ) -> None:
        resolved = dsn or _dsn_from_environment()
        if not resolved:
            raise PgConfigurationError(
                f"缺少 PostgreSQL 连接串：请设置 {DSN_ENV} 或传入 dsn（PG* 变量亦可）")
        self.dsn = resolved
        self.runtime_schema = _ident(runtime_schema, kind="runtime schema")
        self.core_schema = _ident(core_schema, kind="core schema")
        self.sql_dir = Path(sql_dir) if sql_dir is not None else _default_sql_dir()

    # --------------------------------------------------------- 连接
    def connect(self) -> Any:
        """打开一条指向运行时 schema 的连接（autocommit + UTC）。"""
        return self.connect_schema(self.runtime_schema)

    def connect_schema(self, schema: str) -> Any:
        """打开一条连接并把 search_path 固定到指定 schema。"""
        schema = _ident(schema)
        try:
            import psycopg2
        except ImportError as exc:  # pragma: no cover - 环境缺驱动时给出明确指引
            raise PgConfigurationError(
                "未安装 psycopg2：请执行 backend/.venv/Scripts/python.exe -m pip install psycopg2-binary"
            ) from exc
        try:
            conn = psycopg2.connect(self.dsn, connect_timeout=10)
        except Exception as exc:  # noqa: BLE001 - 统一转成可读的连接错误
            raise PgConnectionError(f"连接 PostgreSQL 失败: {exc}") from exc
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute(f"SET search_path TO {schema}")
        return conn

    def health(self) -> bool:
        try:
            conn = self.connect()
        except Exception:  # noqa: BLE001 - 探针不抛异常
            return False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone()[0] == 1
        except Exception:  # noqa: BLE001
            return False
        finally:
            conn.close()

    def server_info(self) -> dict[str, Any]:
        """一次往返拿到可用于探测证据的实例信息。"""
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT version(), current_database(), current_user, inet_server_port(), "
                    "current_setting('TimeZone')")
                version, database, user, port, tz = cur.fetchone()
        finally:
            conn.close()
        return {"version": version, "database": database, "user": user,
                "port": port, "timezone": tz, "runtime_schema": self.runtime_schema,
                "core_schema": self.core_schema}

    # --------------------------------------------------------- schema
    def tables(self, schema: str | None = None) -> set[str]:
        """列出指定 schema（默认运行时 schema）下的表名。"""
        schema = _ident(schema or self.runtime_schema)
        conn = self.connect_schema(schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name",
                    (schema,))
                return {row[0] for row in cur.fetchall()}
        finally:
            conn.close()

    def initialize(self) -> list[str]:
        """建 schema → 执行 001/002（core）与 003（runtime）→ 校验表齐全。

        返回本次**实际执行**的迁移列表（``schema.filename``）；已登记且 sha256 一致的
        文件会被跳过，因此重复调用是幂等的。
        """
        self._ensure_schema(self.runtime_schema)
        self._ensure_schema(self.core_schema)
        applied: list[str] = []
        applied.extend(self._migrate(self.runtime_schema, RUNTIME_SCRIPTS))
        applied.extend(self._migrate(self.core_schema, CORE_SCRIPTS))
        self.verify_schema()
        return applied

    def verify_schema(self) -> None:
        """校验运行时表与核心表都已就位（缺表即抛错，不静默降级）。"""
        missing_runtime = [name for name in RUNTIME_TABLES
                           if name not in self.tables(self.runtime_schema)]
        if missing_runtime:
            raise PgMigrationError(
                f"运行时 schema {self.runtime_schema} 缺表: {', '.join(missing_runtime)}")
        missing_core = [name for name in CORE_TABLES
                        if name not in self.tables(self.core_schema)]
        if missing_core:
            raise PgMigrationError(
                f"核心 schema {self.core_schema} 缺表: {', '.join(missing_core)}")

    def migration_log(self, schema: str | None = None) -> list[dict[str, Any]]:
        """读取迁移账本（按登记时间排序）。"""
        schema = _ident(schema or self.runtime_schema)
        conn = self.connect_schema(schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT name, sha256, applied_at FROM schema_migration ORDER BY applied_at, name")
                rows = cur.fetchall()
        finally:
            conn.close()
        return [{"name": name, "sha256": digest, "applied_at": _iso(applied_at)}
                for name, digest, applied_at in rows]

    def _ensure_schema(self, schema: str) -> None:
        conn = self.connect_schema(schema)
        try:
            with conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        finally:
            conn.close()

    def _migrate(self, schema: str, files: tuple[str, ...]) -> list[str]:
        applied: list[str] = []
        conn = self.connect_schema(schema)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migration("
                    "name TEXT PRIMARY KEY, sha256 TEXT NOT NULL, "
                    "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
                for name in files:
                    path = self.sql_dir / name
                    if not path.is_file():
                        raise PgMigrationError(f"迁移脚本不存在: {path}")
                    sql = path.read_text(encoding="utf-8")
                    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                    cur.execute("SELECT sha256 FROM schema_migration WHERE name=%s", (name,))
                    row = cur.fetchone()
                    if row is not None:
                        if row[0] != digest:
                            raise PgMigrationError(
                                f"{schema}.{name} 已登记但内容变化（登记 {row[0][:12]}…，"
                                f"当前 {digest[:12]}…）；请新增迁移而不是改历史脚本")
                        continue
                    try:
                        cur.execute(sql)
                        cur.execute(
                            "INSERT INTO schema_migration(name,sha256) VALUES(%s,%s)",
                            (name, digest))
                    except Exception as exc:  # noqa: BLE001
                        raise PgMigrationError(f"执行迁移 {schema}.{name} 失败: {exc}") from exc
                    applied.append(f"{schema}.{name}")
        finally:
            conn.close()
        return applied

    def __repr__(self) -> str:  # 不打印可能含密码的 DSN
        return (f"PgDatabase(runtime_schema={self.runtime_schema!r}, "
                f"core_schema={self.core_schema!r})")


# ---------------------------------------------------------------- 仓储基类


class PgRepository:
    """PostgreSQL 仓储基类：持有连接工厂，只在调用期开连接。"""

    backend = "postgresql"

    def __init__(self, connect: Callable[[], Any]) -> None:
        self._connect = connect

    def health(self) -> bool:
        try:
            conn = self._connect()
        except Exception:  # noqa: BLE001 - 探针不抛异常
            return False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone()[0] == 1
        except Exception:  # noqa: BLE001
            return False
        finally:
            conn.close()


# ---------------------------------------------------------------- Evidence


class PgEvidenceRepository(PgRepository, EvidenceRepository):
    def save(self, evidence: dict[str, Any]) -> None:
        from ..core.evidence import canonical

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO evidence(evidence_id,sha256,payload,created_at) "
                    "VALUES(%s,%s,%s::jsonb,%s::timestamptz) "
                    "ON CONFLICT (evidence_id) DO UPDATE SET "
                    "sha256=EXCLUDED.sha256, payload=EXCLUDED.payload, "
                    "created_at=EXCLUDED.created_at",
                    (evidence["evidence_id"], evidence["sha256"], canonical(evidence),
                     evidence["created_at"]))
        finally:
            conn.close()

    def get(self, evidence_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT payload FROM evidence WHERE evidence_id=%s",
                            (evidence_id,))
                row = cur.fetchone()
        finally:
            conn.close()
        return row[0] if row else None

    def count(self, job_id: str | None = None) -> int:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                if job_id:
                    cur.execute("SELECT COUNT(*) FROM evidence WHERE payload->>'job_id'=%s",
                                (job_id,))
                else:
                    cur.execute("SELECT COUNT(*) FROM evidence")
                return int(cur.fetchone()[0])
        finally:
            conn.close()

    def list_by_job(self, job_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT evidence_id,sha256,payload,created_at FROM evidence "
                    "WHERE payload->>'job_id'=%s ORDER BY created_at,evidence_id",
                    (job_id,))
                rows = cur.fetchall()
        finally:
            conn.close()
        out: list[dict[str, Any]] = []
        for evidence_id, sha256, payload, created_at in rows:
            body = payload
            if isinstance(payload, str):  # 防御：驱动未注册 jsonb 类型时仍是字符串
                try:
                    body = json.loads(payload)
                except (json.JSONDecodeError, TypeError):
                    continue
            if isinstance(body, dict) and body.get("job_id") == job_id:
                out.append({"evidence_id": evidence_id, "sha256": sha256,
                            "created_at": _iso(created_at), "payload": body})
        return out


# ---------------------------------------------------------------- CalcRun


class PgCalcRunRepository(PgRepository, CalcRunRepository):
    _COLUMNS = ("calc_run_id,job_id,rule_code,rule_version,basis_pack_id,snapshot_sha256,"
                "snapshot,result,unit,evidence_id,created_at")
    _KEYS = ["calc_run_id", "job_id", "rule_code", "rule_version", "basis_pack_id",
             "snapshot_sha256", "snapshot", "result", "unit", "evidence_id", "created_at"]

    def save(self, run: dict[str, Any]) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO calc_run(calc_run_id,job_id,rule_code,rule_version,"
                    "basis_pack_id,snapshot_sha256,snapshot,result,unit,evidence_id,created_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::timestamptz) "
                    "ON CONFLICT (calc_run_id) DO UPDATE SET "
                    "job_id=EXCLUDED.job_id, rule_code=EXCLUDED.rule_code, "
                    "rule_version=EXCLUDED.rule_version, basis_pack_id=EXCLUDED.basis_pack_id, "
                    "snapshot_sha256=EXCLUDED.snapshot_sha256, snapshot=EXCLUDED.snapshot, "
                    "result=EXCLUDED.result, unit=EXCLUDED.unit, "
                    "evidence_id=EXCLUDED.evidence_id, created_at=EXCLUDED.created_at",
                    (run["calc_run_id"], run.get("job_id"), run["rule_code"],
                     run["rule_version"], run["basis_pack_id"], run["snapshot_sha256"],
                     _dump(run["snapshot"]), run["result"], run["unit"],
                     run.get("evidence_id"), run["created_at"]))
        finally:
            conn.close()

    def get(self, calc_run_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {self._COLUMNS} FROM calc_run WHERE calc_run_id=%s",
                            (calc_run_id,))
                row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return None
        out = dict(zip(self._KEYS, row))
        out["created_at"] = _iso(out["created_at"])
        if isinstance(out["snapshot"], str):
            out["snapshot"] = json.loads(out["snapshot"])
        return out

    def list_by_job(self, job_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT calc_run_id,rule_code,result,unit,snapshot_sha256 FROM calc_run "
                    "WHERE job_id=%s ORDER BY created_at", (job_id,))
                rows = cur.fetchall()
        finally:
            conn.close()
        return [{"calc_run_id": a, "rule_code": b, "result": c, "unit": d, "snapshot_sha256": e}
                for a, b, c, d, e in rows]


# ---------------------------------------------------------------- Audit


class PgAuditRepository(PgRepository, AuditRepository):
    _KEYS = ["actor", "action", "object_type", "object_id", "detail", "trace_id", "at"]

    def add(self, actor: str, action: str, object_type: str, object_id: str,
            detail: str = "", trace_id: str = "") -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO audit_log(actor,action,object_type,object_id,detail,"
                    "trace_id,at) VALUES(%s,%s,%s,%s,%s,%s,%s::timestamptz)",
                    (actor, action, object_type, object_id, detail, trace_id, now_iso()))
        finally:
            conn.close()

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT actor,action,object_type,object_id,detail,trace_id,at "
                    "FROM audit_log ORDER BY id DESC LIMIT %s", (limit,))
                rows = cur.fetchall()
        finally:
            conn.close()
        out = [dict(zip(self._KEYS, row)) for row in rows]
        for row in out:
            row["at"] = _iso(row["at"])
        return out


# ---------------------------------------------------------------- Idem


class PgIdemRepository(PgRepository, IdemRepository):
    """幂等表：唯一裁判是 ``scope_key`` 主键约束（``ON CONFLICT DO NOTHING``）。

    与 SQLite 实现同语义，但认领裁判是 PostgreSQL 的行级唯一索引：并发 ``INSERT``
    同一 scope_key 只有一个事务拿到 ``RETURNING`` 行，其余阻塞到先提交后再判定。
    陈旧认领用 CAS（``UPDATE ... WHERE claimed_at=<旧值>`` 且 rowcount==1）接管。
    """

    IDEM_STATE_IN_PROGRESS = "IN_PROGRESS"
    IDEM_STATE_DONE = "DONE"
    IDEM_CLAIM_TTL_SECONDS = 300.0  # 认领超时（进程崩溃后的接管窗口）

    @staticmethod
    def scope_key(tenant: str, actor: str, method: str, path: str, key: str) -> str:
        return build_idem_scope_key(tenant, actor, method, path, key)

    def lookup(self, scope_key: str, fingerprint: str) -> tuple[str, dict[str, Any] | None]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT fingerprint,response,state FROM idem WHERE scope_key=%s",
                    (scope_key,))
                row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return "miss", None
        row_fp, row_resp, row_state = row
        if row_fp != fingerprint:
            return "conflict", None
        if row_state != self.IDEM_STATE_DONE or row_resp is None:
            return "in_progress", None
        return "hit", row_resp

    def claim(self, scope_key: str, fingerprint: str, tenant: str = "", actor: str = "",
              ttl_seconds: float | None = None) -> tuple[str, dict[str, Any] | None]:
        """数据库级原子认领（返回语义与 SQLite 完全一致）。"""
        ttl = self.IDEM_CLAIM_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        for _ in range(5):
            now = now_iso()
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO idem(scope_key,fingerprint,state,response,tenant,actor,"
                        "claimed_at,at) VALUES(%s,%s,%s,NULL,%s,%s,%s::timestamptz,"
                        "%s::timestamptz) ON CONFLICT (scope_key) DO NOTHING "
                        "RETURNING scope_key",
                        (scope_key, fingerprint, self.IDEM_STATE_IN_PROGRESS, tenant, actor,
                         now, now))
                    if cur.fetchone() is not None:
                        return "claimed", None
                    cur.execute(
                        "SELECT fingerprint,state,response,claimed_at FROM idem "
                        "WHERE scope_key=%s", (scope_key,))
                    row = cur.fetchone()
                    if row is None:
                        continue  # 行在读取前被其他请求释放，重试一次
                    row_fp, row_state, row_resp, row_claimed = row
                    if row_fp != fingerprint:
                        return "conflict", None
                    if row_state == self.IDEM_STATE_DONE and row_resp is not None:
                        cached = dict(row_resp)
                        cached["idem_replay"] = True
                        return "hit", cached
                    if self._takeover(scope_key, fingerprint, row_claimed, now, ttl):
                        return "claimed", None
                    return "in_progress", None
            finally:
                conn.close()
        return "retry", None

    def _takeover(self, scope_key: str, fingerprint: str, claimed_at: Any,
                  now: str, ttl_seconds: float) -> bool:
        """接管陈旧认领：CAS 更新 claimed_at，只有 rowcount==1 的请求能接管。"""
        if claimed_at is None or ttl_seconds is None or ttl_seconds <= 0:
            return False
        if not isinstance(claimed_at, datetime):
            try:
                claimed_at = datetime.fromisoformat(str(claimed_at))
            except ValueError:
                return False
        if claimed_at.tzinfo is None:
            claimed_at = claimed_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - claimed_at).total_seconds()
        if age < ttl_seconds:
            return False
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE idem SET claimed_at=%s::timestamptz, state=%s, response=NULL "
                    "WHERE scope_key=%s AND fingerprint=%s AND state=%s "
                    "AND claimed_at=%s::timestamptz",
                    (now, self.IDEM_STATE_IN_PROGRESS, scope_key, fingerprint,
                     self.IDEM_STATE_IN_PROGRESS, claimed_at))
                return cur.rowcount == 1
        finally:
            conn.close()

    def finish(self, scope_key: str, response: Any, state: str | None = None) -> bool:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE idem SET state=%s, response=%s::jsonb, at=%s::timestamptz "
                    "WHERE scope_key=%s AND state=%s",
                    (state or self.IDEM_STATE_DONE, _dump(response), now_iso(),
                     scope_key, self.IDEM_STATE_IN_PROGRESS))
                return cur.rowcount == 1
        finally:
            conn.close()

    def release(self, scope_key: str) -> bool:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM idem WHERE scope_key=%s AND state=%s",
                            (scope_key, self.IDEM_STATE_IN_PROGRESS))
                return cur.rowcount == 1
        finally:
            conn.close()

    def store(self, scope_key: str, fingerprint: str, response: dict[str, Any],
              tenant: str = "", actor: str = "") -> dict[str, Any]:
        state, _cached = self.claim(scope_key, fingerprint, tenant, actor)
        if state == "claimed":
            self.finish(scope_key, response)
            return response
        state2, cached = self.lookup(scope_key, fingerprint)
        return cached if state2 == "hit" and cached is not None else response


# ---------------------------------------------------------------- Job


class PgJobRepository(PgRepository, JobRepository):
    _COLUMNS = ("job_id,project_id,name,business_line,basis_pack,status,created_by,"
                "created_at,basis_pack_id,updated_by,updated_at,revision,status_history")
    _KEYS = ["job_id", "project_id", "name", "business_line", "basis_pack", "status",
             "created_by", "created_at", "basis_pack_id", "updated_by", "updated_at",
             "revision", "status_history"]

    def create(self, *, job_id: str, project_id: str, name: str, business_line: str,
               basis_pack: dict[str, Any], status: str, created_by: str, created_at: str,
               basis_pack_id: str, updated_by: str, updated_at: str, revision: int,
               status_history: list[dict[str, Any]]) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO job(job_id,project_id,name,business_line,basis_pack,status,"
                    "created_by,created_at,basis_pack_id,updated_by,updated_at,revision,"
                    "status_history) VALUES(%s,%s,%s,%s,%s::jsonb,%s,%s,"
                    "%s::timestamptz,%s,%s,%s::timestamptz,%s,%s::jsonb)",
                    (job_id, project_id, name, business_line, _dump(basis_pack), status,
                     created_by, created_at, basis_pack_id, updated_by, updated_at, revision,
                     _dump(status_history)))
        finally:
            conn.close()

    def get(self, job_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {self._COLUMNS} FROM job WHERE job_id=%s", (job_id,))
                row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return None
        out = dict(zip(self._KEYS, row))
        out["created_at"] = _iso(out["created_at"])
        out["updated_at"] = _iso(out["updated_at"])
        if isinstance(out["basis_pack"], str):
            out["basis_pack"] = json.loads(out["basis_pack"])
        if isinstance(out["status_history"], str):
            out["status_history"] = json.loads(out["status_history"] or "[]")
        return out

    def update_status(self, job_id: str, *, status: str, expected_status: str, by: str,
                      at: str, status_history: list[dict[str, Any]]) -> bool:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE job SET status=%s,updated_by=%s,updated_at=%s::timestamptz,"
                    "revision=COALESCE(revision,1)+1,status_history=%s::jsonb "
                    "WHERE job_id=%s AND status=%s",
                    (status, by, at, _dump(status_history), job_id, expected_status))
                return cur.rowcount == 1
        finally:
            conn.close()


# ---------------------------------------------------------------- Document


class PgDocumentRepository(PgRepository, DocumentRepository):
    _KEYS = ["doc_key", "version", "file_sha256", "verified", "added_at"]

    def add(self, job_id: str, doc_key: str, version: str, file_sha256: str,
            verified: bool, added_at: str) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO job_document(job_id,doc_key,version,file_sha256,verified,"
                    "added_at) VALUES(%s,%s,%s,%s,%s,%s::timestamptz)",
                    (job_id, doc_key, version, file_sha256, bool(verified), added_at))
        finally:
            conn.close()

    def list_by_job(self, job_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT doc_key,version,file_sha256,verified,added_at FROM job_document "
                    "WHERE job_id=%s ORDER BY id", (job_id,))
                rows = cur.fetchall()
        finally:
            conn.close()
        out = [dict(zip(self._KEYS, row)) for row in rows]
        for row in out:
            row["verified"] = int(bool(row["verified"]))  # 与 SQLite 的 0/1 完全一致
            row["added_at"] = _iso(row["added_at"])
        return out


# ---------------------------------------------------------------- Task


class PgTaskRepository(PgRepository, TaskRepository):
    _COLUMNS = ("task_id,job_id,node,status,payload,result,assignee,approver,approved_at,"
                "history,created_at,updated_at")
    _KEYS = ["task_id", "job_id", "node", "status", "payload", "result", "assignee",
             "approver", "approved_at", "history", "created_at", "updated_at"]

    def create(self, *, task_id: str, job_id: str, node: str, status: str,
               payload: dict[str, Any], assignee: str, history: list[dict[str, Any]],
               created_at: str, updated_at: str) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO job_task(task_id,job_id,node,status,payload,result,assignee,"
                    "history,created_at,updated_at) VALUES(%s,%s,%s,%s,%s::jsonb,NULL,%s,"
                    "%s::jsonb,%s::timestamptz,%s::timestamptz)",
                    (task_id, job_id, node, status, _dump(payload), assignee,
                     _dump(history), created_at, updated_at))
        finally:
            conn.close()

    def get(self, task_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {self._COLUMNS} FROM job_task WHERE task_id=%s",
                            (task_id,))
                row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return None
        out = dict(zip(self._KEYS, row))
        if isinstance(out["payload"], str):
            out["payload"] = json.loads(out["payload"] or "{}")
        if isinstance(out["history"], str):
            out["history"] = json.loads(out["history"] or "[]")
        out["approved_at"] = _iso(out["approved_at"])
        out["created_at"] = _iso(out["created_at"])
        out["updated_at"] = _iso(out["updated_at"])
        return out

    def update(self, task_id: str, *, status: str, result: dict[str, Any] | None,
               approver: str | None, approved_at: str | None,
               history: list[dict[str, Any]], updated_at: str) -> bool:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE job_task SET status=%s, result=COALESCE(%s::jsonb, result), "
                    "approver=COALESCE(%s::text, approver), "
                    "approved_at=COALESCE(%s::timestamptz, approved_at), "
                    "history=%s::jsonb, updated_at=%s::timestamptz WHERE task_id=%s",
                    (status, _dump(result) if result is not None else None,
                     approver, approved_at, _dump(history), updated_at, task_id))
                return cur.rowcount == 1
        finally:
            conn.close()

    def list_by_job(self, job_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT task_id FROM job_task WHERE job_id=%s ORDER BY created_at",
                            (job_id,))
                rows = cur.fetchall()
        finally:
            conn.close()
        return [task for task in (self.get(row[0]) for row in rows) if task is not None]


# ---------------------------------------------------------------- Gate


class PgGateRepository(PgRepository, GateRepository):
    def add(self, job_id: str, gate_code: str, result: str,
            failed_rules: list[dict[str, Any]], judged_by: str, judged_at: str) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO gate_record(job_id,gate_code,result,failed_rules,judged_by,"
                    "judged_at) VALUES(%s,%s,%s,%s::jsonb,%s,%s::timestamptz)",
                    (job_id, gate_code, result, _dump(failed_rules), judged_by, judged_at))
        finally:
            conn.close()

    def list_by_job(self, job_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT gate_code,result,failed_rules,judged_by,judged_at FROM gate_record "
                    "WHERE job_id=%s ORDER BY id", (job_id,))
                rows = cur.fetchall()
        finally:
            conn.close()
        out = []
        for gate_code, result, failed_rules, judged_by, judged_at in rows:
            if isinstance(failed_rules, str):
                failed_rules = json.loads(failed_rules)
            out.append({"gate_code": gate_code, "result": result,
                        "failed_rules": failed_rules, "judged_by": judged_by,
                        "judged_at": _iso(judged_at)})
        return out


# ---------------------------------------------------------------- Record


class PgRecordRepository(PgRepository, RecordRepository):
    def put(self, job_id: str, kind: str, payload: dict[str, Any], at: str) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO job_record(job_id,kind,payload,at) "
                    "VALUES(%s,%s,%s::jsonb,%s::timestamptz)",
                    (job_id, kind, _dump(payload), at))
        finally:
            conn.close()

    def list_by_job(self, job_id: str, kind: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT payload FROM job_record WHERE job_id=%s AND kind=%s ORDER BY id",
                    (job_id, kind))
                rows = cur.fetchall()
        finally:
            conn.close()
        return [json.loads(row[0]) if isinstance(row[0], str) else row[0] for row in rows]


# ---------------------------------------------------------------- 仓储集合


class PgRepositorySet(RepositorySet):
    """共享同一 PostgreSQL 连接工厂的一组仓储（对应 ``SqliteRepositorySet``）。"""

    backend = "postgresql"

    def __init__(self, database: PgDatabase) -> None:
        self.database = database
        connect = database.connect
        self.evidence = PgEvidenceRepository(connect)
        self.calc_run = PgCalcRunRepository(connect)
        self.audit = PgAuditRepository(connect)
        self.idem = PgIdemRepository(connect)
        self.jobs = PgJobRepository(connect)
        self.documents = PgDocumentRepository(connect)
        self.tasks = PgTaskRepository(connect)
        self.gates = PgGateRepository(connect)
        self.records = PgRecordRepository(connect)


def build_pg_repositories(
    dsn: str | None = None,
    *,
    runtime_schema: str = DEFAULT_RUNTIME_SCHEMA,
    core_schema: str = DEFAULT_CORE_SCHEMA,
    sql_dir: str | Path | None = None,
    initialize: bool = True,
) -> tuple[PgDatabase, PgRepositorySet]:
    """构造 PostgreSQL 数据库与全部仓储（initialize=True 时执行 001/002/003 迁移）。

    连接串优先级：显式 ``dsn`` > 环境变量 ``TXBUDDY_PG_DSN`` > ``PG*`` 变量拼装。
    """
    database = PgDatabase(dsn, runtime_schema=runtime_schema, core_schema=core_schema,
                          sql_dir=sql_dir)
    if initialize:
        database.initialize()
    return database, PgRepositorySet(database)
