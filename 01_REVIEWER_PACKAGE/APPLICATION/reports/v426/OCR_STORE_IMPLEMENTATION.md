# Store implementation decision
Accepted OCR_ACL_API_CONTRACT.md exactly. New tests should use explicit /submit; legacy /reviews IN_REVIEW is edit (not submit).
All existing ocr_document/ocr_page/ocr_review_event remain schema and bytes unchanged. New metadata tables in same governance.sqlite3 transaction: ocr_asset_anchor, ocr_chain_head, ocr_event_binding, ocr_security_incident, ocr_content, ocr_content_version, ocr_source_record.
Legacy documents with zero events can be adopted only by explicit ingest() with original payload and original/page SHA verified. Existing unanchored nonempty histories fail closed OCR_LEGACY_BASELINE_REQUIRED; never auto-reanchored.
Full validation detects tail deletion using independent chain head; immutable bindings anchor content version/submission/event/page/source relationships. SQL trigger boundary limited to normal application connections, not DB-file-owner attackers.
Need migration PoC optional V426 schema extension; please root own governance/migration.py + sql/006 if able, or I will handle after store. Source snapshots currently captured, implementation starts.
