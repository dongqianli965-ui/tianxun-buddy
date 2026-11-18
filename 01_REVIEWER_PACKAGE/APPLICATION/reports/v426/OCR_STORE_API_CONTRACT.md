# OCR store / API contract proposal
ReviewStore.save keeps legacy review_status values (SQLite CHECK unchanged) and gains optional action, expected_content_sha256, submitted_version.
Actions: edit (IN_REVIEW or NEEDS_EXPERT_REVIEW), submit (IN_REVIEW + binding action SUBMIT; page workflow_state SUBMITTED), approve (REVIEWED), return (RETURNED), signoff (PROFESSIONALLY_SIGNED).
UI/API sees review_status IN_REVIEW but workflow_state SUBMITTED and submitted_version/submitted_content_sha256.
Approvals/returns require explicit exact submitted_version + expected_content_sha256; current page must be SUBMITTED, no content change; returns reason required.
Content SHA hashes corrected_text + structured_fields, excludes internal professional metadata. event version increments independently; content_version increments only on content change.
Legacy direct REVIEWED call should be blocked (submission required). All new audit fields live in separate tables so old events remain byte-preserved.
Please append decision here if collaboration messages are blank.
