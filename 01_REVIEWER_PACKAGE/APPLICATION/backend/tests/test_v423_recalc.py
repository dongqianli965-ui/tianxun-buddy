"""P0-07-RECALC：历史复算可靠性（快照可复现、篡改可检出）。

覆盖两条硬约束：
1. 任意历史 calc_run 按快照重跑引擎，结果落在 A-40 容差内（绝对 0.01 / 相对 1e-6）；
2. 快照被改动必须检出并拒绝——包括“改参数后顺手重算内嵌哈希”的重新盖章攻击，
   靠持久化指纹（calc_run.snapshot_sha256）挡住。
"""
from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest
from tests.authenticated_client import TestClient

from txbuddy.api import app, registry, store
from txbuddy.core.calcrun import (DIGEST_FIELD, build_snapshot, recalc, recalc_record,
                                  snapshot_hash, values_match, verify_snapshot)
from txbuddy.core.rules import RuleError, RuleRegistry

client = TestClient(app)

PACK = dict(basis_pack_id="B-RECALC", region_code="410100", region_level="CITY", major="BUILDING",
            effective_date="2026-01-01", measurement_standard_version="GB/T 50854-2024",
            pricing_standard_version="GB/T 50500-2024", rule_version_scope="HENAN_2025",
            precision_rule_version="P-RECALC", status="DRAFT", test_only=True)

WALL = dict(rule_code="QTY.BOQ.MASONRY.WALL.001", rule_version="2026.1", basis_pack_id="B-RECALC",
            precision_rule_version="P-RECALC", unit="m3",
            params={"L": "6", "H": "3", "T": "0.24", "opening_area": "2.1"})
WALL_RESULT = "3.816"

COLUMN = dict(rule_code="QTY.BOQ.CONC.COLUMN.001", rule_version="2026.1", basis_pack_id="B-RECALC",
              precision_rule_version="P-RECALC", unit="m3", params={"b": "0.5", "h": "0.5", "H": "3"})
COLUMN_RESULT = "0.750"


# ------------------------------------------------------------------ 工具
def reseal(snapshot: dict) -> dict:
    """重新计算内嵌哈希：模拟攻击者改完参数后“重新盖章”。"""
    out = copy.deepcopy(snapshot)
    out[DIGEST_FIELD] = snapshot_hash(out)
    return out


class SpyRegistry:
    """记录引擎被调用次数：被拒的快照绝不允许真正跑引擎。"""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def execute(self, rule_code, params, version):
        self.calls += 1
        return self.inner.execute(rule_code, params, version)


class StubRegistry:
    """可控回归桩：返回与快照不一致的版本/量纲，验证复算前置契约。"""

    def __init__(self, *, version: str = "2026.1", unit: str = "m3", value: str = WALL_RESULT):
        self.version, self.unit, self.value, self.calls = version, unit, value, 0

    def execute(self, rule_code, params, version):
        self.calls += 1
        return {"rule_code": rule_code, "rule_version": self.version, "unit": self.unit,
                "value": self.value, "formula": "STUB", "intermediate_values": [],
                "standard_ref": "TEST-ONLY"}


def api_calc(rule: dict, params: dict) -> dict:
    response = client.post("/v1/quantity/rule-calc",
                           json={"basis_pack": PACK, "rule_code": rule["rule_code"], "params": params})
    assert response.status_code == 200, response.text[:300]
    return response.json()


