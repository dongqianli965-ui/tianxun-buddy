"""XLSX 解析器：逐行抽取单元格文本，保留 sheet / row / cell 定位。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Locator, ParsedDocument, ParseStatus, TextBlock, source_facts

__all__ = ["parse_xlsx"]

_CELL_SEPARATOR = " | "
_MAX_CELL_CHARS = 4000


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _row_text(values: list[Any], columns: list[int]) -> tuple[str, int | None]:
    parts: list[str] = []
    first_column: int | None = None
    for column, value in zip(columns, values):
        text = _cell_text(value)
        if not text:
            continue
        if first_column is None:
            first_column = column
        parts.append(text[:_MAX_CELL_CHARS])
    return _CELL_SEPARATOR.join(parts), first_column


def parse_xlsx(path: str | Path, *, data_only: bool = True) -> ParsedDocument:
    """解析 .xlsx，每个非空行一个文本块（定位 = sheet + row + 首列 cell）。"""
    digest, size = source_facts(path)
    source = str(Path(path))

    try:
        import openpyxl
        from openpyxl.utils import get_column_letter
    except ImportError as exc:  # pragma: no cover - 依赖缺失时明确失败
        return ParsedDocument(
            source=source, format="xlsx", status=ParseStatus.FAILED,
            sha256=digest, size_bytes=size,
            error=f"openpyxl 不可用: {exc}",
        )

    try:
        workbook = openpyxl.load_workbook(filename=source, data_only=False)
        cached = openpyxl.load_workbook(filename=source, data_only=True)
    except Exception as exc:  # noqa: BLE001 - 第三方异常统一转 FAILED
        return ParsedDocument(
            source=source, format="xlsx", status=ParseStatus.FAILED,
            sha256=digest, size_bytes=size,
            error=f"{type(exc).__name__}: {exc}",
        )

    blocks: list[TextBlock] = []
    sheets: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        for sheet in workbook.worksheets:
            non_empty_rows = 0
            structured_rows = []
            header_mapping = {}
            from ..core.boq import _map_headers
            for row in sheet.iter_rows():
                mapping = _map_headers(tuple(cell.value for cell in row))
                if {'code','name','unit','quantity'} <= set(mapping):
                    header_mapping = {k: {'column':v+1,'header_row':row[0].row} for k,v in mapping.items()}
                    break
            for row in sheet.iter_rows():
                values = [cached[sheet.title][cell.coordinate].value if data_only else cell.value for cell in row]
                columns = [int(cell.column) for cell in row]
                text, first_column = _row_text(values, columns)
                cells=[]
                unit_column = header_mapping.get('unit',{}).get('column')
                unit = _cell_text(sheet.cell(row[0].row,unit_column).value) if unit_column else None
                for cell in row:
                    value=cell.value
                    cells.append({'row':cell.row,'column':cell.column,'coordinate':cell.coordinate,
                        'raw_value':value,'cached_value':cached[sheet.title][cell.coordinate].value,
                        'formula':value if cell.data_type=='f' else None,'data_type':cell.data_type,
                        'number_format':cell.number_format,'unit':unit})
                structured_rows.append({'row':row[0].row,'empty':not bool(text),
                    'summary':any(word in text for word in ('合计','小计','总计','本页合计','本表合计')),'cells':cells})
                if not text:
                    continue
                non_empty_rows += 1
                row_index = int(row[0].row)
                locator = Locator(
                    sheet=sheet.title,
                    row=row_index,
                    column=first_column,
                    cell=f"{get_column_letter(first_column)}{row_index}" if first_column else None,
                )
                blocks.append(TextBlock(text=text, locator=locator, kind="row"))
            sheets.append(
                {
                    "name": sheet.title,
                    "max_row": int(sheet.max_row or 0),
                    "max_column": int(sheet.max_column or 0),
                    "non_empty_rows": non_empty_rows,
                    "state": sheet.sheet_state,
                    "merged_cells": [str(x) for x in sheet.merged_cells.ranges],
                    "header_mapping": header_mapping,
                    "rows": structured_rows,
                }
            )
            if non_empty_rows == 0:
                warnings.append(f"sheet {sheet.title!r} 没有非空行")
    finally:
        try:
            workbook.close()
            cached.close()
        except Exception:  # pragma: no cover - 关闭失败不影响结果
            pass

    status = ParseStatus.PARSED if blocks else ParseStatus.FAILED
    error = None if blocks else "工作簿中没有可解析的非空行"
    return ParsedDocument(
        source=source,
        format="xlsx",
        status=status,
        blocks=blocks,
        sha256=digest,
        size_bytes=size,
        metadata={"sheets": sheets, "data_only": data_only,
                  "boq_complete": any(s['header_mapping'] for s in sheets)},
        warnings=warnings,
        error=error,
    )
