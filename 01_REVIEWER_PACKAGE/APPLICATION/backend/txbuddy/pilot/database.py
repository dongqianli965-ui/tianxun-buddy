from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import hashlib, json, os, sqlite3, re


def dumps(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,default=str)


class PilotError(ValueError):
    def __init__(self,code,message):
        self.code=code; self.message=message; super().__init__(f'{code}: {message}')


class Connection:
    def __init__(self,conn,pg): self.raw=conn; self.pg=pg
    def execute(self,sql,params=()):
        cur=self.raw.cursor(); cur.execute(sql.replace('?', '%s') if self.pg else sql,params); return cur
    def rows(self,sql,params=()):
        cur=self.execute(sql,params); names=[x[0] for x in cur.description]
        return [dict(zip(names,r)) for r in cur.fetchall()]
    def many(self,sql,rows):
        cur=self.raw.cursor();cur.executemany(sql.replace('?', '%s') if self.pg else sql,rows);return cur
    def one(self,sql,params=()):
        rows=self.rows(sql,params); return rows[0] if rows else None


class PilotDatabase:
    def __init__(self,location):
        self.location=str(location); self.pg=self.location.startswith(('postgresql://','postgres://')) or bool(re.search(r'(?:^|\s)(?:dbname|host|port)=',self.location))
        if self.pg:
            import psycopg2
            name=psycopg2.extensions.parse_dsn(self.location).get('dbname','')
            if not any(x in name.lower() for x in ('sample','test','baseline','v424')):
                raise PilotError('DB_ISOLATION_REQUIRED','样本连接只能指向独立 sample/test 数据库')
        else:
            p=Path(self.location).resolve()
            if p.name=='txbuddy-dev.sqlite3': raise PilotError('DB_ISOLATION_REQUIRED','禁止导入既有开发库')
            p.parent.mkdir(parents=True,exist_ok=True)
    @contextmanager
    def transaction(self):
        if self.pg:
            import psycopg2
            raw=psycopg2.connect(self.location,connect_timeout=10)
        else:
            raw=sqlite3.connect(self.location,timeout=30); raw.execute('PRAGMA foreign_keys=ON'); raw.execute('BEGIN IMMEDIATE')
        try:
            yield Connection(raw,self.pg)
            raw.commit()
        except BaseException:
            raw.rollback(); raise
        finally: raw.close()
    def migrate(self):
        sql=Path(__file__).resolve().parents[3]/'sql/004_sample_pilot.sql'
        text=sql.read_text(encoding='utf-8'); digest=hashlib.sha256(text.encode()).hexdigest()
        with self.transaction() as c:
            c.execute('CREATE TABLE IF NOT EXISTS pilot_migration (version TEXT PRIMARY KEY,sha256 TEXT NOT NULL)')
            row=c.one('SELECT * FROM pilot_migration WHERE version=?',('004',))
            if row:
                if row['sha256']!=digest: raise PilotError('MIGRATION_DRIFT','004 迁移校验和不一致')
                return
            for statement in text.split(';'):
                if statement.strip(): c.execute(statement)
            c.execute('INSERT INTO pilot_migration VALUES (?,?)',('004',digest))
    def counts(self):
        tables=('source_register','document_inventory','project_sample','project_document','extracted_field','material_price','material_price_correction','quota_item','fee_rate_matrix','basis_pack','data_import_job','data_import_audit')
        with self.transaction() as c:
            return {t:c.one('SELECT COUNT(*) AS n FROM '+t)['n'] for t in tables}
