"""DOCX 解析器：正文段落 + 表格单元格，保留段落序号 / 表格行列定位。

DOCX 是流式排版格式，文件本身不含固定页码；因此定位信息使用
``paragraph`` / ``table_index`` / ``row`` / ``column``，不臆造 page。
"""
from __future__ import annotations

from pathlib import Path

from .base import Locator, ParsedDocument, ParseStatus, TextBlock, source_facts

__all__ = ["parse_docx"]


def _iter_blocks(document):
    """按文档顺序产出 (kind, text, locator)。"""
    from docx.document import Document as _Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = document.element.body
    paragraph_index = 0
    table_index = 0
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph_index += 1
            paragraph = Paragraph(child, document)
            text = (paragraph.text or "").strip()
            if not text:
                continue
            yield "paragraph", text, Locator(paragraph=paragraph_index)
        elif child.tag == qn("w:tbl"):
            table = Table(child, document)
            for row_index, row in enumerate(table.rows, start=1):
                for column_index, cell in enumerate(row.cells, start=1):
                    cell_text = " ".join((cell.text or "").split())
                    if not cell_text:
                        continue
                    yield (
                        "table_cell",
                        cell_text,
                        Locator(
                            table_index=table_index,
                            row=row_index,
                            column=column_index,
                        ),
                    )
            table_index += 1
        elif child.tag == qn("w:sdt"):
            # 内容控件（常见于模板）内部按段落处理
            for inner in child.iter(qn("w:p")):
                paragraph_index += 1
                text = "".join(node.text or "" for node in inner.iter(qn("w:t"))).strip()
                if text:
                    yield "paragraph", text, Locator(paragraph=paragraph_index)


def parse_docx(path: str | Path) -> ParsedDocument:
    """解析 .docx 正文与表格，输出带定位的文本块。"""
    digest, size = source_facts(path)
    source = str(Path(path))

    try:
        import docx
    except ImportError as exc:  # pragma: no cover
        return ParsedDocument(
            source=source, format="docx", status=ParseStatus.FAILED,
            sha256=digest, size_bytes=size, error=f"python-docx 不可用: {exc}",
        )

    try:
        document = docx.Document(source)
    except Exception as exc:  # noqa: BLE001
        return ParsedDocument(
            source=source, format="docx", status=ParseStatus.FAILED,
            sha256=digest, size_bytes=size, error=f"{type(exc).__name__}: {exc}",
        )

    blocks: list[TextBlock] = []
    counts = {"paragraph": 0, "table_cell": 0}
    try:
        for kind, text, locator in _iter_blocks(document):
            blocks.append(TextBlock(text=text, locator=locator, kind=kind))
            counts[kind] = counts.get(kind, 0) + 1
    except Exception as exc:  # noqa: BLE001
        return ParsedDocument(
            source=source, format="docx", status=ParseStatus.FAILED,
            sha256=digest, size_bytes=size, error=f"{type(exc).__name__}: {exc}",
            blocks=blocks,
        )

    status = ParseStatus.PARSED if blocks else ParseStatus.FAILED
    return ParsedDocument(
        source=source,
        format="docx",
        status=status,
        blocks=blocks,
        sha256=digest,
        size_bytes=size,
        metadata={
            "tables": len(document.tables),
            "paragraphs_total": len(document.paragraphs),
            "blocks_by_kind": counts,
        },
        error=None if blocks else "DOCX 中没有可解析的文本",
    )
