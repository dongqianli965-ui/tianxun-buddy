"""Append-only OCR review. Ingestion and review are distinct from professional signoff."""
from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from datetime import datetime, timezone
import hashlib, json, math, sqlite3, uuid, logging

logger = logging.getLogger(__name__)

STATUSES = ('NEEDS_EXPERT_REVIEW', 'IN_REVIEW', 'REVIEWED', 'RETURNED', 'PROFESSIONALLY_SIGNED')

def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def sha_file(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def now():
    return datetime.now(timezone.utc).isoformat()

class ReviewError(ValueError):
    def __init__(self, code, message, status=422):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)

class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()

class ReviewStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS ocr_document (
                document_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, source_path TEXT NOT NULL,
                source_file_sha256 TEXT NOT NULL, OCR_engine TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL, priority INTEGER NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(project_id,source_path,source_file_sha256,payload_sha256));
              CREATE TABLE IF NOT EXISTS ocr_page (
                page_id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES ocr_document(document_id),
                page_number INTEGER NOT NULL CHECK(page_number>0), image_sha256 TEXT NOT NULL,
                image_path TEXT NOT NULL, image_width INTEGER NOT NULL, image_height INTEGER NOT NULL,
                original_text TEXT NOT NULL, confidence REAL, tokens_json TEXT NOT NULL,
                UNIQUE(document_id,page_number));
              CREATE TABLE IF NOT EXISTS ocr_review_event (
                event_id TEXT PRIMARY KEY, page_id TEXT NOT NULL REFERENCES ocr_page(page_id),
                version INTEGER NOT NULL CHECK(version>0), corrected_text TEXT NOT NULL,
                structured_fields_json TEXT NOT NULL, reviewer_id TEXT NOT NULL, reviewed_at TEXT NOT NULL,
                review_status TEXT NOT NULL CHECK(review_status IN
                  ('NEEDS_EXPERT_REVIEW','IN_REVIEW','REVIEWED','RETURNED','PROFESSIONALLY_SIGNED')),
                reason TEXT NOT NULL, human_confirmed INTEGER NOT NULL,
                previous_event_sha256 TEXT NOT NULL, event_sha256 TEXT NOT NULL,
                UNIQUE(page_id,version));
              CREATE INDEX IF NOT EXISTS ocr_doc_project ON ocr_document(project_id,priority);
              CREATE INDEX IF NOT EXISTS ocr_event_page ON ocr_review_event(page_id,version);
              CREATE TABLE IF NOT EXISTS governance_catalog_snapshot (
                snapshot_sha256 TEXT PRIMARY KEY, kind TEXT NOT NULL, created_at TEXT NOT NULL, payload_json TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS governance_catalog_current (
                kind TEXT PRIMARY KEY, snapshot_sha256 TEXT NOT NULL REFERENCES governance_catalog_snapshot(snapshot_sha256));
            ''')
            c.executescript('''
              CREATE TABLE IF NOT EXISTS ocr_content (
                content_id TEXT PRIMARY KEY, source_file_sha256 TEXT NOT NULL UNIQUE,
                file_format TEXT NOT NULL, size_bytes INTEGER NOT NULL, created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS ocr_content_version (
                content_id TEXT NOT NULL REFERENCES ocr_content(content_id), version INTEGER NOT NULL,
                ocr_sha256 TEXT NOT NULL, OCR_engine TEXT NOT NULL, page_count INTEGER NOT NULL,
                pages_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(content_id,version),
                UNIQUE(content_id,ocr_sha256));
              CREATE TABLE IF NOT EXISTS ocr_source_record (
                source_id TEXT PRIMARY KEY, document_id TEXT NOT NULL UNIQUE REFERENCES ocr_document(document_id),
                content_id TEXT NOT NULL, content_version INTEGER NOT NULL, source_path TEXT NOT NULL,
                original_absolute_path TEXT NOT NULL, official_url TEXT, source_name TEXT NOT NULL,
                acquired_at TEXT, authorization_status TEXT NOT NULL, source_version TEXT, source_note TEXT NOT NULL,
                source_record_json TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(content_id,content_version) REFERENCES ocr_content_version(content_id,version));
              CREATE TABLE IF NOT EXISTS ocr_asset_anchor (
                page_id TEXT PRIMARY KEY REFERENCES ocr_page(page_id), document_id TEXT NOT NULL,
                source_id TEXT NOT NULL REFERENCES ocr_source_record(source_id),
                anchor_json TEXT NOT NULL, anchor_sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS ocr_chain_head (
                page_id TEXT PRIMARY KEY REFERENCES ocr_page(page_id), version INTEGER NOT NULL,
                event_sha256 TEXT NOT NULL, binding_sha256 TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS ocr_event_binding (
                event_id TEXT PRIMARY KEY REFERENCES ocr_review_event(event_id), page_id TEXT NOT NULL,
                event_version INTEGER NOT NULL, action TEXT NOT NULL, content_version INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL, submitted_version INTEGER, submitted_content_sha256 TEXT,
                asset_anchor_sha256 TEXT NOT NULL, previous_binding_sha256 TEXT NOT NULL,
                binding_sha256 TEXT NOT NULL, UNIQUE(page_id,event_version));
              CREATE TABLE IF NOT EXISTS ocr_security_incident (
                incident_id TEXT PRIMARY KEY, page_id TEXT NOT NULL, operation TEXT NOT NULL,
                code TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL);
            ''')
            for table in ('ocr_document', 'ocr_page', 'ocr_review_event', 'governance_catalog_snapshot',
                          'ocr_content', 'ocr_content_version', 'ocr_source_record', 'ocr_asset_anchor',
                          'ocr_event_binding', 'ocr_security_incident'):

                for operation in ('UPDATE', 'DELETE'):
                    c.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'OCR_APPEND_ONLY'); END")

    def import_catalog(self, kind, payload):
        if kind not in ('projects','sources') or not isinstance(payload.get(kind),list):
            raise ReviewError('CATALOG_INVALID','资料矩阵格式错误')
        raw=dumps(payload);digest=hashlib.sha256(raw.encode()).hexdigest()
        with self.transaction() as c:
            c.execute('INSERT INTO governance_catalog_snapshot VALUES (?,?,?,?) ON CONFLICT DO NOTHING',(digest,kind,now(),raw))
            c.execute('INSERT INTO governance_catalog_current VALUES (?,?) ON CONFLICT(kind) DO UPDATE SET snapshot_sha256=excluded.snapshot_sha256',(kind,digest))
        return digest

    def catalog(self, kind):
        with self.connect() as c:
            r=c.execute('SELECT s.payload_json,s.snapshot_sha256 FROM governance_catalog_snapshot s JOIN governance_catalog_current p USING(snapshot_sha256) WHERE p.kind=?',(kind,)).fetchone()
            if not r:
                return {kind:[], 'available':False}
            result=json.loads(r['payload_json']);result['snapshot_sha256']=r['snapshot_sha256'];result['available']=True
            return result

    def connect(self):
        c = sqlite3.connect(self.path, timeout=30, factory=ClosingConnection)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        return c

    @contextmanager
    def transaction(self):
        c = self.connect()
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def ingest(self, payload, project_id, source_path, corpus_root, image_root, *, provenance=None):
        """Trusted local importer only. Verify original and all images before one atomic insert."""
        rel = PurePosixPath(source_path)
        if rel.is_absolute() or '..' in rel.parts or '\\' in source_path or ':' in source_path:
            raise ReviewError('SOURCE_PATH_INVALID', '原始资料路径非法')
        corpus_root = Path(corpus_root).resolve()
        source = (corpus_root / source_path).resolve()
        if not source.is_relative_to(corpus_root) or sha_file(source) != payload['sha256']:
            raise ReviewError('SOURCE_HASH_MISMATCH', '原件 SHA256 不一致')
        image_root = Path(image_root).resolve()
        if image_root == corpus_root or image_root.is_relative_to(corpus_root):
            raise ReviewError('DERIVED_LOCATION_INVALID', '派生目录不得位于原始数据仓')
        payload_sha = hashlib.sha256(dumps(payload).encode()).hexdigest()
        docid = 'ocr-' + hashlib.sha256((project_id+'\n'+source_path+'\n'+payload_sha).encode()).hexdigest()[:32]
        name = source.name
        priority = 1 if '173' in name else 2 if ('2026' in name and '23' in name) else 3 if '2024' in name and '指数' in name else 4 if ('计价' in name or '定额' in name or '税' in name or '费用' in name) else 5
        rows = []
        from PIL import Image
        for page in payload['pages']:
            path = Path(page['image']).resolve()
            if not path.is_relative_to(image_root) or sha_file(path) != page['image_sha256']:
                raise ReviewError('IMAGE_HASH_MISMATCH', 'OCR 页图路径或 SHA256 不一致')
            with Image.open(path) as im:
                width, height = im.size
            number = page['page']
            if type(number) is not int or number < 1 or any(r[2] == number for r in rows):
                raise ReviewError('OCR_PAGE_NUMBER_INVALID','页号须为正整数且不可重复')
            tokens = [t for t in payload['tokens'] if t['page'] == number]
            for t in tokens:
                confidence = t.get('confidence')
                if not isinstance(confidence, (float, int)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
                    raise ReviewError('CONFIDENCE_INVALID', '置信度非法')
            text = '\n'.join(t['text'] for t in tokens)
            confidence = sum(t['confidence'] for t in tokens)/len(tokens) if tokens else None
            pageid = docid+'-p'+str(number)
            rows.append((pageid, docid, number, page['image_sha256'], str(path), width, height, text, confidence, dumps(tokens)))
        if any(type(t.get('page')) is not int or t['page'] not in {r[2] for r in rows} for t in payload['tokens']):
            raise ReviewError('OCR_TOKEN_PAGE_INVALID','OCR token 未关联已登记页面')
        if not rows:
            raise ReviewError('OCR_PAGES_REQUIRED', '缺少可定位页图')
        with self.transaction() as c:
            existing = c.execute('SELECT 1 FROM ocr_document WHERE document_id=?', (docid,)).fetchone()
            if not existing:
                c.execute('INSERT INTO ocr_document VALUES (?,?,?,?,?,?,?,?)',
                          (docid, project_id, source_path, payload['sha256'], payload['engine'], payload_sha, priority, now()))
                c.executemany('INSERT INTO ocr_page VALUES (?,?,?,?,?,?,?,?,?,?)', rows)
            stored_doc=c.execute('SELECT * FROM ocr_document WHERE document_id=?',(docid,)).fetchone()
            if (stored_doc['project_id']!=project_id or stored_doc['source_path']!=source_path or
                stored_doc['source_file_sha256']!=payload['sha256'] or stored_doc['payload_sha256']!=payload_sha):
                raise ReviewError('OCR_HISTORY_CORRUPT','原有登记与经校验的导入来源不一致',409)
            stored_rows=[tuple(r) for r in c.execute('SELECT * FROM ocr_page WHERE document_id=? ORDER BY page_number',(docid,))]
            if stored_rows!=sorted(rows,key=lambda r:r[2]):
                raise ReviewError('OCR_HISTORY_CORRUPT','原有页面与经校验的 OCR 结果不一致',409)
            metadata = self._register_content(c, docid, source, payload, rows, provenance or {})
        return {'document_id':docid, 'status':'IDEMPOTENT' if existing else 'IMPORTED',
                'pages':len(rows), **metadata}

    def documents(self):
        with self.connect() as c:
            docs = [dict(r) for r in c.execute('SELECT * FROM ocr_document ORDER BY priority,source_path,document_id')]
            for d in docs:
                pages = c.execute('''SELECT p.page_id,coalesce(e.review_status,'NEEDS_EXPERT_REVIEW') status
                  FROM ocr_page p LEFT JOIN ocr_review_event e ON e.page_id=p.page_id
                    AND e.version=(SELECT max(x.version) FROM ocr_review_event x WHERE x.page_id=p.page_id)
                  WHERE p.document_id=?''', (d['document_id'],)).fetchall()
                d['page_count'] = len(pages)
                d['status_counts'] = {s:sum(p['status']==s for p in pages) for s in STATUSES}
                statuses = {p['status'] for p in pages}
                d['review_status'] = ('PROFESSIONALLY_SIGNED' if statuses=={'PROFESSIONALLY_SIGNED'} else
                  'REVIEWED' if statuses <= {'REVIEWED','PROFESSIONALLY_SIGNED'} else
                  'RETURNED' if 'RETURNED' in statuses else 'IN_REVIEW' if statuses!={'NEEDS_EXPERT_REVIEW'} else 'NEEDS_EXPERT_REVIEW')
                d['formal_usable'] = False  # Signoff does not create a licensed fee/basis pack.
        return docs

    def document(self, docid):
        with self.connect() as c:
            r = c.execute('SELECT * FROM ocr_document WHERE document_id=?', (docid,)).fetchone()
            if not r:
                return None
            d = dict(r)
            d['pages'] = [dict(p) for p in c.execute('SELECT page_id,page_number,image_sha256 FROM ocr_page WHERE document_id=? ORDER BY page_number', (docid,))]
            return d

    def page(self, pageid, *, connection=None):
        c = connection or self.connect()
        try:
            r = c.execute('''SELECT p.*,d.project_id,d.source_path,d.source_file_sha256,d.OCR_engine
              FROM ocr_page p JOIN ocr_document d USING(document_id) WHERE page_id=?''', (pageid,)).fetchone()
            if not r:
                return None
            p = dict(r)
            p['tokens'] = json.loads(p.pop('tokens_json'))
            history = [dict(e) for e in c.execute('SELECT * FROM ocr_review_event WHERE page_id=? ORDER BY version', (pageid,))]
            for e in history:
                e['structured_fields'] = json.loads(e.pop('structured_fields_json'))
            latest = history[-1] if history else {}
            p.update(version=latest.get('version',0), corrected_text=latest.get('corrected_text',p['original_text']),
                     structured_fields=latest.get('structured_fields',{}), reviewer_id=latest.get('reviewer_id'),
                     reviewed_at=latest.get('reviewed_at'), review_status=latest.get('review_status','NEEDS_EXPERT_REVIEW'),
                     change_history=history, formal_usable=False)
            binding = c.execute('SELECT * FROM ocr_event_binding WHERE event_id=?', (latest.get('event_id',''),)).fetchone()
            action = binding['action'] if binding else None
            p.update(content_sha256=self.content_hash(p['corrected_text'], p['structured_fields']),
                     content_version=binding['content_version'] if binding else 1,
                     workflow_state='SUBMITTED' if action=='submit' else
                        'DRAFT' if action=='edit' or not history else p['review_status'],
                     submitted_version=binding['submitted_version'] if binding else None,
                     submitted_content_sha256=binding['submitted_content_sha256'] if binding else None)
            return p
        finally:
            if connection is None:
                c.close()

    @staticmethod
    def content_hash(text, fields):
        # A server-verified professional registry snapshot is not OCR content.
        fields = {k:v for k,v in fields.items() if k != '_professional_signoff'}
        return hashlib.sha256(dumps({'corrected_text':text,'structured_fields':fields}).encode()).hexdigest()

    def _register_content(self, c, docid, source, payload, rows, provenance):
        content_id='ocont-'+payload['sha256']
        normalized=[]
        for row in rows:
            tokens=json.loads(row[9])
            for token in tokens:
                token.pop('image',None)
            normalized.append(dict(page=row[2],image_sha256=row[3],width=row[5],height=row[6],
                original_text=row[7],confidence=row[8],tokens=tokens))
        normalized.sort(key=lambda r:r['page'])
        raw=dumps({'engine':payload['engine'],'pages':normalized})
        digest=hashlib.sha256(raw.encode()).hexdigest()
        c.execute('INSERT INTO ocr_content VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING',
                  (content_id,payload['sha256'],source.suffix.lower().lstrip('.'),source.stat().st_size,now()))
        version=c.execute('SELECT version FROM ocr_content_version WHERE content_id=? AND ocr_sha256=?',
                          (content_id,digest)).fetchone()
        reused=version is not None
        if version:
            version=version['version']
        else:
            version=c.execute('SELECT coalesce(max(version),0)+1 FROM ocr_content_version WHERE content_id=?',
                              (content_id,)).fetchone()[0]
            c.execute('INSERT INTO ocr_content_version VALUES (?,?,?,?,?,?,?)',
                      (content_id,version,digest,payload['engine'],len(rows),raw,now()))
        source_id='osrc-'+hashlib.sha256(docid.encode()).hexdigest()
        document=dict(c.execute('SELECT * FROM ocr_document WHERE document_id=?',(docid,)).fetchone())
        c.execute('INSERT INTO ocr_source_record VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                  (source_id,docid,content_id,version,document['source_path'],str(source),
                   provenance.get('official_url'),provenance.get('source_name',source.name),
                   provenance.get('acquired_at'),provenance.get('authorization_status','UNKNOWN'),
                   provenance.get('source_version'),provenance.get('source_note',''),dumps(provenance),now()))
        for row in rows:
            stored=dict(c.execute('SELECT * FROM ocr_page WHERE page_id=?',(row[0],)).fetchone())
            anchor=c.execute('SELECT 1 FROM ocr_asset_anchor WHERE page_id=?',(row[0],)).fetchone()
            if not anchor:
                if c.execute('SELECT 1 FROM ocr_review_event WHERE page_id=?',(row[0],)).fetchone():
                    raise ReviewError('OCR_LEGACY_BASELINE_REQUIRED',
                        '已有未锚定复核历史，须独立核验后迁移；禁止自动重签历史',409)
                anchor_raw=dumps({'document':document,'page':stored,'source_absolute_path':str(source)})
                anchor_sha=hashlib.sha256(anchor_raw.encode()).hexdigest()
                c.execute('INSERT INTO ocr_asset_anchor VALUES (?,?,?,?,?,?)',
                          (row[0],docid,source_id,anchor_raw,anchor_sha,now()))
                c.execute('INSERT INTO ocr_chain_head VALUES (?,?,?,?)',(row[0],0,'',''))
        return dict(content_id=content_id,content_version=version,ocr_result_reused=reused,source_id=source_id)

    def content_inventory(self):
        with self.connect() as c:
            return {'contents':[dict(r) for r in c.execute('SELECT * FROM ocr_content ORDER BY content_id')],
                    'versions':[dict(r) for r in c.execute('SELECT content_id,version,ocr_sha256,OCR_engine,page_count,created_at FROM ocr_content_version ORDER BY content_id,version')],
                    'sources':[dict(r) for r in c.execute('SELECT * FROM ocr_source_record ORDER BY source_path,source_id')]}

    def _integrity_issues(self,c,pageid):
        issues=[]
        anchor=c.execute('SELECT * FROM ocr_asset_anchor WHERE page_id=?',(pageid,)).fetchone()
        page=c.execute('SELECT * FROM ocr_page WHERE page_id=?',(pageid,)).fetchone()
        if page is None:
            return ['PAGE_MISSING']
        if anchor is None:
            return ['ASSET_ANCHOR_MISSING']
        try:
            facts=json.loads(anchor['anchor_json'])
            if hashlib.sha256(anchor['anchor_json'].encode()).hexdigest()!=anchor['anchor_sha256']:
                issues.append('ANCHOR_HASH_MISMATCH')
            doc=c.execute('SELECT * FROM ocr_document WHERE document_id=?',(page['document_id'],)).fetchone()
            if dict(page)!=facts['page'] or not doc or dict(doc)!=facts['document']:
                issues.append('PAGE_DOCUMENT_RELATION_MISMATCH')
            source=c.execute('SELECT * FROM ocr_source_record WHERE source_id=?',(anchor['source_id'],)).fetchone()
            if not source or source['document_id']!=page['document_id'] or source['original_absolute_path']!=facts['source_absolute_path']:
                issues.append('SOURCE_RELATION_MISMATCH')
            if sha_file(facts['source_absolute_path'])!=facts['document']['source_file_sha256']:
                issues.append('SOURCE_HASH_MISMATCH')
            if sha_file(page['image_path'])!=page['image_sha256']:
                issues.append('IMAGE_HASH_MISMATCH')
        except (OSError,ValueError,KeyError,TypeError):
            issues.append('ASSET_UNAVAILABLE_OR_INVALID')
        previous='';previous_binding='';version=0;content_version=1;submitted=None;submitted_sha=None;state='DRAFT'
        content_sha=self.content_hash(page['original_text'],{})
        for row in c.execute('SELECT * FROM ocr_review_event WHERE page_id=? ORDER BY version',(pageid,)):
            event=dict(row);digest=event.pop('event_sha256');version+=1
            if event['version']!=version or event['previous_event_sha256']!=previous or hashlib.sha256(dumps(event).encode()).hexdigest()!=digest:
                issues.append('EVENT_HASH_SEQUENCE_MISMATCH')
            previous=digest
            binding=c.execute('SELECT * FROM ocr_event_binding WHERE event_id=?',(event['event_id'],)).fetchone()
            if not binding:
                issues.append('EVENT_BINDING_MISSING')
                continue
            binding=dict(binding);binding_sha=binding.pop('binding_sha256')
            try:
                current_sha=self.content_hash(event['corrected_text'],json.loads(event['structured_fields_json']))
            except (ValueError,TypeError,AttributeError):
                issues.append('EVENT_CONTENT_INVALID');continue
            if current_sha!=content_sha:
                content_version+=1
            if (binding['page_id']!=pageid or binding['event_version']!=version or
                binding['content_sha256']!=current_sha or binding['content_version']!=content_version or
                binding['asset_anchor_sha256']!=anchor['anchor_sha256'] or
                binding['previous_binding_sha256']!=previous_binding or
                hashlib.sha256(dumps(binding).encode()).hexdigest()!=binding_sha):
                issues.append('CONTENT_VERSION_OR_BINDING_MISMATCH')
            action=binding['action']
            if action=='edit':
                submitted=None;submitted_sha=None;state='DRAFT'
                if event['review_status'] not in ('NEEDS_EXPERT_REVIEW','IN_REVIEW'):
                    issues.append('EDIT_STATUS_INVALID')
            elif action=='submit':
                if current_sha!=content_sha or event['review_status']!='IN_REVIEW':
                    issues.append('SUBMIT_CONTENT_CHANGED')
                submitted=version;submitted_sha=current_sha;state='SUBMITTED'
            elif action in ('approve','return'):
                if state!='SUBMITTED' or current_sha!=content_sha or event['review_status']!=('REVIEWED' if action=='approve' else 'RETURNED'):
                    issues.append('DECISION_NOT_BOUND_TO_SUBMISSION')
                state=event['review_status']
            elif action=='signoff':
                if state!='REVIEWED' or current_sha!=content_sha or event['review_status']!='PROFESSIONALLY_SIGNED':
                    issues.append('SIGNOFF_NOT_BOUND_TO_APPROVAL')
                state='PROFESSIONALLY_SIGNED'
            else:
                issues.append('BINDING_ACTION_INVALID')
            if binding['submitted_version']!=submitted or binding['submitted_content_sha256']!=submitted_sha:
                issues.append('SUBMISSION_BINDING_MISMATCH')
            content_sha=current_sha;previous_binding=binding_sha
        head=c.execute('SELECT * FROM ocr_chain_head WHERE page_id=?',(pageid,)).fetchone()
        if not head or head['version']!=version or head['event_sha256']!=previous or head['binding_sha256']!=previous_binding:
            issues.append('CHAIN_HEAD_MISMATCH')
        if c.execute('SELECT count(*) FROM ocr_event_binding WHERE page_id=?',(pageid,)).fetchone()[0]!=version:
            issues.append('BINDING_COUNT_MISMATCH')
        return issues

    def _record_incident(self,pageid,issues):
        # This second transaction follows rollback so the refusal remains traceable.
        logger.error('OCR_WRITE_REFUSED page_id=%s issues=%s',pageid,issues)
        try:
            with self.transaction() as c:
                c.execute('INSERT INTO ocr_security_incident VALUES (?,?,?,?,?,?)',
                          ('oinc-'+uuid.uuid4().hex,pageid,'APPEND_REVIEW','OCR_HISTORY_CORRUPT',dumps(issues),now()))
        except sqlite3.Error:
            logger.exception('OCR incident persistence failed page_id=%s',pageid)

    def save(self, pageid, *, expected_version, corrected_text, structured_fields, review_status, reviewer_id,
             reason='', human_confirmed=False, professional=None, action=None,
             expected_content_sha256=None, submitted_version=None):
        if type(expected_version) is not int or expected_version < 0:
            raise ReviewError('REVIEW_VERSION_REQUIRED', '必须提供当前版本号')
        if review_status not in STATUSES:
            raise ReviewError('REVIEW_STATUS_INVALID', '未知复核状态')
        if not isinstance(corrected_text,str) or len(corrected_text)>200000:
            raise ReviewError('REVIEW_TEXT_INVALID', '文本超长或类型非法')
        if not isinstance(structured_fields,dict) or len(dumps(structured_fields))>50000:
            raise ReviewError('REVIEW_FIELDS_INVALID', '结构化字段须为 JSON 对象且不超过50KB')
        if not isinstance(reason,str) or len(reason)>4000:
            raise ReviewError('REVIEW_REASON_INVALID', '修改说明超长或类型非法')
        action=action or {'REVIEWED':'approve','RETURNED':'return','PROFESSIONALLY_SIGNED':'signoff'}.get(review_status,'edit')
        target={'edit':review_status if review_status in ('IN_REVIEW','NEEDS_EXPERT_REVIEW') else 'NEEDS_EXPERT_REVIEW',
                'submit':'IN_REVIEW','approve':'REVIEWED','return':'RETURNED','signoff':'PROFESSIONALLY_SIGNED'}
        if action not in target:
            raise ReviewError('REVIEW_ACTION_INVALID','未知复核动作')
        review_status=target[action]
        if action in ('approve','signoff') and (human_confirmed is not True or not corrected_text.strip()):
            raise ReviewError('HUMAN_REVIEW_REQUIRED', '已复核须由操作人明确确认逐页校对')
        if action=='return' and not reason.strip():
            raise ReviewError('RETURN_REASON_REQUIRED', '复核退回须填写原因')
        issues=[]
        try:
            with self.transaction() as c:
                exists=c.execute('SELECT 1 FROM ocr_page WHERE page_id=?',(pageid,)).fetchone()
                if not exists:
                    raise ReviewError('OCR_NOT_FOUND','OCR 结果不存在',404)
                try:
                    issues=self._integrity_issues(c,pageid)
                except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
                    issues=['INTEGRITY_DATA_INVALID']
                if issues:
                    raise ReviewError('OCR_HISTORY_CORRUPT','原件、页图或完整审计链异常，拒绝追加；须独立调查',409)
                page=self.page(pageid,connection=c)
                if page['version']!=expected_version:
                    raise ReviewError('REVIEW_VERSION_CONFLICT', '已有更新，请重新载入后合并',409)
                content_sha=self.content_hash(corrected_text,structured_fields)
                if expected_content_sha256 is not None and expected_content_sha256!=page['content_sha256']:
                    raise ReviewError('REVIEW_CONTENT_CONFLICT','内容 SHA256 不匹配，请重新载入',409)
                if action!='edit':
                    if content_sha!=page['content_sha256']:
                        raise ReviewError('REVIEW_CONTENT_CONFLICT','提交或复核动作不得同时修改 OCR 内容',409)
                    if expected_content_sha256 is None:
                        raise ReviewError('REVIEW_CONTENT_REQUIRED','须提供准确内容 SHA256',422)
                if action in ('approve','return'):
                    if page['workflow_state']!='SUBMITTED':
                        raise ReviewError('REVIEW_SUBMISSION_REQUIRED','须先提交准确版本供复核',409)
                    if type(submitted_version) is not int or submitted_version!=page['submitted_version']:
                        raise ReviewError('REVIEW_SUBMISSION_CONFLICT','提交版本不匹配',409)
                if action=='signoff':
                    if not professional or professional.get('subject')!=reviewer_id or not professional.get('identity_verified_at') or not professional.get('license_number') or not professional.get('name'):
                        raise ReviewError('PROFESSIONAL_IDENTITY_REQUIRED','尚无真实专业人员身份及资质核验登记',403)
                    if page['review_status']!='REVIEWED':
                        raise ReviewError('PROFESSIONAL_REVIEW_REQUIRED','须先保存人工已复核版本，再对同一内容签认',422)
                previous=page['change_history'][-1]['event_sha256'] if page['change_history'] else ''
                fields=dict(structured_fields)
                # Signoff metadata is assigned only by the verified server path.
                fields.pop('_professional_signoff', None)
                if professional and action=='signoff':
                    fields['_professional_signoff']=professional
                event=dict(event_id='orev-'+uuid.uuid4().hex,page_id=pageid,version=expected_version+1,
                    corrected_text=corrected_text,structured_fields_json=dumps(fields),reviewer_id=reviewer_id,
                    reviewed_at=now(),review_status=review_status,reason=reason,human_confirmed=int(human_confirmed is True),
                    previous_event_sha256=previous)
                event['event_sha256']=hashlib.sha256(dumps(event).encode()).hexdigest()
                c.execute('INSERT INTO ocr_review_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',tuple(event.values()))
                head=c.execute('SELECT * FROM ocr_chain_head WHERE page_id=?',(pageid,)).fetchone()
                anchor=c.execute('SELECT anchor_sha256 FROM ocr_asset_anchor WHERE page_id=?',(pageid,)).fetchone()[0]
                binding=dict(event_id=event['event_id'],page_id=pageid,event_version=event['version'],action=action,
                    content_version=page['content_version']+int(content_sha!=page['content_sha256']),content_sha256=content_sha,
                    submitted_version=event['version'] if action=='submit' else page['submitted_version'] if action in ('approve','return','signoff') else None,
                    submitted_content_sha256=content_sha if action=='submit' else page['submitted_content_sha256'] if action in ('approve','return','signoff') else None,
                    asset_anchor_sha256=anchor,previous_binding_sha256=head['binding_sha256'])
                binding['binding_sha256']=hashlib.sha256(dumps(binding).encode()).hexdigest()
                c.execute('INSERT INTO ocr_event_binding VALUES (?,?,?,?,?,?,?,?,?,?,?)',tuple(binding.values()))
                self._after_event_insert(c,event)
                c.execute('UPDATE ocr_chain_head SET version=?,event_sha256=?,binding_sha256=? WHERE page_id=?',
                          (event['version'],event['event_sha256'],binding['binding_sha256'],pageid))
        except ReviewError:
            if issues:
                self._record_incident(pageid,issues)
            raise
        return self.page(pageid)

    def _after_event_insert(self,c,event):
        """Fault-injection seam; production deliberately performs no side effect."""

    def verify_chain(self,pageid):
        with self.connect() as c:
            c.execute('BEGIN')
            try:
                return not self._integrity_issues(c,pageid)
            except (ValueError,KeyError,TypeError,sqlite3.DatabaseError):
                return False


