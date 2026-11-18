"""P1-08-YAML-WF：真实读取 ``workflows/*.yaml`` 的 L1 工作流加载器 + 校验 + 执行器。

与 ``core/workflow.py``（TX-020 单任务状态机）分工不同：

* **加载**：PyYAML ``safe_load`` 读取真实 YAML 文件，根必须是 mapping，
  节点列表取自 ``steps``（兼容 ``nodes``）。
* **校验**：节点 id 唯一 → ``WF_NODE_DUPLICATE``；依赖必须存在 → ``WF_DEP_MISSING``；
  依赖图无环 → ``WF_CYCLE``；节点缺 id/actor → ``WF_NODE_INVALID``。
* **执行**：按拓扑序执行。每个节点在 ``job_task`` 落一行（task_id 确定性：
  ``wf-<run_id>-<node_id>``），状态 READY → RUNNING → DONE / WAITING_APPROVAL / FAILED，
  因此执行进度可以跨进程 ``resume``，也可以直接 ``store.list_tasks(job_id)`` 查询。
* **审批**：``approval: true`` 的节点执行成功后停在 WAITING_APPROVAL；未审批时
  后继节点不会执行，整体状态为 PAUSED。
* **证据**：每个节点写一条 evidence（job_id + run_id + node_id + 输出摘要），
  同时写 job_record 与 audit_log。
* **恢复**：``resume()`` 只把 FAILED 节点重置为 READY 后重跑，DONE 节点绝不重跑。
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .evidence import build_evidence, canonical
from ..repository.schema import now_iso

__all__ = [
    "DEFAULT_APPROVAL_ROLES",
    "NodeSpec",
    "WorkflowExecutor",
    "WorkflowError",
    "WorkflowNodeError",
    "WorkflowSpec",
    "WorkflowSpecError",
    "load_workflow",
    "parse_workflow",
    "run_workflow",
]

NODE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
DEFAULT_APPROVAL_ROLES: tuple[str, ...] = ("REVIEWER", "PRINCIPAL", "APPRAISER")
_DEP_KEYS = ("depends_on", "needs", "deps", "after")

STATUS_PENDING = "READY"
STATUS_RUNNING = "RUNNING"
STATUS_WAITING = "WAITING_APPROVAL"
STATUS_APPROVED = "APPROVED"
STATUS_DONE = "DONE"
STATUS_FAILED = "FAILED"


class WorkflowError(ValueError):
    """执行期错误（带机器可读 code，便于 API 与测试断言）。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class WorkflowSpecError(WorkflowError):
    """YAML 结构/依赖图校验错误。"""


class WorkflowNodeError(WorkflowError):
    """业务节点执行失败（handler 可抛出它携带业务失败码）。"""


def _text(value: Any, field_name: str, node_id: str) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise WorkflowSpecError("WF_NODE_INVALID", f"节点 {node_id} 缺少 {field_name}")
    return str(value).strip()


