-- 002: V4.2 新增（幂等指纹、JobTask、审批、门禁、业务记录）
-- 说明：本文件为 PostgreSQL 迁移草案，本机无 PostgreSQL，未实测。
BEGIN;

CREATE TABLE IF NOT EXISTS idem (
  scope_key   TEXT PRIMARY KEY,
  fingerprint TEXT NOT NULL,
  response    JSONB NOT NULL,
  tenant      TEXT,
  actor       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_idem_tenant ON idem(tenant, actor);

CREATE TABLE IF NOT EXISTS job (
  job_id       TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL,
  name         TEXT NOT NULL,
  business_line TEXT NOT NULL CHECK (business_line IN ('L1','L2','L3','L4','L5','L6')),
  basis_pack   JSONB NOT NULL,
  status       TEXT NOT NULL,
  created_by   TEXT NOT NULL,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS job_document (
  id BIGSERIAL PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES job(job_id) ON DELETE CASCADE,
  doc_key TEXT NOT NULL,
  version TEXT NOT NULL,
  file_sha256 CHAR(64) NOT NULL,
  verified BOOLEAN NOT NULL DEFAULT false,
  added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (job_id, doc_key, version)
);

-- [V4.2.1] job_task 已在 001 定义，此处移除重复定义

-- [V4.2.1] 移除悬空索引（job_task(job_id, node, status)）

CREATE TABLE IF NOT EXISTS job_record (
  id BIGSERIAL PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES job(job_id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  payload JSONB NOT NULL,
  at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_record_job_kind ON job_record(job_id, kind);

-- [V4.2.1] gate_record 已在 001 定义，此处移除重复定义

-- [V4.2.1] 移除悬空索引（gate_record(job_id, gate_code, judged_at)）

ALTER TABLE calc_run ADD COLUMN IF NOT EXISTS job_id TEXT;
-- [V4.2.1] 移除悬空索引（calc_run(snapshot_sha256)）

ALTER TABLE calc_run ADD COLUMN IF NOT EXISTS snapshot_sha256 TEXT;
COMMIT;