# ------------------------------------------------------------------ 1 历史复算一致
@pytest.mark.parametrize("rule", registry.all(), ids=lambda r: r["rule_code"])
def test_every_rule_version_reproduces_its_historical_result(rule):
    """注册表里每条 rule_version 都必须能从快照复算出与存档一致的结果。"""
    case = rule["tests"][0]
    snapshot = build_snapshot(rule_code=rule["rule_code"], rule_version=rule["version"],
                              basis_pack_id="B-RECALC", precision_rule_version="P-RECALC",
                              params=case["params"], unit=rule["unit"],
                              source_refs=[rule["standard_ref"]])
    out = recalc(registry, snapshot, case["expect"])
    assert out["reproducible"] is True, out
    assert out["rejected"] is False and out["stage"] == "RECALC"
    assert Decimal(out["recomputed_value"]) == Decimal(case["expect"])
    assert out["snapshot_sha256"] == snapshot[DIGEST_FIELD]
    assert out["comparison"]["delta_abs"] == "0.000" or Decimal(out["comparison"]["delta_abs"]) == 0


def test_historical_calc_run_from_store_recalculates_identically():
    """真实链路：API 算一次 → 从 calc_run 表读回快照 → 复算结果必须逐位一致。"""
    calc = api_calc(COLUMN, COLUMN["params"])
    run = store.get_calc_run(calc["calc_run_id"])
    assert run and run["snapshot"] and run["result"] == COLUMN_RESULT

    out = recalc_record(registry, run)
    assert out["reproducible"] is True, out
    assert out["calc_run_id"] == calc["calc_run_id"]
    assert out["recomputed_value"] == COLUMN_RESULT
    assert out["snapshot_sha256"] == run["snapshot_sha256"] == calc["snapshot_sha256"]
    assert out["record_mismatch"] == []
    assert out["evidence_payload"]["formula"] == "b * h * H"
    assert out["engine_build_match"] is True and out["warnings"] == []


def test_snapshot_survives_json_roundtrip_through_storage():
    """快照以 JSON 文本落库再读回，哈希与复算结论都不得漂移。"""
    snapshot = build_snapshot(**WALL)
    restored = json.loads(json.dumps(snapshot, ensure_ascii=False))
    assert restored == snapshot
    assert snapshot_hash(restored) == snapshot_hash(snapshot)
    assert verify_snapshot(restored)["valid"] is True
    assert recalc(registry, restored, WALL_RESULT)["reproducible"] is True


def test_legacy_snapshot_without_schema_marker_still_verifies():
    """升级前入库的历史快照没有 snapshot_schema 字段，仍必须能校验与复算（不得强制迁移）。"""
    snapshot = build_snapshot(**WALL)
    legacy = reseal({k: v for k, v in snapshot.items() if k != "snapshot_schema"})
    assert "snapshot_schema" not in legacy

    check = verify_snapshot(legacy)
    assert check["valid"] is True and check["reason"] == "OK"
    out = recalc(registry, legacy, WALL_RESULT)
    assert out["reproducible"] is True and out["recomputed_value"] == WALL_RESULT


def test_recalc_is_deterministic_across_repeated_runs():
    """同一快照重复复算必须给出完全相同的数值与中间量（不得引入随机/时间因素）。"""
    snapshot = build_snapshot(**COLUMN)
    first = recalc(registry, snapshot, COLUMN_RESULT)
    for _ in range(4):
        again = recalc(registry, snapshot, COLUMN_RESULT)
        assert again["recomputed_value"] == first["recomputed_value"]
        assert again["comparison"] == first["comparison"]
        assert again["evidence_payload"] == first["evidence_payload"]
        assert again["snapshot_sha256"] == first["snapshot_sha256"]


def test_recalc_accepts_result_within_a40_tolerance():
    snapshot = build_snapshot(**WALL)
    assert recalc(registry, snapshot, "3.821")["reproducible"] is True   # 差 0.005，容差内
    assert recalc(registry, snapshot, "3.811")["reproducible"] is True   # 差 -0.005
    beyond = recalc(registry, snapshot, "3.830")                          # 差 0.014，超差
    assert beyond["reproducible"] is False and beyond["rejected"] is False
    assert beyond["comparison"]["reason"] == "RECALC_MISMATCH"


