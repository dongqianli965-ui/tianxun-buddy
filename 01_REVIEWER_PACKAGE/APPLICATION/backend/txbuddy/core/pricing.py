"""组价引擎（price 对外入口）：调差 / 综合单价 / 项目总价。

P0-01-NUMERIC：
- 金额、指数、费率、消耗量、单价全部先过 core/numeric 守卫（NaN/Infinity/1e999/超精度 → NUMERIC_*）；
- 费率（管理费/利润/税率/风险费率）限制在 [0, 1]，越界即 NUMERIC_OUT_OF_RANGE；
- 除零（调差基数指数为 0）返回 NUMERIC_DIV_ZERO，不得抛 Decimal DivisionByZero；
- 所有结果过量级守卫，不得静默返回 Infinity / 1e999。
"""
from decimal import Decimal

from .numeric import (CODE_DIV_ZERO, NumericError, decimal_guard, ensure_finite, to_decimal,
                      to_rate)
from ..models import BasisPack, FeeItem


def D(v, field: str = "value"): return to_decimal(v, field, allow_negative=True)


def index_adjustment(base_amount, base_index, current_index, risk_rate, mode):
    base_amount = to_decimal(base_amount, 'base_amount')
    base_index = to_decimal(base_index, 'base_index')          # 负数已在此拒绝
    current_index = to_decimal(current_index, 'current_index')
    risk = to_rate(risk_rate, 'risk_rate')
    if base_index == 0:
        raise NumericError(CODE_DIV_ZERO, "基期指数为 0，调差比值无法计算（除零）", 'base_index')
    with decimal_guard("index_adjustment"):
        change = ensure_finite(current_index / base_index - Decimal(1), "change_ratio")
        if mode == "WITHIN_BAND_ONLY":
            delta = base_amount * (change - risk) if change > risk else (base_amount * (change + risk) if change < -risk else Decimal(0))
        elif mode == "FULL_EXCESS":
            delta = base_amount * change if abs(change) > risk else Decimal(0)
        elif mode == "PASS_THROUGH":
            delta = base_amount * change
        else:
            raise ValueError("未知 risk_bear_mode")
    return {"change_ratio": change, "delta_amount": ensure_finite(delta, "delta_amount"), "mode": mode}


def fee_base(base_def, boq, measure, other):
    if base_def == "BOQ_TOTAL": return boq
    if base_def == "BOQ_PLUS_MEASURES": return boq + measure
    if base_def == "PRE_TAX_BEFORE_FEES": return boq + measure + other
    raise ValueError(f"未知 base_def：{base_def}")


def project_total(basis_pack: BasisPack, boq_total, measure_total, other_total, fee_items: list[FeeItem], safety_production_fee: FeeItem | None, tax_rate, formal_calculation=True):
    fee_items = list(fee_items or [])
    boq = to_decimal(boq_total, 'boq_total')
    measure = to_decimal(measure_total, 'measure_total')
    other = to_decimal(other_total, 'other_total')
    tax = to_rate(tax_rate, 'tax_rate')
    if basis_pack.rule_version_scope == "HENAN_2025":
        if any(x.fee_item.upper() == "REGULATORY_FEE" for x in fee_items): raise ValueError("HENAN_2025 不得单列规费")
        if safety_production_fee is None and formal_calculation: raise ValueError("HENAN_2025 正式计价必须明确安全生产措施费")
        if safety_production_fee is not None:
            if safety_production_fee.fee_item.upper() != "SAFETY_PRODUCTION_FEE": raise ValueError("安全生产措施费字段名称不正确")
            if not safety_production_fee.is_noncompetitive: raise ValueError("安全生产措施费必须标记为不可竞争")
            if safety_production_fee.amount is None: raise ValueError("安全生产措施费必须有核验金额")
    pre = ensure_finite(boq + measure + other, "pre_tax")
    fee_total = Decimal(0); fee_results = []
    for f in fee_items:
        if f.amount is not None:
            amount = to_decimal(f.amount, f"fee_items.{f.fee_item}.amount")
        else:
            if f.rate is None or f.base_def is None: raise ValueError(f"费项 {f.fee_item} 缺金额/费率基数")
            amount = to_rate(f.rate, f"fee_items.{f.fee_item}.rate") * fee_base(f.base_def, boq, measure, other)
        amount = ensure_finite(amount, f"fee_items.{f.fee_item}.amount")
        fee_total = ensure_finite(fee_total + amount, "fees_total"); fee_results.append({"fee_item": f.fee_item, "amount": amount, "source_ref": f.source_ref})
    safety = to_decimal(safety_production_fee.amount, "safety_production_fee.amount") if safety_production_fee and safety_production_fee.amount is not None else Decimal(0)
    tax_base = ensure_finite(pre + fee_total + safety, "tax_base")
    with decimal_guard("tax_amount"):
        tax_amount = ensure_finite(tax_base * tax, "tax_amount")
    return {"boq_total": boq, "measure_total": measure, "other_total": other, "fee_items": fee_results, "fees_total": fee_total, "safety_production_fee": safety, "tax_base": tax_base, "tax_rate": tax, "tax_amount": tax_amount, "project_total": ensure_finite(tax_base + tax_amount, "project_total"), "rule_version_scope": basis_pack.rule_version_scope, "warning": "不内置河南费率；费率必须来自已核验依据包。"}


