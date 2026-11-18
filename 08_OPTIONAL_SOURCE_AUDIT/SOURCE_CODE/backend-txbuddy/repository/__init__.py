"""Repository 层（V4.2.3 / P1-02）：抽象接口 + SQLite 实现。

用法：
    from txbuddy.repository import build_sqlite_repositories
    database, repos = build_sqlite_repositories("data/txbuddy-dev.sqlite3")
    repos.evidence.save({"evidence_id": ..., "sha256": ..., "created_at": ...})

业务层（``txbuddy.store.EvidenceStore``）持有同一组仓储，只做领域校验后委托。

事务边界（P1-05）：::

    database, repos = build_sqlite_repositories("data/txbuddy-dev.sqlite3")
    with repos.unit_of_work():
        repos.evidence.save(evidence)
        repos.calc_run.save(run)
        repos.idem.finish(scope_key, response)   # 三步同事务，异常整段回滚
"""
from __future__ import annotations

from .base import (
    AuditRepository,
    CalcRunRepository,
    DocumentRepository,
    EvidenceRepository,
    GateRepository,
    IdemRepository,
    JobRepository,
    RecordRepository,
    Repository,
    RepositorySet,
    TaskRepository,
    UnitOfWork,
    UnitOfWorkAbort,
    UnitOfWorkError,
)
from .migrations import (
    SCHEMA_VERSION_TABLE,
    Migration,
    MigrationChecksumError,
    MigrationError,
    MigrationNotFoundError,
    MigrationStateError,
    PostgresMigrationRunner,
    SqliteMigrationRunner,
    discover_postgres_migrations,
    discover_sqlite_migrations,
    split_sql_statements,
)
from .schema import JOB_EXTRA_COLUMNS, SqliteDatabase, now_iso
from .sqlite import (
    SqliteAuditRepository,
    SqliteCalcRunRepository,
    SqliteDocumentRepository,
    SqliteEvidenceRepository,
    SqliteGateRepository,
    SqliteIdemRepository,
    SqliteJobRepository,
    SqliteRecordRepository,
    SqliteRepository,
    SqliteRepositorySet,
    SqliteTaskRepository,
    build_sqlite_repositories,
)
from .unit_of_work import (
    SqliteUnitOfWork,
    TransactionConnection,
    TransactionState,
    active_transaction,
)

__all__ = [
    # 抽象接口
    "Repository",
    "RepositorySet",
    "UnitOfWork",
    "UnitOfWorkError",
    "UnitOfWorkAbort",
    "EvidenceRepository",
    "CalcRunRepository",
    "AuditRepository",
    "IdemRepository",
    "JobRepository",
    "DocumentRepository",
    "TaskRepository",
    "GateRepository",
    "RecordRepository",
    # SQLite 实现
    "SqliteDatabase",
    "SqliteRepository",
    "SqliteRepositorySet",
    "SqliteEvidenceRepository",
    "SqliteCalcRunRepository",
    "SqliteAuditRepository",
    "SqliteIdemRepository",
    "SqliteJobRepository",
    "SqliteDocumentRepository",
    "SqliteTaskRepository",
    "SqliteGateRepository",
    "SqliteRecordRepository",
    "build_sqlite_repositories",
    # 事务边界（P1-05）
    "SqliteUnitOfWork",
    "TransactionConnection",
    "TransactionState",
    "active_transaction",
    "JOB_EXTRA_COLUMNS",
    "now_iso",
    # 版本化迁移与回滚（P1-04）
    "SCHEMA_VERSION_TABLE",
    "Migration",
    "MigrationError",
    "MigrationChecksumError",
    "MigrationNotFoundError",
    "MigrationStateError",
    "SqliteMigrationRunner",
    "PostgresMigrationRunner",
    "discover_sqlite_migrations",
    "discover_postgres_migrations",
    "split_sql_statements",
]
