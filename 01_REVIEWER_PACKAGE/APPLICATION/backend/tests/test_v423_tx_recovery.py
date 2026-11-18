"""V4.2.3 / P1-05-TX-RECOVERY：事务边界与异常恢复测试。

验收语义（对照 tasks/P1-05-TX-RECOVERY/TASK.md）：
  1. 证据 + 复算 + 幂等响应同事务：要么三者一起可见，要么一起消失；
  2. 注入异常后库里无残留半成品（证据、复算、幂等行、业务表都不留）；
  3. 回滚后重试状态一致，且同键重放不重复执行业务；
  4. 事务内的写入对其它连接不可见，提交后才可见；
  5. 嵌套保存点只回滚内层；rollback-only 即使无异常也不提交；
  6. 现有业务门面（EvidenceStore / RepositorySet）的写路径自动加入事务，无需改调用方。

约定：每个用例使用独立的 ``tmp_path`` 库文件，不触碰 data/txbuddy-dev.sqlite3。
"""
from __future__ import annotations

import threading
import uuid

import pytest

from txbuddy.repository import (
    UnitOfWorkAbort,
    UnitOfWorkError,
    active_transaction,
    build_sqlite_repositories,
)
from txbuddy.store import EvidenceStore


# ------------------------------------------------------------------ helpers
def _repos(tmp_path, name: str = "tx.sqlite3"):
    return build_sqlite_repositories(str(tmp_path / name))


def _evidence(eid: str, job_id: str = "J-TX", **extra) -> dict:
    payload = {"evidence_id": eid, "sha256": f"sha-{eid}", "created_at": "2026-01-01T00:00:00Z",
               "job_id": job_id, "input_snapshot": {"qty": "1"}, "basis_pack_id": "B-TX",
               "rule_version": "R1", "precision_rule_version": "P-V1"}
    payload.update(extra)
    return payload


def _calc_run(rid: str, job_id: str = "J-TX", evidence_id: str = "E1", **extra) -> dict:
    run = {"calc_run_id": rid, "job_id": job_id, "rule_code": "QTY.BOQ.CONC.COLUMN.001",
           "rule_version": "R1", "basis_pack_id": "B-TX", "snapshot_sha256": f"snap-{rid}",
           "snapshot": {"b": "1", "h": "0.5"}, "result": "1.5", "unit": "m3",
           "evidence_id": evidence_id, "created_at": "2026-01-01T00:00:00Z"}
    run.update(extra)
    return run


def _scope(repos, key: str | None = None, path: str = "/v1/quantity/rule-calc") -> str:
    return repos.idem.scope_key("T-TX", "u-tx", "POST", path, key or uuid.uuid4().hex[:12])


def _idem_row(repos, scope_key: str):
    """直接读库确认幂等行是否真的存在（不经过仓储抽象）。"""
    conn = repos.database.connect()
    try:
        return conn.execute(
            "SELECT fingerprint,state,response FROM idem WHERE scope_key=?", (scope_key,)
        ).fetchone()
    finally:
        conn.close()


def _rows(repos, sql: str, params: tuple = ()):
    conn = repos.database.connect()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ------------------------------------------------------- 1. 同事务提交
def test_evidence_calc_and_idem_commit_together(tmp_path):
    """正常路径：证据、复算、幂等响应在同一事务里一起生效。"""
    _database, repos = _repos(tmp_path)
    scope = _scope(repos)
    assert repos.idem.claim(scope, "fp-ok")[0] == "claimed"

    saved = repos.commit_calc_bundle(
        evidence=[_evidence("E-OK")],
        calc_runs=[_calc_run("R-OK", evidence_id="E-OK")],
        idem_scope_key=scope,
        idem_response={"calc_run_id": "R-OK", "evidence_id": "E-OK"},
    )

    assert saved == {"evidence": 1, "calc_run": 1, "idem": 1}
    assert repos.evidence.get("E-OK") is not None
    assert repos.calc_run.get("R-OK") is not None
    row = _idem_row(repos, scope)
    assert row is not None and row[1] == "DONE"
    assert repos.idem.lookup(scope, "fp-ok")[0] == "hit"


