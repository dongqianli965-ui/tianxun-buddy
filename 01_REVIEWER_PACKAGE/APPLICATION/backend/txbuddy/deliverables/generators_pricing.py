"""P1-10：费用计算、调差、审核差异、证据索引。"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from .common import (dataset_meta, evidence_lookup, percent_text, record_brief,
                     require_unique_evidence)
from .model import DeliverableDataset
from .xlsx_util import add_sheet, add_total_row, create_workbook, write_table
from openpyxl.styles import Font


def _fee_summary(ds: DeliverableDataset) -> list[list[Any]]:
    totals = ds.totals()
    return [
        [1, "分部分项工程费", "清单合计", totals["boq_total"], "", totals["boq_total"], "工程量清单", "是"],
        [2, "措施项目费", "依据包", totals["measure_total"], "", totals["measure_total"], "依据包", "是"],
        [3, "其他项目费", "依据包", totals["other_total"], "", totals["other_total"], "依据包", "是"],
    ]


def build_fee_xlsx(ds: DeliverableDataset, path: Path, ctx: Any = None) -> dict[str, Any]:
    totals = ds.totals()
    wb = create_workbook()
    ws = add_sheet(wb, "费用计算表", index=0)
    rows = _fee_summary(ds)
    for index, row in enumerate(ds.fees, start=4):
        rows.append([index, row.fee_item, row.base_def, row.base_amount, row.rate,
                     row.amount, row.source_ref, "否" if row.noncompetitive else "是"])
    rows.append([len(rows) + 1, "费用合计", "费项金额合计", totals["tax_base"] - totals["boq_total"]
                 - totals["measure_total"] - totals["other_total"], None, totals["fee_total"],
                 "费用计算汇总", "—"])
    rows.append([len(rows) + 1, "税前造价", "1+2+3+费用合计", totals["tax_base"], None,
                 totals["tax_base"], "费用汇总", "—"])
    rows.append([len(rows) + 1, "税金", "税前造价×税率", totals["tax_base"], totals["tax_rate"],
                 totals["tax_amount"], "法定税率", "否"])
    rows.append([len(rows) + 1, "含税工程造价", "税前造价+税金", totals["tax_base"], None,
                 totals["project_total"], "造价汇总", "—"])
    last = write_table(ws, title="费用计算表",
                       headers=["序号", "费用项目", "计算基础", "基数（元）", "费率",
                                "金额（元）", "依据", "可竞争"],
                       meta=dataset_meta(ds), rows=rows, money_cols=(3, 5),
                       percent_cols=(4,),
                       widths=[6, 22, 24, 16, 10, 16, 24, 10])
    add_total_row(ws, last + 1, ["合计", "", "", "", "", totals["project_total"], "", ""],
                  money_cols=(5,))

    summary = add_sheet(wb, "造价汇总", index=1)
    summary_rows = [
        ["分部分项工程费", totals["boq_total"]],
        ["措施项目费", totals["measure_total"]],
        ["其他项目费", totals["other_total"]],
        ["费用合计", totals["fee_total"]],
        ["税前造价", totals["tax_base"]],
        [f"税金（{percent_text(totals['tax_rate'])}）", totals["tax_amount"]],
        ["含税工程造价", totals["project_total"]],
    ]
    write_table(summary, title="工程造价汇总", headers=["费用项目", "金额（元）"],
                rows=summary_rows, money_cols=(1,), widths=[30, 20])
    wb.save(path)
    return {"fee_rows": len(ds.fees), "project_total": str(totals["project_total"]),
            "sheets": list(wb.sheetnames)}


def build_adjustment_xlsx(ds: DeliverableDataset, path: Path, ctx: Any = None) -> dict[str, Any]:
    wb = create_workbook()
    ws = add_sheet(wb, "调差表", index=0)
    rows = []
    total = Decimal("0")
    for index, row in enumerate(ds.adjustments, start=1):
        rows.append([index, row.resource_code, row.name, row.base_amount, row.base_index,
                     row.current_index, row.change_ratio, row.risk_rate, row.mode,
                     row.delta_amount, row.contract_clause_ref])
        total += row.delta_amount
    last = write_table(ws, title="价格指数调差表",
                       headers=["序号", "资源编码", "资源名称", "基数金额（元）", "基期指数",
                                "当期指数", "变动率", "风险幅度", "调差方式", "调差金额（元）",
                                "合同条款"],
                       meta=dataset_meta(ds), rows=rows, money_cols=(3, 9),
                       percent_cols=(6, 7),
                       widths=[6, 16, 24, 16, 12, 12, 10, 10, 18, 16, 24])
    add_total_row(ws, last + 1, ["合计", "", "", "", "", "", "", "", "", total, ""],
                  money_cols=(9,))
    wb.save(path)
    return {"rows": len(rows), "delta_total": str(total), "sheets": list(wb.sheetnames)}


def build_audit_diff_xlsx(ds: DeliverableDataset, path: Path, ctx: Any = None) -> dict[str, Any]:
    wb = create_workbook()
    ws = add_sheet(wb, "审核差异表", index=0)
    rows = []
    submitted = audited = delta = Decimal("0")
    for index, row in enumerate(ds.audit_diffs, start=1):
        brief = record_brief(evidence_lookup(ctx, row.evidence_id)) if row.evidence_id else ""
        rows.append([index, row.item_code, row.name, row.unit, row.submitted_qty,
                     row.audited_qty, row.submitted_amount, row.audited_amount,
                     row.delta_amount, row.reason, row.evidence_id, brief])
        submitted += row.submitted_amount
        audited += row.audited_amount
        delta += row.delta_amount
    last = write_table(ws, title="审核差异表",
                       headers=["序号", "项目编码", "项目名称", "单位", "送审工程量",
                                "审定工程量", "送审金额（元）", "审定金额（元）",
                                "差额（元）", "差异原因", "证据ID", "证据反查"],
                       meta=dataset_meta(ds), rows=rows, money_cols=(6, 7, 8),
                       qty_cols=(4, 5),
                       widths=[6, 16, 24, 8, 12, 12, 16, 16, 14, 30, 18, 30])
    add_total_row(ws, last + 1, ["合计", "", "", "", "", "", submitted, audited, delta, "", "", ""],
                  money_cols=(6, 7, 8))
    wb.save(path)
    return {"rows": len(rows), "submitted": str(submitted), "audited": str(audited),
            "delta": str(delta), "sheets": list(wb.sheetnames)}


def build_evidence_index_xlsx(ds: DeliverableDataset, path: Path, ctx: Any = None) -> dict[str, Any]:
    require_unique_evidence(ds)
    wb = create_workbook()
    ws = add_sheet(wb, "证据索引", index=0)
    rows = []
    found = 0
    for index, row in enumerate(ds.evidence, start=1):
        record = evidence_lookup(ctx, row.evidence_id)
        status = "FOUND" if record is not None else "UNVERIFIED"
        found += 1 if record is not None else 0
        locator = json.dumps(row.locator, ensure_ascii=False, sort_keys=True) if row.locator else ""
        rows.append([index, row.evidence_id, row.subject_type, row.subject_id, row.file_name,
                     row.file_sha256, locator, row.created_at, status, record_brief(record)])
    last = write_table(ws, title="成果证据索引",
                       headers=["序号", "证据ID", "主题类型", "主题ID", "文件名", "文件SHA-256",
                                "定位信息", "创建时间", "反查状态", "记录摘要"],
                       meta=dataset_meta(ds), rows=rows,
                       widths=[6, 28, 14, 18, 26, 40, 28, 26, 12, 40])
    ws.cell(row=last + 1, column=1, value="合计").font = Font(bold=True)
    ws.cell(row=last + 1, column=2, value=f"{len(rows)} 条证据 / {found} 条已反查")

    guide = add_sheet(wb, "反查说明", index=1)
    index_sha = hashlib.sha256("\n".join(r.evidence_id for r in ds.evidence).encode("utf-8")).hexdigest()
    guide_rows = [
        ["索引口径", "每一行对应 evidence 表的一条记录，证据ID 即 evidence_id 主键"],
        ["反查方法", "store.get(evidence_id) 返回原始证据记录（含 payload、sha256、locator）"],
        ["证据条数", len(ds.evidence)],
        ["已反查条数", found],
        ["索引 SHA-256", index_sha],
        ["生成规则", "P1-10 成果生成器：证据索引只列真实存在的 evidence_id，缺一条即拒绝出件"],
    ]
    write_table(guide, title="证据索引反查说明", headers=["项目", "说明"], rows=guide_rows,
                widths=[20, 80])
    wb.save(path)
    return {"rows": len(rows), "found": found, "index_sha256": index_sha,
            "sheets": list(wb.sheetnames)}
