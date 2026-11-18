-- Optional isolated V426 PoC mapping. Never a production database migration.
CREATE TABLE review.ocr_content(content_id TEXT PRIMARY KEY,source_file_sha256 TEXT NOT NULL UNIQUE,
 file_format TEXT NOT NULL,size_bytes BIGINT NOT NULL,created_at TIMESTAMPTZ NOT NULL);
CREATE TABLE review.ocr_content_version(content_id TEXT NOT NULL REFERENCES review.ocr_content(content_id),
 version INTEGER NOT NULL,ocr_sha256 TEXT NOT NULL,"OCR_engine" TEXT NOT NULL,page_count INTEGER NOT NULL,
 pages_json JSONB NOT NULL,created_at TIMESTAMPTZ NOT NULL,PRIMARY KEY(content_id,version),UNIQUE(content_id,ocr_sha256));
CREATE TABLE review.ocr_source_record(source_id TEXT PRIMARY KEY,document_id TEXT NOT NULL UNIQUE REFERENCES review.ocr_document(document_id),
 content_id TEXT NOT NULL,content_version INTEGER NOT NULL,source_path TEXT NOT NULL,original_absolute_path TEXT NOT NULL,
 official_url TEXT,source_name TEXT NOT NULL,acquired_at TEXT,authorization_status TEXT NOT NULL,source_version TEXT,
 source_note TEXT NOT NULL,source_record_json JSONB NOT NULL,created_at TIMESTAMPTZ NOT NULL,
 FOREIGN KEY(content_id,content_version) REFERENCES review.ocr_content_version(content_id,version));
CREATE TABLE review.ocr_asset_anchor(page_id TEXT PRIMARY KEY REFERENCES review.ocr_page(page_id),document_id TEXT NOT NULL,
 source_id TEXT NOT NULL REFERENCES review.ocr_source_record(source_id),anchor_json JSONB NOT NULL,anchor_sha256 TEXT NOT NULL,created_at TIMESTAMPTZ NOT NULL);
CREATE TABLE review.ocr_chain_head(page_id TEXT PRIMARY KEY REFERENCES review.ocr_page(page_id),version INTEGER NOT NULL,
 event_sha256 TEXT NOT NULL,binding_sha256 TEXT NOT NULL);
CREATE TABLE review.ocr_event_binding(event_id TEXT PRIMARY KEY REFERENCES review.ocr_review_event(event_id),page_id TEXT NOT NULL,
 event_version INTEGER NOT NULL,action TEXT NOT NULL,content_version INTEGER NOT NULL,content_sha256 TEXT NOT NULL,
 submitted_version INTEGER,submitted_content_sha256 TEXT,asset_anchor_sha256 TEXT NOT NULL,previous_binding_sha256 TEXT NOT NULL,
 binding_sha256 TEXT NOT NULL,UNIQUE(page_id,event_version));
CREATE TABLE review.ocr_security_incident(incident_id TEXT PRIMARY KEY,page_id TEXT NOT NULL,operation TEXT NOT NULL,code TEXT NOT NULL,
 details_json JSONB NOT NULL,created_at TIMESTAMPTZ NOT NULL);
DO $$ DECLARE t TEXT; BEGIN
 FOREACH t IN ARRAY ARRAY['ocr_content','ocr_content_version','ocr_source_record','ocr_asset_anchor','ocr_event_binding','ocr_security_incident'] LOOP
  EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON review.%I FOR EACH ROW EXECUTE FUNCTION review.prevent_change()','immutable_'||t,t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['ocr_content','ocr_content_version','ocr_source_record','ocr_asset_anchor','ocr_chain_head','ocr_event_binding','ocr_security_incident'] LOOP
  EXECUTE format('ALTER TABLE review.%I ENABLE ROW LEVEL SECURITY',t);
 END LOOP;
END $$;
-- No general application grant. These management tables are private to the service.

