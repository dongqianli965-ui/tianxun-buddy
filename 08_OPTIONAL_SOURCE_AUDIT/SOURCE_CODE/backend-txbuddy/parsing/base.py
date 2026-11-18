"""P1-09-PARSE：统一解析结果模型（文本块 + 定位信息）。

设计约束
--------
1. **统一结构**：所有格式（XLSX/PDF/DOCX/DXF/DWG）都产出同一个
   :class:`ParsedDocument`，正文是 :class:`TextBlock` 列表，每块必带
   :class:`Locator`（页码 / 行号 / 工作表 / 图元 id …），缺失定位的块不允许进入结果。
2. **原文不动**：解析器只读源文件；:class:`ParsedDocument` 记录源文件 SHA-256
   与字节数，调用方可据此证明原始件未被改写。
3. **不伪造**：无法解析时返回 ``status=BLOCKED``/``FAILED`` 并附原因，
   绝不返回"看起来正常"的空结果（DWG 未配置转换器即属于此类）。
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = [
    "ParseStatus",
    "Locator",
    "TextBlock",
    "ParsedDocument",
    "ParseError",
    "UnsupportedFormat",
    "SourceNotFound",
    "ConverterNotConfigured",
    "ConverterFailed",
    "sha256_file",
    "normalise_suffix",
]


def sha256_file(path: str | Path, chunk: int = 1024 * 1024) -> str:
    """流式计算文件 SHA-256，避免把大文件读进内存。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def normalise_suffix(path: str | Path) -> str:
    return Path(path).suffix.lower().lstrip(".")


class ParseStatus(str, Enum):
    """解析终态。``BLOCKED`` 表示缺少外部条件（如 DWG 转换器未授权/未配置）。"""

    PARSED = "PARSED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


class ParseError(Exception):
    """解析失败基类。"""


class UnsupportedFormat(ParseError):
    """没有对应解析器。"""


class SourceNotFound(ParseError):
    """源文件不存在或不是普通文件。"""


class ConverterNotConfigured(ParseError):
    """DWG 转换器未配置 —— 必须 BLOCKED，不得伪造文本。"""


class ConverterFailed(ParseError):
    """外部转换器执行失败（非 0 退出、无输出、超时）。"""


@dataclass(frozen=True)
class Locator:
    """定位信息。不同格式填各自可得的字段，但至少一个字段非空。

    * PDF：``page`` / ``line`` / ``bbox``
    * XLSX：``sheet`` / ``row`` / ``column`` / ``cell``
    * DOCX：``paragraph`` / ``table_index`` / ``row`` / ``column``
    * DXF/DWG：``entity_id`` / ``layer`` / ``x`` / ``y`` / ``space``
    """

    page: int | None = None
    line: int | None = None
    row: int | None = None
    column: int | None = None
    sheet: str | None = None
    cell: str | None = None
    paragraph: int | None = None
    table_index: int | None = None
    entity_id: str | None = None
    entity_type: str | None = None
    layer: str | None = None
    space: str | None = None
    x: float | None = None
    y: float | None = None
    bbox: tuple[float, float, float, float] | None = None

    def is_located(self) -> bool:
        """是否携带至少一项可用于回溯原文的定位信息。"""
        return any(value is not None for value in asdict(self).values())

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if data["bbox"] is not None:
            data["bbox"] = list(data["bbox"])
        return {key: value for key, value in data.items() if value is not None}

    def describe(self) -> str:
        """人类可读的定位串，例如 ``sheet=S1!row=2``。"""
        parts: list[str] = []
        if self.sheet is not None:
            parts.append(f"sheet={self.sheet}")
        if self.cell is not None:
            parts.append(f"cell={self.cell}")
        if self.page is not None:
            parts.append(f"page={self.page}")
        if self.paragraph is not None:
            parts.append(f"paragraph={self.paragraph}")
        if self.table_index is not None:
            parts.append(f"table={self.table_index}")
        if self.row is not None:
            parts.append(f"row={self.row}")
        if self.column is not None:
            parts.append(f"column={self.column}")
        if self.line is not None:
            parts.append(f"line={self.line}")
        if self.entity_id is not None:
            parts.append(f"entity={self.entity_id}")
        if self.layer is not None:
            parts.append(f"layer={self.layer}")
        if self.x is not None or self.y is not None:
            parts.append(f"at=({self.x},{self.y})")
        return "!".join(parts) if parts else "<unlocated>"


@dataclass(frozen=True)
class TextBlock:
    """一段文本 + 定位信息。"""

    text: str
    locator: Locator
    kind: str = "text"

    def __post_init__(self) -> None:
        if not self.locator.is_located():
            raise ValueError(f"TextBlock 缺少定位信息: {self.text[:40]!r}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "kind": self.kind,
            "locator": self.locator.as_dict(),
            "locator_desc": self.locator.describe(),
        }


@dataclass
class ParsedDocument:
    """统一解析结果。"""

    source: str
    format: str
    status: ParseStatus
    blocks: list[TextBlock] = field(default_factory=list)
    sha256: str | None = None
    size_bytes: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    blocked_reason: str | None = None
    converter: str | None = None

    @property
    def text(self) -> str:
        """所有块按顺序拼接的纯文本。"""
        return "\n".join(block.text for block in self.blocks)

    def block_count(self) -> int:
        return len(self.blocks)

    def unlocated_blocks(self) -> list[TextBlock]:
        return [block for block in self.blocks if not block.locator.is_located()]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "format": self.format,
            "status": self.status.value,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "block_count": len(self.blocks),
            "blocks": [block.as_dict() for block in self.blocks],
            "metadata": self.metadata,
            "warnings": list(self.warnings),
            "error": self.error,
            "blocked_reason": self.blocked_reason,
            "converter": self.converter,
        }


def source_facts(path: str | Path) -> tuple[str, int]:
    """返回 (sha256, size_bytes)，并校验源文件存在。"""
    candidate = Path(path)
    if not candidate.is_file():
        raise SourceNotFound(f"源文件不存在: {candidate}")
    return sha256_file(candidate), candidate.stat().st_size
