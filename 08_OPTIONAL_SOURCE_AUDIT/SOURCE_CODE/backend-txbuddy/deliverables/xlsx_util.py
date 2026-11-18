"""P1-10：XLSX 成果的公共落笔工具（表头、金额格式、列宽、冻结窗格）。

所有成果都是真实工作簿：openpyxl 可重新打开，金额单元格是数值而不是字符串。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

MONEY_FMT = "#,##0.00"
QTY_FMT = "#,##0.000"
PERCENT_FMT = "0.00%"
HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")
TOTAL_FILL = PatternFill("solid", fgColor="FFF2CC")
THIN = Side(style="thin", color="999999")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical="top")
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)


def display_width(text: Any) -> int:
    """中文按 2 个字符宽估算，用于列宽与 PDF 表格对齐。"""
    return sum(2 if ord(char) > 0x2E80 else 1 for char in str(text))


def _cell(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bool):
        return "是" if value else "否"
    return value


def create_workbook() -> Workbook:
    wb = Workbook()
    wb.remove(wb.active)
    return wb


def add_sheet(wb: Workbook, title: str, *, index: int | None = None) -> Worksheet:
    """按固定顺序建表；同名表已存在时直接返回（同一次生成内不重复建表）。"""
    if title in wb.sheetnames:
        return wb[title]
    ws = wb.create_sheet(title=title, index=index)
    return ws


def write_table(ws: Worksheet, *, title: str, headers: Sequence[str],
                rows: Iterable[Sequence[Any]], meta: Iterable[tuple[str, Any]] = (),
                money_cols: Sequence[int] = (), qty_cols: Sequence[int] = (),
                percent_cols: Sequence[int] = (), widths: Sequence[int] | None = None,
                start_row: int = 1, subtitle: str = "") -> int:
    """写一张带标题/元信息/表头的表，返回最后一行行号（1-based）。

    ``money_cols`` / ``qty_cols`` / ``percent_cols`` 是 0 基列下标。
    """
    rows = [list(values) for values in rows]
    ncols = max(1, len(headers))
    last_col = get_column_letter(ncols)
    row = start_row
    ws.merge_cells(f"A{row}:{last_col}{row}")
    cell = ws.cell(row=row, column=1, value=title)
    cell.font = Font(bold=True, size=14)
    cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[row].height = 24
    row += 1
    if subtitle:
        ws.merge_cells(f"A{row}:{last_col}{row}")
        ws.cell(row=row, column=1, value=subtitle).font = Font(italic=True, size=10)
        row += 1
    for label, value in meta:
        ws.cell(row=row, column=1, value=str(label)).font = Font(bold=True)
        if ncols > 1:
            ws.merge_cells(start_row=row, start_column=2, end_row=row, end_column=ncols)
        ws.cell(row=row, column=2, value=_cell(value))
        row += 1
    row += 1  # 空行
    header_row = row
    for index, name in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=index, value=name)
        cell.font = Font(bold=True)
        cell.fill = HEADER_FILL
        cell.alignment = CENTER
        cell.border = BORDER
    ws.row_dimensions[row].height = 28
    row += 1
    for values in rows:
        for index, value in enumerate(values, start=1):
            cell = ws.cell(row=row, column=index, value=_cell(value))
            cell.border = BORDER
            cell.alignment = WRAP
            col = index - 1
            if col in money_cols:
                cell.number_format = MONEY_FMT
            elif col in qty_cols:
                cell.number_format = QTY_FMT
            elif col in percent_cols:
                cell.number_format = PERCENT_FMT
        row += 1
    for index in range(1, ncols + 1):
        letter = get_column_letter(index)
        if widths and index <= len(widths):
            ws.column_dimensions[letter].width = widths[index - 1]
        else:
            longest = max([display_width(headers[index - 1])]
                          + [display_width(r[index - 1]) for r in rows
                             if len(r) >= index and r[index - 1] is not None] or [0])
            ws.column_dimensions[letter].width = min(40, max(10, longest + 2))
    if start_row == 1:
        ws.freeze_panes = f"A{header_row + 1}"
    ws.sheet_view.showGridLines = False
    return row - 1


def add_total_row(ws: Worksheet, row: int, values: Sequence[Any], *,
                  money_cols: Sequence[int] = ()) -> int:
    """在指定行写加粗合计行（数据已经由调用方算好，不在这里改口径）。"""
    for index, value in enumerate(values, start=1):
        cell = ws.cell(row=row, column=index, value=_cell(value))
        cell.font = Font(bold=True)
        cell.fill = TOTAL_FILL
        cell.border = BORDER
        if index - 1 in money_cols:
            cell.number_format = MONEY_FMT
    return row + 1


def save_workbook(wb: Workbook, path) -> None:
    wb.save(path)
