"""P0-04 业务记录写入治理：区分 SERVER 事实与 CLIENT 声明，并强制证据绑定。

strict 模式（TX_STRICT_GATES=1）下，门禁只接受 SERVER 记录，且要求绑定真实凭据：
  reconcile → calc_run_ids（必须能在 calc_run 表中查到，且属于同一 job）
  price     → source_ref + evidence_id
  report    → report_sha256 + evidence_ids
  archive   → package_sha256 + volume_no

本模块是"绑定"这件事的唯一裁判：
- 写入时 write_record 用 missing_bindings 拒收不合格的服务端记录；
- 门禁判定时 gates_service 用同一个 missing_bindings，避免"写得进、判得松"两套标准。
"""
from __future__ import annotations
import os
from typing import Any

BINDING_RULES = {
    "reconcile": [("calc_run_ids", list)],
    "price": [("source_ref", str), ("evidence_id", str)],
    "report": [("report_sha256", str), ("evidence_ids", list)],
    "archive": [("package_sha256", str), ("volume_no", str)],
}
SERVER_KINDS = set(BINDING_RULES)


class RecordError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def strict_mode() -> bool:
    return os.environ.get("TX_STRICT_GATES", "0") == "1"


def missing_bindings(store, kind: str, payload: dict[str, Any] | None,
                     job_id: str | None = None) -> list[str]:
    """返回缺失/无效的绑定项（字段名或 `字段:值`），空列表表示绑定齐全。

    不抛异常，供门禁判定复用；`calc_run_ids` 额外校验存在性与归属。
    """
    payload = payload or {}
    missing: list[str] = []
    for key, typ in BINDING_RULES.get(kind, []):
        value = payload.get(key)
        if not isinstance(value, typ) or not value:
            missing.append(key)
    if kind == "reconcile" and "calc_run_ids" not in missing:
        for rid in payload["calc_run_ids"]:
            run = store.get_calc_run(str(rid))
            if not run:
                missing.append(f"calc_run_ids:{rid}(not-found)")
            elif job_id and run.get("job_id") and run["job_id"] != job_id:
                missing.append(f"calc_run_ids:{rid}(foreign-job)")
    return missing


def write_record(store, job_id: str, kind: str, payload: dict[str, Any], source: str) -> dict[str, Any]:
    if source not in {"SERVER", "CLIENT"}:
        raise RecordError("INTAKE_GATE_NOT_READY", f"未知记录来源：{source}")
    if source == "SERVER" and kind in SERVER_KINDS:
        missing = missing_bindings(store, kind, payload, job_id)
        if missing:
            field = missing[0].split(":", 1)[0]
            if field == "calc_run_ids":
                raise RecordError("GATE_QTY_RECONCILE_FAILED",
                                  f"勾稽绑定的 calc_run 无效：{missing[0]}")
            raise RecordError("REPORT_GATE_NOT_READY", f"{kind} 服务端记录缺少绑定字段：{field}")
    body = dict(payload)
    body["_source"] = source
    store.put_record(job_id, kind, body)
    return {"job_id": job_id, "kind": kind, "source": source, "saved": True,
            "binding_checked": kind in SERVER_KINDS and source == "SERVER",
            "binding_missing": []}


def require_server_record(store, job_id: str, kind: str) -> list[dict[str, Any]]:
    """门禁读取：strict 模式下只返回 SERVER 记录。"""
    records = store.get_records(job_id, kind)
    if strict_mode():
        return [r for r in records if r.get("_source") == "SERVER"]
    return records


def is_server_record(record: dict[str, Any] | None) -> bool:
    return bool(record) and record.get("_source") == "SERVER"
