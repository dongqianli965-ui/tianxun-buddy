"""P1-10：十类成果的数据模型。

成果生成只认这一层规范化数据：所有金额都是有限 Decimal，金额/数量在进入生成器
之前已经补齐（数量×单价、单价合计、审核差额等），生成器本身不再做业务算术。
``DeliverableDataset.from_store`` 是真实数据入口：Job、通用业务记录与 evidence
表按 job_id 取出，证据索引因此能逐条反查到 evidence 记录 id。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

MONEY = Decimal("0.01")
QTY = Decimal("0.001")


class DeliverableError(ValueError):
    """成果生成拒绝（数据不合法 / 证据反查失败 / 输出目录非法）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _decimal(value: Any, field: str = "value") -> Decimal:
    if isinstance(value, bool):
        raise DeliverableError("DELIVERABLE_INVALID_NUMBER", f"{field} 不允许布尔值")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, TypeError, ValueError):
        raise DeliverableError("DELIVERABLE_INVALID_NUMBER", f"{field} 不是合法数值：{value!r}") from None
    if not result.is_finite():
        raise DeliverableError("DELIVERABLE_INVALID_NUMBER", f"{field} 不允许 NaN/Infinity")
    return result


def _optional_decimal(value: Any) -> Decimal | None:
    return None if value is None or value == "" else _decimal(value)


Money = Annotated[Decimal, BeforeValidator(_decimal)]
OptionalMoney = Annotated[Decimal | None, BeforeValidator(_optional_decimal)]


def money(value: Any) -> Decimal:
    """金额四舍五入到分（ROUND_HALF_UP），展示与合计共用同一口径。"""
    return _decimal(value).quantize(MONEY, rounding="ROUND_HALF_UP")


def quantity(value: Any) -> Decimal:
    return _decimal(value).quantize(QTY, rounding="ROUND_HALF_UP")


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BoqRow(_Row):
    item_code: str
    name: str
    unit: str
    quantity: Money
    unit_price: Money
    amount: OptionalMoney = None
    section: str = ""

    @model_validator(mode="after")
    def _fill_amount(self) -> "BoqRow":
        if self.amount is None:
            self.amount = money(self.quantity * self.unit_price)
        return self


class QuantityRow(_Row):
    item_code: str
    name: str
    formula: str
    unit: str
    quantity: Money
    rule_code: str
    rule_version: str = ""
    standard_ref: str = ""
    note: str = ""


class QuotaRow(_Row):
    item_code: str
    name: str
    quota_code: str
    quota_name: str
    unit: str
    quantity: Money
    labor: Money = Decimal("0")
    material: Money = Decimal("0")
    machine: Money = Decimal("0")
    total: OptionalMoney = None

    @model_validator(mode="after")
    def _fill_total(self) -> "QuotaRow":
        if self.total is None:
            self.total = money(self.labor + self.material + self.machine)
        return self


class UnitPriceRow(_Row):
    item_code: str
    name: str
    unit: str
    quantity: Money
    labor: Money = Decimal("0")
    material: Money = Decimal("0")
    machine: Money = Decimal("0")
    management: Money = Decimal("0")
    profit: Money = Decimal("0")
    risk_fee: Money = Decimal("0")
    unit_price: OptionalMoney = None
    amount: OptionalMoney = None
    management_source_ref: str = ""
    profit_source_ref: str = ""
    resource_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _fill_totals(self) -> "UnitPriceRow":
        if self.unit_price is None:
            self.unit_price = money(self.labor + self.material + self.machine
                                    + self.management + self.profit + self.risk_fee)
        if self.amount is None:
            self.amount = money(self.quantity * self.unit_price)
        return self


class ResourceRow(_Row):
    resource_type: Literal["LABOR", "MATERIAL", "MACHINE"]
    resource_code: str
    name: str
    unit: str
    quantity: Money
    unit_price: Money
    amount: OptionalMoney = None
    price_ref: str = ""
    item_code: str = ""

    @model_validator(mode="after")
    def _fill_amount(self) -> "ResourceRow":
        if self.amount is None:
            self.amount = money(self.quantity * self.unit_price)
        return self


class FeeRow(_Row):
    fee_item: str
    base_def: str
    base_amount: Money
    rate: OptionalMoney = None
    amount: Money
    source_ref: str = ""
    noncompetitive: bool = False


class AdjustmentRow(_Row):
    resource_code: str
    name: str
    base_amount: Money
    base_index: Money
    current_index: Money
    change_ratio: OptionalMoney = None
    risk_rate: Money = Decimal("0")
    mode: Literal["WITHIN_BAND_ONLY", "FULL_EXCESS", "PASS_THROUGH"]
    delta_amount: Money
    contract_clause_ref: str = ""

    @model_validator(mode="after")
    def _fill_ratio(self) -> "AdjustmentRow":
        if self.change_ratio is None:
            if self.base_index == 0:
                raise DeliverableError("DELIVERABLE_INVALID_NUMBER", "基期指数为 0，无法计算调差比值")
            self.change_ratio = (self.current_index / self.base_index - Decimal(1)).quantize(
                Decimal("0.000001"), rounding="ROUND_HALF_UP")
        return self


