"""Disposable browser fixture. Never invoked against the real local runtime."""
from pathlib import Path
import json,os,sys
from txbuddy.security.users import hash_password
from txbuddy.security.runtime import PersistentAcl
from txbuddy.governance.ocr_review import ReviewStore
from v425_ocr_setup import ingest_existing

ROOT=Path(__file__).resolve().parents[1]
runtime=Path(sys.argv[1]).resolve()
if 'TEST_ONLY-e2e-' not in runtime.name:
    raise SystemExit('Disposable browser fixture directory required')
Path(os.environ['TXBUDDY_USERS_FILE']).write_text(json.dumps({u:{'password_hash':hash_password('E2E-TEST-ONLY-password'),'roles':['estimator']} for u in ('alice','mallory','editor','reviewer','viewer')}),encoding='utf-8')
projects=json.loads((ROOT/'reports/v425/PROJECT_READINESS.json').read_text(encoding='utf-8'))
sources=json.loads((ROOT/'reports/v425/SOURCE_STATUS.json').read_text(encoding='utf-8'))
store=ReviewStore(runtime/'governance.sqlite3')
corpus=Path(os.environ.get('TXBUDDY_CORPUS_ROOT',str(ROOT.parent/'REAL_DATA_CORPUS/HN_L1_V1_20261116')))
# Explicitly verify original payload/source/page hashes before adopting drafts.
# The importer below is the single source of browser fixtures; copying rows from
# the seed database first would duplicate every document and invalidate the
# expected 40-document ACL fixture. It creates no review events.
ingest_existing(store,corpus,ROOT/'reports/v424/OCR_CORPUS_RESULTS.json',ROOT/'reports/v424/ocr',projects['projects'])
store.import_catalog('projects',projects);store.import_catalog('sources',sources)
acl=PersistentAcl(runtime/'auth.sqlite3')
for project in ['CORPUS_GOVERNANCE']+[p['project_id'] for p in projects['projects']]:
    acl.grant(project,'alice','OWNER',granted_by='TEST_ONLY_E2E_FIXTURE')
    for subject,role in (('editor','EDITOR'),('reviewer','REVIEWER'),('viewer','VIEWER')):
        acl.grant(project,subject,role,granted_by='TEST_ONLY_E2E_FIXTURE')
