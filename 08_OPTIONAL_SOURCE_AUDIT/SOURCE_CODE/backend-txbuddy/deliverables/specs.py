"""P1-10：十类成果的注册表（kind → 文件名 → 生成器）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .generators_budget import build_budget_pdf, build_budget_xlsx
from .generators_detail import (build_quantity_xlsx, build_quota_xlsx,
                                build_resource_summary_xlsx, build_unit_price_xlsx)
from .generators_pricing import (build_adjustment_xlsx, build_audit_diff_xlsx,
                                 build_evidence_index_xlsx, build_fee_xlsx)

Builder = Callable[[Any, Any, Any], dict[str, Any]]


@dataclass(frozen=True)
class DeliverableSpec:
    kind: str
    label: str
    file_name: str
    builder: Builder

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "label": self.label, "file_name": self.file_name}


SPECS: tuple[DeliverableSpec, ...] = (
    DeliverableSpec("budget_xlsx", "预算书（XLSX）", "01_预算书.xlsx", build_budget_xlsx),
    DeliverableSpec("budget_pdf", "预算书（PDF）", "02_预算书.pdf", build_budget_pdf),
    DeliverableSpec("quantity_calc", "工程量计算书", "03_工程量计算书.xlsx", build_quantity_xlsx),
    DeliverableSpec("quota_detail", "定额套用明细", "04_定额套用明细.xlsx", build_quota_xlsx),
    DeliverableSpec("unit_price", "综合单价分析", "05_综合单价分析.xlsx", build_unit_price_xlsx),
    DeliverableSpec("resource_summary", "人工材料机械汇总", "06_人工材料机械汇总.xlsx",
                    build_resource_summary_xlsx),
    DeliverableSpec("fee_calculation", "费用计算", "07_费用计算表.xlsx", build_fee_xlsx),
    DeliverableSpec("index_adjustment", "调差", "08_调差表.xlsx", build_adjustment_xlsx),
    DeliverableSpec("audit_difference", "审核差异", "09_审核差异表.xlsx", build_audit_diff_xlsx),
    DeliverableSpec("evidence_index", "证据索引", "10_证据索引.xlsx", build_evidence_index_xlsx),
)

SPEC_BY_KIND: dict[str, DeliverableSpec] = {spec.kind: spec for spec in SPECS}
