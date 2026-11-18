"""固定算量算子（qty 引擎对外入口）。

P0-01-NUMERIC：
- 所有入参先过 core/numeric.to_decimal（NaN/Infinity/1e999/超精度 一律 NUMERIC_* 422）；
- 所有结果过量级守卫，1e12 × 1e12 这类越界不得静默返回；
- 零尺寸（length/width/height/count = 0）视为非法，避免把“除零/零比例”当成合法 0 工程量。
"""
from decimal import Decimal

from .numeric import (CODE_INVALID, CODE_NEGATIVE, NumericError, ensure_finite,
                      finite_product, to_decimal)


def D(value, field: str = "value") -> Decimal:
    """兼容旧调用：任意入参 -> 合法 Decimal（允许负数，禁止 NaN/Infinity/越界）。"""
    return to_decimal(value, field, allow_negative=True)


def positive(params, key):
    if key not in params:
        raise NumericError(CODE_INVALID, f"缺少算量参数：{key}", key)
    return to_decimal(params[key], key, allow_negative=False, allow_zero=False)


def _list_of_decimal(params, key):
    raw = params.get(key, [])
    if not isinstance(raw, (list, tuple)):
        raise NumericError(CODE_INVALID, f"{key} 必须是数组", key)
    values = [to_decimal(item, f"{key}[{index}]", allow_negative=False)
              for index, item in enumerate(raw)]
    return values


def calculate_quantity(operation, params):
    if operation == "RECTANGULAR_AREA":
        a, b = positive(params, "length"), positive(params, "width")
        value = ensure_finite(a * b, "area")
        return {"value": value, "formula": "length * width", "intermediate_values": {"length": a, "width": b, "gross_area": value}}
    if operation == "RECTANGULAR_VOLUME":
        a, b, c = positive(params, "length"), positive(params, "width"), positive(params, "height")
        gross = ensure_finite(finite_product(a, b, c, field="gross_volume"), "gross_volume")
        ds, adds = _list_of_decimal(params, "deductions"), _list_of_decimal(params, "additions")
        dt, at = sum(ds, Decimal(0)), sum(adds, Decimal(0))
        value = ensure_finite(gross - dt + at, "net_volume")
        if value < 0:
            raise NumericError(CODE_NEGATIVE, "扣减后工程量不能为负数", "net_volume")
        return {"value": value, "formula": "length * width * height - SUM(deductions) + SUM(additions)",
                "intermediate_values": {"length": a, "width": b, "height": c, "gross_volume": gross, "deductions": ds, "deduction_total": dt, "additions": adds, "addition_total": at, "net_volume": value}}
    if operation == "LINEAR_LENGTH":
        length = positive(params, "length")
        count = to_decimal(params.get("count", 1), "count", allow_negative=False, allow_zero=False)
        value = ensure_finite(length * count, "total_length")
        return {"value": value, "formula": "length * count", "intermediate_values": {"length": length, "count": count, "total_length": value}}
    if operation == "NET_VALUE":
        gross = to_decimal(params.get("gross"), "gross", allow_negative=False)
        ds, adds = _list_of_decimal(params, "deductions"), _list_of_decimal(params, "additions")
        value = ensure_finite(gross - sum(ds, Decimal(0)) + sum(adds, Decimal(0)), "net")
        if value < 0:
            raise NumericError(CODE_NEGATIVE, "净工程量不能为负数", "net")
        return {"value": value, "formula": "gross - SUM(deductions) + SUM(additions)", "intermediate_values": {"gross": gross, "deductions": ds, "additions": adds, "net": value}}
    raise ValueError(f"不支持的算量操作：{operation}")
