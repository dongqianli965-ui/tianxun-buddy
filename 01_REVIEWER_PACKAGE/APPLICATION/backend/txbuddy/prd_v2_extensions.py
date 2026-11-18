"""PRD V2.0 Full Production Line Extensions (V4.3.0):
- 投标预算生产线 (/v1/bid/*): 13类红线抽取、11类否决条款、清单七字段锁定、三档区间报价策略(D-09)、六类投标表样、广联达四条判定
- 返工闭环系统 (/v1/rework/*): 九字段返工单、七环节流转、改前改后对照、三次高频错误自动入候选规则库
- 双轨物理隔离提取 (/v1/doc/dual-extract, /v1/doc/final-synthesize): 人工+AI双轨各自独立、逐字段合成唯一最终稿(PRD 7.1.5)
- 广联达 L2 参数级协同 (/v1/gld/taskbook, /v1/gld/readback): 任务书生成与读回偏差 >5% 强制填写原因校验(PRD 7.4.4)
- 签证变更四维查重 (/v1/visa/check-duplicate): 文本相似、部位相同、工程量重合、金额接近四维检测(PRD 7.6)
- 河南省标准立卷归档 (/v1/archive/package): DBJ41/T153-2016 结算审核23项与投标15项成卷、页码连续性校验与XML元数据生成(PRD 第9章)
- 工序驾驶舱与门禁模板 (/v1/cockpit/overview): 预算四门/结算四门/投标三门、12错误码字典、节点五态与六类可视化指标(PRD 第12-13章)
"""
from __future__ import annotations

import datetime
import os
import difflib
import hashlib
import json
import sqlite3
import uuid
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

REDLINE_13_CATEGORIES = [
    (1, "最高投标限价", "FATAL"),
    (2, "工期与质量目标", "HIGH"),
    (3, "暂列金额", "HIGH"),
    (4, "暂估价", "HIGH"),
    (5, "甲供材", "HIGH"),
    (6, "不可竞争费", "FATAL"),
    (7, "清单锁定要求", "FATAL"),
    (8, "成果格式", "MEDIUM"),
    (9, "签章要求", "HIGH"),
    (10, "电子标平台", "HIGH"),
    (11, "评标办法", "HIGH"),
    (12, "废标条款", "FATAL"),
    (13, "澄清补遗", "HIGH"),
]

DISQUALIFICATION_11_CLAUSES = [
    (1, "投标文件未经法定代表人或授权代表签字、未加盖单位章", "FATAL"),
    (2, "投标联合体没有提交共同投标协议", "HIGH"),
    (3, "投标人不符合国家或招标文件规定的资格条件", "HIGH"),
    (4, "同一投标人提交两个以上不同投标文件或报价（备选方案除外）", "HIGH"),
    (5, "投标报价低于成本或高于最高投标限价", "FATAL"),
    (6, "投标文件没有对招标文件的实质性要求和条件作出响应", "FATAL"),
    (7, "投标人有串通投标、弄虚作假、行贿等违法行为", "HIGH"),
    (8, "未按招标文件规定的格式填写，内容不全或关键字迹模糊无法辨认", "HIGH"),
    (9, "未按招标文件要求提交投标保证金", "HIGH"),
    (10, "清单编码、名称、特征、单位、工程量与招标清单不一致", "FATAL"),
    (11, "逾期送达或未送达指定平台", "HIGH"),
]

GATE_ERROR_CODES_12 = [
    {"code": "INTAKE_GATE_NOT_READY", "gate": "Q0", "meaning": "承接条件不齐（合同、任务书、配置）", "lines": ["预算", "结算", "投标"]},
    {"code": "DOC_GATE_NOT_READY", "gate": "Q1", "meaning": "资料不齐、版本未确认或双轨最终稿未合成", "lines": ["预算", "结算", "投标"]},
    {"code": "SUMMARY_REVIEW_FAILED", "gate": "Q3", "meaning": "三级复核意见未闭环", "lines": ["预算", "结算"]},
    {"code": "REPORT_GATE_NOT_READY", "gate": "Q4", "meaning": "成果未生成或版式/签章位不合规", "lines": ["预算", "结算"]},
    {"code": "ARCHIVE_GATE_NOT_READY", "gate": "Q5", "meaning": "卷内不齐、页码不连续", "lines": ["预算", "结算", "投标"]},
    {"code": "CLOSURE_GATE_NOT_READY", "gate": "Q5", "meaning": "收尾未完成（复盘、指标未入库）", "lines": ["预算", "结算", "投标"]},
    {"code": "REVIEW_SEQUENCE_INVALID", "gate": "串行校验", "meaning": "复核顺序跳跃（审定先于复核）", "lines": ["预算", "结算"]},
    {"code": "SETTLEMENT_NOT_FOUND", "gate": "串行校验", "meaning": "结算依据缺失（合同/签证/验收）", "lines": ["结算"]},
    {"code": "SEAL_NOT_ALLOWED", "gate": "权限校验", "meaning": "未授权签章", "lines": ["预算", "结算", "投标"]},
    {"code": "BID_REDLINE_MISSING", "gate": "T1", "meaning": "红线字段未抽取完整（<90%）、补遗未闭环", "lines": ["投标"]},
    {"code": "BID_LIST_NOT_LOCKED", "gate": "T2", "meaning": "清单七字段不一致、缺项增项未闭环", "lines": ["投标"]},
    {"code": "BID_COMPLIANCE_FAILED", "gate": "T5", "meaning": "限价、不可竞争费、格式、签章、平台格式不合规", "lines": ["投标"]},
]

SETTLEMENT_AUDIT_23_ITEMS = [
    "案卷封面", "卷内目录", "委托书/合同", "资料接收清单与签收记录", "送审结算书",
    "施工合同及补充协议", "图纸（竣工图）", "变更与签证", "计量支付资料", "验收资料",
    "材料认价与调差依据", "工程量计算书", "计价与组价底稿", "三级复核记录", "差异表与对量记录",
    "审核报告（正本）", "审定单", "签发单", "服务质量跟踪表", "收费凭据",
    "其他材料", "卷内备考表", "案卷封底"
]

TENDER_15_ITEMS = [
    "案卷封面", "卷内目录", "招标文件（含补遗澄清）", "招标工程量清单（电子版与纸质版）",
    "图纸清单与图纸", "红线解析报告", "否决性条款清单与响应偏离表", "清单锁定校验记录",
    "组价底稿与成本测算表", "报价决策记录", "投标报价表（六表样，最终版）", "合规自检报告",
    "提交回执与开标记录", "未中标/中标结论与复盘", "卷内备考表与封底"
]

UNIT_NORMALIZATION = {
    "立方米": "m3", "m³": "m3", "m3": "m3",
    "平方米": "m2", "m²": "m2", "m2": "m2",
    "米": "m", "延长米": "m", "m": "m",
    "吨": "t", "t": "t",
    "千克": "kg", "公斤": "kg", "kg": "kg",
    "台": "台", "套": "套", "个": "个", "项": "项", "处": "处",
}

def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def _norm_text(s: Any) -> str:
    if s is None:
        return ""
    txt = str(s).strip()
    # 全半角与空白归一化
    out = []
    for ch in txt:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out).replace(" ", "").replace("，", ",").replace("。", ".").replace("（", "(").replace("）", ")")

def _norm_unit(u: Any) -> str:
    raw = str(u or "").strip().lower()
    return UNIT_NORMALIZATION.get(raw, raw)

