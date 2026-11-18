from datetime import date
from decimal import Decimal
from txbuddy.models import BasisPack,FeeItem
from txbuddy.core.quantity import calculate_quantity
from txbuddy.core.pricing import index_adjustment,project_total,compose_unit_price
from txbuddy.core.basis import validate_basis_pack
from txbuddy.core.quota import match_quota,apply_quota_notes
from txbuddy.core.evidence import build_evidence,verify_evidence

def pack():
 return BasisPack(basis_pack_id="B-TEST",region_code="410100",region_level="CITY",major="BUILDING",effective_date=date(2026,1,1),measurement_standard_version="GB/T50854-2024",pricing_standard_version="GB/T50500-2024",rule_version_scope="HENAN_2025",precision_rule_version="P-TEST",status="DRAFT",test_only=True)
def test_column_volume():
 assert calculate_quantity("RECTANGULAR_VOLUME",{"length":"0.5","width":"0.5","height":"3"})["value"]==Decimal("0.750")
def test_slab_volume():
 assert calculate_quantity("RECTANGULAR_VOLUME",{"length":"8.1","width":"6","height":"0.12","deductions":["0.12"]})["value"]==Decimal("5.71200")
def test_negative_quantity_blocked():
 try: calculate_quantity("NET_VALUE",{"gross":"1","deductions":["2"]}); assert False
 except ValueError: assert True
def test_basis_missing_standard():
 p=pack(); p.measurement_standard_version="TBD"
 assert not validate_basis_pack(p)["valid"]
def test_index_modes():
 assert index_adjustment("1000000","100","110","0.05","WITHIN_BAND_ONLY")["delta_amount"]==Decimal("50000")
 assert index_adjustment("1000000","100","110","0.05","FULL_EXCESS")["delta_amount"]==Decimal("100000")
 assert index_adjustment("1000000","100","110","0.05","PASS_THROUGH")["delta_amount"]==Decimal("100000")
def test_henan_2025_blocks_regulatory_fee():
 try: project_total(pack(),"100","10","0",[FeeItem(fee_item="REGULATORY_FEE",amount=Decimal("2"))],None,"0.09",False); assert False
 except ValueError as e: assert "不得单列规费" in str(e)
def test_safety_fee_noncompetitive():
 try: project_total(pack(),"100","10","0",[],FeeItem(fee_item="SAFETY_PRODUCTION_FEE",amount=Decimal("3"),is_noncompetitive=False),"0.09",False); assert False
 except ValueError as e: assert "不可竞争" in str(e)
def test_quota_match_requires_human():
 r=match_quota({"code":"A1","name":"现浇混凝土矩形柱","unit":"m3","major":"BUILDING","tags":["混凝土","柱"]},[{"id":"Q1","item_code":"Q1","mapped_boq_code":"A1","name":"现浇混凝土矩形柱","unit":"m3","major":"BUILDING","work_tags":["混凝土","柱"],"source_ref":"TEST_ONLY","license_status":"TEST_ONLY","status":"ACTIVE","note_effects":[]}])
 assert r["recommended_id"]=="Q1" and r["need_human"]
def test_evidence_tamper():
 e=build_evidence(subject_type="QTY",subject_id="R1",result=Decimal("0.75"),unit="m3",formula="a*b*c",intermediate_values={"a":Decimal("0.5")},input_snapshot={"a":"0.5"},basis_pack_id="B1",rule_version="R1",precision_rule_version="P1",source_refs=["TEST_ONLY"],producer="ENGINE")
 assert verify_evidence(e)["valid"]
 e["result"]="999"; assert not verify_evidence(e)["valid"]


def test_quota_note_factor_and_forbid():
    r=apply_quota_notes([{"resource_code":"R1","consumption":"2"}],[{"id":"N1","scope_level":"ITEM","effect_type":"FACTOR","factor":"1.5","target_resource_code":"R1","source_ref":"TEST_ONLY"}])
    assert r["consumption"][0]["consumption"]=="3.0"
    try: apply_quota_notes([], [{"scope_level":"ITEM","effect_type":"FORBID","source_ref":"TEST_ONLY"}]); assert False
    except ValueError as e: assert "禁止套用" in str(e)

def test_unit_price_composition_and_risk_double_count():
    r=compose_unit_price([{"resource_code":"MAT1","resource_type":"MATERIAL","consumption":"2","unit_price":"10","price_ref":"TEST"}],"0.1","0.05",management_source_ref="TEST-MGMT",profit_source_ref="TEST-PROFIT")
    assert r["unit_price"]==Decimal("23.10")
    try: compose_unit_price([{"resource_code":"MAT1","resource_type":"MATERIAL","consumption":"2","unit_price":"10","price_ref":"TEST"}],"0.1","0.05",adjusted_resource_codes=["MAT1"],risk_resource_codes=["MAT1"],management_source_ref="TEST-MGMT",profit_source_ref="TEST-PROFIT"); assert False
    except ValueError as e: assert "不得重复" in str(e)
