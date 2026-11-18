"""P1-10：成果生成器共用的小工具（元信息、数值展示、证据反查）。"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

from .model import DeliverableDataset, DeliverableError

EvidenceLookup = Callable[[str], dict[str, Any] | None]


def dataset_meta(ds: DeliverableDataset) -> list[tuple[str, Any]]:
    return [
        ("项目名称", ds.project_name),
        ("Job 编号", ds.job_id),
        ("业务线", ds.business_line),
        ("依据包", ds.basis_pack_id),
        ("币种", ds.currency),
        ("编制人", ds.prepared_by),
        ("生成时间", ds.prepared_at.strftime("%Y-%m-%d %H:%M:%S")),
    ]


def money_text(value: Decimal) -> str:
    return f"{value:,.2f}"


def qty_text(value: Decimal) -> str:
    text = f"{value:,.3f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def percent_text(value: Decimal) -> str:
    return f"{value * 100:.2f}%"


def evidence_lookup(ctx: Any, evidence_id: str) -> dict[str, Any] | None:
    lookup = getattr(ctx, "evidence_lookup", None)
    if lookup is None:
        return None
    record = lookup(evidence_id)
    if record is None:
        raise DeliverableError("EVIDENCE_NOT_FOUND", f"证据索引无法反查 evidence_id={evidence_id}")
    return record


def record_brief(record: dict[str, Any] | None) -> str:
    if not record:
        return ""
    parts = []
    for key in ("subject_type", "subject_id", "result"):
        value = record.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    if record.get("sha256"):
        parts.append(f"sha256={str(record['sha256'])[:16]}...")
    return "; ".join(parts)


def require_unique_evidence(ds: DeliverableDataset) -> None:
    seen: set[str] = set()
    for row in ds.evidence:
        if row.evidence_id in seen:
            raise DeliverableError("EVIDENCE_DUPLICATED", f"证据索引重复 evidence_id={row.evidence_id}")
        seen.add(row.evidence_id)
    for row in ds.audit_diffs:
        if row.evidence_id and row.evidence_id not in seen:
            raise DeliverableError(
                "EVIDENCE_NOT_INDEXED",
                f"审核差异引用的证据 {row.evidence_id} 未出现在证据索引数据集中")
