"""Isolated V426 destructive tests. No real corpus or runtime is mutated."""
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

import pytest
from PIL import Image
from txbuddy.governance.ocr_review import ReviewStore, ReviewError, sha_file


@pytest.fixture
def review(tmp_path):
    corpus = tmp_path / 'TEST_ONLY_original'
    images = tmp_path / 'TEST_ONLY_images'
    corpus.mkdir(); images.mkdir()
    source = corpus / 'source.pdf'
    source.write_bytes(b'TEST_ONLY original source bytes')
    png = images / 'page.png'
    Image.new('RGB', (80, 120), 'white').save(png)
    payload = {'sha256': sha_file(source), 'engine': 'TEST_ONLY_OCR',
               'pages': [{'page': 1, 'image': str(png), 'image_sha256': sha_file(png)}],
               'tokens': [{'page': 1, 'text': 'TEST_ONLY 100 mm', 'confidence': .8}]}
    store = ReviewStore(tmp_path / 'TEST_ONLY.sqlite3')
    document = store.ingest(payload, 'TEST_ONLY_P', 'source.pdf', corpus, images)
    pageid = store.document(document['document_id'])['pages'][0]['page_id']
    return store, pageid, corpus, images, payload


def save(store, pageid, **changes):
    page = store.page(pageid)
    values = dict(expected_version=page['version'], corrected_text=page['corrected_text'],
                  structured_fields=page['structured_fields'], review_status='IN_REVIEW',
                  reviewer_id='TEST_ONLY_editor', reason='TEST_ONLY correction')
    values.update(changes)
    return store.save(pageid, **values)


@pytest.mark.parametrize('damage', ['event', 'delete_middle', 'delete_tail', 'previous', 'content', 'sequence'])
def test_damaged_history_rejects_new_writes(review, damage):
    store, pageid, *_ = review
    for value in ('TEST_ONLY 101 mm', 'TEST_ONLY 102 mm', 'TEST_ONLY 103 mm'):
        save(store, pageid, corrected_text=value)
    with store.connect() as conn:
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('DROP TRIGGER immutable_ocr_review_event_UPDATE')
        conn.execute('DROP TRIGGER immutable_ocr_review_event_DELETE')
        if damage == 'event':
            conn.execute("UPDATE ocr_review_event SET reason='changed' WHERE page_id=? AND version=1", (pageid,))
        elif damage == 'delete_middle':
            conn.execute('DELETE FROM ocr_review_event WHERE page_id=? AND version=2', (pageid,))
        elif damage == 'delete_tail':
            conn.execute('DELETE FROM ocr_review_event WHERE page_id=? AND version=3', (pageid,))
        elif damage == 'previous':
            conn.execute("UPDATE ocr_review_event SET previous_event_sha256='wrong' WHERE page_id=? AND version=2", (pageid,))
        elif damage == 'content':
            conn.execute("UPDATE ocr_review_event SET corrected_text='forged' WHERE page_id=? AND version=1", (pageid,))
        else:
            conn.execute('UPDATE ocr_review_event SET version=4 WHERE page_id=? AND version=3', (pageid,))
    before = store.page(pageid)['version']
    with pytest.raises(ReviewError) as caught:
        save(store, pageid, corrected_text='must never append')
    assert caught.value.code == 'OCR_HISTORY_CORRUPT'
    assert store.page(pageid)['version'] == before


@pytest.mark.parametrize('asset', ['source', 'image'])
def test_changed_original_or_page_image_rejects_write(review, asset):
    store, pageid, corpus, images, _ = review
    save(store, pageid)
    (corpus/'source.pdf' if asset=='source' else images/'page.png').write_bytes(b'TEST_ONLY tampered')
    with pytest.raises(ReviewError) as caught:
        save(store, pageid)
    assert caught.value.code == 'OCR_HISTORY_CORRUPT'


def test_competing_instances_allow_only_one_append(review):
    store, pageid, *_ = review
    def append(i):
        other = ReviewStore(store.path)
        try:
            other.save(pageid, expected_version=0, corrected_text=str(i), structured_fields={},
                       review_status='IN_REVIEW', reviewer_id='TEST_ONLY', reason='race')
            return 'ok'
        except ReviewError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(append, range(8)))
    assert outcomes.count('ok') == 1
    assert outcomes.count('REVIEW_VERSION_CONFLICT') == 7
    assert store.verify_chain(pageid)
    save(store, pageid)
    assert store.verify_chain(pageid)


def test_failed_transaction_rolls_back_all_chain_rows(review, monkeypatch):
    store, pageid, *_ = review
    def fail(*args):
        raise RuntimeError('TEST_ONLY failure')
    monkeypatch.setattr(store, '_after_event_insert', fail)
    with pytest.raises(RuntimeError):
        save(store, pageid)
    with store.connect() as conn:
        assert conn.execute('SELECT count(*) FROM ocr_review_event').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM ocr_event_binding').fetchone()[0] == 0
        assert conn.execute('SELECT version FROM ocr_chain_head WHERE page_id=?',(pageid,)).fetchone()[0] == 0
    monkeypatch.undo()
    save(store, pageid)
    assert store.verify_chain(pageid)


def test_corruption_refusal_persists_incident(review):
    store, pageid, corpus, *_ = review
    (corpus / 'source.pdf').write_bytes(b'TEST_ONLY altered')
    with pytest.raises(ReviewError):
        save(store, pageid)
    with store.connect() as conn:
        incident = conn.execute('SELECT code,details_json FROM ocr_security_incident').fetchone()
        assert incident['code'] == 'OCR_HISTORY_CORRUPT'
        assert 'SOURCE_HASH_MISMATCH' in json.loads(incident['details_json'])


def test_page_row_tampering_rejected(review):
    store, pageid, *_ = review
    with store.connect() as conn:
        conn.execute('DROP TRIGGER immutable_ocr_page_UPDATE')
        conn.execute("UPDATE ocr_page SET original_text='forged' WHERE page_id=?", (pageid,))
    with pytest.raises(ReviewError) as exc:
        save(store, pageid)
    assert exc.value.code == 'OCR_HISTORY_CORRUPT'


def test_binding_tampering_rejected(review):
    store, pageid, *_ = review
    save(store, pageid)
    with store.connect() as conn:
        conn.execute('DROP TRIGGER immutable_ocr_event_binding_UPDATE')
        conn.execute("UPDATE ocr_event_binding SET content_version=9 WHERE page_id=?", (pageid,))
    with pytest.raises(ReviewError) as exc:
        save(store, pageid)
    assert exc.value.code == 'OCR_HISTORY_CORRUPT'


def test_identical_sources_share_content_and_preserve_provenance(review):
    store, pageid, corpus, images, payload = review
    (corpus/'copy.pdf').write_bytes((corpus/'source.pdf').read_bytes())
    result=store.ingest(deepcopy(payload),'TEST_ONLY_P','copy.pdf',corpus,images,
                        provenance={'official_url':'https://example.invalid/copy','source_name':'copy'})
    inventory=store.content_inventory()
    assert result['ocr_result_reused'] is True
    assert len(inventory['contents']) == len(inventory['versions']) == 1
    assert len(inventory['sources']) == 2
    assert any(s['official_url']=='https://example.invalid/copy' for s in inventory['sources'])


def test_reserved_signoff_field_cannot_be_forged_by_edit(review):
    store, pageid, *_ = review
    result=save(store,pageid,structured_fields={'_professional_signoff':{'name':'forged'},'actual':'ok'})
    assert '_professional_signoff' not in result['structured_fields']
    assert store.verify_chain(pageid)


