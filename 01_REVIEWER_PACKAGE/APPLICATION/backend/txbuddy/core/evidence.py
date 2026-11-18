"""P0-06-EVIDENCE 证据真实绑定：SHA-256 + 页码/行号/单元格/图纸位置。

证据不再只是一句 ``source_ref``。带文件绑定的证据同时携带：

* ``file_sha256``：证据生成时真实文件内容的 SHA-256；校验时重新读取
  文件并重算比对，因此文件被改动、替换或删除都会立刻校验失败。
* ``locator``：定位信息（page / row / cell / drawing），缺失或非法时
  证据判为“不完整”（``incomplete=True``）。
* ``extracted_text``：摘录 + 全长 + 全文 SHA-256 的摘要；对可解析文本
  还能按 locator 重新切片，复核摘录确实来自该文件的该位置。

向后兼容：不含任何文件绑定字段的历史证据只做载荷摘要校验。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

LOCATOR_ANCHORS = ("page", "row", "cell", "drawing")
_LOCATOR_ALIASES = {
    "page": "page", "pages": "page", "p": "page", "页": "page", "页码": "page",
    "row": "row", "rows": "row", "line": "row", "lines": "row", "r": "row",
    "l": "row", "行": "row", "行号": "row",
    "cell": "cell", "cells": "cell", "单元格": "cell", "格": "cell",
    "drawing": "drawing", "drawing_no": "drawing", "sheet": "drawing",
    "图纸": "drawing", "图号": "drawing", "图纸位置": "drawing",
}
_TEXT_SUFFIXES = frozenset({
    ".txt", ".text", ".md", ".csv", ".tsv", ".json", ".log", ".xml", ".yaml",
    ".yml", ".ini", ".cfg", ".sql", ".py", ".ps1", ".sh", ".rst",
})
_DELIMITED = {".csv": ",", ".tsv": "\t"}
_CELL_RE = re.compile(r"^([A-Za-z]{1,3})(\d{1,7})$")
_RANGE_RE = re.compile(r"(\d+)(?:\s*[-~—至]\s*(\d+))?")


def default(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(type(obj).__name__)


def canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=default)


def sha256_bytes(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def sha256_file(path, chunk_size=1 << 20):
    """流式计算文件内容 SHA-256（不把大文件整个读进内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------------ locator
def _plain(value):
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def _positions(value):
    """把 1-based 定位值解析成位置列表；非法返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return [value] if value >= 1 else None
    if isinstance(value, Decimal):
        return [int(value)] if value == int(value) and int(value) >= 1 else None
    text = str(value).strip().replace(" ", "")
    if not text:
        return None
    positions = []
    for part in re.split(r"[,，、;；]", text):
        if not part:
            return None
        match = _RANGE_RE.fullmatch(part)
        if not match:
            return None
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start:
            return None
        positions.extend(range(start, end + 1))
    return positions or None


def _parse_cell(value):
    """返回 (row, col) 定位；col 为 None 表示整行。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return (value, None) if value >= 1 else None
    text = str(value).strip().upper()
    match = _CELL_RE.fullmatch(text)
    if not match:
        return None
    col = 0
    for char in match.group(1):
        col = col * 26 + (ord(char) - 64)
    row = int(match.group(2))
    return (row, col) if row >= 1 and col >= 1 else None


def _drawing_ok(value):
    if isinstance(value, str):
        return bool(value.strip())
    if not isinstance(value, Mapping):
        return False
    sheet = value.get("sheet") or value.get("drawing_no") or value.get("图号")
    if not str(sheet or "").strip():
        return False
    return any(key in value for key in ("x", "y", "zone", "region", "element_id",
                                        "区域", "坐标"))


