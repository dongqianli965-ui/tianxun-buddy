-- R001: 回滚 001_core_schema_postgresql.sql（领域核心模型，19 张表）
--
-- 与正向脚本的分工：
--   * 正向 DDL 只有一份，仍是 sql/001_core_schema_postgresql.sql（pg.py 与版本化 runner 共用）；
--   * 本文件是它的**版本化回滚脚本**，与 sql/migrations/sqlite/V001 同版本号。
--
-- 约束：
--   * 只删 001 建的表；不删扩展 pgcrypto（实例级共享），不删账本 schema_version；
--   * CASCADE 用来解除表间外键，避免依赖顺序错误；
--   * IF EXISTS 使回滚可重复执行（幂等）。

DROP TABLE IF EXISTS extracted_field CASCADE;
DROP TABLE IF EXISTS project_document CASCADE;
DROP TABLE IF EXISTS component CASCADE;
DROP TABLE IF EXISTS boq_item CASCADE;
DROP TABLE IF EXISTS calc_run CASCADE;
DROP TABLE IF EXISTS evidence CASCADE;
DROP TABLE IF EXISTS basis_source CASCADE;
DROP TABLE IF EXISTS quota_note CASCADE;
DROP TABLE IF EXISTS quota_item CASCADE;
DROP TABLE IF EXISTS calc_rule CASCADE;
DROP TABLE IF EXISTS material_price CASCADE;
DROP TABLE IF EXISTS fee_rate_matrix CASCADE;
DROP TABLE IF EXISTS project_job CASCADE;
DROP TABLE IF EXISTS job_task CASCADE;
DROP TABLE IF EXISTS gate_record CASCADE;
DROP TABLE IF EXISTS audit_log CASCADE;
DROP TABLE IF EXISTS basis_pack CASCADE;
DROP TABLE IF EXISTS project CASCADE;
DROP TABLE IF EXISTS tenant CASCADE;
