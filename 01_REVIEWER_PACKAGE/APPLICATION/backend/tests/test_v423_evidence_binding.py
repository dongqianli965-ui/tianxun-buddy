"""P0-06-EVIDENCE 验收测试：证据与真实文件内容、定位、摘录强绑定。

覆盖 TASK.md 的硬要求：
1. 证据结构含 file_sha256 / locator(page,row,cell,drawing) / extracted_text 摘要；
2. 校验时重新读取文件、重算 SHA-256 并与证据比对；
3. 文件被改动（含只追加一个 NUL 字节）后证据校验失败；
4. 定位信息缺失/非法时证据判为不完整（incomplete，valid=False）；
5. 历史证据（无文件绑定）仍按载荷摘要校验，行为不回归。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from txbuddy.core.evidence import (
    build_evidence,
    evidence_complete,
    extract_located_text,
    locator_report,
    normalize_locator,
    sha256_file,
    summarize_text,
    verify_evidence,
)

TEXT = "清单第1行\n清单第2行\n清单第3行\n清单第4行\n"
LEGACY = dict(subject_type="QUANTITY", subject_id="R1", result="0.750",
              input_snapshot={"length": "1.5"}, basis_pack_id="B-1",
              rule_version="R-1", precision_rule_version="P-1")


def _text_file(tmp_path: Path, name: str = "BOQ-001.txt") -> Path:
    path = tmp_path / name
    path.write_text(TEXT, encoding="utf-8")
    return path


def _bound(tmp_path: Path, **overrides):
    path = _text_file(tmp_path)
    kwargs = dict(LEGACY, file_path=path, locator={"page": 1, "row": 2})
    kwargs.update(overrides)
    return path, build_evidence(**kwargs)


def test_build_binds_real_file_sha256_locator_and_excerpt(tmp_path):
    path, e = _bound(tmp_path)
    assert e["file_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert e["file_sha256"] == sha256_file(path)
    assert e["file_size"] == path.stat().st_size
    assert e["file_name"] == "BOQ-001.txt"
    assert e["locator"] == {"page": 1, "row": 2}
    assert e["extracted_text"]["excerpt"] == "清单第2行"
    assert e["extracted_text"]["length"] == len("清单第2行")
    assert e["extracted_text"]["sha256"] == summarize_text("清单第2行")["sha256"]
    assert e["extracted_text"]["available"] is True
    result = verify_evidence(e)
    assert result["valid"] is True and result["complete"] is True
    assert result["bound"] is True and result["reason"] is None
    assert result["file"]["actual_sha256"] == e["file_sha256"]
    assert result["file"]["exists"] is True
    assert result["text"]["actual_sha256"] == e["extracted_text"]["sha256"]


def test_file_modification_breaks_verification(tmp_path):
    path, e = _bound(tmp_path)
    path.write_text(TEXT.replace("第2行", "第2行(已篡改)"), encoding="utf-8")
    result = verify_evidence(e)
    assert result["valid"] is False
    assert result["reason"] == "FILE_SHA256_MISMATCH"
    assert result["complete"] is True  # 结构仍完整，失败原因只可能是内容哈希
    assert result["file"]["expected_sha256"] == e["file_sha256"]
    assert result["file"]["actual_sha256"] == sha256_file(path)
    assert result["file"]["actual_sha256"] != result["file"]["expected_sha256"]


def test_single_appended_nul_byte_breaks_verification(tmp_path):
    path = tmp_path / "drawing.dxf"
    path.write_bytes(b"0\nSECTION\n2\nENTITIES\n\x00\xff binary tail\n")
    e = build_evidence(**LEGACY, file_path=path,
                       locator={"drawing": {"sheet": "A-01", "x": 120.5,
                                            "y": 88.0, "element_id": "C-12"}})
    assert verify_evidence(e)["valid"] is True
    path.write_bytes(path.read_bytes() + b"\x00")
    result = verify_evidence(e)
    assert result["valid"] is False
    assert result["reason"] == "FILE_SHA256_MISMATCH"
    assert result["file"]["actual_sha256"] == sha256_file(path)


def test_missing_file_breaks_verification(tmp_path):
    path, e = _bound(tmp_path)
    path.unlink()
    result = verify_evidence(e)
    assert result["valid"] is False
    assert result["reason"] == "FILE_MISSING"
    assert result["file"]["exists"] is False
    assert result["file"]["actual_sha256"] is None


def test_locator_missing_marks_evidence_incomplete(tmp_path):
    path = _text_file(tmp_path)
    e = build_evidence(**LEGACY, file_path=path)
    result = verify_evidence(e)
    assert result["valid"] is False
    assert result["complete"] is False
    assert result["reason"] == "LOCATOR_INCOMPLETE"
    assert set(result["missing_locator"]) == {"page", "row", "cell", "drawing"}
    assert result["text"]["present"] is True  # 无定位时摘录退化为不可用，但结构仍在


def test_invalid_locator_values_are_rejected(tmp_path):
    path = _text_file(tmp_path)
    e = build_evidence(**LEGACY, file_path=path, locator={"page": 0, "row": "第2行"})
    result = verify_evidence(e)
    assert result["valid"] is False and result["complete"] is False
    assert result["reason"] == "LOCATOR_INVALID"
    assert set(result["invalid_locator"]) == {"page", "row"}

    report = locator_report({"cell": "！！！！", "drawing": {"x": 1, "y": 2}})
    assert report["complete"] is False
    assert set(report["invalid"]) == {"cell", "drawing"}


def test_locator_without_file_is_not_bound(tmp_path):
    e = build_evidence(**LEGACY, locator={"page": 1})
    result = verify_evidence(e)
    assert result["valid"] is False and result["complete"] is False
    assert result["reason"] == "FILE_NOT_BOUND"
    assert set(result["missing_binding"]) == {"file_sha256", "extracted_text"}


def test_extracted_text_must_match_file_location(tmp_path):
    path = _text_file(tmp_path)
    e = build_evidence(**LEGACY, file_path=path, locator={"row": 2},
                       extracted_text="伪造的摘录内容")
    result = verify_evidence(e)
    assert result["valid"] is False
    assert result["reason"] == "EXTRACTED_TEXT_MISMATCH"
    assert result["text"]["actual_sha256"] == summarize_text("清单第2行")["sha256"]
    assert result["text"]["expected_sha256"] != result["text"]["actual_sha256"]


def test_payload_tamper_still_detected(tmp_path):
    _, e = _bound(tmp_path)
    e["result"] = "999.999"
    result = verify_evidence(e)
    assert result["valid"] is False
    assert result["reason"] == "PAYLOAD_SHA256_MISMATCH"
    assert result["actual_sha256"] != e["sha256"]


def test_csv_cell_locator_binds_real_cell_value(tmp_path):
    path = tmp_path / "BOQ.csv"
    path.write_text("项目,单位,工程量\n混凝土,m3,12.5\n模板,m2,88\n", encoding="utf-8")
    e = build_evidence(**LEGACY, file_path=path, locator={"cell": "C2"})
    assert e["extracted_text"]["excerpt"] == "12.5"
    assert e["extracted_text"]["extractor"] == "cell:C2"
    result = verify_evidence(e)
    assert result["valid"] is True and result["complete"] is True
    assert result["text"]["actual_sha256"] == summarize_text("12.5")["sha256"]

    assert extract_located_text(path, {"cell": "B2"})[0] == "m3"
    out_of_range, reason = extract_located_text(path, {"cell": "Z99"})
    assert out_of_range is None and reason == "CELL_OUT_OF_RANGE"

    path.write_text("项目,单位,工程量\n混凝土,m3,99.9\n模板,m2,88\n", encoding="utf-8")
    assert verify_evidence(e)["reason"] == "FILE_SHA256_MISMATCH"


def test_page_locator_uses_form_feed_pages(tmp_path):
    path = tmp_path / "spec.txt"
    path.write_text("第一页内容\f第二页内容\f第三页内容", encoding="utf-8")
    e = build_evidence(**LEGACY, file_path=path, locator={"page": 2})
    assert e["extracted_text"]["excerpt"] == "第二页内容"
    assert e["extracted_text"]["extractor"] == "page:2"
    assert verify_evidence(e)["valid"] is True


def test_drawing_locator_accepts_binary_and_keeps_text_summary(tmp_path):
    path = tmp_path / "A-01.dxf"
    path.write_bytes(b"0\nSECTION\n2\nENTITIES\n\x00\xff")
    locator = {"drawing": {"sheet": "A-01", "x": 120.5, "y": 88.0,
                           "element_id": "C-12"}}
    e = build_evidence(**LEGACY, file_path=path, locator=locator)
    assert e["locator"] == locator
    assert e["extracted_text"]["available"] is False
    assert e["extracted_text"]["note"] == "UNSUPPORTED_SUFFIX"
    result = verify_evidence(e)
    assert result["valid"] is True
    assert result["locator"]["present"] == ["drawing"]


def test_string_locator_is_normalized(tmp_path):
    assert normalize_locator("page=12; row=40") == {"page": "12", "row": "40"}
    assert normalize_locator("第12页第40行") == {"page": "12", "row": "40"}
    path = _text_file(tmp_path)
    e = build_evidence(**LEGACY, file_path=path, locator="page=1; row=3")
    assert e["locator"] == {"page": "1", "row": "3"}
    assert e["extracted_text"]["excerpt"] == "清单第3行"
    assert verify_evidence(e)["valid"] is True


def test_legacy_evidence_without_file_binding_still_verifies():
    e = build_evidence(**LEGACY, source_refs=["GB/T 50854-2024 表 5.2"])
    result = verify_evidence(e)
    assert result["bound"] is False
    assert result["valid"] is True and result["complete"] is True
    forced = verify_evidence(e, require_file=True)
    assert forced["valid"] is False and forced["reason"] == "FILE_NOT_BOUND"


def test_evidence_digest_is_deterministic(tmp_path):
    path = _text_file(tmp_path)
    first = build_evidence(**LEGACY, file_path=path, locator={"row": 2})
    second = build_evidence(**LEGACY, file_path=path, locator={"row": 2})
    assert first["sha256"] == second["sha256"]
    assert first["evidence_id"] == first["sha256"][:24]
    other = build_evidence(**LEGACY, file_path=path, locator={"row": 3})
    assert other["sha256"] != first["sha256"]


def test_truncated_excerpt_keeps_hash_of_full_slice(tmp_path):
    path = _text_file(tmp_path)
    e = build_evidence(**LEGACY, file_path=path, locator={"row": "1-4"},
                       text_limit=5)
    summary = e["extracted_text"]
    assert summary["truncated"] is True
    assert summary["excerpt"] == TEXT.splitlines()[0][:5]
    assert summary["length"] == len("\n".join(TEXT.splitlines()[:4]))
    assert verify_evidence(e)["valid"] is True


def test_build_rejects_wrong_hash_and_missing_file(tmp_path):
    path = _text_file(tmp_path)
    with pytest.raises(ValueError):
        build_evidence(**LEGACY, file_path=path, file_sha256="0" * 64)
    with pytest.raises(FileNotFoundError):
        build_evidence(**LEGACY, file_path=tmp_path / "missing.pdf",
                       locator={"page": 1})
    good = build_evidence(**LEGACY, file_path=path,
                          file_sha256=sha256_file(path), locator={"row": 1})
    assert good["file_sha256"] == sha256_file(path)


def test_evidence_complete_helper_and_bad_payload(tmp_path):
    _, e = _bound(tmp_path)
    assert evidence_complete(e) is True
    e["locator"] = {}
    assert evidence_complete(e) is False
    assert verify_evidence("not-an-evidence")["reason"] == "EVIDENCE_NOT_MAPPING"

