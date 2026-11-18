"""P0-04-GATES / P0-05 门禁服务：从服务端数据库读取真实业务状态，拒绝调用方自报。

与 core/gates.py 的分工：
- core/gates.py 保留纯函数判据 + 门禁权威性契约（GATE_SERVER_SOURCES / GATE_FAILURE_RULES）；
- 本模块负责"取真实状态 → 调判据 → 落 gate_record"，API 只走本模块。

真实性规则（P0-04-GATES）：
1. 调用方传入的 context / available_documents / basis_pack 一律不采信（context_ignored=True），
   唯一事实入口是 job_id；无 job_id 时权威门由 api 层直接判 FAIL。
2. 每次判定都返回 authority_sources，逐条说明读了哪张服务端表、读了几条，
   并附 failure_rule_catalog（无法满足时的失败规则清单与依据来源）。
3. strict 模式（TX_STRICT_GATES=1）下只接受 SERVER 记录，且证据绑定必须齐全；
   非 strict 模式为兼容历史数据而容忍未绑定记录，但仍会校验"已写的绑定"，并把
   被容忍的问题放进 binding_warnings，绝不静默放行。
"""
from __future__ import annotations

from typing import Any

from .gates import (
    ADVISORY_GATES,
    AUTHORITATIVE_GATES,
    GATE_FAILURE_RULES,
    GATE_SEQUENCE,
    GATE_SERVER_SOURCES,
    REQUIRED,
    check_gate,
    gate_authority,
)
from .record_writer import missing_bindings, require_server_record, strict_mode
from ..models import BasisPack

# api.py 既有导入名保持不变；权威门集合以 gates.py 为唯一来源。
AUTHORITY_GATES = AUTHORITATIVE_GATES
APPROVAL_GATE_NODES = {"G3b": ["SELF_REVIEW", "REVIEW", "APPROVE"], "G3c": ["APPROVE"]}
REVIEW_ORDER = ["SELF_REVIEW", "REVIEW", "APPROVE"]
J_VERIFIED_CODE = {"J2": "JD_MATERIAL_UNVERIFIED", "J3": "JD_ISSUE_NOT_DECOMPOSED",
                   "J4": "JD_CALC_UNSUPPORTED", "J5": "JD_OPINION_INCOMPLETE"}


class GateError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _fail(rule_id: str, code: str, message: str, source: str = "") -> dict[str, str]:
    item = {"rule_id": rule_id, "error_code": code, "message": message}
    if source:
        item["source"] = source
    return item


def _bind(failed: list, warnings: list, strict: bool, rule_id: str, code: str,
          message: str, source: str) -> None:
    """证据绑定缺失：strict 直接 FAIL；兼容模式降级为 warning（不静默）。"""
    item = _fail(rule_id, code, message, source)
    (failed if strict else warnings).append(item)


def _read(reads: list, table: str, count: int, detail: str = "") -> None:
    reads.append({"table": table, "count": int(count or 0), "authoritative": True,
                  "detail": detail})


def _latest_by_key(records: list[dict[str, Any]],
                   keys: tuple[str, ...]) -> list[dict[str, Any]]:
    """同一清单项多次写记录时以最后一条为准（重算/重组价覆盖旧记录）。

    这样"补齐绑定"能真正解除门禁，而不会被历史遗留行永久拖住；
    未带任何键的记录按出现顺序各自独立，仍逐条校验。
    """
    latest: dict[str, dict[str, Any]] = {}
    for i, r in enumerate(records):
        key = next((str(r[k]) for k in keys if r.get(k)), f"#{i}")
        latest[key] = r
    return list(latest.values())


PRICE_KEYS = ("price_id", "item", "item_code", "component_id", "source_ref")


def _basis_from_job(job: dict[str, Any]) -> BasisPack:
    try:
        return BasisPack.model_validate(job["basis_pack"])
    except Exception as exc:  # noqa: BLE001
        raise GateError("BASIS_PACK_MISSING", f"任务依据包无法解析：{exc}")