def parse_locator_text(text):
    """解析 ``page=12; row=40`` 或 ``第12页第40行 单元格B7`` 形式的定位串。"""
    parsed = {}
    for part in re.split(r"[;,；，]", str(text)):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        target = _LOCATOR_ALIASES.get(key.strip().lower())
        if target and value.strip():
            parsed[target] = value.strip()
    if parsed:
        return parsed
    page = re.search(r"(?:第\s*)?(\d+(?:\s*[-~]\s*\d+)*)\s*页", str(text))
    row = re.search(r"(?:第\s*)?(\d+(?:\s*[-~]\s*\d+)*)\s*行", str(text))
    cell = re.search(r"([A-Za-z]{1,3}\d{1,7})", str(text))
    if page:
        parsed["page"] = page.group(1)
    if row:
        parsed["row"] = row.group(1)
    if cell and not page and not row:
        parsed["cell"] = cell.group(1).upper()
    return parsed


def normalize_locator(locator):
    """返回规范化后的 locator 字典（中文/别名键统一成 page/row/cell/drawing）。"""
    if locator is None:
        return {}
    if isinstance(locator, str):
        locator = parse_locator_text(locator)
    if not isinstance(locator, Mapping):
        return {}
    normalized = {}
    for key, value in locator.items():
        target = _LOCATOR_ALIASES.get(str(key).strip().lower(), str(key).strip())
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        normalized[target] = _plain(value)
    return normalized


def locator_report(locator):
    """逐项检查定位是否齐全/合法。"""
    normalized = normalize_locator(locator)
    report = {"complete": False, "present": [], "missing": [], "invalid": [],
              "normalized": normalized}
    for anchor in LOCATOR_ANCHORS:
        value = normalized.get(anchor)
        if value is None or (isinstance(value, str) and not value.strip()):
            report["missing"].append(anchor)
            normalized.pop(anchor, None)
            continue
        if anchor in ("page", "row"):
            ok = _positions(value) is not None
        elif anchor == "cell":
            ok = _parse_cell(value) is not None
        else:
            ok = _drawing_ok(value)
        if ok:
            report["present"].append(anchor)
        else:
            report["invalid"].append(anchor)
            normalized.pop(anchor, None)
    for key in list(normalized):
        if key not in LOCATOR_ANCHORS and not str(normalized[key]).strip():
            normalized.pop(key)
    report["complete"] = bool(report["present"]) and not report["invalid"]
    return report


# ------------------------------------------------------------- text binding
def read_text(path):
    raw = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _col_label(index):
    label = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        label = chr(65 + remainder) + label
    return label


def _csv_cell_text(path, delimiter, anchors):
    cell = _parse_cell(anchors["cell"])
    if cell is None:
        return None, "INVALID_CELL"
    row_index, col_index = cell
    rows = list(csv.reader(io.StringIO(read_text(path)), delimiter=delimiter))
    if row_index > len(rows):
        return None, "CELL_OUT_OF_RANGE"
    row = rows[row_index - 1]
    if col_index is None:
        return "\t".join(row), f"cell:row{row_index}"
    if col_index > len(row):
        return None, "CELL_OUT_OF_RANGE"
    return row[col_index - 1], f"cell:{_col_label(col_index)}{row_index}"


def extract_located_text(path, locator):
    """按 locator 从文件切出文本；返回 ``(text, mode)`` 或 ``(None, reason)``。"""
    path = Path(path)
    report = locator_report(locator)
    if not report["complete"]:
        return None, ("LOCATOR_INVALID" if report["invalid"] else "LOCATOR_INCOMPLETE")
    anchors = report["normalized"]
    suffix = path.suffix.lower()
    if suffix in _DELIMITED and "cell" in anchors:
        return _csv_cell_text(path, _DELIMITED[suffix], anchors)
    if suffix not in _TEXT_SUFFIXES:
        return None, "UNSUPPORTED_SUFFIX"
    text = read_text(path)
    if "row" in anchors:
        positions = _positions(anchors["row"])
        lines = text.splitlines()
        picked = [lines[index - 1] for index in positions if index - 1 < len(lines)]
        if not picked:
            return None, "ROW_OUT_OF_RANGE"
        return "\n".join(picked), "row:" + str(anchors["row"]).replace(" ", "")
    if "page" in anchors:
        positions = _positions(anchors["page"])
        pages = text.split("\f")
        if len(pages) < 2:
            return (text, "page:1") if positions == [1] else (None, "PAGE_MARKERS_ABSENT")
        picked = [pages[index - 1] for index in positions if index - 1 < len(pages)]
        if not picked:
            return None, "PAGE_OUT_OF_RANGE"
        return "\n".join(picked), "page:" + str(anchors["page"]).replace(" ", "")
    return None, "NO_TEXT_ANCHOR"


