-- R002: 回滚 V002（SQLite）
-- 子表先删：job_task / job_document 依赖 job，最后删 idem。
-- 全部 IF EXISTS，重复回滚安全。

DROP TABLE IF EXISTS job_task;
DROP TABLE IF EXISTS job_document;
DROP TABLE IF EXISTS job;
DROP TABLE IF EXISTS idem;
