"""SQLite Repository 实现（V4.2.3 / P1-02）。

全部 SQL 只出现在本文件与 ``schema.py``；``txbuddy.store`` 只做领域校验与委托。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
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
from .schema import SqliteDatabase, now_iso

Connect = Callable[[], Any]


class SqliteRepository:
    """SQLite 仓储基类：持有连接工厂，只在调用期开连接（连接不可跨线程复用）。"""

    backend = "sqlite"

    def __init__(self, connect: Connect) -> None:
        self._connect = connect

    def health(self) -> bool:
        conn = self._connect()
        try:
            conn.execute("SELECT 1").fetchone()
            return True
        except Exception:  # noqa: BLE001 - 探针不抛异常
            return False
        finally:
            conn.close()


class SqliteEvidenceRepository(SqliteRepository, EvidenceRepository):
    def save(self, evidence: dict[str, Any]) -> None:
        from ..core.evidence import canonical

        conn = self._connect()
        try:
            conn.execute("INSERT OR REPLACE INTO evidence VALUES(?,?,?,?)",
                         (evidence["evidence_id"], evidence["sha256"],
                          canonical(evidence), evidence["created_at"]))
        finally:
            conn.close()

    def get(self, evidence_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT payload FROM evidence WHERE evidence_id=?",
                               (evidence_id,)).fetchone()
        finally:
            conn.close()
        return json.loads(row[0]) if row else None

    def count(self, job_id: str | None = None) -> int:
        conn = self._connect()
        try:
            if job_id:
                row = conn.execute("SELECT COUNT(*) FROM evidence WHERE payload LIKE ?",
                                   (f'%"job_id":"{job_id}"%',)).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM evidence").fetchone()
        finally:
            conn.close()
        return int(row[0])

    def list_by_job(self, job_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT evidence_id,sha256,payload,created_at FROM evidence "
                                "ORDER BY created_at,evidence_id").fetchall()
        finally:
            conn.close()
        out: list[dict[str, Any]] = []
        for evidence_id, sha256, payload, created_at in rows:
            try:
                body = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(body, dict) and body.get("job_id") == job_id:
                out.append({"evidence_id": evidence_id, "sha256": sha256,
                            "created_at": created_at, "payload": body})
        return out


class SqliteCalcRunRepository(SqliteRepository, CalcRunRepository):
    _COLUMNS = ("calc_run_id,job_id,rule_code,rule_version,basis_pack_id,snapshot_sha256,"
                "snapshot,result,unit,evidence_id,created_at")
    _KEYS = ["calc_run_id", "job_id", "rule_code", "rule_version", "basis_pack_id",
             "snapshot_sha256", "snapshot", "result", "unit", "evidence_id", "created_at"]

    def save(self, run: dict[str, Any]) -> None:
        conn = self._connect()
        try:
            conn.execute(
                f"INSERT OR REPLACE INTO calc_run({self._COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (run["calc_run_id"], run.get("job_id"), run["rule_code"], run["rule_version"],
                 run["basis_pack_id"], run["snapshot_sha256"],
                 json.dumps(run["snapshot"], ensure_ascii=False),
                 run["result"], run["unit"], run.get("evidence_id"), run["created_at"]))
        finally:
            conn.close()

    def get(self, calc_run_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(f"SELECT {self._COLUMNS} FROM calc_run WHERE calc_run_id=?",
                               (calc_run_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        out = dict(zip(self._KEYS, row))
        out["snapshot"] = json.loads(out["snapshot"])
        return out

    def list_by_job(self, job_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT calc_run_id,rule_code,result,unit,snapshot_sha256 FROM calc_run "
                "WHERE job_id=? ORDER BY created_at", (job_id,)).fetchall()
        finally:
            conn.close()
        return [{"calc_run_id": a, "rule_code": b, "result": c, "unit": d, "snapshot_sha256": e}
                for a, b, c, d, e in rows]


class SqliteAuditRepository(SqliteRepository, AuditRepository):
    _KEYS = ["actor", "action", "object_type", "object_id", "detail", "trace_id", "at"]

    def add(self, actor: str, action: str, object_type: str, object_id: str,
            detail: str = "", trace_id: str = "") -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO audit_log(actor,action,object_type,object_id,detail,trace_id,at) "
                "VALUES(?,?,?,?,?,?,?)",
                (actor, action, object_type, object_id, detail, trace_id, now_iso()))
        finally:
            conn.close()

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT actor,action,object_type,object_id,detail,trace_id,at FROM audit_log "
                "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        finally:
            conn.close()
        return [dict(zip(self._KEYS, row)) for row in rows]


class SqliteIdemRepository(SqliteRepository, IdemRepository):
    """幂等表：唯一裁判是数据库唯一约束，不是 Python 内存状态。

    * 认领成功的判据只有一条：``INSERT ... ON CONFLICT DO NOTHING`` 后 ``changes() == 1``；
    * 陈旧认领用 CAS（``UPDATE ... WHERE claimed_at=<旧值>`` 且 rowcount==1）接管。
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
            row = conn.execute("SELECT fingerprint, response, state FROM idem WHERE scope_key=?",
                               (scope_key,)).fetchone()
        finally:
            conn.close()
        if not row:
            return "miss", None
        row_fp, row_resp, row_state = row
        if row_fp != fingerprint:
            return "conflict", None
        if row_state != self.IDEM_STATE_DONE or row_resp is None:
            return "in_progress", None
        return "hit", json.loads(row_resp)

    def claim(self, scope_key: str, fingerprint: str, tenant: str = "", actor: str = "",
              ttl_seconds: float | None = None) -> tuple[str, dict[str, Any] | None]:
        """数据库级原子认领。

        返回 (state, cached)：
          "claimed"      本次请求拿到执行权，必须随后 finish 或 release
          "hit"          同键同指纹且已完成，cached 为历史响应（含 idem_replay=True）
          "conflict"     同键不同指纹
          "in_progress"  同键同指纹，另一请求正持有执行权
          "retry"        极端竞争下未判定出状态，调用方可重试
        """
        ttl = self.IDEM_CLAIM_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        for _ in range(5):
            now = now_iso()
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO idem(scope_key,fingerprint,state,response,tenant,actor,claimed_at,at) "
                    "VALUES(?,?,?,NULL,?,?,?,?) ON CONFLICT(scope_key) DO NOTHING",
                    (scope_key, fingerprint, self.IDEM_STATE_IN_PROGRESS, tenant, actor, now, now))
                # changes() 由 SQLite 计算，唯一裁判；不受 Python 层 rowcount 语义影响。
                won = int(conn.execute("SELECT changes()").fetchone()[0])
                if won == 1:
                    return "claimed", None
                row = conn.execute(
                    "SELECT fingerprint,state,response,claimed_at FROM idem WHERE scope_key=?",
                    (scope_key,)).fetchone()
                if row is None:
                    continue  # 行在读取前被其他请求释放，重试一次
                row_fp, row_state, row_resp, row_claimed = row
                if row_fp != fingerprint:
                    return "conflict", None
                if row_state == self.IDEM_STATE_DONE and row_resp is not None:
                    cached = json.loads(row_resp)
                    cached["idem_replay"] = True
                    return "hit", cached
                if self._takeover(scope_key, fingerprint, row_claimed, now, ttl):
                    return "claimed", None
                return "in_progress", None
            finally:
                conn.close()
        return "retry", None

    def _takeover(self, scope_key: str, fingerprint: str, claimed_at: str | None,
                  now: str, ttl_seconds: float) -> bool:
        """接管陈旧认领：CAS 更新 claimed_at，只有 rowcount==1 的请求能接管。"""
        if not claimed_at or ttl_seconds is None or ttl_seconds <= 0:
            return False
        try:
            ts = datetime.fromisoformat(claimed_at)
        except ValueError:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age < ttl_seconds:
            return False
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE idem SET claimed_at=?, state=?, response=NULL "
                "WHERE scope_key=? AND fingerprint=? AND state=? AND claimed_at=?",
                (now, self.IDEM_STATE_IN_PROGRESS, scope_key, fingerprint,
                 self.IDEM_STATE_IN_PROGRESS, claimed_at))
            return cur.rowcount == 1
        finally:
            conn.close()

    def finish(self, scope_key: str, response: Any, state: str | None = None) -> bool:
        """写回响应并置 DONE；只有持有 IN_PROGRESS 认领的一方能写。"""
        payload = json.dumps(response, ensure_ascii=False, default=str)
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE idem SET state=?, response=?, at=? WHERE scope_key=? AND state=?",
                (state or self.IDEM_STATE_DONE, payload, now_iso(), scope_key,
                 self.IDEM_STATE_IN_PROGRESS))
            return cur.rowcount == 1
        finally:
            conn.close()

    def release(self, scope_key: str) -> bool:
        """执行业务抛异常时释放认领，使同一幂等键可重试（不缓存失败）。"""
        conn = self._connect()
        try:
            cur = conn.execute("DELETE FROM idem WHERE scope_key=? AND state=?",
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


class SqliteJobRepository(SqliteRepository, JobRepository):
    _COLUMNS = ("job_id,project_id,name,business_line,basis_pack,status,created_by,created_at,"
                "basis_pack_id,updated_by,updated_at,revision,status_history")
    _KEYS = ["job_id", "project_id", "name", "business_line", "basis_pack", "status",
             "created_by", "created_at", "basis_pack_id", "updated_by", "updated_at",
             "revision", "status_history"]

    def create(self, *, job_id: str, project_id: str, name: str, business_line: str,
               basis_pack: dict[str, Any], status: str, created_by: str, created_at: str,
               basis_pack_id: str, updated_by: str, updated_at: str, revision: int,
               status_history: list[dict[str, Any]]) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO job(job_id,project_id,name,business_line,basis_pack,status,created_by,"
                "created_at,basis_pack_id,updated_by,updated_at,revision,status_history) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, project_id, name, business_line,
                 json.dumps(basis_pack, ensure_ascii=False), status, created_by, created_at,
                 basis_pack_id, updated_by, updated_at, revision,
                 json.dumps(status_history, ensure_ascii=False)))
        finally:
            conn.close()

    def get(self, job_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(f"SELECT {self._COLUMNS} FROM job WHERE job_id=?",
                               (job_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        out = dict(zip(self._KEYS, row))
        out["basis_pack"] = json.loads(out["basis_pack"])
        out["status_history"] = json.loads(out["status_history"] or "[]")
        return out

    def update_status(self, job_id: str, *, status: str, expected_status: str, by: str,
                      at: str, status_history: list[dict[str, Any]]) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE job SET status=?,updated_by=?,updated_at=?,"
                "revision=COALESCE(revision,1)+1,status_history=? "
                "WHERE job_id=? AND status=?",
                (status, by, at, json.dumps(status_history, ensure_ascii=False),
                 job_id, expected_status))
            return cur.rowcount == 1
        finally:
            conn.close()


class SqliteDocumentRepository(SqliteRepository, DocumentRepository):
    _KEYS = ["doc_key", "version", "file_sha256", "verified", "added_at"]

    def add(self, job_id: str, doc_key: str, version: str, file_sha256: str,
            verified: bool, added_at: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO job_document(job_id,doc_key,version,file_sha256,verified,added_at) "
                "VALUES(?,?,?,?,?,?)",
                (job_id, doc_key, version, file_sha256, 1 if verified else 0, added_at))
        finally:
            conn.close()

    def list_by_job(self, job_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT doc_key,version,file_sha256,verified,added_at FROM job_document "
                "WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
        finally:
            conn.close()
        return [dict(zip(self._KEYS, row)) for row in rows]


class SqliteTaskRepository(SqliteRepository, TaskRepository):
    _COLUMNS = ("task_id,job_id,node,status,payload,result,assignee,approver,approved_at,"
                "history,created_at,updated_at")
    _KEYS = ["task_id", "job_id", "node", "status", "payload", "result", "assignee",
             "approver", "approved_at", "history", "created_at", "updated_at"]

    def create(self, *, task_id: str, job_id: str, node: str, status: str,
               payload: dict[str, Any], assignee: str, history: list[dict[str, Any]],
               created_at: str, updated_at: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO job_task(task_id,job_id,node,status,payload,result,assignee,history,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (task_id, job_id, node, status, json.dumps(payload, ensure_ascii=False), None,
                 assignee, json.dumps(history, ensure_ascii=False), created_at, updated_at))
        finally:
            conn.close()

    def get(self, task_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(f"SELECT {self._COLUMNS} FROM job_task WHERE task_id=?",
                               (task_id,)).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        out = dict(zip(self._KEYS, row))
        out["payload"] = json.loads(out["payload"] or "{}")
        out["result"] = json.loads(out["result"]) if out["result"] else None
        out["history"] = json.loads(out["history"] or "[]")
        return out

    def update(self, task_id: str, *, status: str, result: dict[str, Any] | None,
               approver: str | None, approved_at: str | None,
               history: list[dict[str, Any]], updated_at: str) -> bool:
        conn = self._connect()
        try:
            cur = conn.execute(
                "UPDATE job_task SET status=?, result=COALESCE(?, result), "
                "approver=COALESCE(?, approver), approved_at=COALESCE(?, approved_at), "
                "history=?, updated_at=? WHERE task_id=?",
                (status,
                 json.dumps(result, ensure_ascii=False) if result is not None else None,
                 approver, approved_at, json.dumps(history, ensure_ascii=False),
                 updated_at, task_id))
            return cur.rowcount == 1
        finally:
            conn.close()

    def list_by_job(self, job_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT task_id FROM job_task WHERE job_id=? ORDER BY created_at",
                                (job_id,)).fetchall()
        finally:
            conn.close()
        return [self.get(row[0]) for row in rows]


class SqliteGateRepository(SqliteRepository, GateRepository):
    def add(self, job_id: str, gate_code: str, result: str,
            failed_rules: list[dict[str, Any]], judged_by: str, judged_at: str) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO gate_record(job_id,gate_code,result,failed_rules,judged_by,judged_at) "
                "VALUES(?,?,?,?,?,?)",
                (job_id, gate_code, result, json.dumps(failed_rules, ensure_ascii=False),
                 judged_by, judged_at))
        finally:
            conn.close()

    def list_by_job(self, job_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT gate_code,result,failed_rules,judged_by,judged_at FROM gate_record "
                "WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
        finally:
            conn.close()
        return [{"gate_code": a, "result": b, "failed_rules": json.loads(c),
                 "judged_by": d, "judged_at": e} for a, b, c, d, e in rows]


class SqliteRecordRepository(SqliteRepository, RecordRepository):
    def put(self, job_id: str, kind: str, payload: dict[str, Any], at: str) -> None:
        conn = self._connect()
        try:
            conn.execute("INSERT INTO job_record(job_id,kind,payload,at) VALUES(?,?,?,?)",
                         (job_id, kind, json.dumps(payload, ensure_ascii=False), at))
        finally:
            conn.close()

    def list_by_job(self, job_id: str, kind: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT payload FROM job_record WHERE job_id=? AND kind=? ORDER BY id",
                (job_id, kind)).fetchall()
        finally:
            conn.close()
        return [json.loads(row[0]) for row in rows]


class SqliteRepositorySet(RepositorySet):
    """共享同一 SQLite 数据库的一组仓储。"""

    backend = "sqlite"

    def __init__(self, database: SqliteDatabase) -> None:
        self.database = database
        # P1-05：用 connect_scoped 而不是 connect —— 事务体内全部仓储复用同一条连接，
        # 事务外行为与原来完全一致（每次调用开新连接）。
        connect = database.connect_scoped
        self.evidence = SqliteEvidenceRepository(connect)
        self.calc_run = SqliteCalcRunRepository(connect)
        self.audit = SqliteAuditRepository(connect)
        self.idem = SqliteIdemRepository(connect)
        self.jobs = SqliteJobRepository(connect)
        self.documents = SqliteDocumentRepository(connect)
        self.tasks = SqliteTaskRepository(connect)
        self.gates = SqliteGateRepository(connect)
        self.records = SqliteRecordRepository(connect)

    def unit_of_work(self, *, immediate: bool = True, label: str = "unit-of-work"):
        """事务边界：证据 + 复算 + 幂等响应同事务提交。"""
        return self.database.unit_of_work(immediate=immediate, label=label)


def build_sqlite_repositories(
    path: str = "data/txbuddy-dev.sqlite3", *, initialize: bool = True
) -> tuple[SqliteDatabase, SqliteRepositorySet]:
    """构造 SQLite 数据库与全部仓储（initialize=True 时顺带完成建表与旧库迁移）。"""
    database = SqliteDatabase(path)
    if initialize:
        database.initialize()
    return database, SqliteRepositorySet(database)