# --------------------------------------------- 2. 注入异常 → 无残留
def test_injected_exception_rolls_back_every_write(tmp_path):
    """体内抛异常：证据、复算、幂等认领全部回滚，库里没有任何半成品。"""
    _database, repos = _repos(tmp_path)
    scope = _scope(repos)

    with pytest.raises(RuntimeError, match="injected-boom"):
        with repos.unit_of_work(label="inject"):
            assert repos.idem.claim(scope, "fp-boom")[0] == "claimed"
            repos.evidence.save(_evidence("E-BOOM"))
            repos.calc_run.save(_calc_run("R-BOOM", evidence_id="E-BOOM"))
            repos.idem.finish(scope, {"calc_run_id": "R-BOOM"})
            assert repos.evidence.get("E-BOOM") is not None, "事务体内必须能读到自己的写入"
            raise RuntimeError("injected-boom")

    assert repos.evidence.get("E-BOOM") is None, "证据不得残留"
    assert repos.calc_run.get("R-BOOM") is None, "复算不得残留"
    assert _idem_row(repos, scope) is None, "幂等行（含 IN_PROGRESS 认领）不得残留"
    assert _rows(repos, "SELECT COUNT(*) FROM evidence")[0][0] == 0
    assert _rows(repos, "SELECT COUNT(*) FROM calc_run")[0][0] == 0


def test_real_serialization_failure_after_evidence_leaves_no_residue(tmp_path):
    """真实故障注入：calc_run.snapshot 不可序列化 → 证据已写也要整段回滚，重试后只有一份。"""
    _database, repos = _repos(tmp_path)
    scope = _scope(repos)

    with pytest.raises(UnitOfWorkAbort, match="序列化失败"):
        with repos.unit_of_work():
            assert repos.idem.claim(scope, "fp-json")[0] == "claimed"
            repos.evidence.save(_evidence("E-JSON"))
            with pytest.raises(TypeError):
                repos.calc_run.save(
                    _calc_run("R-JSON", snapshot={"bad": object()}, evidence_id="E-JSON"))
            raise UnitOfWorkAbort("复算快照序列化失败")

    assert repos.evidence.get("E-JSON") is None
    assert repos.calc_run.get("R-JSON") is None
    assert _idem_row(repos, scope) is None

    # 重试：同一幂等键重新认领成功，写入完整且唯一
    assert repos.idem.claim(scope, "fp-json")[0] == "claimed"
    repos.commit_calc_bundle(evidence=[_evidence("E-JSON")],
                             calc_runs=[_calc_run("R-JSON", evidence_id="E-JSON")],
                             idem_scope_key=scope, idem_response={"calc_run_id": "R-JSON"})
    assert repos.evidence.get("E-JSON") is not None
    assert repos.calc_run.get("R-JSON") is not None
    assert len(repos.calc_run.list_by_job("J-TX")) == 1
    assert len(repos.evidence.list_by_job("J-TX")) == 1
    assert repos.idem.lookup(scope, "fp-json")[0] == "hit"


def test_lost_idem_claim_aborts_bundle_without_partial_rows(tmp_path):
    """幂等认领已失效（finish 返回 False）→ 整个打包提交回滚，不留证据/复算。"""
    _database, repos = _repos(tmp_path)
    scope = _scope(repos)
    assert repos.idem.claim(scope, "fp-lost")[0] == "claimed"
    assert repos.idem.release(scope) is True, "模拟认领被释放/进程崩溃回收"

    with pytest.raises(UnitOfWorkAbort, match="幂等认领已失效"):
        repos.commit_calc_bundle(
            evidence=[_evidence("E-LOST")],
            calc_runs=[_calc_run("R-LOST", evidence_id="E-LOST")],
            idem_scope_key=scope, idem_response={"calc_run_id": "R-LOST"})

    assert repos.evidence.get("E-LOST") is None
    assert repos.calc_run.get("R-LOST") is None
    assert _idem_row(repos, scope) is None


