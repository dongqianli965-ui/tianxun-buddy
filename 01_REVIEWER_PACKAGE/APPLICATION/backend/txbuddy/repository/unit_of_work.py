"""SQLite 事务边界（Unit of Work）—— V4.2.3 / P1-05-TX-RECOVERY。

设计要点
--------
1. **连接随事务走**：``SqliteDatabase.connect_scoped()`` 在事务体内返回同一个连接代理，
   仓储方法里的 ``conn.close()`` 变成空操作，因此现有仓储实现一行不改就能参与事务。
2. **作用域隔离**：活动事务登记在 :class:`contextvars.ContextVar` 里，键是库文件绝对路径，
   并带线程号校验 —— 另一线程 / 另一进程永远拿不到本线程未提交的连接。
3. **可嵌套**：最外层 ``BEGIN IMMEDIATE``，内层 ``SAVEPOINT``；内层失败只回滚自己的写入，
   内层成功 ``RELEASE``。最外层要么 ``COMMIT`` 要么 ``ROLLBACK``。
4. **不静默降级**：提交失败一定尝试回滚并把异常抛给调用方；重复进入 / 已结束再用直接报错。

典型用法::

    from txbuddy.repository import build_sqlite_repositories

    database, repos = build_sqlite_repositories("data/dev.sqlite3")
    with repos.unit_of_work():
        repos.idem.claim(scope, fingerprint, tenant, actor)
        repos.evidence.save(evidence)
        repos.calc_run.save(run)
        repos.idem.finish(scope, response)
    # 以上四步要么全部生效，要么全部消失
"""
from __future__ import annotations

import contextvars
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from .base import UnitOfWork, UnitOfWorkError

__all__ = [
    "SqliteUnitOfWork",
    "TransactionConnection",
    "TransactionState",
    "UnitOfWorkError",
    "active_transaction",
]


class TransactionConnection:
    """事务内共享连接的代理：``close()`` 不真正关闭，其余属性/方法直通底层连接。"""

    __slots__ = ("_connection",)

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    @property
    def raw(self) -> Any:
        """底层 ``sqlite3.Connection``（测试与诊断用）。"""
        return self._connection

    def close(self) -> None:
        """事务所有者负责关闭；仓储里的 ``finally: conn.close()`` 因此变成空操作。"""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def __enter__(self) -> "TransactionConnection":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


@dataclass
class TransactionState:
    """一个已开启事务的运行时状态（一条连接 + 保存点栈）。"""

    connection: Any
    proxy: TransactionConnection
    thread_id: int
    label: str = ""
    depth: int = 0
    savepoints: list[str] = field(default_factory=list)
    rollback_only: bool = False
    writes: int = 0


_ACTIVE: contextvars.ContextVar[dict[str, TransactionState] | None] = contextvars.ContextVar(
    "txbuddy_active_transactions", default=None)


def _registry() -> dict[str, TransactionState]:
    """当前上下文的登记表（contextvar 默认值不可变，首次使用时懒建）。"""
    registry = _ACTIVE.get()
    if registry is None:
        registry = {}
        _ACTIVE.set(registry)
    return registry


def active_transaction(key: str) -> TransactionState | None:
    """取本线程当前上下文中该库的活动事务；跨线程一律返回 None（不共享连接）。"""
    registry = _ACTIVE.get()
    if not registry:
        return None
    state = registry.get(key)
    if state is None:
        return None
    if state.thread_id != threading.get_ident():
        return None
    return state


