"""P1-09-PARSE 解析包：XLSX / PDF / DOCX / DXF + DWG 授权转换适配。

对外统一入口 :func:`parse_file`，所有格式返回同一个
:class:`~txbuddy.parsing.base.ParsedDocument`（文本块 + page/row/sheet/entity_id 定位）。
"""
from __future__ import annotations

from .base import (
    ConverterFailed,
    ConverterNotConfigured,
    Locator,
    ParsedDocument,
    ParseError,
    ParseStatus,
    SourceNotFound,
    TextBlock,
    UnsupportedFormat,
    normalise_suffix,
    sha256_file,
)
from .docx_parser import parse_docx
from .dwg_adapter import ConverterConfig, DwgConverterAdapter
from .dxf_parser import parse_dxf
from .pdf_parser import parse_pdf
from .registry import PARSERS, parse_file, parser_for, supported_extensions
from .xlsx_parser import parse_xlsx

__all__ = [
    "PARSERS",
    "ConverterConfig",
    "ConverterFailed",
    "ConverterNotConfigured",
    "DwgConverterAdapter",
    "Locator",
    "ParseError",
    "ParseStatus",
    "ParsedDocument",
    "SourceNotFound",
    "TextBlock",
    "UnsupportedFormat",
    "normalise_suffix",
    "parse_docx",
    "parse_dxf",
    "parse_file",
    "parse_pdf",
    "parse_xlsx",
    "parser_for",
    "sha256_file",
    "supported_extensions",
]