def _str_list(value: Any, field_name: str, node_id: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise WorkflowSpecError(
            "WF_NODE_INVALID", f"节点 {node_id} 的 {field_name} 必须是字符串列表")
    out: list[str] = []
    for item in value:
        if not str(item or "").strip():
            raise WorkflowSpecError(
                "WF_NODE_INVALID", f"节点 {node_id} 的 {field_name} 含空值")
        out.append(str(item).strip())
    return tuple(out)


@dataclass(frozen=True)
class NodeSpec:
    """一个业务节点（对应 YAML 的一条 step）。"""

    id: str
    actor: str
    name: str = ""
    depends_on: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    approval: bool = False
    approval_roles: tuple[str, ...] = DEFAULT_APPROVAL_ROLES
    approval_license: str | None = None
    index: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return self.name or self.id


@dataclass(frozen=True)
class WorkflowSpec:
    """一条完整工作流：line + 节点 + 拓扑序。"""

    line: str
    name: str
    nodes: tuple[NodeSpec, ...]
    order: tuple[NodeSpec, ...]
    source: str = "<memory>"
    version: Any = None

    @property
    def by_id(self) -> dict[str, NodeSpec]:
        return {node.id: node for node in self.nodes}

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(node.id for node in self.order)

    def node(self, node_id: str) -> NodeSpec:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise WorkflowSpecError("WF_NODE_UNKNOWN", f"工作流不存在节点 {node_id}")


def _parse_approval(raw: Mapping[str, Any], node_id: str) -> tuple[bool, tuple[str, ...], str | None]:
    approval = raw.get("approval")
    roles_override = raw.get("approver_roles") or raw.get("approval_roles")
    license_override = raw.get("license_required") or raw.get("approval_license")
    if approval in (None, False):
        if roles_override or license_override:
            approval = True
        else:
            return False, DEFAULT_APPROVAL_ROLES, None
    roles: tuple[str, ...] = DEFAULT_APPROVAL_ROLES
    license_code: str | None = None
    if isinstance(approval, Mapping):
        raw_roles = approval.get("roles", approval.get("role"))
        if isinstance(raw_roles, str):
            roles = (raw_roles.strip(),)
        elif raw_roles:
            roles = tuple(str(r).strip() for r in raw_roles if str(r).strip())
        license_code = approval.get("license") or approval.get("license_required")
    elif approval is not True:
        raise WorkflowSpecError(
            "WF_NODE_INVALID", f"节点 {node_id} 的 approval 只能是布尔或映射")
    if roles_override:
        if isinstance(roles_override, str):
            roles = (roles_override.strip(),)
        else:
            roles = tuple(str(r).strip() for r in roles_override if str(r).strip())
    if license_override:
        license_code = license_override
    if not roles:
        raise WorkflowSpecError("WF_NODE_INVALID", f"节点 {node_id} 的审批角色为空")
    return True, tuple(roles), (str(license_code).strip() if license_code else None)


def _topological_order(nodes: list[NodeSpec]) -> tuple[NodeSpec, ...]:
    """Kahn 拓扑排序（同层按 YAML 声明顺序，保证可复现）。有环抛 WF_CYCLE。"""
    by_id = {node.id: node for node in nodes}
    indegree = {node.id: len(node.depends_on) for node in nodes}
    children: dict[str, list[str]] = {node.id: [] for node in nodes}
    for node in nodes:
        for dep in node.depends_on:
            children[dep].append(node.id)
    ready = [node.id for node in nodes if indegree[node.id] == 0]
    ordered: list[NodeSpec] = []
    while ready:
        current = ready.pop(0)
        ordered.append(by_id[current])
        for child in children[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort(key=lambda nid: by_id[nid].index)
    if len(ordered) != len(nodes):
        stuck = [node.id for node in nodes if indegree[node.id] > 0]
        raise WorkflowSpecError(
            "WF_CYCLE", f"依赖图存在环路，涉及节点：{_cycle_path(by_id, stuck)}")
    return tuple(ordered)


def _cycle_path(by_id: dict[str, NodeSpec], stuck: list[str]) -> str:
    """从残留节点里挑一条真实环路用于报错信息。"""
    remaining = set(stuck)
    seen: list[str] = []
    current = stuck[0] if stuck else ""
    while current and current not in seen:
        seen.append(current)
        nxt = [dep for dep in by_id[current].depends_on if dep in remaining]
        current = nxt[0] if nxt else ""
    if current in seen:
        loop = seen[seen.index(current):] + [current]
        return " → ".join(reversed(loop))
    return " → ".join(seen)


def parse_workflow(document: Any, *, source: str = "<memory>") -> WorkflowSpec:
    """从已解析的 YAML 对象构建并校验 WorkflowSpec。"""
    if not isinstance(document, Mapping):
        raise WorkflowSpecError("WF_YAML_INVALID", f"{source}: 根节点必须是映射")
    line = str(document.get("line") or "").strip()
    if not line:
        raise WorkflowSpecError("WF_YAML_INVALID", f"{source}: 缺少 line")
    raw_nodes = document.get("steps")
    if raw_nodes is None:
        raw_nodes = document.get("nodes")
    if not isinstance(raw_nodes, Sequence) or isinstance(raw_nodes, (str, bytes)) or not raw_nodes:
        raise WorkflowSpecError("WF_YAML_INVALID", f"{source}: steps 必须是非空列表")

    nodes: list[NodeSpec] = []
    ids: set[str] = set()
    for index, raw in enumerate(raw_nodes):
        if not isinstance(raw, Mapping):
            raise WorkflowSpecError("WF_NODE_INVALID", f"{source}: 第 {index + 1} 个节点不是映射")
        node_id = str(raw.get("id") or "").strip()
        if not node_id:
            raise WorkflowSpecError("WF_NODE_INVALID", f"{source}: 第 {index + 1} 个节点缺少 id")
        if not NODE_ID_RE.match(node_id):
            raise WorkflowSpecError("WF_NODE_INVALID", f"{source}: 节点 id 非法：{node_id}")
        if node_id in ids:
            raise WorkflowSpecError("WF_NODE_DUPLICATE", f"{source}: 节点 id 重复：{node_id}")
        ids.add(node_id)
        actor = _text(raw.get("actor"), "actor", node_id)
        depends_on: tuple[str, ...] = ()
        for key in _DEP_KEYS:
            if raw.get(key) is not None:
                depends_on = _str_list(raw.get(key), key, node_id)
                break
        approval, roles, license_code = _parse_approval(raw, node_id)
        nodes.append(NodeSpec(
            id=node_id,
            actor=actor,
            name=str(raw.get("name") or "").strip(),
            depends_on=depends_on,
            inputs=_str_list(raw.get("input"), "input", node_id),
            outputs=_str_list(raw.get("output"), "output", node_id),
            failures=_str_list(raw.get("failure"), "failure", node_id),
            approval=approval,
            approval_roles=roles,
            approval_license=license_code,
            index=index,
            raw=dict(raw),
        ))

    for node in nodes:
        for dep in node.depends_on:
            if dep == node.id:
                raise WorkflowSpecError(
                    "WF_CYCLE", f"{source}: 节点 {node.id} 依赖自身")
            if dep not in ids:
                raise WorkflowSpecError(
                    "WF_DEP_MISSING", f"{source}: 节点 {node.id} 依赖的 {dep} 不存在")

    order = _topological_order(nodes)
    return WorkflowSpec(
        line=line,
        name=str(document.get("name") or "").strip(),
        nodes=tuple(nodes),
        order=order,
        source=source,
        version=document.get("version"),
    )


def load_workflow(path: str | Path) -> WorkflowSpec:
    """真实读取 YAML 文件并校验。文件不存在 / YAML 语法错分别有独立错误码。"""
    target = Path(path)
    if not target.is_file():
        raise WorkflowSpecError("WF_YAML_NOT_FOUND", f"工作流文件不存在：{target}")
    raw = target.read_text(encoding="utf-8-sig")
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        raise WorkflowSpecError("WF_YAML_INVALID", f"{target}: YAML 语法错误：{first}") from None
    return parse_workflow(document, source=str(target))


def _coerce_spec(spec: Any) -> WorkflowSpec:
    if isinstance(spec, WorkflowSpec):
        return spec
    if isinstance(spec, Mapping):
        return parse_workflow(spec)
    return load_workflow(spec)


class WorkflowExecutor:
    """按 YAML 依赖图执行工作流，进度落 job_task，可跨进程续跑。"""

    def __init__(self, store, spec: Any, job_id: str, *, run_id: str | None = None,
                 handlers: Mapping[str, Callable[[dict[str, Any]], Mapping[str, Any] | None]] | None = None,
                 actor: str = "workflow"):
        self.store = store
        self.spec = _coerce_spec(spec)
        self.job_id = job_id
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:10]}"
        self.handlers = dict(handlers or {})
        self.actor = actor or "workflow"
        if not store.get_job(job_id):
            raise WorkflowError("WF_JOB_NOT_FOUND", f"job 不存在：{job_id}")
        self._ensure_tasks()

    # ------------------------------------------------------------ 任务持久化
    def task_id(self, node_id: str) -> str:
        return f"wf-{self.run_id}-{node_id}"

    def _ensure_tasks(self) -> None:
        for node in self.spec.order:
            tid = self.task_id(node.id)
            existing = self.store.get_task(tid)
            if existing is None:
                self.store.create_task(tid, self.job_id, node.id, {
                    "workflow": self.spec.line,
                    "workflow_name": self.spec.name,
                    "run_id": self.run_id,
                    "node_id": node.id,
                    "actor": node.actor,
                    "depends_on": list(node.depends_on),
                    "inputs": list(node.inputs),
                    "outputs": list(node.outputs),
                    "approval": node.approval,
                }, node.actor)
                continue
            payload = existing.get("payload") or {}
            if existing.get("job_id") != self.job_id or payload.get("run_id") != self.run_id:
                raise WorkflowError(
                    "WF_RUN_CONFLICT",
                    f"task {tid} 已属于 job={existing.get('job_id')} run={payload.get('run_id')}")

    def _task(self, node_id: str) -> dict[str, Any]:
        task = self.store.get_task(self.task_id(node_id))
        if task is None:
            raise WorkflowError("WF_TASK_MISSING", f"节点 {node_id} 的 job_task 不存在")
        return task

    def _status(self, node_id: str) -> str:
        return self._task(node_id).get("status") or STATUS_PENDING

    def _result(self, node_id: str) -> dict[str, Any]:
        return self._task(node_id).get("result") or {}

    def _attempts(self, node_id: str) -> int:
        try:
            return int(self._result(node_id).get("attempts") or 0)
        except (TypeError, ValueError):
            return 0

    def _write(self, node_id: str, status: str, *, result: dict[str, Any] | None = None,
               by: str | None = None, approver: str | None = None,
               approved_at: str | None = None) -> None:
        self.store.update_task(self.task_id(node_id), status=status, by=by or self.actor,
                               result=result, approver=approver, approved_at=approved_at)

    # ---------------------------------------------------------------- 执行
    def run(self) -> dict[str, Any]:
        """从当前持久化状态继续执行；遇审批暂停/失败即返回（不跳过）。"""
        self._ensure_tasks()
        for node in self.spec.order:
            status = self._status(node.id)
            if status == STATUS_DONE:
                continue
            if status == STATUS_APPROVED:
                self._write(node.id, STATUS_DONE, result=self._result(node.id), by=self.actor)
                continue
            if status == STATUS_WAITING:
                return self._finish("PAUSED")
            if status == STATUS_FAILED:
                return self._finish("FAILED")
            if status not in (STATUS_PENDING, STATUS_RUNNING):
                raise WorkflowError("WF_TASK_STATE", f"节点 {node.id} 状态非法：{status}")
            if status == STATUS_RUNNING:
                # 上一进程执行中中断：重置为 READY 重跑（handler 需幂等）。
                self._write(node.id, STATUS_PENDING, by=self.actor, result={
                    **self._result(node.id), "interrupted": True,
                    "interrupted_at": now_iso()})
                self.store.add_audit(self.actor, "WF_NODE_INTERRUPTED", "job_task",
                                     self.task_id(node.id), detail=node.id)
            unmet = [dep for dep in node.depends_on if self._status(dep) != STATUS_DONE]
            if unmet:
                self._fail(node, WorkflowNodeError(
                    "WF_DEP_NOT_DONE",
                    f"前置节点未完成：{', '.join(unmet)}"), attempt=None)
                return self._finish("FAILED")
            self._run_node(node)
            new_status = self._status(node.id)
            if new_status == STATUS_FAILED:
                return self._finish("FAILED")
            if new_status == STATUS_WAITING:
                return self._finish("PAUSED")
        return self._finish("COMPLETED")

    def _run_node(self, node: NodeSpec) -> None:
        attempt = self._attempts(node.id) + 1
        previous = self._result(node.id)
        self._write(node.id, STATUS_RUNNING, by=self.actor, result={
            "attempts": attempt, "started_at": now_iso(), "by": self.actor,
            "previous_error": previous.get("error"),
        })
        self.store.add_audit(self.actor, "WF_NODE_START", "job_task",
                             self.task_id(node.id), detail=node.id)
        try:
            outputs = self._invoke(node, attempt)
        except WorkflowNodeError as exc:
            self._fail(node, exc, attempt=attempt)
            return
        except Exception as exc:  # noqa: BLE001 - 业务 handler 任何异常都要落 FAILED
            self._fail(node, WorkflowNodeError(
                "WF_NODE_FAILED", f"{type(exc).__name__}: {exc}"), attempt=attempt)
            return

        target = STATUS_WAITING if node.approval else STATUS_DONE
        evidence = self._record_evidence(node, attempt, outputs, target)
        record_kind = f"workflow_node:{node.id}"
        self.store.put_record(self.job_id, record_kind, {
            "run_id": self.run_id, "node_id": node.id, "actor": node.actor,
            "status": target, "attempt": attempt, "outputs": outputs,
            "evidence_id": evidence["evidence_id"], "completed_at": now_iso(),
        })
        self._write(node.id, target, by=self.actor, result={
            "attempts": attempt,
            "outputs": outputs,
            "evidence_id": evidence["evidence_id"],
            "record_kind": record_kind,
            "completed_at": now_iso(),
            "by": self.actor,
        })
        self.store.add_audit(self.actor, "WF_NODE_COMPLETE", "job_task",
                             self.task_id(node.id), detail=f"{node.id}|{target}")

    def _fail(self, node: NodeSpec, exc: WorkflowNodeError, attempt: int | None) -> None:
        attempt = attempt or (self._attempts(node.id) or 1)
        result = {
            "attempts": attempt,
            "outputs": {},
            "error": exc.message,
            "failure_code": exc.code,
            "failed_at": now_iso(),
            "declared_failures": list(node.failures),
            "by": self.actor,
        }
        self._write(node.id, STATUS_FAILED, by=self.actor, result=result)
        self.store.add_audit(self.actor, "WF_NODE_FAIL", "job_task",
                             self.task_id(node.id), detail=f"{node.id}|{exc.code}")
        self._record_evidence(node, attempt, {"error": exc.message, "code": exc.code},
                              STATUS_FAILED)

    # ------------------------------------------------------------ handler
    def _inputs(self, node: NodeSpec) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for dep in node.depends_on:
            result = self._result(dep)
            resolved[dep] = result.get("outputs") or {}
        return resolved

    def _invoke(self, node: NodeSpec, attempt: int) -> dict[str, Any]:
        handler = self.handlers.get(node.id) or self.handlers.get("*")
        context = {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "node": node,
            "spec": self.spec,
            "store": self.store,
            "executor": self,
            "attempt": attempt,
            "inputs": self._inputs(node),
        }
        if handler is None:
            return self._simulated_outputs(node, attempt)
        produced = handler(context)
        if produced is None:
            return self._simulated_outputs(node, attempt)
        if not isinstance(produced, Mapping):
            raise WorkflowNodeError(
                "WF_NODE_INVALID",
                f"节点 {node.id} 的 handler 必须返回映射或 None，实际：{type(produced).__name__}")
        return {str(key): value for key, value in produced.items()}

    @staticmethod
    def _simulated_outputs(node: NodeSpec, attempt: int) -> dict[str, Any]:
        return {name: {"node_id": node.id, "name": name, "state": "SIMULATED",
                       "attempt": attempt} for name in node.outputs}

    def _record_evidence(self, node: NodeSpec, attempt: int, outputs: Mapping[str, Any],
                         status: str) -> dict[str, Any]:
        evidence = build_evidence(
            job_id=self.job_id,
            workflow_line=self.spec.line,
            workflow_name=self.spec.name,
            workflow_source=self.spec.source,
            run_id=self.run_id,
            node_id=node.id,
            node_actor=node.actor,
            attempt=attempt,
            status=status,
            inputs=list(node.inputs),
            outputs=list(node.outputs),
            outputs_sha256=canonical(dict(outputs)) if isinstance(outputs, Mapping) else None,
            executed_at=now_iso(),
        )
        # outputs_sha256 里再嵌 sha256 便于复核 handler 产物未被改写
        self.store.save(evidence)
        self.store.add_audit(self.actor, "WF_NODE_EVIDENCE", "evidence",
                             evidence["evidence_id"], detail=f"{node.id}|{status}")
        return evidence

    # -------------------------------------------------------------- 审批
    def approve(self, node_id: str, *, actor: str, role: str, license: str | None = None,
                comment: str = "") -> dict[str, Any]:
        node = self.spec.node(node_id)
        if not node.approval:
            raise WorkflowError("WF_APPROVAL_NOT_REQUIRED", f"节点 {node.id} 不是审批节点")
        task = self._task(node.id)
        if task.get("status") != STATUS_WAITING:
            raise WorkflowError("WF_APPROVAL_STATE",
                                f"节点 {node.id} 当前状态 {task.get('status')}，不是待审批")
        if role not in node.approval_roles:
            raise WorkflowError(
                "WF_APPROVAL_ROLE",
                f"角色 {role} 无权审批 {node.id}，允许角色：{', '.join(node.approval_roles)}")
        if node.approval_license and str(license or "") != node.approval_license:
            raise WorkflowError(
                "WF_APPROVAL_LICENSE",
                f"节点 {node.id} 需要 {node.approval_license} 资格，实际：{license}")
        now = now_iso()
        result = {**(task.get("result") or {}), "approval": {
            "by": actor, "role": role, "license": license,
            "comment": comment, "at": now}}
        self._write(node.id, STATUS_APPROVED, result=result, by=actor,
                    approver=actor, approved_at=now)
        self._write(node.id, STATUS_DONE, result=result, by=actor)
        self.store.add_audit(actor, "WF_NODE_APPROVE", "job_task",
                             self.task_id(node.id), detail=f"{node.id}|{role}|{comment}")
        return self._task(node.id)

    # -------------------------------------------------------- 恢复/查询
    def resume(self) -> dict[str, Any]:
        """只重置 FAILED 节点为 READY 后继续；DONE 节点永不重跑。"""
        retried: list[str] = []
        for node in self.spec.order:
            if self._status(node.id) == STATUS_FAILED:
                self._write(node.id, STATUS_PENDING, by=self.actor, result={
                    **self._result(node.id), "retry_requested_at": now_iso()})
                self.store.add_audit(self.actor, "WF_NODE_RETRY", "job_task",
                                     self.task_id(node.id), detail=node.id)
                retried.append(node.id)
        summary = self.run()
        summary["retried"] = retried
        return summary

    def next_ready(self) -> list[str]:
        ready: list[str] = []
        for node in self.spec.order:
            if self._status(node.id) != STATUS_PENDING:
                continue
            if all(self._status(dep) == STATUS_DONE for dep in node.depends_on):
                ready.append(node.id)
        return ready

    def status(self) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        for node in self.spec.order:
            task = self._task(node.id)
            result = task.get("result") or {}
            nodes.append({
                "node_id": node.id,
                "name": node.title,
                "actor": node.actor,
                "status": task.get("status") or STATUS_PENDING,
                "approval": node.approval,
                "depends_on": list(node.depends_on),
                "attempts": int(result.get("attempts") or 0),
                "evidence_id": result.get("evidence_id"),
                "error": result.get("error"),
                "failure_code": result.get("failure_code"),
                "updated_at": task.get("updated_at"),
                "history": [h.get("to") for h in (task.get("history") or [])],
                "task_id": self.task_id(node.id),
            })
        done = sum(1 for item in nodes if item["status"] == STATUS_DONE)
        failed = [item["node_id"] for item in nodes if item["status"] == STATUS_FAILED]
        waiting = [item["node_id"] for item in nodes if item["status"] == STATUS_WAITING]
        running = [item["node_id"] for item in nodes if item["status"] == STATUS_RUNNING]
        if failed:
            state = "FAILED"
        elif waiting:
            state = "PAUSED"
        elif done == len(nodes):
            state = "COMPLETED"
        elif running:
            state = "RUNNING"
        else:
            state = "PENDING"
        return {
            "run_id": self.run_id,
            "job_id": self.job_id,
            "line": self.spec.line,
            "name": self.spec.name,
            "state": state,
            "total": len(nodes),
            "done": done,
            "failed": failed,
            "waiting_approval": waiting,
            "running": running,
            "next": self.next_ready(),
            "nodes": nodes,
            "evidence_ids": [item["evidence_id"] for item in nodes if item["evidence_id"]],
            "updated_at": now_iso(),
        }

    def _finish(self, state: str) -> dict[str, Any]:
        summary = self.status()
        summary["state"] = state
        self.store.put_record(self.job_id, "workflow_run", summary)
        return summary


def run_workflow(store, spec: Any, job_id: str, **kwargs: Any) -> dict[str, Any]:
    """便捷入口：构建执行器并跑到暂停/失败/完成。"""
    return WorkflowExecutor(store, spec, job_id, **kwargs).run()
