"""Run the V4.2.5 unified-storage experiment in fresh test databases.

No live connection setting is changed. Credentials and database/file backups
remain in the private directory outside the source tree.
"""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'backend'))
import psycopg2
from psycopg2 import sql
from txbuddy.governance.migration import (
    MigrationSafetyError, collect_sources, connect_target, copy_assets, file_sha,
    grant_test_role, initialize_target, load_target, read_postgres, require_test_target,
    snapshot_manifest, target_manifest, verify_target,
)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str)+'\n', encoding='utf-8')


def restrict_private_directory(path):
    path=Path(path).resolve(strict=True)
    if os.name=='nt':
        principal=subprocess.check_output(['whoami.exe'],text=True,timeout=10).strip()
        completed=subprocess.run(['icacls.exe',str(path),'/inheritance:r','/grant:r',
            principal+':(OI)(CI)F','SYSTEM:(OI)(CI)F','/T','/C'],capture_output=True,timeout=60)
        if completed.returncode:
            raise RuntimeError('Private backup directory ACL could not be restricted')
        return dict(passed=True,policy='inheritance removed; current Windows user and SYSTEM only')
    path.chmod(0o700)
    return dict(passed=True,policy='private directory mode 0700')


def new_database(source_dsn, name):
    params = psycopg2.extensions.parse_dsn(source_dsn)
    target = psycopg2.extensions.make_dsn(**{**params, 'dbname':name})
    require_test_target(target, source_dsn=source_dsn)
    # This user-space cluster has no postgres maintenance database. CREATE
    # DATABASE is cluster DDL; connecting to the read-only source location for
    # this command does not alter any source schema or business row.
    admin = source_dsn
    with closing(psycopg2.connect(admin, connect_timeout=10)) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    return target


def execute(dsn, statement, params=(), fetch=False):
    with closing(connect_target(dsn)) as conn, conn:
        with conn.cursor() as cur:
            cur.execute(statement, params)
            return cur.fetchall() if fetch else cur.rowcount


def denied_sql(dsn, statement, params=(), expected='23503'):
    try:
        execute(dsn, statement, params)
    except psycopg2.Error as exc:
        if exc.pgcode == expected:
            return True
        raise
    return False


def relation_checks(dsn):
    queries = {
        'job_project_scope':'SELECT count(*) FROM runtime.job j LEFT JOIN security.project_scope p ON p.project_id=j.project_id WHERE p.project_id IS NULL',
        'member_project_scope':'SELECT count(*) FROM security.acl_member m LEFT JOIN security.project_scope p USING(project_id) WHERE p.project_id IS NULL',
        'ocr_project_scope':'SELECT count(*) FROM review.ocr_document d LEFT JOIN security.project_scope p USING(project_id) WHERE p.project_id IS NULL',
        'document_job':'SELECT count(*) FROM runtime.job_document d LEFT JOIN runtime.job j USING(job_id) WHERE j.job_id IS NULL',
        'document_blob':'SELECT count(*) FROM runtime.job_document d LEFT JOIN storage.file_blob b ON b.sha256=d.file_sha256 WHERE b.sha256 IS NULL',
        'document_version_project_hash':'''SELECT count(*) FROM runtime.job_document d JOIN runtime.job j USING(job_id)
            LEFT JOIN storage.file_version v ON v.doc_key=d.doc_key AND v.version::text=d.version
              AND v.sha256=d.file_sha256 AND v.project_id=j.project_id WHERE v.doc_key IS NULL''',
        'price_file_hash':'SELECT count(*) FROM corpus.material_price p JOIN corpus.document_inventory d USING(document_id) WHERE p.sha256<>d.sha256',
        'correction_source_hash':'''SELECT count(*) FROM corpus.material_price_correction c
            JOIN corpus.document_inventory d ON d.document_id=c.source_document_id WHERE c.sha256<>d.sha256''',
        'extracted_document':'SELECT count(*) FROM corpus.extracted_field e LEFT JOIN corpus.document_inventory d USING(document_id) WHERE d.document_id IS NULL',
        'ocr_page_document':'SELECT count(*) FROM review.ocr_page p LEFT JOIN review.ocr_document d USING(document_id) WHERE d.document_id IS NULL',
        'ocr_review_page':'SELECT count(*) FROM review.ocr_review_event e LEFT JOIN review.ocr_page p USING(page_id) WHERE p.page_id IS NULL',
    }
    orphan_counts = {k:execute(dsn, q, fetch=True)[0][0] for k,q in queries.items()}
    bad_fk = denied_sql(dsn, "INSERT INTO runtime.job_document(job_id,doc_key,version,file_sha256) VALUES(%s,%s,%s,%s)",
                        ('TEST_ONLY_MISSING_JOB','TEST_ONLY/missing','1','0'*64))
    duplicate = denied_sql(dsn, 'INSERT INTO corpus.material_price SELECT * FROM corpus.material_price LIMIT 1', expected='23505')
    constraints = execute(dsn, "SELECT contype,count(*) FROM pg_constraint WHERE connamespace IN "
        "(SELECT oid FROM pg_namespace WHERE nspname IN ('runtime','security','storage','review','corpus')) GROUP BY contype ORDER BY contype", fetch=True)
    return dict(passed=all(v==0 for v in orphan_counts.values()) and bad_fk and duplicate,
        orphan_counts=orphan_counts, foreign_key_violation_rejected=bad_fk, duplicate_key_rejected=duplicate,
        constraint_counts=dict(constraints))