def summarize_text(text, limit=200):
    text = "" if text is None else str(text)
    return {"excerpt": text if len(text) <= limit else text[:limit],
            "sha256": sha256_bytes(text.encode("utf-8")),
            "length": len(text), "truncated": len(text) > limit}


def _binding_text_summary(path, locator, limit):
    text, mode = extract_located_text(path, locator)
    if text is None:
        return {"excerpt": "", "sha256": sha256_bytes(b""), "length": 0,
                "truncated": False, "extractor": "unavailable",
                "available": False, "note": mode}
    return {**summarize_text(text, limit), "extractor": mode, "available": True}


_BINDING_FIELDS = ("file_path", "file_sha256", "locator", "extracted_text")
_META_KEYS = frozenset({"evidence_id", "sha256", "created_at"})
_LEGACY_REQUIRED = ("input_snapshot", "basis_pack_id", "rule_version",
                    "precision_rule_version", "sha256")


def build_evidence(file_path=None, locator=None, extracted_text=None,
                   text_limit=200, file_sha256=None, file_size=None, **kw):
    """构建证据；给 ``file_path`` 时自动绑定真实文件哈希、定位与摘录。"""
    payload = dict(kw)
    if file_path is not None:
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"evidence file not found: {path}")
        actual = sha256_file(path)
        if file_sha256 is not None and str(file_sha256).lower() != actual:
            raise ValueError("file_sha256 does not match file content")
        payload["file_path"] = str(path)
        payload["file_name"] = path.name
        payload["file_sha256"] = actual
        payload["file_size"] = path.stat().st_size
    elif file_sha256 is not None:
        payload["file_sha256"] = str(file_sha256).lower()
        if file_size is not None:
            payload["file_size"] = int(file_size)
    if locator is not None:
        payload["locator"] = normalize_locator(locator)
    if extracted_text is not None:
        payload["extracted_text"] = (_plain(extracted_text)
                                     if isinstance(extracted_text, Mapping)
                                     else summarize_text(extracted_text, text_limit))
    elif payload.get("file_path"):
        payload["extracted_text"] = _binding_text_summary(
            payload["file_path"], payload.get("locator"), text_limit)
    digest = sha256_bytes(canonical(payload).encode("utf-8"))
    return {"evidence_id": digest[:24], **payload, "sha256": digest,
            "created_at": datetime.now(timezone.utc).isoformat()}


def _failure(result, code, detail=None):
    result["reasons"].append(code)
    if detail:
        result.setdefault("details", {})[code] = detail


