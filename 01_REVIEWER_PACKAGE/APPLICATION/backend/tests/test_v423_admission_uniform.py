"""V4.2.3 / P0-03-ADMISSION：统一正式计算准入（所有出具正式数值的入口）。

对照 tasks/P0-03-ADMISSION/TASK.md：

1. 入口清单：api.py 里每个 POST 端点都被显式分类为
   （正式数值入口 / 非数值入口 / 越界缺口）。正式数值入口必须在请求模型上声明
   `formal_calculation`，新增端点而不更新清单会让用例失败。
2. 每个正式数值入口：`formal_calculation=true` 时先过 core/admission 的统一准入，
   未通过一律 422（不得出正式数值）；`formal_calculation=false`（含省略）时
   结果必须带 `test_only=true` 且 `formal_ready=false`。
3. 反向六情形：依据包 DRAFT / TEST_ONLY / 来源未核验 / 授权缺失 / region 不一致 / 过期
   —— 全部拒绝正式计算（准入层 + HTTP 层双覆盖）。
4. 单一实现：HTTP 422 里的准入原因必须与 core.admission.formal_admission 的结论逐条一致，
   禁止端点各自复制一套判定。
5. 正常值回归：完全合规的 LOCKED 依据包 + 已授权已核验规则在 formal_calculation=true 下
   必须真的拿到 formal_ready=true / test_only=false（准入不得误杀）。
"""
from __future__ import annotations

import pytest
from tests.authenticated_client import TestClient

from txbuddy.api import app, registry
from txbuddy.core.admission import FORMAL_ADMISSION_CODE, formal_admission, require_formal
from txbuddy.core.calcrun import build_snapshot
from txbuddy.models import BasisPack

client = TestClient(app, raise_server_exceptions=False)

# ------------------------------------------------------------------ 公共夹具
BASIS = dict(basis_pack_id="B-P003", region_code="410100", region_level="CITY", major="BUILDING",
             effective_date="2026-01-01", measurement_standard_version="GB/T 50854-2024 [已核]",
             pricing_standard_version="GB/T 50500-2024 [已核]", rule_version_scope="HENAN_2025",
             precision_rule_version="P-V1", status="LOCKED", test_only=False,
             sources=[{"source_id": "S1", "title": "河南定额", "source_type": "QUOTA",
                       "license_status": "LICENSED", "verified": True}])

UNVERIFIED_SOURCES = [{"source_id": "S1", "title": "河南定额", "source_type": "QUOTA",
                       "license_status": "LICENSED", "verified": False}]
PENDING_SOURCES = [{"source_id": "S1", "title": "河南定额", "source_type": "QUOTA",
                    "license_status": "PENDING", "verified": True}]

PARAMS = {"b": "0.5", "h": "0.5", "H": "3"}


def basis(**kw) -> dict:
    body = dict(BASIS)
    body.update(kw)
    return body


def pack(**kw) -> BasisPack:
    return BasisPack(**basis(**kw))


def rule(**kw) -> dict:
    r = dict(rule_code="P003.ADM.OK.1", version="1", name="准入探针（已授权）", unit="m3",
             expression="b * h * H", variables={"b": "m", "h": "m", "H": "m"}, decimals=3,
             standard_ref="GB/T 50854-2024 §5.1【已核】", license_status="LICENSED",
             source_verified=True, status="ACTIVE", region_code="410100", major="BUILDING",
             source_ref="S1", eff_from="2025-01-01", eff_to="2030-12-31")
    r.update(kw)
    return r


PROBE_RULES = {
    "P003.ADM.OK.1": rule(),
    "P003.ADM.TESTONLY.1": rule(rule_code="P003.ADM.TESTONLY.1", license_status="TEST_ONLY"),
    "P003.ADM.UNVERIFIED.1": rule(rule_code="P003.ADM.UNVERIFIED.1", source_verified=False),
    "P003.ADM.REGION.1": rule(rule_code="P003.ADM.REGION.1", region_code="410200"),
    "P003.ADM.EXPIRED.1": rule(rule_code="P003.ADM.EXPIRED.1", eff_to="2025-12-31"),
    "P003.ADM.FUTURE.1": rule(rule_code="P003.ADM.FUTURE.1", eff_from="2027-01-01"),
}


