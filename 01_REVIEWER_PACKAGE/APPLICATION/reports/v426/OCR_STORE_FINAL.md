# OCR store V426 integration status

- OCR audit-chain, immutable asset binding and fail-closed write verification implemented in backend/txbuddy/governance/ocr_review.py. Separate persisted security incidents record failed integrity checks. Database-owner-level attackers can bypass SQLite triggers: this is not absolute tamper resistance.
- Explicit import of unanchored existing pages without review history is supported after asset validation; nonempty unanchored legacy review histories fail closed and require independent migration validation.
- SHA256 content/version/source inventory added. Real isolated rebuild in OCR_CONTENT_DEDUP.json: 40 registrations, 33 distinct source hashes, 33 content records and versions, 40 independent source records, 7 reused OCR results; all checked source/cache/page assets remained unchanged, zero reviews created. Original production DB was not used or modified by the rebuild. Private DB: F:\ZHU-FISH\TIANXUN_BUDDY_V4\_CODEX_V426\ocr_dedup_private.sqlite3.
- Pre-fix evidence reports/v426/ocr_store_before.log (8 failing tests); combined current OCR store/ACL/V425 suite reports/v426/ocr_combined_final.log: 69 passed.
- Optional isolated PostgreSQL PoC extension sql/006_v426_ocr_audit_poc.sql and mapping in backend/txbuddy/governance/migration.py preserve original 005 checksum. These changes still require PostgreSQL migration tests using an isolated test DSN; please include in your full suite. No production migration performed.
- Reserved signoff fields are dropped from user-edit fields and injected only by server signoff.
- Source metadata preserved in private inventory; missing acquisition and authorization details remain unknown. Human review and qualified professional signoff are not fabricated.
- Existing offline OCR execution cache matches both SHA256 and requested_pages; this change deduplicates registered OCR results but does not guarantee one engine run across different requested page selections. Cache reuse itself does not revalidate image hashes; ReviewStore ingest and write paths independently validate original/page hashes. Treat universal engine-level dedup and cache validation as outstanding work.

## Final integration evidence

The subsequent full backend run includes `test_v425_migration.py` against a fresh isolated PostgreSQL database and passes all 1036 backend tests with zero skips. See `FINAL_RESULT.json` and `FINAL_BACKEND.xml` for the exact source manifest and database name. This supersedes the earlier request above to include migration tests; it does not certify production migration readiness or change the live adapter.
