-- 003: V4.2.3 仓储运行时表（PostgreSQL）
-- 目的：给 backend/txbuddy/repository/pg.py 提供与 SQLite 同构的运行时表，使
--       PostgresRepository 与 SqliteRepository 在同一个 Repository 抽象接口下可互换。
--
-- 与 001/002 的分工（重要）：
--   * 001_core_schema_postgresql.sql 是**领域核心模型**（tenant/project/basis_pack/
--     boq_item/calc_run/evidence/... 全部 BIGINT 主键 + 外键 + NUMERIC 精度），由
--     pg.PgDatabase 应用到独立 schema（默认 txbuddy_core），是生产目标的权威 DDL；
--   * 002_workflow_and_idem_postgresql.sql 是 001 之上的**工作流扩展**（job/idem/
--     job_document/job_record + calc_run 增列），必须与 001 落在同一 schema；
--   * 本文件是**应用运行时表**，列名/语义与 repository/schema.py 的 SQLite schema 一一
--     对应（TEXT 主键 + JSONB 载荷 + TIMESTAMPTZ），供当前 API 层直接使用。
--
-- 取舍说明：
--   1. 不使用 BIGINT 代理键，因为仓储接口用业务键（evidence_id/job_id/task_id）寻址；
--   2. 运行时不建外键：SQLite 侧同样是"应用层校验、库层不强制"，保留孤儿引用是既有
--      行为（历史数据迁移期需要），设 FK 会让同一套用例在两种后端上行为不一致；
--   3. JSON 列一律 JSONB（比 SQLite 的 TEXT 更严格：非法 JSON 由数据库直接拒绝）；
--   4. 时间列一律 TIMESTAMPTZ，pg.py 读出后统一转成与 SQLite 完全相同的 ISO8601 字符串。
--
-- 幂等：全部 IF NOT EXISTS，重复执行安全；实际由 pg.PgDatabase 的迁移账本避免重跑。

CREATE TABLE IF NOT EXISTS evidence(
    evidence_id TEXT PRIMARY KEY,
    sha256      TEXT,
    payload     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS calc_run(
    calc_run_id     TEXT PRIMARY KEY,
    job_id          TEXT,
    rule_code       TEXT NOT NULL,
    rule_version    TEXT NOT NULL,
    basis_pack_id   TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    snapshot        JSONB NOT NULL,
    result          TEXT,
    unit            TEXT,
    evidence_id     TEXT,
    created_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_calc_run_job ON calc_run(job_id, created_at);

CREATE TABLE IF NOT EXISTS audit_log(
    id          BIGSERIAL PRIMARY KEY,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id   TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    trace_id    TEXT NOT NULL DEFAULT '',
    at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 幂等表：V4.2.3 的数据库级原子认领，唯一裁判是 scope_key 主键约束。
-- state 只允许 IN_PROGRESS / DONE；未完成的认领 response 必须为 NULL。
CREATE TABLE IF NOT EXISTS idem(
    scope_key   TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'DONE' CHECK (state IN ('IN_PROGRESS', 'DONE')),
    response    JSONB,
    tenant      TEXT,
    actor       TEXT,
    claimed_at  TIMESTAMPTZ,
    at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT idem_done_has_response CHECK (state <> 'DONE' OR response IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_idem_claim ON idem(state, claimed_at);

CREATE TABLE IF NOT EXISTS job(
    job_id         TEXT PRIMARY KEY,
    project_id     TEXT,
    name           TEXT,
    business_line  TEXT,
    basis_pack     JSONB,
    status         TEXT,
    created_by     TEXT,
    created_at     TIMESTAMPTZ,
    basis_pack_id  TEXT,
    updated_by     TEXT,
    updated_at     TIMESTAMPTZ,
    revision       INTEGER DEFAULT 1,
    status_history JSONB DEFAULT '[]'::jsonb
);

CREATE TABLE IF NOT EXISTS job_document(
    id          BIGSERIAL PRIMARY KEY,
    job_id      TEXT NOT NULL,
    doc_key     TEXT NOT NULL,
    version     TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    verified    BOOLEAN NOT NULL DEFAULT FALSE,
    added_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_document_job ON job_document(job_id, id);

CREATE TABLE IF NOT EXISTS job_task(
    task_id     TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL,
    node        TEXT,
    status      TEXT,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb,
    result      JSONB,
    assignee    TEXT,
    approver    TEXT,
    approved_at TIMESTAMPTZ,
    history     JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at  TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_task_job ON job_task(job_id, created_at);

CREATE TABLE IF NOT EXISTS job_record(
    id      BIGSERIAL PRIMARY KEY,
    job_id  TEXT NOT NULL,
    kind    TEXT NOT NULL,
    payload JSONB NOT NULL,
    at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_record_job_kind ON job_record(job_id, kind, id);

CREATE TABLE IF NOT EXISTS gate_record(
    id           BIGSERIAL PRIMARY KEY,
    job_id       TEXT NOT NULL,
    gate_code    TEXT NOT NULL,
    result       TEXT NOT NULL,
    failed_rules JSONB NOT NULL DEFAULT '[]'::jsonb,
    judged_by    TEXT,
    judged_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gate_job ON gate_record(job_id, id);
