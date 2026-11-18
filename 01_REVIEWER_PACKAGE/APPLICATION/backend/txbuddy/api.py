"""FastAPI 入口（V4.2）。

P0 修复落点：
- P0-01 正式准入：core/admission.py，TEST_ONLY 规则永不 formal_ready；
- P0-02 幂等：按 tenant/actor/method/path/key 隔离 + 请求指纹，冲突返回 409 IDEM_KEY_CONFLICT；
- P0-03 统一准入：所有出正式数值的入口（rule-calc / quantity-calc / quota-apply-notes /
  price-unit / price-index-adjust / price-project-total / calc-recalc）都接受 formal_calculation，
  true 时统一走 require_formal（未过即 422，不出数），false/省略时结果带 test_only=true；
- P0-04 数值安全：core/numeric.py，NaN/Infinity/越界/除零统一 422 业务码；
- P0-05 门禁：绑定 job_id 走 gates_service 读服务端状态；无 job 时权威门一律 FAIL（advisory）。
- P0-05-JGATE 鉴定线：J0–J5 六门只做影子运行（/v1/jgate/{job_id}），正式成果默认 SHADOW，
  必须经 /signoff 人工签署（自然人 + 执业资格 + 声明 + 门禁快照）后由 /issue 显式出件；
  通用 /records 接口拒收 j_opinion/j_signoff/j_deliverable，鉴定与编制口径不得混用。
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from .api_models import GateRequest, RecalcRequest, RuleExecuteRequest
from .core import calcrun, precision
from .core.admission import (FORMAL_ADMISSION_CODE, FORMAL_ADMISSION_MESSAGE,
                             admission_issues, require_formal)
from .core.basis import validate_basis_pack
from .core.boq import import_boq
from .core.evidence import build_evidence, verify_evidence
from .core.gates import (GATE_SEQUENCE, J_FORBIDDEN_COMPILATION_KINDS, J_GATE_SET,
                         J_GATES, J_LINE, J_RESERVED_RECORD_KINDS, J_SERVER_ONLY_KINDS,
                         check_gate, check_issue_ready, check_manual_signoff,
                         j_deliverable_state, j_gate_snapshot, j_gate_status,
                         j_shadow_policy, latest_gate_results)
from .core.gates_service import AUTHORITY_GATES, GateError, evaluate as gate_evaluate
from .core.record_writer import RecordError, write_record
from .core.numeric import NumericError, http_error, install_exception_handlers, to_decimal
from .core.pricing import compose_unit_price, index_adjustment, project_total
from .core.quantity import calculate_quantity
from .core.quota import apply_quota_notes, match_quota
from .core.rules import RuleError, RuleRegistry
from .core import workflow as wf
from .models import (BasisPack, IndexAdjustRequest, ProjectTotalRequest,
                     QuantityCalcRequest, QuotaMatchRequest, QuotaNotesRequest, UnitPriceRequest)
from .store import EvidenceStore

RULES_PATH = Path(__file__).resolve().parents[1] / "data" / "rules_test_only.json"
app = FastAPI(title="天寻 Buddy Deterministic Core", version="4.2.6")
RUNTIME_ROOT = Path(os.environ.get('TXBUDDY_RUNTIME_ROOT', 'data/v424-runtime')).resolve()
store = EvidenceStore(os.environ.get('TXBUDDY_STORE_PATH', str(RUNTIME_ROOT / 'business.sqlite3')))
registry = RuleRegistry(RULES_PATH)

# P0-01-NUMERIC：全引擎数值安全兜底。
# core 层只抛 NumericError(NUMERIC_*)；此处把漏网的 DecimalException / 数值型校验错误
# 统一映射成 422 业务错误码，禁止 500。RuleError 也在此兜底（如复算入口的未知规则）。
install_exception_handlers(app, domain_errors=(RuleError,))


def stringify(x: Any):
    if isinstance(x, Decimal):
        return str(x)
    if isinstance(x, list):
        return [stringify(v) for v in x]
    if isinstance(x, dict):
        return {k: stringify(v) for k, v in x.items()}
    return x


# ------------------------------------------------------------------ P0-03 统一正式计算准入
# 所有会产出“可用于正式成果的数值”的入口共用下面两个函数；准入判定只有 core/admission 一份实现。
# 请求不声明 formal_calculation 时按测试计算处理（结果恒带 test_only=true）：
# 只有显式 formal_calculation=true 且全项准入通过，才允许对外给出 formal_ready=true 的正式数值。

class FormalRuleExecuteRequest(RuleExecuteRequest):
    formal_calculation: bool = False


class FormalQuantityCalcRequest(QuantityCalcRequest):
    formal_calculation: bool = False


class FormalUnitPriceRequest(UnitPriceRequest):
    basis_pack: BasisPack | None = None
    formal_calculation: bool = False


class FormalQuotaNotesRequest(QuotaNotesRequest):
    basis_pack: BasisPack | None = None
    formal_calculation: bool = False


class FormalIndexAdjustRequest(IndexAdjustRequest):
    formal_calculation: bool = False


class FormalRecalcRequest(RecalcRequest):
    formal_calculation: bool = False


def _admit(basis, formal: bool, rule: dict | None = None, source_refs=()) -> dict:
    """统一准入口：formal=True 未通过时必须 422，不给出任何正式数值。"""
    admission = require_formal(rule, basis, formal, source_refs=source_refs)
    if formal and not admission['rejected']:
        database = app.state.pilot_db
        pack_id = getattr(basis, 'basis_pack_id', None)
        with database.transaction() as c:
            registered = c.one('SELECT basis_pack_id FROM basis_pack WHERE basis_pack_id=? AND formal_usable=1 AND license_status=?',
                               (pack_id, 'LICENSED_VERIFIED'))
        if not registered:
            raise HTTPException(422,detail={'code':FORMAL_ADMISSION_CODE,
                'message':'正式计算必须绑定服务端登记的授权依据包；客户端自述许可不能作为授权',
                'formal_calculation':True,'formal_ready':False,
                'admission_reasons':['TRUSTED_BASIS_NOT_REGISTERED']})
    if admission["rejected"]:
        raise HTTPException(422, detail={
            "code": FORMAL_ADMISSION_CODE, "message": FORMAL_ADMISSION_MESSAGE,
            "basis_pack_id": admission.get("basis_pack_id"),
            "rule_code": admission.get("rule_code"), "rule_version": admission.get("rule_version"),
            "formal_calculation": True,
            "issues": admission_issues(admission),
            "admission_reasons": admission["admission_reasons"]})
    return admission


def _mark(admission: dict) -> dict:
    """把准入结论写进响应：非正式计算一律 test_only=true、formal_ready=false。"""
    return {"formal_calculation": admission["requested_formal"],
            "formal_ready": admission["admitted"], "test_only": admission["test_only"],
            "admission_reasons": admission["admission_reasons"]}


def _fingerprint(payload: Any) -> str:
    import json
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# 幂等诊断日志默认关闭：50 并发下写日志会引入 I/O 竞争并污染测试计时。
# 需要取证时设 TXBUDDY_IDEM_DEBUG=1，日志落在 data/_idem_debug.log。
IDEM_DEBUG = os.environ.get("TXBUDDY_IDEM_DEBUG", "") in {"1", "true", "TRUE", "yes"}


def _idem_dbg(msg: str) -> None:
    if not IDEM_DEBUG:
        return
    try:
        from pathlib import Path as _P
        log = _P(__file__).resolve().parents[1] / "data" / "_idem_debug.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(msg + "\n")
    except Exception:
        pass


def _idem(request: Request, payload: Any, handler):
    key = request.headers.get("X-Idem-Key")
    if not key:
        return handler()
    tenant = request.headers.get("X-Tenant-Id", "")
    actor = getattr(getattr(request.state, 'identity', None), 'subject', 'anonymous')
    scope = store.idem_scope(tenant, actor, request.method, request.url.path, key)
    fp = _fingerprint(payload)
    rid = uuid.uuid4().hex[:8]
    _idem_dbg(f"[{rid}] enter path={request.url.path} key={key} tenant={tenant!r} actor={actor} fp={fp[:16]}")
    # 预检只用于诊断（并发下 pre 与 claim 之间无共享状态，不能作为判定依据）。
    if IDEM_DEBUG:
        pre_state, _ = store.idem_lookup(scope, fp)
        _idem_dbg(f"[{rid}] pre={pre_state}")
    state, cached = store.idem_claim(scope, fp, tenant, actor)
    _idem_dbg(f"[{rid}] claim={state}")
    if state == "hit":
        cached["idem_replay"] = True
        return cached
    if state == "conflict":
        _idem_dbg(f"[{rid}] -> 409(after claim)")
        return JSONResponse(status_code=409, content={
            "code": "IDEM_KEY_CONFLICT", "detail": "相同幂等键对应不同请求内容，拒绝重放",
            "idem_key": key, "fingerprint": fp[:16]})
    if state == "in_progress":
        return JSONResponse(status_code=425, content={"code": "IDEM_IN_PROGRESS", "retryable": True})
    if state == "retry":
        return JSONResponse(status_code=503, content={"code": "IDEM_RETRY", "retryable": True})
    # state == "claimed"：执行权在本次请求手里。失败必须释放，否则同键永久卡 IN_PROGRESS。
    try:
        result = handler()
    except BaseException:
        store.idem_release(scope)
        _idem_dbg(f"[{rid}] released-on-error fp={fp[:16]}")
        raise
    if not store.idem_finish(scope, result):
        _idem_dbg(f"[{rid}] finish-failed(claim lost) fp={fp[:16]}")
    _idem_dbg(f"[{rid}] executed fp={fp[:16]} scope={scope}")
    return result

@app.middleware("http")
async def trace_and_audit(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:16]
    try:
        response = await call_next(request)
    except Exception:
        raise
    response.headers["X-Trace-Id"] = trace_id
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and response.status_code < 400:
        store.add_audit(request.headers.get("X-Actor", "anonymous"),
                        f"{request.method} {request.url.path}", "HTTP", str(response.status_code),
                        trace_id=trace_id)
    return response


@app.get("/health")
def health():
    environment = os.environ.get('TXBUDDY_ENV', 'UNCONFIRMED')
    return {"status": "ok", "service": "txbuddy-core", "version": "4.2.6", "mode": "data-governance-sample",
            "environment": environment, "test_only": environment == 'TEST_ONLY',
            "production_accepted": False, "accuracy_E": "BLOCKED",
            "rules_loaded": len(registry.all()), "precision": precision.PRECISION_VERSION}


@app.get("/v1/precision")
def precision_info():
    return precision.describe()


# ------------------------------------------------------------------ 依据与规则
@app.post("/v1/basis/validate")
def basis_validate(pack: BasisPack):
    return validate_basis_pack(pack)


@app.get("/v1/rules")
def rules_list():
    return {"count": len(registry.all()), "rules": [
        {k: r.get(k) for k in ("rule_code", "version", "name", "component_type", "caliber", "unit",
                               "expression", "standard_ref", "license_status", "source_verified", "status")}
        for r in registry.all()]}


@app.post("/v1/rules/validate")
def rules_validate():
    return registry.validate()


@app.post("/v1/quantity/rule-calc")
def quantity_rule_calc(req: FormalRuleExecuteRequest, request: Request):
    def run():
        try:
            rule = registry.get(req.rule_code, req.rule_version)
        except RuleError as exc:
            raise http_error(exc)
        # 正式计算先过准入（规则 × 依据包），未通过直接拒绝，不算数。
        admission = _admit(req.basis_pack, req.formal_calculation, rule)
        try:
            result = registry.execute(req.rule_code, req.params, req.rule_version)
        except NumericError as exc:
            raise http_error(exc)
        except RuleError as exc:
            raise http_error(exc)
        snapshot = calcrun.build_snapshot(
            rule_code=result["rule_code"], rule_version=result["rule_version"],
            basis_pack_id=req.basis_pack.basis_pack_id,
            precision_rule_version=req.basis_pack.precision_rule_version,
            params=req.params, unit=result["unit"],
            source_refs=[result["standard_ref"]], components=req.component_ids)
        evidence = build_evidence(
            job_id=req.job_id,
            subject_type="QUANTITY", subject_id=result["rule_code"], result=result["value"],
            unit=result["unit"], formula=result["formula"],
            intermediate_values=result["intermediate_values"], input_snapshot=req.params,
            basis_pack_id=req.basis_pack.basis_pack_id, rule_version=result["rule_version"],
            precision_rule_version=req.basis_pack.precision_rule_version,
            source_refs=[result["standard_ref"]], producer="ENGINE",
            engine_build=calcrun.ENGINE_BUILD, license_status=rule.get("license_status"))
        store.save(evidence)
        store.save_calc_run({"calc_run_id": evidence["evidence_id"], "job_id": req.job_id,
                             "rule_code": result["rule_code"], "rule_version": result["rule_version"],
                             "basis_pack_id": req.basis_pack.basis_pack_id,
                             "snapshot_sha256": snapshot["snapshot_sha256"], "snapshot": snapshot,
                             "result": result["value"], "unit": result["unit"],
                             "evidence_id": evidence["evidence_id"], "created_at": calcrun.now_iso()})
        return {
            "calc_run_id": evidence["evidence_id"], "result": result["value"], "unit": result["unit"],
            "formula": result["formula"], "intermediate_values": stringify(result["intermediate_values"]),
            "standard_ref": result["standard_ref"], **_mark(admission),
            "snapshot_sha256": snapshot["snapshot_sha256"],
            "evidence_id": evidence["evidence_id"], "evidence_sha256": evidence["sha256"],
            "warning": "TEST_ONLY 数据仅用于测试；正式成果必须使用已授权依据包与已核验规则。",
        }
    return _idem(request, req.model_dump(), run)


# ------------------------------------------------------------------ 固定算子
@app.post("/v1/quantity/calc")
def quantity_calc(req: FormalQuantityCalcRequest):
    admission = _admit(req.basis_pack, req.formal_calculation)
    try:
        params = {k: to_decimal(v, k) for k, v in req.params.items()
                  if k in {"length", "width", "height", "count", "gross"}}
        params.update({k: [to_decimal(x, k) for x in v] for k, v in req.params.items()
                       if k in {"deductions", "additions"}})
        r = calculate_quantity(req.operation, params)
    except NumericError as exc:
        raise http_error(exc)
    except Exception as exc:  # noqa: BLE001
        raise http_error(exc, fallback="CALC_PARAM_MISSING")
    evidence = build_evidence(job_id=req.job_id, subject_type="QUANTITY", subject_id=req.rule_code, result=r["value"],
                              unit=req.unit, formula=r["formula"], intermediate_values=r["intermediate_values"],
                              input_snapshot=req.params, basis_pack_id=req.basis_pack.basis_pack_id,
                              rule_version=req.rule_version,
                              precision_rule_version=req.basis_pack.precision_rule_version,
                              source_refs=[req.standard_ref], producer="ENGINE")
    store.save(evidence)
    return {"result": str(r["value"]), "unit": req.unit, "formula": r["formula"],
            "intermediate_values": stringify(r["intermediate_values"]),
            "evidence_id": evidence["evidence_id"], "evidence_sha256": evidence["sha256"],
            **_mark(admission)}


@app.post("/v1/quota/match")
def quota_match(req: QuotaMatchRequest):
    return match_quota(req.boq_item, req.quota_items, req.exact_mappings)


@app.post("/v1/quota/apply-notes")
def quota_apply_notes(req: FormalQuotaNotesRequest):
    admission = _admit(req.basis_pack, req.formal_calculation)
    try:
        return {**stringify(apply_quota_notes(req.consumption, req.notes, req.conversion)), **_mark(admission)}
    except NumericError as exc:
        raise http_error(exc)
    except Exception as exc:  # noqa: BLE001
        raise http_error(exc, fallback="BASIS_ITEM_MISSING")


@app.post("/v1/price/unit")
def price_unit(req: FormalUnitPriceRequest):
    admission = _admit(req.basis_pack, req.formal_calculation,
                       source_refs=(req.management_source_ref, req.profit_source_ref))
    try:
        return {**stringify(compose_unit_price(req.resources, req.management_rate, req.profit_rate,
                                               req.management_base, req.profit_base, req.explicit_risk_fee,
                                               req.adjusted_resource_codes, req.risk_resource_codes,
                                               req.management_source_ref, req.profit_source_ref)),
                **_mark(admission)}
    except NumericError as exc:
        raise http_error(exc)
    except Exception as exc:  # noqa: BLE001
        raise http_error(exc, fallback="PRICE_GATE_NOT_READY")


@app.post("/v1/price/index-adjust")
def price_adjust(req: FormalIndexAdjustRequest):
    admission = _admit(req.basis_pack, req.formal_calculation)
    try:
        r = index_adjustment(req.base_amount, req.base_index, req.current_index, req.risk_rate, req.mode)
    except NumericError as exc:
        raise http_error(exc)
    except Exception as exc:  # noqa: BLE001
        raise http_error(exc, fallback="ADJUST_RULE_INCOMPLETE")
    return {**stringify(r), "basis_pack_id": req.basis_pack.basis_pack_id,
            "contract_clause_ref": req.contract_clause_ref, **_mark(admission)}


@app.post("/v1/price/project-total")
def total(req: ProjectTotalRequest):
    admission = _admit(req.basis_pack, req.formal_calculation)
    try:
        return {**stringify(project_total(req.basis_pack, req.boq_total, req.measure_total, req.other_total,
                                          req.fee_items, req.safety_production_fee, req.tax_rate,
                                          req.formal_calculation)),
                **_mark(admission)}
    except NumericError as exc:
        raise http_error(exc)
    except Exception as exc:  # noqa: BLE001
        raise http_error(exc, fallback="PRICE_GATE_NOT_READY")


# ------------------------------------------------------------------ BOQ 导入
@app.post("/v1/boq/import")
async def boq_import(file: UploadFile = File(...), x_actor: str = Header(default="anonymous")):
    raw = await file.read()
    if not raw:
        raise HTTPException(422, detail={"code": "DOC_GATE_NOT_READY", "message": "空文件"})
    try:
        result = import_boq(raw)
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "DOC_GATE_NOT_READY", "message": str(exc)})
    store.add_audit(x_actor, "BOQ_IMPORT", "BOQ", result.get("content_sha256") or "",
                    detail=f"valid={result['stats']['valid']} invalid={result['stats']['invalid']}")
    return result


# ------------------------------------------------------------------ 复算
@app.post("/v1/calc/recalc")
def calc_recalc(req: FormalRecalcRequest, request: Request):
    def run():
        # P0-01-NUMERIC：存档值/快照参数都可能是非法数值；先过守卫，禁止 NaN 比较抛 500。
        to_decimal(req.stored_value, "stored_value", allow_negative=True)
        rule = None
        if req.formal_calculation:
            code = (req.snapshot or {}).get("rule_code")
            if code:
                try:
                    rule = registry.get(str(code), (req.snapshot or {}).get("rule_version"))
                except RuleError as exc:
                    raise http_error(exc)
        admission = _admit(req.basis_pack, req.formal_calculation, rule)
        try:
            result = calcrun.recalc(registry, req.snapshot, req.stored_value)
        except NumericError as exc:
            raise http_error(exc)
        except RuleError as exc:
            raise http_error(exc)
        if not result.get("reproducible"):
            result["hint"] = "按 A-40：先查快照完整性，再查 rule_version 与依据包，最后判实现缺陷。"
        return {**stringify(result), **_mark(admission)}
    return _idem(request, req.model_dump(), run)


@app.get("/v1/calc/{run_id}")
def calc_get(run_id: str):
    run = store.get_calc_run(run_id)
    if not run:
        raise HTTPException(404, detail={"code": "EVIDENCE_NOT_FOUND", "message": "calc_run 不存在"})
    return stringify(run)


# ------------------------------------------------------------------ Job / Task
from pydantic import BaseModel  # noqa: E402


class JobCreate(BaseModel):
    project_id: str
    name: str
    business_line: str
    basis_pack: dict[str, Any]
    created_by: str = "tester"


class DocumentAdd(BaseModel):
    doc_key: str
    version: str
    file_sha256: str
    verified: bool = False


class RecordPut(BaseModel):
    kind: str
    payload: dict[str, Any]


class TaskCreate(BaseModel):
    node: str
    payload: dict[str, Any] = {}
    assignee: str = "tester"


class TaskAction(BaseModel):
    actor: str = "tester"
    role: str = "EDITOR"
    license: str | None = None
    result: dict[str, Any] = {}
    reason: str = ""
    comment: str = ""


class JOpinionDraft(BaseModel):
    title: str = "造价鉴定意见书（草稿）"
    conclusion_form: str = "QUALIFIED"
    evidence_level: str = "MEDIUM"
    summary: str = ""


class JSignoffRequest(BaseModel):
    """人工签署：必须落到具体自然人 + 执业资格 + 签署声明。"""

    actor: str
    license: str
    statement: str
    opinion_id: str | None = None


class JIssueRequest(BaseModel):
    actor: str = "tester"
    opinion_id: str | None = None


class JRunRequest(BaseModel):
    actor: str = "tester"


# ------------------------------------------------------ P0-05-JGATE 鉴定线（L6）
def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _verified_professional(identity, license_type: str | None = None) -> dict[str, Any] | None:
    """Return a server-registered credential bound to the authenticated subject.

    JWT roles alone are not a credential: an identity must also have a complete
    server-side verification record, and callers may require an exact license.
    """
    if not identity.has_role("verified_professional"):
        return None
    registry_path = os.environ.get("TXBUDDY_PROFESSIONALS_FILE")
    try:
        registry = json.loads(Path(registry_path).read_text(encoding="utf-8")) if registry_path else {}
    except (OSError, ValueError):
        registry = {}
    verified = registry.get(identity.subject) if isinstance(registry, dict) else None
    if (not isinstance(verified, dict) or verified.get("subject") != identity.subject
            or not all(verified.get(key) for key in ("identity_verified_at", "license_number", "name"))
            or (license_type is not None and verified.get("license_type") != license_type)):
        return None
    return verified


def _j_job(job_id: str) -> dict[str, Any]:
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, detail={"code": "INTAKE_GATE_NOT_READY",
                                         "message": f"job 不存在：{job_id}"})
    if job["business_line"] != J_LINE:
        raise HTTPException(422, detail={
            "code": "JD_LINE_MIXED",
            "message": f"业务线 {job['business_line']} 不是鉴定线 {J_LINE}；鉴定口径与编制口径不得混用"})
    return job


def _latest_record(job_id: str, kind: str) -> dict[str, Any] | None:
    rows = store.get_records(job_id, kind)
    return rows[-1] if rows else None


def _j_view(job_id: str) -> dict[str, Any]:
    job = _j_job(job_id)
    records = store.list_gate_records(job_id)
    latest = latest_gate_results(records)
    opinion = _latest_record(job_id, "j_opinion")
    signoff = _latest_record(job_id, "j_signoff")
    deliverable = _latest_record(job_id, "j_deliverable")
    opinion_id = (opinion or {}).get("opinion_id")
    snapshot = j_gate_snapshot(records)
    all_passed = all(latest.get(gate) == "PASS" for gate in J_GATES)
    signoff_failed = check_manual_signoff(signoff, opinion_id) if signoff else \
        check_manual_signoff(None)
    signoff_valid = bool(signoff) and not signoff_failed
    issued = bool(deliverable and deliverable.get("issued"))
    state = j_deliverable_state(latest, signoff, opinion_id, issued)
    return {
        "job_id": job_id,
        "business_line": job["business_line"],
        "policy": j_shadow_policy(),
        "shadow_run": True,
        "auto_issue": False,
        "requires_manual_signoff": True,
        "deliverable_state": state,
        "gates": j_gate_status(records),
        "gate_results": {gate: latest.get(gate, "PENDING") for gate in J_GATES},
        "all_gates_passed": all_passed,
        "missing_gates": [gate for gate in J_GATES if latest.get(gate) != "PASS"],
        "gate_snapshot_sha256": snapshot,
        "opinion": opinion,
        "signoff": signoff,
        "signoff_valid": signoff_valid,
        "signoff_failed_rules": signoff_failed,
        "deliverable": deliverable or {
            "state": state, "issued": False, "issuable": False,
            "auto_issue": False, "requires_manual_signoff": True,
        },
        "issuable": bool(opinion_id and all_passed and signoff_valid and not issued),
    }


def _guard_record_kind(job_id: str, kind: str, source: str) -> None:
    """封住绕过通道：签署/出件记录禁止走通用写记录接口；J 线口径不得混用。"""
    if kind in J_RESERVED_RECORD_KINDS:
        raise HTTPException(422, detail={
            "code": "JD_AUTO_ISSUE_FORBIDDEN",
            "message": (f"鉴定出件记录 {kind} 只能由 /v1/jgate/{{job_id}}/signoff|issue "
                        "专用接口写入，禁止通用记录接口伪造签署")})
    job = store.get_job(job_id)
    if not job or job["business_line"] != J_LINE:
        return
    if kind in J_FORBIDDEN_COMPILATION_KINDS:
        raise HTTPException(422, detail={
            "code": "JD_LINE_MIXED",
            "message": f"鉴定线（{J_LINE}）不得出具编制口径成果记录 {kind}"})
    if kind in J_SERVER_ONLY_KINDS and source != "SERVER":
        raise HTTPException(422, detail={
            "code": "JD_GATE_NOT_READY",
            "message": (f"鉴定事实记录 {kind} 必须由服务端写入（/internal）；"
                        "调用方 client 自述不得作为 J 线门禁判据")})


@app.post("/v1/job")
def job_create(req: JobCreate, request: Request):
    from .models import JobValidationError
    from pydantic import ValidationError
    try:
        BasisPack.model_validate(req.basis_pack)
    except ValidationError as exc:
        raise HTTPException(422,detail={'code':'JOB_BASIS_PACK_INVALID','message':str(exc)}) from None
    identity = request.state.identity
    app.state.security.acl.claim(req.project_id, identity)
    req.created_by = identity.subject
    job_id = wf.new_id("job")
    try:
        store.create_job(job_id, req.project_id, req.name, req.business_line, req.basis_pack, req.created_by)
    except JobValidationError as exc:
        raise HTTPException(422,detail={'code':exc.code,'message':str(exc)}) from None
    store.add_audit(req.created_by, "JOB_CREATE", "job", job_id, detail=req.business_line)
    return store.get_job(job_id)


@app.get("/v1/job/{job_id}")
def job_get(job_id: str):
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(404, detail={"code": "INTAKE_GATE_NOT_READY", "message": "job 不存在"})
    job["documents"] = store.list_documents(job_id)
    job["tasks"] = store.list_tasks(job_id)
    job["gate_records"] = store.list_gate_records(job_id)
    job["calc_runs"] = store.list_calc_runs(job_id)
    return stringify(job)


@app.post("/v1/job/{job_id}/documents")
def job_add_document(job_id: str, req: DocumentAdd):
    store.add_document(job_id, req.doc_key, req.version, req.file_sha256, req.verified)
    return {"job_id": job_id, "documents": store.list_documents(job_id)}


@app.post("/v1/job/{job_id}/records")
def job_put_record(job_id: str, req: RecordPut, x_actor: str = Header(default="tester")):
    _guard_record_kind(job_id, req.kind, "CLIENT")
    try:
        out = write_record(store, job_id, req.kind, req.payload, "CLIENT")
    except RecordError as exc:
        raise HTTPException(422, detail={"code": exc.code, "message": exc.message})
    store.add_audit(x_actor, "JOB_RECORD_CLIENT", req.kind, job_id)
    return out


@app.post("/internal/job/{job_id}/records")
def internal_put_record(job_id: str, req: RecordPut, x_internal_token: str = Header(default="")):
    from .security.tokens import constant_time_equals
    expected = os.environ.get("TX_INTERNAL_TOKEN", "")
    if not expected or expected == 'dev-internal' or not constant_time_equals(x_internal_token, expected):
        raise HTTPException(403, detail={"code": "SEAL_NOT_ALLOWED", "message": "内部写入口令无效"})
    _guard_record_kind(job_id, req.kind, "SERVER")
    try:
        out = write_record(store, job_id, req.kind, req.payload, "SERVER")
    except RecordError as exc:
        raise HTTPException(422, detail={"code": exc.code, "message": exc.message})
    store.add_audit("internal", "JOB_RECORD_SERVER", req.kind, job_id)
    return out


@app.get("/v1/job/{job_id}/records")
def job_get_records(job_id: str, kind: str):
    return {"job_id": job_id, "kind": kind, "records": store.get_records(job_id, kind)}


@app.post("/v1/job/{job_id}/tasks")
def task_create(job_id: str, req: TaskCreate):
    try:
        return wf.create_task(store, job_id, req.node, req.payload, req.assignee)
    except wf.WorkflowError as exc:
        raise HTTPException(422, detail={"code": exc.code, "message": exc.message})


@app.get("/v1/task/{task_id}")
def task_get(task_id: str):
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, detail={"code": "INTAKE_GATE_NOT_READY", "message": "task 不存在"})
    return stringify(task)


@app.post("/v1/task/{task_id}/start")
def task_start(task_id: str, action: TaskAction, request: Request):
    try:
        identity = request.state.identity
        return stringify(wf.start_task(store, task_id, identity.subject, "EDITOR"))
    except wf.WorkflowError as exc:
        raise HTTPException(422, detail={"code": exc.code, "message": exc.message})


@app.post("/v1/task/{task_id}/complete")
def task_complete(task_id: str, action: TaskAction, request: Request):
    try:
        identity = request.state.identity
        return stringify(wf.complete_node(store, task_id, identity.subject, action.result, "EDITOR"))
    except wf.WorkflowError as exc:
        raise HTTPException(422, detail={"code": exc.code, "message": exc.message})


@app.post("/v1/task/{task_id}/approve")
def task_approve(task_id: str, action: TaskAction, request: Request):
    identity = request.state.identity
    if action.actor != identity.subject:
        raise HTTPException(403, detail={"code": "TASK_ACTOR_MISMATCH",
                                         "message": "审批人必须与当前登录身份一致"})
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(404, detail={"code": "INTAKE_GATE_NOT_READY", "message": "任务不存在"})
    # The outer ACL guard requires project REVIEW permission. Derive ordinary
    # reviewer authority from that server-side decision; request.role is not an
    # authorization claim. APPROVE additionally requires a server-registered
    # professional credential bound to the authenticated subject.
    workflow_role = "REVIEWER"
    if task["node"] == "APPROVE":
        if not identity.has_role("verified_professional"):
            raise HTTPException(403, detail={"code": "PROFESSIONAL_IDENTITY_REQUIRED",
                                             "message": "审定节点须由服务端核验的专业人员身份完成"})
        license_roles = {"REGISTERED_COST_ENGINEER": "PRINCIPAL", "APPRAISER": "APPRAISER"}
        if license_roles.get(action.license or "") != action.role:
            raise HTTPException(403, detail={"code": "PROFESSIONAL_LICENSE_MISMATCH",
                                             "message": "审定角色与执业资格类型不匹配"})
        if _verified_professional(identity, action.license) is None:
            raise HTTPException(403, detail={"code": "PROFESSIONAL_IDENTITY_REQUIRED",
                                             "message": "尚无与当前登录人及执业资格匹配的服务端核验登记"})
        workflow_role = action.role
    try:
        return stringify(wf.approve_task(store, task_id, identity.subject, workflow_role, action.license, action.comment))
    except wf.WorkflowError as exc:
        raise HTTPException(422, detail={"code": exc.code, "message": exc.message})


@app.post("/v1/task/{task_id}/fail")
def task_fail(task_id: str, action: TaskAction, request: Request):
    try:
        return stringify(wf.fail_task(store, task_id, request.state.identity.subject, action.reason))
    except wf.WorkflowError as exc:
        raise HTTPException(422, detail={"code": exc.code, "message": exc.message})


@app.post("/v1/task/{task_id}/retry")
def task_retry(task_id: str, action: TaskAction, request: Request):
    try:
        return stringify(wf.retry_task(store, task_id, request.state.identity.subject))
    except wf.WorkflowError as exc:
        raise HTTPException(422, detail={"code": exc.code, "message": exc.message})


# ------------------------------------------------------------------ 门禁
@app.post("/v1/gate/check")
def gate_check(req: GateRequest, request: Request, x_actor: str = Header(default="tester")):
    job_id = getattr(req, "job_id", None)
    if req.gate_code in J_GATE_SET and req.business_line != J_LINE:
        raise HTTPException(422, detail={
            "code": "JD_LINE_MIXED",
            "message": f"鉴定门 {req.gate_code} 只适用于鉴定线 {J_LINE}，不得套用到编制线 {req.business_line}"})
    if job_id:
        try:
            out = gate_evaluate(store, job_id, req.gate_code, x_actor, req.business_line)
        except GateError as exc:
            raise HTTPException(422, detail={"code": exc.code, "message": exc.message})
        if req.gate_code in J_GATE_SET:
            # 影子运行标记：判定通过也不等于可签发。
            out.update({"shadow_run": True, "auto_issue": False,
                        "requires_manual_signoff": True})
        return out
    if req.gate_code in J_GATE_SET:
        # 鉴定门禁止"无 job_id 自述放行"；J0/J1 曾被列为提示门，这里显式封死。
        raise HTTPException(422, detail={
            "code": "JD_GATE_NOT_READY",
            "message": ("鉴定门 J0–J5 必须绑定 job_id，由服务端读取真实状态；"
                        "不接受调用方自述的受理/质证结论")})
    if req.gate_code in AUTHORITY_GATES:
        try:
            base = check_gate(req.business_line, req.gate_code, set(req.available_documents),
                              req.basis_pack, req.context)
            failed = list(base.get("failed_rules", []))
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "INTAKE_GATE_NOT_READY", "message": str(exc)})
        failed.append({"rule_id": f"{req.gate_code}.BINDING", "error_code": "REVIEW_SEQUENCE_INVALID",
                       "message": "该门禁必须绑定 job_id，由服务端读取真实状态；调用方自报状态不作为放行依据"})
        return {"gate": req.gate_code, "business_line": req.business_line, "result": "FAIL",
                "failed_rules": failed, "authoritative": False, "context_ignored": True}
    try:
        return check_gate(req.business_line, req.gate_code, set(req.available_documents),
                          req.basis_pack, None)
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "INTAKE_GATE_NOT_READY", "message": str(exc)})


@app.get("/v1/gate/sequence/{line}")
def gate_sequence(line: str):
    if line not in GATE_SEQUENCE:
        raise HTTPException(422, detail={"code": "INTAKE_GATE_NOT_READY", "message": f"未知业务线：{line}"})
    return {"business_line": line, "gates": GATE_SEQUENCE[line]}


# ------------------------------------------------------- P0-05-JGATE 鉴定线接口
@app.get("/v1/jgate")
def jgate_policy():
    """鉴定线影子运行契约（六门序列 / 不可自动签发 / 人工签署要求）。"""
    return j_shadow_policy()


@app.get("/v1/jgate/{job_id}")
def jgate_status(job_id: str):
    """J0–J5 六门结果与出件状态查询（只读，不触发任何签发）。"""
    return _j_view(job_id)


@app.post("/v1/jgate/{job_id}/run")
def jgate_run(job_id: str, req: JRunRequest):
    """按 J0→J5 顺序服务端判定六道鉴定门；只产生 gate_record，不产生成果。"""
    _j_job(job_id)
    results: list[dict[str, Any]] = []
    for code in J_GATES:
        try:
            out = gate_evaluate(store, job_id, code, req.actor)
        except GateError as exc:
            raise HTTPException(422, detail={"code": exc.code, "message": exc.message})
        out.update({"shadow_run": True, "auto_issue": False,
                    "requires_manual_signoff": True})
        results.append(out)
    view = _j_view(job_id)
    return {
        "job_id": job_id,
        "shadow_run": True,
        "auto_issue": False,
        "requires_manual_signoff": True,
        "results": results,
        "gate_results": view["gate_results"],
        "all_gates_passed": view["all_gates_passed"],
        "deliverable_state": view["deliverable_state"],
        "issuable": view["issuable"],
    }


@app.post("/v1/jgate/{job_id}/draft")
def jgate_draft(job_id: str, req: JOpinionDraft, x_actor: str = Header(default="tester")):
    """建立鉴定意见草稿：默认 SHADOW 状态，不可签发。"""
    _j_job(job_id)
    deliverable = _latest_record(job_id, "j_deliverable")
    if deliverable and deliverable.get("issued"):
        raise HTTPException(409, detail={
            "code": "JD_AUTO_ISSUE_FORBIDDEN",
            "message": "该鉴定任务已签发成果，不得再改草稿；请新开鉴定任务"})
    opinion = {
        "opinion_id": wf.new_id("jop"),
        "title": req.title,
        "conclusion_form": req.conclusion_form,
        "evidence_level": req.evidence_level,
        "summary": req.summary,
        "state": "SHADOW",
        "issued": False,
        "issuable": False,
        "auto_issue": False,
        "requires_manual_signoff": True,
        "created_by": x_actor,
        "created_at": _now_iso(),
        "_source": "SERVER",
    }
    store.put_record(job_id, "j_opinion", opinion)
    store.add_audit(x_actor, "JGATE_SHADOW_DRAFT", "job", job_id, detail=opinion["opinion_id"])
    return {"job_id": job_id, "shadow_run": True, **opinion}


@app.post("/v1/jgate/{job_id}/signoff")
def jgate_signoff(job_id: str, req: JSignoffRequest, request: Request):
    """人工签署：六门全通过 + 具体自然人 + 执业资格 + 签署声明，缺一不可。"""
    identity = request.state.identity
    if req.actor != identity.subject:
        raise HTTPException(403, detail={"code": "SIGNOFF_ACTOR_MISMATCH",
                                         "message": "签署人必须与当前登录身份一致"})
    if _verified_professional(identity, req.license) is None:
        raise HTTPException(403, detail={"code": "PROFESSIONAL_IDENTITY_REQUIRED",
                                         "message": "尚无与当前登录人及执业资格匹配的服务端核验登记"})
    _j_job(job_id)
    deliverable = _latest_record(job_id, "j_deliverable")
    if deliverable and deliverable.get("issued"):
        raise HTTPException(409, detail={
            "code": "JD_AUTO_ISSUE_FORBIDDEN",
            "message": "该鉴定任务已签发成果，不得再补签；如需更正请新开鉴定任务"})
    records = store.list_gate_records(job_id)
    latest = latest_gate_results(records)
    opinion = _latest_record(job_id, "j_opinion")
    if not opinion:
        raise HTTPException(422, detail={
            "code": "JD_OPINION_INCOMPLETE", "message": "无鉴定意见草稿，不得签署"})
    opinion_id = req.opinion_id or opinion.get("opinion_id")
    if opinion_id != opinion.get("opinion_id"):
        raise HTTPException(422, detail={
            "code": "JD_OPINION_INCOMPLETE",
            "message": f"签署意见 {opinion_id} 与当前草稿 {opinion.get('opinion_id')} 不一致"})
    unresolved = [gate for gate in J_GATES if latest.get(gate) != "PASS"]
    if unresolved:
        raise HTTPException(422, detail={
            "code": "JD_GATE_NOT_READY",
            "message": f"鉴定六门未全部通过，不得签署：{unresolved}",
            "failed_gates": unresolved})
    signoff = {
        "signoff_id": wf.new_id("jsig"),
        "opinion_id": opinion_id,
        "signed_by": req.actor,
        "license": req.license,
        "statement": req.statement,
        "signed_at": _now_iso(),
        "manual": True,
        "gate_snapshot_sha256": j_gate_snapshot(records),
        "deliverable_state": "SIGNED",
        "issued": False,
        "auto_issue": False,
        "requires_manual_signoff": True,
        "_source": "SERVER",
    }
    problems = check_manual_signoff(signoff, opinion_id)
    if problems:
        raise HTTPException(422, detail={
            "code": problems[0]["error_code"],
            "message": problems[0]["message"],
            "failed_rules": problems})
    store.put_record(job_id, "j_signoff", signoff)
    store.add_audit(req.actor, "JGATE_SIGNOFF", "job", job_id,
                    detail=f"{opinion_id} license={req.license}")
    return {"job_id": job_id, "shadow_run": True, "auto_issue": False, **signoff}


@app.post("/v1/jgate/{job_id}/issue")
def jgate_issue(job_id: str, req: JIssueRequest, request: Request):
    """显式签发：必须已有有效人工签署记录，且签署后门禁未被改判。"""
    identity = request.state.identity
    if req.actor != identity.subject:
        raise HTTPException(403, detail={"code": "TASK_ACTOR_MISMATCH",
                                         "message": "签发操作者必须与当前登录身份一致"})
    _j_job(job_id)
    records = store.list_gate_records(job_id)
    latest = latest_gate_results(records)
    opinion = _latest_record(job_id, "j_opinion")
    signoff = _latest_record(job_id, "j_signoff")
    opinion_id = req.opinion_id or (opinion or {}).get("opinion_id")
    snapshot = j_gate_snapshot(records)
    problems = check_issue_ready(latest, signoff, opinion_id, snapshot)
    if problems:
        raise HTTPException(422, detail={
            "code": problems[0]["error_code"],
            "message": problems[0]["message"],
            "failed_rules": problems,
            "deliverable_state": "SHADOW",
            "issued": False,
            "auto_issue": False})
    existing = _latest_record(job_id, "j_deliverable")
    if existing and existing.get("issued"):
        return {"job_id": job_id, "idempotent_replay": True, **existing}
    deliverable = {
        "deliverable_id": wf.new_id("jdel"),
        "opinion_id": opinion_id,
        "signoff_id": signoff.get("signoff_id"),
        "signed_by": signoff.get("signed_by"),
        "license": signoff.get("license"),
        "signed_at": signoff.get("signed_at"),
        "gate_snapshot_sha256": snapshot,
        "state": "ISSUED",
        "issued": True,
        "issued_at": _now_iso(),
        "issued_by": signoff.get("signed_by"),
        "manual_signoff": True,
        "auto_issue": False,
        "requires_manual_signoff": True,
        "_source": "SERVER",
    }
    store.put_record(job_id, "j_deliverable", deliverable)
    store.add_audit(identity.subject, "JGATE_ISSUE", "job", job_id,
                    detail=f"{opinion_id} signoff={signoff.get('signoff_id')}")
    return {"job_id": job_id, "auto_issue": False, **deliverable}


# ------------------------------------------------------------------ 证据与审计
@app.get("/v1/evidence/{eid}")
def evidence_get(eid: str):
    r = store.get(eid)
    if not r:
        raise HTTPException(404, detail={"code": "EVIDENCE_NOT_FOUND", "message": "证据不存在"})
    return r


@app.post("/v1/evidence/verify")
def evidence_verify(payload: dict):
    return verify_evidence(payload)


@app.get("/v1/audit")
def audit(limit: int = 50):
    return {"items": store.list_audit(limit)}


# V4.2.4: the old deterministic entry inventory remains intact; the extension has
# its own route inventory/tests. The outer guard covers BOTH applications.
from .prd_v2_extensions import router as prd_v2_router
app.include_router(prd_v2_router)

from .security.runtime import runtime_context, install_business_guard
from .security.app import install_security
from .pilot.api import create_pilot_api
from .governance.api import create_governance_api
app.state.security = runtime_context(RUNTIME_ROOT)
install_security(app, app.state.security)
app.mount('/v1/governance', create_governance_api(app, RUNTIME_ROOT))
app.mount('/', create_pilot_api(app, lambda: store, RUNTIME_ROOT))
install_business_guard(app, lambda: store)
