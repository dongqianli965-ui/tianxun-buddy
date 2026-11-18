"""P1-08-YAML-WF：真实读取 workflows/L1_budget_compilation.yaml 并执行 11 个业务节点。

覆盖 TASK.md 要求的全部语义：
* 加载器 + 校验：节点 id 唯一、依赖必须存在、依赖图无环；
* 执行器：11 节点全跑通、拓扑序、job_task 状态持久化且可查询；
* 审批暂停点：未审批时后继节点不得执行；
* 失败恢复：失败节点从中断处继续，已完成节点不重跑。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from txbuddy.core import workflow_yaml as wf
from txbuddy.store import EvidenceStore

WORKFLOW_DIR = Path(__file__).resolve().parents[2] / "workflows"
L1_PATH = WORKFLOW_DIR / "L1_budget_compilation.yaml"
L1_IDS = [f"L1-{index:02d}" for index in range(1, 12)]
APPROVAL_IDS = ["L1-04", "L1-07", "L1-10"]
PACK = {
    "basis_pack_id": "B-WF-L1",
    "region_code": "410100",
    "region_level": "CITY",
    "major": "BUILDING",
    "effective_date": "2026-01-01",
    "measurement_standard_version": "GB/T 50854-2024",
    "pricing_standard_version": "GB/T 50500-2024",
    "rule_version_scope": "HENAN_2025",
    "precision_rule_version": "P-V1",
    "status": "DRAFT",
    "test_only": True,
}


def new_store(tmp_path, name="wf.sqlite3") -> EvidenceStore:
    return EvidenceStore(str(tmp_path / name))


def new_job(store: EvidenceStore, job_id="job-wf-1", line="L1") -> str:
    store.create_job(job_id=job_id, project_id="P-WF", name="L1 工作流测试",
                     business_line=line, basis_pack=PACK, created_by="tester")
    return job_id


def inline_spec(*nodes, line="L1"):
    return wf.parse_workflow({"line": line, "name": "inline", "steps": list(nodes)},
                             source="<inline>")


def drive(executor: wf.WorkflowExecutor, summary=None, limit: int = 20):
    """推进工作流：遇到审批暂停就批准（持证），直到 COMPLETED/FAILED。"""
    summary = summary or executor.run()
    rounds = 0
    while summary["state"] == "PAUSED" and rounds < limit:
        for item in summary["nodes"]:
            if item["status"] == wf.STATUS_WAITING:
                executor.approve(item["node_id"], actor="reviewer", role="PRINCIPAL",
                                 license="REGISTERED_COST_ENGINEER", comment="drive")
        summary = executor.run()
        rounds += 1
    return summary


def test_loader_reads_real_l1_yaml():
    assert L1_PATH.is_file(), L1_PATH
    spec = wf.load_workflow(L1_PATH)
    assert spec.line == "L1"
    assert spec.name == "预算编制"
    assert len(spec.nodes) == 11
    assert spec.ids == tuple(L1_IDS)
    by_id = spec.by_id
    assert by_id["L1-01"].actor == "PROJECT_OWNER"
    assert by_id["L1-01"].depends_on == ()
    assert by_id["L1-08"].depends_on == ("L1-05", "L1-07")
    assert by_id["L1-08"].outputs == ("unit_prices", "project_total", "price_evidence")
    assert [node.id for node in spec.nodes if node.approval] == APPROVAL_IDS
    assert by_id["L1-10"].approval_license == "REGISTERED_COST_ENGINEER"
    assert by_id["L1-10"].approval_roles == ("REVIEWER", "PRINCIPAL")
    assert by_id["L1-05"].failures == ("missing_rule_to_BASIS_ITEM_MISSING",)
    # 11 节点全部被真实读到（不是 API 手工编排的占位）
    assert all(node.actor and node.outputs for node in spec.nodes)


def test_all_workflow_yaml_files_load():
    files = sorted(WORKFLOW_DIR.glob("*.yaml"))
    assert len(files) >= 6, files
    loaded = {path.stem: wf.load_workflow(path) for path in files}
    assert len(loaded["L1_budget_compilation"].nodes) == 11
    for name, spec in loaded.items():
        assert spec.nodes, name
        assert len(set(spec.ids)) == len(spec.ids), name
        assert len(spec.order) == len(spec.nodes), name


def test_duplicate_node_id_is_rejected():
    with pytest.raises(wf.WorkflowSpecError) as excinfo:
        inline_spec({"id": "A1", "actor": "X"},
                    {"id": "A1", "actor": "X"})
    assert excinfo.value.code == "WF_NODE_DUPLICATE"
    assert "A1" in excinfo.value.message


def test_missing_dependency_is_rejected():
    with pytest.raises(wf.WorkflowSpecError) as excinfo:
        inline_spec({"id": "A1", "actor": "X"},
                    {"id": "A2", "actor": "X", "depends_on": ["A1", "GHOST"]})
    assert excinfo.value.code == "WF_DEP_MISSING"
    assert "GHOST" in excinfo.value.message


def test_cycle_is_rejected():
    with pytest.raises(wf.WorkflowSpecError) as excinfo:
        inline_spec({"id": "A1", "actor": "X", "depends_on": ["A3"]},
                    {"id": "A2", "actor": "X", "depends_on": ["A1"]},
                    {"id": "A3", "actor": "X", "depends_on": ["A2"]})
    assert excinfo.value.code == "WF_CYCLE"
    assert "A1" in excinfo.value.message and "A3" in excinfo.value.message

    with pytest.raises(wf.WorkflowSpecError) as self_cycle:
        inline_spec({"id": "B1", "actor": "X", "depends_on": ["B1"]})
    assert self_cycle.value.code == "WF_CYCLE"


def test_invalid_and_missing_yaml_file_rejected(tmp_path):
    broken = tmp_path / "broken.yaml"
    broken.write_text("line: L1\nsteps: [ {id: A1, actor: X}\n", encoding="utf-8")
    with pytest.raises(wf.WorkflowSpecError) as excinfo:
        wf.load_workflow(broken)
    assert excinfo.value.code == "WF_YAML_INVALID"

    with pytest.raises(wf.WorkflowSpecError) as missing:
        wf.load_workflow(tmp_path / "nope.yaml")
    assert missing.value.code == "WF_YAML_NOT_FOUND"

    assert excinfo.value.message.splitlines()[0]


def test_execution_follows_dependency_order_not_declaration_order(tmp_path):
    spec = inline_spec(
        {"id": "N2", "actor": "X", "depends_on": ["N1"], "output": ["b"]},
        {"id": "N1", "actor": "X", "output": ["a"]},
        {"id": "N3", "actor": "X", "depends_on": ["N2"], "output": ["c"]},
    )
    assert spec.ids == ("N1", "N2", "N3")
    store = new_store(tmp_path)
    job_id = new_job(store)
    calls = []
    seen_inputs = {}

    def handler(ctx):
        node_id = ctx["node"].id
        calls.append(node_id)
        seen_inputs[node_id] = dict(ctx["inputs"])
        return {"produced": node_id}

    executor = wf.WorkflowExecutor(store, spec, job_id,
                                   handlers={nid: handler for nid in spec.ids})
    summary = executor.run()
    assert summary["state"] == "COMPLETED"
    assert calls == ["N1", "N2", "N3"]
    assert seen_inputs["N2"] == {"N1": {"produced": "N1"}}
    assert seen_inputs["N1"] == {}


def test_full_11_node_flow_persists_and_completes(tmp_path):
    spec = wf.load_workflow(L1_PATH)
    store = new_store(tmp_path)
    job_id = new_job(store)
    calls = []

    def handler(ctx):
        calls.append(ctx["node"].id)
        return {"produced": ctx["node"].id, "actor": ctx["node"].actor}

    handlers = {node.id: handler for node in spec.nodes}
    executor = wf.WorkflowExecutor(store, spec, job_id, handlers=handlers)
    summary = drive(executor)
    assert summary["state"] == "COMPLETED", summary
    assert summary["total"] == 11 and summary["done"] == 11
    assert summary["failed"] == [] and summary["waiting_approval"] == []
    assert calls == L1_IDS

    tasks = {task["node"]: task for task in store.list_tasks(job_id)}
    assert set(tasks) == set(L1_IDS)
    for node_id in L1_IDS:
        task = tasks[node_id]
        assert task["status"] == "DONE", node_id
        assert task["job_id"] == job_id
        assert task["payload"]["run_id"] == executor.run_id
        assert task["payload"]["node_id"] == node_id
        assert task["result"]["attempts"] == 1
        assert task["result"]["outputs"]["produced"] == node_id
        assert task["result"]["evidence_id"] in summary["evidence_ids"]
        transitions = [item["to"] for item in task["history"]]
        assert transitions[0] == "READY"
        assert "RUNNING" in transitions and transitions[-1] == "DONE"
    assert tasks["L1-08"]["payload"]["depends_on"] == ["L1-05", "L1-07"]
    assert tasks["L1-04"]["result"]["approval"]["role"] == "PRINCIPAL"
    assert tasks["L1-04"]["approver"] == "reviewer"

    evidence = store.list_evidence(job_id)
    assert len(evidence) == 11
    assert {item["payload"]["node_id"] for item in evidence} == set(L1_IDS)
    for item in evidence:
        assert item["payload"]["job_id"] == job_id
        assert item["payload"]["run_id"] == executor.run_id
        assert len(item["sha256"]) == 64
    records = store.get_records(job_id, "workflow_node:L1-11")
    assert records and records[0]["outputs"]["produced"] == "L1-11"
    runs = store.get_records(job_id, "workflow_run")
    assert runs and runs[-1]["state"] == "COMPLETED"

    actions = [row["action"] for row in store.list_audit(500)]
    for expected in ("WF_NODE_START", "WF_NODE_COMPLETE", "WF_NODE_EVIDENCE", "WF_NODE_APPROVE"):
        assert expected in actions, expected

    # 同一 run_id 新建执行器（模拟重启）：全部 DONE，绝不重跑任何节点。
    rebooted = wf.WorkflowExecutor(store, spec, job_id, run_id=executor.run_id,
                                   handlers=handlers)
    again = rebooted.run()
    assert again["state"] == "COMPLETED" and again["done"] == 11
    assert calls == L1_IDS


def test_approval_blocks_downstream_until_approved(tmp_path):
    spec = wf.load_workflow(L1_PATH)
    store = new_store(tmp_path)
    job_id = new_job(store)
    executor = wf.WorkflowExecutor(store, spec, job_id)  # 默认 handler：按 YAML output 产出

    summary = executor.run()
    assert summary["state"] == "PAUSED"
    assert summary["waiting_approval"] == ["L1-04"]
    assert summary["done"] == 3
    assert store.get_task(executor.task_id("L1-05"))["status"] == "READY"
    assert "L1-05" not in summary["next"]
    assert executor.status()["state"] == "PAUSED"

    with pytest.raises(wf.WorkflowError) as denied:
        executor.approve("L1-04", actor="intruder", role="EDITOR")
    assert denied.value.code == "WF_APPROVAL_ROLE"
    assert store.get_task(executor.task_id("L1-04"))["status"] == "WAITING_APPROVAL"

    executor.approve("L1-04", actor="owner", role="REVIEWER", comment="构件核对通过")
    task = store.get_task(executor.task_id("L1-04"))
    assert task["status"] == "DONE" and task["approver"] == "owner"
    assert task["result"]["approval"]["comment"] == "构件核对通过"
    with pytest.raises(wf.WorkflowError) as reapproved:
        executor.approve("L1-04", actor="owner", role="REVIEWER")
    assert reapproved.value.code == "WF_APPROVAL_STATE"
    with pytest.raises(wf.WorkflowError) as not_approval:
        executor.approve("L1-05", actor="owner", role="REVIEWER")
    assert not_approval.value.code == "WF_APPROVAL_NOT_REQUIRED"

    summary = executor.run()
    assert summary["state"] == "PAUSED" and summary["waiting_approval"] == ["L1-07"]
    assert summary["done"] == 6
    records = store.get_records(job_id, "workflow_node:L1-05")
    assert records[0]["outputs"]["qty_boq"]["state"] == "SIMULATED"

    executor.approve("L1-07", actor="owner", role="PRINCIPAL")
    summary = executor.run()
    assert summary["waiting_approval"] == ["L1-10"]
    with pytest.raises(wf.WorkflowError) as license_denied:
        executor.approve("L1-10", actor="boss", role="PRINCIPAL")
    assert license_denied.value.code == "WF_APPROVAL_LICENSE"
    assert store.get_task(executor.task_id("L1-10"))["status"] == "WAITING_APPROVAL"

    executor.approve("L1-10", actor="boss", role="PRINCIPAL",
                     license="REGISTERED_COST_ENGINEER", comment="审定通过")
    summary = executor.run()
    assert summary["state"] == "COMPLETED" and summary["done"] == 11


def test_failed_node_resumes_from_checkpoint(tmp_path):
    spec = wf.load_workflow(L1_PATH)
    store = new_store(tmp_path)
    job_id = new_job(store)
    calls: dict[str, int] = {}

    def handler(ctx):
        node_id = ctx["node"].id
        calls[node_id] = calls.get(node_id, 0) + 1
        if node_id == "L1-05" and calls[node_id] == 1:
            raise wf.WorkflowNodeError(
                "QTY_RULE_MISSING", "缺少计量规则 QTY.BOQ.CONC.COLUMN.001")
        return {"produced": node_id, "attempt": calls[node_id]}

    handlers = {node.id: handler for node in spec.nodes}
    executor = wf.WorkflowExecutor(store, spec, job_id, handlers=handlers)
    summary = drive(executor)
    assert summary["state"] == "FAILED"
    assert summary["failed"] == ["L1-05"]
    assert summary["done"] == 4
    failed_task = store.get_task(executor.task_id("L1-05"))
    assert failed_task["status"] == "FAILED"
    assert failed_task["result"]["failure_code"] == "QTY_RULE_MISSING"
    assert "QTY.BOQ.CONC.COLUMN.001" in failed_task["result"]["error"]
    assert store.get_task(executor.task_id("L1-06"))["status"] == "READY"
    assert calls["L1-05"] == 1 and "L1-06" not in calls

    # 模拟进程重启：新执行器同 run_id，从失败节点续跑，已完成节点不重跑。
    rebooted = wf.WorkflowExecutor(store, spec, job_id, run_id=executor.run_id,
                                   handlers=handlers)
    resumed_summary = rebooted.resume()
    assert resumed_summary["retried"] == ["L1-05"]
    summary = drive(rebooted, resumed_summary)
    assert summary["state"] == "COMPLETED"
    assert summary["done"] == 11
    assert calls["L1-05"] == 2
    assert all(calls[node] == 1 for node in L1_IDS if node != "L1-05")
    assert store.get_task(rebooted.task_id("L1-05"))["result"]["attempts"] == 2
    assert store.get_task(rebooted.task_id("L1-04"))["result"]["attempts"] == 1
    assert store.get_task(rebooted.task_id("L1-04"))["status"] == "DONE"

    # 失败也留证据，且只有失败节点产生过 FAILED 证据。
    evidence = store.list_evidence(job_id)
    statuses = [item["payload"]["status"] for item in evidence]
    assert len(evidence) == 12
    assert statuses.count("FAILED") == 1
    assert statuses.count("DONE") == 8
    assert statuses.count("WAITING_APPROVAL") == 3

    history = [item["to"] for item in store.get_task(rebooted.task_id("L1-05"))["history"]]
    assert history == ["READY", "RUNNING", "FAILED", "READY", "RUNNING", "DONE"]


def test_interrupted_running_node_is_recovered(tmp_path):
    spec = wf.load_workflow(L1_PATH)
    store = new_store(tmp_path)
    job_id = new_job(store)
    calls: dict[str, int] = {}
    crash = {"armed": True}

    def handler(ctx):
        node_id = ctx["node"].id
        calls[node_id] = calls.get(node_id, 0) + 1
        if node_id == "L1-02" and crash["armed"]:
            crash["armed"] = False
            raise KeyboardInterrupt("模拟 worker 被杀")
        return {"produced": node_id}

    handlers = {node.id: handler for node in spec.nodes}
    executor = wf.WorkflowExecutor(store, spec, job_id, handlers=handlers)
    with pytest.raises(KeyboardInterrupt):
        executor.run()
    interrupted = store.get_task(executor.task_id("L1-02"))
    assert interrupted["status"] == "RUNNING"
    assert interrupted["result"]["attempts"] == 1
    assert calls["L1-02"] == 1

    resumed = wf.WorkflowExecutor(store, spec, job_id, run_id=executor.run_id,
                                  handlers=handlers)
    summary = drive(resumed, resumed.run())
    assert summary["state"] == "COMPLETED"
    assert calls["L1-01"] == 1 and calls["L1-02"] == 2
    actions = [row["action"] for row in store.list_audit(500)]
    assert "WF_NODE_INTERRUPTED" in actions
    assert store.get_task(resumed.task_id("L1-02"))["result"]["attempts"] == 2


def test_workflow_requires_existing_job(tmp_path):
    spec = wf.load_workflow(L1_PATH)
    store = new_store(tmp_path)
    with pytest.raises(wf.WorkflowError) as excinfo:
        wf.WorkflowExecutor(store, spec, "job-does-not-exist")
    assert excinfo.value.code == "WF_JOB_NOT_FOUND"
