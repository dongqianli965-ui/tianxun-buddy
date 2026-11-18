"""Repository 抽象接口（V4.2.3 / P1-02）。

分层约定：
  * 业务层（``txbuddy.store``）只做领域校验（状态机、BasisPack、幂等语义），不再出现 SQL；
  * 本文件定义与具体数据库无关的协议，SQLite 与后续 PostgreSQL 实现同一组方法；
  * 方法出入参一律使用纯 Python 标量 / dict / list，便于跨引擎替换与单测打桩。

八类仓储与 ``TASK.md`` 要求一一对应：
Evidence / CalcRun / Audit / Idem / Job / Document / Task / Gate（外加 Record，用于
``job_record`` 通用记录表，store 的 put_record / get_records 委托给它）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any


class UnitOfWorkError(RuntimeError):
    """事务边界错误基类（重复进入、已结束后再用等编程错误）。"""


class UnitOfWorkAbort(UnitOfWorkError):
    """事务内必须回滚的领域失败：抛出后该边界内已写入的行全部回滚。

    与业务异常分开命名，便于调用方区分“事务纪律错误”和“这次写入被中止”。
    """


class UnitOfWork(ABC):
    """事务边界（unit of work）协议。

    语义约定：
      * ``with`` 体内的全部仓储写入要么一起可见，要么一起消失；
      * 体内抛任何异常 → 整段回滚，库里不留半成品；
      * 嵌套 ``with`` 只在最外层真正提交/回滚，内层异常回滚到保存点（SAVEPOINT）；
      * 同一线程/上下文共享同一条连接，事务外的连接看不到未提交的行。
    """

    backend: str = "abstract"

    @abstractmethod
    def __enter__(self) -> "UnitOfWork":
        """开始事务（嵌套时建立保存点）。"""

    @abstractmethod
    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        """结束事务：无异常则提交，有异常（或已标记 rollback-only）则回滚。"""

    @abstractmethod
    def commit(self) -> None:
        """立即提交并结束边界。"""

    @abstractmethod
    def rollback(self) -> None:
        """立即回滚并结束边界。"""

    @abstractmethod
    def set_rollback_only(self) -> None:
        """标记整个事务只能回滚：即使 __exit__ 没有异常也不提交。"""


def build_idem_scope_key(tenant: str, actor: str, method: str, path: str, key: str) -> str:
    """幂等作用域键（与后端无关的纯函数）：(tenant, actor, method, path, key) 五元组。"""
    return f"{tenant or '-'}|{actor or '-'}|{method.upper()}|{path}|{key}"


class Repository(ABC):
    """所有仓储的根接口：``backend`` 供健康检查与日志标注当前存储实现。"""

    backend: str = "abstract"

    def health(self) -> bool:
        """轻量可用性探针；实现方应在 1 次往返内返回。"""
        return True


class EvidenceRepository(Repository):
    """证据表（evidence）：按 evidence_id 存取，按 payload.job_id 关联 Job。"""

    @abstractmethod
    def save(self, evidence: dict[str, Any]) -> None:
        """写入（同 id 覆盖）一条证据记录。"""

    @abstractmethod
    def get(self, evidence_id: str) -> dict[str, Any] | None:
        """按 id 取回证据 payload；不存在返回 None。"""

    @abstractmethod
    def count(self, job_id: str | None = None) -> int:
        """证据条数；job_id 非空时只统计该 Job 的证据。"""

    @abstractmethod
    def list_by_job(self, job_id: str) -> list[dict[str, Any]]:
        """列出归属于该 Job 的全部证据（按 created_at, evidence_id 升序）。"""


class CalcRunRepository(Repository):
    """计算运行表（calc_run）：每条规则一次运行的快照、结果与证据引用。"""

    @abstractmethod
    def save(self, run: dict[str, Any]) -> None:
        """写入（同 id 覆盖）一次计算运行。"""

    @abstractmethod
    def get(self, calc_run_id: str) -> dict[str, Any] | None:
        """取回单次运行（snapshot 已反序列化为对象）；不存在返回 None。"""

    @abstractmethod
    def list_by_job(self, job_id: str) -> list[dict[str, Any]]:
        """列出该 Job 的全部运行摘要（不含 snapshot 正文），按 created_at 升序。"""


class AuditRepository(Repository):
    """审计日志表（audit_log）：只追加，从不修改或删除。"""

    @abstractmethod
    def add(self, actor: str, action: str, object_type: str, object_id: str,
            detail: str = "", trace_id: str = "") -> None:
        """追加一条审计记录。"""

    @abstractmethod
    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        """按写入顺序倒序列出最近 limit 条审计记录。"""


class IdemRepository(Repository):
    """幂等表（idem）：以数据库唯一约束作为唯一裁判。

    状态机：``IN_PROGRESS --finish--> DONE``；``release`` 删除 IN_PROGRESS 行使同键可重试。
    """

    @staticmethod
    def scope_key(tenant: str, actor: str, method: str, path: str, key: str) -> str:
        """幂等作用域键：(tenant, actor, method, path, key) 五元组序列化。"""
        return build_idem_scope_key(tenant, actor, method, path, key)

    @abstractmethod
    def lookup(self, scope_key: str, fingerprint: str) -> tuple[str, dict[str, Any] | None]:
        """只读查询，返回 ("miss"|"conflict"|"hit", cached)。"""

    @abstractmethod
    def claim(self, scope_key: str, fingerprint: str, tenant: str = "", actor: str = "",
              ttl_seconds: float | None = None) -> tuple[str, dict[str, Any] | None]:
        """原子认领，返回 ("claimed"|"hit"|"conflict"|"in_progress"|"retry", cached)。"""

    @abstractmethod
    def finish(self, scope_key: str, response: Any, state: str | None = None) -> bool:
        """写回响应并置 DONE；仅持有 IN_PROGRESS 认领的调用方能成功。"""

    @abstractmethod
    def release(self, scope_key: str) -> bool:
        """执行业务异常时释放认领，使同一幂等键可重试。"""

    @abstractmethod
    def store(self, scope_key: str, fingerprint: str, response: dict[str, Any],
              tenant: str = "", actor: str = "") -> dict[str, Any]:
        """兼容旧调用：认领成功后立即完成（claim + finish 的原子语义封装）。"""


class JobRepository(Repository):
    """Job 表（job）：聚合根行，状态迁移由业务层校验、仓储做 CAS 落库。"""

    @abstractmethod
    def create(self, *, job_id: str, project_id: str, name: str, business_line: str,
               basis_pack: dict[str, Any], status: str, created_by: str, created_at: str,
               basis_pack_id: str, updated_by: str, updated_at: str, revision: int,
               status_history: list[dict[str, Any]]) -> None:
        """插入一行 Job（调用方已完成业务线 / BasisPack / 状态校验）。"""

    @abstractmethod
    def get(self, job_id: str) -> dict[str, Any] | None:
        """取回 Job 行（basis_pack 与 status_history 已反序列化）；不存在返回 None。"""

    @abstractmethod
    def update_status(self, job_id: str, *, status: str, expected_status: str, by: str,
                      at: str, status_history: list[dict[str, Any]]) -> bool:
        """CAS 迁移状态：仅当库里仍是 expected_status 时更新，返回是否命中 1 行。"""


class DocumentRepository(Repository):
    """文档引用表（job_document）：只登记版本与哈希，不存文件本体。"""

    @abstractmethod
    def add(self, job_id: str, doc_key: str, version: str, file_sha256: str,
            verified: bool, added_at: str) -> None:
        """登记一个文档版本。"""

    @abstractmethod
    def list_by_job(self, job_id: str) -> list[dict[str, Any]]:
        """按登记顺序列出该 Job 的文档版本。"""


class TaskRepository(Repository):
    """流程任务表（job_task）：任务节点状态、载荷、审批与迁移历史。"""

    @abstractmethod
    def create(self, *, task_id: str, job_id: str, node: str, status: str,
               payload: dict[str, Any], assignee: str, history: list[dict[str, Any]],
               created_at: str, updated_at: str) -> None:
        """插入一个任务节点。"""

    @abstractmethod
    def get(self, task_id: str) -> dict[str, Any] | None:
        """取回任务（payload / result / history 已反序列化）；不存在返回 None。"""

    @abstractmethod
    def update(self, task_id: str, *, status: str, result: dict[str, Any] | None,
               approver: str | None, approved_at: str | None,
               history: list[dict[str, Any]], updated_at: str) -> bool:
        """更新任务状态；result/approver/approved_at 为 None 时保留原值。"""

    @abstractmethod
    def list_by_job(self, job_id: str) -> list[dict[str, Any]]:
        """按创建时间列出该 Job 的全部任务。"""


class GateRepository(Repository):
    """门禁记录表（gate_record）：只追加每次判定结果与失败规则。"""

    @abstractmethod
    def add(self, job_id: str, gate_code: str, result: str,
            failed_rules: list[dict[str, Any]], judged_by: str, judged_at: str) -> None:
        """追加一条门禁判定记录。"""

    @abstractmethod
    def list_by_job(self, job_id: str) -> list[dict[str, Any]]:
        """按判定顺序列出该 Job 的全部门禁记录。"""


class RecordRepository(Repository):
    """通用业务记录表（job_record）：按 (job_id, kind) 追加任意 JSON 载荷。"""

    @abstractmethod
    def put(self, job_id: str, kind: str, payload: dict[str, Any], at: str) -> None:
        """追加一条记录。"""

    @abstractmethod
    def list_by_job(self, job_id: str, kind: str) -> list[dict[str, Any]]:
        """按写入顺序列出该 Job 指定 kind 的全部记录。"""


class RepositorySet:
    """一组共享同一后端的仓储，供业务层一次性持有（避免每处重新构造）。"""

    backend: str = "abstract"
    evidence: EvidenceRepository
    calc_run: CalcRunRepository
    audit: AuditRepository
    idem: IdemRepository
    jobs: JobRepository
    documents: DocumentRepository
    tasks: TaskRepository
    gates: GateRepository
    records: RecordRepository

    def health(self) -> dict[str, bool]:
        """逐仓储探活，返回 {仓储名: 是否可用}。"""
        return {
            "evidence": self.evidence.health(),
            "calc_run": self.calc_run.health(),
            "audit": self.audit.health(),
            "idem": self.idem.health(),
            "jobs": self.jobs.health(),
            "documents": self.documents.health(),
            "tasks": self.tasks.health(),
            "gates": self.gates.health(),
            "records": self.records.health(),
        }

    # ------------------------------------------------------ 事务边界（P1-05）
    def unit_of_work(self, **options: Any) -> UnitOfWork:
        """返回本后端的事务边界；未实现的后端必须显式失败，禁止静默降级。"""
        raise NotImplementedError(f"{self.backend} 仓储组未提供 unit_of_work")

    def commit_calc_bundle(
        self,
        *,
        evidence: Iterable[dict[str, Any]] = (),
        calc_runs: Iterable[dict[str, Any]] = (),
        idem_scope_key: str | None = None,
        idem_response: dict[str, Any] | None = None,
        idem_state: str | None = None,
    ) -> dict[str, int]:
        """证据 + 复算 + 幂等响应同事务落库（P1-05 的核心写路径）。

        任一步失败（含幂等认领已失效）都会抛 :class:`UnitOfWorkAbort` 并整段回滚，
        因此调用方永远看不到“证据在、复算没了、幂等响应还是旧的”这种半成品组合。
        """
        saved = {"evidence": 0, "calc_run": 0, "idem": 0}
        with self.unit_of_work(label="calc-bundle"):
            for item in evidence:
                self.evidence.save(item)
                saved["evidence"] += 1
            for run in calc_runs:
                self.calc_run.save(run)
                saved["calc_run"] += 1
            if idem_scope_key is not None:
                if idem_response is None:
                    raise UnitOfWorkAbort("幂等响应为空：拒绝提交半成品事务")
                if not self.idem.finish(idem_scope_key, idem_response, idem_state):
                    raise UnitOfWorkAbort(
                        f"幂等认领已失效，无法写回响应：{idem_scope_key}")
                saved["idem"] = 1
        return saved
