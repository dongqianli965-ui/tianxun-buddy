"""解析器注册表：按扩展名分发，DWG 交给外部转换适配层。"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .base import (
    ConverterNotConfigured,
    ParsedDocument,
    ParseStatus,
    SourceNotFound,
    normalise_suffix,
    sha256_file,
)
from .docx_parser import parse_docx
from .dwg_adapter import DwgConverterAdapter
from .dxf_parser import parse_dxf
from .pdf_parser import parse_pdf
from .xlsx_parser import parse_xlsx
from .html_parser import parse_html
from .text_parser import parse_text
from .doc_adapter import parse_doc
from .archive_adapter import parse_archive

__all__ = [
    "PARSERS",
    "parse_file",
    "supported_extensions",
    "parser_for",
]

Parser = Callable[..., ParsedDocument]

PARSERS: dict[str, Parser] = {
    "xlsx": parse_xlsx,
    "xlsm": parse_xlsx,
    "pdf": parse_pdf,
    "docx": parse_docx,
    "doc": parse_doc,
    "dxf": parse_dxf,
    "html": parse_html,
    "htm": parse_html,
    **{ext: parse_text for ext in ('csv','tsv','txt','md')},
    "zip": parse_archive,
    "rar": parse_archive,
}

DWG_EXTENSIONS = ("dwg",)


def supported_extensions() -> tuple[str, ...]:
    """全部受支持扩展名（含 DWG；DWG 需外部授权转换器才能真的解析）。"""
    return tuple(sorted(PARSERS)) + DWG_EXTENSIONS


def parser_for(suffix: str) -> Parser | None:
    return PARSERS.get(suffix.lower().lstrip("."))


def parse_file(
    path: str | Path,
    *,
    dwg_adapter: DwgConverterAdapter | None = None,
    **kwargs,
) -> ParsedDocument:
    """统一入口：按扩展名解析，返回 :class:`ParsedDocument`。"""
    candidate = Path(path)
    suffix = normalise_suffix(candidate)
    if not candidate.is_file():
        raise SourceNotFound(f"源文件不存在: {candidate}")

    parser = parser_for(suffix)
    if parser is not None:
        return parser(candidate, **kwargs)

    if suffix in DWG_EXTENSIONS:
        if dwg_adapter is None:
            try:
                dwg_adapter = DwgConverterAdapter()
            except ConverterNotConfigured as exc:
                return ParsedDocument(
                    source=str(candidate), format="dwg", status=ParseStatus.BLOCKED,
                    sha256=sha256_file(candidate), size_bytes=candidate.stat().st_size,
                    blocked_reason=f"DWG 转换器配置非法: {exc}",
                )
        return dwg_adapter.parse(candidate)

    if suffix in ('jpg','jpeg','png'):
        return ParsedDocument(source=str(candidate),format=suffix,status=ParseStatus.BLOCKED,
            sha256=sha256_file(candidate),size_bytes=candidate.stat().st_size,
            blocked_reason='IMAGE_OCR_REQUIRED: 请调用离线 OCR 通道')

    return ParsedDocument(
        source=str(candidate),
        format=suffix or "<none>",
        status=ParseStatus.FAILED,
        sha256=sha256_file(candidate),
        size_bytes=candidate.stat().st_size,
        error=f"不支持的格式 {candidate.suffix!r}，支持: {', '.join(supported_extensions())}",
    )
