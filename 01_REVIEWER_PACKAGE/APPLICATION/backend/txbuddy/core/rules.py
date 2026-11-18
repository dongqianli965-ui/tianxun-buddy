"""受限规则注册表与表达式执行（QEL 子集）。

设计约束（对应 V3.1 裁定 A-8/A-40 与 docs/02_架构.md「不执行任意 eval」）：
1. 不使用 eval/exec；表达式经 ast.parse 后按白名单节点遍历执行。
2. 只允许：数字常量、绑定变量、+ - * / **（乘方）、一元正负、括号、白名单函数。
   注意：表达式按 Python 语法解析，乘方必须写 **；^ 是位异或，一律拒绝（仅需量纲乘方，不需要位运算）。
3. 单位随变量绑定，按量纲代数推导；结果量纲必须等于规则声明单位，否则报错。
4. 规则必须带 source_ref 与 license_status；TEST_ONLY 规则只能产出测试结果。
5. 每条规则可自带 tests，注册表提供自检（validate_registry），未通过自检不得上线。
"""
from __future__ import annotations

import ast
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, getcontext
from .numeric import (CODE_PRECISION_EXCEEDED, MAX_EXP, NumericError, decimal_guard,
                      ensure_finite, to_decimal)
from pathlib import Path
from typing import Any

getcontext().prec = 28

# 规则表达式的数值失败在进程内保留 CALC_* 语义码（既有调用方依赖），
# 对外 HTTP 契约由 core/numeric.unified_code 统一迁移到 NUMERIC_*。
CODE_RULE_DIV_ZERO = "CALC_DIV_ZERO"

DEFAULT_DECIMALS = {"m3": 3, "m2": 2, "m": 2, "kg": 3, "t": 3, "unitless": 6}
ALLOWED_FUNCS = {"MIN", "MAX", "ABS", "ROUND", "CEIL", "FLOOR"}
UNIT_TOKENS = {"m": {"m": 1}, "m2": {"m": 2}, "m3": {"m": 3}, "kg": {"kg": 1},
               "t": {"t": 1}, "unitless": {}, "": {}}


