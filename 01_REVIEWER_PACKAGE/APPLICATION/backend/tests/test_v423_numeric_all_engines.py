"""V4.2.3 / P0-01-NUMERIC：Decimal 全引擎数值安全回归（入口 × 非法值矩阵）。

覆盖范围（对照 tasks/P0-01-NUMERIC/TASK.md）：
1. 入口清单：api.py 中每个 POST 端点都被显式分类（数值入口 / 非数值入口 / 越界缺口），
   新增端点而不更新本清单会让用例失败 —— 避免“只覆盖部分入口”。
2. 每个数值入口 × 6 类非法数值（NaN / Infinity / -Infinity / 1e999 / 除零 / 超精度）
   必须返回 422 + NUMERIC_* 业务码，且永远不是 500。
3. 每个核心计算函数（qty / quota / price / rules / precision / goldenset）直接注入非法值：
   必须抛 NumericError(NUMERIC_*)，不得让 decimal.InvalidOperation / DivisionByZero 逃逸。
4. 禁止静默截断/静默溢出：1e-30 不得当 0、1e999 不得当大数、1e12×1e12 不得原样返回。
5. 正常值回归：守卫不得误伤合法计算。
6. goldenset：数值来自 Excel，非法数值必须 fail-closed（不得 ok=true / formal_ready=true）。

关于“除零”类：只有真正含除法的入口（rules 表达式、price/index-adjust 的指数比值）可以
构造除零输入；不含除法的入口（quantity / price-project-total / quota）用
“零操作数 422 + AST 静态证明该引擎无除法 + 共享 safe_div 守卫” 覆盖，既不 skip 也不放松。
"""
from __future__ import annotations

import ast
import inspect
import io
import textwrap
from decimal import Decimal, DecimalException

import pytest
from tests.authenticated_client import TestClient
from openpyxl import Workbook

from txbuddy.api import RULES_PATH, app, registry
from txbuddy.core import goldenset as gs_module
from txbuddy.core import numeric, precision, pricing, quantity, quota
from txbuddy.core.calcrun import build_snapshot
from txbuddy.core.numeric import (CODE_DIV_ZERO, CODE_NEGATIVE, CODE_NOT_FINITE, CODE_OVERFLOW,
                                  CODE_PRECISION_EXCEEDED, CODE_ZERO_NOT_ALLOWED, NUMERIC_CODES,
                                  NumericError, ensure_finite, safe_div, to_decimal)
from txbuddy.core.rules import RuleError, RuleRegistry

client = TestClient(app, raise_server_exceptions=False)

# ---------------------------------------------------------------- 公共夹具
BASIS = dict(basis_pack_id="B-V423-NUM", region_code="410100", region_level="CITY", major="BUILDING",
             effective_date="2026-01-01", measurement_standard_version="GB/T 50854-2024 [已核]",
             pricing_standard_version="GB/T 50500-2024 [已核]", rule_version_scope="HENAN_2025",
             precision_rule_version="P-V1", status="LOCKED", test_only=False,
             sources=[{"source_id": "S1", "title": "河南定额", "source_type": "QUOTA",
                       "license_status": "LICENSED", "verified": True}])

DIV_RULE_CODE = "TEST.V423.DIV.ZERO"


@pytest.fixture(scope="module", autouse=True)
def div_rule():
    """注册一条只在本文件使用的除法规则，用于构造真实“除零”入口；用例结束即摘除。"""
    registry.register({
        "rule_code": DIV_RULE_CODE, "version": "1", "name": "除零探针（TEST_ONLY）",
        "unit": "unitless", "expression": "L / H", "variables": {"L": "unitless", "H": "unitless"},
        "decimals": 6, "standard_ref": "TEST_ONLY", "license_status": "TEST_ONLY",
        "source_ref": "S1",
    })
    try:
        yield DIV_RULE_CODE
    finally:
        registry._rules.pop((DIV_RULE_CODE, "1"), None)


def _code(response) -> str | None:
    """取业务错误码；非 JSON 或非 detail.code 形状返回 None（测试会因此失败）。"""
    try:
        detail = response.json().get("detail")
    except ValueError:
        return None
    return detail.get("code") if isinstance(detail, dict) else None


def _assert_numeric_422(response, expected: str | None = None):
    assert response.status_code != 500, f"非法数值不得 500：{response.text[:300]}"
    assert response.status_code == 422, f"应返回 422，实际 {response.status_code}：{response.text[:300]}"
    code = _code(response)
    assert code in NUMERIC_CODES, f"错误码必须落在 NUMERIC_* 族，实际 {code!r}：{response.text[:300]}"
    if expected is not None:
        assert code == expected, f"期望 {expected}，实际 {code}：{response.text[:300]}"