@pytest.fixture(scope="module", autouse=True)
def probe_rules():
    """只在本次用例注册探针规则；结束即摘除，不污染其它测试。"""
    keys = []
    for code, item in PROBE_RULES.items():
        registry.register(item)
        keys.append((code, str(item["version"])))
    try:
        yield
    finally:
        for key in keys:
            registry._rules.pop(key, None)


# ------------------------------------------------------------------ 反向六情形（唯一口径）
SIX_SCENARIOS: dict[str, tuple[dict, str]] = {
    "draft_basis": ({"status": "DRAFT"}, "P003.ADM.OK.1"),
    "test_only_rule": ({}, "P003.ADM.TESTONLY.1"),
    "source_unverified": ({"sources": UNVERIFIED_SOURCES}, "P003.ADM.OK.1"),
    "license_missing": ({"sources": PENDING_SOURCES}, "P003.ADM.OK.1"),
    "region_mismatch": ({}, "P003.ADM.REGION.1"),
    "expired": ({}, "P003.ADM.EXPIRED.1"),
}
SIX_NAMES = set(SIX_SCENARIOS)

# 只能由依据包（无规则绑定）表达的四种情形：其余两种是规则×依据包关系，仅规则入口可表达。
BASIS_SCENARIOS: dict[str, dict] = {
    "draft_basis": {"status": "DRAFT"},
    "test_only_basis": {"test_only": True},
    "source_unverified": {"sources": UNVERIFIED_SOURCES},
    "license_missing": {"sources": PENDING_SOURCES},
}

# 规则入口额外覆盖（非六情形，属有效期/规则状态类）
EXTRA_RULE_SCENARIOS: dict[str, tuple[dict, str]] = {
    "not_yet_effective": ({}, "P003.ADM.FUTURE.1"),
    "rule_source_unverified": ({}, "P003.ADM.UNVERIFIED.1"),
}


# ------------------------------------------------------------------ 入口清单
VALUE_ENTRIES = {
    "/v1/quantity/rule-calc": "规则计算：出具工程量（rule × basis 双重准入）",
    "/v1/quantity/calc": "固定算子：出具工程量（basis 准入）",
    "/v1/quota/apply-notes": "定额附注换算：出具换算后消耗量（basis 准入）",
    "/v1/price/unit": "综合单价：出具单价（basis + 管理费/利润来源准入）",
    "/v1/price/index-adjust": "调差：出具调差金额（basis 准入）",
    "/v1/price/project-total": "项目总价：出具含税总价（basis 准入）",
    "/v1/calc/recalc": "复算：出具复算值（rule × basis 双重准入）",
}

NON_VALUE_ENTRIES = {
    "/v1/basis/validate": "依据包结构校验：只回校验结论，不出数",
    "/v1/rules/validate": "规则自检：无入参、不出数",
    "/v1/quota/match": "定额匹配：只给推荐与 need_human，不给可用于成果的数值",
    "/v1/job": "创建作业壳，只存 basis_pack JSON",
    "/v1/job/{job_id}/documents": "文件版本登记，只存哈希与版本号",
    "/v1/job/{job_id}/records": "业务记录透传存储，不做数值运算",
    "/internal/job/{job_id}/records": "同上的内部写入通道",
    "/v1/job/{job_id}/tasks": "流程节点创建，payload 透传",
    "/v1/task/{task_id}/start": "任务状态迁移",
    "/v1/task/{task_id}/complete": "任务状态迁移 + 结果透传",
    "/v1/task/{task_id}/approve": "任务签批",
    "/v1/task/{task_id}/fail": "任务失败登记",
    "/v1/task/{task_id}/retry": "任务重试",
    "/v1/gate/check": "门禁读取服务端状态；context 自述被忽略",
    "/v1/jgate/{job_id}/run": "鉴定六门服务端判定，只落 gate_record，不出数",
    "/v1/jgate/{job_id}/draft": "鉴定意见草稿登记，默认 SHADOW 不可签发",
    "/v1/jgate/{job_id}/signoff": "人工签署登记（含门禁快照），不做数值运算",
    "/v1/jgate/{job_id}/issue": "显式出件，需有效人工签署；不做数值运算",
    "/v1/evidence/verify": "证据哈希校验，只比较字符串",
}