def verify_evidence(e, *, require_file=False):
    """校验证据：重算文件哈希 + 定位完整性 + 摘录复核 + 载荷摘要。

    返回 ``valid``（整体是否可信）、``complete``（结构与定位是否齐全）、
    ``reasons``（失败原因码）、``file`` / ``text``（明细）。
    """
    result = {"valid": False, "complete": False, "bound": False,
              "missing": [], "missing_binding": [], "missing_locator": [],
              "invalid_locator": [], "reasons": [], "reason": None,
              "file": None, "text": None, "actual_sha256": None,
              "expected_sha256": None}
    if not isinstance(e, Mapping):
        _failure(result, "EVIDENCE_NOT_MAPPING")
        result["reason"] = "EVIDENCE_NOT_MAPPING"
        return result
    result["expected_sha256"] = e.get("sha256")
    result["missing"] = [field for field in _LEGACY_REQUIRED if not e.get(field)]
    bound = any(field in e for field in _BINDING_FIELDS)
    result["bound"] = bound
    locator_complete = True
    if bound or require_file:
        file_path = e.get("file_path")
        expected = e.get("file_sha256")
        if not file_path or not expected:
            result["missing_binding"].append("file_sha256")
            _failure(result, "FILE_NOT_BOUND")
        else:
            info = {"path": str(file_path),
                    "expected_sha256": str(expected).lower(),
                    "exists": False, "actual_sha256": None, "size": None}
            path = Path(file_path)
            if not path.is_file():
                _failure(result, "FILE_MISSING")
            else:
                info["exists"] = True
                try:
                    info["size"] = path.stat().st_size
                    info["actual_sha256"] = sha256_file(path)
                except OSError as exc:
                    info["error"] = f"{type(exc).__name__}: {exc}"
                    _failure(result, "FILE_UNREADABLE")
                if (info["actual_sha256"] is not None
                        and info["actual_sha256"] != info["expected_sha256"]):
                    _failure(result, "FILE_SHA256_MISMATCH")
            result["file"] = info
        report = locator_report(e.get("locator"))
        locator_complete = report["complete"]
        result["locator"] = {"complete": report["complete"],
                             "present": report["present"],
                             "normalized": report["normalized"]}
        result["missing_locator"] = report["missing"]
        result["invalid_locator"] = report["invalid"]
        if not report["complete"]:
            _failure(result, "LOCATOR_INVALID" if report["invalid"]
                     else "LOCATOR_INCOMPLETE")
        summary = e.get("extracted_text")
        if summary is None:
            result["missing_binding"].append("extracted_text")
            _failure(result, "EXTRACTED_TEXT_MISSING")
        elif not isinstance(summary, Mapping):
            _failure(result, "EXTRACTED_TEXT_INVALID")
        else:
            text_info = {"present": True,
                         "available": summary.get("available", True),
                         "expected_sha256": summary.get("sha256"),
                         "actual_sha256": None,
                         "length": summary.get("length")}
            digest_ok = (isinstance(summary.get("sha256"), str)
                         and len(summary["sha256"]) == 64
                         and isinstance(summary.get("length"), int))
            if not digest_ok:
                _failure(result, "EXTRACTED_TEXT_INVALID")
            elif (text_info["available"] and result["file"]
                  and result["file"].get("actual_sha256") is not None
                  and result["file"]["actual_sha256"] == result["file"]["expected_sha256"]
                  and report["complete"]):
                fresh, mode = extract_located_text(file_path, e.get("locator"))
                text_info["extractor"] = mode
                if fresh is None:
                    text_info["note"] = mode
                else:
                    text_info["actual_sha256"] = sha256_bytes(fresh.encode("utf-8"))
                    if (text_info["actual_sha256"] != str(summary["sha256"]).lower()
                            or len(fresh) != summary["length"]):
                        _failure(result, "EXTRACTED_TEXT_MISMATCH")
            result["text"] = text_info
    payload = {key: value for key, value in e.items() if key not in _META_KEYS}
    result["actual_sha256"] = sha256_bytes(canonical(payload).encode("utf-8"))
    expected_digest = str(e.get("sha256") or "").lower()
    if not result["missing"] and result["actual_sha256"] != expected_digest:
        _failure(result, "PAYLOAD_SHA256_MISMATCH")
    result["complete"] = (not result["missing"] and not result["missing_binding"]
                          and locator_complete and not result["invalid_locator"])
    result["valid"] = bool(result["complete"] and not result["reasons"])
    result["reason"] = result["reasons"][0] if result["reasons"] else None
    return result


def evidence_complete(e):
    """便捷判断：证据结构/定位齐全且文件哈希可复核。"""
    return bool(verify_evidence(e).get("valid"))
