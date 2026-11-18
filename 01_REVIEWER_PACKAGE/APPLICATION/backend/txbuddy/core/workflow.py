"""TX-020 JobTask 与人工确认：状态机 + 权限 + 审批留痕。

状态：READY → RUNNING → WAITING_APPROVAL → APPROVED → DONE，另有 FAILED。
人工确认不是前端按钮：必须由服务端校验角色、资格与状态迁移，并写 audit_log。
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

APPROVAL_NODES = {"SELF_REVIEW", "REVIEW", "APPROVE", "CONFIRM_QUOTA", "REVIEW_PRICE", "REVIEW_BOQ"}
TRANSITIONS = {
    "READY": {"RUNNING", "FAILED"},
    "RUNNING": {"WAITING_APPROVAL", "DONE", "FAILED"},
    "WAITING_APPROVAL": {"APPROVED", "FAILED"},
    "APPROVED": {"DONE"},
    "DONE": set(),
    "FAILED": {"READY"},
}
ROLE_CAN_CONFIRM = {"REVIEWER", "PRINCIPAL", "APPRAISER"}
ROLE_CAN_SUBMIT = {"EDITOR", "REVIEWER", "PRINCIPAL", "APPRAISER"}
SEAL_LICENSES = {"REGISTERED_COST_ENGINEER", "APPRAISER"}
NODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,30}$")


class WorkflowError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_transition(task: dict[str, Any], target: str) -> None:
    allowed = TRANSITIONS.get(task["status"], set())
    if target not in allowed:
        raise WorkflowError("INTAKE_GATE_NOT_READY", f"非法状态迁移：{task['status']} → {target}")


def create_task(store, job_id: str, node: str, payload: dict[str, Any], assignee: str) -> dict[str, Any]:
    if not NODE_RE.match(node):
        raise WorkflowError("INTAKE_GATE_NOT_READY", f"节点名非法：{node}")
    job = store.get_job(job_id)
    if not job:
        raise WorkflowError("INTAKE_GATE_NOT_READY", f"任务所属 job 不存在：{job_id}")
    tid = new_id("task")
    store.create_task(tid, job_id, node, payload, assignee)
    store.add_audit(assignee, "TASK_CREATE", "job_task", tid, detail=node)
    return store.get_task(tid)


def start_task(store, task_id: str, actor: str, role: str = "EDITOR") -> dict[str, Any]:
    task = store.get_task(task_id)
    if not task:
        raise WorkflowError("INTAKE_GATE_NOT_READY", f"任务不存在：{task_id}")
    if role not in ROLE_CAN_SUBMIT:
        raise WorkflowError("SEAL_NOT_ALLOWED", f"角色 {role} 无权执行任务")
    _check_transition(task, "RUNNING")
    store.update_task(task_id, status="RUNNING", by=actor)
    store.add_audit(actor, "TASK_START", "job_task", task_id)
    return store.get_task(task_id)


def complete_node(store, task_id: str, actor: str, result: dict[str, Any],
                  role: str = "EDITOR") -> dict[str, Any]:
    task = store.get_task(task_id)
    if not task:
        raise WorkflowError("INTAKE_GATE_NOT_READY", f"任务不存在：{task_id}")
    if role not in ROLE_CAN_SUBMIT:
        raise WorkflowError("SEAL_NOT_ALLOWED", f"角色 {role} 无权提交节点结果")
    if task["status"] == "READY":
        _check_transition(task, "RUNNING")
        store.update_task(task_id, status="RUNNING", by=actor)
        task = store.get_task(task_id)
    target = "WAITING_APPROVAL" if task["node"] in APPROVAL_NODES else "DONE"
    _check_transition(task, target)
    store.update_task(task_id, status=target, by=actor, result=result)
    store.add_audit(actor, "TASK_COMPLETE_NODE", "job_task", task_id, detail=target)
    return store.get_task(task_id)


def approve_task(store, task_id: str, actor: str, role: str, license: str | None = None,
                 comment: str = "") -> dict[str, Any]:
    task = store.get_task(task_id)
    if not task:
        raise WorkflowError("INTAKE_GATE_NOT_READY", f"任务不存在：{task_id}")
    if task["status"] != "WAITING_APPROVAL":
        raise WorkflowError("SUMMARY_REVIEW_FAILED", f"任务不在待确认状态：{task['status']}")
    if role not in ROLE_CAN_CONFIRM:
        raise WorkflowError("SEAL_NOT_ALLOWED", f"角色 {role} 无权确认")
    if task["node"] == "APPROVE" and license not in SEAL_LICENSES:
        raise WorkflowError("SEAL_NOT_ALLOWED", f"审定节点需注册造价工程师/鉴定人资格，实际：{license}")
    _check_transition(task, "APPROVED")
    store.update_task(task_id, status="APPROVED", by=actor, approver=actor, approved_at=_now())
    store.update_task(task_id, status="DONE", by=actor)
    store.add_audit(actor, "TASK_APPROVE", "job_task", task_id, detail=f"{task['node']}|{comment}")
    return store.get_task(task_id)


def fail_task(store, task_id: str, actor: str, reason: str) -> dict[str, Any]:
    task = store.get_task(task_id)
    if not task:
        raise WorkflowError("INTAKE_GATE_NOT_READY", f"任务不存在：{task_id}")
    if "FAILED" not in TRANSITIONS.get(task["status"], set()):
        raise WorkflowError("SUMMARY_REVIEW_FAILED", f"当前状态不可标记失败：{task['status']}")
    store.update_task(task_id, status="FAILED", by=actor, result={"error": reason})
    store.add_audit(actor, "TASK_FAIL", "job_task", task_id, detail=reason)
    return store.get_task(task_id)


def retry_task(store, task_id: str, actor: str) -> dict[str, Any]:
    task = store.get_task(task_id)
    if not task:
        raise WorkflowError("INTAKE_GATE_NOT_READY", f"任务不存在：{task_id}")
    if task["status"] != "FAILED":
        raise WorkflowError("SUMMARY_REVIEW_FAILED", f"仅失败任务可重试，当前：{task['status']}")
    store.update_task(task_id, status="READY", by=actor)
    store.add_audit(actor, "TASK_RETRY", "job_task", task_id)
    return store.get_task(task_id)
