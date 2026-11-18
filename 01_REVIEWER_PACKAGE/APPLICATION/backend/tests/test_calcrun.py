from datetime import date
from decimal import Decimal
import pytest
from txbuddy.core.calcrun import build_snapshot, verify_snapshot, recalc, values_match
from txbuddy.core.rules import RuleRegistry

RULES = "data/rules_test_only.json"
SNAP_ARGS = dict(rule_code="QTY.BOQ.MASONRY.WALL.001", rule_version="2026.1", basis_pack_id="B-TEST",
                 precision_rule_version="P-TEST", unit="m3",
                 params={"L": "6", "H": "3", "T": "0.24", "opening_area": "2.1"})

def test_snapshot_hash_is_deterministic():
    a = build_snapshot(**SNAP_ARGS)
    b = build_snapshot(**SNAP_ARGS)
    assert a["snapshot_sha256"] == b["snapshot_sha256"]
    assert verify_snapshot(a)["valid"] is True

def test_snapshot_tamper_detected():
    s = build_snapshot(**SNAP_ARGS)
    s["params"]["T"] = "0.30"
    out = verify_snapshot(s)
    assert out["valid"] is False and out["reason"] == "SNAPSHOT_TAMPERED"

def test_snapshot_missing_field():
    s = build_snapshot(**SNAP_ARGS)
    s.pop("basis_pack_id")
    out = verify_snapshot(s)
    assert out["valid"] is False and "basis_pack_id" in out["missing"]

def test_recalc_consistent_and_inconsistent():
    s = build_snapshot(**SNAP_ARGS)
    ok = recalc(RuleRegistry(RULES), s, "3.816")
    assert ok["reproducible"] is True and ok["comparison"]["consistent"] is True
    bad = recalc(RuleRegistry(RULES), s, "4.000")
    assert bad["reproducible"] is False
    assert bad["comparison"]["reason"] == "RECALC_MISMATCH"

def test_tolerance_band_per_a40():
    hit = values_match("100.000", "100.010")
    assert hit["consistent"] is True          # 绝对容差 0.01 边界
    miss = values_match("100.000", "100.020")
    assert miss["consistent"] is False
    rel = values_match("1000000", "1000001")  # 相对 1e-6 内
    assert rel["consistent"] is True

def test_recalc_reports_rule_and_evidence_payload():
    s = build_snapshot(**SNAP_ARGS)
    out = recalc(RuleRegistry(RULES), s, "3.816")
    assert out["evidence_payload"]["rule_code"] == SNAP_ARGS["rule_code"]
    assert out["evidence_payload"]["formula"]
    assert out["evidence_payload"]["standard_ref"]
