"""G0–G5 门禁（TX-019 扩展）。

对应 V3.1 门禁体系：G0 立项与依据包、G1 资料与解析、G2 量与清单、G3a 计价、
G3b 三级复核闭环、G3c 审定签署、G4 成果、G5 归档与知识。J 线（鉴定）由调用方传 gate="J0".."J5"。

P0-05-JGATE：J0–J5 只允许影子运行。门禁全部通过也不得自动签发，正式成果默认 SHADOW，
必须有人工签署记录（j_signoff）才可由显式签发接口出件；鉴定口径与编制口径不得混用。
错误码只取 24 主码；细分原因放在 failed_rules[].rule_id 与 message。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

from .basis import validate_basis_pack

REQUIRED = {
    "L1": {"contract_or_entrustment", "drawing_set", "design_description", "finish_schedule"},
    "L2": {"submitted_budget", "review_scope", "contract_or_tender_basis", "drawing_set"},
    "L3": {"contract", "as_built_drawings", "change_visa_register", "measurement_records"},
    "L4": {"submitted_settlement", "contract", "as_built_drawings", "prior_round_snapshot"},
    "L5": {"tender_document", "addenda_register", "boq_file", "bid_forms"},
    "L6": {"authorized_entrustment", "dispute_scope", "party_evidence", "conflict_declaration"},
}

GATE_SEQUENCE = {
    "L1": ["G0", "G1", "G2", "G3a", "G3b", "G3c", "G4", "G5"],
    "L2": ["G0", "G1", "G2", "G3a", "G3b", "G3c", "G4", "G5"],
    "L3": ["G0", "G1", "G2", "G3a", "G3b", "G3c", "G4", "G5"],
    "L4": ["G0", "G1", "G2", "G3a", "G3b", "G3c", "G4", "G5"],
    "L5": ["G0", "G1", "G2", "G3a", "G4", "G5"],
    "L6": ["J0", "J1", "J2", "J3", "J4", "J5"],
}

# ---------------------------------------------------------------------------
# P0-04-GATES 门禁真实性契约
# ---------------------------------------------------------------------------
# 权威门：只能由服务端记录判定。调用方请求体（context / available_documents /
# basis_pack / business_line 自述）一律不参与放行；没有 job_id 时一律 FAIL。
AUTHORITATIVE_GATES = frozenset({
    "G0", "G1", "G2", "G3a", "G3b", "G3c", "G4", "G5",
    "J2", "J3", "J4", "J5",
})
# 提示门：只做参考提示，不构成放行依据（受理阶段尚无服务端事实）。
ADVISORY_GATES = frozenset({"J0", "J1"})

# 每道门实际读取的服务端记录（表名 / 记录种类）。这是门禁唯一的“事实入口”清单：
# 任何判据都必须能对应到下面某一项，否则不得写进门禁。
GATE_SERVER_SOURCES: dict[str, tuple[str, ...]] = {
    "G0": ("job", "job.basis_pack", "basis_pack.sources"),
    "G1": ("job", "job_document", "job_record:boq", "job_record:version_conflict"),
    "G2": ("calc_run", "job_record:reconcile", "job_record:diff"),
    "G3a": ("job_record:price", "calc_run", "evidence"),
    "G3b": ("job_task", "job_record:review_issue", "gate_record"),
    "G3c": ("job_task", "audit_log", "gate_record"),
    "G4": ("job_record:report", "calc_run", "evidence"),
    "G5": ("job_record:archive", "job_record:report"),
    "J0": ("job_document", "job_record:conflict"),
    "J1": ("job_document", "job_record:material"),
    "J2": ("job_document", "job_record:material"),
    "J3": ("job_document", "job_record:dispute"),
    "J4": ("job_document", "job_record:calc_support", "job_record:opinion"),
    "J5": ("job_document", "job_record:opinion"),
}

# 无法满足时的失败规则清单：rule_id / error_code / 触发条件 / 依据的服务端记录。
GATE_FAILURE_RULES: dict[str, list[dict[str, str]]] = {
    "G0": [
        {"rule_id": "G0.BASIS", "error_code": "BASIS_PACK_MISSING",
         "condition": "job.basis_pack 缺失、无法解析或未锁定", "source": "job.basis_pack"},
        {"rule_id": "G0.BASIS", "error_code": "INTAKE_GATE_NOT_READY",
         "condition": "依据来源未验证 / 测试数据 / 版本范围不一致", "source": "basis_pack.sources"},
    ],
    "G1": [
        {"rule_id": "G1.DOCS", "error_code": "DOC_GATE_NOT_READY",
         "condition": "服务端 job_document 缺必备 doc_key（且 verified=1）", "source": "job_document"},
        {"rule_id": "G1.UNVERIFIED", "error_code": "DOC_GATE_NOT_READY",
         "condition": "存在未通过校验（verified=0）的资料", "source": "job_document"},
        {"rule_id": "G1.SHA", "error_code": "DOC_GATE_NOT_READY",
         "condition": "资料记录缺文件指纹 file_sha256", "source": "job_document"},
        {"rule_id": "G1.BOQ", "error_code": "DOC_GATE_NOT_READY",
         "condition": "服务端无 job_record(kind=boq) 导入记录", "source": "job_record:boq"},
        {"rule_id": "G1.VERSION", "error_code": "VERSION_CONFLICT",
         "condition": "服务端存在 job_record(kind=version_conflict)", "source": "job_record:version_conflict"},
    ],
    "G2": [
        {"rule_id": "G2.QTY", "error_code": "GATE_QTY_NOT_READY",
         "condition": "服务端无 calc_run 记录", "source": "calc_run"},
        {"rule_id": "G2.EVIDENCE", "error_code": "GATE_QTY_NOT_READY",
         "condition": "存在缺 snapshot_sha256 的计算记录", "source": "calc_run"},
        {"rule_id": "G2.RECONCILE", "error_code": "GATE_QTY_RECONCILE_FAILED",
         "condition": "无 job_record(kind=reconcile) 或 reconciled != true", "source": "job_record:reconcile"},
        {"rule_id": "G2.BINDING", "error_code": "GATE_QTY_RECONCILE_FAILED",
         "condition": "reconcile 未绑定 calc_run_ids，或绑定的 run 不存在/不属于本 job", "source": "job_record:reconcile"},
        {"rule_id": "G2.SOURCE", "error_code": "GATE_QTY_RECONCILE_FAILED",
         "condition": "strict 模式下 reconcile 非服务端写入（_source != SERVER）", "source": "job_record:reconcile"},
        {"rule_id": "G2.DIFF", "error_code": "GATE_DIFF_UNEXPLAINED",
         "condition": "job_record(kind=diff).unexplained > 0", "source": "job_record:diff"},
    ],
    "G3a": [
        {"rule_id": "G3a.PRICE", "error_code": "PRICE_GATE_NOT_READY",
         "condition": "服务端无 job_record(kind=price) 且无 calc_run", "source": "job_record:price"},
        {"rule_id": "G3a.SOURCE", "error_code": "PRICE_GATE_NOT_READY",
         "condition": "组价记录缺 source_ref 依据来源", "source": "job_record:price"},
        {"rule_id": "G3a.BINDING", "error_code": "PRICE_GATE_NOT_READY",
         "condition": "strict 模式下组价缺 evidence_id 证据绑定", "source": "evidence"},
        {"rule_id": "G3a.CLIENT", "error_code": "PRICE_GATE_NOT_READY",
         "condition": "strict 模式下组价记录非服务端写入", "source": "job_record:price"},
    ],
    "G3b": [
        {"rule_id": "G3b.SELF_REVIEW", "error_code": "SUMMARY_REVIEW_FAILED",
         "condition": "缺 SELF_REVIEW 任务或未 DONE", "source": "job_task"},
        {"rule_id": "G3b.REVIEW", "error_code": "SUMMARY_REVIEW_FAILED",
         "condition": "缺 REVIEW 任务或未 DONE", "source": "job_task"},
        {"rule_id": "G3b.APPROVE", "error_code": "SUMMARY_REVIEW_FAILED",
         "condition": "缺 APPROVE 任务或未 DONE", "source": "job_task"},
        {"rule_id": "G3b.SEQUENCE", "error_code": "REVIEW_SEQUENCE_INVALID",
         "condition": "审批时间序与 SELF→REVIEW→APPROVE 不符", "source": "job_task"},
        {"rule_id": "G3b.CLOSED", "error_code": "SUMMARY_REVIEW_FAILED",
         "condition": "job_record(kind=review_issue).status == OPEN 未闭环", "source": "job_record:review_issue"},
    ],
    "G3c": [
        {"rule_id": "G3c.APPROVE", "error_code": "SUMMARY_REVIEW_FAILED",
         "condition": "缺 APPROVE 任务或未 DONE", "source": "job_task"},
        {"rule_id": "G3c.SEAL", "error_code": "SEAL_NOT_ALLOWED",
         "condition": "APPROVE 任务无 approver / approved_at 签署留痕", "source": "job_task"},
        {"rule_id": "G3c.AUDIT", "error_code": "SEAL_NOT_ALLOWED",
         "condition": "audit_log 无该任务 TASK_APPROVE 留痕", "source": "audit_log"},
    ],
    "G4": [
        {"rule_id": "G4.REPORT", "error_code": "REPORT_GATE_NOT_READY",
         "condition": "无 job_record(kind=report) 或 generated != true", "source": "job_record:report"},
        {"rule_id": "G4.LAYOUT", "error_code": "REPORT_GATE_NOT_READY",
         "condition": "layout_checked != true", "source": "job_record:report"},
        {"rule_id": "G4.EVIDENCE", "error_code": "REPORT_GATE_NOT_READY",
         "condition": "calc_run 快照指纹覆盖不足（存在无 snapshot_sha256 的 run）", "source": "calc_run"},
        {"rule_id": "G4.BINDING", "error_code": "REPORT_GATE_NOT_READY",
         "condition": "report 缺 report_sha256 或 evidence_ids 证据绑定", "source": "job_record:report"},
        {"rule_id": "G4.SOURCE", "error_code": "REPORT_GATE_NOT_READY",
         "condition": "strict 模式下 report 非服务端写入", "source": "job_record:report"},
        {"rule_id": "G4.AI", "error_code": "AI_DIRECT_NUMBER_FORBIDDEN",
         "condition": "ai_direct_numbers 非 0，模型直出数值进入成果", "source": "job_record:report"},
    ],
    "G5": [
        {"rule_id": "G5.VOLUME", "error_code": "ARCHIVE_GATE_NOT_READY",
         "condition": "volume_complete != true", "source": "job_record:archive"},
        {"rule_id": "G5.PACKAGE", "error_code": "ARCHIVE_GATE_NOT_READY",
         "condition": "package_sha256 缺失", "source": "job_record:archive"},
        {"rule_id": "G5.BINDING", "error_code": "ARCHIVE_GATE_NOT_READY",
         "condition": "strict 模式下缺卷号 volume_no 绑定", "source": "job_record:archive"},
        {"rule_id": "G5.CLOSE", "error_code": "CLOSURE_GATE_NOT_READY",
         "condition": "indicators_stored != true，指标/案例未入库", "source": "job_record:archive"},
    ],
}

for _gate, _rules in {
    "J0": [("J0.DOC", "JD_ACCEPT_FAILED", "受理材料缺失", "job_document"),
           ("J0.CONFLICT", "JD_ACCEPT_FAILED", "存在回避情形未处理", "job_record:conflict")],
    "J1": [("J1.DOC", "JD_MATERIAL_UNVERIFIED", "材料未质证或台账缺失", "job_document"),
           ("J1.UNVERIFIED", "JD_MATERIAL_UNVERIFIED", "计算引用了未质证材料", "job_record:material")],
    "J2": [("J2.DOC", "JD_ISSUE_NOT_DECOMPOSED", "勘验记录不全", "job_document"),
           ("J2.UNVERIFIED", "JD_MATERIAL_UNVERIFIED", "引用了未通过校验的资料", "job_document")],
    "J3": [("J3.DOC", "JD_ISSUE_NOT_DECOMPOSED", "争议项未分解或方法未选", "job_document"),
           ("J3.SCOPE", "JD_ISSUE_NOT_DECOMPOSED", "无争议部分未隔离", "job_record:dispute"),
           ("J3.UNVERIFIED", "JD_MATERIAL_UNVERIFIED", "引用了未通过校验的资料", "job_document")],
    "J4": [("J4.DOC", "JD_CALC_UNSUPPORTED", "计算或复核记录缺失", "job_document"),
           ("J4.SUPPORT", "JD_CALC_UNSUPPORTED", "存在依据不可溯源的计算", "job_record:calc_support"),
           ("J4.FORM", "JD_OPINION_INCOMPLETE", "证据不足却出确定性意见", "job_record:opinion"),
           ("J4.UNVERIFIED", "JD_CALC_UNSUPPORTED", "引用了未通过校验的资料", "job_document")],
    "J5": [("J5.DOC", "JD_OPINION_INCOMPLETE", "意见书要素缺失", "job_document"),
           ("J5.LEGAL", "JD_OPINION_INCOMPLETE", "意见书含法律判断用语", "job_record:opinion"),
           ("J5.UNVERIFIED", "JD_OPINION_INCOMPLETE", "引用了未通过校验的资料", "job_document")],
}.items():
    GATE_FAILURE_RULES[_gate] = [
        {"rule_id": r, "error_code": c, "condition": cond, "source": src} for r, c, cond, src in _rules
    ]
del _gate, _rules

# ---------------------------------------------------------------------------
# P0-05-JGATE 鉴定线（L6）影子运行 + 人工签署契约
# ---------------------------------------------------------------------------
# 鉴定线 J0–J5 只允许"影子运行"：门禁全部通过也不得自动签发正式成果。
# 出件路径唯一：人工签署记录（j_signoff）→ 显式签发（j_deliverable）。
J_LINE = "L6"
J_GATES: tuple[str, ...] = ("J0", "J1", "J2", "J3", "J4", "J5")
J_GATE_SET = frozenset(J_GATES)

# 这些记录是"出件/签署"本身，只能由 /v1/jgate/{job_id}/signoff|issue 专用接口写入。
# 通用 /v1/job/{job_id}/records 与 /internal/job/{job_id}/records 一律拒收，防伪造。
J_RESERVED_RECORD_KINDS = frozenset({"j_opinion", "j_signoff", "j_deliverable"})
# 鉴定专属事实（回避/质证/争议分解/计算可溯源/意见形态）：必须服务端写入，
# 调用方 client 自述不得作为 J 线门禁判据。
J_SERVER_ONLY_KINDS = frozenset({"conflict", "material", "dispute", "calc_support", "opinion"})
# 编制口径成果（预算/结算报告）不得出现在鉴定线，鉴定成果也不得走编制出件口径。
J_FORBIDDEN_COMPILATION_KINDS = frozenset({"report"})
# 鉴定人执业资格：注册造价工程师 / 鉴定人。
J_SIGNOFF_LICENSES = frozenset({"REGISTERED_COST_ENGINEER", "APPRAISER"})
# 不允许作为签署人的系统/自动身份。人工签署必须落到具体自然人。
J_AUTO_ACTORS = frozenset({
    "", "system", "auto", "automatic", "bot", "ai", "model", "scheduler", "cron",
    "none", "null", "system:auto", "system:job", "系统", "自动", "机器人", "后台",
})
J_DELIVERABLE_STATES = ("SHADOW", "SIGNED", "ISSUED")

GATE_FAILURE_RULES["J5"] = list(GATE_FAILURE_RULES["J5"]) + [
    {"rule_id": "J5.SHADOW", "error_code": "JD_AUTO_ISSUE_FORBIDDEN",
     "condition": "未经人工签署即尝试签发鉴定成果（影子运行默认不可签发）",
     "source": "job_record:j_signoff"},
    {"rule_id": "J5.SIGNOFF", "error_code": "JD_SIGNOFF_REQUIRED",
     "condition": "无人工签署记录：签署人/执业资格/签署时间/意见编号/签署声明不全",
     "source": "job_record:j_signoff"},
    {"rule_id": "J5.STALE", "error_code": "JD_SIGNOFF_STALE",
     "condition": "签署后 J0–J5 门禁结果发生变化，原签署失效，须重新人工签署",
     "source": "gate_record"},
]


def is_j_gate(gate: str) -> bool:
    return gate in J_GATE_SET


def j_shadow_policy() -> dict[str, Any]:
    """鉴定线影子运行策略：对外可查询的硬约束清单。"""
    return {
        "business_line": J_LINE,
        "gate_sequence": list(J_GATES),
        "mode": "SHADOW",
        "shadow_run": True,
        "auto_issue": False,
        "requires_manual_signoff": True,
        "default_deliverable_state": "SHADOW",
        "deliverable_states": list(J_DELIVERABLE_STATES),
        "signoff_licenses": sorted(J_SIGNOFF_LICENSES),
        "reserved_record_kinds": sorted(J_RESERVED_RECORD_KINDS),
        "server_only_kinds": sorted(J_SERVER_ONLY_KINDS),
        "forbidden_compilation_kinds": sorted(J_FORBIDDEN_COMPILATION_KINDS),
        "line_separation": "鉴定口径（J0–J5）与编制口径（G0–G5）不得混用",
        "api_requires_job_id": True,
    }


def gate_authority(gate: str) -> dict[str, Any]:
    """门禁的权威性契约：读哪些服务端记录 + 失败规则清单。"""
    authority = {
        "gate": gate,
        "mode": "SERVER_RECORDS_ONLY" if gate in AUTHORITATIVE_GATES else "ADVISORY",
        "server_sources": list(GATE_SERVER_SOURCES.get(gate, ())),
        "failure_rules": [dict(r) for r in GATE_FAILURE_RULES.get(gate, [])],
    }
    if gate in J_GATE_SET:
        # J 线：即使历史上被标为提示门，API 也强制要求 job_id（无 job_id 一律拒绝）。
        authority.update({
            "shadow_run": True,
            "auto_issue": False,
            "requires_manual_signoff": True,
            "api_requires_job_id": True,
            "business_line": J_LINE,
        })
    return authority


def _fail(rule_id: str, code: str, message: str) -> dict[str, str]:
    return {"rule_id": rule_id, "error_code": code, "message": message}


def _result(line: str, gate: str, failed: list[dict[str, str]], **extra: Any) -> dict[str, Any]:
    payload = {
        "gate": gate,
        "business_line": line,
        "result": "PASS" if not failed else "FAIL",
        "failed_rules": failed,
        # 自证字段：纯函数判据也必须能说明它依据哪份权威清单（真实读取由 gates_service 填 manifest）。
        "authority": gate_authority(gate),
        "advisory": gate in ADVISORY_GATES,
        "authoritative": gate in AUTHORITATIVE_GATES,
    }
    if gate in J_GATE_SET:
        # 影子运行：任何一道 J 门通过都不构成签发依据。
        payload.update({
            "shadow_run": True,
            "auto_issue": False,
            "requires_manual_signoff": True,
            "deliverable_state": "SHADOW",
        })
    payload.update(extra)
    return payload


def check_gate(line: str, gate: str, docs: set[str] | list[str], basis, context: dict[str, Any] | None = None) -> dict[str, Any]:
    ctx = context or {}
    docs = set(docs or [])
    if line not in REQUIRED:
        raise ValueError(f"未知业务线：{line}")

    if gate == "G0":
        info = validate_basis_pack(basis)
        failed = []
        if not info["formal_ready"]:
            code = "BASIS_PACK_MISSING" if info["issues"] else "INTAKE_GATE_NOT_READY"
            for issue in (info["issues"] or ["依据包未锁定或为测试数据"]):
                failed.append(_fail("G0.BASIS", code, issue))
        return _result(line, gate, failed, basis_pack_id=basis.basis_pack_id,
                       warnings=info.get("warnings", []))

    if gate == "G1":
        missing = sorted(REQUIRED[line] - docs)
        failed = [_fail("G1.DOCS", "DOC_GATE_NOT_READY", f"缺少必备资料：{d}") for d in missing]
        unverified = sorted(ctx.get("unverified_documents") or [])
        if unverified:
            failed.append(_fail("G1.UNVERIFIED", "DOC_GATE_NOT_READY",
                                f"资料未通过校验（verified=0）：{unverified}"))
        no_sha = sorted(ctx.get("documents_without_sha") or [])
        if no_sha:
            failed.append(_fail("G1.SHA", "DOC_GATE_NOT_READY",
                                f"资料缺文件指纹 file_sha256：{no_sha}"))
        boq_needed = ctx.get("boq_required", line in {"L1", "L2", "L5"})
        if boq_needed and not ctx.get("boq_imported", False):
            failed.append(_fail("G1.BOQ", "DOC_GATE_NOT_READY", "清单尚未导入或未通过校验"))
        if ctx.get("version_conflict"):
            failed.append(_fail("G1.VERSION", "VERSION_CONFLICT", str(ctx["version_conflict"])))
        return _result(line, gate, failed, missing_documents=missing,
                       unverified_documents=unverified)

    if gate == "G2":
        failed = []
        qty_items = ctx.get("qty_items") or []
        incomplete = [i for i in qty_items if i.get("status") not in {"DONE", "CONFIRMED"}]
        if not qty_items:
            failed.append(_fail("G2.QTY", "GATE_QTY_NOT_READY", "尚无任何工程量计算结果"))
        elif incomplete:
            failed.append(_fail("G2.QTY", "GATE_QTY_NOT_READY", f"{len(incomplete)} 个清单项量未完成"))
        if ctx.get("reconciled") is not True:
            failed.append(_fail("G2.RECONCILE", "GATE_QTY_RECONCILE_FAILED", "勾稽校验未通过或未执行"))
        unexplained = int(ctx.get("unexplained_diffs", 0) or 0)
        if unexplained:
            failed.append(_fail("G2.DIFF", "GATE_DIFF_UNEXPLAINED", f"{unexplained} 处量差超阈值未解释"))
        return _result(line, gate, failed)

    if gate == "G3a":
        failed = []
        price_items = ctx.get("price_items") or []
        if not price_items:
            failed.append(_fail("G3a.PRICE", "PRICE_GATE_NOT_READY", "尚无组价结果"))
        missing_ref = [p for p in price_items if not p.get("source_ref")]
        if missing_ref:
            failed.append(_fail("G3a.SOURCE", "PRICE_GATE_NOT_READY", f"{len(missing_ref)} 条组价缺依据来源"))
        if ctx.get("missing_basis_items"):
            failed.append(_fail("G3a.BASIS", "BASIS_ITEM_MISSING", str(ctx["missing_basis_items"])))
        return _result(line, gate, failed)

    if gate == "G3b":
        failed = []
        levels = ctx.get("review_levels") or []
        expected = ["SELF", "REVIEW", "APPROVE"]
        if levels != expected:
            if sorted(levels) == sorted(expected):
                failed.append(_fail("G3b.SEQUENCE", "REVIEW_SEQUENCE_INVALID", f"复核顺序错误：{levels}"))
            else:
                failed.append(_fail("G3b.SEQUENCE", "SUMMARY_REVIEW_FAILED", f"复核级别不全：{levels}，应为 {expected}"))
        if ctx.get("open_review_issues", 0):
            failed.append(_fail("G3b.CLOSED", "SUMMARY_REVIEW_FAILED", f"{ctx['open_review_issues']} 条复核意见未闭环"))
        return _result(line, gate, failed)

    if gate == "G3c":
        failed = []
        seal = ctx.get("seal") or {}
        if not seal.get("signed_by"):
            failed.append(_fail("G3c.SEAL", "SEAL_NOT_ALLOWED", "无审定人签署记录"))
        if seal.get("license") not in {"REGISTERED_COST_ENGINEER", "APPRAISER"}:
            failed.append(_fail("G3c.SEAL", "SEAL_NOT_ALLOWED", f"签署人资格不符：{seal.get('license')}"))
        return _result(line, gate, failed)

    if gate == "G4":
        failed = []
        report = ctx.get("report") or {}
        if not report.get("generated"):
            failed.append(_fail("G4.REPORT", "REPORT_GATE_NOT_READY", "成果文件未生成"))
        if report.get("layout_checked") is not True:
            failed.append(_fail("G4.LAYOUT", "REPORT_GATE_NOT_READY", "版式/签章位未校验"))
        coverage = ctx.get("evidence_coverage")
        if coverage is None or coverage < 1:
            failed.append(_fail("G4.EVIDENCE", "REPORT_GATE_NOT_READY", f"证据链覆盖率不足：{coverage}"))
        if ctx.get("ai_direct_numbers"):
            failed.append(_fail("G4.AI", "AI_DIRECT_NUMBER_FORBIDDEN", "存在由模型直接产生的数值进入成果"))
        return _result(line, gate, failed)

    if gate == "G5":
        failed = []
        archive = ctx.get("archive") or {}
        if not archive.get("volume_complete"):
            failed.append(_fail("G5.VOLUME", "ARCHIVE_GATE_NOT_READY", "卷内不齐或页码不连续"))
        if not archive.get("package_sha256"):
            failed.append(_fail("G5.PACKAGE", "ARCHIVE_GATE_NOT_READY", "归档包未生成或无指纹"))
        if archive.get("indicators_stored") is not True:
            failed.append(_fail("G5.CLOSE", "CLOSURE_GATE_NOT_READY", "收尾未完成：指标/案例未入库"))
        return _result(line, gate, failed)

    if gate.startswith("J"):
        return _check_j_gate(line, gate, docs, basis, ctx)

    raise ValueError(f"未知门禁：{gate}")


J_REQUIRED = {
    "J0": ["authorized_entrustment", "conflict_declaration", "qualification"],
    "J1": ["material_ledger", "cross_examination_record"],
    "J2": ["survey_notice", "survey_record", "photo_index"],
    "J3": ["dispute_decomposition", "method_selection"],
    "J4": ["calc_book", "review_record"],
    "J5": ["opinion_draft", "citation_list"],
}


def _check_j_gate(line: str, gate: str, docs: set[str], basis, ctx: dict[str, Any]) -> dict[str, Any]:
    failed = []
    if gate == "J0":
        missing = [d for d in J_REQUIRED["J0"] if d not in docs]
        failed += [_fail("J0.DOC", "JD_ACCEPT_FAILED", f"受理材料缺失：{d}") for d in missing]
        if ctx.get("conflict_of_interest"):
            failed.append(_fail("J0.CONFLICT", "JD_ACCEPT_FAILED", "存在回避情形未处理"))
    elif gate == "J1":
        missing = [d for d in J_REQUIRED["J1"] if d not in docs]
        failed += [_fail("J1.DOC", "JD_MATERIAL_UNVERIFIED", f"材料未质证或台账缺失：{d}") for d in missing]
        if ctx.get("unverified_materials_used"):
            failed.append(_fail("J1.UNVERIFIED", "JD_MATERIAL_UNVERIFIED", "计算引用了未质证材料"))
    elif gate == "J2":
        missing = [d for d in J_REQUIRED["J2"] if d not in docs]
        failed += [_fail("J2.DOC", "JD_ISSUE_NOT_DECOMPOSED", f"勘验记录不全：{d}") for d in missing]
    elif gate == "J3":
        missing = [d for d in J_REQUIRED["J3"] if d not in docs]
        failed += [_fail("J3.DOC", "JD_ISSUE_NOT_DECOMPOSED", f"争议项未分解或方法未选：{d}") for d in missing]
        if ctx.get("undisputed_mixed"):
            failed.append(_fail("J3.SCOPE", "JD_ISSUE_NOT_DECOMPOSED", "无争议部分未隔离"))
    elif gate == "J4":
        missing = [d for d in J_REQUIRED["J4"] if d not in docs]
        failed += [_fail("J4.DOC", "JD_CALC_UNSUPPORTED", f"计算或复核记录缺失：{d}") for d in missing]
        if ctx.get("unsupported_calc"):
            failed.append(_fail("J4.SUPPORT", "JD_CALC_UNSUPPORTED", "存在依据不可溯源的计算"))
        form = ctx.get("opinion_form")
        if form == "DEFINITE" and ctx.get("evidence_level") == "LOW":
            failed.append(_fail("J4.FORM", "JD_OPINION_INCOMPLETE", "证据不足却出确定性意见"))
    elif gate == "J5":
        missing = [d for d in J_REQUIRED["J5"] if d not in docs]
        failed += [_fail("J5.DOC", "JD_OPINION_INCOMPLETE", f"意见书要素缺失：{d}") for d in missing]
        # 意见书不得替代法院/仲裁机构作法律判断：显式标记或把意见形态写成法律判断均禁止。
        form = str(ctx.get("opinion_form") or "").upper()
        if ctx.get("illegal_legal_judgement") or form in {"LEGAL_JUDGEMENT", "LEGAL_OPINION"}:
            failed.append(_fail("J5.LEGAL", "JD_OPINION_INCOMPLETE", "意见书含法律判断用语"))
    else:
        raise ValueError(f"未知鉴定门禁：{gate}")
    return _result(line, gate, failed, j_gate=True)


# ---------------------------------------------------------------------------
# P0-05-JGATE 判定助手（纯函数，供 API 层复用；不依赖 store）
# ---------------------------------------------------------------------------
def latest_gate_results(records: Iterable[Mapping[str, Any]] | None) -> dict[str, str]:
    """按判定顺序取每道门最后一次结果（重判以最新为准）。"""
    latest: dict[str, str] = {}
    for record in records or []:
        gate = str(record.get("gate_code") or "")
        if gate:
            latest[gate] = str(record.get("result") or "")
    return latest


def j_gate_status(records: Iterable[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """J0–J5 六门状态：未判定为 PENDING，其余取最后一次判定。"""
    rows = list(records or [])
    latest = latest_gate_results(rows)
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for record in rows:
        grouped.setdefault(str(record.get("gate_code") or ""), []).append(record)
    status: list[dict[str, Any]] = []
    for gate in J_GATES:
        entries = grouped.get(gate, [])
        last = entries[-1] if entries else {}
        status.append({
            "gate": gate,
            "result": latest.get(gate, "PENDING"),
            "passed": latest.get(gate) == "PASS",
            "judged_by": last.get("judged_by"),
            "judged_at": last.get("judged_at"),
            "failed_rules": list(last.get("failed_rules") or []),
            "runs": len(entries),
        })
    return status


def j_gate_snapshot(records: Iterable[Mapping[str, Any]] | None) -> str:
    """六门结果指纹：签署时记账，签发时复核（防止签署后门禁被改判）。"""
    latest = latest_gate_results(records)
    material = {gate: latest.get(gate, "PENDING") for gate in J_GATES}
    raw = json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def check_manual_signoff(signoff: Mapping[str, Any] | None,
                         opinion_id: str | None = None) -> list[dict[str, str]]:
    """人工签署记录校验：默认不可签发，只有合规的人工签署才能解除。

    返回失败规则列表，空列表表示签署有效。
    """
    if not signoff:
        return [_fail("J5.SIGNOFF", "JD_SIGNOFF_REQUIRED",
                      "鉴定成果默认不可签发：尚无人签署记录")]
    failed: list[dict[str, str]] = []
    signed_by = str(signoff.get("signed_by") or "").strip()
    if not signed_by:
        failed.append(_fail("J5.SIGNOFF", "JD_SIGNOFF_REQUIRED", "签署记录缺签署人"))
    elif signed_by.casefold() in J_AUTO_ACTORS or signed_by.casefold().startswith("system"):
        failed.append(_fail("J5.SHADOW", "JD_AUTO_ISSUE_FORBIDDEN",
                            f"签署人不得为系统/自动身份：{signed_by}"))
    if signoff.get("manual") is not True:
        failed.append(_fail("J5.SIGNOFF", "JD_SIGNOFF_REQUIRED",
                            "签署记录未标记人工签署（manual=true）"))
    license_ = str(signoff.get("license") or "")
    if license_ not in J_SIGNOFF_LICENSES:
        failed.append(_fail("J5.SIGNOFF", "JD_SIGNOFF_REQUIRED",
                            f"签署人执业资格不符：{license_ or '缺失'}"))
    if not str(signoff.get("signed_at") or "").strip():
        failed.append(_fail("J5.SIGNOFF", "JD_SIGNOFF_REQUIRED", "签署记录缺签署时间"))
    if len(str(signoff.get("statement") or "").strip()) < 4:
        failed.append(_fail("J5.SIGNOFF", "JD_SIGNOFF_REQUIRED", "缺人工签署声明"))
    if opinion_id and signoff.get("opinion_id") != opinion_id:
        failed.append(_fail("J5.SIGNOFF", "JD_SIGNOFF_REQUIRED",
                            f"签署记录关联意见 {signoff.get('opinion_id')} 与当前意见 {opinion_id} 不一致"))
    return failed


def check_issue_ready(gates: Mapping[str, str] | None,
                      signoff: Mapping[str, Any] | None,
                      opinion_id: str | None = None,
                      snapshot: str | None = None) -> list[dict[str, str]]:
    """签发前复核：意见草稿 + 有效人工签署 + 六门全通过 + 签署未被改判。"""
    gates = gates or {}
    failed: list[dict[str, str]] = []
    if not opinion_id:
        failed.append(_fail("J5.DOC", "JD_OPINION_INCOMPLETE", "无鉴定意见草稿，禁止签发"))
    if not signoff:
        failed.append(_fail("J5.SIGNOFF", "JD_SIGNOFF_REQUIRED",
                            "鉴定成果默认不可签发：尚无人签署记录"))
    else:
        if snapshot and str(signoff.get("gate_snapshot_sha256") or "") != snapshot:
            failed.append(_fail("J5.STALE", "JD_SIGNOFF_STALE",
                                "签署后 J0–J5 门禁结果发生变化，签署失效，需重新人工签署"))
        failed.extend(check_manual_signoff(signoff, opinion_id))
    unresolved = [gate for gate in J_GATES if gates.get(gate) != "PASS"]
    if unresolved:
        failed.append(_fail("J5.GATE", "JD_GATE_NOT_READY",
                            f"鉴定六门未全部通过，禁止签发：{unresolved}"))
    return failed


def j_deliverable_state(gates: Mapping[str, str] | None,
                        signoff: Mapping[str, Any] | None,
                        opinion_id: str | None = None,
                        issued: bool = False) -> str:
    """成果状态：SHADOW（默认）/ SIGNED（已人工签署未出件）/ ISSUED（已出件）。"""
    if issued:
        return "ISSUED"
    if opinion_id and not check_manual_signoff(signoff, opinion_id) \
            and all((gates or {}).get(gate) == "PASS" for gate in J_GATES):
        return "SIGNED"
    return "SHADOW"