def test_partial_batch_failure_rolls_back_earlier_rows(tmp_path):
    """批次写到一半失败：同批次更早写入的证据/审计也必须回滚，不能留下半批。"""
    _database, repos = _repos(tmp_path)

    with pytest.raises(TypeError):
        with repos.unit_of_work():
            repos.evidence.save(_evidence("E-BATCH-1"))
            repos.audit.add("u-tx", "BATCH_WRITE", "evidence", "E-BATCH-1", detail="batch")
            repos.evidence.save(_evidence("E-BATCH-2", extra={"bad": object()}))

    assert repos.evidence.get("E-BATCH-1") is None
    assert repos.evidence.get("E-BATCH-2") is None
    assert [a for a in repos.audit.list() if a["action"] == "BATCH_WRITE"] == []


# ------------------------------------------- 3. 重试后状态一致（幂等）
def test_retry_after_rollback_is_consistent_and_replay_is_stable(tmp_path):
    """首次失败回滚 → 重试成功 → 再重放：状态与计数完全一致，不重复执行。"""
    _database, repos = _repos(tmp_path)
    scope = _scope(repos)

    with pytest.raises(ValueError):
        with repos.unit_of_work():
            assert repos.idem.claim(scope, "fp-retry")[0] == "claimed"
            repos.evidence.save(_evidence("E-RETRY"))
            raise ValueError("first attempt failed")
    assert repos.evidence.get("E-RETRY") is None

    # 回滚释放了认领：同键同指纹必须能重新拿到执行权（而不是卡在 in_progress）
    assert repos.idem.claim(scope, "fp-retry")[0] == "claimed"
    repos.commit_calc_bundle(
        evidence=[_evidence("E-RETRY")],
        calc_runs=[_calc_run("R-RETRY", evidence_id="E-RETRY")],
        idem_scope_key=scope, idem_response={"calc_run_id": "R-RETRY", "result": "1.5"})

    state, cached = repos.idem.claim(scope, "fp-retry")
    assert state == "hit" and cached is not None
    assert cached["idem_replay"] is True
    assert cached["calc_run_id"] == "R-RETRY"
    assert len(repos.calc_run.list_by_job("J-TX")) == 1
    assert len(repos.evidence.list_by_job("J-TX")) == 1
    assert _rows(repos, "SELECT COUNT(*) FROM evidence")[0][0] == 1


# ------------------------------------- 4. 未提交写入对其它连接不可见
def test_uncommitted_rows_are_invisible_to_other_connections(tmp_path):
    """事务体内写入对独立连接不可见；提交后才可见。"""
    database, repos = _repos(tmp_path)
    with repos.unit_of_work():
        repos.evidence.save(_evidence("E-VIS"))
        reader = database.connect()
        try:
            assert reader.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0
        finally:
            reader.close()
        assert repos.evidence.get("E-VIS") is not None
    assert repos.evidence.get("E-VIS") is not None
    reader = database.connect()
    try:
        assert reader.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 1
    finally:
        reader.close()


# ------------------------------------- 5. 嵌套保存点 / rollback-only
def test_nested_boundary_rolls_back_only_inner_writes(tmp_path):
    """内层失败只回滚内层写入（SAVEPOINT），外层可在捕获异常后继续提交。"""
    _database, repos = _repos(tmp_path)
    with repos.unit_of_work():
        repos.evidence.save(_evidence("E-OUTER"))
        with pytest.raises(ValueError, match="inner"):
            with repos.unit_of_work():
                repos.evidence.save(_evidence("E-INNER"))
                assert repos.evidence.get("E-INNER") is not None
                raise ValueError("inner")
        assert repos.evidence.get("E-OUTER") is not None, "外层写入必须保留"
        assert repos.evidence.get("E-INNER") is None, "内层写入必须回滚到保存点"
    assert repos.evidence.get("E-OUTER") is not None
    assert repos.evidence.get("E-INNER") is None


def test_rollback_only_forces_rollback_without_exception(tmp_path):
    """rollback_only：即使 with 体内没异常，也不得提交。"""
    _database, repos = _repos(tmp_path)
    scope = _scope(repos)
    with repos.unit_of_work() as uow:
        repos.evidence.save(_evidence("E-RO"))
        assert repos.idem.claim(scope, "fp-ro")[0] == "claimed"
        uow.set_rollback_only()
    assert repos.evidence.get("E-RO") is None
    assert _idem_row(repos, scope) is None


