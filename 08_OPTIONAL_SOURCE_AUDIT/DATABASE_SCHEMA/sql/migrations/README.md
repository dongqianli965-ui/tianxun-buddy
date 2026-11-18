# sql/migrations —— 版本化迁移与回滚（P1-04-MIGRATION）

## 1 命名与配对

| 方言 | 正向脚本 | 回滚脚本 |
| --- | --- | --- |
| SQLite | `sqlite/V{版本}__{名称}.sql` | `sqlite/R{版本}__{名称}.sql` |
| PostgreSQL | `sql/{版本}_{名称}_postgresql.sql`（历史位置，唯一来源） | `postgresql/R{版本}__{名称}.sql` |

* 版本号三位补零、从 001 起、连续且唯一；同一版本的正向/回滚脚本文件名后半段必须一致。
* SQLite 每个版本是独立文件，空库执行 V001→V002→V003 即得完整 schema。
* PostgreSQL 的正向 DDL 保留在 `sql/` 根目录（`pg.py` 的迁移账本、`test_pg_integration.py`、
  `test_v421_hardening.py` 都直接读这几个路径），**不复制副本**，避免两份 DDL 漂移；
  `postgresql/R00x__*.sql` 是对应版本的回滚脚本，由 `PostgresMigrationRunner` 执行。
* 编号即版本：001=core schema，002=workflow/idem，003=repository runtime。

## 2 账本 `schema_version`

```sql
CREATE TABLE schema_version(
    version           INTEGER PRIMARY KEY,
    name              TEXT    NOT NULL UNIQUE,
    checksum          TEXT    NOT NULL,   -- 正向脚本规范化后的 sha256
    rollback_checksum TEXT    NOT NULL,   -- 回滚脚本规范化后的 sha256
    applied_at        TEXT/TIMESTAMPTZ NOT NULL,
    execution_ms      INTEGER NOT NULL
);
```

* `checksum` 的规范化 = 统一 UTF-8 + CRLF→LF，避免换行符差异造成假漂移。
* 已登记版本的正向或回滚脚本一旦被改动，升级/回滚都会抛 `MigrationChecksumError`：
  **历史脚本只读，变更必须新增版本**。
* 每个版本一个事务：DDL 与账本行同进同退；失败自动 `ROLLBACK`，不会留下半套表。
* SQLite 的 `schema_version` 与业务表同库；PostgreSQL 按 schema 各一张（core / runtime 分开记账）。

## 3 使用

```python
from txbuddy.repository.migrations import SqliteMigrationRunner

runner = SqliteMigrationRunner("data/txbuddy-dev.sqlite3")
runner.upgrade()            # 空库全量；已是最新则返回 []（幂等）
runner.current_version()    # 3
runner.downgrade(target=1)  # 只回滚 V003/V002 之外的部分，保留 V001
runner.downgrade()          # 全部回滚，只剩账本表（无业务表残留）
```

```python
from txbuddy.repository.migrations import PostgresMigrationRunner

runner = PostgresMigrationRunner(pg_database)   # pg_database 为 PgDatabase
runner.upgrade()            # 1/2 落 core schema，3 落 runtime schema
runner.downgrade()          # 逆序执行 R003→R002→R001
```

`SqliteDatabase.initialize()` 已改为执行本目录的版本化迁移，因此服务启动、旧库升级、
测试建库走的都是同一套脚本；`repository/schema.py` 里的 `SCHEMA_SQL` 保留为**对照 DDL**，
由 `tests/test_v423_migration.py` 校验迁移结果与它逐列等价。

## 4 纪律

* 不修改已发布的 V/R 脚本；需要变更就加 V004/R004。
* 回滚脚本必须能重复执行（`IF EXISTS` / `DROP TABLE IF EXISTS ... CASCADE`）。
* 回滚只删本版本建的对象，不删账本表、不删扩展、不删别人的 schema。
