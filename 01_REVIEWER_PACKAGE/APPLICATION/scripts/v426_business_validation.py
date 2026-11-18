"""Read-only verification of actual PostgreSQL sample and B_REFERENCE workbooks."""
import hashlib, json, sys
from pathlib import Path
from decimal import Decimal
import psycopg2
from openpyxl import load_workbook
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from txbuddy.pilot.boq import workbook_boq
corpus=ROOT.parent/'REAL_DATA_CORPUS/HN_L1_V1_20261116'
reports=ROOT/'reports/v426'; reports.mkdir(parents=True,exist_ok=True)
config=ROOT.parent/'_CODEX_V425/local-pilot/config.private.json'
dsn=json.loads(config.read_text(encoding='utf-8'))['TXBUDDY_SAMPLE_DSN']
connection=psycopg2.connect(dsn,connect_timeout=10)
connection.set_session(readonly=True,isolation_level='REPEATABLE READ')
try:
    c=connection.cursor()
    c.execute('SELECT period,COUNT(*) FROM material_price GROUP BY period ORDER BY period'); periods=dict(c.fetchall())
    c.execute('SELECT count(*) FROM material_price_correction'); corrections=c.fetchone()[0]
    c.execute("SELECT count(*) FROM material_price p LEFT JOIN document_inventory d ON p.document_id=d.document_id LEFT JOIN source_register s ON d.source_id=s.source_id WHERE d.sha256 IS DISTINCT FROM p.sha256 OR s.official_url IS NULL OR s.official_url='' "); bad=c.fetchone()[0]
    c.execute("SELECT material_name,spec_model,unit,period,count(*) FROM material_price WHERE scope_status<>'ERRATA_FOR_Q2' GROUP BY material_name,spec_model,unit,period HAVING count(*)>1 ORDER BY material_name,spec_model LIMIT 1"); duplicate=c.fetchone()
    c.execute('SELECT p.record_id,p.price_incl_tax,p.price_excl_tax,x.price_incl_tax,x.price_excl_tax,x.source_page,p.sha256,x.sha256 FROM material_price p JOIN material_price_correction x ON p.record_id=x.record_id ORDER BY p.record_id'); errata=c.fetchall()
    c.execute('SELECT region,count(*) FROM material_price GROUP BY region'); regions=dict(c.fetchall())
    c.execute('SELECT DISTINCT d.local_path,d.sha256 FROM material_price p JOIN document_inventory d ON p.document_id=d.document_id'); source_files=c.fetchall()
finally:
    connection.rollback();connection.close()
source_checks=[{'path':path,'sha256':sha,'matches':hashlib.sha256((corpus/path).read_bytes()).hexdigest()==sha} for path,sha in source_files]
price={'period_counts':periods,'total':sum(periods.values()),'corrections':corrections,'unverified_source_count':bad,'source_files':source_checks,'regions':regions,'duplicate_exact_key':duplicate,'errata':errata,'cross_region_positive':'BLOCKED: 只有一个实际地域','current_formal_price':'BLOCKED: 缺少本期依据和专业签认'}
assert periods=={'2026-09':939,'2026-Q2':5026,'2026-Q3':5024} and corrections==3 and bad==0 and all(x['matches'] for x in source_checks)
expected=[('ZZ-20262-1782','492.34','435.70'),('ZZ-20262-1783','470.95','416.77'),('ZZ-20262-1784','439.83','389.23')]
assert all(row[0]==item[0] and Decimal(row[3])==Decimal(item[1]) and Decimal(row[4])==Decimal(item[2]) and row[5]==137 for row,item in zip(errata,expected)) and len(errata)==3
folder=corpus/'05_REAL_PROJECTS/L1_BUDGET/L1_001/B_REFERENCE'; workbooks=[]
for suffix in ('土建_工程量清单.xlsx','安装_工程量清单.xlsx'):
    path=next(folder.glob('*'+suffix)); before=hashlib.sha256(path.read_bytes()).hexdigest(); result=workbook_boq(path)
    book=load_workbook(path,data_only=True); mismatches=[]
    try:
        for item in result['items']:
            s=book[item['sheet']]; r=item['row']
            for field,col in [('code',2),('name',3),('feature',4),('unit',6)]:
                if item[field]!=str(s.cell(r,col).value or '').strip(): mismatches.append([r,field])
            if Decimal(item['quantity'])!=Decimal(str(s.cell(r,7).value)): mismatches.append([r,'quantity'])
    finally: book.close()
    after=hashlib.sha256(path.read_bytes()).hexdigest()
    workbooks.append({'file':path.name,'sha256_before':before,'sha256_after':after,'rows':result['count'],'errors':result['errors'],'mismatches':mismatches,'skipped_rows':result['skipped_rows']})
assert sum(x['rows'] for x in workbooks)==148 and all(not x['mismatches'] and x['sha256_before']==x['sha256_after'] for x in workbooks)
boq={'workbooks':workbooks,'total_rows':148,'data_role':'B_REFERENCE','independently_calculated':False,'accuracy_verification':'BLOCKED'}
for name,data in [('MATERIAL_PRICE_VALIDATION',price),('BOQ_VALIDATION',boq)]:
    (reports/(name+'.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
(reports/'MATERIAL_PRICE_VALIDATION.md').write_text('# 材料价格真实库验证\n\n只读一致性快照：2026-Q2 5026 条、2026-Q3 5024 条、2026-09 939 条，共 10989 条，勘误 3 条；来源登记及源文件哈希通过。三条勘误分别为含税/不含税 492.34/435.70、470.95/416.77、439.83/389.23（第 137 页），保留原价与历史版本。实际存在同名同规格同单位同期多条记录，严格解析拒绝歧义。\n\n仅有郑州市四环以内一个地域，跨地域正例 BLOCKED；当前期正式使用缺依据与专业签认，BLOCKED。结构化证据见同名 JSON。\n',encoding='utf-8')
(reports/'BOQ_VALIDATION.md').write_text('# B_REFERENCE 真实清单验证\n\n土建 50 行、安装 98 行，共 148 行；逐行比对原始编码、名称、项目特征、单位、工程量，均一致；原文件前后 SHA256 不变。项目特征描述列现可识别，缺失或非法字段保留原始单元格和错误，名称包含“小计”的明细不再误删。真实 API 导入及导出往返由业务回归测试验证。\n\n导入值是 B_REFERENCE 原清单值，未独立算量，精度验证仍为 BLOCKED；封面文件没有明细，不构造工程量。结构化记录见同名 JSON。\n',encoding='utf-8')
print('PASS: actual price database 10989 rows, three errata; 148 B_REFERENCE rows compared to original cells')
