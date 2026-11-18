"""P1-10：成果生成服务（输出目录、原子落盘、SHA-256 清单、证据反查预检）。"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import EvidenceLookup, require_unique_evidence
from .model import DeliverableDataset, DeliverableError
from .specs import SPEC_BY_KIND, SPECS, DeliverableSpec

MANIFEST_SCHEMA = "deliverable_manifest/1"
FORBIDDEN_NAME_CHARS = set('/\\:*?"<>|')


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_job_dir_name(job_id: str) -> str:
    raw = str(job_id or "")
    name = raw.strip()
    if not name or name in {".", ".."}:
        raise DeliverableError("OUTPUT_DIR_INVALID", f"job_id 不能作为输出目录：{job_id!r}")
    if raw != name:
        raise DeliverableError("OUTPUT_DIR_INVALID", f"job_id 前后不得有空白：{job_id!r}")
    if len(name) > 96:
        raise DeliverableError("OUTPUT_DIR_INVALID", "job_id 过长（>96 字符）")
    if any(char in FORBIDDEN_NAME_CHARS for char in name) or any(ord(char) < 32 for char in name):
        raise DeliverableError("OUTPUT_DIR_INVALID", f"job_id 含非法路径字符：{job_id!r}")
    if name != name.rstrip(" ."):
        raise DeliverableError("OUTPUT_DIR_INVALID", f"job_id 不得以点或空格结尾：{job_id!r}")
    return name


@dataclass(frozen=True)
class DeliverableContext:
    evidence_lookup: EvidenceLookup | None = None


@dataclass(frozen=True)
class DeliverableFile:
    kind: str
    label: str
    file_name: str
    path: Path
    sha256: str
    size: int
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "file_name": self.file_name,
            "sha256": self.sha256,
            "size": self.size,
            "details": self.details,
        }


@dataclass(frozen=True)
class DeliverableManifest:
    job_id: str
    output_dir: Path
    generated_at: str
    files: tuple[DeliverableFile, ...]
    manifest_path: Path
    manifest_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": MANIFEST_SCHEMA,
            "task": "P1-10-DELIVER",
            "job_id": self.job_id,
            "generated_at": self.generated_at,
            "output_dir": str(self.output_dir),
            "count": len(self.files),
            "files": [item.as_dict() for item in self.files],
            "manifest_sha256": self.manifest_sha256,
        }

    def by_kind(self, kind: str) -> DeliverableFile:
        for item in self.files:
            if item.kind == kind:
                return item
        raise DeliverableError("DELIVERABLE_KIND_UNKNOWN", f"清单中没有成果类型：{kind}")


class DeliverableService:
    """十类成果的唯一出口。默认写到运行目录下的 data/deliverables/<job_id>/。"""

    def __init__(self, output_root: str | Path = "data/deliverables",
                 evidence_lookup: EvidenceLookup | None = None) -> None:
        self.output_root = Path(output_root)
        self.context = DeliverableContext(evidence_lookup=evidence_lookup)

    def output_dir(self, job_id: str) -> Path:
        return self.output_root / _safe_job_dir_name(job_id)

    def generate(self, dataset: DeliverableDataset, kind: str) -> DeliverableFile:
        spec = SPEC_BY_KIND.get(str(kind))
        if spec is None:
            raise DeliverableError("DELIVERABLE_KIND_UNKNOWN",
                                   f"未知成果类型：{kind!r}；允许 {[s.kind for s in SPECS]}")
        return self._generate_one(dataset, spec)

    def _generate_one(self, dataset: DeliverableDataset, spec: DeliverableSpec) -> DeliverableFile:
        out_dir = self.output_dir(dataset.job_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / spec.file_name
        temp = out_dir / f".{spec.file_name}.tmp"
        try:
            details = spec.builder(dataset, temp, self.context) or {}
            if not temp.is_file() or temp.stat().st_size == 0:
                raise DeliverableError("DELIVERABLE_EMPTY", f"{spec.label} 生成器没有落盘内容")
            os.replace(temp, target)
        finally:
            if temp.exists():
                temp.unlink()
        return DeliverableFile(kind=spec.kind, label=spec.label, file_name=spec.file_name,
                               path=target, sha256=sha256_file(target),
                               size=target.stat().st_size, details=dict(details))

    def _preflight(self, dataset: DeliverableDataset) -> None:
        require_unique_evidence(dataset)
        lookup = self.context.evidence_lookup
        if lookup is None:
            return
        for row in dataset.evidence:
            if lookup(row.evidence_id) is None:
                raise DeliverableError("EVIDENCE_NOT_FOUND",
                                       f"证据索引无法反查 evidence_id={row.evidence_id}")
        indexed = {row.evidence_id for row in dataset.evidence}
        for row in dataset.audit_diffs:
            if row.evidence_id and row.evidence_id not in indexed:
                raise DeliverableError("EVIDENCE_NOT_INDEXED",
                                       f"审核差异引用的证据未进入索引：{row.evidence_id}")

    def generate_all(self, dataset: DeliverableDataset) -> DeliverableManifest:
        self._preflight(dataset)
        files = tuple(self._generate_one(dataset, spec) for spec in SPECS)
        generated_at = datetime.now(timezone.utc).isoformat()
        manifest_body = {
            "schema": MANIFEST_SCHEMA,
            "task": "P1-10-DELIVER",
            "job_id": dataset.job_id,
            "project_name": dataset.project_name,
            "basis_pack_id": dataset.basis_pack_id,
            "generated_at": generated_at,
            "output_dir": str(self.output_dir(dataset.job_id)),
            "count": len(files),
            "files": [item.as_dict() for item in files],
        }
        encoded = json.dumps(manifest_body, ensure_ascii=False, indent=2, sort_keys=False)
        manifest_path = self.output_dir(dataset.job_id) / "manifest.json"
        temp = manifest_path.with_name(".manifest.json.tmp")
        payload = encoded.encode("utf-8")
        temp.write_bytes(payload)
        os.replace(temp, manifest_path)
        return DeliverableManifest(
            job_id=dataset.job_id,
            output_dir=self.output_dir(dataset.job_id),
            generated_at=generated_at,
            files=files,
            manifest_path=manifest_path,
            manifest_sha256=hashlib.sha256(payload).hexdigest(),
        )