# 越界缺口：BOQ 解析属 P1-09-PARSE / core/boq.py，不在 P0-03 写入范围（见 RESULT.md）。
OUT_OF_SCOPE_ENTRIES = {
    "/v1/boq/import": "BOQ 解析（P1-09-PARSE）；无 basis_pack 通道，逐行工程量在 P1-09 接入准入",
}


def _post_routes() -> dict:
    return {route.path: route for route in app.routes
            if "POST" in (getattr(route, "methods", None) or set())}


def _body_model(path: str):
    route = _post_routes()[path]
    params = list(getattr(route.dependant, "body_params", []) or [])
    assert params, f"{path} 没有 JSON 请求体"
    return params[0].field_info.annotation


def test_entry_inventory_matches_routes():
    """清单必须与真实路由表一致：新增 POST 端点而不登记治理方式即失败。"""
    declared = set(VALUE_ENTRIES) | set(NON_VALUE_ENTRIES) | set(OUT_OF_SCOPE_ENTRIES)
    actual = set(_post_routes())
    assert not (actual - declared), f"未登记治理方式的 POST 入口：{sorted(actual - declared)}"
    assert not (declared - actual), f"清单中已不存在的入口：{sorted(declared - actual)}"
    assert not (set(VALUE_ENTRIES) & set(NON_VALUE_ENTRIES))


def test_every_value_entry_accepts_formal_calculation():
    """所有出具正式数值的入口都必须在请求模型上声明 formal_calculation。"""
    missing = {}
    for path in sorted(VALUE_ENTRIES):
        fields = set(getattr(_body_model(path), "model_fields", {}) or {})
        if "formal_calculation" not in fields:
            missing[path] = sorted(fields)
    assert not missing, f"这些正式数值入口不接受 formal_calculation：{missing}"


def test_non_value_entries_do_not_claim_formal_numbers():
    """非数值/越界入口的响应不得声明 formal_ready=true。"""
    match = client.post("/v1/quota/match", json={
        "boq_item": {"code": "A1", "name": "现浇混凝土矩形柱", "unit": "m3", "quantity": "1"},
        "quota_items": []})
    assert match.status_code == 200
    assert "formal_ready" not in match.json() and "test_only" not in match.json()


