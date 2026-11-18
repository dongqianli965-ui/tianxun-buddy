"""V4.1 新增请求模型（规则引擎 / BOQ 导入 / 复算 / 门禁）。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .models import (
    BasisPack,
    BusinessLine,
    Job,
    JobStatus,
    parse_business_line,
    parse_job_status,
)


class RuleExecuteRequest(BaseModel):
    basis_pack: BasisPack
    job_id: str | None = None
    rule_code: str
    rule_version: str | None = None
    params: dict[str, Any]
    component_ids: list[int] = Field(default_factory=list)


class RecalcRequest(BaseModel):
    basis_pack: BasisPack
    snapshot: dict[str, Any]
    stored_value: str


class GateRequest(BaseModel):
    job_id: str | None = None
    business_line: Literal["L1", "L2", "L3", "L4", "L5", "L6"]
    gate_code: str
    basis_pack: BasisPack
    available_documents: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class BoqImportSummary(BaseModel):
    items: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    stats: dict[str, Any]
    content_sha256: str | None = None
    sheet: str | None = None


# ---------------------------------------------------------------------------
# P1-01-JOBMODEL：Job 的 API 契约（六业务线共用，字段与 models.Job 同源）
# ---------------------------------------------------------------------------


class JobCreateRequest(BaseModel):
    """创建 Job。business_line 只接受 L1..L6；basis_pack 必须是合法 BasisPack。"""

    model_config = {"extra": "forbid"}

    project_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    business_line: BusinessLine
    basis_pack: BasisPack
    created_by: str = "tester"

    @field_validator("business_line", mode="before")
    @classmethod
    def _check_business_line(cls, v: Any) -> BusinessLine:
        return parse_business_line(v)

    def basis_pack_payload(self) -> dict[str, Any]:
        return self.basis_pack.model_dump(mode="json")


class JobStatusChangeRequest(BaseModel):
    """Job 状态迁移请求；非法迁移由 models.validate_job_transition 拒绝。"""

    model_config = {"extra": "forbid"}

    target_status: JobStatus
    by: str = "tester"
    reason: str | None = None

    @field_validator("target_status", mode="before")
    @classmethod
    def _check_target(cls, v: Any) -> JobStatus:
        return parse_job_status(v)


class JobDocumentRequest(BaseModel):
    doc_key: str = Field(min_length=1)
    version: str = Field(min_length=1)
    file_sha256: str = Field(min_length=1)
    verified: bool = False


class JobSummary(BaseModel):
    """Job 聚合的读取视图（与 models.Job 一一对应）。"""

    model_config = {"extra": "forbid"}

    job_id: str
    project_id: str
    name: str
    business_line: BusinessLine
    business_line_label: str
    status: JobStatus
    basis_pack_id: str
    revision: int
    is_terminal: bool
    allowed_next_statuses: list[JobStatus]
    document_count: int = 0
    task_count: int = 0
    evidence_count: int = 0
    created_by: str
    created_at: str | None = None
    updated_by: str | None = None
    updated_at: str | None = None

    @classmethod
    def from_job(cls, job: Job) -> "JobSummary":
        return cls(
            job_id=job.job_id,
            project_id=job.project_id,
            name=job.name,
            business_line=job.business_line,
            business_line_label=job.business_line_label,
            status=job.status,
            basis_pack_id=job.basis_pack_id,
            revision=job.revision,
            is_terminal=job.is_terminal,
            allowed_next_statuses=job.allowed_next_statuses(),
            document_count=len(job.documents),
            task_count=len(job.tasks),
            evidence_count=len(job.evidence),
            created_by=job.created_by,
            created_at=job.created_at.isoformat() if job.created_at else None,
            updated_by=job.updated_by,
            updated_at=job.updated_at.isoformat() if job.updated_at else None,
        )
