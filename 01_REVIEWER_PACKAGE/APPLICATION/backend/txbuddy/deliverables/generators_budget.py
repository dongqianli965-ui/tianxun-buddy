"""P1-10：预算书（XLSX 三表 + PDF 正文）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import dataset_meta, money_text, percent_text, qty_text
from .model import DeliverableDataset
from .pdf import PdfWriter
from .xlsx_util import add_total_row, add_sheet, create_workbook, write_table

BUDGET_SHEETS = ("封面", "总预算表", "单位工程预算表")


def _summary_rows(ds: DeliverableDataset) -> list[list[Any]]:
    totals = ds.totals()
    return [
        [1, "分部分项工程费", "清单项目合价合计", totals["boq_total"], ""],
        [2, "措施项目费", "按依据包计取", totals["measure_total"], ""],
        [3, "其他项目费", "按依据包计取", totals["other_total"], ""],
        [4, "费用合计", "管理费/利润/规费等", totals["fee_total"], ""],
        [5, "税前造价", "1+2+3+4", totals["tax_base"], ""],
        [6, "税金", f"税率 {percent_text(totals['tax_rate'])}", totals["tax_amount"], ""],
        [7, "含税工程造价", "5+6", totals["project_total"], ""],
    ]


def build_budget_xlsx(ds: DeliverableDataset, path: Path, ctx: Any = None) -> dict[str, Any]:
    wb = create_workbook()
    totals = ds.totals()

    cover = add_sheet(wb, "封面", index=0)
    cover_rows = dataset_meta(ds) + [
        ("分部分项工程费", money_text(totals["boq_total"])),
        ("含税工程造价", money_text(totals["project_total"])),
        ("编制说明", ds.remark or "本预算书由天巡造价引擎按依据包自动生成，金额单位为人民币元。"),
    ]
    write_table(cover, title="工程预算书", subtitle="BUDGET BOOK", headers=["项目", "内容"],
                rows=cover_rows, widths=[22, 66])

    summary = add_sheet(wb, "总预算表", index=1)
    write_table(summary, title="总预算表", headers=["序号", "费用项目", "计算基础", "金额（元）", "备注"],
                meta=dataset_meta(ds), rows=_summary_rows(ds), money_cols=(3,),
                widths=[6, 24, 34, 18, 12])

    detail = add_sheet(wb, "单位工程预算表", index=2)
    rows = []
    boq_total = 0
    for index, item in enumerate(ds.boq, start=1):
        rows.append([index, item.item_code, item.name, item.unit, item.quantity,
                     item.unit_price, item.amount, item.section])
        boq_total += item.amount
    last = write_table(detail, title="单位工程预算表",
                       headers=["序号", "项目编码", "项目名称", "单位", "工程量",
                                "综合单价（元）", "合价（元）", "分部"],
                       meta=dataset_meta(ds), rows=rows, money_cols=(5, 6), qty_cols=(4,),
                       widths=[6, 16, 36, 8, 12, 16, 16, 14])
    add_total_row(detail, last + 1, ["合计", "", "", "", "", "", totals["boq_total"], ""],
                  money_cols=(6,))
    wb.save(path)
    return {"sheets": list(wb.sheetnames), "boq_rows": len(rows),
            "boq_total": str(totals["boq_total"]),
            "project_total": str(totals["project_total"])}


def build_budget_pdf(ds: DeliverableDataset, path: Path, ctx: Any = None) -> dict[str, Any]:
    totals = ds.totals()
    writer = PdfWriter(f"Budget Book - {ds.job_id}", subject=ds.project_name)
    writer.line("工程预算书", size=18)
    writer.line("BUDGET BOOK", size=10)
    writer.line(f"Job: {ds.job_id}    Business line: {ds.business_line}")
    writer.line(f"Project: {ds.project_name}")
    writer.line(f"Basis pack: {ds.basis_pack_id}    Currency: {ds.currency}")
    writer.line(f"Prepared by: {ds.prepared_by}    Generated at: "
                f"{ds.prepared_at.strftime('%Y-%m-%d %H:%M:%S')}")
    writer.line()
    writer.line("一、总预算表", size=12)
    summary_rows = [[str(row[0]), row[1], row[2], money_text(row[3])] for row in _summary_rows(ds)]
    writer.table(["序号", "费用项目", "计算基础", "金额（元）"], [5, 22, 34, 18],
                 summary_rows, size=9, aligns=["l", "l", "l", "r"])
    writer.line()
    writer.line("二、单位工程预算表", size=12)
    boq_rows = []
    for index, item in enumerate(ds.boq, start=1):
        boq_rows.append([str(index), item.item_code, item.name, item.unit,
                         qty_text(item.quantity), money_text(item.unit_price),
                         money_text(item.amount)])
    writer.table(["序号", "项目编码", "项目名称", "单位", "工程量", "综合单价", "合价"],
                 [4, 14, 30, 6, 9, 12, 14], boq_rows, size=8.5,
                 aligns=["l", "l", "l", "l", "r", "r", "r"],
                 page_header=(f"工程预算书（续）  {ds.project_name}",))
    writer.line()
    writer.line(f"分部分项工程费合计：{money_text(totals['boq_total'])} 元", size=10)
    writer.line(f"含税工程造价：{money_text(totals['project_total'])} 元", size=11)
    writer.line()
    writer.line("编制人：________________    审核人：________________", size=10)
    writer.line("日期：________________", size=10)
    writer.write(path)
    return {"pages": len(writer.pages), "boq_rows": len(boq_rows),
            "project_total": str(totals["project_total"])}
