"""定额匹配与附注执行（quota 引擎对外入口）。

P0-01-NUMERIC：
- 入口先对携带的工程量/消耗量/单价字段做数值守卫（NaN/Infinity/1e999/超精度 → NUMERIC_*）；
- 附注系数 factor 必须是正数，且“消耗量 × 系数”的结果要过量级守卫，
  不允许把 1e999、NaN 直接写回 consumption（历史上会静默产出 "Infinity"）。
"""
from difflib import SequenceMatcher
from decimal import Decimal

from .numeric import NumericError, ensure_finite, to_decimal

# 匹配/附注入口中可能携带数值的业务字段：先守卫再进入业务逻辑。
BOQ_NUMERIC_FIELDS = ("quantity", "qty", "consumption", "unit_price")
QUOTA_NUMERIC_FIELDS = ("quantity", "qty", "consumption", "unit_price", "amount")


def _guard_numeric_fields(payload: dict, keys, prefix: str) -> None:
    if not isinstance(payload, dict):
        raise NumericError("NUMERIC_INVALID", f"{prefix} 必须是对象", prefix)
    for key in keys:
        if key not in payload:
            continue
        value = payload[key]
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        to_decimal(value, f"{prefix}.{key}", allow_negative=True)


def norm(s): return "".join(str(s or "").lower().split()).replace("（", "(").replace("）", ")")
def name_score(a, b): return SequenceMatcher(None, norm(a), norm(b)).ratio()
def tag_score(a, b):
    sa, sb = set(a or []), set(b or [])
    # 分母用 max(1, ...) 保护：标签全空时不得触发除零
    return len(sa & sb) / max(1, len(sa | sb)) if sa or sb else 0.0


def match_quota(boq_item, quota_items, exact_mappings=None):
    _guard_numeric_fields(boq_item or {}, BOQ_NUMERIC_FIELDS, "boq_item")
    for index, item in enumerate(quota_items or []):
        _guard_numeric_fields(item or {}, QUOTA_NUMERIC_FIELDS, f"quota_items[{index}]")
    exact_mappings = exact_mappings or {}; code = str(boq_item.get("code", "")); mapped = exact_mappings.get(code); out = []
    for item in quota_items:
        reasons = []; excluded = False
        if "FORBID" in item.get("note_effects", []): excluded = True; reasons.append("存在 FORBID 附注")
        if not item.get("source_ref"): excluded = True; reasons.append("缺少定额来源")
        if item.get("license_status", "UNKNOWN") not in {"PUBLIC", "LICENSED", "TEST_ONLY"}: excluded = True; reasons.append("授权状态未确认")
        if item.get("status", "ACTIVE") != "ACTIVE": excluded = True; reasons.append("定额子目非有效状态")
        exact = mapped is not None and str(item.get("item_code")) == str(mapped)
        code_match = bool(code and code == str(item.get("mapped_boq_code", "")))
        major = boq_item.get("major") == item.get("major"); unit = boq_item.get("unit") == item.get("unit")
        ns = name_score(boq_item.get("name", ""), item.get("name", "")); ts = tag_score(boq_item.get("tags", []), item.get("work_tags", []))
        score = 1.0 if exact else .30 * ns + .25 * ts + .20 * float(major) + .15 * float(unit) + .10 * float(code_match)
        if exact: reasons.append("命中精确映射")
        else:
            if ns >= .75: reasons.append("名称相似")
            if ts > 0: reasons.append("工作内容标签重合")
            if major: reasons.append("专业一致")
            if unit: reasons.append("单位一致")
        out.append({"quota_item_id": item.get("id"), "item_code": item.get("item_code"), "name": item.get("name"), "unit": item.get("unit"), "score": round(score, 4), "reasons": reasons, "excluded": excluded, "source_ref": item.get("source_ref")})
    eligible = sorted([x for x in out if not x["excluded"]], key=lambda x: x["score"], reverse=True)
    recommended = None
    if eligible and eligible[0]["score"] >= .85:
        margin = eligible[0]["score"] - (eligible[1]["score"] if len(eligible) > 1 else 0)
        if margin >= .08: recommended = eligible[0]["quota_item_id"]
    return {"candidates": sorted(out, key=lambda x: x["score"], reverse=True), "recommended_id": recommended, "need_human": True, "warning": "候选匹配未经专业人员确认不得进入正式组价。"}


SCOPE_ORDER = {"BOOK": 1, "CHAPTER": 2, "ITEM": 3, "CONVERSION": 4}


def apply_quota_notes(consumption: list[dict], notes: list[dict], conversion: dict | None = None) -> dict:
    """按册→章→子目→换算顺序执行附注；所有效果必须有来源。"""
    if not isinstance(consumption, (list, tuple)):
        raise NumericError("NUMERIC_INVALID", "consumption 必须是数组", "consumption")
    rows = [dict(x) for x in consumption]
    for index, row in enumerate(rows):
        if "consumption" in row:
            to_decimal(row["consumption"], f"consumption[{index}]", allow_negative=True)
    trace = []
    ordered = sorted(notes, key=lambda n: (SCOPE_ORDER.get(n.get("scope_level"), 99), int(n.get("priority", 100)), str(n.get("id", ""))))
    if conversion is not None and conversion.get("allowed") is False:
        raise ValueError("当前换算不允许执行")
    for note in ordered:
        effect = note.get("effect_type")
        if not note.get("source_ref"):
            raise ValueError("定额附注缺少来源，不能执行")
        if effect == "FORBID":
            raise ValueError(f"定额附注禁止套用：{note.get('note_text', '')}")
        if effect == "FACTOR":
            factor = to_decimal(note.get("factor"), "factor", allow_negative=False, allow_zero=False)
            target = note.get("target_resource_code")
            matched = [row for row in rows if target is None or row.get("resource_code") == target]
            if not matched: raise ValueError("系数附注未匹配到目标资源")
            for row in matched:
                base = to_decimal(row.get("consumption"), "consumption", allow_negative=True)
                row["consumption"] = str(ensure_finite(base * factor, "consumption"))
            trace.append({"effect": effect, "factor": str(factor), "target": target, "source_ref": note["source_ref"]})
        elif effect == "REPLACE":
            old, new = note.get("target_resource_code"), note.get("replacement_resource_code")
            if not old or not new or not note.get("replacement_source_ref"):
                raise ValueError("资源替换附注缺少旧资源、新资源或来源")
            matched = [row for row in rows if row.get("resource_code") == old]
            if not matched: raise ValueError("资源替换附注未匹配到目标资源")
            for row in matched:
                row["resource_code"] = new
                row["replacement_source_ref"] = note["replacement_source_ref"]
            trace.append({"effect": effect, "from": old, "to": new, "source_ref": note["source_ref"]})
        elif effect == "INFO":
            trace.append({"effect": effect, "note_text": note.get("note_text", ""), "source_ref": note["source_ref"]})
        else:
            raise ValueError(f"未知附注 effect_type：{effect}")
    return {"consumption": rows, "execution_trace": trace, "note_order": [n.get("id") for n in ordered]}
