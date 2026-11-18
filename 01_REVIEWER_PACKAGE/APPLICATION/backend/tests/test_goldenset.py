"""L1_GoldenSet_001 接入守护测试：把交接说明的铁律变成可执行断言。"""
import os
from decimal import Decimal

import pytest

from txbuddy.core import goldenset as gsm

ROOT = os.environ.get("TX_GOLDENSET_ROOT", r"F:\ZHU-FISH\TIANXUN_BUDDY_V4\TEST_DATA\L1_GoldenSet_001")
pytestmark = pytest.mark.skipif(not os.path.isdir(ROOT), reason="GoldenSet 资料包未就位")


@pytest.fixture(scope="module")
def gs():
    return gsm.load(ROOT)


def test_zip_sha256_matches(gs):
    assert gs.zip_sha256_ok is True


def test_row_count_148_and_major_split(gs):
    assert len(gs.rows) == 148
    assert gs.stats["major_土建"] == 50
    assert gs.stats["major_安装"] == 98


def test_anchors_sum_equals_control_total(gs):
    parts = ["分部分项工程费", "安全文明施工费", "单价措施费", "暂列金额", "规费", "税金"]
    s = sum((gs.anchors[k] for k in parts), Decimal("0"))
    assert s == Decimal("5898506.43")
    assert gs.anchors["招标控制价合计"] == Decimal("5898506.43")


def test_bid_price_below_control(gs):
    assert gs.anchors["中标价"] == Decimal("5886069.47")
    assert gs.anchors["中标价"] < gs.anchors["招标控制价合计"]


def test_no_fabricated_fields(gs):
    res = gsm.validate(gs)
    assert res["ok"] is True, res["problems"]


def test_formal_ready_always_false(gs):
    assert gs.formal_ready is False


def test_gaps_are_exposed_not_hidden(gs):
    joined = " ".join(gs.gaps)
    for key in ["施工图", "计算底稿", "定额", "信息价", "审核", "合同原件"]:
        assert key in joined
    assert "图纸" in joined


def test_no_downpush_guard_raises_when_unit_price_present():
    class Fake:
        anchors = {"招标控制价合计": Decimal("5898506.43")}
        rows = [{"原始工程量": "83.63", "综合单价": "123.45"}]
        formal_ready = False
    with pytest.raises(gsm.GoldenSetError):
        gsm.assert_no_downpush(Fake())


def test_manifest_export_writes_json(gs, tmp_path):
    out = gsm.export_manifest(gs, tmp_path / "manifest.json")
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert '"formal_ready": false' in text and '"row_count": 148' in text
