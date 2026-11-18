"""TX-008 专业精度体系（V3.1 A-40）。

- 中间值 8 位不中断；输出点按 professional precision_rule 舍入。
- 金额 2 位；工程量按专业/单位配置。
- 同一报表 round_stage 必须一致（ITEM 或 SUM，由调用方显式声明并校验）。
- 精度配置带版本，历史计算记录 precision_rule_version。
- 汇总校验容差：绝对 0.01 元 / 相对 1e-6（与 core/calcrun.py 共用同一常量来源）。
- P0-01-NUMERIC：所有舍入入口先过 core/numeric 守卫；NaN/Infinity/1e999/超精度
  一律抛 NumericError(NUMERIC_*)，不得静默截断，也不得让 decimal.InvalidOperation 逃逸成 500。
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .numeric import (CODE_PRECISION_EXCEEDED, NumericError, decimal_guard, ensure_decimal,
                      ensure_finite, to_decimal)

INTERMEDIATE_QUANT = Decimal("0.00000001")   # 8 位
MONEY_QUANT = Decimal("0.01")
PRECISION_VERSION = "P-V1"
TOLERANCE_ABS = Decimal("0.01")
TOLERANCE_REL = Decimal("0.000001")
MAX_OUTPUT_DECIMALS = 12                       # 输出小数位上限，超出即视为非法精度要求

UNIT_DECIMALS = {"m3": 3, "m2": 2, "m": 2, "kg": 3, "t": 3, "个": 0, "台": 0, "套": 0, "樘": 0, "项": 0}
MAJOR_OVERRIDES = {
    "BUILDING": {},
    "INSTALLATION": {"m": 2, "m2": 2},
    "MUNICIPAL": {},
}
ROUND_STAGES = {"ITEM", "SUM"}


def _guarded(value, field: str) -> Decimal:
    """舍入前的数值守卫：必须是有限 Decimal，且在可表示量级内。"""
    return ensure_finite(ensure_decimal(value, field), field)


def decimals_for(unit: str, major: str = "BUILDING") -> int:
    overrides = MAJOR_OVERRIDES.get(major, {})
    return overrides.get(unit, UNIT_DECIMALS.get(unit, 3))


def round_intermediate(value: Decimal) -> Decimal:
    target = _guarded(value, "round_intermediate")
    with decimal_guard("round_intermediate"):
        return target.quantize(INTERMEDIATE_QUANT, rounding=ROUND_HALF_UP)


def round_quantity(value: Decimal, unit: str, major: str = "BUILDING") -> Decimal:
    decimals = decimals_for(unit, major)
    if decimals < 0 or decimals > MAX_OUTPUT_DECIMALS:
        raise NumericError(CODE_PRECISION_EXCEEDED,
                           f"工程量小数位配置越界（0..{MAX_OUTPUT_DECIMALS}）：{decimals}", "decimals")
    target = _guarded(value, "round_quantity")
    with decimal_guard("round_quantity"):
        return target.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


def round_money(value: Decimal) -> Decimal:
    target = _guarded(value, "round_money")
    with decimal_guard("round_money"):
        return target.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def assert_round_stage(stage: str) -> str:
    if stage not in ROUND_STAGES:
        raise ValueError(f"round_stage 非法：{stage}，只允许 {sorted(ROUND_STAGES)}")
    return stage


def compare_sum(a, b) -> dict:
    left = to_decimal(a, "compare_sum.a", allow_negative=True)
    right = to_decimal(b, "compare_sum.b", allow_negative=True)
    with decimal_guard("compare_sum"):
        delta = left - right
        abs_d = abs(delta)
        rel_d = abs_d / (abs(left) if left else Decimal(1))
        ok = abs_d <= TOLERANCE_ABS or rel_d <= TOLERANCE_REL
        return {"consistent": ok, "delta": str(delta), "delta_abs": str(abs_d),
                "delta_rel": str(rel_d.quantize(Decimal("1e-9"))),
                "tolerance_abs": str(TOLERANCE_ABS), "tolerance_rel": str(TOLERANCE_REL),
                "reason": "OK" if ok else "GATE_QTY_RECONCILE_FAILED"}


def describe() -> dict:
    return {"precision_rule_version": PRECISION_VERSION, "intermediate_digits": 8,
            "money_quant": str(MONEY_QUANT), "unit_decimals": UNIT_DECIMALS,
            "major_overrides": MAJOR_OVERRIDES, "tolerance_abs": str(TOLERANCE_ABS),
            "tolerance_rel": str(TOLERANCE_REL), "round_stages": sorted(ROUND_STAGES)}
