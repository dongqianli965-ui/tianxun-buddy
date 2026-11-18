"""V4.2.3 / P0-02 幂等语义测试：数据库级原子认领（复核冻结轮）。

对照 tasks/P0-02-IDEM/TASK.md 的四条验收语义：
  (a) 陈旧 IN_PROGRESS 认领在 TTL 过后可被另一个 claim 接管，且只有一个成功；
  (b) handler 抛异常后 idem_release 生效，同键同参可再次成功执行；
  (c) 同键异指纹仍返回 409；
  (d) DONE 后重放返回 idem_replay=true 且不重复执行业务。

补充 (e)：两个独立 EvidenceStore 实例（模拟两个进程）共享同一库文件时，认领裁判仍然只有
数据库一处 —— 证明判定不依赖任何进程内内存状态。

约定：所有用例使用 uuid 唯一幂等键，避免 data/txbuddy-dev.sqlite3 跨轮次残留互相干扰；
用例结束删除自己写入的 idem 行，不污染其它测试。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Body, FastAPI, HTTPException, Request
from tests.authenticated_client import TestClient

from txbuddy.api import _idem, app, registry, store
from txbuddy.store import EvidenceStore

client = TestClient(app)

RULE_CODE = "QTY.BOQ.CONC.COLUMN.001"
PATH_RULE_CALC = "/v1/quantity/rule-calc"
PATH_PROBE = "/v1/_idem/probe"


# ------------------------------------------------------------------ helpers
def _basis(**kw):
    """与 test_v421_hardening.basis() 等价的 LOCKED 依据包，便于复用同一规则。"""
    b = dict(basis_pack_id="B-IDEM", region_code="410100", region_level="CITY", major="BUILDING",
             effective_date="2026-01-01", measurement_standard_version="GB/T 50854-2024 [已核]",
             pricing_standard_version="GB/T 50500-2024 [已核]", rule_version_scope="HENAN_2025",
             precision_rule_version="P-V1", status="LOCKED", test_only=False,
             sources=[{"source_id": "S1", "title": "河南定额", "source_type": "QUOTA",
                       "license_status": "LICENSED", "verified": True}])
    b.update(kw)
    return b


def _calc_body(k: float = 1.0) -> dict:
    return {"basis_pack": _basis(), "rule_code": RULE_CODE,
            "params": {"b": str(k), "h": "0.5", "H": "3"}}


def _key(prefix: str) -> str:
    return prefix + "-" + uuid.uuid4().hex[:12]


def _scope(actor: str, path: str, key: str) -> str:
    return store.idem_scope("", actor, "POST", path, key)


def _row_of(st, scope_key: str):
    """直接读库：返回 (fingerprint, state, response, claimed_at) 或 None。"""
    c = st._conn()
    try:
        return c.execute("SELECT fingerprint,state,response,claimed_at FROM idem WHERE scope_key=?",
                         (scope_key,)).fetchone()
    finally:
        c.close()


def _idem_row(scope_key: str):
    """生产 store（data/txbuddy-dev.sqlite3）里的幂等行。"""
    return _row_of(store, scope_key)


def _drop_idem(scope_key: str) -> None:
    c = store._conn()
    try:
        c.execute("DELETE FROM idem WHERE scope_key=?", (scope_key,))
    finally:
        c.close()


# ---------------------------------------------------------------------- (a)
def test_a_stale_in_progress_claim_is_taken_over_exactly_once(tmp_path):
    """陈旧 IN_PROGRESS：TTL 内不可抢，TTL 过期后并发接管只允许一个成功。"""
    st = EvidenceStore(str(tmp_path / "idem-takeover.sqlite3"))
    scope = st.idem_scope("T-TTL", "u-ttl", "POST", PATH_RULE_CALC, "k-ttl")
    fp = "fp-ttl-fixed"

    assert st.idem_claim(scope, fp, "T-TTL", "u-ttl") == ("claimed", None)
    # 认领尚未过期：同键同指纹的第二个请求不得抢走执行权
    assert st.idem_claim(scope, fp, "T-TTL", "u-ttl") == ("in_progress", None)

    # 把 claimed_at 拨回 600 秒前，模拟持有者进程崩溃留下的陈旧认领
    stale = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    c = st._conn()
    try:
        c.execute("UPDATE idem SET claimed_at=?, response=NULL WHERE scope_key=?", (stale, scope))
    finally:
        c.close()

    states: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        state, cached = st.idem_claim(scope, fp, "T-TTL", "u-ttl", ttl_seconds=300)
        with lock:
            states.append(state)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert len(states) == 8, states
    assert states.count("claimed") == 1, f"陈旧认领只能被一个请求接管，实际 {states}"
    assert states.count("in_progress") == 7, f"其余请求必须被拒为 in_progress，实际 {states}"

    row = _row_of(st, scope)  # 同一条库记录（tmp 库）
    assert row is not None
    assert row[1] == "IN_PROGRESS", "接管后仍是未完成认领"
    assert row[3] != stale, "接管必须原子改写 claimed_at（CAS）"

    # 接管者写回响应后，后续同键同指纹走重放而不是再执行
    assert st.idem_finish(scope, {"result": "1.5"}) is True
    replay_state, cached = st.idem_claim(scope, fp, "T-TTL", "u-ttl")
    assert replay_state == "hit"
    assert cached is not None and cached["idem_replay"] is True
    assert cached["result"] == "1.5"


# ---------------------------------------------------------------------- (b)
def _probe_app() -> tuple[FastAPI, dict]:
    """用生产 _idem 包一层可控 handler：第一次抛 HTTPException（与规则引擎失败同构），之后正常返回。"""
    probe = FastAPI()
    state = {"executions": 0, "failures": 1}

    @probe.post(PATH_PROBE)
    async def probe_endpoint(request: Request, body: dict = Body(...)):
        def handler():
            state["executions"] += 1
            if state["failures"] > 0:
                state["failures"] -= 1
                raise HTTPException(422, detail={"code": "PROBE_HANDLER_ERROR", "message": "业务失败"})
            return {"ok": True, "execution": state["executions"], "echo": body.get("n")}

        return _idem(request, body, handler)

    return probe, state


def test_b_handler_error_releases_claim_and_allows_retry():
    """handler 抛异常 → idem_release 删除 IN_PROGRESS 行 → 同键同参可真正重跑一次。"""
    probe, state = _probe_app()
    c = TestClient(probe)
    key = _key("vk-release")
    scope = _scope("anonymous", PATH_PROBE, key)
    _drop_idem(scope)
    try:
        first = c.post(PATH_PROBE, json={"n": 1}, headers={"X-Idem-Key": key})
        assert first.status_code == 422, first.text
        assert first.json()["detail"]["code"] == "PROBE_HANDLER_ERROR"
        assert state["executions"] == 1
        assert _idem_row(scope) is None, "handler 抛异常后必须释放认领，不得留下 IN_PROGRESS"

        second = c.post(PATH_PROBE, json={"n": 1}, headers={"X-Idem-Key": key})
        assert second.status_code == 200, second.text
        assert second.json()["ok"] is True
        assert state["executions"] == 2, "释放后同键同参必须真正重新执行业务"

        row = _idem_row(scope)
        assert row is not None and row[1] == "DONE" and row[2] is not None

        third = c.post(PATH_PROBE, json={"n": 1}, headers={"X-Idem-Key": key})
        assert third.status_code == 200
        assert third.json()["idem_replay"] is True
        assert state["executions"] == 2, "DONE 之后只能重放，不得再执行"
    finally:
        _drop_idem(scope)


# ---------------------------------------------------------------------- (c)
def test_c_same_key_different_fingerprint_still_409():
    """同键异指纹：即使原请求已 DONE，也必须 409 而不是返回旧结果。"""
    key = _key("vk-conflict")
    scope = _scope("anonymous", PATH_RULE_CALC, key)
    _drop_idem(scope)
    try:
        first = client.post(PATH_RULE_CALC, json=_calc_body(1.0), headers={"X-Idem-Key": key})
        assert first.status_code == 200, first.text

        conflict = client.post(PATH_RULE_CALC, json=_calc_body(2.0), headers={"X-Idem-Key": key})
        assert conflict.status_code == 409, conflict.text
        payload = conflict.json()
        assert payload["code"] == "IDEM_KEY_CONFLICT"
        assert payload["idem_key"] == key

        row = _idem_row(scope)
        assert row is not None and row[1] == "DONE", "冲突请求不得覆盖已完成的响应"

        replay = client.post(PATH_RULE_CALC, json=_calc_body(1.0), headers={"X-Idem-Key": key})
        assert replay.status_code == 200
        assert replay.json()["idem_replay"] is True
        assert replay.json()["calc_run_id"] == first.json()["calc_run_id"]
    finally:
        _drop_idem(scope)


# ---------------------------------------------------------------------- (d)
def test_d_done_replay_does_not_reexecute_business(monkeypatch):
    """DONE 后重放：idem_replay=true，业务只执行一次，结果一致。"""
    key = _key("vk-replay")
    scope = _scope("anonymous", PATH_RULE_CALC, key)
    _drop_idem(scope)
    calls = {"n": 0}
    original = registry.execute

    def counted(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, "execute", counted)
    try:
        first = client.post(PATH_RULE_CALC, json=_calc_body(1.0), headers={"X-Idem-Key": key})
        assert first.status_code == 200, first.text
        assert calls["n"] == 1
        assert first.json().get("idem_replay") is not True, "首次执行不是重放"

        second = client.post(PATH_RULE_CALC, json=_calc_body(1.0), headers={"X-Idem-Key": key})
        assert second.status_code == 200, second.text
        assert second.json()["idem_replay"] is True
        assert calls["n"] == 1, "重放不得再次执行业务代码"
        assert second.json()["calc_run_id"] == first.json()["calc_run_id"]
        assert second.json()["evidence_sha256"] == first.json()["evidence_sha256"]

        row = _idem_row(scope)
        assert row is not None and row[1] == "DONE"
    finally:
        _drop_idem(scope)


# ---------------------------------------------------------------------- (e)
def test_e_two_store_instances_share_one_claim(tmp_path):
    """两个独立实例（模拟两个进程）共用同一库：裁判只在数据库，不在进程内存。"""
    db = str(tmp_path / "idem-shared.sqlite3")
    a, b = EvidenceStore(db), EvidenceStore(db)
    scope = a.idem_scope("T-SHARED", "u-shared", "POST", PATH_RULE_CALC, "k-shared")
    assert a.idem_claim(scope, "fp-1", "T-SHARED", "u-shared") == ("claimed", None)
    assert b.idem_claim(scope, "fp-1", "T-SHARED", "u-shared") == ("in_progress", None)
    assert b.idem_claim(scope, "fp-2", "T-SHARED", "u-shared") == ("conflict", None)
    assert a.idem_finish(scope, {"v": 1}) is True
    state, cached = b.idem_claim(scope, "fp-1", "T-SHARED", "u-shared")
    assert state == "hit" and cached is not None
    assert cached["idem_replay"] is True and cached["v"] == 1