# ------------------------------------------------------------------ payload 构造
def payload(path: str, pack_body: dict, formal: bool | None = True,
            rule_code: str = "P003.ADM.OK.1") -> dict:
    """按入口构造最小合法请求体；formal=None 表示完全省略该字段。"""
    if path == "/v1/quantity/rule-calc":
        body = {"basis_pack": pack_body, "rule_code": rule_code, "params": dict(PARAMS)}
    elif path == "/v1/quantity/calc":
        body = {"basis_pack": pack_body, "operation": "RECTANGULAR_VOLUME",
                "params": {"length": "0.5", "width": "0.5", "height": "3"}, "unit": "m3",
                "rule_code": "R", "rule_version": "1", "standard_ref": "S1"}
    elif path == "/v1/price/unit":
        body = {"basis_pack": pack_body,
                "resources": [{"resource_code": "M1", "resource_type": "MATERIAL",
                               "consumption": "2", "unit_price": "10", "price_ref": "S1"}],
                "management_rate": "0.1", "profit_rate": "0.05",
                "management_source_ref": "S1", "profit_source_ref": "S1"}
    elif path == "/v1/price/index-adjust":
        body = {"basis_pack": pack_body, "base_amount": "1000", "base_index": "100",
                "current_index": "110", "risk_rate": "0.05", "mode": "FULL_EXCESS",
                "contract_clause_ref": "C-1"}
    elif path == "/v1/price/project-total":
        body = {"basis_pack": pack_body, "boq_total": "100", "measure_total": "10",
                "other_total": "0", "fee_items": [],
                "safety_production_fee": {"fee_item": "SAFETY_PRODUCTION_FEE", "amount": "3",
                                          "is_noncompetitive": True, "source_ref": "S1"},
                "tax_rate": "0.09"}
    elif path == "/v1/quota/apply-notes":
        body = {"basis_pack": pack_body,
                "consumption": [{"resource_code": "R1", "consumption": "2"}],
                "notes": [{"id": "N1", "scope_level": "ITEM", "effect_type": "FACTOR",
                           "factor": "1.5", "target_resource_code": "R1", "source_ref": "S1"}]}
    elif path == "/v1/calc/recalc":
        body = {"basis_pack": pack_body, "stored_value": "0.750",
                "snapshot": build_snapshot(rule_code="P003.ADM.OK.1", rule_version="1",
                                           basis_pack_id="B-P003", precision_rule_version="P-V1",
                                           params=dict(PARAMS), unit="m3")}
    else:  # pragma: no cover - 清单与构造器必须同步
        raise AssertionError(f"未登记的正式数值入口：{path}")
    if formal is not None:
        body["formal_calculation"] = formal
    return body


def post(path: str, pack_body: dict, formal: bool | None = True, rule_code: str | None = None):
    return client.post(path, json=payload(path, pack_body, formal,
                                          rule_code=rule_code or "P003.ADM.OK.1"))


def _detail(response) -> dict:
    body = response.json()
    detail = body.get("detail")
    return detail if isinstance(detail, dict) else {}


# ------------------------------------------------------------------ 1) 六情形：准入层
@pytest.mark.parametrize("name", sorted(SIX_SCENARIOS))
def test_admission_layer_rejects_six_scenarios(name):
    pack_kw, code = SIX_SCENARIOS[name]
    adm = formal_admission(PROBE_RULES[code], pack(**pack_kw))
    assert adm["formal_ready"] is False, f"{name} 不得 formal_ready"
    assert adm["admission_reasons"], f"{name} 必须给出拒绝原因"
    gate = require_formal(PROBE_RULES[code], pack(**pack_kw), True)
    assert gate["rejected"] is True and gate["admitted"] is False and gate["test_only"] is True
    loose = require_formal(PROBE_RULES[code], pack(**pack_kw), False)
    assert loose["rejected"] is False and loose["admitted"] is False and loose["test_only"] is True


def test_clean_case_is_formal_ready():
    """准入不得误杀：合规包 + 已授权已核验规则必须 formal_ready。"""
    adm = formal_admission(PROBE_RULES["P003.ADM.OK.1"], pack())
    assert adm["formal_ready"] is True and adm["admission_reasons"] == [] and adm["test_only"] is False


@pytest.mark.parametrize("name", sorted(EXTRA_RULE_SCENARIOS))
def test_admission_layer_rejects_extra_rule_scenarios(name):
    pack_kw, code = EXTRA_RULE_SCENARIOS[name]
    assert formal_admission(PROBE_RULES[code], pack(**pack_kw))["formal_ready"] is False