def test_constant_rule_with_empty_params_is_reproducible():
    """无变量规则（params={}）是合法快照：空 params 不得被误判为缺字段。"""
    local = RuleRegistry()
    local.register({"rule_code": "QTY.TEST.CONST.001", "version": "1", "unit": "unitless",
                    "expression": "2", "variables": {}, "standard_ref": "TEST-ONLY",
                    "license_status": "TEST_ONLY"})
    snapshot = build_snapshot(rule_code="QTY.TEST.CONST.001", rule_version="1", basis_pack_id="B-RECALC",
                              precision_rule_version="P-RECALC", params={}, unit="unitless")
    assert verify_snapshot(snapshot)["valid"] is True
    out = recalc(local, snapshot, "2.000000")
    assert out["reproducible"] is True and Decimal(out["recomputed_value"]) == Decimal(2)


# ------------------------------------------------------------------ 2 篡改检出与拒绝
def test_tampered_snapshot_is_detected_and_engine_never_runs():
    snapshot = build_snapshot(**WALL)
    tampered = copy.deepcopy(snapshot)
    tampered["params"]["T"] = "0.30"          # 故意改参数，不重算哈希

    check = verify_snapshot(tampered)
    assert check["valid"] is False
    assert check["reason"] == "SNAPSHOT_TAMPERED"
    assert check["actual_sha256"] != check["expected_sha256"]

    spy = SpyRegistry(registry)
    out = recalc(spy, tampered, WALL_RESULT)
    assert out["reproducible"] is False and out["rejected"] is True and out["stage"] == "SNAPSHOT"
    assert out["reason"] == "SNAPSHOT_TAMPERED"
    assert "recomputed_value" not in out, "被拒快照不得给出复算值"
    assert spy.calls == 0, "被拒快照不得真正执行引擎"


def test_resealed_tampered_snapshot_fails_against_persisted_fingerprint():
    """高级攻击：改参数并重算内嵌哈希。内嵌自洽，但持久化指纹仍能检出。"""
    snapshot = build_snapshot(**WALL)
    tampered = copy.deepcopy(snapshot)
    tampered["params"]["T"] = "0.30"
    forged = reseal(tampered)
    assert verify_snapshot(forged)["valid"] is True, "内嵌哈希被重新盖章后自洽（这正是风险点）"

    spy = SpyRegistry(registry)
    out = recalc(spy, forged, WALL_RESULT, expected_sha256=snapshot[DIGEST_FIELD])
    assert out["reproducible"] is False and out["rejected"] is True
    assert out["stage"] == "SNAPSHOT"
    assert out["reason"] == "SNAPSHOT_FINGERPRINT_MISMATCH"
    assert out["snapshot_fingerprint_checked"] is True
    assert out["snapshot_fingerprint_sha256"] == snapshot[DIGEST_FIELD]
    assert spy.calls == 0


def test_store_tamper_of_snapshot_payload_is_detected():
    """数据库里的快照 JSON 被改（指纹列保留），历史复算必须拒绝并指出不一致字段。"""
    calc = api_calc(COLUMN, COLUMN["params"])
    run = store.get_calc_run(calc["calc_run_id"])
    assert recalc_record(registry, run)["reproducible"] is True

    run["snapshot"]["params"]["b"] = "1"      # 只改库里快照负载
    store.save_calc_run(run)
    tampered = store.get_calc_run(calc["calc_run_id"])

    out = recalc_record(registry, tampered)
    assert out["reproducible"] is False and out["rejected"] is True
    assert out["stage"] == "SNAPSHOT"
    assert out["reason"] == "SNAPSHOT_TAMPERED"
    # 行内指纹列与内嵌哈希仍一致（攻击只改了负载），所以是内嵌哈希这一层检出。
    assert out["record_mismatch"] == []
    assert "recomputed_value" not in out


