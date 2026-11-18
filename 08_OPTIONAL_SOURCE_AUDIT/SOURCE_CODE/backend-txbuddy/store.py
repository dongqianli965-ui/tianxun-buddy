"""SQLite 原型存储（V4.2）：证据、计算运行、审计、幂等、业务对象与门禁记录。

P1-02：所有 SQL 已下沉到 ``txbuddy.repository``（抽象接口见 ``repository/base.py``，
SQLite 实现见 ``repository/sqlite.py`` 与 ``repository/schema.py``）。本模块只保留
领域校验（状态机、BasisPack、幂等语义）与调用编排，读写一律委托仓储。

P0-02：幂等键按 (tenant, actor, method, path, key) 隔离，并保存请求指纹；同键不同指纹判冲突。
生产以 sql/001 与 sql/002 为准；本模块只负责本地原型与测试。
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .models import (
    JOB_INITIAL_STATUS,
    BasisPack,
    Job,
    JobDocumentRef,
    JobEvidenceRef,
    JobStatus,
    JobStatusEvent,
    JobTaskRef,
    JobTransitionError,
    JobValidationError,
    parse_business_line,
    parse_job_status,
    validate_job_transition,
)
from .repository.base import (
    AuditRepository,
    CalcRunRepository,
    DocumentRepository,
    EvidenceRepository,
    GateRepository,
    IdemRepository,
    JobRepository,
    RecordRepository,
    TaskRepository,
    build_idem_scope_key,
)
from .repository.schema import JOB_EXTRA_COLUMNS, SqliteDatabase, now_iso
from .repository.sqlite import SqliteRepositorySet

__all__ = ["EvidenceStore", "JOB_EXTRA_COLUMNS", "JobTransitionError", "JobStatus"]


def _now() -> str:
    return now_iso()


class EvidenceStore:
    """业务门面：构造一组 SQLite 仓储，并把原有公开方法逐一委托过去。

    公开方法签名与返回结构与 P1-01 完全一致（``api.py``、既有测试不改一行）。
    """

    def __init__(self, path: str = "data/txbuddy-dev.sqlite3"):
        self._database = SqliteDatabase(path)
        self.path = self._database.path
        self._init()
        self.repositories = SqliteRepositorySet(self._database)

    # ------------------------------------------------------------ 仓储入口
    @property
    def evidence_repo(self) -> EvidenceRepository:
        return self.repositories.evidence

    @property
    def calc_run_repo(self) -> CalcRunRepository:
        return self.repositories.calc_run

    @property
    def audit_repo(self) -> AuditRepository:
        return self.repositories.audit

    @property
    def idem_repo(self) -> IdemRepository:
        return self.repositories.idem

    @property
    def job_repo(self) -> JobRepository:
        return self.repositories.jobs

    @property
    def document_repo(self) -> DocumentRepository:
        return self.repositories.documents

    @property
    def task_repo(self) -> TaskRepository:
        return self.repositories.tasks

    @property
    def gate_repo(self) -> GateRepository:
        return self.repositories.gates

    @property
    def record_repo(self) -> RecordRepository:
        return self.repositories.records

    def _conn(self):
        """并发安全连接：WAL + busy timeout（50 线程并发实测会触发 database is locked）。"""
        return self._database.connect()

    # ---------------------------------------------------------------- schema
    def _columns(self, table: str) -> set[str]:
        return self._database.columns(table)

    def _migrate_legacy(self) -> None:
        """旧库兼容（列补全 / 表重建规则见 repository/schema.py）。"""
        self._database.migrate_legacy()

    def _init(self) -> None:
        self._database.initialize()

    def _ensure_job_columns(self) -> None:
        """补齐 job 增量列并回填 basis_pack_id（旧库升级不丢数据、不改列序）。"""
        self._database.ensure_job_columns()

    @property
    def schema_version(self) -> str:
        """当前存储后端标识，供健康检查与迁移日志使用。"""
        return self._database.backend

    # ------------------------------------------------------------- evidence
    def save(self, e: dict[str, Any]) -> None:
        self.repositories.evidence.save(e)

    def get(self, eid: str) -> dict[str, Any] | None:
        return self.repositories.evidence.get(eid)

    def evidence_count(self, job_id: str | None = None) -> int:
        return self.repositories.evidence.count(job_id)

    def list_evidence(self, job_id: str) -> list[dict[str, Any]]:
        """按 payload.job_id 关联 Job 的全部证据（解析 JSON 判定，不依赖序列化格式）。"""
        return self.repositories.evidence.list_by_job(job_id)

    # ------------------------------------------------------------- calc run
    def save_calc_run(self, run: dict[str, Any]) -> None:
        self.repositories.calc_run.save(run)

    def get_calc_run(self, run_id: str) -> dict[str, Any] | None:
        return self.repositories.calc_run.get(run_id)

    def list_calc_runs(self, job_id: str) -> list[dict[str, Any]]:
        return self.repositories.calc_run.list_by_job(job_id)

    # ---------------------------------------------------------------- audit
    def add_audit(self, actor: str, action: str, object_type: str, object_id: str,
                  detail: str = "", trace_id: str = "") -> None:
        self.repositories.audit.add(actor, action, object_type, object_id, detail, trace_id)

    def list_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.repositories.audit.list(limit)

    # ----------------------------------------------------- idempotency (P0-02)
    @staticmethod
    def idem_scope(tenant: str, actor: str, method: str, path: str, key: str) -> str:
        return build_idem_scope_key(tenant, actor, method, path, key)

    def idem_lookup(self, scope_key: str, fingerprint: str) -> tuple[str, dict[str, Any] | None]:
        return self.repositories.idem.lookup(scope_key, fingerprint)

    def idem_store(self, scope_key: str, fingerprint: str, response: dict[str, Any],
                   tenant: str = "", actor: str = "") -> dict[str, Any]:
        """兼容旧调用：等价于一次认领成功后立即完成。新代码请用 idem_claim/idem_finish。"""
        return self.repositories.idem.store(scope_key, fingerprint, response, tenant, actor)

    # --- V4.2.3 幂等核心：唯一裁判是数据库唯一约束，不是 Python 内存状态 ---
    # 状态机：IN_PROGRESS --idem_finish--> DONE
    #                       --idem_release(异常)--> 行删除，可重试
    IDEM_STATE_IN_PROGRESS = "IN_PROGRESS"
    IDEM_STATE_DONE = "DONE"
    IDEM_CLAIM_TTL_SECONDS = 300.0  # 认领超时（进程崩溃后的接管窗口）

    def idem_claim(self, scope_key: str, fingerprint: str, tenant: str = "", actor: str = "",
                   ttl_seconds: float | None = None) -> tuple[str, dict[str, Any] | None]:
        """数据库级原子认领（判定逻辑见 repository/sqlite.py::SqliteIdemRepository.claim）。"""
        ttl = self.IDEM_CLAIM_TTL_SECONDS if ttl_seconds is None else ttl_seconds
        return self.repositories.idem.claim(scope_key, fingerprint, tenant, actor, ttl)

    def idem_finish(self, scope_key: str, response: Any, state: str | None = None) -> bool:
        """写回响应并置 DONE；只有持有 IN_PROGRESS 认领的一方能写。"""
        return self.repositories.idem.finish(scope_key, response, state)

    def idem_release(self, scope_key: str) -> bool:
        """执行业务抛异常时释放认领，使同一幂等键可重试（不缓存失败）。"""
        return self.repositories.idem.release(scope_key)

    # ------------------------------------------------------------------ job
    def create_job(self, job_id: str, project_id: str, name: str, business_line: str,
                   basis_pack: dict[str, Any], created_by: str) -> None:
        """创建 Job：业务线必须是 L1..L6，basis_pack 必须是完整 BasisPack。

        初始状态固定为 JOB_INITIAL_STATUS（CREATED），并写入首条状态迁移审计事件。
        """
        line = parse_business_line(business_line)
        if not str(project_id or "").strip():
            raise JobValidationError("project_id 不得为空", code="JOB_PROJECT_REQUIRED")
        if not str(name or "").strip():
            raise JobValidationError("name 不得为空", code="JOB_NAME_REQUIRED")
        if not isinstance(basis_pack, dict):
            raise JobValidationError("basis_pack 必须是对象", code="JOB_BASIS_PACK_INVALID")
        try:
            pack = BasisPack.model_validate(basis_pack)
        except ValidationError as exc:
            raise JobValidationError(
                f"basis_pack 不合法：{exc.errors()[0].get('loc')} {exc.errors()[0].get('msg')}",
                code="JOB_BASIS_PACK_INVALID") from None
        now = _now()
        history = [JobStatusEvent(to_status=JOB_INITIAL_STATUS, by=created_by or "system",
                                  at=now, reason="JOB_CREATE").model_dump(mode="json")]
        self.repositories.jobs.create(
            job_id=job_id, project_id=project_id, name=name, business_line=line.value,
            basis_pack=pack.model_dump(mode="json"), status=JOB_INITIAL_STATUS.value,
            created_by=created_by, created_at=now, basis_pack_id=pack.basis_pack_id,
            updated_by=created_by, updated_at=now, revision=1, status_history=history)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        return self.repositories.jobs.get(job_id)

    def set_job_status(self, job_id: str, status: str, *, by: str = "system",
                       reason: str | None = None) -> dict[str, Any]:
        """状态迁移：按 JOB_TRANSITIONS 校验，非法迁移直接拒绝（JobTransitionError）。

        更新走 CAS（仓储层 WHERE status=<当前状态>），并发下只有一个迁移能生效。
        """
        target = parse_job_status(status)
        job = self.get_job(job_id)
        if not job:
            raise JobValidationError(f"job 不存在：{job_id}", code="JOB_NOT_FOUND")
        current = parse_job_status(job["status"])
        validate_job_transition(current, target)
        history = list(job["status_history"])
        history.append(JobStatusEvent(from_status=current, to_status=target, by=by or "system",
                                      at=_now(), reason=reason).model_dump(mode="json"))
        self._update_job_status(job_id, target, current, by or "system", history)
        return self.get_job(job_id)  # type: ignore[return-value]

    def _update_job_status(self, job_id: str, target: JobStatus, current: JobStatus,
                           by: str, history: list[dict[str, Any]]) -> None:
        updated = self.repositories.jobs.update_status(
            job_id, status=target.value, expected_status=current.value, by=by,
            at=_now(), status_history=history)
        if not updated:
            raise JobValidationError(
                f"job {job_id} 状态并发冲突：期望 {current.value}", code="JOB_STATUS_CONFLICT")

    def transition_job_status(self, job_id: str, status: str, *, by: str = "system",
                              reason: str | None = None) -> dict[str, Any]:
        """set_job_status 的语义别名（迁移到指定状态）。"""
        return self.set_job_status(job_id, status, by=by, reason=reason)

    def get_job_model(self, job_id: str) -> Job | None:
        """Job 聚合根：job 行 + documents + tasks + evidence + 状态迁移审计。"""
        job = self.get_job(job_id)
        if not job:
            return None
        try:
            return Job(
                job_id=job["job_id"],
                project_id=job["project_id"],
                name=job["name"],
                business_line=job["business_line"],
                status=job["status"],
                basis_pack=job["basis_pack"],
                basis_pack_id=job["basis_pack_id"] or job["basis_pack"].get("basis_pack_id", ""),
                created_by=job["created_by"],
                created_at=job["created_at"],
                updated_by=job["updated_by"],
                updated_at=job["updated_at"],
                revision=job["revision"] or 1,
                documents=[JobDocumentRef(**d) for d in self.list_documents(job_id)],
                tasks=[JobTaskRef(task_id=t["task_id"], node=t["node"], status=t["status"],
                                  payload=t["payload"] or {}, result=t["result"],
                                  assignee=t["assignee"], approver=t["approver"],
                                  approved_at=t["approved_at"], updated_at=t["updated_at"])
                       for t in self.list_tasks(job_id)],
                evidence=[JobEvidenceRef(evidence_id=e["evidence_id"], sha256=e["sha256"],
                                         created_at=e["created_at"], payload=e["payload"])
                          for e in self.list_evidence(job_id)],
                status_history=[JobStatusEvent(**h) for h in job["status_history"]],
            )
        except JobValidationError:
            raise
        except Exception as exc:
            raise JobValidationError(f"job 记录不合法：{exc}", code="JOB_MODEL_INVALID") from None

    def list_job_evidence_ids(self, job_id: str) -> list[str]:
        return [e["evidence_id"] for e in self.list_evidence(job_id)]

    def get_job_summary(self, job_id: str):
        """Job 读取视图（计数 + 允许的下一状态），供列表/工作台使用。"""
        from .api_models import JobSummary

        job = self.get_job_model(job_id)
        return None if job is None else JobSummary.from_job(job)

    def add_document(self, job_id: str, doc_key: str, version: str, file_sha256: str,
                     verified: bool = False) -> None:
        if not self.get_job(job_id):
            raise JobValidationError(f"job 不存在：{job_id}", code="JOB_NOT_FOUND")
        self.repositories.documents.add(job_id, doc_key, version, file_sha256,
                                        verified, _now())

    def list_documents(self, job_id: str) -> list[dict[str, Any]]:
        return self.repositories.documents.list_by_job(job_id)

    # ----------------------------------------------------------------- task
    def create_task(self, task_id: str, job_id: str, node: str, payload: dict[str, Any],
                    assignee: str) -> None:
        now = _now()
        self.repositories.tasks.create(
            task_id=task_id, job_id=job_id, node=node, status="READY", payload=payload,
            assignee=assignee, history=[{"to": "READY", "at": now, "by": assignee}],
            created_at=now, updated_at=now)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self.repositories.tasks.get(task_id)

    def update_task(self, task_id: str, *, status: str, by: str,
                    result: dict[str, Any] | None = None, approver: str | None = None,
                    approved_at: str | None = None) -> None:
        task = self.get_task(task_id)
        history = task["history"] if task else []
        history.append({"from": task["status"] if task else None, "to": status,
                        "by": by, "at": _now()})
        self.repositories.tasks.update(task_id, status=status, result=result,
                                       approver=approver, approved_at=approved_at,
                                       history=history, updated_at=_now())

    def list_tasks(self, job_id: str) -> list[dict[str, Any]]:
        return self.repositories.tasks.list_by_job(job_id)

    # --------------------------------------------------------------- record
    def put_record(self, job_id: str, kind: str, payload: dict[str, Any]) -> None:
        self.repositories.records.put(job_id, kind, payload, _now())

    def get_records(self, job_id: str, kind: str) -> list[dict[str, Any]]:
        return self.repositories.records.list_by_job(job_id, kind)

    # ---------------------------------------------------------------- gates
    def add_gate_record(self, job_id: str, gate_code: str, result: str,
                        failed_rules: list[dict[str, Any]], judged_by: str) -> None:
        self.repositories.gates.add(job_id, gate_code, result, failed_rules,
                                    judged_by, _now())

    def list_gate_records(self, job_id: str) -> list[dict[str, Any]]:
        return self.repositories.gates.list_by_job(job_id)
