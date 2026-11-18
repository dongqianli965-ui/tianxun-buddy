"""PostgreSQL 集成测试：本机无 psql/服务，默认跳过；具备实例时执行真实迁移与事务测试。

运行方式（有 PG 时）：
    set TX_PG_DSN=postgresql://user:pass@127.0.0.1:5432/txbuddy_test
    set TX_PG_PSQL=psql 路径
    pytest tests/test_pg_integration.py -q
"""
import os
import shutil
import subprocess
import pytest
import uuid

PSQL = os.environ.get("TX_PG_PSQL") or shutil.which("psql")
DSN = os.environ.get("TX_PG_DSN")
PSQL_ENV = os.environ.copy()
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL_FILES = [os.path.join(HERE, "..", "sql", "001_core_schema_postgresql.sql"),
             os.path.join(HERE, "..", "sql", "002_workflow_and_idem_postgresql.sql")]

pytestmark = pytest.mark.skipif(not (PSQL and DSN),
                                reason="未检测到 PostgreSQL（psql/TX_PG_DSN），迁移未实测")


@pytest.fixture(scope='module',autouse=True)
def isolated_legacy_schema():
    import psycopg2
    from psycopg2 import sql
    name=psycopg2.extensions.parse_dsn(DSN).get('dbname','')
    if not any(marker in name for marker in ('test','regression','baseline')):
        pytest.fail('旧 psql 验证仅允许使用独立测试数据库')
    schema='txbuddy_legacy_'+uuid.uuid4().hex[:12]
    conn=psycopg2.connect(DSN);conn.autocommit=True
    with conn.cursor() as c:c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    PSQL_ENV['PGOPTIONS']='-c search_path='+schema
    try:yield
    finally:
        with conn.cursor() as c:c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        conn.close()


def _run(sql_path):
    r = subprocess.run([PSQL, "-d", DSN, "-v", "ON_ERROR_STOP=1", "-f", sql_path],
                       capture_output=True, text=True, env=PSQL_ENV)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_migrations_apply():
    for f in SQL_FILES:
        _run(f)


def test_unique_constraint_and_transaction_rollback():
    _run(SQL_FILES[1])
    def q(sql):
        return subprocess.run([PSQL, "-d", DSN, "-At", "-c", sql], capture_output=True, text=True, env=PSQL_ENV)
    q("BEGIN; INSERT INTO job(job_id,project_id,name,business_line,basis_pack,status,created_by) "
      "VALUES('j1','p','n','L1','{}','CREATED','t'); ROLLBACK;")
    out = q("SELECT count(*) FROM job WHERE job_id='j1';").stdout.strip()
    assert out == "0", "事务回滚后不应残留"
    dup = q("INSERT INTO idem(scope_key,fingerprint,response) VALUES('k','f','{}'); "
            "INSERT INTO idem(scope_key,fingerprint,response) VALUES('k','f','{}');")
    assert dup.returncode != 0, "幂等键主键约束应拒绝重复"