# ---------------------------------------------------------------- 6 类非法数值
BAD_NUMERICS = {
    "nan": "NaN",
    "infinity": "Infinity",
    "negative_infinity": "-Infinity",
    "overflow_1e999": "1e999",
    "precision_1e-30": "1e-30",
}
ALL_CLASSES = ["nan", "infinity", "negative_infinity", "overflow_1e999", "divide_by_zero", "precision_1e-30"]


# ---------------------------------------------------------------- 入口清单
def _post_paths() -> set[str]:
    return {route.path for route in app.routes if "POST" in (getattr(route, "methods", None) or set())}


def _basis(**kw) -> dict:
    body = dict(BASIS)
    body.update(kw)
    return body


def _qty_calc(bad_class: str):
    value = "0" if bad_class == "divide_by_zero" else BAD_NUMERICS[bad_class]
    return {"basis_pack": _basis(), "operation": "RECTANGULAR_VOLUME",
            "params": {"length": value, "width": "1", "height": "1"}, "unit": "m3",
            "rule_code": "R", "rule_version": "1", "standard_ref": "TEST"}


def _rule_calc(bad_class: str):
    if bad_class == "divide_by_zero":
        return {"basis_pack": _basis(), "rule_code": DIV_RULE_CODE, "params": {"L": "1", "H": "0"}}
    return {"basis_pack": _basis(), "rule_code": "QTY.BOQ.CONC.COLUMN.001",
            "params": {"b": BAD_NUMERICS[bad_class], "h": "1", "H": "1"}}


def _price_unit(bad_class: str):
    consumption = "0" if bad_class == "divide_by_zero" else BAD_NUMERICS[bad_class]
    return {"resources": [{"resource_code": "M1", "resource_type": "MATERIAL",
                           "consumption": consumption, "unit_price": "10", "price_ref": "TEST"}],
            "management_rate": "0.1", "profit_rate": "0.05",
            "management_source_ref": "TEST-MGMT", "profit_source_ref": "TEST-PROFIT"}


def _index_adjust(bad_class: str):
    if bad_class == "divide_by_zero":
        body = {"base_amount": "1000", "base_index": "0"}
    else:
        body = {"base_amount": BAD_NUMERICS[bad_class], "base_index": "100"}
    body.update({"basis_pack": _basis(), "current_index": "110", "risk_rate": "0.05",
                 "mode": "FULL_EXCESS", "contract_clause_ref": "C-1"})
    return body


def _project_total(bad_class: str):
    body = {"basis_pack": _basis(), "boq_total": "100", "measure_total": "10", "other_total": "0",
            "fee_items": [], "safety_production_fee": {"fee_item": "SAFETY_PRODUCTION_FEE",
                                                       "amount": "3", "is_noncompetitive": True,
                                                       "source_ref": "S1"},
            "tax_rate": "0.09", "formal_calculation": True}
    if bad_class in BAD_NUMERICS:
        body["boq_total"] = BAD_NUMERICS[bad_class]
    return body


def _apply_notes(bad_class: str):
    if bad_class == "divide_by_zero":
        factor, consumption = "0", "2"
    else:
        factor, consumption = "1.5", BAD_NUMERICS[bad_class]
    return {"consumption": [{"resource_code": "R1", "consumption": consumption}],
            "notes": [{"id": "N1", "scope_level": "ITEM", "effect_type": "FACTOR",
                       "factor": factor, "target_resource_code": "R1", "source_ref": "TEST"}]}


def _quota_match(bad_class: str):
    return {"boq_item": {"code": "A1", "name": "现浇混凝土矩形柱", "unit": "m3",
                         "quantity": BAD_NUMERICS[bad_class]},
            "quota_items": []}


def _recalc(bad_class: str):
    params = {"L": "1", "H": "0"} if bad_class == "divide_by_zero" else {
        "L": BAD_NUMERICS[bad_class], "H": "1"}
    snapshot = build_snapshot(rule_code=DIV_RULE_CODE, rule_version="1", basis_pack_id="B-V423-NUM",
                              precision_rule_version="P-V1", params=params, unit="unitless")
    return {"basis_pack": _basis(), "snapshot": snapshot, "stored_value": "1"}


