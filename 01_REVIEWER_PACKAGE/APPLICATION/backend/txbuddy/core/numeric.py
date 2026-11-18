"""数值安全（P0-01 / P0-01-NUMERIC）：全引擎 Decimal + 有限性校验 + 统一 NUMERIC_* 错误码。

本模块是全系统唯一的数值闸门：qty / quota / price / rules / goldenset 的所有对外入口
都必须先经过这里的守卫，再进入业务运算。

六类非法数值与对应错误码（全部属于 NUMERIC_* 族，对外一律 422）：

    NaN / Infinity / -Infinity   -> NUMERIC_NOT_FINITE
    1e999（超出量级）            -> NUMERIC_OVERFLOW
    除数为 0                     -> NUMERIC_DIV_ZERO
    超出 Decimal 精度上限        -> NUMERIC_PRECISION_EXCEEDED
    （小数位 > 12 或量级 < 1e-12）
    无法解析为数值               -> NUMERIC_INVALID
    负数 / 零值越界              -> NUMERIC_NEGATIVE / NUMERIC_ZERO_NOT_ALLOWED

纪律：
- 禁止静默截断：1e-30 不得被当作 0 继续计算，1e999 不得被当作 Infinity 或大数继续计算。
- 禁止 500：任何 DecimalException（InvalidOperation / DivisionByZero / Overflow）都必须
  在核心层转换成 NumericError；HTTP 边界再由 install_exception_handlers 统一映射 422。
- 对外错误码只有一个契约：NUMERIC_*。历史 CALC_* 语义码只在进程内保留
  （见 LEGACY_CODE_MAP），对外经 unified_code() 迁移。
"""
from __future__ import annotations

from contextlib import contextmanager
from decimal import (Decimal, DecimalException, DivisionByZero, InvalidOperation,
                     Overflow)

# ---------------------------------------------------------------- 量级边界
MAX_ABS = Decimal("1e12")      # 单值上限：千亿，超出视为脏数据
MIN_ABS = Decimal("1e-12")     # 结果最小可表示量级，低于此值不得当作 0 继续计算
MIN_EXP = -12                  # 最小可表示指数（小数位上限）
MAX_EXP = 12                   # 最大可表示指数
MAX_RATE = Decimal(1)          # 费率上界：100%

# ---------------------------------------------------------------- 统一错误码族
CODE_INVALID = "NUMERIC_INVALID"
CODE_NOT_FINITE = "NUMERIC_NOT_FINITE"
CODE_OVERFLOW = "NUMERIC_OVERFLOW"
CODE_PRECISION_EXCEEDED = "NUMERIC_PRECISION_EXCEEDED"
CODE_DIV_ZERO = "NUMERIC_DIV_ZERO"
CODE_NEGATIVE = "NUMERIC_NEGATIVE"
CODE_ZERO_NOT_ALLOWED = "NUMERIC_ZERO_NOT_ALLOWED"
CODE_OUT_OF_RANGE = "NUMERIC_OUT_OF_RANGE"
CODE_ARITHMETIC_ERROR = "NUMERIC_ARITHMETIC_ERROR"

NUMERIC_CODES = frozenset({
    CODE_INVALID, CODE_NOT_FINITE, CODE_OVERFLOW, CODE_PRECISION_EXCEEDED,
    CODE_DIV_ZERO, CODE_NEGATIVE, CODE_ZERO_NOT_ALLOWED, CODE_OUT_OF_RANGE,
    CODE_ARITHMETIC_ERROR,
})

# 进程内历史语义码 -> 对外统一码。只迁移“数值类”语义码；
# CALC_PARAM_MISSING（缺参数）、CALC_UNIT_MISMATCH（量纲不一致）、CALC_RULE_NOT_FOUND 等
# 属于规则语义错误，不是非法数值，保持原码。
LEGACY_CODE_MAP = {
    "CALC_DIV_ZERO": CODE_DIV_ZERO,
}


class NumericError(ValueError):
    """非法数值统一异常。code 恒为 NUMERIC_*；message 供人阅读；field 指向入参字段。"""

    def __init__(self, code: str, message: str, field: str | None = None):
        self.code = code
        self.message = message
        self.field = field
        super().__init__(f"{code}: {message}")


def unified_code(code: str | None, default: str = CODE_INVALID) -> str:
    """把进程内语义码迁移到对外统一错误码族。"""
    if not code:
        return default
    if code in NUMERIC_CODES:
        return code
    return LEGACY_CODE_MAP.get(code, code)


