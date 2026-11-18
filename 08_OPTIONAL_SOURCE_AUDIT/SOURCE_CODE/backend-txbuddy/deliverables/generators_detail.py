"""P1-10：工程量计算书、定额套用明细、综合单价分析、人材机汇总。"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from .common import dataset_meta
from .model import DeliverableDataset
from .xlsx_util import add_sheet, add_total_row, create_workbook, write_table

RESOURCE_LABELS = {"LABOR": "人工", "MATERIAL": "材料", "MACHINE": "机械"}


def build_quantity_xlsx(ds: DeliverableDataset, path: Path, ctx: Any = None) -> dict[str, Any]:
    wb = create_workbook()
    ws = add_sheet(wb, "工程量计算书", index=0)
    rows = []
    for index, row in enumerate(ds.quantities, start=1):
        rows.append([index, row.item_code, row.name, row.formula, row.unit, row.quantity,
                     row.rule_code, row.rule_version, row.standard_ref, row.note])
    last = write_table(ws, title="工程量计算书",
                       headers=["序号", "项目编码", "项目名称", "计算式", "单位", "工程量",
                                "规则编号", "规则版本", "标准依据", "备注"],
                       meta=dataset_meta(ds), rows=rows, qty_cols=(5,),
                       widths=[6, 16, 24, 40, 8, 12, 14, 10, 24, 20])
    add_total_row(ws, last + 1, ["合计", f"{len(rows)} 条", "", "", "", "", "", "", "", ""])

    summary = add_sheet(wb, "计算规则汇总", index=1)
    counter = Counter((row.rule_code, row.rule_version, row.standard_ref) for row in ds.quantities)
    summary_rows = [[code, version, ref, count] for (code, version, ref), count in sorted(counter.items())]
    write_table(summary, title="计算规则汇总", headers=["规则编号", "规则版本", "标准依据", "条目数"],
                rows=summary_rows, widths=[20, 12, 36, 10])
    wb.save(path)
    return {"rows": len(rows), "rules": len(counter), "sheets": list(wb.sheetnames)}


def build_quota_xlsx(ds: DeliverableDataset, path: Path, ctx: Any = None) -> dict[str, Any]:
    wb = create_workbook()
    ws = add_sheet(wb, "定额套用明细", index=0)
    rows = []
    total = Decimal("0")
    for index, row in enumerate(ds.quotas, start=1):
        rows.append([index, row.item_code, row.name, row.quota_code, row.quota_name, row.unit,
                     row.quantity, row.labor, row.material, row.machine, row.total])
        total += row.total
    last = write_table(ws, title="定额套用明细",
                       headers=["序号", "项目编码", "项目名称", "定额编号", "定额名称", "单位",
                                "工程量", "人工费（元）", "材料费（元）", "机械费（元）", "合计（元）"],
                       meta=dataset_meta(ds), rows=rows, money_cols=(7, 8, 9, 10), qty_cols=(6,),
                       widths=[6, 16, 24, 16, 30, 8, 12, 14, 14, 14, 14])
    add_total_row(ws, last + 1, ["合计", "", "", "", "", "", "", "", "", "", total], money_cols=(10,))
    wb.save(path)
    return {"rows": len(rows), "total": str(total), "sheets": list(wb.sheetnames)}


def build_unit_price_xlsx(ds: DeliverableDataset, path: Path, ctx: Any = None) -> dict[str, Any]:
    wb = create_workbook()
    ws = add_sheet(wb, "综合单价分析", index=0)
    rows = []
    total = Decimal("0")
    for index, row in enumerate(ds.unit_prices, start=1):
        rows.append([index, row.item_code, row.name, row.unit, row.quantity, row.labor,
                     row.material, row.machine, row.management, row.profit, row.risk_fee,
                     row.unit_price, row.amount])
        total += row.amount
    last = write_table(ws, title="综合单价分析表",
                       headers=["序号", "项目编码", "项目名称", "单位", "工程量", "人工费",
                                "材料费", "机械费", "管理费", "利润", "风险费",
                                "综合单价（元）", "合价（元）"],
                       meta=dataset_meta(ds), rows=rows, money_cols=tuple(range(5, 13)),
                       qty_cols=(4,), widths=[6, 16, 24, 8, 12, 12, 12, 12, 12, 12, 12, 16, 16])
    add_total_row(ws, last + 1, ["合计", "", "", "", "", "", "", "", "", "", "", "", total],
                  money_cols=(12,))

    resources = add_sheet(wb, "资源明细", index=1)
    resource_rows = []
    for index, row in enumerate(ds.resources, start=1):
        resource_rows.append([index, row.item_code, RESOURCE_LABELS[row.resource_type],
                              row.resource_code, row.name, row.unit, row.quantity,
                              row.unit_price, row.amount, row.price_ref])
    write_table(resources, title="综合单价资源明细",
                headers=["序号", "项目编码", "资源类型", "资源编码", "资源名称", "单位",
                         "数量", "单价（元）", "金额（元）", "价格依据"],
                rows=resource_rows, money_cols=(7, 8), qty_cols=(6,),
                widths=[6, 16, 10, 16, 30, 8, 12, 14, 14, 24])

    basis = add_sheet(wb, "依据来源", index=2)
    seen: set[tuple[str, str, str]] = set()
    basis_rows = []
    for row in ds.unit_prices:
        key = (row.item_code, row.management_source_ref, row.profit_source_ref)
        if key not in seen:
            seen.add(key)
            basis_rows.append(list(key))
    write_table(basis, title="管理费与利润依据来源",
                headers=["项目编码", "管理费依据", "利润依据"], rows=basis_rows,
                widths=[18, 36, 36])
    wb.save(path)
    return {"rows": len(rows), "resources": len(resource_rows), "total": str(total),
            "sheets": list(wb.sheetnames)}


def build_resource_summary_xlsx(ds: DeliverableDataset, path: Path, ctx: Any = None) -> dict[str, Any]:
    wb = create_workbook()
    summary = add_sheet(wb, "人材机汇总", index=0)
    by_type = {key: Decimal("0") for key in RESOURCE_LABELS}
    for row in ds.resources:
        by_type[row.resource_type] += row.amount
    total = sum(by_type.values(), Decimal("0"))
    summary_rows = []
    for key, label in RESOURCE_LABELS.items():
        amount = by_type[key]
        share = (amount / total) if total else Decimal("0")
        summary_rows.append([label, amount, share])
    last = write_table(summary, title="人工材料机械汇总表",
                       headers=["资源类型", "金额（元）", "占比"], meta=dataset_meta(ds),
                       rows=summary_rows, money_cols=(1,), percent_cols=(2,),
                       widths=[16, 18, 12])
    add_total_row(summary, last + 1, ["合计", total, Decimal("1") if total else Decimal("0")],
                  money_cols=(1,))

    detail = add_sheet(wb, "资源明细", index=1)
    rows = []
    for index, row in enumerate(ds.resources, start=1):
        rows.append([index, RESOURCE_LABELS[row.resource_type], row.resource_code, row.name,
                     row.unit, row.quantity, row.unit_price, row.amount, row.price_ref,
                     row.item_code])
    write_table(detail, title="人工材料机械明细",
                headers=["序号", "资源类型", "资源编码", "资源名称", "单位", "数量",
                         "单价（元）", "金额（元）", "价格依据", "关联项目"],
                rows=rows, money_cols=(6, 7), qty_cols=(5,),
                widths=[6, 10, 16, 30, 8, 12, 14, 14, 24, 16])
    wb.save(path)
    return {"types": {key: str(value) for key, value in by_type.items()},
            "total": str(total), "rows": len(rows), "sheets": list(wb.sheetnames)}
