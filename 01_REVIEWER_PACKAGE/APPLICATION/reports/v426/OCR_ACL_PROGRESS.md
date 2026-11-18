# ACL progress for root
Communication tool messages are blank; this file is authoritative subagent handoff.
API and old OCR tests now pass: reports/v426/ACL_REGRESSION.log = 54 passed, 0 failed (tests/test_v426_ocr_acl.py + test_v425_ocr_review.py). Old direct approval test now explicitly submits, approves exact SHA, resubmits, returns; no reduction in assertions.
frontend build passed reports/v426/OCR_FRONTEND_BUILD.log. I am NOW running Playwright governance.spec.ts + ocr-roles.spec.ts on existing v425 config at ports8016/4186. Root do not run browser simultaneously.
Added scripts/v425_e2e_seed.py editor/reviewer/viewer TEST_ONLY users and project membership, no owner-claim changes. Store seed still copies old draft tables; browser source anchor may need explicit ingest if rejected.
New API model DecisionInput requires submitted_version; legacy ReviewInput permits optional hash+submission binding but decisions require both.

Continuation check 2026-11-16: current source OCR API/store tests rerun with backend/.venv/Scripts/python.exe: 69 passed, 1 warning in 43.22 seconds (test_v426_ocr_acl.py, test_v425_ocr_review.py, test_v426_ocr_store.py). Old test_status_human_review_and_return_reason no longer fails. No source or test edits performed in this continuation. Existing venv lacked Pillow; installed pillow 13.0.0 there. Root message body appears empty; awaiting concrete follow-up in shared file if needed.