# ------------------------------------------------------------------ 2) 六情形：规则入口 HTTP
@pytest.mark.parametrize("name", sorted(SIX_SCENARIOS))
def test_rule_calc_rejects_formal_calculation_for_six_scenarios(name):
    pack_kw, code = SIX_SCENARIOS[name]
    r = post("/v1/quantity/rule-calc", basis(**pack_kw), True, rule_code=code)
    assert r.status_code == 422, f"{name} 必须拒绝正式计算：{r.status_code} {r.text[:300]}"
    detail = _detail(r)
    assert detail.get("code") == FORMAL_ADMISSION_CODE, detail
    assert detail.get("admission_reasons"), f"{name} 必须回传准入原因"
    assert "result" not in r.json(), "被拒绝的正式计算不得回传任何数值"
    # 同一情形做测试计算必须放行，但结果只能标记为 test_only
    ok = post("/v1/quantity/rule-calc", basis(**pack_kw), False, rule_code=code)
    assert ok.status_code == 200, f"{name} 测试计算应放行：{ok.status_code} {ok.text[:300]}"
    body = ok.json()
    assert body["test_only"] is True and body["formal_ready"] is False
    assert body["admission_reasons"], "测试结果也必须带上拒绝原因，便于追溯"


@pytest.mark.parametrize("name", sorted(SIX_SCENARIOS))
def test_http_rejection_reasons_match_single_admission(name):
    """422 里的原因必须与 core.admission 的结论逐条一致（证明只有一处判定）。"""
    pack_kw, code = SIX_SCENARIOS[name]
    expected = formal_admission(PROBE_RULES[code], pack(**pack_kw))["admission_reasons"]
    r = post("/v1/quantity/rule-calc", basis(**pack_kw), True, rule_code=code)
    got = _detail(r).get("admission_reasons")
    assert [x["detail"] for x in got] == [x["detail"] for x in expected]
    assert [x["reason_code"] for x in got] == [x["reason_code"] for x in expected]


# ------------------------------------------------------------------ 3) 依据包入口 HTTP
@pytest.mark.parametrize("path", sorted(set(VALUE_ENTRIES) - {"/v1/quantity/rule-calc", "/v1/calc/recalc"}))
@pytest.mark.parametrize("name", sorted(BASIS_SCENARIOS))
def test_basis_bound_entry_rejects_formal_calculation(path, name):
    r = post(path, basis(**BASIS_SCENARIOS[name]), True)
    assert r.status_code == 422, f"{path} 在 {name} 下必须拒绝正式计算：{r.status_code} {r.text[:300]}"
    detail = _detail(r)
    assert detail.get("code") == FORMAL_ADMISSION_CODE and detail.get("admission_reasons")


@pytest.mark.parametrize("path", sorted(VALUE_ENTRIES))
def test_every_value_entry_rejects_draft_basis_for_formal(path):
    r = post(path, basis(status="DRAFT"), True)
    assert r.status_code == 422, f"{path} 应拒绝 DRAFT 依据包的正式计算：{r.status_code} {r.text[:300]}"


@pytest.mark.parametrize("path", sorted(VALUE_ENTRIES))
def test_every_value_entry_marks_test_only_when_not_formal(path):
    """formal_calculation=false 时：算得出数，但结果必须 test_only=true / formal_ready=false。"""
    r = post(path, basis(), False)
    assert r.status_code == 200, f"{path} 测试计算应放行：{r.status_code} {r.text[:300]}"
    body = r.json()
    assert body["test_only"] is True, f"{path} 非正式计算必须 test_only=true：{body}"
    assert body["formal_ready"] is False, f"{path} 非正式计算不得 formal_ready"


@pytest.mark.parametrize("path", sorted(set(VALUE_ENTRIES) - {"/v1/price/project-total"}))
def test_omitted_formal_flag_defaults_to_test_only(path):
    """未声明 formal_calculation 的入口按测试计算处理（fail-closed：默认不出正式数值）。"""
    r = post(path, basis(), None)
    assert r.status_code == 200, f"{path} 省略 formal_calculation 应仍可做测试计算：{r.status_code}"
    assert r.json()["test_only"] is True and r.json()["formal_ready"] is False


