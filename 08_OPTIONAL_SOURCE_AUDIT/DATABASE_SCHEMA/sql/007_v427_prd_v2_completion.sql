-- 007_v427_prd_v2_completion.sql
-- PRD V2.0 一期全量五条生产线与闭环管理新增核心数据表（投标4表 + 返工单表 + 双轨提取表 + 广联达任务书表 + 签证台账表 + 案卷目录表）

CREATE TABLE IF NOT EXISTS bid_project (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    tenderee TEXT NOT NULL,
    bid_deadline TEXT NOT NULL,
    bond_amount NUMERIC(18, 2) NOT NULL DEFAULT 0,
    control_price NUMERIC(18, 2) NOT NULL,
    eplatform TEXT DEFAULT 'EXCEL_PDF',
    must_use_glodon BOOLEAN NOT NULL DEFAULT FALSE,
    glodon_reason TEXT,
    status TEXT NOT NULL DEFAULT 'T0_INTAKE',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bid_redline (
    id TEXT PRIMARY KEY,
    bid_project_id TEXT NOT NULL REFERENCES bid_project(id),
    category_no INTEGER NOT NULL CHECK (category_no BETWEEN 1 AND 13),
    category_name TEXT NOT NULL,
    quote_text TEXT NOT NULL,
    page_no INTEGER NOT NULL,
    clause_no TEXT NOT NULL,
    system_interpretation TEXT NOT NULL,
    required_action TEXT NOT NULL,
    risk_level TEXT NOT NULL CHECK (risk_level IN ('FATAL', 'HIGH', 'MEDIUM')),
    closed BOOLEAN NOT NULL DEFAULT FALSE,
    source_ref TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bid_list_lock (
    id TEXT PRIMARY KEY,
    bid_project_id TEXT NOT NULL REFERENCES bid_project(id),
    item_code TEXT NOT NULL,
    field_name TEXT NOT NULL CHECK (field_name IN ('item_code', 'item_name', 'feature', 'unit', 'qty', 'missing_item', 'extra_item')),
    tender_value TEXT,
    bid_value TEXT,
    diff_type TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('MATCHED', 'BLOCKED', 'SIGNED_ALLOWED_EXTRA')),
    signed_by TEXT,
    checked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bid_quote (
    id TEXT PRIMARY KEY,
    bid_project_id TEXT NOT NULL REFERENCES bid_project(id),
    version TEXT NOT NULL,
    cost_floor NUMERIC(18, 2) NOT NULL,
    range_aggressive_low NUMERIC(18, 2) NOT NULL,
    range_aggressive_high NUMERIC(18, 2) NOT NULL,
    range_neutral_low NUMERIC(18, 2) NOT NULL,
    range_neutral_high NUMERIC(18, 2) NOT NULL,
    range_conservative_low NUMERIC(18, 2) NOT NULL,
    range_conservative_high NUMERIC(18, 2) NOT NULL,
    target_amount NUMERIC(18, 2),
    down_rate NUMERIC(8, 4),
    decided_by TEXT,
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rework_order (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('GATE', 'REVIEW')),
    business_type TEXT NOT NULL,
    owner TEXT NOT NULL,
    major TEXT NOT NULL,
    issue TEXT NOT NULL,
    location TEXT NOT NULL,
    clause TEXT NOT NULL,
    expected_action TEXT NOT NULL,
    deadline TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'RECTIFIED', 'CLOSED', 'REOPENED')),
    before_value TEXT,
    after_value TEXT,
    reviewer TEXT,
    reopen_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS dual_track_extract (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    track_mode TEXT NOT NULL CHECK (track_mode IN ('MANUAL', 'AI', 'FINAL')),
    extracted_fields_json TEXT NOT NULL,
    field_provenance_json TEXT,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, document_id, track_mode)
);

CREATE TABLE IF NOT EXISTS gld_taskbook (
    taskbook_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    taskbook_type TEXT NOT NULL CHECK (taskbook_type IN ('MODELING', 'QUOTA_PRICING', 'PRICE_ADJUST', 'BATCH_EDIT')),
    instructions_json TEXT NOT NULL,
    readback_json TEXT,
    max_deviation_ratio NUMERIC(8, 4),
    diff_reason TEXT,
    status TEXT NOT NULL DEFAULT 'GENERATED',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS visa_register (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    visa_no TEXT NOT NULL,
    visa_date TEXT NOT NULL,
    location TEXT NOT NULL,
    reason TEXT NOT NULL,
    qty NUMERIC(18, 4) NOT NULL,
    amount NUMERIC(18, 2) NOT NULL,
    seal_complete BOOLEAN NOT NULL DEFAULT TRUE,
    duplicate_flag BOOLEAN NOT NULL DEFAULT FALSE,
    overlap_amount NUMERIC(18, 2) DEFAULT 0,
    duplicate_evidence TEXT
);

CREATE TABLE IF NOT EXISTS archive_volume (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    business_type TEXT NOT NULL,
    volume_no TEXT NOT NULL,
    standard_ref TEXT NOT NULL,
    catalog_json TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    retention_years INTEGER NOT NULL CHECK (retention_years >= 5),
    status TEXT NOT NULL DEFAULT 'PACKAGED',
    created_at TEXT NOT NULL
);