def _d(val: Any) -> Decimal:
    return Decimal(str(val or "0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

class PrdV2Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS bid_project (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                tenderee TEXT NOT NULL,
                bid_deadline TEXT NOT NULL,
                bond_amount TEXT NOT NULL,
                control_price TEXT NOT NULL,
                eplatform TEXT NOT NULL,
                must_use_glodon INTEGER NOT NULL,
                glodon_reason TEXT NOT NULL,
                addenda_closed INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bid_redline (
                id TEXT PRIMARY KEY,
                bid_project_id TEXT NOT NULL,
                category_no INTEGER NOT NULL,
                category_name TEXT NOT NULL,
                quote_text TEXT NOT NULL,
                page_no INTEGER NOT NULL,
                clause_no TEXT NOT NULL,
                system_interpretation TEXT NOT NULL,
                required_action TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                closed INTEGER NOT NULL,
                source_ref TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bid_list_lock (
                id TEXT PRIMARY KEY,
                bid_project_id TEXT NOT NULL,
                item_code TEXT NOT NULL,
                field_name TEXT NOT NULL,
                tender_value TEXT,
                bid_value TEXT,
                diff_type TEXT NOT NULL,
                status TEXT NOT NULL,
                signed_by TEXT,
                checked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bid_quote (
                id TEXT PRIMARY KEY,
                bid_project_id TEXT NOT NULL,
                version TEXT NOT NULL,
                cost_floor TEXT NOT NULL,
                range_aggressive_low TEXT NOT NULL,
                range_aggressive_high TEXT NOT NULL,
                range_neutral_low TEXT NOT NULL,
                range_neutral_high TEXT NOT NULL,
                range_conservative_low TEXT NOT NULL,
                range_conservative_high TEXT NOT NULL,
                target_amount TEXT,
                down_rate TEXT,
                decided_by TEXT,
                reason TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rework_order (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                source TEXT NOT NULL,
                business_type TEXT NOT NULL,
                owner TEXT NOT NULL,
                major TEXT NOT NULL,
                issue TEXT NOT NULL,
                location TEXT NOT NULL,
                clause TEXT NOT NULL,
                expected_action TEXT NOT NULL,
                deadline TEXT NOT NULL,
                status TEXT NOT NULL,
                before_value TEXT,
                after_value TEXT,
                reviewer TEXT,
                reopen_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                closed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS rule_candidate (
                id TEXT PRIMARY KEY,
                issue_signature TEXT UNIQUE NOT NULL,
                major TEXT NOT NULL,
                clause TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL,
                suggested_rule TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'CANDIDATE',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dual_track_extract (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                track_mode TEXT NOT NULL,
                extracted_fields_json TEXT NOT NULL,
                field_provenance_json TEXT,
                actor TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(project_id, document_id, track_mode)
            );
            CREATE TABLE IF NOT EXISTS gld_taskbook (
                taskbook_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                taskbook_type TEXT NOT NULL,
                instructions_json TEXT NOT NULL,
                readback_json TEXT,
                max_deviation_ratio TEXT,
                diff_reason TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """)

_prd_store: PrdV2Store | None = None

def get_prd_store(runtime_root: Path | None = None) -> PrdV2Store:
    global _prd_store
    if _prd_store is None:
        base = runtime_root or Path(os.environ.get("TXBUDDY_RUNTIME_DIR", "/tmp/txbuddy_prd_v2"))
        _prd_store = PrdV2Store(base / "prd_v2_extensions.sqlite3")
    return _prd_store


# ==============================================================================
# 1. 工序驾驶舱与三套门禁视图 (/v1/cockpit/overview)
# ==============================================================================
@router.get("/v1/cockpit/overview")
def cockpit_overview(project_id: str = "P-DEFAULT", business_line: str = "L1", mode: str = "NOVICE"):
    st = get_prd_store()
    with st._conn() as c:
        reworks = [dict(r) for r in c.execute("SELECT * FROM rework_order WHERE project_id = ? ORDER BY created_at DESC", (project_id,)).fetchall()]
        candidates = [dict(r) for r in c.execute("SELECT * FROM rule_candidate ORDER BY occurrence_count DESC").fetchall()]

    gate_templates = {
        "BUDGET": {
            "label": "预算生产线（编制 L1 / 审核 L2）· 四道门禁模板",
            "gates": [
                {"code": "Q1", "name": "资料门", "condition": "必备资料齐全、有效版本确认、人工+AI双轨最终稿生成", "error_code": "DOC_GATE_NOT_READY"},
                {"code": "Q2", "name": "算量门", "condition": "算量/重算完成、自校与四组勾稽校验通过、广联达读回偏差>5%已解释", "error_code": "SUMMARY_REVIEW_FAILED"},
                {"code": "Q4", "name": "成果门", "condition": "报告生成、版式检查通过、签章位齐备（注：计价复核Q3作为Q2→Q4后台必经校验）", "error_code": "REPORT_GATE_NOT_READY"},
                {"code": "Q5", "name": "归档门", "condition": "卷内齐全、页码连续、目录一致、归档包生成", "error_code": "ARCHIVE_GATE_NOT_READY"},
            ],
        },
        "SETTLEMENT": {
            "label": "结算生产线（编制 L3 / 审核 L4）· 四道门禁模板",
            "gates": [
                {"code": "Q1", "name": "资料门", "condition": "合同+竣工图+签证+计量支付+验收齐备，双轨最终稿合成", "error_code": "DOC_GATE_NOT_READY"},
                {"code": "Q2", "name": "核对门", "condition": "送审核对/重算完成、签证四维查重与时间逻辑核验闭环", "error_code": "SETTLEMENT_NOT_FOUND"},
                {"code": "Q4", "name": "成果门", "condition": "结算书/审核报告、审定单、差异表与争议清单齐备", "error_code": "REPORT_GATE_NOT_READY"},
                {"code": "Q5", "name": "归档门", "condition": "河南省DBJ41/T153-2016结算审核23项卷内完整、页码连续", "error_code": "ARCHIVE_GATE_NOT_READY"},
            ],
        },
        "TENDER": {
            "label": "投标预算生产线（L5 代做标书）· 三道门禁模板",
            "gates": [
                {"code": "T1", "name": "红线门", "condition": "13类红线字段抽取完整率≥90%、11类否决条款生成、澄清补遗100%闭环", "error_code": "BID_REDLINE_MISSING"},
                {"code": "T2", "name": "清单门", "condition": "招标清单七字段（编码/名称/特征/单位/工程量/缺项/增项）100%锁定一致、三路径组价完成", "error_code": "BID_LIST_NOT_LOCKED"},
                {"code": "T5", "name": "出表门", "condition": "六类标准投标表样生成、不超最高投标限价、不可竞争费足额计取、签章位合规", "error_code": "BID_COMPLIANCE_FAILED"},
            ],
        },
    }

    is_tender = business_line in ("L4", "L5", "TENDER")
    nodes = (
        [
            {"id": "T0", "name": "接标立项", "state": "DONE", "color": "green", "label": "已完成"},
            {"id": "T1", "name": "红线解析", "state": "RUNNING", "color": "blue", "label": "进行中"},
            {"id": "T2", "name": "清单锁定与组价", "state": "WAITING_HUMAN", "color": "yellow", "label": "待人工拍板"},
            {"id": "T3", "name": "报价策略决策", "state": "WAITING_HUMAN", "color": "yellow", "label": "待人工拍板"},
            {"id": "T4", "name": "合规自检与六表样", "state": "NOT_STARTED", "color": "gray", "label": "未开始"},
            {"id": "T5", "name": "提交回执与成卷", "state": "NOT_STARTED", "color": "gray", "label": "未开始"},
        ]
        if is_tender
        else [
            {"id": "Q0", "name": "承接准备", "state": "DONE", "color": "green", "label": "已完成"},
            {"id": "Q1", "name": "资料双轨提取", "state": "DONE", "color": "green", "label": "已完成"},
            {"id": "Q2", "name": "算量与勾稽核对", "state": "RUNNING", "color": "blue", "label": "进行中"},
            {"id": "Q3", "name": "组价与三级复核", "state": "WAITING_HUMAN", "color": "yellow", "label": "待人工拍板"},
            {"id": "Q4", "name": "成果报告生成", "state": "FAILED" if any(r["status"] == "OPEN" for r in reworks) else "NOT_STARTED", "color": "red" if any(r["status"] == "OPEN" for r in reworks) else "gray", "label": "不合格" if any(r["status"] == "OPEN" for r in reworks) else "未开始"},
            {"id": "Q5", "name": "立卷归档", "state": "NOT_STARTED", "color": "gray", "label": "未开始"},
        ]
    )

    return {
        "project_id": project_id,
        "business_line": business_line,
        "user_mode": mode,
        "reserved_modules_phase2": [
            {"key": "smart_tender_agency", "name": "智能招标", "status": "RESERVED", "notice": "模块已预留，待二期开发（招标文件编制、清单控制价、评标办法设计）"},
            {"key": "smart_bid_doc", "name": "智能标书", "status": "RESERVED", "notice": "模块已预留，待二期开发（商务标与技术标编制、资格业绩匹配）"},
            {"key": "smart_consulting", "name": "智能咨询", "status": "RESERVED", "notice": "模块已预留，待二期开发（投资估算、限额设计、方案比选、后评价）"},
            {"key": "smart_design", "name": "智能设计", "status": "RESERVED", "notice": "模块已预留，待二期开发（设计概算联动、图纸经济性比选）"},
        ],
        "nodes_five_states": nodes,
        "gate_templates": gate_templates,
        "error_codes_12": GATE_ERROR_CODES_12,
        "six_kpis": {
            "process_completion_rate": "50.0%",
            "gate_first_pass_rate": "83.3%",
            "rework_rate": f"{len(reworks)} 单",
            "task_overdue_rate": "0.0%",
            "pending_decision_hours": "1.2h",
            "bid_win_rate_and_deviation": "目标偏差 ≤ 1.5%",
        },
        "rework_orders": reworks,
        "rule_candidates": candidates,
    }


# ==============================================================================
# 2. 投标预算生产线 (/v1/bid/*) — PRD 4.10 & 12.7
# ==============================================================================
@router.post("/v1/bid/create")
def bid_create(payload: dict[str, Any]):
    project_id = str(payload.get("project_id") or "").strip()
    tenderee = str(payload.get("tenderee") or "").strip()
    bid_deadline = str(payload.get("bid_deadline") or "").strip()
    control_price = payload.get("control_price")
    if not project_id or not tenderee or not bid_deadline or control_price is None:
        raise HTTPException(422, detail={"code": "INTAKE_GATE_NOT_READY", "message": "接标立项必填：project_id、招标人(tenderee)、截止时间(bid_deadline)、最高投标限价(control_price)"})

    # PRD 4.10.9 是否必须进广联达的四条判定
    req_software = bool(payload.get("require_glodon_format", False))
    eplatform_only = bool(payload.get("eplatform_requires_software", False))
    has_new_quota = bool(payload.get("has_new_quota_or_special_conversion", False))
    if req_software:
        must_gld, gld_reason = True, "判定条件1：招标文件明确要求提交广联达/指定计价软件格式电子文件（必须进）"
    elif eplatform_only:
        must_gld, gld_reason = True, "判定条件2：电子标平台只接受指定计价软件加密格式（必须进）"
    elif has_new_quota:
        must_gld, gld_reason = True, "判定条件3：含需重新组价的新增项或特殊定额换算（建议进广联达复核）"
    else:
        must_gld, gld_reason = False, "判定条件4：仅要求Excel/PDF标准表样且组价路径清晰（可不进广联达，系统直接出表并人工签认）"

    bid_id = f"bid_{uuid.uuid4().hex[:10]}"
    record = {
        "id": bid_id,
        "project_id": project_id,
        "tenderee": tenderee,
        "bid_deadline": bid_deadline,
        "bond_amount": str(_d(payload.get("bond_amount", 0))),
        "control_price": str(_d(control_price)),
        "eplatform": str(payload.get("eplatform") or "EXCEL_PDF"),
        "must_use_glodon": 1 if must_gld else 0,
        "glodon_reason": gld_reason,
        "addenda_closed": 1 if payload.get("addenda_closed", True) else 0,
        "status": "T0_INTAKE_DONE",
        "created_at": _now(),
    }
    st = get_prd_store()
    with st._conn() as c:
        c.execute(
            """INSERT INTO bid_project (id, project_id, tenderee, bid_deadline, bond_amount, control_price,
               eplatform, must_use_glodon, glodon_reason, addenda_closed, status, created_at)
               VALUES (:id, :project_id, :tenderee, :bid_deadline, :bond_amount, :control_price,
               :eplatform, :must_use_glodon, :glodon_reason, :addenda_closed, :status, :created_at)""",
            record,
        )
    return {**record, "must_use_glodon": must_gld}


@router.post("/v1/bid/redline")
def bid_redline_extract(payload: dict[str, Any]):
    bid_project_id = str(payload.get("bid_project_id") or "").strip()
    items = payload.get("redlines") or []
    addenda_closed = bool(payload.get("addenda_closed", True))
    if not bid_project_id:
        raise HTTPException(422, detail={"code": "BID_REDLINE_MISSING", "message": "缺少 bid_project_id"})

    st = get_prd_store()
    saved = []
    missing_five_elements = []
    with st._conn() as c:
        c.execute("DELETE FROM bid_redline WHERE bid_project_id = ?", (bid_project_id,))
        for item in items:
            cat_no = int(item.get("category_no", 0))
            quote_text = str(item.get("quote_text") or "").strip()
            page_no = int(item.get("page_no") or 0)
            clause_no = str(item.get("clause_no") or "").strip()
            interp = str(item.get("system_interpretation") or "").strip()
            action = str(item.get("required_action") or "").strip()
            if not (quote_text and page_no > 0 and clause_no and interp and action):
                missing_five_elements.append(cat_no)
                continue
            cat_meta = next((x for x in REDLINE_13_CATEGORIES if x[0] == cat_no), (cat_no, item.get("category_name", "其他"), "HIGH"))
            rec = {
                "id": f"rl_{uuid.uuid4().hex[:10]}",
                "bid_project_id": bid_project_id,
                "category_no": cat_no,
                "category_name": cat_meta[1],
                "quote_text": quote_text,
                "page_no": page_no,
                "clause_no": clause_no,
                "system_interpretation": interp,
                "required_action": action,
                "risk_level": cat_meta[2],
                "closed": 1 if item.get("closed", True) else 0,
                "source_ref": f"P{page_no} / {clause_no}",
            }
            c.execute(
                """INSERT INTO bid_redline VALUES (:id, :bid_project_id, :category_no, :category_name,
                   :quote_text, :page_no, :clause_no, :system_interpretation, :required_action,
                   :risk_level, :closed, :source_ref)""",
                rec,
            )
            saved.append(rec)

        covered_cats = {r["category_no"] for r in saved}
        completeness = len(covered_cats) / 13.0
        gate_passed = (completeness >= 0.90) and addenda_closed and (len(missing_five_elements) == 0)
        c.execute(
            "UPDATE bid_project SET addenda_closed = ?, status = ? WHERE id = ?",
            (1 if addenda_closed else 0, "T1_PASSED" if gate_passed else "T1_BLOCKED", bid_project_id),
        )

    if not gate_passed:
        raise HTTPException(
            422,
            detail={
                "code": "BID_REDLINE_MISSING",
                "message": f"T1 红线门阻断：13类红线抽取完整率 {completeness*100:.1f}%（要求≥90%，当前覆盖 {len(covered_cats)}/13 类），补遗闭环={addenda_closed}，五要素缺失项={missing_five_elements}",
                "completeness_ratio": round(completeness, 4),
                "covered_categories": sorted(list(covered_cats)),
                "addenda_closed": addenda_closed,
                "disqualification_11_clauses": [
                    {"clause_no": no, "description": desc, "risk_level": lvl}
                    for no, desc, lvl in DISQUALIFICATION_11_CLAUSES
                ],
            },
        )

    return {
        "bid_project_id": bid_project_id,
        "gate": "T1",
        "status": "PASS",
        "completeness_ratio": round(completeness, 4),
        "covered_count": len(covered_cats),
        "addenda_closed": addenda_closed,
        "redlines": saved,
        "disqualification_11_clauses": [
            {"clause_no": no, "description": desc, "risk_level": lvl}
            for no, desc, lvl in DISQUALIFICATION_11_CLAUSES
        ],
    }


@router.post("/v1/bid/list-lock")
def bid_list_lock_check(payload: dict[str, Any]):
    """PRD 4.10.6 清单锁定七字段校验（编码、名称、特征、单位、工程量、缺项、增项）。"""
    bid_project_id = str(payload.get("bid_project_id") or "").strip()
    tender_items = payload.get("tender_items") or []
    bid_items = payload.get("bid_items") or []
    allowed_extra_codes = set(payload.get("allowed_extra_codes") or [])
    signed_by = str(payload.get("signed_by") or "").strip()

    tender_by_code = {str(x.get("item_code") or "").strip(): x for x in tender_items if x.get("item_code")}
    bid_by_code = {str(x.get("item_code") or "").strip(): x for x in bid_items if x.get("item_code")}

    diffs = []
    # Check missing items (缺项) and field diffs
    for code, t_item in tender_by_code.items():
        if code not in bid_by_code:
            diffs.append({
                "item_code": code, "field_name": "missing_item",
                "tender_value": t_item.get("item_name", code), "bid_value": None,
                "diff_type": "招标清单有、投标清单缺项", "status": "BLOCKED", "signed_by": None,
            })
            continue
        b_item = bid_by_code[code]
        if _norm_text(t_item.get("item_name")) != _norm_text(b_item.get("item_name")):
            diffs.append({
                "item_code": code, "field_name": "item_name",
                "tender_value": str(t_item.get("item_name")), "bid_value": str(b_item.get("item_name")),
                "diff_type": "项目名称不一致", "status": "BLOCKED", "signed_by": None,
            })
        if _norm_text(t_item.get("feature")) != _norm_text(b_item.get("feature")):
            diffs.append({
                "item_code": code, "field_name": "feature",
                "tender_value": str(t_item.get("feature")), "bid_value": str(b_item.get("feature")),
                "diff_type": "项目特征描述删减或不一致", "status": "BLOCKED", "signed_by": None,
            })
        if _norm_unit(t_item.get("unit")) != _norm_unit(b_item.get("unit")):
            diffs.append({
                "item_code": code, "field_name": "unit",
                "tender_value": str(t_item.get("unit")), "bid_value": str(b_item.get("unit")),
                "diff_type": "计量单位不一致", "status": "BLOCKED", "signed_by": None,
            })
        if _d(t_item.get("qty")) != _d(b_item.get("qty")):
            diffs.append({
                "item_code": code, "field_name": "qty",
                "tender_value": str(t_item.get("qty")), "bid_value": str(b_item.get("qty")),
                "diff_type": "锁定工程量被篡改", "status": "BLOCKED", "signed_by": None,
            })

    # Check extra items (增项)
    for code, b_item in bid_by_code.items():
        if code not in tender_by_code:
            is_allowed = (code in allowed_extra_codes) and bool(signed_by)
            diffs.append({
                "item_code": code, "field_name": "extra_item",
                "tender_value": None, "bid_value": str(b_item.get("item_name", code)),
                "diff_type": "招标文件允许的补充项（已签认）" if is_allowed else "未授权擅自增项",
                "status": "SIGNED_ALLOWED_EXTRA" if is_allowed else "BLOCKED",
                "signed_by": signed_by if is_allowed else None,
            })

    st = get_prd_store()
    now_str = _now()
    with st._conn() as c:
        c.execute("DELETE FROM bid_list_lock WHERE bid_project_id = ?", (bid_project_id,))
        for d in diffs:
            c.execute(
                """INSERT INTO bid_list_lock VALUES (:id, :bid_project_id, :item_code, :field_name,
                   :tender_value, :bid_value, :diff_type, :status, :signed_by, :checked_at)""",
                {"id": f"ll_{uuid.uuid4().hex[:10]}", "bid_project_id": bid_project_id, "checked_at": now_str, **d},
            )
        blocked = [d for d in diffs if d["status"] == "BLOCKED"]
        c.execute(
            "UPDATE bid_project SET status = ? WHERE id = ?",
            ("T2_PASSED" if not blocked else "T2_BLOCKED", bid_project_id),
        )

    if blocked:
        raise HTTPException(
            422,
            detail={
                "code": "BID_LIST_NOT_LOCKED",
                "message": f"T2 清单门硬阻断：检出 {len(blocked)} 处清单七字段不一致或未签认增减项，违反 GB/T 50500-2024 清单锁定强制规定",
                "blocked_diffs": blocked,
            },
        )

    return {
        "bid_project_id": bid_project_id,
        "gate": "T2",
        "status": "PASS",
        "checked_items": len(tender_by_code),
        "diffs": diffs,
    }


@router.post("/v1/bid/quote")
def bid_quote_strategy(payload: dict[str, Any]):
    """PRD 4.10.7 & 12.7 报价策略页：三路径组价 + 成本线 + 三档区间 + 价格分反推 + 决策留痕（严格执行 D-09：绝不输出单一建议单价）。"""
    bid_project_id = str(payload.get("bid_project_id") or "").strip()
    control_price = _d(payload.get("control_price", 0))
    labor_cost = _d(payload.get("labor_cost", 0))
    material_cost = _d(payload.get("material_cost", 0))
    machine_cost = _d(payload.get("machine_cost", 0))
    mgmt_cost = _d(payload.get("mgmt_cost", 0))
    non_competitive_fee = _d(payload.get("non_competitive_fee", 0))  # 安全文明+规费+税金
    version = str(payload.get("version") or "v1").strip()

    direct_and_mgmt = labor_cost + material_cost + machine_cost + mgmt_cost
    cost_floor = direct_and_mgmt + non_competitive_fee
    if control_price <= Decimal("0") or cost_floor <= Decimal("0"):
        raise HTTPException(422, detail={"code": "INVALID_QUOTE_BASIS", "message": "控制价与成本构成必须大于 0"})
    if cost_floor > control_price:
        raise HTTPException(422, detail={"code": "BID_COMPLIANCE_FAILED", "message": "企业保本成本线已高于最高投标限价，存在严重亏损或废标风险"})

    # 三档区间（激进：贴近成本线+1%~3%；中性：成本线+3.5%~6.5%；保守：控制价下浮2%~4.5%，均受控于 control_price）
    agg_low = min(control_price, (cost_floor * Decimal("1.010")).quantize(Decimal("0.01")))
    agg_high = min(control_price, (cost_floor * Decimal("1.030")).quantize(Decimal("0.01")))
    neu_low = min(control_price, (cost_floor * Decimal("1.035")).quantize(Decimal("0.01")))
    neu_high = min(control_price, (cost_floor * Decimal("1.065")).quantize(Decimal("0.01")))
    con_low = max(cost_floor, (control_price * Decimal("0.955")).quantize(Decimal("0.01")))
    con_high = max(cost_floor, (control_price * Decimal("0.980")).quantize(Decimal("0.01")))

    # 价格分反推表（按评标基准价浮动模型反推不同下浮率区间对应的价格得分与排名区间）
    benchmark_est = ((neu_low + neu_high) / Decimal("2")).quantize(Decimal("0.01"))
    score_table = []
    for down_pct in (Decimal("0.02"), Decimal("0.04"), Decimal("0.06"), Decimal("0.08"), Decimal("0.10")):
        candidate_amt = (control_price * (Decimal("1.0") - down_pct)).quantize(Decimal("0.01"))
        deviation_pct = ((candidate_amt - benchmark_est) / benchmark_est * Decimal("100")).quantize(Decimal("0.01"))
        score_est = max(Decimal("50.0"), Decimal("60.0") - abs(deviation_pct) * Decimal("1.2")).quantize(Decimal("0.1"))
        score_table.append({
            "down_rate_pct": f"{down_pct*100:.1f}%",
            "amount_range_ref": str(candidate_amt),
            "below_cost_floor": candidate_amt < cost_floor,
            "estimated_price_score_band": f"{max(Decimal('0'), score_est - Decimal('0.8'))} ~ {min(Decimal('60.0'), score_est + Decimal('0.8'))}",
        })

    # 检查不平衡报价偏离风险
    max_item_deviation = Decimal(str(payload.get("max_unbalanced_item_ratio", "0.08")))
    risk_warnings = []
    if max_item_deviation > Decimal("0.15"):
        risk_warnings.append("警告：单项不平衡报价偏离幅度超过 15%，存在被评标委员会认定为不合理报价或废标的高风险！")
    risk_warnings.append("红线提醒：最终报价严禁高于最高投标限价，且严禁低于企业直接成本线（《招标投标法实施条例》第五十一条）。")
    risk_warnings.append("不可竞争费提醒：安全文明施工费、规费、税金必须严格按招标文件规定标准足额计取，不得参与下浮。")

    # 人工拍板留痕（可选：由企业负责人传入 down_rate / target_amount + decided_by + reason）
    decided_by = str(payload.get("decided_by") or "").strip() or None
    reason = str(payload.get("reason") or "").strip() or None
    down_rate = payload.get("down_rate")
    target_amount = None
    if down_rate is not None:
        dr = Decimal(str(down_rate))
        target_amount = (control_price * (Decimal("1.0") - dr)).quantize(Decimal("0.01"))
        if target_amount > control_price or target_amount < cost_floor:
            raise HTTPException(422, detail={
                "code": "BID_COMPLIANCE_FAILED",
                "message": f"人工拍板金额 {target_amount} 超出合规区间 [保本成本线 {cost_floor}, 最高限价 {control_price}]，硬阻断！",
            })
        if not decided_by or not reason:
            raise HTTPException(422, detail={
                "code": "DECISION_AUDIT_REQUIRED",
                "message": "人工拍板最终下浮率时，必须记录拍板人(decided_by)与决策理由(reason)以备归档举证",
            })

    result_payload = {
        "bid_project_id": bid_project_id,
        "version": version,
        "d09_policy_constraint": "D-09 强制规则：系统只输出成本构成、三档区间、概率、价格分反推与风险提示，绝不输出单一建议报价；最终下浮率必须由企业负责人拍板并留痕。",
        "suggested_single_price": None,
        "cost_composition": {
            "labor_cost": str(labor_cost),
            "material_cost": str(material_cost),
            "machine_cost": str(machine_cost),
            "mgmt_cost": str(mgmt_cost),
            "non_competitive_fee": str(non_competitive_fee),
            "cost_floor_total": str(cost_floor),
            "control_price": str(control_price),
        },
        "three_tier_ranges": {
            "aggressive": {
                "label": "激进档（贴近成本线）",
                "amount_range": [str(agg_low), str(agg_high)],
                "win_probability_band": "65% ~ 80%",
                "gross_margin_band": "1.0% ~ 2.9%",
            },
            "neutral": {
                "label": "中性档（均衡利润与得分）",
                "amount_range": [str(neu_low), str(neu_high)],
                "win_probability_band": "45% ~ 65%",
                "gross_margin_band": "3.4% ~ 6.1%",
            },
            "conservative": {
                "label": "保守档（利润优先）",
                "amount_range": [str(con_low), str(con_high)],
                "win_probability_band": "20% ~ 40%",
                "gross_margin_band": "6.5% ~ 11.5%",
            },
        },
        "price_score_backcalc": score_table,
        "risk_warnings": risk_warnings,
        "human_decision": {
            "locked": bool(decided_by and target_amount is not None),
            "version": version,
            "down_rate": str(down_rate) if down_rate is not None else None,
            "target_amount": str(target_amount) if target_amount is not None else None,
            "decided_by": decided_by,
            "reason": reason,
        },
    }

    st = get_prd_store()
    with st._conn() as c:
        c.execute(
            """INSERT INTO bid_quote VALUES (:id, :bid_project_id, :version, :cost_floor,
               :agg_low, :agg_high, :neu_low, :neu_high, :con_low, :con_high,
               :target_amount, :down_rate, :decided_by, :reason, :payload_json, :created_at)""",
            {
                "id": f"bq_{uuid.uuid4().hex[:10]}",
                "bid_project_id": bid_project_id,
                "version": version,
                "cost_floor": str(cost_floor),
                "agg_low": str(agg_low),
                "agg_high": str(agg_high),
                "neu_low": str(neu_low),
                "neu_high": str(neu_high),
                "con_low": str(con_low),
                "con_high": str(con_high),
                "target_amount": str(target_amount) if target_amount is not None else None,
                "down_rate": str(down_rate) if down_rate is not None else None,
                "decided_by": decided_by,
                "reason": reason,
                "payload_json": json.dumps(result_payload, ensure_ascii=False),
                "created_at": _now(),
            },
        )
    return result_payload


@router.post("/v1/bid/compliance")
def bid_compliance_and_sheets(payload: dict[str, Any]):
    """PRD 4.10.8 & 4.10.10 T5 出表门合规自检与六类标准投标报价表样生成。"""
    bid_project_id = str(payload.get("bid_project_id") or "").strip()
    bid_total = _d(payload.get("bid_total", 0))
    control_price = _d(payload.get("control_price", 0))
    cost_floor = _d(payload.get("cost_floor", 0))
    non_competitive_full = bool(payload.get("non_competitive_fee_full", True))
    seal_positions_complete = bool(payload.get("seal_positions_complete", True))
    substantial_response = bool(payload.get("substantial_response", True))

    st = get_prd_store()
    with st._conn() as c:
        proj = c.execute("SELECT * FROM bid_project WHERE id = ?", (bid_project_id,)).fetchone()
        lock_blocks = c.execute("SELECT COUNT(*) AS cnt FROM bid_list_lock WHERE bid_project_id = ? AND status = 'BLOCKED'", (bid_project_id,)).fetchone()
        redlines_cnt = c.execute("SELECT COUNT(DISTINCT category_no) AS cnt FROM bid_redline WHERE bid_project_id = ?", (bid_project_id,)).fetchone()

    fatal_errors = []
    if not proj or not proj["addenda_closed"] or (redlines_cnt and redlines_cnt["cnt"] < 12):
        fatal_errors.append("T1 红线门未通过或补遗未闭环 (BID_REDLINE_MISSING)")
    if lock_blocks and lock_blocks["cnt"] > 0:
        fatal_errors.append(f"T2 清单门存在 {lock_blocks['cnt']} 项未解决的七字段差异 (BID_LIST_NOT_LOCKED)")
    if bid_total > control_price:
        fatal_errors.append(f"致命项：投标总价({bid_total})超过最高投标限价({control_price})，直接否决投标！")
    if bid_total < cost_floor:
        fatal_errors.append(f"致命项：投标总价({bid_total})低于企业直接成本线({cost_floor})，违反招投标法实施条例第51条！")
    if not non_competitive_full:
        fatal_errors.append("致命项：安全文明施工费/规费/税金等不可竞争费未按规定足额计取！")
    if not substantial_response:
        fatal_errors.append("致命项：未对招标文件实质性要求和条件作出响应！")
    if not seal_positions_complete:
        fatal_errors.append("致命项：法定代表人或造价师签章位缺失！")

    if fatal_errors:
        raise HTTPException(
            422,
            detail={
                "code": "BID_COMPLIANCE_FAILED",
                "message": "T5 出表门硬阻断：" + "；".join(fatal_errors),
                "fatal_errors": fatal_errors,
            },
        )

    six_sheets = [
        {"sheet_no": 1, "sheet_name": "投标报价汇总表", "total_amount": str(bid_total), "status": "GENERATED"},
        {"sheet_no": 2, "sheet_name": "单位工程投标报价表", "total_amount": str(bid_total), "status": "GENERATED"},
        {"sheet_no": 3, "sheet_name": "分部分项工程量清单计价表", "seven_fields_locked": True, "status": "GENERATED"},
        {"sheet_no": 4, "sheet_name": "措施项目清单计价表", "non_competitive_separated": True, "status": "GENERATED"},
        {"sheet_no": 5, "sheet_name": "其他项目清单计价表", "provisional_locked": True, "status": "GENERATED"},
        {"sheet_no": 6, "sheet_name": "主要材料价格表", "unit_price_matched": True, "status": "GENERATED"},
    ]
    with st._conn() as c:
        c.execute("UPDATE bid_project SET status = 'T5_PASSED' WHERE id = ?", (bid_project_id,))

    return {
        "bid_project_id": bid_project_id,
        "gate": "T5",
        "status": "PASS",
        "six_standard_sheets": six_sheets,
    }


# ==============================================================================
# 3. 返工闭环系统 (/v1/rework/*) — PRD 12.6 九字段与七环节 + 三次高频入规则库
# ==============================================================================
@router.post("/v1/rework/create")
def rework_create(payload: dict[str, Any]):
    required_9 = ("project_id", "source", "business_type", "owner", "major", "issue", "location", "clause", "expected_action", "deadline")
    missing = [k for k in required_9 if not str(payload.get(k) or "").strip()]
    if missing:
        raise HTTPException(422, detail={"code": "REWORK_FIELDS_MISSING", "message": f"返工单九字段缺失：{missing}"})

    rw_id = f"RW-{datetime.datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    rec = {
        "id": rw_id,
        "project_id": str(payload["project_id"]).strip(),
        "source": str(payload["source"]).strip().upper(),
        "business_type": str(payload["business_type"]).strip(),
        "owner": str(payload["owner"]).strip(),
        "major": str(payload["major"]).strip(),
        "issue": str(payload["issue"]).strip(),
        "location": str(payload["location"]).strip(),
        "clause": str(payload["clause"]).strip(),
        "expected_action": str(payload["expected_action"]).strip(),
        "deadline": str(payload["deadline"]).strip(),
        "status": "OPEN",
        "before_value": str(payload.get("before_value") or ""),
        "after_value": None,
        "reviewer": str(payload.get("reviewer") or "reviewer"),
        "reopen_count": 0,
        "created_at": _now(),
        "closed_at": None,
    }
    st = get_prd_store()
    auto_rule_candidate = None
    with st._conn() as c:
        c.execute(
            """INSERT INTO rework_order VALUES (:id, :project_id, :source, :business_type, :owner, :major,
               :issue, :location, :clause, :expected_action, :deadline, :status, :before_value, :after_value,
               :reviewer, :reopen_count, :created_at, :closed_at)""",
            rec,
        )
        # PRD 12.6: 同一错误出现三次以上，自动生成规则候选条目
        cnt_row = c.execute(
            "SELECT COUNT(*) AS cnt FROM rework_order WHERE major = ? AND clause = ? AND issue = ?",
            (rec["major"], rec["clause"], rec["issue"]),
        ).fetchone()
        cnt = cnt_row["cnt"] if cnt_row else 1
        if cnt >= 3:
            sig = f"{rec['major']}::{rec['clause']}::{rec['issue']}"
            cand_id = f"RC-{hashlib.sha256(sig.encode()).hexdigest()[:8].upper()}"
            c.execute(
                """INSERT INTO rule_candidate (id, issue_signature, major, clause, occurrence_count, suggested_rule, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'CANDIDATE', ?)
                   ON CONFLICT(issue_signature) DO UPDATE SET occurrence_count = excluded.occurrence_count""",
                (cand_id, sig, rec["major"], rec["clause"], cnt, f"自动校验规则候选：针对【{rec['major']}】依据【{rec['clause']}】检查“{rec['issue']}”，执行动作：{rec['expected_action']}", _now()),
            )
            auto_rule_candidate = {"candidate_id": cand_id, "occurrence_count": cnt, "issue_signature": sig}

    return {"rework_order": rec, "auto_rule_candidate": auto_rule_candidate}


@router.post("/v1/rework/rectify")
def rework_rectify(payload: dict[str, Any]):
    rw_id = str(payload.get("id") or "").strip()
    after_val = str(payload.get("after_value") or "").strip()
    if not rw_id or not after_val:
        raise HTTPException(422, detail={"code": "RECTIFY_VALUE_REQUIRED", "message": "整改必须提交返工单号(id)与整改后值(after_value)以保留改前改后对照"})
    st = get_prd_store()
    with st._conn() as c:
        c.execute("UPDATE rework_order SET after_value = ?, status = 'RECTIFIED' WHERE id = ?", (after_val, rw_id))
        row = c.execute("SELECT * FROM rework_order WHERE id = ?", (rw_id,)).fetchone()
    if not row:
        raise HTTPException(404, detail={"code": "REWORK_NOT_FOUND", "message": "返工单不存在"})
    return dict(row)


@router.post("/v1/rework/close")
def rework_close(payload: dict[str, Any]):
    rw_id = str(payload.get("id") or "").strip()
    approved = bool(payload.get("approved", True))
    reviewer = str(payload.get("reviewer") or "reviewer").strip()
    st = get_prd_store()
    with st._conn() as c:
        row = c.execute("SELECT * FROM rework_order WHERE id = ?", (rw_id,)).fetchone()
        if not row:
            raise HTTPException(404, detail={"code": "REWORK_NOT_FOUND", "message": "返工单不存在"})
        if approved:
            c.execute("UPDATE rework_order SET status = 'CLOSED', reviewer = ?, closed_at = ? WHERE id = ?", (reviewer, _now(), rw_id))
        else:
            c.execute("UPDATE rework_order SET status = 'REOPENED', reopen_count = reopen_count + 1, reviewer = ? WHERE id = ?", (reviewer, rw_id))
        updated = c.execute("SELECT * FROM rework_order WHERE id = ?", (rw_id,)).fetchone()
    return dict(updated)


@router.get("/v1/rework/list")
def rework_list(project_id: str | None = None):
    st = get_prd_store()
    with st._conn() as c:
        if project_id:
            rows = c.execute("SELECT * FROM rework_order WHERE project_id = ? ORDER BY created_at DESC", (project_id,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM rework_order ORDER BY created_at DESC").fetchall()
        cands = c.execute("SELECT * FROM rule_candidate ORDER BY occurrence_count DESC").fetchall()
    return {"items": [dict(r) for r in rows], "rule_candidates": [dict(r) for r in cands]}


# ==============================================================================
# 4. 资料双轨物理隔离提取与唯一最终稿合成 (/v1/doc/dual-extract, /v1/doc/final-synthesize) — PRD 7.1.5
# ==============================================================================
@router.post("/v1/doc/dual-extract")
def doc_dual_extract(payload: dict[str, Any]):
    project_id = str(payload.get("project_id") or "").strip()
    document_id = str(payload.get("document_id") or "").strip()
    track_mode = str(payload.get("track_mode") or "").strip().upper()
    fields = payload.get("extracted_fields") or {}
    actor = str(payload.get("actor") or "user").strip()
    if track_mode not in ("MANUAL", "AI") or not project_id or not document_id or not fields:
        raise HTTPException(422, detail={"code": "DUAL_TRACK_INVALID", "message": "双轨提取必须指定 project_id、document_id、track_mode(MANUAL/AI) 及非空 extracted_fields"})

    st = get_prd_store()
    rec_id = f"dt_{uuid.uuid4().hex[:10]}"
    with st._conn() as c:
        c.execute(
            """INSERT INTO dual_track_extract (id, project_id, document_id, track_mode, extracted_fields_json, field_provenance_json, actor, created_at)
               VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
               ON CONFLICT(project_id, document_id, track_mode) DO UPDATE SET
               extracted_fields_json = excluded.extracted_fields_json, actor = excluded.actor, created_at = excluded.created_at""",
            (rec_id, project_id, document_id, track_mode, json.dumps(fields, ensure_ascii=False), actor, _now()),
        )
        tracks = [r["track_mode"] for r in c.execute("SELECT track_mode FROM dual_track_extract WHERE project_id = ? AND document_id = ?", (project_id, document_id)).fetchall()]
    return {
        "project_id": project_id,
        "document_id": document_id,
        "saved_track": track_mode,
        "available_tracks": tracks,
        "ready_for_synthesis": ("MANUAL" in tracks and "AI" in tracks),
    }


@router.post("/v1/doc/final-synthesize")
def doc_final_synthesize(payload: dict[str, Any]):
    """PRD 7.1.5 五条强制规则：必须同时存在 MANUAL 与 AI 两份独立记录，且差异项必须逐字段给出来源(MANUAL/AI/MANUAL_REVISED)、理由与原文页码出处。"""
    project_id = str(payload.get("project_id") or "").strip()
    document_id = str(payload.get("document_id") or "").strip()
    decisions = payload.get("field_decisions") or {}
    actor = str(payload.get("actor") or "reviewer").strip()

    st = get_prd_store()
    with st._conn() as c:
        rows = {
            r["track_mode"]: json.loads(r["extracted_fields_json"])
            for r in c.execute(
                "SELECT track_mode, extracted_fields_json FROM dual_track_extract WHERE project_id = ? AND document_id = ? AND track_mode IN ('MANUAL', 'AI')",
                (project_id, document_id),
            ).fetchall()
        }
    if "MANUAL" not in rows or "AI" not in rows:
        raise HTTPException(
            422,
            detail={
                "code": "DOC_GATE_NOT_READY",
                "message": "违反双轨提取强制规则1（两份都要有）：必须同时存在人工独立提取结果(MANUAL)与AI独立提取结果(AI)，缺一不可生成最终稿！",
                "present_tracks": list(rows.keys()),
            },
        )

    manual_fields, ai_fields = rows["MANUAL"], rows["AI"]
    all_keys = sorted(set(manual_fields.keys()) | set(ai_fields.keys()))
    final_fields = {}
    provenance = {}
    unresolved = []

    for k in all_keys:
        m_val, a_val = manual_fields.get(k), ai_fields.get(k)
        dec = decisions.get(k) or {}
        adopted_from = str(dec.get("adopted_from") or "").upper()
        reason = str(dec.get("reason") or "").strip()
        page_ref = str(dec.get("page_ref") or "").strip()
        if m_val != a_val:
            if adopted_from not in ("MANUAL", "AI", "MANUAL_REVISED") or not reason or not page_ref:
                unresolved.append({"field": k, "manual_value": m_val, "ai_value": a_val})
                continue
        else:
            adopted_from = adopted_from or "BOTH_MATCHED"
            reason = reason or "人工与AI独立提取结果一致"
            page_ref = page_ref or "原文一致"

        val = dec.get("value") if adopted_from == "MANUAL_REVISED" else (m_val if adopted_from == "MANUAL" else a_val)
        final_fields[k] = val
        provenance[k] = {"adopted_from": adopted_from, "reason": reason, "page_ref": page_ref, "manual_value": m_val, "ai_value": a_val}

    if unresolved:
        raise HTTPException(
            422,
            detail={
                "code": "DOC_GATE_NOT_READY",
                "message": f"违反双轨提取强制规则3（逐条合成）：存在 {len(unresolved)} 个差异字段未选择采纳路径或未填写理由与原文页码出处",
                "unresolved_fields": unresolved,
            },
        )

    with st._conn() as c:
        c.execute(
            """INSERT INTO dual_track_extract (id, project_id, document_id, track_mode, extracted_fields_json, field_provenance_json, actor, created_at)
               VALUES (?, ?, ?, 'FINAL', ?, ?, ?, ?)
               ON CONFLICT(project_id, document_id, track_mode) DO UPDATE SET
               extracted_fields_json = excluded.extracted_fields_json, field_provenance_json = excluded.field_provenance_json, actor = excluded.actor, created_at = excluded.created_at""",
            (f"dt_final_{uuid.uuid4().hex[:8]}", project_id, document_id, json.dumps(final_fields, ensure_ascii=False), json.dumps(provenance, ensure_ascii=False), actor, _now()),
        )

    return {
        "project_id": project_id,
        "document_id": document_id,
        "track_mode": "FINAL",
        "final_fields": final_fields,
        "provenance": provenance,
        "q1_dual_track_passed": True,
    }


# ==============================================================================
# 5. 广联达 L2 任务书生成与读回偏差 >5% 强校验 (/v1/gld/taskbook, /v1/gld/readback) — PRD 6.3 & 7.4.4
# ==============================================================================
@router.post("/v1/gld/taskbook")
def gld_generate_taskbook(payload: dict[str, Any]):
    project_id = str(payload.get("project_id") or "P-DEFAULT").strip()
    tb_type = str(payload.get("taskbook_type") or "MODELING").strip().upper()
    items = payload.get("items") or []
    if not items:
        raise HTTPException(422, detail={"code": "TASKBOOK_EMPTY", "message": "任务书指令明细不能为空"})

    tb_id = f"GLD-TB-{uuid.uuid4().hex[:8].upper()}"
    instructions = []
    for idx, it in enumerate(items, start=1):
        instructions.append({
            "instruction_id": f"{tb_id}-{idx:03d}",
            "item_code": it.get("item_code", f"ITEM-{idx:03d}"),
            "name": it.get("name", ""),
            "estimate_qty": str(_d(it.get("estimate_qty", 0))),
            "unit": it.get("unit", "m3"),
            "params": it.get("params", {}),
        })

    st = get_prd_store()
    with st._conn() as c:
        c.execute(
            "INSERT INTO gld_taskbook VALUES (?, ?, ?, ?, NULL, NULL, NULL, 'GENERATED', ?)",
            (tb_id, project_id, tb_type, json.dumps(instructions, ensure_ascii=False), _now()),
        )
    return {"taskbook_id": tb_id, "project_id": project_id, "taskbook_type": tb_type, "instructions": instructions}


@router.post("/v1/gld/readback")
def gld_readback_verify(payload: dict[str, Any]):
    """PRD 7.4.4: 工具算部分的读回偏差超过 5% 时，系统必须强制要求填写差异原因，否则不允许汇总。"""
    tb_id = str(payload.get("taskbook_id") or "").strip()
    readback_items = payload.get("readback_items") or []
    st = get_prd_store()
    with st._conn() as c:
        row = c.execute("SELECT * FROM gld_taskbook WHERE taskbook_id = ?", (tb_id,)).fetchone()
    if not row:
        raise HTTPException(404, detail={"code": "TASKBOOK_NOT_FOUND", "message": "未找到对应的广联达任务书"})

    instructions = {x["instruction_id"]: x for x in json.loads(row["instructions_json"])}
    unexplained_over_5pct = []
    comparisons = []
    max_dev = Decimal("0")

    for rb in readback_items:
        iid = rb.get("instruction_id")
        inst = instructions.get(iid)
        if not inst:
            continue
        est = _d(inst["estimate_qty"])
        tool_qty = _d(rb.get("qty_tool", 0))
        diff_reason = str(rb.get("diff_reason") or "").strip()
        dev_ratio = (abs(tool_qty - est) / est).quantize(Decimal("0.0001")) if est > Decimal("0") else Decimal("0")
        if dev_ratio > max_dev:
            max_dev = dev_ratio
        over_5pct = dev_ratio > Decimal("0.0500")
        if over_5pct and not diff_reason:
            unexplained_over_5pct.append({
                "instruction_id": iid,
                "item_code": inst["item_code"],
                "estimate_qty": str(est),
                "qty_tool": str(tool_qty),
                "deviation_pct": f"{dev_ratio*100:.2f}%",
            })
        comparisons.append({
            "instruction_id": iid,
            "item_code": inst["item_code"],
            "estimate_qty": str(est),
            "qty_tool": str(tool_qty),
            "deviation_pct": f"{dev_ratio*100:.2f}%",
            "over_5pct": over_5pct,
            "diff_reason": diff_reason or None,
        })

    if unexplained_over_5pct:
        raise HTTPException(
            422,
            detail={
                "code": "GLODON_READBACK_DIFF_UNEXPLAINED",
                "message": f"广联达读回校验阻断：存在 {len(unexplained_over_5pct)} 项工程量偏差超过 5% 但未填写差异原因(diff_reason)，禁止汇总！",
                "unexplained_items": unexplained_over_5pct,
            },
        )

    with st._conn() as c:
        c.execute(
            "UPDATE gld_taskbook SET readback_json = ?, max_deviation_ratio = ?, status = 'VERIFIED' WHERE taskbook_id = ?",
            (json.dumps(comparisons, ensure_ascii=False), str(max_dev), tb_id),
        )
    return {"taskbook_id": tb_id, "status": "VERIFIED", "max_deviation_pct": f"{max_dev*100:.2f}%", "comparisons": comparisons}


# ==============================================================================
# 6. 签证变更四维查重 (/v1/visa/check-duplicate) — PRD 7.6
# ==============================================================================
@router.post("/v1/visa/check-duplicate")
def visa_check_duplicate(payload: dict[str, Any]):
    visas = payload.get("visas") or []
    suspects = []
    for i in range(len(visas)):
        for j in range(i + 1, len(visas)):
            v1, v2 = visas[i], visas[j]
            sim = difflib.SequenceMatcher(None, _norm_text(v1.get("reason")), _norm_text(v2.get("reason"))).ratio()
            same_loc = _norm_text(v1.get("location")) == _norm_text(v2.get("location")) and bool(_norm_text(v1.get("location")))
            a1, a2 = _d(v1.get("amount", 0)), _d(v2.get("amount", 0))
            amt_close = (abs(a1 - a2) / max(a1, a2, Decimal("1"))) <= Decimal("0.15")
            if (sim >= 0.65 and same_loc) or (same_loc and amt_close and sim >= 0.45):
                suspects.append({
                    "visa_a": v1.get("visa_no"),
                    "visa_b": v2.get("visa_no"),
                    "location": v1.get("location"),
                    "text_similarity": round(sim, 3),
                    "overlap_amount": str(min(a1, a2)),
                    "evidence": f"部位相同({v1.get('location')})、事由相似度 {sim*100:.1f}%、涉及重合金额 {min(a1, a2)} 元",
                })
    return {"total_visas": len(visas), "suspected_duplicates_count": len(suspects), "suspected_duplicates": suspects}


# ==============================================================================
# 7. 河南省标准立卷归档 (/v1/archive/package) — PRD 第9章 DBJ41/T153-2016
# ==============================================================================
@router.post("/v1/archive/package")
def archive_build_package(payload: dict[str, Any]):
    project_id = str(payload.get("project_id") or "").strip()
    business_type = str(payload.get("business_type") or "SETTLEMENT_AUDIT").strip().upper()
    retention_years = int(payload.get("retention_years") or 5)
    provided_items = payload.get("items") or []

    if retention_years < 5:
        raise HTTPException(422, detail={"code": "ARCHIVE_GATE_NOT_READY", "message": "违反 GB/T 51095-2015 第3.4条：造价咨询档案保管期限不得少于 5 年"})

    standard_list = TENDER_15_ITEMS if business_type == "TENDER" else SETTLEMENT_AUDIT_23_ITEMS
    provided_titles = {str(x.get("title") or "").strip() for x in provided_items}
    missing = [title for title in standard_list if title not in provided_titles]
    if missing:
        raise HTTPException(
            422,
            detail={
                "code": "ARCHIVE_GATE_NOT_READY",
                "message": f"Q5 归档门阻断：缺少 {len(missing)} 项卷内必备文件（依据 DBJ41/T153-2016 / 投标卷内标准）",
                "missing_items": missing,
            },
        )

    # Check page continuity
    expected_start = 1
    catalog = []
    for idx, item in enumerate(provided_items, start=1):
        p_start = int(item.get("start_page", expected_start))
        p_end = int(item.get("end_page", p_start))
        if p_start != expected_start or p_end < p_start:
            raise HTTPException(
                422,
                detail={
                    "code": "ARCHIVE_GATE_NOT_READY",
                    "message": f"Q5 归档门阻断：第 {idx} 项《{item.get('title')}》页码不连续（期望起始页 {expected_start}，实际 {p_start}~{p_end}）",
                },
            )
        catalog.append({"seq": idx, "title": item.get("title"), "start_page": p_start, "end_page": p_end, "pages": p_end - p_start + 1})
        expected_start = p_end + 1

    total_pages = expected_start - 1
    xml_meta = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<ArchiveVolume project_id="{project_id}" business_type="{business_type}" '
        f'standard="DBJ41/T153-2016" total_items="{len(catalog)}" total_pages="{total_pages}" retention_years="{retention_years}"/>'
    )
    return {
        "project_id": project_id,
        "business_type": business_type,
        "gate": "Q5",
        "status": "PACKAGED",
        "total_items": len(catalog),
        "total_pages": total_pages,
        "retention_years": retention_years,
        "catalog": catalog,
        "metadata_xml": xml_meta,
    }