def test_resealed_store_snapshot_is_caught_by_row_fingerprint():
    """攻击者改了库里快照并重算内嵌哈希，但行内 snapshot_sha256 列仍是旧值 → 必须拒绝。"""
    calc = api_calc(COLUMN, COLUMN["params"])
    run = store.get_calc_run(calc["calc_run_id"])

    run["snapshot"]["params"]["b"] = "1"
    run["snapshot"] = reseal(run["snapshot"])
    store.save_calc_run(run)                  # 指纹列保持原值
    tampered = store.get_calc_run(calc["calc_run_id"])

    out = recalc_record(registry, tampered)
    assert out["reproducible"] is False and out["rejected"] is True
    assert out["stage"] == "SNAPSHOT"
    assert out["reason"] == "SNAPSHOT_FINGERPRINT_MISMATCH"
    assert out["record_mismatch"] == ["snapshot_sha256"]
    assert out["record_reason"] == "CALC_RUN_SNAPSHOT_MISMATCH"
    assert "recomputed_value" not in out


def test_row_and_snapshot_disagreement_is_rejected_at_record_stage():
    """记录行自报 rule_code 与快照不符（SQL 层面调包），即使快照完好也必须拒绝。"""
    calc = api_calc(COLUMN, COLUMN["params"])
    run = store.get_calc_run(calc["calc_run_id"])
    run["rule_code"] = "QTY.BOQ.MASONRY.WALL.001"
    store.save_calc_run(run)

    out = recalc_record(registry, store.get_calc_run(calc["calc_run_id"]))
    assert out["reproducible"] is False and out["rejected"] is True
    assert out["stage"] == "RECORD"
    assert out["reason"] == "CALC_RUN_SNAPSHOT_MISMATCH"
    assert out["record_mismatch"] == ["rule_code"]


def test_unsealed_snapshot_is_refused():
    snapshot = build_snapshot(**WALL)
    del snapshot[DIGEST_FIELD]
    check = verify_snapshot(snapshot)
    assert check["valid"] is False and check["reason"] == "SNAPSHOT_UNSEALED"
    assert recalc(registry, snapshot, WALL_RESULT)["rejected"] is True


def test_verify_snapshot_never_crashes_on_malformed_payloads():
    assert verify_snapshot(["not", "a", "dict"])["reason"] == "SNAPSHOT_INVALID"
    assert verify_snapshot({"rule_code": "X"})["reason"] == "SNAPSHOT_INCOMPLETE"

    malformed = build_snapshot(**WALL)
    malformed["params"] = [["L", "6"]]       # 字段类型被改坏
    malformed = reseal(malformed)
    check = verify_snapshot(malformed)
    assert check["valid"] is False and check["reason"] == "SNAPSHOT_MALFORMED"
    assert "params" in check["malformed"]

    unreadable = build_snapshot(**WALL)
    unreadable["extra"] = object()            # 不可 JSON 序列化
    check = verify_snapshot(unreadable)
    assert check["valid"] is False and check["reason"] == "SNAPSHOT_UNREADABLE"


def test_rule_version_and_unit_drift_are_rejected():
    snapshot = build_snapshot(**WALL)
    drift_version = recalc(StubRegistry(version="2027.1"), snapshot, WALL_RESULT)
    assert drift_version["reproducible"] is False and drift_version["rejected"] is True
    assert drift_version["stage"] == "RULE"
    assert drift_version["reason"] == "RECALC_RULE_VERSION_DRIFT"
    assert drift_version["recomputed_rule_version"] == "2027.1"

    drift_unit = recalc(StubRegistry(unit="m2"), snapshot, WALL_RESULT)
    assert drift_unit["reproducible"] is False and drift_unit["rejected"] is True
    assert drift_unit["stage"] == "UNIT"
    assert drift_unit["reason"] == "RECALC_UNIT_MISMATCH"
    assert drift_unit["recomputed_unit"] == "m2" and drift_unit["snapshot_unit"] == "m3"


