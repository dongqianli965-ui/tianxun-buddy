"""P0-01 / P0-03 正式计算准入（唯一实现）。

规则绑定依据包内真实来源（source_ref 命中、已核验、已授权、region+major 一致、有效期覆盖），
TEST_ONLY / DRAFT 依据包与 TEST_ONLY 规则永不 formal_ready。

P0-03：所有会产出“可用于正式成果的数值”的入口都必须走本模块的 `require_formal`：

    adm = require_formal(rule, basis, formal_calculation, source_refs=[...])
    if adm["rejected"]:
        -> 入口返回 422（不得算出正式数值）
    -> formal_calculation=false 时 adm["test_only"] is True，结果一律标记为测试结果

判定只有一份实现，入口只负责“传参 + 拒绝 + 打标”，不得各自复制准入逻辑。
"""
from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from .basis import validate_basis_pack

FORMAL_LICENSES = {"LICENSED", "PUBLIC"}
SUSPECT = ("待核", "TEST", "TBD", "UNKNOWN", "占位")

# 入口拒绝正式计算时统一的业务错误码（沿用既有对外契约 BASIS_PACK_MISSING）
FORMAL_ADMISSION_CODE = "BASIS_PACK_MISSING"
FORMAL_ADMISSION_MESSAGE = (
    "正式计算未通过准入：依据包必须 LOCKED 且使用已授权、已核验的真实来源，"
    "规则须绑定包内 source_ref、region/major 与依据包一致并在有效期内；"
    "测试数据只能以 formal_calculation=false 做测试计算"
)


def _suspect(text: str) -> bool:
    up = (text or "").upper()
    return (not text) or any(m.upper() in up for m in SUSPECT)


def _reason(code: str, detail: str) -> dict[str, str]:
    return {"reason_code": code, "detail": detail}