class RuleError(ValueError):
    """规则或表达式错误（对外映射为业务错误码）。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def parse_unit(unit: str) -> dict[str, int]:
    """把单位字符串解析为量纲字典，例如 m3 -> {'m':3}，kg/m -> {'kg':1,'m':-1}。"""
    unit = (unit or "").strip()
    if unit in UNIT_TOKENS:
        return dict(UNIT_TOKENS[unit])
    if "/" in unit:
        num, den = unit.split("/", 1)
        top, bottom = parse_unit(num), parse_unit(den)
        out = dict(top)
        for k, v in bottom.items():
            out[k] = out.get(k, 0) - v
        return {k: v for k, v in out.items() if v}
    raise RuleError("CALC_UNIT_MISMATCH", f"不支持的单位：{unit!r}")


def unit_str(dims: dict[str, int]) -> str:
    if not dims:
        return "unitless"
    pos = "".join(k + str(v) for k, v in sorted(dims.items()) if v > 0)
    neg = "".join(k + str(-v) for k, v in sorted(dims.items()) if v < 0)
    return pos + ("/" + neg if neg else "")


def _combine(a: dict[str, int], b: dict[str, int], sign: int) -> dict[str, int]:
    out = dict(a)
    for k, v in b.items():
        out[k] = out.get(k, 0) + sign * v
    return {k: v for k, v in out.items() if v}


def _dec(value: Any, code: str = "CALC_PARAM_MISSING") -> Decimal:
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise RuleError(code, f"非法数值：{value!r}")


class Evaluator:
    """在白名单 AST 节点上执行 Decimal 运算，同时记录中间值与量纲。"""

    def __init__(self, variables: dict[str, tuple[Decimal, str]]):
        self.vars = variables
        self.trace: list[dict[str, Any]] = []
        self._seq = 0

    # --- public ---
    def run(self, expression: str, expected_unit: str) -> tuple[Decimal, list[dict[str, Any]]]:
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise RuleError("CALC_EXPR_SYNTAX", f"表达式语法错误：{exc.msg}")
        value, dims = self._eval(tree.body)
        actual = unit_str(dims)
        expected = unit_str(parse_unit(expected_unit))
        if actual != expected:
            raise RuleError(
                "CALC_UNIT_MISMATCH",
                f"表达式量纲 {actual} 与规则声明单位 {expected} 不一致",
            )
        return value, self.trace

    # --- internals ---
    def _record(self, expr: str, value: Decimal, dims: dict[str, int]) -> None:
        self._seq += 1
        self.trace.append({"path": str(self._seq), "expr": expr, "value": str(value), "unit": unit_str(dims)})

    def _eval(self, node: ast.AST) -> tuple[Decimal, dict[str, int]]:
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
                raise RuleError("CALC_EXPR_SYNTAX", "只允许数字常量")
            value = to_decimal(node.value, "常量")
            self._record(str(node.value), value, {})
            return value, {}
        if isinstance(node, ast.Name):
            if node.id not in self.vars:
                raise RuleError("CALC_PARAM_MISSING", f"未绑定变量：{node.id}")
            value, unit = self.vars[node.id]
            dims = parse_unit(unit)
            self._record(node.id, value, dims)
            return value, dims
        if isinstance(node, ast.BinOp):
            left, ld = self._eval(node.left)
            right, rd = self._eval(node.right)
            if isinstance(node.op, (ast.Add, ast.Sub)):
                if ld != rd:
                    raise RuleError("CALC_UNIT_MISMATCH", f"加减法量纲不一致：{unit_str(ld)} 与 {unit_str(rd)}")
                with decimal_guard("表达式加减"):
                    value = left + right if isinstance(node.op, ast.Add) else left - right
                label = "+" if isinstance(node.op, ast.Add) else "-"
                self._record(f"({left}{label}{right})", value, ld)
                return value, ld
            if isinstance(node.op, ast.Mult):
                with decimal_guard("表达式乘法"):
                    value = left * right
                dims = _combine(ld, rd, 1)
                self._record(f"({left}*{right})", value, dims)
                return value, dims
            if isinstance(node.op, ast.Div):
                if right == 0:
                    raise RuleError(CODE_RULE_DIV_ZERO, "除数为 0")
                with decimal_guard("表达式除法"):
                    value = left / right
                dims = _combine(ld, rd, -1)
                self._record(f"({left}/{right})", value, dims)
                return value, dims
            if isinstance(node.op, ast.Pow):
                if rd:
                    raise RuleError("CALC_UNIT_MISMATCH", "指数必须是无量纲整数")
                if right != right.to_integral_value():
                    raise RuleError("CALC_EXPR_SYNTAX", "指数必须是整数")
                exp = int(right)
                if abs(exp) > 4:
                    raise RuleError("CALC_EXPR_SYNTAX", "指数绝对值不得超过 4")
                if left == 0 and exp < 0:
                    raise RuleError(CODE_RULE_DIV_ZERO, "零不能取负指数（等价除零）")
                with decimal_guard("表达式乘方"):
                    value = left ** exp
                dims = {k: v * exp for k, v in ld.items()}
                dims = {k: v for k, v in dims.items() if v}
                self._record(f"({left}^{exp})", value, dims)
                return value, dims
            raise RuleError("CALC_EXPR_SYNTAX", f"不支持的运算符：{type(node.op).__name__}")
        if isinstance(node, ast.UnaryOp):
            value, dims = self._eval(node.operand)
            if isinstance(node.op, ast.USub):
                self._record(f"-{value}", -value, dims)
                return -value, dims
            if isinstance(node.op, ast.UAdd):
                return value, dims
            raise RuleError("CALC_EXPR_SYNTAX", "不支持的一元运算")
        if isinstance(node, ast.Call):
            return self._call(node)
        raise RuleError("CALC_EXPR_SYNTAX", f"表达式含禁止的语法：{type(node).__name__}")

    def _call(self, node: ast.Call) -> tuple[Decimal, dict[str, int]]:
        if not isinstance(node.func, ast.Name):
            raise RuleError("CALC_EXPR_SYNTAX", "只允许调用白名单函数")
        name = node.func.id
        if name not in ALLOWED_FUNCS:
            raise RuleError("CALC_EXPR_SYNTAX", f"函数不在白名单：{name}")
        if node.keywords:
            raise RuleError("CALC_EXPR_SYNTAX", "函数不支持关键字参数")
        args = [self._eval(a) for a in node.args]
        values = [v for v, _ in args]
        dims = [d for _, d in args]
        if name in {"MIN", "MAX"}:
            if len(values) < 2 or any(d != dims[0] for d in dims):
                raise RuleError("CALC_UNIT_MISMATCH", "MIN/MAX 参数量纲必须一致且至少两个")
            value = min(values) if name == "MIN" else max(values)
            self._record(f"{name}({values})", value, dims[0])
            return value, dims[0]
        if name == "ABS":
            if len(values) != 1:
                raise RuleError("CALC_EXPR_SYNTAX", "ABS 只接受一个参数")
            self._record(f"ABS({values[0]})", abs(values[0]), dims[0])
            return abs(values[0]), dims[0]
        if name == "ROUND":
            if len(values) != 2 or dims[1]:
                raise RuleError("CALC_EXPR_SYNTAX", "ROUND 需要 (数值, 位数)")
            places = int(values[1])
            if places < -MAX_EXP or places > MAX_EXP:
                raise NumericError(CODE_PRECISION_EXCEEDED,
                                   f"ROUND 位数超出精度上限 ±{MAX_EXP}：{places}", "ROUND")
            quantum = Decimal(1).scaleb(-places)
            with decimal_guard("ROUND"):
                value = values[0].quantize(quantum, rounding=ROUND_HALF_UP)
            self._record(f"ROUND({values[0]},{places})", value, dims[0])
            return value, dims[0]
        if name in {"CEIL", "FLOOR"}:
            if len(values) != 1:
                raise RuleError("CALC_EXPR_SYNTAX", f"{name} 只接受一个参数")
            rounding = "ROUND_CEILING" if name == "CEIL" else "ROUND_FLOOR"
            value = values[0].to_integral_value(rounding=rounding)
            self._record(f"{name}({values[0]})", value, dims[0])
            return value, dims[0]
        raise RuleError("CALC_EXPR_SYNTAX", f"未实现函数：{name}")


class RuleRegistry:
    """规则注册表：加载、检索、执行、自检。"""

    def __init__(self, path: str | Path | None = None):
        self._rules: dict[tuple[str, str], dict[str, Any]] = {}
        if path:
            self.load(path)

    # --- loading ---
    def load(self, path: str | Path) -> None:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for rule in data.get("rules", []):
            self.register(rule)

    def register(self, rule: dict[str, Any]) -> None:
        required = ["rule_code", "version", "unit", "expression", "variables", "standard_ref"]
        missing = [k for k in required if k not in rule or (k != "variables" and not rule.get(k))]
        if missing:
            raise RuleError("CALC_RULE_NOT_FOUND", f"规则缺必填字段：{missing}")
        if not isinstance(rule.get("variables"), dict):
            raise RuleError("CALC_RULE_NOT_FOUND", "variables 必须是 {变量名: 单位} 字典")
        if rule.get("license_status") not in {"TEST_ONLY", "LICENSED", "PUBLIC"}:
            raise RuleError("BASIS_ITEM_MISSING", f"规则授权状态未确认：{rule.get('license_status')}")
        rule.setdefault("source_verified", False)
        rule.setdefault("status", "ACTIVE")
        key = (rule["rule_code"], str(rule["version"]))
        if key in self._rules:
            raise RuleError("CALC_RULE_CONFLICT", f"规则重复注册：{key}")
        self._rules[key] = rule

    # --- lookup ---
    def get(self, rule_code: str, version: str | None = None) -> dict[str, Any]:
        if version:
            rule = self._rules.get((rule_code, str(version)))
            if rule:
                return rule
            raise RuleError("CALC_RULE_NOT_FOUND", f"未找到规则 {rule_code}@{version}")
        candidates = [r for (code, _), r in self._rules.items() if code == rule_code]
        if not candidates:
            raise RuleError("CALC_RULE_NOT_FOUND", f"未找到规则：{rule_code}")
        if len(candidates) > 1:
            versions = sorted(str(r["version"]) for r in candidates)
            raise RuleError("CALC_RULE_CONFLICT", f"规则 {rule_code} 存在多个版本，必须指定版本：{versions}")
        return candidates[0]

    def match(self, component_type: str | None = None, caliber: str | None = None,
              attrs: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        attrs = attrs or {}
        hits = []
        for rule in self._rules.values():
            if component_type and rule.get("component_type") != component_type:
                continue
            if caliber and rule.get("caliber") != caliber:
                continue
            cond = rule.get("condition") or {}
            if any(attrs.get(k) != v for k, v in cond.items()):
                continue
            hits.append(rule)
        return hits

    def all(self) -> list[dict[str, Any]]:
        return sorted(self._rules.values(), key=lambda r: (r["rule_code"], str(r["version"])))

    # --- execution ---
    def execute(self, rule_code: str, params: dict[str, Any], version: str | None = None) -> dict[str, Any]:
        rule = self.get(rule_code, version)
        variables = rule["variables"]
        bound: dict[str, tuple[Decimal, str]] = {}
        missing = [k for k in variables if k not in params]
        if missing:
            raise RuleError("CALC_PARAM_MISSING", f"缺少参数：{missing}")
        extra = [k for k in params if k not in variables]
        if extra:
            raise RuleError("CALC_PARAM_UNKNOWN", f"存在未声明参数：{extra}")
        for name, unit in variables.items():
            bound[name] = (to_decimal(params[name], name), unit)
        ev = Evaluator(bound)
        value, trace = ev.run(rule["expression"], rule["unit"])
        value = ensure_finite(value, "rule_result")
        try:
            decimals = int(rule.get("decimals", DEFAULT_DECIMALS.get(rule["unit"], 3)))
        except (TypeError, ValueError):
            raise NumericError(CODE_PRECISION_EXCEEDED,
                               f"规则精度声明不是整数：{rule.get('decimals')!r}", "decimals") from None
        if decimals < 0 or decimals > MAX_EXP:
            raise NumericError(CODE_PRECISION_EXCEEDED,
                               f"规则精度声明越界（0..{MAX_EXP}）：{decimals}", "decimals")
        with decimal_guard("rule_result.quantize"):
            rounded = value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
        return {
            "rule_code": rule["rule_code"],
            "rule_version": str(rule["version"]),
            "name": rule.get("name", ""),
            "unit": rule["unit"],
            "value": str(rounded),
            "raw_value": str(value),
            "formula": rule["expression"],
            "intermediate_values": trace,
            "standard_ref": rule["standard_ref"],
            "license_status": rule["license_status"],
            "test_only": rule["license_status"] == "TEST_ONLY",
            "decimals": decimals,
        }

    # --- self test ---
    def validate(self) -> dict[str, Any]:
        results = []
        for rule in self.all():
            cases = rule.get("tests") or []
            failed = []
            for case in cases:
                try:
                    got = Decimal(self.execute(rule["rule_code"], case["params"], rule["version"])["value"])
                    exp = Decimal(str(case["expect"]))
                    if got != exp:
                        failed.append({"params": case["params"], "expect": str(exp), "got": str(got)})
                except RuleError as exc:
                    failed.append({"params": case["params"], "error": exc.message})
                except NumericError as exc:
                    failed.append({"params": case["params"], "error": exc.message})
            results.append({"rule_code": rule["rule_code"], "version": str(rule["version"]),
                            "cases": len(cases), "failed": failed})
        total = sum(r["cases"] for r in results)
        bad = [r for r in results if r["failed"] or r["cases"] == 0]
        return {"rules": len(results), "cases": total, "rules_without_case": [r["rule_code"] for r in results if r["cases"] == 0],
                "failed": bad, "ok": not bad}
