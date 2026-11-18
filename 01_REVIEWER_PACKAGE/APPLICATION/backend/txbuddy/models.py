from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal
from decimal import Decimal as _Dec
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator


def finite_decimal(v):
    """P0-01：所有 Decimal 字段在解析阶段拒绝 NaN/Infinity/指数越界。"""
    if isinstance(v, _Dec):
        if not v.is_finite():
            raise ValueError("不允许 NaN/Infinity")
        if v != 0 and (v.as_tuple().exponent > 12 or v.as_tuple().exponent < -12):
            raise ValueError("Decimal 指数越界")
        if abs(v) > _Dec("1e12"):
            raise ValueError("Decimal 超出允许量级")
    return v

class BasisSource(BaseModel):
    source_id: str
    title: str
    doc_no: str | None = None
    source_type: Literal["LAW", "STANDARD", "LOCAL_RULE", "QUOTA", "PRICE", "CONTRACT", "TEST"]
    license_status: Literal["PUBLIC", "LICENSED", "PENDING", "UNKNOWN", "TEST_ONLY"] = "UNKNOWN"
    verified: bool = False

class BasisPack(BaseModel):
    model_config = ConfigDict(extra="forbid")
    basis_pack_id: str
    region_code: str
    region_level: Literal["PROVINCE", "CITY", "COUNTY"]
    major: str
    effective_date: date
    measurement_standard_version: str
    pricing_standard_version: str
    quota_pack_version: str | None = None
    rule_version_scope: Literal["HENAN_2025", "HENAN_LEGACY", "NATIONAL"]
    precision_rule_version: str
    status: Literal["DRAFT", "APPROVED", "LOCKED"] = "DRAFT"
    sources: list[BasisSource] = Field(default_factory=list)
    test_only: bool = False

class QuantityCalcRequest(BaseModel):
    basis_pack: BasisPack
    job_id: str | None = None
    operation: Literal["RECTANGULAR_AREA", "RECTANGULAR_VOLUME", "LINEAR_LENGTH", "NET_VALUE"]
    params: dict[str, Any]
    unit: str
    rule_code: str
    rule_version: str
    standard_ref: str

class QuotaMatchRequest(BaseModel):
    boq_item: dict[str, Any]
    quota_items: list[dict[str, Any]]
    exact_mappings: dict[str, str] = Field(default_factory=dict)

class IndexAdjustRequest(BaseModel):
    basis_pack: BasisPack
    base_amount: Decimal
    base_index: Decimal
    current_index: Decimal
    risk_rate: Decimal

    _v = field_validator("base_amount", "base_index", "current_index", "risk_rate")(finite_decimal)
    mode: Literal["WITHIN_BAND_ONLY", "FULL_EXCESS", "PASS_THROUGH"]
    contract_clause_ref: str

class FeeItem(BaseModel):
    fee_item: str
    amount: Decimal | None = None
    rate: Decimal | None = None

    _v = field_validator("amount", "rate", mode="before")(lambda v: v if v is None else finite_decimal(Decimal(str(v))))
    base_def: Literal["BOQ_TOTAL", "BOQ_PLUS_MEASURES", "PRE_TAX_BEFORE_FEES"] | None = None
    is_noncompetitive: bool = False
    source_ref: str | None = None

class ProjectTotalRequest(BaseModel):
    basis_pack: BasisPack
    boq_total: Decimal
    measure_total: Decimal
    other_total: Decimal

    _v = field_validator("boq_total", "measure_total", "other_total", "tax_rate")(finite_decimal)
    fee_items: list[FeeItem] = Field(default_factory=list)
    safety_production_fee: FeeItem | None = None
    tax_rate: Decimal
    formal_calculation: bool = True


class UnitPriceRequest(BaseModel):
    resources: list[dict[str, Any]]
    management_rate: Decimal
    profit_rate: Decimal
    management_base: str = "DIRECT_COST"
    profit_base: str = "DIRECT_COST_PLUS_MANAGEMENT"
    explicit_risk_fee: dict[str, Any] | None = None
    adjusted_resource_codes: list[str] = Field(default_factory=list)
    risk_resource_codes: list[str] = Field(default_factory=list)
    management_source_ref: str
    profit_source_ref: str

