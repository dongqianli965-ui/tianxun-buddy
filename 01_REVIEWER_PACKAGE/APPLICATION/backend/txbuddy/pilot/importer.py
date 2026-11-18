from pathlib import Path
import csv, hashlib, json, uuid, collections
from datetime import datetime,timezone
from decimal import Decimal
from .database import PilotDatabase, PilotError, dumps


def digest(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read_csv(path): return list(csv.DictReader(Path(path).open(encoding='utf-8-sig',newline='')))
def ident(path): return 'doc-'+hashlib.sha256(str(path).encode()).hexdigest()[:24]
def now(): return datetime.now(timezone.utc).isoformat()
def safe_file(root,relative):
    p=(root/relative).resolve()
    if root.resolve() not in p.parents or not p.is_file(): raise PilotError('CORPUS_PATH_INVALID',str(relative))
    return p


def verify_corpus(root):
    root=Path(root); results=[]
    for line in (root/'00_INDEX/SHA256SUMS.txt').read_text(encoding='utf-8-sig').splitlines():
        if not line.strip(): continue
        expected,relative=line.split(maxsplit=1); relative=relative.lstrip('* ')
        f=safe_file(root,relative)
        results.append({'path':relative,'sha256':digest(f),'ok':digest(f)==expected.lower()})
    if not all(x['ok'] for x in results): raise PilotError('CORPUS_HASH_MISMATCH','原件哈希不一致，导入拒绝')
    return results


def _import_corpus(db: PilotDatabase,root,*,fail_after: int | None=None):
    root=Path(root).resolve(); hashes=verify_corpus(root)
    verified_hashes={x['path'].replace('\\','/'):x['sha256'] for x in hashes}
    key='HN_L1_V1:'+digest(root/'00_INDEX/SHA256SUMS.txt')
    jobid='imp-'+uuid.uuid4().hex; step=0
    def tick():
        nonlocal step
        step+=1
        if fail_after is not None and step>=fail_after: raise PilotError('IMPORT_INJECTED_FAILURE','验证事务回滚')
    with db.transaction() as c:
        old=c.one('SELECT * FROM data_import_job WHERE import_key=?',(key,))
        if old: return {'status':'IDEMPOTENT','job_id':old['job_id'],'hashes':len(hashes)}
        c.execute('INSERT INTO data_import_job VALUES (?,?,?,?,?)',(jobid,key,'RUNNING',now(),dumps({'corpus':str(root)})))
        sources=read_csv(root/'00_INDEX/SOURCE_REGISTER.csv')
        bypath={s['local_path'].replace('\\','/'):s for s in sources}
        byhash={}
        for s in sources:
            if s.get('official_url'): byhash.setdefault(s['sha256'],s['official_url'])
        for s in sources:
            f=safe_file(root,s['local_path']); actual=digest(f)
            if actual!=s['sha256']: raise PilotError('SOURCE_HASH_MISMATCH',s['source_id'])
            url=s.get('official_url') or byhash.get(actual,'')
            s={**s,'resolved_url':url,'url_resolution':'REGISTERED' if s.get('official_url') else ('SAME_SHA256_REGISTER' if url else 'UNRESOLVED')}
            c.execute('INSERT INTO source_register VALUES (?,?,?,?,?,?,?)',(s['source_id'],s['local_path'],actual,url,
                s['license_status'],s['verification_status'],dumps(s))); tick()
        for f in sorted(x for x in root.rglob('*') if x.is_file()):
            rel=f.relative_to(root).as_posix(); s=bypath.get(rel)
            classification='ORIGINAL' if s else ('DERIVED' if rel.startswith(('06_','07_')) else 'INDEX_REPORT')
            c.execute('INSERT INTO document_inventory VALUES (?,?,?,?,?,?)',(ident(rel),rel,digest(f),s['source_id'] if s else None,classification,
                dumps({'original_read_only':True,'source':s,'kind':classification}))); tick()
        for s in read_csv(root/'00_INDEX/PROJECT_SAMPLE_MATRIX.csv'):
            prefix='05_REAL_PROJECTS/'+s['project_id']+'/'
            c.execute('INSERT INTO project_sample VALUES (?,?,?,?)',(s['sample_id'],s['project_id'],'NEEDS_EXPERT_REVIEW',dumps(s)))
            for d in c.rows('SELECT * FROM document_inventory WHERE local_path LIKE ?',(prefix+'%',)):
                part='A_INPUT' if '/A_INPUT/' in d['local_path'] else ('B_REFERENCE' if '/B_REFERENCE/' in d['local_path'] else 'UNPARTITIONED')
                c.execute('INSERT INTO project_document VALUES (?,?,?)',(s['sample_id'],d['document_id'],part))
        tables=root/'07_DERIVED_DATA/STRUCTURED_TABLES'
        for filename,period,expected in [('郑州_2026Q2_材料价格信息_结构化.csv','2026-Q2',5026),('郑州_2026Q3_材料价格信息_结构化.csv','2026-Q3',5024),('郑州_2026年9月_主要材料价格信息_结构化.csv','2026-09',939)]:
            f=tables/filename; rows=read_csv(f)
            if len(rows)!=expected: raise PilotError('PRICE_COUNT_MISMATCH',filename)
            duplicate_ids=collections.Counter(r['record_id'] for r in rows)
            for line,r in enumerate(rows,2):
                original=safe_file(root,r['original_file']); sha=r['file_sha256']
                if verified_hashes[r['original_file']]!=sha: raise PilotError('PRICE_SOURCE_HASH_MISMATCH',r['record_id'])
                for field in ('material_name','unit','license_status','region','period'):
                    if not r.get(field): raise PilotError('PRICE_REQUIRED_FIELD',field)
                if r['region']!='郑州市': raise PilotError('REGION_MISMATCH','非郑州记录禁止写入郑州库')
                values=[]
                for field in ('price_incl_tax','price_excl_tax'):
                    v=Decimal(r[field])
                    if not v.is_finite() or v<0: raise PilotError('PRICE_INVALID',r['record_id'])
                    values.append(str(v))
                role='ERRATA_REFERENCE' if period=='2026-Q3' and int(r['source_page'])==137 and int(r['source_row']) in (1782,1783,1784) else 'OFFICIAL_INFORMATION'
                raw={**r,'derived_csv':f.relative_to(root).as_posix(),'derived_sha256':verified_hashes[f.relative_to(root).as_posix()],'csv_row':line,'tax_rate_inferred':False,
                     'raw_record_id':r['record_id'],'record_role':role,'duplicate_source_id':duplicate_ids[r['record_id']]>1}
                storage_id=r['record_id'] if duplicate_ids[r['record_id']]==1 else r['record_id']+f"@p{r['source_page']}-csv{line}"
                c.execute('INSERT INTO material_price VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(
                    storage_id,r['material_name'],r['spec_model'],r['unit'],'410100-INNER-4RING',period,*values,
                    ident(r['original_file']),sha,int(r['source_page']),int(r['source_row']),r['license_status'],
                    ('ERRATA_FOR_Q2' if role=='ERRATA_REFERENCE' else 'VERIFIED') if period=='2026-Q3' else 'NOTICE_PENDING_VERIFICATION','OFFICIAL_INFORMATION',dumps(raw),jobid)); tick()
        q3=c.one('SELECT document_id,sha256 FROM material_price WHERE period=? LIMIT 1',('2026-Q3',))
        for r in read_csv(tables/'郑州_2026Q2_沥青混凝土_勘误记录.csv'):
            record='ZZ-20262-'+r['sequence']
            original=c.one('SELECT * FROM material_price WHERE record_id=?',(record,))
            if not original or original['material_name']!=r['material_name']: raise PilotError('ERRATA_TARGET_MISMATCH',record)
            c.execute('INSERT INTO material_price_correction VALUES (?,?,?,?,?,?,?,?,?)',('Q3-ERRATA-'+r['sequence'],record,
                q3['document_id'],q3['sha256'],137,r['corrected_price_incl_tax'],r['corrected_price_excl_tax'],dumps(r),jobid))
        c.execute('UPDATE data_import_job SET status=? WHERE job_id=?',('SUCCEEDED',jobid))
        c.execute('INSERT INTO data_import_audit VALUES (?,?,?,?,?)',(uuid.uuid4().hex,jobid,'ATOMIC_IMPORT_COMMIT',now(),dumps({'hashes':len(hashes),'prices':10989,'corrections':3})))
    return {'status':'SUCCEEDED','job_id':jobid,'hashes':len(hashes),'counts':db.counts()}


def import_corpus(db: PilotDatabase,root,*,fail_after: int | None=None):
    try:
        return _import_corpus(db,root,fail_after=fail_after)
    except Exception as exc:
        # Data transaction has rolled back. Preserve a separate failure audit transaction.
        failed='failed-'+uuid.uuid4().hex;at=datetime.now(timezone.utc).isoformat()
        with db.transaction() as c:
            detail={'code':getattr(exc,'code',type(exc).__name__),'reason':str(exc),'business_transaction':'ROLLED_BACK'}
            c.execute('INSERT INTO data_import_job VALUES (?,?,?,?,?)',(failed,failed,'FAILED',at,dumps(detail)))
            c.execute('INSERT INTO data_import_audit VALUES (?,?,?,?,?)',(uuid.uuid4().hex,failed,'IMPORT_ROLLBACK',at,dumps(detail)))
        raise