# 数值入口：path -> (payload builder, {class: 期望码 or None=由“无除法”证明覆盖})
NUMERIC_ENTRIES = {
    "/v1/quantity/calc": (_qty_calc, {
        "nan": CODE_NOT_FINITE, "infinity": CODE_NOT_FINITE, "negative_infinity": CODE_NOT_FINITE,
        "overflow_1e999": CODE_OVERFLOW, "divide_by_zero": CODE_ZERO_NOT_ALLOWED,
        "precision_1e-30": CODE_PRECISION_EXCEEDED}),
    "/v1/quantity/rule-calc": (_rule_calc, {
        "nan": CODE_NOT_FINITE, "infinity": CODE_NOT_FINITE, "negative_infinity": CODE_NOT_FINITE,
        "overflow_1e999": CODE_OVERFLOW, "divide_by_zero": CODE_DIV_ZERO,
        "precision_1e-30": CODE_PRECISION_EXCEEDED}),
    "/v1/quota/match": (_quota_match, {
        "nan": CODE_NOT_FINITE, "infinity": CODE_NOT_FINITE, "negative_infinity": CODE_NOT_FINITE,
        "overflow_1e999": CODE_OVERFLOW, "divide_by_zero": None,
        "precision_1e-30": CODE_PRECISION_EXCEEDED}),
    "/v1/quota/apply-notes": (_apply_notes, {
        "nan": CODE_NOT_FINITE, "infinity": CODE_NOT_FINITE, "negative_infinity": CODE_NOT_FINITE,
        "overflow_1e999": CODE_OVERFLOW, "divide_by_zero": CODE_ZERO_NOT_ALLOWED,
        "precision_1e-30": CODE_PRECISION_EXCEEDED}),
    "/v1/price/unit": (_price_unit, {
        "nan": CODE_NOT_FINITE, "infinity": CODE_NOT_FINITE, "negative_infinity": CODE_NOT_FINITE,
        "overflow_1e999": CODE_OVERFLOW, "divide_by_zero": CODE_ZERO_NOT_ALLOWED,
        "precision_1e-30": CODE_PRECISION_EXCEEDED}),
    "/v1/price/index-adjust": (_index_adjust, {
        "nan": CODE_NOT_FINITE, "infinity": CODE_NOT_FINITE, "negative_infinity": CODE_NOT_FINITE,
        "overflow_1e999": CODE_OVERFLOW, "divide_by_zero": CODE_DIV_ZERO,
        "precision_1e-30": CODE_PRECISION_EXCEEDED}),
    "/v1/price/project-total": (_project_total, {
        "nan": CODE_NOT_FINITE, "infinity": CODE_NOT_FINITE, "negative_infinity": CODE_NOT_FINITE,
        "overflow_1e999": CODE_OVERFLOW, "divide_by_zero": None,
        "precision_1e-30": CODE_PRECISION_EXCEEDED}),
    "/v1/calc/recalc": (_recalc, {
        "nan": CODE_NOT_FINITE, "infinity": CODE_NOT_FINITE, "negative_infinity": CODE_NOT_FINITE,
        "overflow_1e999": CODE_OVERFLOW, "divide_by_zero": CODE_DIV_ZERO,
        "precision_1e-30": CODE_PRECISION_EXCEEDED}),
}

# 非数值入口：列举理由（门禁/流程/证据/记录类端点不做 Decimal 运算）。
NON_NUMERIC_ENTRIES = {
    "/v1/basis/validate": "依据包结构校验，无数值运算",
    "/v1/rules/validate": "规则自检，入参为空",
    "/v1/job": "创建作业壳，只存 basis_pack JSON",
    "/v1/job/{job_id}/documents": "文件版本登记，只存哈希与版本号",
    "/v1/job/{job_id}/records": "业务记录透传存储，不做数值运算",
    "/internal/job/{job_id}/records": "同上的内部写入通道",
    "/v1/job/{job_id}/tasks": "流程节点创建，payload 透传",
    "/v1/task/{task_id}/start": "任务状态迁移",
    "/v1/task/{task_id}/complete": "任务状态迁移 + 结果透传",
    "/v1/task/{task_id}/approve": "任务签批",
    "/v1/task/{task_id}/fail": "任务失败登记",
    "/v1/task/{task_id}/retry": "任务重试",
    "/v1/gate/check": "门禁读取服务端状态；context 自述被忽略",
    "/v1/jgate/{job_id}/run": "鉴定六门服务端判定，只落 gate_record，不出数",
    "/v1/jgate/{job_id}/draft": "鉴定意见草稿登记，默认 SHADOW 不可签发",
    "/v1/jgate/{job_id}/signoff": "人工签署登记（含门禁快照），不做数值运算",
    "/v1/jgate/{job_id}/issue": "显式出件，需有效人工签署；不做数值运算",
    "/v1/evidence/verify": "证据哈希校验，只比较字符串",
}