def ocr_chain_check(dsn):
    """Reconstruct the original SQLite event hash contract from PostgreSQL types."""
    with closing(connect_target(dsn)) as conn:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM review.ocr_review_event ORDER BY page_id,version')
            columns=[x.name for x in cur.description]
            rows=[dict(zip(columns,row)) for row in cur.fetchall()]
    previous={};versions={};failures=[]
    for record in rows:
        page=record['page_id'];digest=record.pop('event_sha256')
        record['reviewed_at']=record['reviewed_at'].astimezone(timezone.utc).isoformat()
        record['structured_fields_json']=json.dumps(record['structured_fields_json'],ensure_ascii=False,
            sort_keys=True,separators=(',',':'),allow_nan=False)
        version=versions.get(page,0)+1
        expected=hashlib.sha256(json.dumps(record,ensure_ascii=False,sort_keys=True,
            separators=(',',':'),allow_nan=False).encode()).hexdigest()
        if record['version']!=version or record['previous_event_sha256']!=previous.get(page,'') or digest!=expected:
            failures.append(dict(page_id=page,version=version))
        versions[page]=version;previous[page]=digest
    return dict(passed=not failures,events_checked=len(rows),page_chains_checked=len(previous),failures=failures,
                empty_history_means_no_human_review=len(rows)==0)


def concurrency_check(dsn, workers=32):
    key = 'TEST_ONLY_V425_CONCURRENT_'+uuid.uuid4().hex
    fingerprint = uuid.uuid4().hex
    def claim(_):
        return bool(execute(dsn, 'INSERT INTO runtime.idem(scope_key,fingerprint,state,response,tenant,actor) '
            "VALUES(%s,%s,'IN_PROGRESS',NULL,'TEST_ONLY','TEST_ONLY') ON CONFLICT DO NOTHING RETURNING scope_key",
            (key, fingerprint), fetch=True))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(claim, range(workers)))
    winner_count = sum(results)
    rows = execute(dsn, 'SELECT fingerprint,state FROM runtime.idem WHERE scope_key=%s', (key,), fetch=True)
    execute(dsn, 'DELETE FROM runtime.idem WHERE scope_key=%s', (key,))
    return dict(passed=winner_count==1 and len(rows)==1 and rows[0]==(fingerprint,'IN_PROGRESS'),
                parallel_claims=workers, winners=winner_count, duplicates=workers-winner_count,
                test_fixture_removed=True)


