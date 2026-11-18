import json
from decimal import Decimal
import pytest
from txbuddy.core.rules import RuleRegistry, RuleError, parse_unit, unit_str

RULES = "data/rules_test_only.json"

def reg():
    return RuleRegistry(RULES)

def test_registry_selftest_all_rules():
    r = reg().validate()
    assert r["ok"], r["failed"]
    assert r["rules"] == 6 and r["cases"] == 6

def test_wall_rule_value_and_evidence_shape():
    out = reg().execute("QTY.BOQ.MASONRY.WALL.001", {"L": "6", "H": "3", "T": "0.24", "opening_area": "2.1"})
    assert out["value"] == "3.816"
    assert out["unit"] == "m3"
    assert len(out["intermediate_values"]) >= 4
    assert out["test_only"] is True
    assert "待核" in out["standard_ref"]

def test_column_slab_rebar_formwork_earth():
    r = reg()
    assert r.execute("QTY.BOQ.CONC.COLUMN.001", {"b": "0.5", "h": "0.5", "H": "3"})["value"] == "0.750"
    assert r.execute("QTY.BOQ.CONC.SLAB.001", {"A": "50", "T": "0.12", "deduction": "0.12"})["value"] == "5.880"
    assert r.execute("QTY.BOQ.REBAR.WEIGHT.001", {"length": "12", "count": "20", "unit_weight": "0.88848"})["value"] == "213.235"
    assert r.execute("QTY.BOQ.CONC.FORMWORK.SLAB.001", {"A": "48.6", "perimeter": "28.2", "T": "0.12"})["value"] == "51.98"
    assert r.execute("QTY.BOQ.EARTH.EXC.001", {"a": "10", "b": "8", "H": "2", "K": "0.33"})["value"] == "184.922"

def test_missing_and_unknown_params():
    with pytest.raises(RuleError) as e:
        reg().execute("QTY.BOQ.MASONRY.WALL.001", {"L": "6", "H": "3", "T": "0.24"})
    assert e.value.code == "CALC_PARAM_MISSING"
    with pytest.raises(RuleError) as e:
        reg().execute("QTY.BOQ.MASONRY.WALL.001", {"L": "6", "H": "3", "T": "0.24", "opening_area": "2", "x": "1"})
    assert e.value.code == "CALC_PARAM_UNKNOWN"

def test_unit_dimension_mismatch_blocked():
    r = RuleRegistry()
    r.register({"rule_code": "T.BAD", "version": "1", "unit": "m3", "expression": "L * H",
                "variables": {"L": "m", "H": "m"}, "standard_ref": "TEST", "license_status": "TEST_ONLY"})
    with pytest.raises(RuleError) as e:
        r.execute("T.BAD", {"L": "2", "H": "3"})
    assert e.value.code == "CALC_UNIT_MISMATCH"

def test_addition_with_different_units_blocked():
    r = RuleRegistry()
    r.register({"rule_code": "T.ADD", "version": "1", "unit": "m2", "expression": "A + L",
                "variables": {"A": "m2", "L": "m"}, "standard_ref": "TEST", "license_status": "TEST_ONLY"})
    with pytest.raises(RuleError) as e:
        r.execute("T.ADD", {"A": "1", "L": "2"})
    assert e.value.code == "CALC_UNIT_MISMATCH"

def test_unsafe_expression_rejected():
    r = RuleRegistry()
    r.register({"rule_code": "T.ATTR", "version": "1", "unit": "unitless", "expression": "L.__class__",
                "variables": {"L": "unitless"}, "standard_ref": "TEST", "license_status": "TEST_ONLY"})
    with pytest.raises(RuleError) as e:
        r.execute("T.ATTR", {"L": "1"})
    assert e.value.code == "CALC_EXPR_SYNTAX"

def test_unknown_function_and_div_zero():
    r = RuleRegistry()
    r.register({"rule_code": "T.F", "version": "1", "unit": "unitless", "expression": "EVIL(L)",
                "variables": {"L": "unitless"}, "standard_ref": "TEST", "license_status": "TEST_ONLY"})
    with pytest.raises(RuleError) as e:
        r.execute("T.F", {"L": "1"})
    assert e.value.code == "CALC_EXPR_SYNTAX"
    r2 = RuleRegistry()
    r2.register({"rule_code": "T.D", "version": "1", "unit": "unitless", "expression": "L / 0",
                 "variables": {"L": "unitless"}, "standard_ref": "TEST", "license_status": "TEST_ONLY"})
    with pytest.raises(RuleError) as e:
        r2.execute("T.D", {"L": "1"})
    assert e.value.code == "CALC_DIV_ZERO"

def test_rule_lookup_conflicts():
    r = RuleRegistry()
    base = {"rule_code": "T.M", "unit": "unitless", "expression": "L", "variables": {"L": "unitless"},
            "standard_ref": "TEST", "license_status": "TEST_ONLY"}
    r.register({**base, "version": "1"})
    r.register({**base, "version": "2"})
    with pytest.raises(RuleError) as e:
        r.get("T.M")
    assert e.value.code == "CALC_RULE_CONFLICT"
    assert r.get("T.M", "2")["version"] == "2"
    with pytest.raises(RuleError) as e:
        r.get("T.NOPE")
    assert e.value.code == "CALC_RULE_NOT_FOUND"

def test_license_status_enforced():
    r = RuleRegistry()
    with pytest.raises(RuleError) as e:
        r.register({"rule_code": "T.L", "version": "1", "unit": "unitless", "expression": "1",
                    "variables": {}, "standard_ref": "TEST", "license_status": "UNKNOWN"})
    assert e.value.code == "BASIS_ITEM_MISSING"

def test_unit_parse_and_str():
    assert parse_unit("m3") == {"m": 3}
    assert parse_unit("kg/m") == {"kg": 1, "m": -1}
    assert unit_str({"m": 2}) == "m2"
