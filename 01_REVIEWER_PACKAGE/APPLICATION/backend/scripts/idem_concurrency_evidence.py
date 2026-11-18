"""V4.2.3 幂等并发取证：两个场景各独立运行 N 次，记录真实执行次数、数据库认领结果、请求指纹。

用法（在 backend/ 下）：
    .venv/Scripts/python.exe scripts/idem_concurrency_evidence.py --runs 10

产出：
    ../logs/idem_evidence_<UTC时间戳>.json   逐次明细
    ../logs/idem_evidence_<UTC时间戳>.md     人读汇总
不修改任何业务代码；只在运行前清空本次使用的幂等 scope。
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from txbuddy.api import app, store  # noqa: E402

client = TestClient(app)
LOGS = BACKEND.parent / "logs"
TENANT = "T-EVIDENCE"

# 真实执行次数不能靠 evidence 行数差：evidence_id 由内容决定，重复执行会覆盖同一行。
# 这里直接包住 store.save（业务函数每次执行必调一次）做计数，counted_save 只在本脚本内生效。
_EXEC_COUNTER = {"n": 0}
_ORIG_SAVE = store.save


def _counted_save(e):
    _EXEC_COUNTER["n"] += 1
    return _ORIG_SAVE(e)


store.save = _counted_save

N_THREADS = 50
KEY_SAME = "vk-evidence-same"
KEY_CONFLICT = "vk-evidence-conflict"


def basis(**kw):
    b = dict(basis_pack_id="B-OK", region_code="410100", region_level="CITY", major="BUILDING",
             effective_date="2026-01-01", measurement_standard_version="GB/T 50854-2024 [已核]",
             pricing_standard_version="GB/T 50500-2024 [已核]", rule_version_scope="HENAN_2025",
             precision_rule_version="P-V1", status="LOCKED", test_only=False,
             sources=[{"source_id": "S1", "title": "河南定额", "source_type": "QUOTA",
                       "license_status": "LICENSED", "verified": True}])
    b.update(kw)
    return b


def calc_body(k: float = 1.0):
    return {"basis_pack": basis(), "rule_code": "QTY.BOQ.CONC.COLUMN.001",
            "params": {"b": str(k), "h": "0.5", "H": "3"}}


def reset_scopes(*keys: str) -> None:
    c = store._conn()
    try:
        for k in keys:
            c.execute("DELETE FROM idem WHERE scope_key LIKE ?", ("%|" + k,))
    finally:
        c.close()


def idem_row(actor: str, key: str):
    scope = store.idem_scope(TENANT, actor, "POST", "/v1/quantity/rule-calc", key)
    c = store._conn()
    try:
        row = c.execute("SELECT fingerprint,state,response IS NOT NULL,claimed_at,at FROM idem WHERE scope_key=?",
                        (scope,)).fetchone()
    finally:
        c.close()
    if not row:
        return None
    return {"scope_key": scope, "fingerprint_prefix": row[0][:16], "state": row[1],
            "response_written": bool(row[2]), "claimed_at": row[3], "finished_at": row[4]}


def run_batch(differ_params: bool, actor: str, key: str) -> dict:
    codes = []
    results = []
    errors = []
    fps_seen = []
    lock = threading.Lock()

    def worker(i: int):
        try:
            body = calc_body(1.0 + i / 100) if differ_params else calc_body()
            r = client.post("/v1/quantity/rule-calc", json=body,
                            headers={"X-Idem-Key": key, "X-Actor": actor, "X-Tenant-Id": TENANT})
            payload = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            from txbuddy.api import _fingerprint
            with lock:
                codes.append(r.status_code)
                results.append(payload.get("result"))
                fps_seen.append(_fingerprint(body))
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(type(exc).__name__ + ": " + str(exc))

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
    t0 = time.perf_counter()
    [t.start() for t in ts]
    [t.join() for t in ts]
    elapsed = round(time.perf_counter() - t0, 3)
    fp_counter = Counter(fps_seen)
    return {"elapsed_s": elapsed, "status_counts": dict(Counter(codes)),
            "distinct_results": len({r for r in results if r is not None}),
            "distinct_fingerprints": len(fp_counter),
            "fingerprint_prefixes": sorted(f[:16] for f in fp_counter)[:3],
            "errors": errors[:5], "n_requests": len(codes) + len(errors)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=10)
    args = ap.parse_args()

    LOGS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    records = []
    overall_ok = True

    for idx in range(1, args.runs + 1):
        for scenario, differ, actor, key in (
            ("SAME_REQUEST_SINGLE_EXECUTION", False, "u-evidence-same", KEY_SAME),
            ("DIFFERENT_PARAMS_CONFLICT", True, "u-evidence-conflict", KEY_CONFLICT),
        ):
            reset_scopes(key)
            _EXEC_COUNTER["n"] = 0
            evidence_before = store.evidence_count()
            batch = run_batch(differ, actor, key)
            executions = _EXEC_COUNTER["n"]
            evidence_added = store.evidence_count() - evidence_before
            row = idem_row(actor, key)
            item = {"run": idx, "scenario": scenario, "executions_observed": executions,
                    "evidence_rows_added": evidence_added, "db_claim": row}
            item.update(batch)
            if scenario == "SAME_REQUEST_SINGLE_EXECUTION":
                item["expect"] = {"executions_observed": 1, "distinct_results": 1,
                                  "allowed_status": "200>=1 且结果唯一，其余 409/425/503"}
                passed = (executions == 1
                          and batch["status_counts"].get(200, 0) >= 1
                          and batch["distinct_results"] == 1
                          and not batch["errors"]
                          and (row or {}).get("state") == "DONE")
            else:
                item["expect"] = {"status_200": "<=1", "executions_observed": "<=1", "409": "必须出现"}
                passed = (batch["status_counts"].get(200, 0) <= 1
                          and batch["status_counts"].get(409, 0) >= 1
                          and executions <= 1
                          and not batch["errors"])
            item["passed"] = bool(passed)
            overall_ok = overall_ok and bool(passed)
            records.append(item)
            print(json.dumps(item, ensure_ascii=False))

    payload = {"generated_at": stamp, "runs": args.runs, "threads_per_run": N_THREADS,
               "all_passed": overall_ok, "records": records}
    (LOGS / ("idem_evidence_" + stamp + ".json")).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = ["# 幂等并发取证 " + stamp, "",
             "- 场景 2 × 每个 " + str(args.runs) + " 轮，每轮 " + str(N_THREADS) + " 并发线程",
             "- 总体结果：" + ("PASS" if overall_ok else "FAIL"), "",
             "| 轮 | 场景 | 状态码分布 | 真实执行次数 | DB认领状态 | 不同结果数 | 结论 |",
             "| -- | ---- | ---------- | ------------ | ---------- | ---------- | ---- |"]
    for r in records:
        lines.append("| " + str(r["run"]) + " | " + r["scenario"] + " | " + str(r["status_counts"])
                     + " | " + str(r["executions_observed"]) + " | "
                     + str((r["db_claim"] or {}).get("state", "NO_ROW")) + " | "
                     + str(r["distinct_results"]) + " | " + ("PASS" if r["passed"] else "FAIL") + " |")
    (LOGS / ("idem_evidence_" + stamp + ".md")).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("")
    print("ARTIFACT=" + str(LOGS / ("idem_evidence_" + stamp + ".md")))
    print("ALL_PASSED=" + str(overall_ok))
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())