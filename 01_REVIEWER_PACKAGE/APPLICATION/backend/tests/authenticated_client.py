"""Migrate pre-V424 business tests to genuine signed JWTs; no production auth bypass.

Only the main business app receives credentials. Security tests use the original
TestClient and real login. Existing X-Actor fixtures become authenticated test
subjects so the original idempotency assertions keep their meaning.
"""
import os
import json
from fastapi.testclient import TestClient as RawTestClient
from txbuddy.security.tokens import encode_token

TEST_INTERNAL_TOKEN='v424-legacy-fixture-token'
os.environ.setdefault('TX_INTERNAL_TOKEN',TEST_INTERNAL_TOKEN)

def synthetic_professional_registry(tmp_path, monkeypatch, subjects):
    """Register synthetic qualifications only inside a test's private temporary directory."""
    path = tmp_path / 'professionals_TEST_ONLY.json'
    path.write_text(json.dumps({subject: {
        'subject': subject, 'identity_verified_at': 'TEST_ONLY_FIXTURE',
        'license_number': f'TEST_ONLY-{subject}', 'license_type': 'REGISTERED_COST_ENGINEER',
        'name': f'TEST_ONLY {subject}',
    } for subject in subjects}), encoding='utf-8')
    monkeypatch.setenv('TXBUDDY_PROFESSIONALS_FILE', str(path))


class TestClient(RawTestClient):
    __test__=False
    def request(self,method,url,**kwargs):
        from txbuddy.api import app
        if self.app is app:
            # Approved-basis branches in legacy unit tests use an isolated SQLite fixture registry.
            # These synthetic approvals are never imported into the real PostgreSQL sample corpus.
            payload=kwargs.get('json') or {}
            if isinstance(payload,dict) and payload.get('formal_calculation') is True and isinstance(payload.get('basis_pack'),dict):
                from txbuddy.pilot.database import dumps
                db=app.state.pilot_db
                if db.pg:raise RuntimeError('Legacy approval fixtures require an isolated SQLite test runtime')
                pack=payload['basis_pack'];pack_id=pack.get('basis_pack_id')
                if pack_id:
                    with db.transaction() as c:
                        c.execute('INSERT INTO basis_pack VALUES (?,?,?,?) ON CONFLICT DO NOTHING',
                            (pack_id,'LICENSED_VERIFIED',1,dumps({'TEST_ONLY_UNIT_FIXTURE':True,'basis':pack})))
            headers=dict(kwargs.get('headers') or {})
            if not any(k.lower()=='authorization' for k in headers):
                actor=next((v for k,v in headers.items() if k.lower()=='x-actor'),'anonymous')
                token=encode_token(subject=actor,settings=app.state.security.settings,roles=('admin','verified_professional'))
                headers['Authorization']='Bearer '+token
            kwargs['headers']=headers
        return super().request(method,url,**kwargs)
