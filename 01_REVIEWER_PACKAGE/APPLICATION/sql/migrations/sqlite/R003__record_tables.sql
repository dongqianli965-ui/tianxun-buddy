-- R003: 回滚 V003（SQLite）
-- 先删门禁记录，再删通用业务记录；IF EXISTS 使重复回滚安全。

DROP TABLE IF EXISTS gate_record;
DROP TABLE IF EXISTS job_record;
