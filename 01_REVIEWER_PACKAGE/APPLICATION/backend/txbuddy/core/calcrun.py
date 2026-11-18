"""计算快照、内容哈希与历史复算（TX-018 / P0-07-RECALC）。

对应 V3.1 A-40 与 docs/06「计算复现不变量」：一次正式计算必须留下
input_snapshot + rule_version + basis_pack_id + precision_rule_version，缺一即不可复现。

历史复算的两条硬约束（本模块是唯一实现）：

1. **快照可复现**：``recalc`` 只信 snapshot，按 snapshot 里的 rule_code / rule_version /
   params 重跑引擎，绝不复用调用方给的“上次结果”；再用 A-40 容差（绝对 0.01 元、
   相对 1e-6，满足其一）与存档值比较。
2. **篡改可检出**：``verify_snapshot`` 先比对快照内嵌的 snapshot_sha256，再（可选用）
   比对持久化记录里的指纹 ``expected_sha256``。后者是必需的：只改参数再顺手重算内嵌
   哈希的“重新盖章”快照，单看内嵌哈希是自洽的，只有外部指纹能挡住。
   ``recalc_record`` 把这条规则固化到历史 calc_run 行上：行内 rule_code/rule_version/
   basis_pack_id/snapshot_sha256 与快照不一致即拒绝复算。

对外的拒绝语义：``reproducible=False`` + ``rejected=True`` + ``stage`` 指明卡在哪一层
（SNAPSHOT 快照未通过校验、RULE 规则版本漂移、UNIT 量纲漂移、RECORD 记录与快照不一致）。
校验通过的记录走 ``stage=RECALC``；此时 ``reproducible=False`` 表示真实复算偏差，不是拒绝。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .numeric import CODE_INVALID, CODE_NOT_FINITE, NumericError

ENGINE_BUILD = "txbuddy-core/0.2.0"
SNAPSHOT_SCHEMA = "calc_snapshot/1"

TOLERANCE_ABS = Decimal("0.01")
TOLERANCE_REL = Decimal("0.000001")

#: 快照必须携带的字段（A-40：缺一即不可复现）。
REQUIRED_FIELDS = ("rule_code", "rule_version", "basis_pack_id", "precision_rule_version", "params", "unit")

DIGEST_FIELD = "snapshot_sha256"

REASON_OK = "OK"
REASON_INCOMPLETE = "SNAPSHOT_INCOMPLETE"
REASON_INVALID = "SNAPSHOT_INVALID"
REASON_MALFORMED = "SNAPSHOT_MALFORMED"
REASON_UNREADABLE = "SNAPSHOT_UNREADABLE"
REASON_UNSEALED = "SNAPSHOT_UNSEALED"
REASON_TAMPERED = "SNAPSHOT_TAMPERED"
REASON_FINGERPRINT = "SNAPSHOT_FINGERPRINT_MISMATCH"
REASON_RULE_VERSION = "RECALC_RULE_VERSION_DRIFT"
REASON_UNIT = "RECALC_UNIT_MISMATCH"
REASON_MISMATCH = "RECALC_MISMATCH"
REASON_RECORD = "CALC_RUN_SNAPSHOT_MISMATCH"

#: 这些字段缺失或为空即视为快照不完整（params 允许是空字典：无变量规则合法）。
_EMPTY_MEANS_MISSING = (None, "", [])
_STRING_FIELDS = ("rule_code", "basis_pack_id", "precision_rule_version", "unit")


# ------------------------------------------------------------------ 规范化与哈希
def _canonical(payload: dict[str, Any]) -> str:
    """稳定序列化：键排序、无多余空白、Decimal 按十进制字符串、拒绝不可哈希的对象。"""
    def default(obj: Any):
        if isinstance(obj, Decimal):
            return str(obj)
        raise TypeError(f"{type(obj).__name__} 不可序列化进快照")

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=default)


def build_snapshot(*, rule_code: str, rule_version: str, basis_pack_id: str,
                   precision_rule_version: str, params: dict[str, Any],
                   unit: str, source_refs: list[str] | None = None,
                   components: list[int] | None = None) -> dict[str, Any]:
    if not all([rule_code, rule_version, basis_pack_id, precision_rule_version, unit]):
        raise ValueError("快照缺关键字段：rule_code/rule_version/basis_pack_id/precision_rule_version/unit")
    snapshot = {
        "snapshot_schema": SNAPSHOT_SCHEMA,
        "rule_code": rule_code,
        "rule_version": str(rule_version),
        "basis_pack_id": basis_pack_id,
        "precision_rule_version": precision_rule_version,
        # 参数一律落成字符串：Decimal/float/int 在 JSON 往返后不会改变哈希。
        "params": {str(k): str(v) for k, v in params.items()},
        "unit": unit,
        "source_refs": source_refs or [],
        "component_ids": components or [],
        "engine_build": ENGINE_BUILD,
    }
    snapshot[DIGEST_FIELD] = hashlib.sha256(_canonical(snapshot).encode("utf-8")).hexdigest()
    return snapshot


def snapshot_hash(snapshot: dict[str, Any]) -> str:
    """重算快照指纹（不含 snapshot_sha256 自身）。"""
    base = {k: v for k, v in snapshot.items() if k != DIGEST_FIELD}
    return hashlib.sha256(_canonical(base).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 快照校验
def _malformed_fields(snapshot: dict[str, Any]) -> list[str]:
    bad = []
    if "params" in snapshot and not isinstance(snapshot["params"], dict):
        bad.append("params")
    for key in _STRING_FIELDS:
        if key in snapshot and not isinstance(snapshot[key], str):
            bad.append(key)
    if "rule_version" in snapshot and not isinstance(snapshot["rule_version"], (str, int)):
        bad.append("rule_version")
    return bad


def verify_snapshot(snapshot: dict[str, Any], *, expected_sha256: str | None = None) -> dict[str, Any]:
    """校验快照完整性与指纹。

    ``expected_sha256`` 是快照之外的持久化指纹（calc_run.snapshot_sha256、审计记录等）。
    不给时只校验内嵌哈希；给了就连“重新盖章”的伪造快照一起拒绝。
    """
    out: dict[str, Any] = {
        "valid": False,
        "reason": REASON_INVALID,
        "missing": [],
        "actual_sha256": None,
        "expected_sha256": None,
        "embedded_sha256": None,
        "fingerprint_sha256": str(expected_sha256) if expected_sha256 else None,
        "fingerprint_checked": bool(expected_sha256),
    }
    if not isinstance(snapshot, dict):
        out["detail"] = f"快照必须是字典，实际为 {type(snapshot).__name__}"
        return out

    out["missing"] = [k for k in REQUIRED_FIELDS if k not in snapshot or snapshot.get(k) in _EMPTY_MEANS_MISSING]
    if out["missing"]:
        out["reason"] = REASON_INCOMPLETE
        out["detail"] = f"快照缺关键字段：{out['missing']}"
        return out

    malformed = _malformed_fields(snapshot)
    if malformed:
        out["reason"] = REASON_MALFORMED
        out["malformed"] = malformed
        out["detail"] = f"快照字段类型非法：{malformed}"
        return out

    try:
        actual = snapshot_hash(snapshot)
    except (TypeError, ValueError) as exc:
        # 含不可序列化对象（datetime/set/自定义类）的快照不得把校验器打成 500。
        out["reason"] = REASON_UNREADABLE
        out["detail"] = f"快照无法规范化：{type(exc).__name__}: {exc}"
        return out

    embedded = snapshot.get(DIGEST_FIELD)
    out["actual_sha256"] = actual
    out["embedded_sha256"] = embedded
    out["expected_sha256"] = embedded  # 向后兼容：历史消费者把 expected 理解为内嵌哈希
    if not isinstance(embedded, str) or not embedded:
        out["reason"] = REASON_UNSEALED
        out["detail"] = "快照未携带 snapshot_sha256，无法证明未被改动"
        return out
    if actual != embedded:
        out["reason"] = REASON_TAMPERED
        out["detail"] = "快照内容与内嵌 snapshot_sha256 不一致"
        return out
    if expected_sha256 and actual != str(expected_sha256):
        out["reason"] = REASON_FINGERPRINT
        out["detail"] = "快照内容与持久化记录的 snapshot_sha256 不一致（疑似重新盖章）"
        return out

    out["valid"] = True
    out["reason"] = REASON_OK
    return out


# ------------------------------------------------------------------ A-40 比较
def _as_decimal(value: Any, field: str) -> Decimal:
    try:
        d = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, AttributeError, TypeError, ValueError):
        raise NumericError(CODE_INVALID, f"{field} 不是合法数值：{value!r}", field) from None
    if not d.is_finite():
        raise NumericError(CODE_NOT_FINITE, f"{field} 不允许 NaN/Infinity：{value!r}", field)
    return d


def _decimal_text(value: Decimal, places: int = 9) -> str:
    """定点展示，不因量级过大而抛 InvalidOperation（现网曾在此 500）。"""
    try:
        return str(value.quantize(Decimal(1).scaleb(-places)))
    except InvalidOperation:
        return str(value)


def values_match(stored: Any, recomputed: Any,
                 tol_abs: Decimal = TOLERANCE_ABS, tol_rel: Decimal = TOLERANCE_REL) -> dict[str, Any]:
    """A-40 容差口径：绝对 0.01 或相对 1e-6 满足其一即视为一致。"""
    a, b = _as_decimal(stored, "stored_value"), _as_decimal(recomputed, "recomputed_value")
    delta = a - b
    abs_delta = abs(delta)
    denom = abs(a) if a != 0 else Decimal(1)
    rel_delta = abs_delta / denom
    consistent = abs_delta <= tol_abs or rel_delta <= tol_rel
    return {
        "consistent": consistent,
        "stored": str(a),
        "recomputed": str(b),
        "delta": str(delta),
        "delta_abs": str(abs_delta),
        "delta_rel": _decimal_text(rel_delta),
        "tolerance_abs": str(tol_abs),
        "tolerance_rel": str(tol_rel),
        "reason": REASON_OK if consistent else REASON_MISMATCH,
    }


# ------------------------------------------------------------------ 复算
def _refusal(stage: str, reason: str, check: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """统一“拒绝复算”的返回形状：引擎未产出可信结果。"""
    payload: dict[str, Any] = {"reproducible": False, "rejected": True, "stage": stage, "reason": reason}
    payload.update(check)
    payload["snapshot_fingerprint_sha256"] = check.get("fingerprint_sha256")
    payload["snapshot_fingerprint_checked"] = bool(check.get("fingerprint_checked"))
    payload.update(extra)
    return payload


def recalc(registry, snapshot: dict[str, Any], stored_value: Any,
           *, expected_sha256: str | None = None) -> dict[str, Any]:
    """按快照重新执行规则，并与存档值比较。

    ``registry`` 需实现 ``execute(rule_code, params, version)``。
    ``expected_sha256`` 传历史记录里的持久化指纹；只改快照不改记录必被拒绝。
    未知规则/非法数值仍向上抛 RuleError / NumericError，由 HTTP 边界映射 422。
    """
    check = verify_snapshot(snapshot, expected_sha256=expected_sha256)
    if not check["valid"]:
        return _refusal("SNAPSHOT", check["reason"], check)

    rule_code = str(snapshot["rule_code"])
    rule_version = str(snapshot["rule_version"])
    result = registry.execute(rule_code, snapshot["params"], rule_version)

    contract = {
        "snapshot_sha256": snapshot.get(DIGEST_FIELD),
        "snapshot_fingerprint_sha256": check.get("fingerprint_sha256"),
        "snapshot_fingerprint_checked": check.get("fingerprint_checked", False),
        "rule_code": rule_code,
        "rule_version": rule_version,
        "basis_pack_id": snapshot["basis_pack_id"],
        "precision_rule_version": snapshot["precision_rule_version"],
        "snapshot_unit": snapshot["unit"],
    }
    if str(result.get("rule_version")) != rule_version:
        return _refusal("RULE", REASON_RULE_VERSION, {},
                        recomputed_rule_version=str(result.get("rule_version")), **contract)
    if str(result.get("unit")) != str(snapshot["unit"]):
        return _refusal("UNIT", REASON_UNIT, {},
                        recomputed_unit=result.get("unit"), **contract)

    cmp = values_match(stored_value, result["value"])
    snapshot_engine = snapshot.get("engine_build")
    engine_match = snapshot_engine is None or str(snapshot_engine) == ENGINE_BUILD
    warnings: list[dict[str, Any]] = []
    if not engine_match:
        warnings.append({"code": "ENGINE_BUILD_CHANGED",
                         "snapshot_engine_build": str(snapshot_engine),
                         "current_engine_build": ENGINE_BUILD})
    return {
        "reproducible": cmp["consistent"],
        "rejected": False,
        "stage": "RECALC",
        "reason": cmp["reason"],
        **contract,
        "unit": result["unit"],
        "stored_value": cmp["stored"],
        "recomputed_value": result["value"],
        "engine_build": ENGINE_BUILD,
        "snapshot_engine_build": snapshot_engine,
        "engine_build_match": engine_match,
        "warnings": warnings,
        "comparison": cmp,
        "evidence_payload": {
            "rule_code": result["rule_code"],
            "rule_version": result["rule_version"],
            "formula": result["formula"],
            "intermediate_values": result["intermediate_values"],
            "standard_ref": result["standard_ref"],
        },
    }


def recalc_record(registry, run: dict[str, Any], *, expected_sha256: str | None = None) -> dict[str, Any]:
    """按历史 calc_run 行复算：以行内持久化指纹为准，行与快照不一致即拒绝。

    ``run`` 形如 ``store.get_calc_run(calc_run_id)`` 的返回：
    calc_run_id / rule_code / rule_version / basis_pack_id / snapshot_sha256 / snapshot / result。
    """
    if not isinstance(run, dict):
        raise ValueError("calc_run 记录必须是字典")
    snapshot = run.get("snapshot")
    row = {
        "rule_code": run.get("rule_code"),
        "rule_version": run.get("rule_version"),
        "basis_pack_id": run.get("basis_pack_id"),
        "snapshot_sha256": run.get("snapshot_sha256"),
    }
    fingerprint = str(expected_sha256 or row["snapshot_sha256"] or "") or None
    stored = run.get("result")

    mismatch: list[str] = []
    if isinstance(snapshot, dict):
        for key in ("rule_code", "rule_version", "basis_pack_id"):
            if row[key] is not None and snapshot.get(key) is not None and str(row[key]) != str(snapshot.get(key)):
                mismatch.append(key)
        embedded = snapshot.get(DIGEST_FIELD)
        if row["snapshot_sha256"] and embedded and str(row["snapshot_sha256"]) != str(embedded):
            if "snapshot_sha256" not in mismatch:
                mismatch.append("snapshot_sha256")

    out = recalc(registry, snapshot, stored, expected_sha256=fingerprint)
    out["calc_run_id"] = run.get("calc_run_id")
    out["stored_value"] = None if stored is None else str(stored)
    out["record_checks"] = {
        **row,
        "snapshot_embedded_sha256": snapshot.get(DIGEST_FIELD) if isinstance(snapshot, dict) else None,
    }
    out["record_mismatch"] = mismatch
    if mismatch:
        out["reproducible"] = False
        out["rejected"] = True
        if out.get("stage") == "SNAPSHOT":
            # 快照本身已拒；记录层不一致作为附加证据保留。
            out["record_reason"] = REASON_RECORD
        else:
            out["stage"] = "RECORD"
            out["reason"] = REASON_RECORD
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
