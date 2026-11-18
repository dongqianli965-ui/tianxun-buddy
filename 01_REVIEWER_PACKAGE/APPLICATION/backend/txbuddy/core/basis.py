from datetime import date
from ..models import BasisPack
HENAN_NEW_EFFECTIVE = date(2025, 9, 1)

def validate_basis_pack(pack: BasisPack) -> dict:
    issues, warnings = [], []
    if not pack.measurement_standard_version or pack.measurement_standard_version.upper() in {"TBD", "UNKNOWN"}:
        issues.append("measurement_standard_version 缺失")
    if not pack.pricing_standard_version or pack.pricing_standard_version.upper() in {"TBD", "UNKNOWN"}:
        issues.append("pricing_standard_version 缺失")
    if not pack.precision_rule_version: issues.append("precision_rule_version 缺失")
    if not pack.region_code: issues.append("region_code 缺失")
    if pack.rule_version_scope == "HENAN_2025" and pack.effective_date < HENAN_NEW_EFFECTIVE:
        issues.append("HENAN_2025 与项目适用日期冲突，需人工核定跨期规则")
    if pack.rule_version_scope == "HENAN_LEGACY" and pack.effective_date >= HENAN_NEW_EFFECTIVE:
        warnings.append("2025-09-01 后项目选择旧版规则，必须有招标文件/合同依据和人工批准")
    if pack.status == "LOCKED":
        if not pack.sources: issues.append("LOCKED 依据包没有来源记录")
        for src in pack.sources:
            if not src.verified: issues.append(f"来源未核验：{src.source_id}")
            if src.license_status in {"PENDING", "UNKNOWN"}: issues.append(f"来源授权未确认：{src.source_id}")
            if src.license_status == "TEST_ONLY" and not pack.test_only: issues.append(f"测试来源不能进入正式依据包：{src.source_id}")
    formal_ready = pack.status == "LOCKED" and not issues and not pack.test_only
    return {"valid": not issues, "formal_ready": formal_ready, "issues": issues, "warnings": warnings,
            "basis_pack_id": pack.basis_pack_id, "rule_version_scope": pack.rule_version_scope}
