-- V001: 证据 / 计算运行 / 审计日志（SQLite）
-- 权威参照：backend/txbuddy/repository/schema.py 的 SCHEMA_SQL（列名/类型/默认值必须一致）。
-- 幂等：CREATE TABLE IF NOT EXISTS —— 旧库已建表时只补记版本号（基线接管），不重建表。

CREATE TABLE IF NOT EXISTS evidence(
    evidence_id TEXT PRIMARY KEY, sha256 TEXT, payload TEXT, created_at TEXT);

CREATE TABLE IF NOT EXISTS calc_run(
    calc_run_id TEXT PRIMARY KEY, job_id TEXT, rule_code TEXT, rule_version TEXT, basis_pack_id TEXT,
    snapshot_sha256 TEXT, snapshot TEXT, result TEXT, unit TEXT, evidence_id TEXT, created_at TEXT);

CREATE TABLE IF NOT EXISTS audit_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, action TEXT, object_type TEXT,
    object_id TEXT, detail TEXT, trace_id TEXT, at TEXT);