class QuotaNotesRequest(BaseModel):
    consumption: list[dict[str, Any]]
    notes: list[dict[str, Any]]
    conversion: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# P1-01-JOBMODEL：六业务线共用的 Job 领域模型
# ---------------------------------------------------------------------------
# Job 是六条业务线唯一的根对象。业务线、状态、basis_pack 引用、文档、任务、
# 证据关联与审计字段全部在这里一次定义，业务线之间不再靠字符串约定。
# 状态机只接受下方迁移表内的迁移，任何表外迁移一律拒绝（见 JobTransitionError）。


class BusinessLine(str, Enum):
    """六条业务线（与 core.gates.REQUIRED / GATE_SEQUENCE 的键一一对应）。"""

    L1 = "L1"  # 预算编制
    L2 = "L2"  # 预算审核
    L3 = "L3"  # 结算编制
    L4 = "L4"  # 结算审核
    L5 = "L5"  # 投标报价编制
    L6 = "L6"  # 造价鉴定


BUSINESS_LINE_LABELS: dict[str, str] = {
    BusinessLine.L1.value: "预算编制",
    BusinessLine.L2.value: "预算审核",
    BusinessLine.L3.value: "结算编制",
    BusinessLine.L4.value: "结算审核",
    BusinessLine.L5.value: "投标报价编制",
    BusinessLine.L6.value: "造价鉴定",
}


class JobStatus(str, Enum):
    """Job 状态机节点。CREATED 是唯一入口状态，ARCHIVED/CANCELLED 是终态。"""

    CREATED = "CREATED"
    BASIS_BOUND = "BASIS_BOUND"
    INTAKE = "INTAKE"
    IN_PROGRESS = "IN_PROGRESS"
    REVIEW = "REVIEW"
    SIGNED = "SIGNED"
    DELIVERED = "DELIVERED"
    ARCHIVED = "ARCHIVED"
    CANCELLED = "CANCELLED"


JOB_INITIAL_STATUS: JobStatus = JobStatus.CREATED
JOB_TERMINAL_STATUSES: frozenset[JobStatus] = frozenset({JobStatus.ARCHIVED, JobStatus.CANCELLED})

# 状态机迁移表：字典里没写的迁移一律非法。返回上一状态只允许 REVIEW -> IN_PROGRESS。
JOB_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.CREATED: frozenset({JobStatus.BASIS_BOUND, JobStatus.INTAKE, JobStatus.CANCELLED}),
    JobStatus.BASIS_BOUND: frozenset({JobStatus.INTAKE, JobStatus.CANCELLED}),
    JobStatus.INTAKE: frozenset({JobStatus.IN_PROGRESS, JobStatus.CANCELLED}),
    JobStatus.IN_PROGRESS: frozenset({JobStatus.REVIEW, JobStatus.CANCELLED}),
    JobStatus.REVIEW: frozenset({JobStatus.IN_PROGRESS, JobStatus.SIGNED, JobStatus.CANCELLED}),
    JobStatus.SIGNED: frozenset({JobStatus.DELIVERED, JobStatus.ARCHIVED}),
    JobStatus.DELIVERED: frozenset({JobStatus.ARCHIVED}),
    JobStatus.ARCHIVED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


class JobModelError(ValueError):
    """Job 模型层拒绝（业务线非法 / 状态非法 / 迁移非法）。"""

    code = "JOB_MODEL_INVALID"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code


class JobValidationError(JobModelError):
    code = "JOB_MODEL_INVALID"


class JobTransitionError(JobModelError):
    """非法状态迁移。code=JOB_STATUS_ILLEGAL，并携带 from/to/allowed 供审计。"""

    code = "JOB_STATUS_ILLEGAL"

    def __init__(self, current: "JobStatus", target: "JobStatus",
                 allowed: frozenset["JobStatus"] | None = None) -> None:
        allowed = JOB_TRANSITIONS.get(current, frozenset()) if allowed is None else allowed
        self.current = current
        self.target = target
        self.allowed = allowed
        super().__init__(
            f"非法状态迁移 {current.value} -> {target.value}；"
            f"允许的目标状态：{[s.value for s in sorted(allowed, key=lambda s: s.value)] or '无（终态）'}",
            code="JOB_STATUS_ILLEGAL",
        )


