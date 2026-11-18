-- V003: 通用业务记录 / 门禁记录（SQLite）
-- 权威参照：backend/txbuddy/repository/schema.py 的 SCHEMA_SQL。
-- job_record 是 store.put_record 的落库表；gate_record 是每次门禁判定的只追加日志。

CREATE TABLE IF NOT EXISTS job_record(
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, kind TEXT, payload TEXT, at TEXT);

CREATE TABLE IF NOT EXISTS gate_record(
    id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, gate_code TEXT, result TEXT,
    failed_rules TEXT, judged_by TEXT, judged_at TEXT);