def compose_unit_price(resources: list[dict], management_rate, profit_rate, management_base="DIRECT_COST", profit_base="DIRECT_COST_PLUS_MANAGEMENT", explicit_risk_fee=None, adjusted_resource_codes=None, risk_resource_codes=None, management_source_ref=None, profit_source_ref=None) -> dict:
    """资源消耗×单价形成直接费；管理费/利润/风险费率必须由依据包或合同显式提供。"""
    management_rate = to_rate(management_rate, 'management_rate')
    profit_rate = to_rate(profit_rate, 'profit_rate')
    if not management_source_ref or not profit_source_ref:
        raise ValueError("管理费和利润必须有明确依据来源")
    adjusted_resource_codes = set(adjusted_resource_codes or [])
    risk_resource_codes = set(risk_resource_codes or [])
    if adjusted_resource_codes & risk_resource_codes:
        raise ValueError("已调差资源不得重复计入风险费")
    buckets = {"LABOR": Decimal(0), "MATERIAL": Decimal(0), "MACHINE": Decimal(0)}
    detail = []
    for index, item in enumerate(resources or []):
        kind = item.get("resource_type")
        if kind not in buckets: raise ValueError(f"未知资源类型：{kind}")
        if not item.get("price_ref"): raise ValueError("资源单价缺少来源")
        qty = to_decimal(item.get("consumption"), f"resources[{index}].consumption",
                         allow_negative=False, allow_zero=False)
        price = to_decimal(item.get("unit_price"), f"resources[{index}].unit_price", allow_negative=False)
        amount = ensure_finite(qty * price, f"resources[{index}].amount")
        buckets[kind] = ensure_finite(buckets[kind] + amount, "direct_cost")
        detail.append({"resource_code": item.get("resource_code"), "resource_type": kind, "consumption": qty, "unit_price": price, "amount": amount, "price_ref": item["price_ref"]})
    direct = ensure_finite(buckets["LABOR"] + buckets["MATERIAL"] + buckets["MACHINE"], "direct_cost")
    mr, pr = to_rate(management_rate, "management_rate"), to_rate(profit_rate, "profit_rate")
    if management_base != "DIRECT_COST": raise ValueError("当前原型只支持显式 DIRECT_COST 管理费基数")
    management = ensure_finite(direct * mr, "management")
    if profit_base == "DIRECT_COST_PLUS_MANAGEMENT": base_profit = ensure_finite(direct + management, "profit_base")
    elif profit_base == "DIRECT_COST": base_profit = direct
    else: raise ValueError("未知利润基数")
    profit = ensure_finite(base_profit * pr, "profit")
    risk_fee = Decimal(0)
    if explicit_risk_fee is not None:
        if not explicit_risk_fee.get("contract_clause_ref") or not explicit_risk_fee.get("source_ref"):
            raise ValueError("显式风险费必须有合同条款和依据来源")
        rate = to_rate(explicit_risk_fee.get("rate"), "explicit_risk_fee.rate")
        cap = to_decimal(explicit_risk_fee.get("cap"), "explicit_risk_fee.cap")
        risk_base = ensure_finite(sum((x["amount"] for x in detail if x.get("resource_code") in risk_resource_codes), Decimal(0)), "risk_base")
        risk_fee = ensure_finite(min(risk_base * rate, cap), "risk_fee")
    total = ensure_finite(direct + management + profit + risk_fee, "unit_price")
    return {"labor": buckets["LABOR"], "material": buckets["MATERIAL"], "machine": buckets["MACHINE"], "direct_cost": direct, "management": management, "profit": profit, "risk_fee": risk_fee, "unit_price": total, "resource_detail": detail, "management_source_ref": management_source_ref, "profit_source_ref": profit_source_ref}
