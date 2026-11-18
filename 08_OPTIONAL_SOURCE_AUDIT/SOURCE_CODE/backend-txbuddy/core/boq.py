"""清单（BOQ）导入与校验：Excel/CSV → 标准结构 + 校验报告。

规则来源 docs/06（BOQItem）与 docs/03（禁止无来源数据入库）：
- 必需列：项目编码、项目名称、计量单位、工程量；项目特征可选但缺失告警。
- 单位只接受白名单；m²/m³/米/平方米/立方米 归一化为 m2/m3/m。
- 工程量必须是 >=0 的十进制；非法行进入 errors，不静默丢弃。
- 输出带内容哈希，便于版本比对与证据引用。
"""
from __future__ import annotations

import hashlib
import io
from decimal import Decimal, InvalidOperation
from typing import Any, BinaryIO

from openpyxl import load_workbook

REQUIRED_HEADERS = {"code": "项目编码", "name": "项目名称", "unit": "计量单位", "quantity": "工程量"}
HEADER_ALIASES = {
    "code": ["项目编码", "清单编码", "编码", "code", "item_code"],
    "name": ["项目名称", "清单名称", "名称", "name"],
    "feature": ["项目特征", "项目特征描述", "特征", "feature"],
    "unit": ["计量单位", "单位", "unit"],
    "quantity": ["工程量", "数量", "quantity", "qty"],
    "section": ["分部", "章节", "分部名称", "section"],
}
UNIT_MAP = {
    "m": "m", "米": "m", "m2": "m2", "m²": "m2", "平方米": "m2", "㎡": "m2",
    "m3": "m3", "m³": "m3", "立方米": "m3", "方": "m3",
    "kg": "kg", "千克": "kg", "公斤": "kg", "t": "t", "吨": "t",
    "个": "个", "台": "台", "套": "套", "樘": "樘", "处": "处", "组": "组", "根": "根", "项": "项",
}


def _norm(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _map_headers(row: tuple) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx, raw in enumerate(row):
        text = _norm(raw).replace(" ", "")
        if not text:
            continue
        for key, aliases in HEADER_ALIASES.items():
            if key in mapping:
                continue
            if text in aliases or any(text == a.replace(" ", "") for a in aliases):
                mapping[key] = idx
                break
    return mapping


def import_boq(source: str | bytes | BinaryIO, sheet_name: str | None = None,
               max_rows: int = 20000) -> dict[str, Any]:
    """解析清单文件，返回 items + errors + warnings + 统计 + 内容哈希。"""
    raw_bytes = source if isinstance(source, (bytes, bytearray)) else None
    file_sha256 = hashlib.sha256(bytes(raw_bytes)).hexdigest() if raw_bytes is not None else None
    data = source
    if isinstance(source, (bytes, bytearray)):
        data = io.BytesIO(source)
    wb = load_workbook(data if not isinstance(source, str) else source, read_only=True, data_only=True)
    ws = wb[sheet_name] if sheet_name else wb[wb.sheetnames[0]]

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    seen_codes: dict[str, int] = {}

    rows = ws.iter_rows(values_only=True)
    try:
        header_row = next(rows)
    except StopIteration:
        return {"items": [], "errors": [{"row": 0, "code": "BOQ_EMPTY", "message": "工作表为空"}],
                "warnings": [], "stats": {"rows": 0, "valid": 0, "invalid": 0}, "content_sha256": None}

    mapping = _map_headers(header_row)
    missing_headers = [REQUIRED_HEADERS[k] for k in REQUIRED_HEADERS if k not in mapping]
    if missing_headers:
        raise ValueError(f"清单缺少必需列：{missing_headers}；实际表头={[_norm(x) for x in header_row]}")

    mapping_snapshot = {k: _norm(header_row[v]) for k, v in sorted(mapping.items())}
    row_no = 1
    for row in rows:
        row_no += 1
        if row_no > max_rows + 1:
            warnings.append({"row": row_no, "code": "BOQ_ROW_LIMIT", "message": f"超过 {max_rows} 行，后续被截断"})
            break
        if row is None or all(_norm(v) == "" for v in row):
            continue
        def cell(key: str) -> str:
            idx = mapping.get(key)
            return _norm(row[idx]) if idx is not None and idx < len(row) else ""

        code, name, unit_raw, qty_raw = cell("code"), cell("name"), cell("unit"), cell("quantity")
        feature, section = cell("feature"), cell("section")
        row_errors = []
        if not code:
            row_errors.append({"code": "BOQ_CODE_MISSING", "message": "项目编码为空"})
        if not name:
            row_errors.append({"code": "BOQ_NAME_MISSING", "message": "项目名称为空"})
        unit = UNIT_MAP.get(unit_raw.replace(" ", ""))
        if not unit:
            row_errors.append({"code": "BOQ_UNIT_UNKNOWN", "message": f"计量单位不在白名单：{unit_raw!r}"})
        quantity = None
        try:
            quantity = Decimal(qty_raw.replace(",", ""))
            if quantity < 0:
                row_errors.append({"code": "BOQ_QTY_NEGATIVE", "message": f"工程量为负：{qty_raw}"})
        except (InvalidOperation, AttributeError):
            row_errors.append({"code": "BOQ_QTY_INVALID", "message": f"工程量非法：{qty_raw!r}"})
        if not feature:
            warnings.append({"row": row_no, "code": "BOQ_FEATURE_MISSING", "message": f"{code or '?'} 缺项目特征"})
        if code:
            if code in seen_codes:
                warnings.append({"row": row_no, "code": "BOQ_CODE_DUPLICATE",
                                 "message": f"项目编码重复：{code}（首次出现在第 {seen_codes[code]} 行）"})
            else:
                seen_codes[code] = row_no
        if row_errors:
            errors.append({"row": row_no, "code": "BOQ_ROW_INVALID", "details": row_errors,
                           "raw": {"code": code, "name": name, "unit": unit_raw, "quantity": qty_raw}})
            continue
        items.append({
            "row": row_no, "code": code, "name": name, "feature": feature, "unit": unit,
            "quantity": str(quantity), "section": section,
        })

    import json as _json
    normalized = [
        {
            "row": i["row"],
            "code": i["code"],
            "name": i["name"],
            "feature": i.get("feature", ""),
            "unit": i["unit"],
            "quantity": str(Decimal(i["quantity"]).normalize()),
            "section": i.get("section", ""),
        }
        for i in items
    ]
    business_payload = {"schema": "boq.norm.v1", "headers": mapping_snapshot, "items": normalized}
    digest = hashlib.sha256(_json.dumps(business_payload, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "items": items,
        "errors": errors,
        "warnings": warnings,
        "stats": {"rows": row_no - 1, "valid": len(items), "invalid": len(errors)},
        "headers": {k: _norm(header_row[v]) for k, v in mapping.items()},
        "content_sha256": digest,
        "file_sha256": file_sha256,
        "hash_schema": "boq.norm.v1",
        "sheet": ws.title,
    }
