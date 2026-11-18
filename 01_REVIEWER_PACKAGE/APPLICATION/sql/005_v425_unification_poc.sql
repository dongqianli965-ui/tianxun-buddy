-- V4.2.5 isolated migration PoC only. Never applied by the live application.
-- 003 is loaded into runtime; 004 is loaded into corpus by the PoC runner.
CREATE SCHEMA security;
CREATE SCHEMA storage;
CREATE SCHEMA review;
CREATE SCHEMA migration_control;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE TABLE migration_control.version(
    version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE migration_control.batch(
    batch_sha256 TEXT PRIMARY KEY, table_count INTEGER NOT NULL, row_count BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE security.user_account(
    username TEXT PRIMARY KEY, password_hash TEXT NOT NULL, roles JSONB NOT NULL,
    display_name TEXT NOT NULL, active BOOLEAN NOT NULL);
-- acl_project is an owner-claim registry, not a complete project directory:
-- PersistentAcl.grant() may create members without creating an owner claim.
CREATE TABLE security.project_scope(project_id TEXT PRIMARY KEY, discovery_sources JSONB NOT NULL);
CREATE TABLE security.acl_project(project_id TEXT PRIMARY KEY REFERENCES security.project_scope(project_id), owner TEXT NOT NULL);
CREATE TABLE security.acl_member(
    project_id TEXT NOT NULL REFERENCES security.project_scope(project_id) DEFERRABLE INITIALLY DEFERRED,
    subject TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('OWNER','EDITOR','REVIEWER','VIEWER')),
    granted_by TEXT, granted_at DOUBLE PRECISION, updated_at DOUBLE PRECISION,
    PRIMARY KEY(project_id,subject));
CREATE TABLE security.auth_revoked(jti TEXT PRIMARY KEY, expires_at BIGINT NOT NULL);
CREATE TABLE storage.file_blob(
    sha256 TEXT PRIMARY KEY CHECK(sha256 ~ '^[0-9a-f]{64}$'), size BIGINT NOT NULL CHECK(size>=0),
    storage_path TEXT NOT NULL UNIQUE, created_at TIMESTAMPTZ NOT NULL);
CREATE TABLE storage.file_version(
    doc_key TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0),
    sha256 TEXT NOT NULL REFERENCES storage.file_blob(sha256) DEFERRABLE INITIALLY DEFERRED,
    size BIGINT NOT NULL CHECK(size>=0), project_id TEXT REFERENCES security.project_scope(project_id)
      DEFERRABLE INITIALLY DEFERRED,
    filename TEXT, content_type TEXT, actor TEXT, created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(doc_key,version));
CREATE INDEX file_version_project ON storage.file_version(project_id);
CREATE TABLE runtime.schema_version(
    version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, checksum TEXT NOT NULL,
    rollback_checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL, execution_ms INTEGER NOT NULL);
