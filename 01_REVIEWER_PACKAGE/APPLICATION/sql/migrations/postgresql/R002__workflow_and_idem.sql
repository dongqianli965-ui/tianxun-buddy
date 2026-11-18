-- R002: 回滚 002_workflow_and_idem_postgresql.sql
--
-- 002 只新建 4 张表（idem / job / job_document / job_record）并给 calc_run 增列；
-- 删表即同时撤销增列（calc_run 属 001，由 R001 负责整体删除）。
-- 本文件不删账本 schema_version 与扩展。

DROP TABLE IF EXISTS job_document CASCADE;
DROP TABLE IF EXISTS job_record CASCADE;
DROP TABLE IF EXISTS job CASCADE;
DROP TABLE IF EXISTS idem CASCADE;