def rollback_check(dsn, data, failed_dsn):
    project = execute(dsn, 'SELECT project_id FROM runtime.job ORDER BY job_id LIMIT 1', fetch=True)[0][0]
    jobid = 'TEST_ONLY_V425_ROLLBACK_'+uuid.uuid4().hex
    rejected = False
    try:
        with closing(connect_target(dsn)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute('INSERT INTO runtime.job(job_id,project_id,name) VALUES(%s,%s,%s)',
                            (jobid, project, 'TEST_ONLY_ROLLBACK'))
                cur.execute('INSERT INTO runtime.job_document(job_id,doc_key,version,file_sha256) VALUES(%s,%s,%s,%s)',
                            (jobid, 'TEST_ONLY/MISSING_BLOB','1','0'*64))
    except psycopg2.Error as exc:
        if exc.pgcode != '23503':
            raise
        rejected = True
    remaining = execute(dsn, 'SELECT count(*) FROM runtime.job WHERE job_id=%s', (jobid,), fetch=True)[0][0]
    initialize_target(failed_dsn, ROOT/'sql')
    fault = False
    try:
        load_target(failed_dsn, data, fail_after_table='corpus.material_price')
    except RuntimeError as exc:
        fault = str(exc).startswith('TEST_ONLY_MIGRATION_FAILURE_AFTER')
    counts = target_manifest(failed_dsn, data)['tables']
    nonempty = [r['table'] for r in counts if r['count']]
    return dict(passed=rejected and remaining==0 and fault and not nonempty,
                relation_failure_rolled_back_job=remaining==0, forced_batch_failure_observed=fault,
                failed_batch_nonempty_tables=nonempty, failure_database=require_test_target(failed_dsn))


def permission_checks(dsn, role, data):
    members = data['security.acl_member']
    owners = [dict(zip(members.columns,r)) for r in members.rows]
    owner = next(r['subject'] for r in owners if r['role']=='OWNER')
    tables = ('runtime.job','runtime.job_document','runtime.job_record','storage.file_version',
              'review.ocr_document','review.ocr_page','review.ocr_review_event')
    def visible(subject):
        with closing(connect_target(dsn)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                cur.execute("SELECT set_config('app.subject',%s,true)", (subject,))
                counts={}
                for table in tables:
                    cur.execute(sql.SQL('SELECT count(*) FROM {}.{}').format(*map(sql.Identifier,table.split('.'))))
                    counts[table]=cur.fetchone()[0]
                cur.execute("UPDATE runtime.job SET name='TEST_ONLY_UNAUTHORIZED' WHERE project_id=%s", ('NOT_A_MEMBER',))
                counts['cross_project_updated']=cur.rowcount
                return counts
    own = visible(owner)
    other = visible('TEST_ONLY_NONMEMBER_'+uuid.uuid4().hex)
    no_subject = visible('')
    blocked = {}
    for label,statement in {
        'password_hash_denied':'SELECT password_hash FROM security.user_account',
        'revocations_denied':'SELECT * FROM security.auth_revoked',
        'acl_write_denied':"UPDATE security.acl_member SET role='OWNER'",
        'raw_catalog_snapshot_denied':'SELECT payload_json FROM review.governance_catalog_snapshot',
        'raw_catalog_current_denied':'SELECT * FROM review.governance_catalog_current',
        'cross_project_insert_denied':"INSERT INTO runtime.job(job_id,project_id) VALUES('TEST_ONLY_DENIED','NOT_A_MEMBER')",
    }.items():
        try:
            with closing(connect_target(dsn)) as conn, conn:
                with conn.cursor() as cur:
                    cur.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                    cur.execute("SELECT set_config('app.subject',%s,true)", ('TEST_ONLY_NONMEMBER',))
                    cur.execute(statement)
        except psycopg2.Error as exc:
            if exc.pgcode!='42501':
                raise
            blocked[label]=True
        else:
            blocked[label]=False
    # Also prove defense in depth if a future GRANT ALL TABLES is too broad.
    execute(dsn,sql.SQL('GRANT SELECT ON review.governance_catalog_snapshot,review.governance_catalog_current TO {}').format(sql.Identifier(role)))
    try:
        with closing(connect_target(dsn)) as conn, conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                cur.execute("SELECT set_config('app.subject',%s,true)",(owner,))
                cur.execute('SELECT count(*) FROM review.governance_catalog_snapshot')
                snapshot_rows=cur.fetchone()[0]
                cur.execute('SELECT count(*) FROM review.governance_catalog_current')
                current_rows=cur.fetchone()[0]
        blocked['raw_catalog_hidden_after_broad_grant']=snapshot_rows==0 and current_rows==0
    finally:
        execute(dsn,sql.SQL('REVOKE SELECT ON review.governance_catalog_snapshot,review.governance_catalog_current FROM {}').format(sql.Identifier(role)))
    return dict(passed=own['runtime.job']>0 and all(v==0 for v in other.values())
                and all(v==0 for v in no_subject.values()) and all(blocked.values()),
                owner_visible=own, nonmember_visible=other, unset_identity_visible=no_subject, **blocked,
                role='NOLOGIN/NOSUPERUSER/NOBYPASSRLS',
                identity_contract='app.subject is set by a trusted authenticated service; database access is not given to end users')


def pg_command_env(dsn):
    params = psycopg2.extensions.parse_dsn(dsn)
    env = os.environ.copy()
    mapping = {'host':'PGHOST','hostaddr':'PGHOSTADDR','port':'PGPORT','user':'PGUSER','password':'PGPASSWORD'}
    for key,name in mapping.items():
        if key in params:
            env[name]=params[key]
    return env


def run_pg(program, dsn, args):
    completed = subprocess.run([str(program), *map(str,args)], env=pg_command_env(dsn),
        capture_output=True, timeout=180, text=True, encoding='utf-8', errors='replace')
    if completed.returncode:
        # The report must not contain DSNs, SQL parameter values or credentials.
        raise RuntimeError(program.name+' failed with exit '+str(completed.returncode))


def inventory_catalog(data, dsn, development_dsn=None):
    actual={}
    for schema in ('runtime','security','storage','review','corpus','migration_control'):
        actual.update(read_postgres(dsn, schema, schema))
    rows=[]
    for key in sorted(actual):
        table=actual[key]
        source=data.get(key)
        rows.append(dict(table=key, current_storage=source.source if source else 'V425 PoC新增表',
            current_count=len(source.rows) if source else 0, target_storage='统一PostgreSQL/'+key,
            target_count=len(table.rows), source_definition=source.definition if source else None,
            target_definition=table.definition,
            acl=('仅迁移/运维角色' if key.startswith('migration_control.') or key in
                ('security.user_account','security.project_scope','security.acl_project','security.acl_member','security.auth_revoked',
                 'runtime.audit_log','runtime.idem','storage.file_blob','runtime.schema_version',
                 'review.governance_catalog_snapshot','review.governance_catalog_current') else
                '受认证服务按资料授权范围只读；导入另授写权' if key.startswith('corpus.') else '项目成员RLS；写入仅OWNER/EDITOR；OCR原文追加不可改'),
            transaction='同一批次单事务、主外键约束、校验后提交',
            migration='逐列保留业务键；JSONB/时间/路径规范化并核对内容哈希',
            rollback='切换前不改源库；失败整批回滚；独立库恢复并核验后显式切连接',
            poc_migrated=bool(source)))
    if development_dsn:
        for schema in ('public','txbuddy_core'):
            for key,table in read_postgres(development_dsn,schema,'legacy_'+schema).items():
                if schema=='public' and table.name not in ('schema_migration',):
                    target='runtime.'+table.name+'（使用真实SQLite运行数据，原PG空表不重复导入）'
                else:
                    target=('migration_control.legacy_core_ledger' if table.name=='schema_migration' else
                            'domain.'+table.name+'（后续业务键映射阶段，未实现）')
                rows.append(dict(table=key,current_storage='原开发PostgreSQL/'+schema+'.'+table.name,
                    current_count=len(table.rows),target_storage=target,target_count=None,
                    source_definition=table.definition,target_definition=None,
                    acl='按tenant_id/project_id；迁移前须补真实身份/租户映射，当前未接入API',
                    transaction='实体/依据包/计算/证据同事务；当前空表仅盘点',
                    migration='先保持001/002独立；不能将代理键与003/004同名表直接拼接',
                    rollback='保留原库及001/002迁移账本；新版本迁移不得改历史脚本',poc_migrated=False))
    return rows


def markdown_plan(result, catalog):
    lines=[
        '# V4.2.5 统一数据库架构规划', '',
        '日期：'+result['generated_at']+'。生产验收仍为 False；E级精度 BLOCKED。', '',
        '本轮完成独立测试库的逐列迁移 PoC，没有切换现有样本运行环境。PoC 通过不等于业务门面、身份系统和 FileStore 已使用 PostgreSQL。', '',
        '## 当前结构和目标', '',
        '当前 PostgreSQL 样本库使用 004：资料、来源、价格、勘误和项目登记。业务门面 EvidenceStore 固定 SQLite；认证 ACL/吊销独立 SQLite，用户目录为仓外 JSON；FileStore 元数据也为 SQLite，文件按 SHA256 保存。新增 OCR 使用另一独立 SQLite，原文和复核事件追加留存。', '',
        '另有 001/002 的领域核心模型（BIGINT 代理键）和 003 的运行模型（TEXT 业务键）。二者与 004 存在同名异义表，不能按名称合并。原开发 PostgreSQL 的业务表当前为空；它不能替代对真实运行 SQLite 的迁移验收。', '',
        '本轮实际发现：ACL成员表覆盖20个项目，但 owner-claim 登记仅1行，原因是现有 grant() 不执行 claim()。PoC 将已有 Job/OCR/FileStore/ACL 引用的项目键取并集建立 project_scope，记录每个键的发现来源；不补造 owner 或成员。成员、业务和OCR的外键指向 project_scope，原 owner-claim 表完整保留。', '',
        '```mermaid',
        'flowchart LR',
        '  API[认证后的 API] --> R[业务仓储接口]',
        '  R --> PG[(统一 PostgreSQL 测试目标)]',
        '  PG --- RT[runtime：Job / 任务 / 证据 / 幂等]',
        '  PG --- SC[security：用户 / ACL / 吊销]',
        '  PG --- CP[corpus：资料 / 来源 / 价格 / 勘误]',
        '  PG --- RV[review：OCR原文 / 页 / 追加复核事件]',
        '  PG --- ST[storage：文件索引 / 版本]',
        '  PG --- MC[migration_control：版本 / 批次]',
        '  ST --> FS[内容寻址 FileStore：SHA256 对象]',
        '  RV --> IMG[OCR 派生页图：SHA256 对象]',
        '  CORE[001/002 领域模型] -. 后续显式业务键映射 .-> PG',
        '```', '',
        '目标阶段一以现有 API 业务键为准，保持 runtime/corpus/review 分域，用户、ACL、文件版本和迁移账本集中到同一实例。在应用内一个业务操作使用一条事务连接；RLS 是第二道权限边界。文件实体和页图保留在内容寻址对象仓，数据库只保存哈希、版本与对象键。文件落盘成功和数据库登记之间需 outbox/补偿与孤儿对象回收，不能声称文件系统与 SQL 已有分布式事务。', '',
        '目标阶段二才处理租户模型、001/002 代理键与现有业务键映射：新增不可变映射表，双读对账后逐项替换，不双写正式造价数据。当前没有相应租户/用户映射和完整 API 兼容证明，此阶段未实施。', '',
        '## 逐表目录', '',
        '主键、外键和唯一约束取自实际数据库目录。完整列类型、当前及目标约束见 reports/v425/DATABASE_TABLE_CATALOG.json。下表每行同时给出事务、ACL、迁移及回滚规则；“后续”目标尚未实施。', '',
        '| 表 | 当前位置 / 行数 | 目标位置 | 主键、外键、唯一约束 | 事务 | ACL | 迁移 / 回滚 |',
        '|---|---|---|---|---|---|---|',
    ]
    for item in catalog:
        definition=item['target_definition'] or item['source_definition'] or {}
        constraints='；'.join(c['definition'] for c in definition.get('constraints',[]) if
            any(t in c['definition'] for t in ('PRIMARY KEY','FOREIGN KEY','UNIQUE')))
        if not constraints:
            constraints='见逐列目录（源表未声明的关系不会被冒充为已存在）'
        current=item['current_storage'].replace('|','/')+' / '+str(item['current_count'])
        cells=[item['table'], current, item['target_storage'], constraints,
               item['transaction'], item['acl'], item['migration']+'；'+item['rollback']]
        lines.append('| '+' | '.join(str(c).replace('|','/').replace('\n',' ') for c in cells)+' |')
    lines.extend(['', '## PoC 验收', '',
        '独立数据库：`'+result['database']+'`。恢复库和故障注入库分别独立创建。原运行库及连接设置保持不变。', '',
        '| 检查 | 实测结果 |', '|---|---|'])
    for key,value in result['checks'].items():
        lines.append('| '+key+' | '+('PASS' if value.get('passed') else 'FAIL')+' |')
    lines.extend(['',
        '规范化内容哈希逐表包含全部列和全部行；不抽样，不仅比较行数。JSON 文本转 JSONB 时忽略空白和键顺序；时间统一 UTC；布尔值统一 0/1；文件路径变为相同 SHA256 对象键。原文件字节哈希另行逐件校验。原 SQLite 主键、业务编号、已发布 B_REFERENCE 清单身份、10989条材料原记录与3条勘误均保留。', '',
        '权限测试使用无登录、非超级用户、无 BYPASSRLS 角色，分别验证所属项目可见、跨项目/无身份不可见、跨项目写入拒绝及口令哈希/吊销/ACL管理不可读写。app.subject 只能由可信服务在已验证 JWT 的事务中设置，不将数据库口令发给终端用户，也不把可修改 GUC 当作身份认证。当前本机 PG 的 trust 开发认证不是生产部署条件。', '',
        '新增 OCR 原文/页/事件使用 UPDATE/DELETE 拒绝触发器，专业签认仍须应用端真实人员及资质核验；PoC 不生成复核或专业签认。事件哈希和页图哈希随迁移保留。所有 TEST_ONLY 并发、回滚、权限夹具只写入测试库并回滚或删除。', '',
        'governance_catalog_snapshot 的一条 payload_json 包含多个项目，不能把整行 JSON 直接作为业务 API 返回。PoC 对业务角色同时拒绝原始 snapshot/current 两表的 SELECT；两表另启用无公共策略的 RLS，实测即使误授 SELECT，普通业务角色仍看不到任何完整快照。它们仅供迁移和管理审计。当前 SQLite API 已在返回前逐条按 project_id/ACL 过滤。正式 PostgreSQL 适配还须将目录展开为带 project_id 的投影表或使用经认证的逐项目查询服务，并保留按项目过滤后再分页的行为。不能通过授予原始快照全表读取权来接通前端。', '',
        '## 迁移与回滚执行方案', '',
        '1. 在维护窗口暂停新写入，备份每个 SQLite（通过在线备份 API）和 PG 一致性快照，保存用户目录与对象清单。当前 PoC 用只读一致性事务取数，并在结尾重读对比；跨多个源库的全局同一时刻快照尚未实现。',
        '2. 新建目标库并固定迁移脚本校验和。导入只读快照，先主体/ACL和对象索引，再业务、语料和 OCR。未映射新表、无效 JSON、重复键内容不一致、孤儿引用或哈希差异即停止并整批回滚。',
        '3. 对每表全量行数和内容哈希、关系、权限、幂等、并发、失败回滚、备份恢复验收。对象仓和 OCR 页图必须实际复制且逐件校验。原库在此阶段保持可用。',
        '4. 实现 EvidenceStore 的 PostgreSQL 依赖注入、认证/ACL/吊销及 FileStore 元数据适配器、OCR PostgreSQL 适配器、事务连接传播和对象 outbox。随后在同一份隔离副本执行所有 API 与 Playwright 用例，覆盖重启、跨项目和失败补偿。此阶段未完成，当前禁止切换。',
        '5. 只有适配器验收、真实用户身份迁移、账号权限收敛、备份恢复演练和维护窗口获准后，才提出一次显式切换。切换前失败可直接保留旧连接；切换后出现新写入时不能直接退回陈旧 SQLite，须冻结写入、对账并从已验证的备份/增量恢复到新库后回切。禁止用破坏性 downgrade 删除原数据。', '',
        '## 是否具备切换条件', '',
        '**尚不具备。** PoC 证明的是该批真实样本数据在独立目标模型上的迁移和恢复，不是生产迁移完成。仍缺全部运行适配器、跨源一致性冻结/增量方案、生产认证、完整统一后端 API/浏览器回归和业务负责人验收。现有本地样本环境继续使用原连接。', '',
        '复现入口：`python scripts/v425_migration_poc.py --config <仓外私有config>`；创建的库名强制含 v425_test。SOURCE/target 相同会拒绝，不接受把 sample 或原开发库作为写入目标。详细结果与私有备份位置摘要见 DATABASE_POC_RESULT.json。', ''])
    return '\n'.join(lines)


def run(config_path, out, private, pg_bin):
    config_path=Path(config_path).resolve(strict=True)
    config=json.loads(config_path.read_text(encoding='utf-8-sig'))
    out=Path(out);out.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'_'+uuid.uuid4().hex[:6]
    private=Path(private)/('migration_'+stamp);private.mkdir(parents=True)
    private_protection=restrict_private_directory(private)
    # Keep password hashes, backups and source row snapshots outside the deliverable.
    source_files=[Path(config['TXBUDDY_RUNTIME_ROOT'])/name for name in
                  ('business.sqlite3','auth.sqlite3','blobs/meta.sqlite3','governance.sqlite3')]
    source_files.append(Path(config['TXBUDDY_USERS_FILE']))
    source_files.append(config_path)
    file_before={str(p):file_sha(p) for p in source_files if p.exists()}
    data=collect_sources(config)
    source_manifest=snapshot_manifest(data)
    write_json(private/'SOURCE_MANIFEST.json',source_manifest)
    print(json.dumps({'stage':'source_snapshot','tables':source_manifest['table_count'],'rows':source_manifest['row_count']}),flush=True)
    base='txbuddy_v425_test_'+stamp.lower()
    dsn=new_database(config['TXBUDDY_SAMPLE_DSN'],base)
    restored=new_database(config['TXBUDDY_SAMPLE_DSN'],base+'_restore')
    failed=new_database(config['TXBUDDY_SAMPLE_DSN'],base+'_rollback')
    role='txbuddy_v425_test_role_'+uuid.uuid4().hex[:16]
    execute(dsn,sql.SQL('CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS').format(sql.Identifier(role)))
    initialize_target(dsn,ROOT/'sql')
    assets=copy_assets(data,private/'objects')
    print(json.dumps({'stage':'assets_copied','references':len(assets)}),flush=True)
    load_target(dsn,data)
    comparison=verify_target(dsn,data)
    before=target_manifest(dsn,data)
    initialize_again=initialize_target(dsn,ROOT/'sql')
    load_target(dsn,data)
    after=target_manifest(dsn,data)
    checks={
        'data_count':dict(passed=all(x['count_equal'] for x in comparison['tables']),
                          table_count=len(comparison['tables']),rows=source_manifest['row_count']),
        'hash':dict(passed=comparison['passed'],tables=comparison['tables'],
                    assets_checked=len(assets),asset_bytes=sum(a['bytes'] for a in assets)),
        'relationships':relation_checks(dsn),
        'ocr_event_chain':ocr_chain_check(dsn),
        'idempotency':dict(passed=before==after and not initialize_again,
                          schema_reapplied=initialize_again,content_unchanged=before==after),
        'concurrent_writes':concurrency_check(dsn),
        'transaction_rollback':rollback_check(dsn,data,failed),
    }
    grant_test_role(dsn,role)
    checks['permission_isolation']=permission_checks(dsn,role,data)
    print(json.dumps({'stage':'migration_checks','passed':all(v['passed'] for v in checks.values())}),flush=True)
    dump=private/'unified.pgdump'
    run_pg(Path(pg_bin)/'pg_dump.exe',dsn,['--format=custom','--no-owner','--file',dump,'--dbname',base])
    run_pg(Path(pg_bin)/'pg_restore.exe',restored,['--exit-on-error','--no-owner','--dbname',base+'_restore',dump])
    restore_comparison=verify_target(restored,data)
    shutil.copytree(private/'objects',private/'restored_objects')
    recovered_assets=all(file_sha(private/'restored_objects'/a['key'])==a['sha256'] for a in assets)
    restored_permissions=permission_checks(restored,role,data)
    checks['backup_restore']=dict(passed=restore_comparison['passed'] and recovered_assets and restored_permissions['passed'],
        database=base+'_restore',table_count=len(restore_comparison['tables']),
        backup_sha256=file_sha(dump),backup_bytes=dump.stat().st_size,objects_restored=len(assets),
        objects_hash_equal=recovered_assets,permissions_restored=restored_permissions['passed'])
    restored_chain=ocr_chain_check(restored)
    checks['backup_restore']['ocr_event_chain_restored']=restored_chain['passed']
    checks['backup_restore']['passed']=checks['backup_restore']['passed'] and restored_chain['passed']
    checks['private_backup_protection']=private_protection
    final=verify_target(dsn,data)
    source_after=snapshot_manifest(collect_sources(config))
    files_unchanged=all(file_sha(p)==digest for p,digest in file_before.items())
    checks['source_unchanged']=dict(passed=source_manifest['batch_sha256']==source_after['batch_sha256'] and files_unchanged,
        logical_hash_equal=source_manifest['batch_sha256']==source_after['batch_sha256'],source_files_unchanged=files_unchanged,
        checked_sqlite_and_user_files=len(file_before))
    # Temporary roles have no login. Remove grants and the role from both test DBs.
    for location in (dsn,restored):
        execute(location,sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
    execute(dsn,sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
    development_file=ROOT/'backend/.pg-dsn'
    catalog=inventory_catalog(data,dsn,development_file.read_text().strip() if development_file.exists() else None)
    result=dict(version='4.2.5',generated_at=datetime.now(timezone.utc).isoformat(),
        database=base,scope='ISOLATED_TEST_DATABASE_ONLY',source_runtime=Path(config['TXBUDDY_RUNTIME_ROOT']).as_posix(),
        migration_poc_passed=all(v['passed'] for v in checks.values()) and final['passed'],
        production_switch_ready=False,production_accepted=False,accuracy_verification='BLOCKED',
        live_connection_changed=False,checks=checks,private_evidence_directory=str(private),
        backup_file=str(dump),source_batch_sha256=source_manifest['batch_sha256'],
        copied_assets=assets,temporary_role_removed=True,
        unique_copied_objects=len({a['key'] for a in assets}),
        limitations=['现有EvidenceStore/认证/ACL/FileStore/OCR运行适配器尚未统一切换',
            '001/002领域代理键与003/004业务键仍需显式映射',
            'PoC逐源只读事务加末尾重核，未建立维护窗口全局冻结或增量迁移',
            '本机trust认证仅用于开发；不满足生产账号/网络/密钥收敛要求'])
    previous=[out/name for name in ('DATABASE_POC_RESULT.json','DATABASE_INVENTORY.json','DATABASE_TABLE_CATALOG.json')]
    if any(p.exists() for p in previous):
        archive=out/'database_history'/stamp;archive.mkdir(parents=True)
        for path in previous+[ROOT/'DATABASE_UNIFICATION_PLAN_V425.md']:
            if path.exists():
                shutil.copyfile(path,archive/path.name)
    write_json(out/'DATABASE_POC_RESULT.json',result)
    write_json(out/'DATABASE_INVENTORY.json',source_manifest)
    write_json(out/'DATABASE_TABLE_CATALOG.json',catalog)
    (ROOT/'DATABASE_UNIFICATION_PLAN_V425.md').write_text(markdown_plan(result,catalog),encoding='utf-8')
    print(json.dumps({k:result[k] for k in ('database','migration_poc_passed','production_switch_ready','live_connection_changed')},ensure_ascii=False))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    preferred=ROOT.parent/'_CODEX_V425/local-pilot/config.private.json'
    parser.add_argument('--config',type=Path,default=preferred if preferred.exists() else ROOT.parent/'_CODEX_V424/local-pilot/config.private.json')
    parser.add_argument('--out',type=Path,default=ROOT/'reports/v425')
    parser.add_argument('--private',type=Path,default=ROOT.parent/'_CODEX_V425')
    parser.add_argument('--pg-bin',type=Path,default=Path(r'F:\ZHU-FISH\PG_LOCAL\pgsql\bin'))
    args=parser.parse_args()
    result=run(args.config,args.out,args.private,args.pg_bin)
    raise SystemExit(0 if result['migration_poc_passed'] else 2)