# 明确的越界缺口：本任务写入范围不含 core/boq.py。
OUT_OF_SCOPE_ENTRIES = {
    "/v1/boq/import": "BOQ 解析属 P1-09-PARSE / core/boq.py，不在 P0-01-NUMERIC 写入范围；"
                      "本文件只断言非法单元格不得把导入打成 500（缺口登记见 RESULT.md）。",
}

NO_DIVISION_ENTRIES = {
    "/v1/quota/match": quota.match_quota,
    "/v1/price/project-total": pricing.project_total,
}


def test_entry_inventory_covers_every_post_route():
    """入口清单必须与真实路由表一致：新增 POST 端点而不登记会让本用例失败。"""
    declared = set(NUMERIC_ENTRIES) | set(NON_NUMERIC_ENTRIES) | set(OUT_OF_SCOPE_ENTRIES)
    actual = _post_paths()
    assert not (actual - declared), f"发现未登记数值治理的 POST 入口：{sorted(actual - declared)}"
    assert not (declared - actual), f"清单中的入口在 app 中已不存在：{sorted(declared - actual)}"
    assert not (set(NUMERIC_ENTRIES) & set(NON_NUMERIC_ENTRIES))
    assert set(NO_DIVISION_ENTRIES) <= set(NUMERIC_ENTRIES)


def _division_nodes(func) -> int:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return sum(1 for node in ast.walk(tree)
               if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div))


def _assert_no_division_entry(path: str) -> None:
    """不含除法入口的除零类覆盖：AST 证明无除法 + 共享守卫拒绝 0 除数 + 分母有保护。"""
    func = NO_DIVISION_ENTRIES[path]
    assert _division_nodes(func) == 0, f"{path} 的引擎出现了除法，必须补真实除零用例"
    with pytest.raises(NumericError) as exc:
        safe_div(Decimal("1"), Decimal("0"))
    assert exc.value.code == CODE_DIV_ZERO
    assert quota.tag_score([], []) == 0.0, "空标签不得触发除零，也不得返回非零分"


@pytest.mark.parametrize("path", sorted(NUMERIC_ENTRIES))
@pytest.mark.parametrize("bad_class", ALL_CLASSES)
def test_numeric_entry_rejects_each_bad_value(path, bad_class):
    """每个数值入口 × 每类非法数值：422 + NUMERIC_*，绝不 500。"""
    builder, expected_map = NUMERIC_ENTRIES[path]
    expected = expected_map[bad_class]
    if expected is None:
        _assert_no_division_entry(path)
        return
    _assert_numeric_422(client.post(path, json=builder(bad_class)), expected)


def test_quota_tag_score_division_is_guarded():
    """quota 匹配里唯一的除法：分母 max(1, …) 保护，空标签不得除零。"""
    assert _division_nodes(quota.tag_score) == 1, "tag_score 的除法必须显式登记"
    assert quota.tag_score([], []) == 0.0
    assert quota.tag_score(["a", "b"], ["b", "c"]) == pytest.approx(1 / 3)