ALTER TABLE runtime.job ADD CONSTRAINT job_project_fk FOREIGN KEY(project_id)
    REFERENCES security.project_scope(project_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE runtime.job_document ADD CONSTRAINT job_document_job_fk FOREIGN KEY(job_id)
    REFERENCES runtime.job(job_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE runtime.job_document ADD CONSTRAINT job_document_blob_fk FOREIGN KEY(file_sha256)
    REFERENCES storage.file_blob(sha256) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE runtime.job_document ADD CONSTRAINT job_document_version_unique UNIQUE(job_id,doc_key,version);
ALTER TABLE runtime.job_task ADD CONSTRAINT job_task_job_fk FOREIGN KEY(job_id)
    REFERENCES runtime.job(job_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE runtime.job_record ADD CONSTRAINT job_record_job_fk FOREIGN KEY(job_id)
    REFERENCES runtime.job(job_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE runtime.gate_record ADD CONSTRAINT gate_record_job_fk FOREIGN KEY(job_id)
    REFERENCES runtime.job(job_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE runtime.calc_run ADD CONSTRAINT calc_run_job_fk FOREIGN KEY(job_id)
    REFERENCES runtime.job(job_id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE runtime.calc_run ADD CONSTRAINT calc_run_evidence_fk FOREIGN KEY(evidence_id)
    REFERENCES runtime.evidence(evidence_id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE review.ocr_document(
    document_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES security.project_scope(project_id)
      DEFERRABLE INITIALLY DEFERRED,
    source_path TEXT NOT NULL, source_file_sha256 TEXT NOT NULL, "OCR_engine" TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL, priority INTEGER NOT NULL, created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(project_id,source_path,source_file_sha256,payload_sha256));
CREATE TABLE review.ocr_page(
    page_id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES review.ocr_document(document_id),
    page_number INTEGER NOT NULL CHECK(page_number>0), image_sha256 TEXT NOT NULL,
    image_path TEXT NOT NULL, image_width INTEGER NOT NULL CHECK(image_width>0),
    image_height INTEGER NOT NULL CHECK(image_height>0), original_text TEXT NOT NULL,
    confidence DOUBLE PRECISION CHECK(confidence>=0 AND confidence<=1), tokens_json JSONB NOT NULL,
    UNIQUE(document_id,page_number));
CREATE TABLE review.ocr_review_event(
    event_id TEXT PRIMARY KEY, page_id TEXT NOT NULL REFERENCES review.ocr_page(page_id),
    version INTEGER NOT NULL CHECK(version>0), corrected_text TEXT NOT NULL,
    structured_fields_json JSONB NOT NULL, reviewer_id TEXT NOT NULL, reviewed_at TIMESTAMPTZ NOT NULL,
    review_status TEXT NOT NULL CHECK(review_status IN
       ('NEEDS_EXPERT_REVIEW','IN_REVIEW','REVIEWED','RETURNED','PROFESSIONALLY_SIGNED')),
    reason TEXT NOT NULL, human_confirmed INTEGER NOT NULL CHECK(human_confirmed IN (0,1)),
    previous_event_sha256 TEXT NOT NULL, event_sha256 TEXT NOT NULL, UNIQUE(page_id,version));
CREATE FUNCTION review.prevent_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'OCR_APPEND_ONLY'; END; $$;
CREATE TRIGGER immutable_ocr_document BEFORE UPDATE OR DELETE ON review.ocr_document
    FOR EACH ROW EXECUTE FUNCTION review.prevent_change();
CREATE TRIGGER immutable_ocr_page BEFORE UPDATE OR DELETE ON review.ocr_page
    FOR EACH ROW EXECUTE FUNCTION review.prevent_change();
CREATE TRIGGER immutable_ocr_review_event BEFORE UPDATE OR DELETE ON review.ocr_review_event
    FOR EACH ROW EXECUTE FUNCTION review.prevent_change();
CREATE TABLE review.governance_catalog_snapshot(
    snapshot_sha256 TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL, payload_json JSONB NOT NULL);
CREATE TABLE review.governance_catalog_current(
    kind TEXT PRIMARY KEY, snapshot_sha256 TEXT NOT NULL REFERENCES review.governance_catalog_snapshot(snapshot_sha256));
CREATE TRIGGER immutable_governance_catalog_snapshot BEFORE UPDATE OR DELETE ON review.governance_catalog_snapshot
    FOR EACH ROW EXECUTE FUNCTION review.prevent_change();
-- Full JSON snapshots span projects. They are management artifacts, not API
-- query tables. With no public RLS policy, accidental broad SELECT grants to a
-- non-owner still cannot reveal a complete snapshot.
ALTER TABLE review.governance_catalog_snapshot ENABLE ROW LEVEL SECURITY;
ALTER TABLE review.governance_catalog_current ENABLE ROW LEVEL SECURITY;

-- app.subject is set only by a trusted JWT-authenticated service transaction.
-- These functions are not a credential or a substitute for API authorization.
CREATE FUNCTION security.can_project(p TEXT,w BOOLEAN DEFAULT false) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,security AS $$
 SELECT EXISTS(SELECT 1 FROM security.acl_member m
   WHERE m.project_id=p AND m.subject=nullif(current_setting('app.subject',true),'')
     AND (NOT w OR m.role IN ('OWNER','EDITOR'))); $$;
CREATE FUNCTION security.can_job(j TEXT,w BOOLEAN DEFAULT false) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,security,runtime AS $$
 SELECT EXISTS(SELECT 1 FROM runtime.job x WHERE x.job_id=j AND security.can_project(x.project_id,w)); $$;
CREATE FUNCTION security.can_ocr(d TEXT,w BOOLEAN DEFAULT false) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,security,review AS $$
 SELECT EXISTS(SELECT 1 FROM review.ocr_document x WHERE x.document_id=d AND security.can_project(x.project_id,w)); $$;
CREATE FUNCTION security.can_page(p TEXT,w BOOLEAN DEFAULT false) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,security,review AS $$
 SELECT EXISTS(SELECT 1 FROM review.ocr_page x WHERE x.page_id=p AND security.can_ocr(x.document_id,w)); $$;

ALTER TABLE runtime.job ENABLE ROW LEVEL SECURITY;
CREATE POLICY job_acl ON runtime.job USING(security.can_project(project_id))
    WITH CHECK(security.can_project(project_id,true));
ALTER TABLE runtime.job_document ENABLE ROW LEVEL SECURITY;
CREATE POLICY document_acl ON runtime.job_document USING(security.can_job(job_id))
    WITH CHECK(security.can_job(job_id,true));
ALTER TABLE runtime.job_task ENABLE ROW LEVEL SECURITY;
CREATE POLICY task_acl ON runtime.job_task USING(security.can_job(job_id))
    WITH CHECK(security.can_job(job_id,true));
ALTER TABLE runtime.job_record ENABLE ROW LEVEL SECURITY;
CREATE POLICY record_acl ON runtime.job_record USING(security.can_job(job_id))
    WITH CHECK(security.can_job(job_id,true));
ALTER TABLE runtime.gate_record ENABLE ROW LEVEL SECURITY;
CREATE POLICY gate_acl ON runtime.gate_record USING(security.can_job(job_id))
    WITH CHECK(security.can_job(job_id,true));
ALTER TABLE runtime.calc_run ENABLE ROW LEVEL SECURITY;
CREATE POLICY calc_acl ON runtime.calc_run USING(security.can_job(job_id))
    WITH CHECK(security.can_job(job_id,true));
ALTER TABLE runtime.evidence ENABLE ROW LEVEL SECURITY;
CREATE POLICY evidence_acl ON runtime.evidence USING(security.can_job(payload->>'job_id'))
    WITH CHECK(security.can_job(payload->>'job_id',true));
ALTER TABLE storage.file_version ENABLE ROW LEVEL SECURITY;
CREATE POLICY file_acl ON storage.file_version USING(security.can_project(project_id))
    WITH CHECK(security.can_project(project_id,true));
ALTER TABLE review.ocr_document ENABLE ROW LEVEL SECURITY;
CREATE POLICY ocr_document_acl ON review.ocr_document USING(security.can_project(project_id))
    WITH CHECK(security.can_project(project_id,true));
ALTER TABLE review.ocr_page ENABLE ROW LEVEL SECURITY;
CREATE POLICY ocr_page_acl ON review.ocr_page USING(security.can_ocr(document_id))
    WITH CHECK(security.can_ocr(document_id,true));
ALTER TABLE review.ocr_review_event ENABLE ROW LEVEL SECURITY;
CREATE POLICY ocr_event_acl ON review.ocr_review_event USING(security.can_page(page_id))
    WITH CHECK(security.can_page(page_id,true));

-- No PUBLIC grants to auth credentials, ACL administration, raw blobs or audit.
REVOKE ALL ON ALL TABLES IN SCHEMA security,storage,review,runtime,corpus,migration_control FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA runtime FROM PUBLIC;