def _dedupe(reasons: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for item in reasons:
        key = (item.get("reason_code", ""), item.get("detail", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def rule_admission(rule: dict[str, Any], basis) -> tuple[bool, list[dict]]:
    reasons: list[dict] = []
    lic = str(rule.get("license_status", "UNKNOWN"))
    if lic not in FORMAL_LICENSES:
        reasons.append({"reason_code": "BASIS_ITEM_MISSING", "detail": f"规则授权状态不可用于正式成果：{lic}"})
    if rule.get("source_verified") is not True:
        reasons.append({"reason_code": "BASIS_ITEM_MISSING", "detail": "规则来源未经核验（source_verified≠true）"})
    if _suspect(str(rule.get("standard_ref", ""))):
        reasons.append({"reason_code": "BASIS_ITEM_MISSING", "detail": f"standard_ref 缺失或含未核实标记"})
    if str(rule.get("status", "ACTIVE")).upper() != "ACTIVE":
        reasons.append({"reason_code": "BASIS_ITEM_MISSING", "detail": "规则状态非 ACTIVE"})
    src_ref = str(rule.get("source_ref") or "")
    if not src_ref:
        reasons.append({"reason_code": "BASIS_ITEM_MISSING", "detail": "规则未声明 source_ref"})
    else:
        hit = [s for s in (basis.sources or []) if s.source_id == src_ref]
        if not hit:
            reasons.append({"reason_code": "BASIS_PACK_MISSING", "detail": f"source_ref={src_ref} 不属于当前依据包"})
        else:
            src = hit[0]
            if not src.verified:
                reasons.append({"reason_code": "BASIS_ITEM_MISSING", "detail": f"来源 {src_ref} 未核验"})
            if src.license_status not in FORMAL_LICENSES:
                reasons.append({"reason_code": "BASIS_ITEM_MISSING",
                                "detail": f"来源 {src_ref} 授权状态不可用于正式成果：{src.license_status}"})
    rc = rule.get("region_code")
    if not rc:
        reasons.append({"reason_code": "BASIS_PACK_MISSING", "detail": "规则缺 region_code"})
    elif rc != basis.region_code:
        reasons.append({"reason_code": "BASIS_PACK_MISSING", "detail": f"规则地区 {rc} ≠ 依据包 {basis.region_code}"})
    mj = rule.get("major")
    if not mj:
        reasons.append({"reason_code": "BASIS_PACK_MISSING", "detail": "规则缺 major"})
    elif mj != basis.major:
        reasons.append({"reason_code": "BASIS_PACK_MISSING", "detail": f"规则专业 {mj} ≠ 依据包 {basis.major}"})
    for key, label in (("eff_from", "生效日"), ("eff_to", "失效日")):
        raw = rule.get(key)
        if not raw:
            continue
        try:
            d = date.fromisoformat(str(raw))
        except ValueError:
            reasons.append({"reason_code": "BASIS_PACK_MISSING", "detail": "规则有效期格式非法"})
            continue
        if key == "eff_from" and basis.effective_date < d:
            reasons.append({"reason_code": "BASIS_PACK_MISSING", "detail": "依据日期早于规则生效日"})
        if key == "eff_to" and basis.effective_date > d:
            reasons.append({"reason_code": "BASIS_PACK_MISSING", "detail": "依据日期晚于规则失效日"})
    return (not reasons), reasons


def source_ref_admission(basis, source_refs: Iterable[str | None]) -> list[dict]:
    """费用/资源/单价引用的 source_ref 必须命中依据包且已核验、已授权。"""
    reasons: list[dict] = []
    by_id = {s.source_id: s for s in (getattr(basis, "sources", None) or [])}
    for raw in source_refs or ():
        ref = str(raw or "").strip()
        if not ref:
            reasons.append({"reason_code": "BASIS_ITEM_MISSING", "detail": "计价来源未声明 source_ref"})
            continue
        src = by_id.get(ref)
        if src is None:
            reasons.append({"reason_code": "BASIS_PACK_MISSING", "detail": f"source_ref={ref} 不属于当前依据包"})
            continue
        if not src.verified:
            reasons.append({"reason_code": "BASIS_ITEM_MISSING", "detail": f"来源 {ref} 未核验"})
        if src.license_status not in FORMAL_LICENSES:
            reasons.append({"reason_code": "BASIS_ITEM_MISSING",
                            "detail": f"来源 {ref} 授权状态不可用于正式成果：{src.license_status}"})
    return reasons


def formal_admission(rule: dict[str, Any] | None, basis, source_refs: Iterable[str | None] = ()) -> dict[str, Any]:
    """规则 + 依据包 + 引用来源的整体准入结论（不看请求是否真的要正式计算）。"""
    info = validate_basis_pack(basis)
    reasons: list[dict] = []
    if not info["formal_ready"]:
        for issue in info["issues"] or ["依据包未锁定或为测试数据"]:
            reasons.append({"reason_code": "BASIS_PACK_MISSING", "detail": issue})
    rule_ready = True
    if rule is not None:
        rule_ready, rule_reasons = rule_admission(rule, basis)
        reasons.extend(rule_reasons)
    source_reasons = source_ref_admission(basis, source_refs)
    reasons.extend(source_reasons)
    reasons = _dedupe(reasons)
    formal_ready = bool(info["formal_ready"]) and rule_ready and not source_reasons
    rule_test_only = rule is not None and str(rule.get("license_status", "UNKNOWN")) not in FORMAL_LICENSES
    return {"formal_ready": formal_ready, "basis_ready": info["formal_ready"], "rule_ready": rule_ready,
            "test_only": bool(basis.test_only) or rule_test_only or not formal_ready,
            "admission_reasons": reasons, "basis_pack_id": basis.basis_pack_id,
            "rule_code": (rule or {}).get("rule_code"), "rule_version": str((rule or {}).get("version"))}


def _no_basis_admission(rule: dict[str, Any] | None) -> dict[str, Any]:
    return {"formal_ready": False, "basis_ready": False, "rule_ready": rule is not None,
            "test_only": True,
            "admission_reasons": [_reason("BASIS_PACK_MISSING", "未提供依据包 basis_pack：结果只能作为测试计算")],
            "basis_pack_id": None, "rule_code": (rule or {}).get("rule_code"),
            "rule_version": str((rule or {}).get("version"))}


def require_formal(rule: dict[str, Any] | None, basis, formal_calculation: bool,
                   source_refs: Iterable[str | None] = ()) -> dict[str, Any]:
    """统一准入口：formal_calculation=True 时必须 formal_ready，否则 rejected=True。

    返回字段：
      admitted   本次请求是否取得了“正式成果”资格（只有显式正式 + 全部准入通过才为 True）
      rejected   formal_calculation=True 但未通过准入 —— 调用方必须拒绝（HTTP 422），不得出数
      test_only  结果是否必须标记为测试结果（非正式请求恒为 True）
    """
    requested = bool(formal_calculation)
    adm = _no_basis_admission(rule) if basis is None else formal_admission(rule, basis, source_refs)
    admitted = bool(requested and adm["formal_ready"])
    adm["requested_formal"] = requested
    adm["admitted"] = admitted
    adm["rejected"] = bool(requested and not adm["formal_ready"])
    adm["test_only"] = True if not admitted else False
    return adm


def admission_issues(admission: dict[str, Any]) -> list[str]:
    """把准入原因压平成字符串列表，供 HTTP 422 的 issues 字段使用。"""
    return [str(r.get("detail", "")) for r in admission.get("admission_reasons") or []]
