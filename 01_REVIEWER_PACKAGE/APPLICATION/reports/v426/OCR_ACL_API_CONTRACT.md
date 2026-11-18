# Accepted ACL/API contract
Collaboration messages arrive blank; using files for coordination. BASELINE_CAPTURED exists; source edits can begin based on isolated snapshot.
Accept lower-case actions edit, submit, approve, return, signoff in ReviewStore.save.
API legacy /reviews IN_REVIEW or NEEDS_EXPERT_REVIEW -> action edit, WRITE only. Explicit /submit -> action submit with unchanged current content, WRITE only. /decisions -> approve/return, REVIEW only.
DecisionInput requires expected_version, submitted_version, expected_content_sha256, decision APPROVE/RETURN, reason, human_confirmed.
Legacy /reviews REVIEWED/RETURNED requires submitted_version + expected_content_sha256 and exact content comparison; role REVIEW, store atomic compare. Existing legacy REVIEWED tests need explicit submit before decision; no weakening.
/signoff REVIEW + verified_professional role+server registry, action signoff, expected_content_sha256 mandatory, store validates already approved unchanged content. Body no longer mutates itself.
API public_page will add permissions object can_edit/can_review/can_signoff for minimal UI disable; no acl.py/runtime.py changes anticipated.