def _finish(store, job_id: str, gate: str, line: str, failed: list, actor: str,
            reads: list, seq: list[str], passed: list[str], warnings: list,
            extra: dict[str, Any] | None = None) -> dict[str, Any]:
    result = "PASS" if not failed else "FAIL"
    store.add_gate_record(job_id, gate, result, failed, actor)
    payload: dict[str, Any] = {
        "gate": gate,
        "business_line": line,
        "result": result,
        "failed_rules": failed,
        "authoritative": True,
        "advisory": False,
        "context_ignored": True,
        "job_id": job_id,
        "gate_sequence": seq,
        "passed_gates": passed + ([gate] if result == "PASS" else []),
        "authority": gate_authority(gate),
        "authority_sources": reads,
        "reads": [r["table"] for r in reads],
        "failure_rule_catalog": GATE_FAILURE_RULES.get(gate, []),
        "binding_required": strict_mode(),
        "binding_warnings": warnings,
        "server_mode": "STRICT" if strict_mode() else "COMPAT",
    }
    if extra:
        payload.update(extra)
    return payload


def evaluate(store, job_id: str, gate: str, actor: str = "system",
             declared_line: str | None = None) -> dict[str, Any]:
    job = store.get_job(job_id)
    if not job:
        raise GateError("INTAKE_GATE_NOT_READY", f"任务不存在：{job_id}")
    line = job["business_line"]
    if declared_line and declared_line != line:
        raise GateError("INTAKE_GATE_NOT_READY",
                        f"调用方声明业务线 {declared_line} 与任务实际业务线 {line} 不一致，拒绝评估")
    seq = GATE_SEQUENCE.get(line, [])
    if gate not in seq:
        raise GateError("INTAKE_GATE_NOT_READY", f"业务线 {line} 不适用门禁 {gate}（允许：{seq}）")

    strict = strict_mode()
    reads: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    _read(reads, "job", 1, f"business_line={line}")

    records = store.list_gate_records(job_id)
    _read(reads, "gate_record", len(records), "已判定门禁记录（用于前置门禁与顺序校验）")
    latest: dict[str, str] = {}
    for r in records:
        latest[r["gate_code"]] = r["result"]
    passed = [g for g in seq if latest.get(g) == "PASS"]
    idx = seq.index(gate)
    missing_prev = [g for g in seq[:idx] if latest.get(g) != "PASS"]
    if missing_prev:
        failed = [_fail(f"{gate}.SEQUENCE", "REVIEW_SEQUENCE_INVALID",
                        f"前置门禁未通过：{missing_prev}", "gate_record")]
        return _finish(store, job_id, gate, line, failed, actor, reads, seq, passed, warnings)

    basis = _basis_from_job(job)
    sources = GATE_SERVER_SOURCES.get(gate, ())
    failed = []
    extra: dict[str, Any] = {}

    doc_rows: list[dict[str, Any]] = []
    docs: set[str] = set()
    unverified: list[str] = []
    no_sha: list[str] = []
    if "job_document" in sources:
        doc_rows = store.list_documents(job_id)
        docs = {d["doc_key"] for d in doc_rows}
        unverified = sorted({d["doc_key"] for d in doc_rows if not d.get("verified")})
        no_sha = sorted({d["doc_key"] for d in doc_rows if not str(d.get("file_sha256") or "").strip()})
        _read(reads, "job_document", len(doc_rows),
              f"verified={len(doc_rows) - len([d for d in doc_rows if not d.get('verified')])}")

    runs: list[dict[str, Any]] = []
    if "calc_run" in sources:
        runs = store.list_calc_runs(job_id)
        _read(reads, "calc_run", len(runs), "计算记录（快照指纹）")

    if gate == "G0":
        _read(reads, "job.basis_pack", 1, f"basis_pack_id={basis.basis_pack_id} status={basis.status}")
        _read(reads, "basis_pack.sources", len(basis.sources or []), "依据来源许可与校验状态")
        failed = check_gate(line, "G0", docs, basis)["failed_rules"]

    elif gate == "G1":
        boq_records = store.get_records(job_id, "boq")
        _read(reads, "job_record:boq", len(boq_records), "清单导入记录")
        conflicts = store.get_records(job_id, "version_conflict")
        _read(reads, "job_record:version_conflict", len(conflicts), "版本冲突登记")
        ctx = {
            "boq_required": line in {"L1", "L2", "L5"},
            "boq_imported": bool(boq_records),
            "version_conflict": (conflicts or [{}])[0].get("detail"),
            "unverified_documents": unverified,
            "documents_without_sha": no_sha,
        }
        failed = check_gate(line, "G1", docs, basis, ctx)["failed_rules"]
        extra["missing_documents"] = sorted(REQUIRED[line] - docs)
        extra["unverified_documents"] = unverified
        extra["documents_without_sha"] = no_sha

    elif gate == "G2":
        if not runs:
            failed.append(_fail("G2.QTY", "GATE_QTY_NOT_READY", "无计算记录（calc_run）", "calc_run"))
        if any(not r.get("snapshot_sha256") for r in runs):
            failed.append(_fail("G2.EVIDENCE", "GATE_QTY_NOT_READY",
                                "存在缺少快照指纹的计算记录", "calc_run"))
        raw_recon = store.get_records(job_id, "reconcile")
        _read(reads, "job_record:reconcile", len(raw_recon), "勾稽通过记录（服务端判定）")
        recon = (require_server_record(store, job_id, "reconcile") or [None])[-1]
        if not recon:
            failed.append(_fail("G2.RECONCILE", "GATE_QTY_RECONCILE_FAILED",
                                "服务端无勾稽通过记录", "job_record:reconcile"))
            if strict and raw_recon:
                failed.append(_fail("G2.SOURCE", "GATE_QTY_RECONCILE_FAILED",
                                    "strict 模式下勾稽记录非服务端写入", "job_record:reconcile"))
        elif recon.get("reconciled") is not True:
            failed.append(_fail("G2.RECONCILE", "GATE_QTY_RECONCILE_FAILED",
                                "最新服务端勾稽记录未通过", "job_record:reconcile"))
        else:
            missing = missing_bindings(store, "reconcile", recon, job_id)
            if missing:
                _bind(failed, warnings, strict, "G2.BINDING", "GATE_QTY_RECONCILE_FAILED",
                      f"勾稽记录缺 calc_run 证据绑定：{missing}", "job_record:reconcile")
        raw_diff = store.get_records(job_id, "diff")
        _read(reads, "job_record:diff", len(raw_diff), "量差解释记录")
        diff = (require_server_record(store, job_id, "diff") or [None])[-1]
        unexplained = int((diff or {}).get("unexplained", 0) or 0)
        if unexplained > 0:
            failed.append(_fail("G2.DIFF", "GATE_DIFF_UNEXPLAINED",
                                f"{unexplained} 处量差未解释", "job_record:diff"))

    elif gate == "G3a":
        raw_prices = store.get_records(job_id, "price")
        _read(reads, "job_record:price", len(raw_prices), "组价记录（按清单项取最新）")
        prices = _latest_by_key(require_server_record(store, job_id, "price"), PRICE_KEYS)
        _read(reads, "evidence", len(prices), "组价依据 evidence_id 绑定")
        if not prices and not runs:
            failed.append(_fail("G3a.PRICE", "PRICE_GATE_NOT_READY", "无组价记录",
                                "job_record:price"))
        bad = [p for p in prices if not p.get("source_ref")]
        if bad:
            failed.append(_fail("G3a.SOURCE", "PRICE_GATE_NOT_READY",
                                f"{len(bad)} 条组价缺依据来源", "job_record:price"))
        if strict and raw_prices and not prices:
            failed.append(_fail("G3a.CLIENT", "PRICE_GATE_NOT_READY",
                                "strict 模式下组价记录非服务端写入", "job_record:price"))
        unbound = [p for p in prices if not p.get("evidence_id")]
        if unbound:
            _bind(failed, warnings, strict, "G3a.BINDING", "PRICE_GATE_NOT_READY",
                  f"{len(unbound)} 条组价缺 evidence_id 证据绑定", "evidence")

    elif gate in {"G3b", "G3c"}:
        tasks = store.list_tasks(job_id)
        _read(reads, "job_task", len(tasks), "三级复核任务与签署留痕")
        by_node: dict[str, list[dict[str, Any]]] = {}
        for t in tasks:
            by_node.setdefault(t["node"], []).append(t)
        need = APPROVAL_GATE_NODES.get(gate, [])
        for node in need:
            items = by_node.get(node, [])
            if not items:
                failed.append(_fail(f"{gate}.{node}", "SUMMARY_REVIEW_FAILED",
                                    f"缺少 {node} 任务", "job_task"))
                continue
            if not all(t["status"] == "DONE" for t in items):
                failed.append(_fail(f"{gate}.{node}", "SUMMARY_REVIEW_FAILED",
                                    f"{node} 未完成", "job_task"))
        if gate == "G3b":
            issues = store.get_records(job_id, "review_issue")
            _read(reads, "job_record:review_issue", len(issues), "复核意见闭环状态")
            open_issues = [i for i in issues if str(i.get("status", "")).upper() == "OPEN"]
            if open_issues:
                failed.append(_fail("G3b.CLOSED", "SUMMARY_REVIEW_FAILED",
                                    f"{len(open_issues)} 条复核意见未闭环", "job_record:review_issue"))
            stamps = [(n, (by_node.get(n) or [{}])[0].get("approved_at") or "")
                      for n in REVIEW_ORDER]
            stamps = [(n, s) for n, s in stamps if s]
            if len(stamps) >= 2:
                ordered = all(stamps[i][1] <= stamps[i + 1][1] for i in range(len(stamps) - 1))
                if not ordered:
                    failed.append(_fail("G3b.SEQUENCE", "REVIEW_SEQUENCE_INVALID",
                                        f"审批时间序与 SELF→REVIEW→APPROVE 不符：{stamps}",
                                        "job_task"))
        else:
            sealed = [t for t in by_node.get("APPROVE", [])
                      if t["status"] == "DONE" and t.get("approver") and t.get("approved_at")]
            if not sealed:
                failed.append(_fail("G3c.SEAL", "SEAL_NOT_ALLOWED",
                                    "无审定人签署记录", "job_task"))
            audits = store.list_audit(1000)
            _read(reads, "audit_log", len(audits), "TASK_APPROVE 签署留痕")
            for t in by_node.get("APPROVE", []):
                hit = any(a["action"] == "TASK_APPROVE" and a["object_id"] == t["task_id"]
                          for a in audits)
                if not hit:
                    failed.append(_fail("G3c.AUDIT", "SEAL_NOT_ALLOWED",
                                        f"任务 {t['task_id']} 无服务端签署留痕", "audit_log"))

    elif gate == "G4":
        raw_reports = store.get_records(job_id, "report")
        _read(reads, "job_record:report", len(raw_reports), "成果生成记录")
        rep = (require_server_record(store, job_id, "report") or [{}])[-1]
        _read(reads, "evidence", len(rep.get("evidence_ids") or []), "成果证据链绑定")
        if not rep.get("generated"):
            failed.append(_fail("G4.REPORT", "REPORT_GATE_NOT_READY",
                                "服务端无成果生成记录", "job_record:report"))
        if rep.get("layout_checked") is not True:
            failed.append(_fail("G4.LAYOUT", "REPORT_GATE_NOT_READY", "版式未校验",
                                "job_record:report"))
        if runs:
            covered = sum(1 for r in runs if r.get("snapshot_sha256"))
            if covered != len(runs):
                failed.append(_fail("G4.EVIDENCE", "REPORT_GATE_NOT_READY",
                                    f"证据覆盖率 {covered}/{len(runs)}", "calc_run"))
        if rep:
            missing = missing_bindings(store, "report", rep, job_id)
            if missing:
                _bind(failed, warnings, strict, "G4.BINDING", "REPORT_GATE_NOT_READY",
                      f"成果记录缺证据绑定：{missing}", "job_record:report")
        if strict and rep.get("_source") != "SERVER":
            failed.append(_fail("G4.SOURCE", "REPORT_GATE_NOT_READY",
                                "成果记录非服务端生成，strict 模式禁止放行", "job_record:report"))
        if rep.get("ai_direct_numbers"):
            failed.append(_fail("G4.AI", "AI_DIRECT_NUMBER_FORBIDDEN",
                                "成果含模型直出数值", "job_record:report"))

    elif gate == "G5":
        raw_arc = store.get_records(job_id, "archive")
        _read(reads, "job_record:archive", len(raw_arc), "归档包记录")
        arcs = require_server_record(store, job_id, "archive")
        arc = (arcs or [{}])[-1]
        _read(reads, "job_record:report", len(store.get_records(job_id, "report")),
              "归档前置成果记录")
        if not arc.get("volume_complete"):
            failed.append(_fail("G5.VOLUME", "ARCHIVE_GATE_NOT_READY", "卷内不齐",
                                "job_record:archive"))
        if not arc.get("package_sha256"):
            failed.append(_fail("G5.PACKAGE", "ARCHIVE_GATE_NOT_READY", "归档包无指纹",
                                "job_record:archive"))
        if arc:
            missing = missing_bindings(store, "archive", arc, job_id)
            if missing:
                _bind(failed, warnings, strict, "G5.BINDING", "ARCHIVE_GATE_NOT_READY",
                      f"归档记录缺证据绑定：{missing}", "job_record:archive")
        if arc.get("indicators_stored") is not True:
            failed.append(_fail("G5.CLOSE", "CLOSURE_GATE_NOT_READY", "指标未入库",
                                "job_record:archive"))

    elif gate.startswith("J"):
        ctx = {
            "conflict_of_interest": (store.get_records(job_id, "conflict") or [{}])[-1].get("exists"),
            "unverified_materials_used": (store.get_records(job_id, "material") or [{}])[-1].get("unverified_used"),
            "undisputed_mixed": (store.get_records(job_id, "dispute") or [{}])[-1].get("undisputed_mixed"),
            "unsupported_calc": (store.get_records(job_id, "calc_support") or [{}])[-1].get("unsupported"),
            "opinion_form": (store.get_records(job_id, "opinion") or [{}])[-1].get("form"),
            "evidence_level": (store.get_records(job_id, "opinion") or [{}])[-1].get("evidence_level"),
        }
        for kind in ("conflict", "material", "dispute", "calc_support", "opinion"):
            n = len(store.get_records(job_id, kind))
            if n or f"job_record:{kind}" in sources:
                _read(reads, f"job_record:{kind}", n)
        failed = check_gate(line, gate, docs, basis, ctx)["failed_rules"]
        if unverified:
            code = J_VERIFIED_CODE.get(gate, "JD_MATERIAL_UNVERIFIED")
            failed.append(_fail(f"{gate}.UNVERIFIED", code,
                                f"引用了未通过校验的资料：{unverified}", "job_document"))
    else:
        raise GateError("INTAKE_GATE_NOT_READY", f"未知门禁：{gate}")

    return _finish(store, job_id, gate, line, failed, actor, reads, seq, passed, warnings,
                   extra={**extra, "docs_required": sorted(REQUIRED.get(line, set()))})


def advisory(gate: str) -> bool:
    """该门是否只是提示门（不构成放行依据）。"""
    return gate in ADVISORY_GATES
