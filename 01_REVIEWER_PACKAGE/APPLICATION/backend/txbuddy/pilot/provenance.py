from pathlib import Path
import json
from .importer import read_csv,digest
from .database import dumps


def enrich_sources(db,corpus):
    root=Path(corpus);logs=root.parent/'_logs';links={}
    for row in read_csv(root/'00_INDEX/SOURCE_REGISTER.csv'):
        if row.get('official_url'):links[row['sha256']]={'url':row['official_url'],'method':'REGISTERED'}
    reuse=logs/'reuse_manifest.csv'
    if reuse.is_file():
        sha=digest(reuse)
        for r in read_csv(reuse):
            url=r.get('official_url') or r.get('download_url')
            if url:links[r['sha256']]={'url':url,'method':'REUSE_MANIFEST_SHA256','evidence_file':str(reuse),'evidence_sha256':sha}
    download=logs/'download_log.jsonl'
    if download.is_file():
        sha=digest(download)
        for line in download.read_text(encoding='utf-8-sig').splitlines():
            r=json.loads(line)
            if r.get('ok') and r.get('sha256'):
                links[r['sha256']]={'url':r.get('final_url') or r['url'],'method':'DOWNLOAD_LOG_SHA256','evidence_file':str(download),'evidence_sha256':sha}
    unresolved=[];resolved=0
    with db.transaction() as c:
        for row in c.rows('SELECT * FROM source_register'):
            raw=json.loads(row['payload']);link=links.get(row['sha256'])
            if link:
                raw['resolved_url']=link['url'];raw['source_resolution_evidence']=link
                c.execute('UPDATE source_register SET official_url=?,payload=? WHERE source_id=?',(link['url'],dumps(raw),row['source_id']));resolved+=1
            else:unresolved.append({'source_id':row['source_id'],'path':row['local_path'],'status':row['verification_status'],'reason':'来源 URL 缺失；保留原状态，不编造 URL 或授权'})
    return {'resolved_url_count':resolved,'unresolved':unresolved,'official_content_unchanged':True}
