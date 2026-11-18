"""PDF 解析器：逐页取文本层，每行一个块（定位 = page + line）。"""
from __future__ import annotations

from pathlib import Path

from .base import Locator, ParsedDocument, ParseStatus, TextBlock, source_facts

__all__ = ["parse_pdf"]


def _clean_lines(raw: str) -> list[str]:
    text = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            lines.append(stripped)
    return lines


def parse_pdf(path: str | Path, *, password: str | None = None) -> ParsedDocument:
    """解析带文本层的 PDF。扫描件无文本层时给出明确 warning，不伪造 OCR 结果。"""
    digest, size = source_facts(path)
    source = str(Path(path))

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover
        return ParsedDocument(
            source=source, format="pdf", status=ParseStatus.FAILED,
            sha256=digest, size_bytes=size, error=f"pypdf 不可用: {exc}",
        )

    try:
        reader = PdfReader(source)
        encrypted = bool(reader.is_encrypted)
        if encrypted:
            opened = reader.decrypt(password or "")
            if not opened:
                return ParsedDocument(
                    source=source, format="pdf", status=ParseStatus.BLOCKED,
                    sha256=digest, size_bytes=size,
                    blocked_reason="PDF 已加密且未提供可用密码",
                )
    except Exception as exc:  # noqa: BLE001
        return ParsedDocument(
            source=source, format="pdf", status=ParseStatus.FAILED,
            sha256=digest, size_bytes=size, error=f"{type(exc).__name__}: {exc}",
        )

    blocks: list[TextBlock] = []
    warnings: list[str] = []
    pages_meta: list[dict[str, object]] = []
    empty_pages: list[int] = []
    try:
        page_total = len(reader.pages)
        for index, page in enumerate(reader.pages, start=1):
            try:
                raw = page.extract_text() or ""
            except Exception as exc:  # noqa: BLE001 - 单页失败不拖垮整份文档
                warnings.append(f"page {index} 提取失败: {type(exc).__name__}: {exc}")
                raw = ""
            lines = _clean_lines(raw)
            if not lines:
                empty_pages.append(index)
            for line_no, line in enumerate(lines, start=1):
                blocks.append(
                    TextBlock(
                        text=line,
                        locator=Locator(page=index, line=line_no),
                        kind="line",
                    )
                )
            pages_meta.append({"page": index, "lines": len(lines)})
    except Exception as exc:  # noqa: BLE001
        return ParsedDocument(
            source=source, format="pdf", status=ParseStatus.FAILED,
            sha256=digest, size_bytes=size, error=f"{type(exc).__name__}: {exc}",
            warnings=warnings,
        )

    if empty_pages:
        warnings.append(
            "以下页面没有文本层（可能是扫描件，需要 OCR，本解析器不伪造内容）: "
            + ",".join(str(page) for page in empty_pages)
        )

    status = ParseStatus.PARSED if blocks else ParseStatus.FAILED
    error = None if blocks else "PDF 没有可提取的文本层（扫描件需 OCR）"
    return ParsedDocument(
        source=source,
        format="pdf",
        status=status,
        blocks=blocks,
        sha256=digest,
        size_bytes=size,
        metadata={
            "pages": page_total,
            "pages_detail": pages_meta,
            "empty_pages": empty_pages,
            "encrypted": encrypted,
            "has_text_layer": bool(blocks),
        },
        warnings=warnings,
        error=error,
    )