# ---------------------------------------------------------------- 核心函数矩阵
CORE_ENGINE_CASES = {
    "quantity.volume": lambda bad: quantity.calculate_quantity(
        "RECTANGULAR_VOLUME", {"length": bad, "width": "1", "height": "1"}),
    "quantity.length": lambda bad: quantity.calculate_quantity(
        "LINEAR_LENGTH", {"length": bad, "count": "1"}),
    "quantity.net_value": lambda bad: quantity.calculate_quantity(
        "NET_VALUE", {"gross": bad, "deductions": []}),
    "quota.consumption": lambda bad: quota.apply_quota_notes(
        [{"resource_code": "R1", "consumption": bad}],
        [{"id": "N1", "scope_level": "ITEM", "effect_type": "INFO", "source_ref": "T"}]),
    "quota.factor": lambda bad: quota.apply_quota_notes(
        [{"resource_code": "R1", "consumption": "2"}],
        [{"id": "N1", "scope_level": "ITEM", "effect_type": "FACTOR", "factor": bad,
          "target_resource_code": "R1", "source_ref": "T"}]),
    "pricing.index.base_amount": lambda bad: pricing.index_adjustment(
        bad, "100", "110", "0.05", "FULL_EXCESS"),
    "pricing.index.base_index": lambda bad: pricing.index_adjustment(
        "1000", bad, "110", "0.05", "FULL_EXCESS"),
    "pricing.unit.consumption": lambda bad: pricing.compose_unit_price(
        [{"resource_code": "M1", "resource_type": "MATERIAL", "consumption": bad,
          "unit_price": "10", "price_ref": "T"}], "0.1", "0.05",
        management_source_ref="T", profit_source_ref="T"),
    "pricing.unit.rate": lambda bad: pricing.compose_unit_price(
        [{"resource_code": "M1", "resource_type": "MATERIAL", "consumption": "2",
          "unit_price": "10", "price_ref": "T"}], bad, "0.05",
        management_source_ref="T", profit_source_ref="T"),
    "precision.money": lambda bad: precision.round_money(Decimal(bad)),
    "precision.quantity": lambda bad: precision.round_quantity(Decimal(bad), "m3"),
    "precision.intermediate": lambda bad: precision.round_intermediate(Decimal(bad)),
    "precision.compare_sum": lambda bad: precision.compare_sum(Decimal(bad), Decimal("1")),
}


@pytest.mark.parametrize("engine", sorted(CORE_ENGINE_CASES))
@pytest.mark.parametrize("bad_class", sorted(BAD_NUMERICS))
def test_core_engine_rejects_bad_values(engine, bad_class):
    """核心计算函数直接注入非法值：必须抛 NumericError(NUMERIC_*)，不得泄漏 Decimal 异常。"""
    func = CORE_ENGINE_CASES[engine]
    with pytest.raises(NumericError) as exc:
        func(BAD_NUMERICS[bad_class])
    assert exc.value.code in NUMERIC_CODES, f"{engine} 错误码不在 NUMERIC_* 族：{exc.value.code}"


def test_core_to_decimal_never_silently_truncates():
    """1e-30 / 1e999 不得被静默当成 0 或大数。"""
    with pytest.raises(NumericError) as exc:
        to_decimal("1e-30", "x")
    assert exc.value.code == CODE_PRECISION_EXCEEDED
    with pytest.raises(NumericError) as exc:
        to_decimal("1e999", "x")
    assert exc.value.code == CODE_OVERFLOW
    for value in ("NaN", "Infinity", "-Infinity", float("nan"), float("inf"), float("-inf")):
        with pytest.raises(NumericError) as exc:
            to_decimal(value, "x")
        assert exc.value.code == CODE_NOT_FINITE
    assert to_decimal("0.000000000001", "x") == Decimal("1E-12")


def test_core_ensure_finite_blocks_overflow_and_underflow():
    with pytest.raises(NumericError) as exc:
        ensure_finite(Decimal("1E+24"), "result")
    assert exc.value.code == CODE_OVERFLOW
    with pytest.raises(NumericError) as exc:
        ensure_finite(Decimal("1E-30"), "result")
    assert exc.value.code == CODE_PRECISION_EXCEEDED
    with pytest.raises(NumericError) as exc:
        ensure_finite(Decimal("NaN"), "result")
    assert exc.value.code == CODE_NOT_FINITE
    assert ensure_finite(Decimal("0.000000000001"), "result") == Decimal("1E-12")


def test_core_safe_div():
    with pytest.raises(NumericError) as exc:
        safe_div(Decimal("1"), Decimal("0"))
    assert exc.value.code == CODE_DIV_ZERO
    assert safe_div(Decimal("10"), Decimal("4")) == Decimal("2.5")
    with pytest.raises(NumericError) as exc:
        safe_div(Decimal("1"), "NaN")
    assert exc.value.code == CODE_NOT_FINITE


def test_zero_operand_guards():
    with pytest.raises(NumericError) as exc:
        quantity.calculate_quantity("RECTANGULAR_AREA", {"length": "0", "width": "1"})
    assert exc.value.code == CODE_ZERO_NOT_ALLOWED
    with pytest.raises(NumericError) as exc:
        pricing.compose_unit_price(
            [{"resource_code": "M1", "resource_type": "MATERIAL", "consumption": "0",
              "unit_price": "10", "price_ref": "T"}], "0.1", "0.05",
            management_source_ref="T", profit_source_ref="T")
    assert exc.value.code == CODE_ZERO_NOT_ALLOWED
    with pytest.raises(NumericError) as exc:
        quota.apply_quota_notes([{"resource_code": "R1", "consumption": "2"}],
                                [{"id": "N1", "scope_level": "ITEM", "effect_type": "FACTOR",
                                  "factor": "0", "target_resource_code": "R1", "source_ref": "T"}])
    assert exc.value.code == CODE_ZERO_NOT_ALLOWED
    with pytest.raises(NumericError) as exc:
        pricing.index_adjustment("1000", "0", "110", "0.05", "FULL_EXCESS")
    assert exc.value.code == CODE_DIV_ZERO