class SqliteUnitOfWork(UnitOfWork):
    """SQLite 事务边界：``BEGIN IMMEDIATE`` + 保存点嵌套 + 异常整段回滚。"""

    backend = "sqlite"

    def __init__(self, database: Any, *, immediate: bool = True, label: str = "") -> None:
        self.database = database
        self.key = str(database.path.resolve())
        self.immediate = immediate
        self.label = label or "unit-of-work"
        self._state: TransactionState | None = None
        self._nested = False
        self._savepoint = ""
        self._finished = False

    # ------------------------------------------------------------ 只读属性
    @property
    def connection(self) -> Any:
        """当前事务连接（未进入或已结束时报错，不返回 None）。"""
        state = self._live_state()
        return state.proxy

    @property
    def nested(self) -> bool:
        return self._nested

    @property
    def finished(self) -> bool:
        return self._finished

    def _live_state(self) -> TransactionState:
        if self._state is None:
            raise UnitOfWorkError("unit of work 尚未进入或已结束")
        if self._finished:
            raise UnitOfWorkError("unit of work 已结束，不可重复使用")
        return self._state

    # ------------------------------------------------------------ 生命周期
    def __enter__(self) -> "SqliteUnitOfWork":
        if self._finished:
            raise UnitOfWorkError("unit of work 不可重复进入")
        if self._state is not None:
            raise UnitOfWorkError("同一 unit of work 实例不可重入（请新建嵌套边界）")
        outer = active_transaction(self.key)
        if outer is not None:
            outer.depth += 1
            savepoint = f"uow_{uuid.uuid4().hex[:10]}"
            outer.connection.execute(f"SAVEPOINT {savepoint}")
            outer.savepoints.append(savepoint)
            self._nested = True
            self._savepoint = savepoint
            self._state = outer
            return self
        connection = self.database.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if self.immediate else "BEGIN")
        except Exception:
            connection.close()
            raise
        state = TransactionState(
            connection=connection,
            proxy=TransactionConnection(connection),
            thread_id=threading.get_ident(),
            label=self.label,
        )
        _registry()[self.key] = state
        self._state = state
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._finished or self._state is None:
            return False
        should_rollback = exc_type is not None or self._state.rollback_only
        try:
            if should_rollback:
                self.rollback()
            else:
                self.commit()
        except Exception:  # noqa: BLE001 - 提交失败必须让调用方看到
            if exc_type is not None:
                return False  # 已有异常在飞时不要遮蔽原始失败
            raise
        return False

    def commit(self) -> None:
        state = self._live_state()
        if self._nested:
            state.connection.execute(f"RELEASE SAVEPOINT {self._savepoint}")
            self._drop_savepoint(state)
            self._finished = True
            return
        try:
            state.connection.execute("COMMIT")
        except Exception:
            self._close(state, rollback=True)
            self._finished = True
            raise
        self._close(state, rollback=False)
        self._finished = True

    def rollback(self) -> None:
        state = self._live_state()
        if self._nested:
            state.connection.execute(f"ROLLBACK TO SAVEPOINT {self._savepoint}")
            state.connection.execute(f"RELEASE SAVEPOINT {self._savepoint}")
            self._drop_savepoint(state)
            self._finished = True
            return
        self._close(state, rollback=True)
        self._finished = True

    def set_rollback_only(self) -> None:
        self._live_state().rollback_only = True

    # ------------------------------------------------------------ 内部收尾
    def _drop_savepoint(self, state: TransactionState) -> None:
        if self._savepoint in state.savepoints:
            state.savepoints.remove(self._savepoint)
        state.depth = max(state.depth - 1, 0)

    def _close(self, state: TransactionState, *, rollback: bool) -> None:
        """注销登记、回滚（可选）并关闭连接；任何一步失败都不吞掉。"""
        registry = _ACTIVE.get()
        if registry is not None and registry.get(self.key) is state:
            registry.pop(self.key, None)
        error: Exception | None = None
        try:
            if rollback:
                state.connection.execute("ROLLBACK")
        except Exception as exc:  # noqa: BLE001 - 保留首个错误，继续关连接
            error = exc
        finally:
            try:
                state.connection.close()
            except Exception:  # noqa: BLE001 - 关连接失败不覆盖回滚错误
                pass
        if error is not None:
            raise error
