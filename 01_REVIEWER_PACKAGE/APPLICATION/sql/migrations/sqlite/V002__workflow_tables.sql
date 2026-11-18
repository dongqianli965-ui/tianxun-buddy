-- V002: 幂等 / Job / 文档 / 任务（SQLite）
-- 权威参照：backend/txbuddy/repository/schema.py 的 SCHEMA_SQL。
-- idem 为 V4.2.3 数据库级原子认领模型：state ∈ {IN_PROGRESS, DONE}，DONE 才有 response。
-- job 含 P1-01 的增量列（basis_pack_id / updated_by / updated_at / revision / status_history），
-- 新建库直接是全列；旧库走 SqliteDatabase.ensure_job_columns 补列（不改列序）。

CREATE TABLE IF NOT EXISTS idem(
    scope_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'DONE', response TEXT,
    tenant TEXT, actor TEXT, claimed_at TEXT, at TEXT);

CREATE TABLE IF NOT EXISTS job(
    job_id TEXT PRIMARY KEY, project_id TEXT, name TEXT, business_line TEXT,
    basis_pack TEXT, status TEXT, created_by TEXT, created_at TEXT,
    basis_pack_id TEXT, updated_by TEXT, updated_at TEXT,
    revision INTEGER DEFAULT 1, status_history TEXT DEFAULT '[]');

CREATE TABLE IF NOT EXISTS job_document(
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, doc_key TEXT, version TEXT,
    file_sha256 TEXT, verified INTEGER DEFAULT 0, added_at TEXT);

CREATE TABLE IF NOT EXISTS job_task(
    task_id TEXT PRIMARY KEY, job_id TEXT, node TEXT, status TEXT, payload TEXT,
    result TEXT, assignee TEXT, approver TEXT, approved_at TEXT, history TEXT,
    created_at TEXT, updated_at TEXT);