def test_rate_out_of_range_is_rejected():
    with pytest.raises(NumericError) as exc:
        pricing.index_adjustment("1000", "100", "110", "1.5", "FULL_EXCESS")
    assert exc.value.code == numeric.CODE_OUT_OF_RANGE
    with pytest.raises(NumericError) as exc:
        pricing.compose_unit_price(
            [{"resource_code": "M1", "resource_type": "MATERIAL", "consumption": "2",
              "unit_price": "10", "price_ref": "T"}], "1.5", "0.05",
            management_source_ref="T", profit_source_ref="T")
    assert exc.value.code == numeric.CODE_OUT_OF_RANGE


def test_negative_values_are_rejected_where_business_forbids_them():
    with pytest.raises(NumericError) as exc:
        pricing.index_adjustment("1000", "-100", "110", "0.05", "FULL_EXCESS")
    assert exc.value.code == CODE_NEGATIVE
    with pytest.raises(NumericError) as exc:
        to_decimal("-1", "amount")
    assert exc.value.code == CODE_NEGATIVE
    with pytest.raises(NumericError):
        quantity.calculate_quantity("NET_VALUE", {"gross": "1", "deductions": ["2"]})


def test_results_may_not_overflow_silently():
    """1e12 × 1e12 这类结果越界必须在核心层被拒绝，而不是原样返回。"""
    with pytest.raises(NumericError) as exc:
        quantity.calculate_quantity("RECTANGULAR_AREA", {"length": "1e12", "width": "1e12"})
    assert exc.value.code == CODE_OVERFLOW
    with pytest.raises(NumericError) as exc:
        pricing.compose_unit_price(
            [{"resource_code": "M1", "resource_type": "MATERIAL", "consumption": "1e12",
              "unit_price": "1e12", "price_ref": "T"}], "0.1", "0.05",
            management_source_ref="T", profit_source_ref="T")
    assert exc.value.code == CODE_OVERFLOW


# ---------------------------------------------------------------- rules 引擎
def test_rules_engine_rejects_bad_params():
    reg = RuleRegistry(RULES_PATH)
    for bad in ("NaN", "Infinity", "-Infinity", "1e999"):
        with pytest.raises(NumericError) as exc:
            reg.execute("QTY.BOQ.CONC.COLUMN.001", {"b": bad, "h": "1", "H": "1"})
        assert exc.value.code in NUMERIC_CODES
    with pytest.raises(NumericError) as exc:
        reg.execute("QTY.BOQ.CONC.COLUMN.001", {"b": "1e-30", "h": "1", "H": "1"})
    assert exc.value.code == CODE_PRECISION_EXCEEDED


def test_rules_expression_division_by_zero_maps_to_numeric_code():
    """表达式除零在进程内保留 CALC_DIV_ZERO（既有调用方契约），对外统一迁移 NUMERIC_DIV_ZERO。"""
    reg = RuleRegistry()
    reg.register({"rule_code": "T.DIV", "version": "1", "unit": "unitless", "expression": "L / 0",
                  "variables": {"L": "unitless"}, "standard_ref": "TEST", "license_status": "TEST_ONLY"})
    with pytest.raises(RuleError) as exc:
        reg.execute("T.DIV", {"L": "1"})
    assert exc.value.code == "CALC_DIV_ZERO"
    assert numeric.unified_code(exc.value.code) == CODE_DIV_ZERO


def test_rules_zero_to_negative_power_is_division_by_zero():
    reg = RuleRegistry()
    reg.register({"rule_code": "T.POW", "version": "1", "unit": "unitless", "expression": "L ** -1",
                  "variables": {"L": "unitless"}, "standard_ref": "TEST", "license_status": "TEST_ONLY"})
    with pytest.raises(RuleError) as exc:
        reg.execute("T.POW", {"L": "0"})
    assert numeric.unified_code(exc.value.code) == CODE_DIV_ZERO