class AuditDiffRow(_Row):
    item_code: str
    name: str
    unit: str
    submitted_qty: Money
    audited_qty: Money
    submitted_amount: Money
    audited_amount: Money
    delta_amount: OptionalMoney = None
    reason: str = ""
    evidence_id: str = ""

    @model_validator(mode="after")
    def _fill_delta(self) -> "AuditDiffRow":
        if self.delta_amount is None:
            self.delta_amount = money(self.audited_amount - self.submitted_amount)
        return self


class EvidenceRow(_Row):
    evidence_id: str
    subject_type: str = ""
    subject_id: str = ""
    file_name: str = ""
    file_sha256: str = ""
    locator: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    result: str = ""

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "EvidenceRow":
        if not isinstance(record, dict):
            raise DeliverableError("EVIDENCE_INVALID", "evidence 记录必须是对象")
        body = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        merged = {**body, **{key: value for key, value in record.items()
                             if key != "payload" and value is not None}}
        evidence_id = merged.get("evidence_id")
        if not evidence_id:
            raise DeliverableError("EVIDENCE_INVALID", "evidence 记录缺少 evidence_id")
        locator = merged.get("locator")
        return cls(
            evidence_id=str(evidence_id),
            subject_type=str(merged.get("subject_type") or ""),
            subject_id=str(merged.get("subject_id") or ""),
            file_name=str(merged.get("file_name") or ""),
            file_sha256=str(merged.get("file_sha256") or ""),
            locator=dict(locator) if isinstance(locator, dict) else {},
            created_at=str(merged.get("created_at") or ""),
            result=str(merged.get("result") or ""),
        )


class DeliverableDataset(_Row):
    job_id: str
    project_name: str
    basis_pack_id: str
    business_line: str = "L1"
    prepared_by: str = "system"
    prepared_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    currency: str = "CNY"
    measure_total: Money = Decimal("0")
    other_total: Money = Decimal("0")
    tax_rate: Money = Decimal("0.09")
    remark: str = ""
    boq: list[BoqRow] = Field(default_factory=list)
    quantities: list[QuantityRow] = Field(default_factory=list)
    quotas: list[QuotaRow] = Field(default_factory=list)
    unit_prices: list[UnitPriceRow] = Field(default_factory=list)
    resources: list[ResourceRow] = Field(default_factory=list)
    fees: list[FeeRow] = Field(default_factory=list)
    adjustments: list[AdjustmentRow] = Field(default_factory=list)
    audit_diffs: list[AuditDiffRow] = Field(default_factory=list)
    evidence: list[EvidenceRow] = Field(default_factory=list)

    @model_validator(mode="after")
    def _required_text(self) -> "DeliverableDataset":
        for field in ("job_id", "project_name", "basis_pack_id"):
            if not str(getattr(self, field) or "").strip():
                raise DeliverableError("DELIVERABLE_INVALID_DATASET", f"{field} 不得为空")
        return self

    # ------------------------------------------------------------------ 合计
    def totals(self) -> dict[str, Decimal]:
        boq_total = money(sum((row.amount for row in self.boq), Decimal("0")))
        fee_total = money(sum((row.amount for row in self.fees), Decimal("0")))
        tax_base = money(boq_total + self.measure_total + self.other_total + fee_total)
        tax_amount = money(tax_base * self.tax_rate)
        return {
            "boq_total": boq_total,
            "measure_total": money(self.measure_total),
            "other_total": money(self.other_total),
            "fee_total": fee_total,
            "tax_rate": self.tax_rate,
            "tax_base": tax_base,
            "tax_amount": tax_amount,
            "project_total": money(tax_base + tax_amount),
        }

    # ------------------------------------------------------------ 真实数据入口
    @classmethod
    def from_store(cls, store: Any, job_id: str, *, prepared_by: str = "system",
                   prepared_at: datetime | None = None) -> "DeliverableDataset":
        """从 EvidenceStore 组装数据集：Job + job_record + evidence 三处按 job_id 取数。"""
        job = store.get_job(job_id)
        if not job:
            raise DeliverableError("JOB_NOT_FOUND", f"job 不存在：{job_id}")

        def payload(kind: str) -> dict[str, Any]:
            rows = store.get_records(job_id, kind)
            latest = rows[-1] if rows else {}
            return latest if isinstance(latest, dict) else {}

        def items(kind: str) -> list[dict[str, Any]]:
            value = payload(kind).get("items") or []
            if not isinstance(value, list):
                raise DeliverableError("DELIVERABLE_INVALID_DATASET", f"{kind}.items 必须是数组")
            return value

        fees_payload = payload("fees")
        return cls(
            job_id=job_id,
            project_name=job["name"],
            basis_pack_id=job.get("basis_pack_id") or job["basis_pack"]["basis_pack_id"],
            business_line=job.get("business_line") or "L1",
            prepared_by=prepared_by,
            prepared_at=prepared_at or datetime.now(timezone.utc),
            measure_total=fees_payload.get("measure_total", "0"),
            other_total=fees_payload.get("other_total", "0"),
            tax_rate=fees_payload.get("tax_rate", "0.09"),
            remark=str(fees_payload.get("remark") or ""),
            boq=items("boq"),
            quantities=items("quantity_calc"),
            quotas=items("quota_match"),
            unit_prices=items("unit_price"),
            resources=items("resources"),
            fees=fees_payload.get("items") or [],
            adjustments=items("index_adjustment"),
            audit_diffs=items("audit_diff"),
            evidence=[EvidenceRow.from_record(e) for e in store.list_evidence(job_id)],
        )