def test_engine_build_drift_is_surfaced_not_hidden():
    """引擎构建号变化不影响数值复算，但必须在结论里显式提示，不得静默。"""
    snapshot = build_snapshot(**WALL)
    snapshot["engine_build"] = "txbuddy-core/0.1.0"
    snapshot = reseal(snapshot)
    out = recalc(registry, snapshot, WALL_RESULT)
    assert out["reproducible"] is True
    assert out["engine_build_match"] is False
    assert out["snapshot_engine_build"] == "txbuddy-core/0.1.0"
    assert [w["code"] for w in out["warnings"]] == ["ENGINE_BUILD_CHANGED"]


def test_unknown_rule_still_raises_business_error():
    """未知规则保持原有契约：抛 RuleError（HTTP 层映射 422），不得伪装成可复现。"""
    snapshot = build_snapshot(rule_code="NO.SUCH.RULE", rule_version="1", basis_pack_id="B-RECALC",
                              precision_rule_version="P-RECALC", params={"x": "1"}, unit="unitless")
    with pytest.raises(RuleError) as excinfo:
        recalc(registry, snapshot, "1")
    assert excinfo.value.code == "CALC_RULE_NOT_FOUND"


# ------------------------------------------------------------------ 3 A-40 容差口径
def test_values_match_boundaries():
    assert values_match("100.000", "100.010")["consistent"] is True     # 绝对容差边界
    assert values_match("100.000", "100.011")["consistent"] is False
    assert values_match("1000000", "1000001")["consistent"] is True     # 相对 1e-6 边界
    assert values_match("1000000", "1000002")["consistent"] is False
    assert values_match("-50.000", "-50.005")["consistent"] is True
    assert values_match("-50.000", "-50.020")["consistent"] is False
    assert values_match("0", "0.009")["consistent"] is True
    assert values_match("0", "0.020")["consistent"] is False
    assert values_match("3.816", "3.816")["reason"] == "OK"


def test_values_match_handles_extreme_ratio_without_crashing():
    """量级悬殊时 delta_rel 曾因 quantize 抛 InvalidOperation（500）；必须返回结构化结论。"""
    out = values_match("1e-30", "1")
    assert out["consistent"] is False and out["reason"] == "RECALC_MISMATCH"
    assert "E+30" in out["delta_rel"].upper()


# ------------------------------------------------------------------ 4 HTTP 契约回归
def test_api_recalc_uses_a40_tolerance_and_reports_fingerprint():
    calc = api_calc(COLUMN, COLUMN["params"])
    run = client.get(f"/v1/calc/{calc['calc_run_id']}").json()

    ok = client.post("/v1/calc/recalc", json={"basis_pack": PACK, "snapshot": run["snapshot"],
                                              "stored_value": "0.755"}).json()
    assert ok["reproducible"] is True and ok["rejected"] is False
    assert Decimal(ok["comparison"]["delta_abs"]) == Decimal("0.005")
    assert ok["snapshot_sha256"] == calc["snapshot_sha256"]

    bad = client.post("/v1/calc/recalc", json={"basis_pack": PACK, "snapshot": run["snapshot"],
                                               "stored_value": "0.900"}).json()
    assert bad["reproducible"] is False and bad["rejected"] is False
    assert bad["comparison"]["reason"] == "RECALC_MISMATCH"
    assert bad.get("hint")


def test_api_recalc_refuses_tampered_snapshot():
    calc = api_calc(WALL, WALL["params"])
    run = client.get(f"/v1/calc/{calc['calc_run_id']}").json()
    tampered = copy.deepcopy(run["snapshot"])
    tampered["params"]["T"] = "0.30"

    response = client.post("/v1/calc/recalc", json={"basis_pack": PACK, "snapshot": tampered,
                                                    "stored_value": calc["result"]})
    assert response.status_code == 200
    body = response.json()
    assert body["reproducible"] is False and body["rejected"] is True
    assert body["stage"] == "SNAPSHOT" and body["reason"] == "SNAPSHOT_TAMPERED"
    assert "recomputed_value" not in body
    assert body["test_only"] is True and body["formal_ready"] is False