def parse_business_line(value: Any) -> BusinessLine:
    """业务线解析：只接受 L1..L6（大小写与空白容错），其它一律拒绝。"""
    if isinstance(value, BusinessLine):
        return value
    text = str(value).strip().upper() if value is not None else ""
    try:
        return BusinessLine(text)
    except ValueError:
        raise JobValidationError(
            f"业务线非法：{value!r}；只允许 {[b.value for b in BusinessLine]}",
            code="JOB_BUSINESS_LINE_INVALID",
        ) from None


def parse_job_status(value: Any) -> JobStatus:
    """状态解析：只接受 JobStatus 成员名，未知状态拒绝。"""
    if isinstance(value, JobStatus):
        return value
    text = str(value).strip().upper() if value is not None else ""
    try:
        return JobStatus(text)
    except ValueError:
        raise JobValidationError(
            f"状态非法：{value!r}；只允许 {[s.value for s in JobStatus]}",
            code="JOB_STATUS_UNKNOWN",
        ) from None


def allowed_job_transitions(status: Any) -> frozenset[JobStatus]:
    return JOB_TRANSITIONS[parse_job_status(status)]


def can_transition_job(current: Any, target: Any) -> bool:
    cur, tgt = parse_job_status(current), parse_job_status(target)
    return tgt in JOB_TRANSITIONS[cur]


def validate_job_transition(current: Any, target: Any) -> JobStatus:
    """校验一次状态迁移；非法迁移抛 JobTransitionError（不返回 False）。"""
    cur, tgt = parse_job_status(current), parse_job_status(target)
    if tgt not in JOB_TRANSITIONS[cur]:
        raise JobTransitionError(cur, tgt)
    return tgt


class JobDocumentRef(BaseModel):
    """Job 关联的文档版本（只登记哈希与版本号，不存文件本体）。"""

    doc_key: str
    version: str
    file_sha256: str
    verified: bool = False
    added_at: datetime | None = None


class JobTaskRef(BaseModel):
    """Job 关联的流程任务节点。"""

    task_id: str
    node: str
    status: str
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] | None = None
    assignee: str | None = None
    approver: str | None = None
    approved_at: str | None = None
    updated_at: datetime | None = None


class JobEvidenceRef(BaseModel):
    """Job 关联的取证记录（evidence 表按 payload.job_id 关联）。"""

    evidence_id: str
    sha256: str
    created_at: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class JobStatusEvent(BaseModel):
    """状态迁移审计事件（谁/何时/从哪到哪/原因）。"""

    from_status: JobStatus | None = None
    to_status: JobStatus
    by: str
    at: datetime
    reason: str | None = None


class Job(BaseModel):
    """Job 聚合根：六业务线共用，字段与状态机同源（models.JOB_TRANSITIONS）。"""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    project_id: str
    name: str
    business_line: BusinessLine
    status: JobStatus
    basis_pack: BasisPack
    basis_pack_id: str
    created_by: str
    created_at: datetime
    updated_by: str | None = None
    updated_at: datetime | None = None
    revision: int = 1
    documents: list[JobDocumentRef] = Field(default_factory=list)
    tasks: list[JobTaskRef] = Field(default_factory=list)
    evidence: list[JobEvidenceRef] = Field(default_factory=list)
    status_history: list[JobStatusEvent] = Field(default_factory=list)

    @property
    def business_line_label(self) -> str:
        return BUSINESS_LINE_LABELS[self.business_line.value]

    @property
    def is_terminal(self) -> bool:
        return self.status in JOB_TERMINAL_STATUSES

    def allowed_next_statuses(self) -> list[JobStatus]:
        return sorted(JOB_TRANSITIONS[self.status], key=lambda s: s.value)

    def can_transition(self, target: Any) -> bool:
        return can_transition_job(self.status, target)

    def assert_transition(self, target: Any) -> JobStatus:
        return validate_job_transition(self.status, target)

    @model_validator(mode="after")
    def _basis_pack_ref_must_match(self) -> "Job":
        if self.basis_pack_id != self.basis_pack.basis_pack_id:
            raise ValueError(
                f"basis_pack 引用不一致：job.basis_pack_id={self.basis_pack_id!r} "
                f"!= basis_pack.basis_pack_id={self.basis_pack.basis_pack_id!r}")
        return self
