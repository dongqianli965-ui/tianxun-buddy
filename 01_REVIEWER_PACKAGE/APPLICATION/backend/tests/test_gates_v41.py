from datetime import date
import pytest
from txbuddy.core.gates import check_gate, GATE_SEQUENCE
from txbuddy.models import BasisPack

def locked_pack():
    return BasisPack(basis_pack_id="B-LOCK", region_code="410100", region_level="CITY", major="BUILDING",
                     effective_date=date(2026, 1, 1), measurement_standard_version="GB/T 50854-2024",
                     pricing_standard_version="GB/T 50500-2024", rule_version_scope="HENAN_2025",
                     precision_rule_version="P-TEST", status="LOCKED",
                     sources=[{"source_id": "S1", "title": "河南定额", "source_type": "QUOTA",
                               "license_status": "LICENSED", "verified": True}])

def test_g0_blocks_unlocked_test_pack():
    pack = locked_pack(); pack.status = "DRAFT"; pack.test_only = True
    r = check_gate("L1", "G0", set(), pack)
    assert r["result"] == "FAIL"
    assert any(f["error_code"] in {"BASIS_PACK_MISSING", "INTAKE_GATE_NOT_READY"} for f in r["failed_rules"])

def test_g0_pass_when_locked_and_verified():
    r = check_gate("L1", "G0", set(), locked_pack())
    assert r["result"] == "PASS"

def test_g1_missing_docs_and_boq():
    r = check_gate("L1", "G1", {"drawing_set"}, locked_pack(), {"boq_required": True, "boq_imported": False})
    assert r["result"] == "FAIL"
    assert r["missing_documents"]
    assert any(f["rule_id"] == "G1.BOQ" for f in r["failed_rules"])

def test_g2_requires_qty_reconcile_and_explained_diff():
    base = {"qty_items": [{"status": "DONE"}], "reconciled": True, "unexplained_diffs": 0}
    assert check_gate("L1", "G2", set(), locked_pack(), base)["result"] == "PASS"
    bad = dict(base, unexplained_diffs=2)
    r = check_gate("L1", "G2", set(), locked_pack(), bad)
    assert r["result"] == "FAIL"
    assert any(f["error_code"] == "GATE_DIFF_UNEXPLAINED" for f in r["failed_rules"])

def test_g3a_requires_price_source():
    ok = {"price_items": [{"source_ref": "Q-1"}]}
    assert check_gate("L1", "G3a", set(), locked_pack(), ok)["result"] == "PASS"
    bad = {"price_items": [{"source_ref": ""}]}
    r = check_gate("L1", "G3a", set(), locked_pack(), bad)
    assert r["result"] == "FAIL" and r["failed_rules"]

def test_g3b_review_sequence():
    ok = {"review_levels": ["SELF", "REVIEW", "APPROVE"], "open_review_issues": 0}
    assert check_gate("L1", "G3b", set(), locked_pack(), ok)["result"] == "PASS"
    bad = {"review_levels": ["REVIEW", "SELF", "APPROVE"]}
    r = check_gate("L1", "G3b", set(), locked_pack(), bad)
    assert any(f["error_code"] == "REVIEW_SEQUENCE_INVALID" for f in r["failed_rules"])

def test_g3c_seal_requires_registered_engineer():
    bad = {"seal": {"signed_by": "u1", "license": "NONE"}}
    r = check_gate("L1", "G3c", set(), locked_pack(), bad)
    assert any(f["error_code"] == "SEAL_NOT_ALLOWED" for f in r["failed_rules"])
    ok = {"seal": {"signed_by": "u1", "license": "REGISTERED_COST_ENGINEER"}}
    assert check_gate("L1", "G3c", set(), locked_pack(), ok)["result"] == "PASS"

def test_g4_evidence_coverage_and_ai_direct_number():
    ok = {"report": {"generated": True, "layout_checked": True}, "evidence_coverage": 1}
    assert check_gate("L1", "G4", set(), locked_pack(), ok)["result"] == "PASS"
    bad = {"report": {"generated": True, "layout_checked": True}, "evidence_coverage": 0.98}
    assert check_gate("L1", "G4", set(), locked_pack(), bad)["result"] == "FAIL"
    ai = dict(ok, ai_direct_numbers=1)
    r = check_gate("L1", "G4", set(), locked_pack(), ai)
    assert any(f["error_code"] == "AI_DIRECT_NUMBER_FORBIDDEN" for f in r["failed_rules"])

def test_g5_archive_and_closure():
    ok = {"archive": {"volume_complete": True, "package_sha256": "abc", "indicators_stored": True}}
    assert check_gate("L1", "G5", set(), locked_pack(), ok)["result"] == "PASS"
    bad = {"archive": {"volume_complete": True, "package_sha256": "abc", "indicators_stored": False}}
    r = check_gate("L1", "G5", set(), locked_pack(), bad)
    assert any(f["error_code"] == "CLOSURE_GATE_NOT_READY" for f in r["failed_rules"])

def test_j_line_gates():
    docs = {"authorized_entrustment", "conflict_declaration", "qualification"}
    assert check_gate("L6", "J0", docs, locked_pack())["result"] == "PASS"
    r = check_gate("L6", "J1", {"material_ledger"}, locked_pack())
    assert any(f["error_code"] == "JD_MATERIAL_UNVERIFIED" for f in r["failed_rules"])
    r2 = check_gate("L6", "J4", {"calc_book", "review_record"}, locked_pack(),
                    {"opinion_form": "DEFINITE", "evidence_level": "LOW"})
    assert any(f["error_code"] == "JD_OPINION_INCOMPLETE" for f in r2["failed_rules"])

def test_gate_sequence_defined_for_all_lines():
    for line in ["L1", "L2", "L3", "L4", "L5", "L6"]:
        assert GATE_SEQUENCE[line][0] in {"G0", "J0"}