def test_rules_round_places_out_of_precision_is_rejected():
    reg = RuleRegistry()
    reg.register({"rule_code": "T.ROUND", "version": "1", "unit": "unitless",
                  "expression": "ROUND(L, 30)", "variables": {"L": "unitless"},
                  "standard_ref": "TEST", "license_status": "TEST_ONLY", "decimals": 6})
    with pytest.raises(NumericError) as exc:
        reg.execute("T.ROUND", {"L": "1.5"})
    assert exc.value.code == CODE_PRECISION_EXCEEDED


def test_rules_rule_decimals_out_of_range_is_rejected():
    reg = RuleRegistry()
    reg.register({"rule_code": "T.DEC", "version": "1", "unit": "unitless", "expression": "L",
                  "variables": {"L": "unitless"}, "standard_ref": "TEST",
                  "license_status": "TEST_ONLY", "decimals": 99})
    with pytest.raises(NumericError) as exc:
        reg.execute("T.DEC", {"L": "1.5"})
    assert exc.value.code == CODE_PRECISION_EXCEEDED


def test_rules_tiny_result_is_not_truncated_to_zero():
    reg = RuleRegistry()
    reg.register({"rule_code": "T.TINY", "version": "1", "unit": "unitless", "expression": "L / H",
                  "variables": {"L": "unitless", "H": "unitless"}, "standard_ref": "TEST",
                  "license_status": "TEST_ONLY", "decimals": 6})
    with pytest.raises(NumericError) as exc:
        reg.execute("T.TINY", {"L": "1e-12", "H": "1e12"})
    assert exc.value.code == CODE_PRECISION_EXCEEDED


# ---------------------------------------------------------------- 正常值回归（守卫不误伤）
def test_valid_core_values_still_compute():
    assert quantity.calculate_quantity(
        "RECTANGULAR_VOLUME", {"length": "0.5", "width": "0.5", "height": "3"})["value"] == Decimal("0.750")
    assert precision.round_quantity(Decimal("1") / Decimal("3"), "m3") == Decimal("0.333")
    assert precision.round_money(Decimal("10") / Decimal("3")) == Decimal("3.33")
    assert precision.compare_sum(Decimal("100.00"), Decimal("100.01"))["consistent"] is True
    assert precision.compare_sum(Decimal("100.00"), Decimal("100.02"))["consistent"] is False
    assert pricing.index_adjustment("1000000", "100", "110", "0.05",
                                    "FULL_EXCESS")["delta_amount"] == Decimal("100000")
    assert quota.apply_quota_notes(
        [{"resource_code": "R1", "consumption": "2"}],
        [{"id": "N1", "scope_level": "ITEM", "effect_type": "FACTOR", "factor": "1.5",
          "target_resource_code": "R1", "source_ref": "TEST_ONLY"}])["consumption"][0]["consumption"] == "3.0"
    match = quota.match_quota(
        {"code": "A1", "name": "现浇混凝土矩形柱", "unit": "m3", "major": "BUILDING", "quantity": "3.5"},
        [{"id": "Q1", "item_code": "Q1", "mapped_boq_code": "A1", "name": "现浇混凝土矩形柱",
          "unit": "m3", "major": "BUILDING", "work_tags": ["混凝土", "柱"],
          "source_ref": "TEST_ONLY", "license_status": "TEST_ONLY", "status": "ACTIVE"}],
        {"A1": "Q1"})
    assert match["recommended_id"] == "Q1" and match["need_human"] is True


def test_valid_http_paths_still_return_200():
    ok_qty = client.post("/v1/quantity/calc", json={
        "basis_pack": _basis(), "operation": "RECTANGULAR_VOLUME",
        "params": {"length": "0.5", "width": "0.5", "height": "3"}, "unit": "m3",
        "rule_code": "R", "rule_version": "1", "standard_ref": "TEST"})
    assert ok_qty.status_code == 200 and ok_qty.json()["result"] == "0.75"

    ok_rule = client.post("/v1/quantity/rule-calc", json={
        "basis_pack": _basis(), "rule_code": "QTY.BOQ.CONC.COLUMN.001",
        "params": {"b": "0.5", "h": "0.5", "H": "3"}})
    assert ok_rule.status_code == 200 and ok_rule.json()["result"] == "0.750"

    ok_price = client.post("/v1/price/unit", json={
        "resources": [{"resource_code": "M1", "resource_type": "MATERIAL", "consumption": "2",
                       "unit_price": "10", "price_ref": "TEST"}],
        "management_rate": "0.1", "profit_rate": "0.05",
        "management_source_ref": "TEST-MGMT", "profit_source_ref": "TEST-PROFIT"})
    assert ok_price.status_code == 200 and Decimal(ok_price.json()["unit_price"]) == Decimal("23.10")