# ------------------------------------- 6. 线程隔离 / 生命周期纪律
def test_active_transaction_is_not_shared_across_threads(tmp_path):
    """事务状态只属于开启它的线程：另一线程拿不到共享连接，也读不到未提交行。"""
    database, repos = _repos(tmp_path)
    key = str(database.path.resolve())
    seen: dict = {}

    with repos.unit_of_work():
        repos.evidence.save(_evidence("E-THREAD"))
        assert active_transaction(key) is not None

        def worker() -> None:
            seen["state"] = active_transaction(key)
            seen["row"] = repos.evidence.get("E-THREAD")
            seen["count"] = _rows(repos, "SELECT COUNT(*) FROM evidence")[0][0]

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

    assert seen["state"] is None, "另一线程不得看到本线程的活动事务"
    assert seen["row"] is None, "未提交写入对其它线程不可见"
    assert seen["count"] == 0
    assert repos.evidence.get("E-THREAD") is not None


def test_finished_unit_of_work_cannot_be_reused(tmp_path):
    """已结束的边界不可复用，重复进入必须显式报错而不是静默另开事务。"""
    _database, repos = _repos(tmp_path)
    uow = repos.unit_of_work()
    with uow:
        repos.evidence.save(_evidence("E-FIN"))
    with pytest.raises(UnitOfWorkError):
        uow.commit()
    with pytest.raises(UnitOfWorkError):
        with uow:
            pass
    # 结束后的边界不残留登记，新边界照常工作
    assert active_transaction(str(repos.database.path.resolve())) is None
    with repos.unit_of_work():
        repos.evidence.save(_evidence("E-FIN2"))
    assert repos.evidence.get("E-FIN2") is not None


# ------------------------------------- 7. 业务门面写路径自动加入事务
def test_store_facade_writes_join_the_transaction(tmp_path):
    """EvidenceStore 原有方法（证据/记录/审计）在事务体内同样可回滚，store.py 无需改动。"""
    store = EvidenceStore(str(tmp_path / "store.sqlite3"))
    repos = store.repositories

    with pytest.raises(RuntimeError, match="facade-boom"):
        with repos.unit_of_work(label="facade"):
            store.save(_evidence("E-STORE"))
            store.put_record("J-TX", "probe", {"n": 1})
            store.add_audit("u-tx", "PROBE_WRITE", "evidence", "E-STORE", detail="probe")
            raise RuntimeError("facade-boom")

    assert store.get("E-STORE") is None
    assert store.get_records("J-TX", "probe") == []
    assert [a for a in store.list_audit() if a["action"] == "PROBE_WRITE"] == []

    with repos.unit_of_work(label="facade-ok"):
        store.save(_evidence("E-STORE"))
        store.put_record("J-TX", "probe", {"n": 1})
        store.add_audit("u-tx", "PROBE_WRITE", "evidence", "E-STORE", detail="probe")

    assert store.get("E-STORE") is not None
    assert store.get_records("J-TX", "probe") == [{"n": 1}]
    assert len([a for a in store.list_audit() if a["action"] == "PROBE_WRITE"]) == 1


# ------------------------------------- 8. 并发：事务串行化同键业务
def test_concurrent_transactions_allow_exactly_one_execution(tmp_path):
    """两线程各自开事务抢同一幂等键：只有一个 executed，另一个重放，证据只有一份。"""
    _database, repos = _repos(tmp_path)
    scope = _scope(repos)
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    outcomes: list[tuple[int, str]] = []

    def worker(idx: int) -> None:
        barrier.wait()
        with repos.unit_of_work(label=f"worker-{idx}"):
            state, _cached = repos.idem.claim(scope, "fp-conc")
            with lock:
                outcomes.append((idx, state))
            if state == "claimed":
                repos.evidence.save(_evidence("E-CONC"))
                repos.idem.finish(scope, {"by": idx})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    states = [state for _idx, state in outcomes]
    assert sorted(states) == ["claimed", "hit"], f"并发结果必须是一执一重放：{outcomes}"
    assert len(repos.evidence.list_by_job("J-TX")) == 1, "同一幂等键只能落一份证据"
    row = _idem_row(repos, scope)
    assert row is not None and row[1] == "DONE"
    winner = [idx for idx, state in outcomes if state == "claimed"][0]
    assert f'"by": {winner}' in (row[2] or "")
