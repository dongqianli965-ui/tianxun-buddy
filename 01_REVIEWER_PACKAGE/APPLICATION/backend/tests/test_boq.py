import io
import pytest
from openpyxl import Workbook
from txbuddy.core.boq import import_boq

HEADERS = ["项目编码", "项目名称", "项目特征", "计量单位", "工程量"]

def make_book(rows):
    wb = Workbook(); ws = wb.active
    ws.append(HEADERS)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return buf.read()

def test_import_valid_rows_and_unit_normalization():
    data = make_book([
        ["010502001001", "现浇混凝土矩形柱", "C30", "m³", 12.5],
        ["010401003001", "实心砖墙", "MU10", "平方米", 100],
        ["010515001001", "钢筋", "HRB400", "吨", 1.2],
    ])
    res = import_boq(data)
    assert res["stats"] == {"rows": 3, "valid": 3, "invalid": 0}
    assert [i["unit"] for i in res["items"]] == ["m3", "m2", "t"]
    assert res["items"][0]["quantity"] == "12.5"
    assert len(res["content_sha256"]) == 64

def test_invalid_unit_and_quantity_are_reported_not_dropped():
    data = make_book([
        ["A1", "合法项", "特征", "m3", 1],
        ["A2", "非法单位", "特征", "见说明", 2],
        ["A3", "非法工程量", "特征", "m2", "abc"],
    ])
    res = import_boq(data)
    assert res["stats"]["valid"] == 1 and res["stats"]["invalid"] == 2
    codes = [d["code"] for e in res["errors"] for d in e["details"]]
    assert "BOQ_UNIT_UNKNOWN" in codes and "BOQ_QTY_INVALID" in codes

def test_duplicate_code_and_missing_feature_warn():
    data = make_book([
        ["B1", "项目一", "特征一", "m", 5],
        ["B1", "项目一重复", "", "m", 6],
    ])
    res = import_boq(data)
    warns = [w["code"] for w in res["warnings"]]
    assert "BOQ_CODE_DUPLICATE" in warns
    assert "BOQ_FEATURE_MISSING" in warns
    assert res["stats"]["valid"] == 2

def test_missing_required_header_raises():
    wb = Workbook(); ws = wb.active
    ws.append(["编码", "名称", "单位"])  # 缺工程量
    ws.append(["A1", "x", "m"])
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    with pytest.raises(ValueError) as e:
        import_boq(buf.read())
    assert "缺少必需列" in str(e.value)

def test_hash_is_stable_for_same_content():
    data = make_book([["C1", "项目", "特征", "m3", 3]])
    assert import_boq(data)["content_sha256"] == import_boq(data)["content_sha256"]