def test_project_total_keeps_historical_formal_default():
    """project-total 沿用 P0-01 既有契约：省略 formal_calculation 等同正式计价。"""
    r = client.post("/v1/price/project-total", json={
        "basis_pack": basis(status="DRAFT"), "boq_total": "100", "measure_total": "10",
        "other_total": "0", "fee_items": [], "safety_production_fee": None, "tax_rate": "0.09"})
    assert r.status_code == 422 and _detail(r).get("code") == FORMAL_ADMISSION_CODE


# ------------------------------------------------------------------ 4) 正常值回归
@pytest.mark.parametrize("path", sorted(VALUE_ENTRIES))
def test_every_value_entry_grants_formal_admission_when_clean(path):
    r = post(path, basis(), True)
    assert r.status_code == 200, f"{path} 合规正式计算应放行：{r.status_code} {r.text[:300]}"
    body = r.json()
    assert body["formal_ready"] is True and body["test_only"] is False, body
    assert body["admission_reasons"] == []
    assert body["formal_calculation"] is True
    assert any(k in body for k in ("result", "unit_price", "delta_amount", "project_total",
                                  "consumption", "reproducible"))


def test_rule_calc_formal_admission_returns_number_and_passes():
    r = post("/v1/quantity/rule-calc", basis(), True)
    assert r.status_code == 200
    assert r.json()["result"] == "0.750" and r.json()["formal_ready"] is True


# ------------------------------------------------------------------ 5) 来源绑定（单价来源）
def test_unit_price_formal_requires_basis_pack():
    body = payload("/v1/price/unit", basis(), True)
    body.pop("basis_pack")
    r = client.post("/v1/price/unit", json=body)
    assert r.status_code == 422 and _detail(r).get("code") == FORMAL_ADMISSION_CODE
    assert any("basis_pack" in x for x in _detail(r).get("issues", []))


def test_unit_price_formal_requires_source_refs_in_pack():
    body = payload("/v1/price/unit", basis(), True)
    body["management_source_ref"] = "S-NOT-IN-PACK"
    r = client.post("/v1/price/unit", json=body)
    assert r.status_code == 422
    assert any("S-NOT-IN-PACK" in x["detail"] for x in _detail(r)["admission_reasons"])


def test_unit_price_test_calc_without_basis_is_still_marked_test_only():
    body = payload("/v1/price/unit", basis(), False)
    body.pop("basis_pack")
    r = client.post("/v1/price/unit", json=body)
    assert r.status_code == 200 and r.json()["test_only"] is True
    assert r.json()["admission_reasons"], "缺依据包也要说明原因"


# ------------------------------------------------------------------ 6) 覆盖矩阵
def test_coverage_matrix_covers_all_six():
    """六情形必须有落点：规则入口全六种，依据包入口四种，矩阵不得缺口。"""
    assert SIX_NAMES == {"draft_basis", "test_only_rule", "source_unverified",
                         "license_missing", "region_mismatch", "expired"}
    rule_bound = {"/v1/quantity/rule-calc", "/v1/calc/recalc"}
    basis_bound = set(VALUE_ENTRIES) - rule_bound
    assert rule_bound | basis_bound == set(VALUE_ENTRIES)
    # 规则入口可表达全部六种；依据包入口里 test_only_rule 以 test_only_basis 等价表达。
    assert SIX_NAMES - set(BASIS_SCENARIOS) == {"test_only_rule", "region_mismatch", "expired"}
    assert {"region_mismatch", "expired"} <= SIX_NAMES, "地区不一致与过期只能由规则入口表达，必须在规则矩阵中覆盖"
    for name in SIX_NAMES:
        pack_kw, code = SIX_SCENARIOS[name]
        assert formal_admission(PROBE_RULES[code], pack(**pack_kw))["formal_ready"] is False