# ---------------------------------------------------------------- recalc 专项
def test_recalc_bad_stored_value_returns_422_not_500():
    snapshot = build_snapshot(rule_code="QTY.BOQ.CONC.COLUMN.001", rule_version="2026.1",
                              basis_pack_id="B-V423-NUM", precision_rule_version="P-V1",
                              params={"b": "0.5", "h": "0.5", "H": "3"}, unit="m3")
    for bad in ("NaN", "Infinity", "-Infinity", "1e999", "1e-30"):
        _assert_numeric_422(client.post("/v1/calc/recalc", json={
            "basis_pack": _basis(), "snapshot": snapshot, "stored_value": bad}))


def test_recalc_unknown_rule_is_business_422_not_500():
    snapshot = build_snapshot(rule_code="NO.SUCH.RULE", rule_version="1", basis_pack_id="B-V423-NUM",
                              precision_rule_version="P-V1", params={"x": "1"}, unit="unitless")
    response = client.post("/v1/calc/recalc",
                           json={"basis_pack": _basis(), "snapshot": snapshot, "stored_value": "1"})
    assert response.status_code != 500
    assert response.status_code == 422 and _code(response) == "CALC_RULE_NOT_FOUND"


# ---------------------------------------------------------------- 缺口：BOQ 导入
def test_boq_import_never_500s_on_bad_numeric_cells():
    """BOQ 解析不在本任务写入范围：只保证非法单元格不会把导入打成 500（缺口见 RESULT.md）。"""
    for bad in ("NaN", "Infinity", "1e999"):
        wb = Workbook(); ws = wb.active
        ws.append(["项目编码", "项目名称", "项目特征", "计量单位", "工程量", "分部"])
        ws.append(["A1", "现浇混凝土柱", "C30", "m3", bad, "混凝土工程"])
        buf = io.BytesIO(); wb.save(buf)
        response = client.post("/v1/boq/import", files={"file": ("a.xlsx", buf.getvalue(), "x")})
        assert response.status_code != 500, f"BOQ 导入遇到 {bad} 不得 500：{response.text[:300]}"


# ---------------------------------------------------------------- goldenset（fail-closed）
def test_goldenset_has_no_http_numeric_entry():
    """goldenset 的数值只来自 Excel，没有 HTTP 计算入口；任何结果都不得 formal_ready。"""
    paths = {route.path for route in app.routes}
    assert not [path for path in paths if "golden" in path.lower()], "goldenset 不应有对外数值入口"


def _write_goldenset(root, bad_value: str):
    workdir = root / "11_真实GoldenSet对照"
    workdir.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    anchors = wb.active
    anchors.title = gs_module.ANCHOR_SHEET
    anchors.append(["序号", "项目", "金额"])
    anchors.append([1, "招标控制价合计", bad_value])
    anchors.append([2, "分部分项工程费", "100"])
    rows = wb.create_sheet(gs_module.ROWS_SHEET)
    rows.append(["项目编码", "项目名称", "原始工程量"])
    rows.append(["A1", "现浇混凝土柱", "1"])
    wb.save(workdir / "真实工程量及计价对照表.xlsx")


def test_goldenset_fails_closed_on_bad_numeric_cells(tmp_path):
    """锚点单元格写入 NaN/Infinity/1e999：不得当成控制价、不得 ok=true、不得 formal_ready=true。"""
    for bad in ("NaN", "Infinity", "1e999"):
        root = tmp_path / f"GoldenSet_{bad.replace('-', 'n')}"
        _write_goldenset(root, bad)
        loaded = gs_module.load(root)
        assert loaded.formal_ready is False
        assert loaded.manifest()["formal_ready"] is False
        total = loaded.anchors.get("招标控制价合计")
        assert not isinstance(total, Decimal) or not total.is_finite() or total != gs_module.CONTROL_TOTAL, \
            f"{bad} 不得被当成控制价合计"
        try:
            report = gs_module.validate(loaded)
        except DecimalException:
            report = {"ok": False, "formal_ready": False}   # 抛错也是 fail-closed（见 RESULT.md 缺口）
        assert report["ok"] is False, f"{bad} 不得让 goldenset 校验通过"
        assert report["formal_ready"] is False