# ---------------------------------------------------------------- 入参守卫
def _check_magnitude(d: Decimal, field: str) -> Decimal:
    if d == 0:
        return d
    exponent = d.as_tuple().exponent
    if exponent > MAX_EXP or abs(d) > MAX_ABS:
        raise NumericError(
            CODE_OVERFLOW,
            f"{field} 超出允许量级（|值| ≤ {MAX_ABS}，指数 ≤ {MAX_EXP}）：{d}",
            field,
        )
    if exponent < MIN_EXP:
        raise NumericError(
            CODE_PRECISION_EXCEEDED,
            f"{field} 超出 Decimal 精度上限（小数位 ≤ {-MIN_EXP}）：{d}",
            field,
        )
    return d


def to_decimal(value, field: str = "value", *, allow_negative: bool = False,
               allow_zero: bool = True) -> Decimal:
    """把任意入参转换为合法 Decimal；任何非法数值都抛 NumericError（NUMERIC_*）。"""
    if isinstance(value, bool):
        raise NumericError(CODE_INVALID, f"{field} 不允许布尔值：{value!r}", field)
    if value is None:
        raise NumericError(CODE_INVALID, f"{field} 缺失或为 null", field)
    if isinstance(value, Decimal):
        d = value
    else:
        try:
            text = str(value).strip()
            if not text:
                raise InvalidOperation("empty")
            d = Decimal(text)
        except (InvalidOperation, TypeError, ValueError):
            raise NumericError(CODE_INVALID, f"{field} 不是合法数值：{value!r}", field) from None
    if not d.is_finite():
        raise NumericError(CODE_NOT_FINITE, f"{field} 不允许 NaN/Infinity：{value!r}", field)
    _check_magnitude(d, field)
    if d < 0 and not allow_negative:
        raise NumericError(CODE_NEGATIVE, f"{field} 不允许负数：{d}", field)
    if d == 0 and not allow_zero:
        raise NumericError(CODE_ZERO_NOT_ALLOWED, f"{field} 不允许零", field)
    return d


def to_rate(value, field: str = "rate", *, allow_zero: bool = True) -> Decimal:
    """费率/比例：必须落在 [0, 1]。"""
    d = to_decimal(value, field, allow_negative=False, allow_zero=allow_zero)
    if d > MAX_RATE:
        raise NumericError(CODE_OUT_OF_RANGE, f"{field} 费率必须在 [0, 1] 内：{d}", field)
    return d


def ensure_decimal(value, field: str = "value") -> Decimal:
    """把任意中间值规整成 Decimal（允许负数，禁止 NaN/Infinity/越界）。"""
    return value if isinstance(value, Decimal) and value.is_finite() else to_decimal(
        value, field, allow_negative=True)


# ---------------------------------------------------------------- 结果守卫
def ensure_finite(d: Decimal, field: str = "result") -> Decimal:
    """计算结果守卫：非有限 / 超量级 / 低于最小可表示量级一律拒绝，不得静默截断为 0。"""
    if not isinstance(d, Decimal):
        raise NumericError(CODE_INVALID, f"{field} 计算结果不是 Decimal：{d!r}", field)
    if not d.is_finite():
        raise NumericError(CODE_NOT_FINITE, f"{field} 计算结果非法（NaN/Infinity）", field)
    if abs(d) > MAX_ABS:
        raise NumericError(CODE_OVERFLOW, f"{field} 计算结果超出允许量级：{d}", field)
    if d != 0 and abs(d) < MIN_ABS:
        raise NumericError(CODE_PRECISION_EXCEEDED,
                           f"{field} 计算结果低于最小可表示量级 {MIN_ABS}：{d}", field)
    return d


def safe_div(a, b, field: str = "quotient") -> Decimal:
    """安全除法：除数为 0 -> NUMERIC_DIV_ZERO；结果再过量级守卫。"""
    numerator = to_decimal(a, f"{field}.numerator", allow_negative=True)
    denominator = to_decimal(b, f"{field}.denominator", allow_negative=True)
    if denominator == 0:
        raise NumericError(CODE_DIV_ZERO, f"{field} 除数为 0", f"{field}.denominator")
    return ensure_finite(numerator / denominator, field)


def finite_product(*factors, field: str = "product") -> Decimal:
    """连乘并做结果守卫，避免 1e12 × 1e12 这类中间越界被静默返回。"""
    result = Decimal(1)
    for index, factor in enumerate(factors):
        result = result * to_decimal(factor, f"{field}[{index}]", allow_negative=True)
    return ensure_finite(result, field)


@contextmanager
def decimal_guard(field: str = "value"):
    """把 Decimal 算术异常收口成 NumericError，禁止逃逸成 500。"""
    try:
        yield
    except NumericError:
        raise
    except DivisionByZero:
        raise NumericError(CODE_DIV_ZERO, f"{field} 除数为 0", field) from None
    except Overflow:
        raise NumericError(CODE_OVERFLOW, f"{field} 计算溢出", field) from None
    except InvalidOperation:
        raise NumericError(CODE_PRECISION_EXCEEDED, f"{field} 超出 Decimal 精度上限", field) from None
    except DecimalException as exc:  # 兜底：任何 Decimal 异常都不能变成 500
        raise NumericError(CODE_ARITHMETIC_ERROR, f"{field} Decimal 运算失败：{exc}", field) from None


# ---------------------------------------------------------------- HTTP 边界映射
def classify(exc: BaseException, fallback: str = CODE_INVALID) -> tuple[str, str]:
    """把核心层异常映射成 (NUMERIC_* / 语义码, 人类可读消息)。"""
    if isinstance(exc, NumericError):
        return exc.code, exc.message
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None)
    if isinstance(code, str) and code:
        return unified_code(code), message or str(exc)
    if isinstance(exc, DivisionByZero):
        return CODE_DIV_ZERO, str(exc) or "除数为 0"
    if isinstance(exc, Overflow):
        return CODE_OVERFLOW, str(exc) or "数值溢出"
    if isinstance(exc, InvalidOperation):
        return CODE_INVALID, str(exc) or "非法数值运算"
    if isinstance(exc, DecimalException):
        return CODE_ARITHMETIC_ERROR, str(exc) or "Decimal 运算失败"
    return fallback, str(exc)


def http_error(exc: BaseException, fallback: str = CODE_INVALID):
    """构造 422 HTTPException(业务错误码)，供 API 层统一使用。"""
    from fastapi import HTTPException
    code, message = classify(exc, fallback)
    return HTTPException(status_code=422, detail={"code": code, "message": message})


def _validation_code(error: dict) -> str | None:
    """判定 pydantic 校验错误是否属于“非法数值”，并给出 NUMERIC_* 码；否则返回 None。"""
    err_type = str(error.get("type") or "")
    msg = str(error.get("msg") or "")
    raw = error.get("input")
    if err_type == "finite_number":
        return CODE_NOT_FINITE
    numeric_hint = any(token in msg for token in
                       ("NaN", "Infinity", "Decimal", "指数", "量级", "数值", "finite", "decimal"))
    if not numeric_hint:
        return None
    try:
        d = raw if isinstance(raw, Decimal) else Decimal(str(raw).strip())
    except (InvalidOperation, TypeError, ValueError):
        return CODE_INVALID
    if not d.is_finite():
        return CODE_NOT_FINITE
    exponent = d.as_tuple().exponent
    if exponent > MAX_EXP or abs(d) > MAX_ABS:
        return CODE_OVERFLOW
    if exponent < MIN_EXP:
        return CODE_PRECISION_EXCEEDED
    return CODE_INVALID


def install_exception_handlers(app, domain_errors=()) -> None:
    """在 FastAPI 应用上安装统一 422 兜底。

    - NumericError / DecimalException / DecimalException 子类：NUMERIC_* + 422
    - domain_errors（如 RuleError）：按其 .code 迁移后 422，禁止 500
    - RequestValidationError：数值类校验错误映射 NUMERIC_*；其它保持默认 422 形状
    """
    from decimal import DecimalException as _DecimalException

    from fastapi.encoders import jsonable_encoder
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    async def _numeric_handler(request, exc):  # noqa: ARG001
        code, message = classify(exc)
        return JSONResponse(status_code=422,
                            content={"detail": {"code": code, "message": message,
                                                "field": getattr(exc, "field", None)}})

    async def _domain_handler(request, exc):  # noqa: ARG001
        code, message = classify(exc, default=CODE_INVALID)
        return JSONResponse(status_code=422, content={"detail": {"code": code, "message": message}})

    async def _validation_handler(request, exc: RequestValidationError):  # noqa: ARG001
        errors = exc.errors()
        for error in errors:
            code = _validation_code(error)
            if code:
                fields = [str(part) for part in error.get("loc", ()) if part != "body"]
                return JSONResponse(status_code=422, content={"detail": {
                    "code": code,
                    "message": f"{'.'.join(fields) or 'body'}: {error.get('msg', '')}",
                    "errors": jsonable_encoder(errors),
                }})
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})

    app.add_exception_handler(NumericError, _numeric_handler)
    app.add_exception_handler(_DecimalException, _numeric_handler)
    for error_type in domain_errors:
        app.add_exception_handler(error_type, _domain_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
