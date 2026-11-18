"""Generate final delivery explanations from independent acceptance evidence."""
from pathlib import Path
from datetime import datetime,timezone,timedelta
from collections import Counter
import hashlib,json,os,sqlite3,subprocess

ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'reports/v425'

def read(name):return json.loads((OUT/name).read_text(encoding='utf-8'))

def generate():
    regression=read('REGRESSION_RESULT.json');browser=read('playwright-results.json');db=read('DATABASE_POC_RESULT.json');integrity=read('DATA_INTEGRITY.json')
    queue=json.loads((ROOT/'scripts/V425_TASK_QUEUE.json').read_text(encoding='utf-8'));counts=dict(Counter(t['status'] for t in queue['tasks']))
    if regression['exit_code'] or browser['stats']['unexpected'] or not integrity['passed'] or not db['migration_poc_passed']:
        raise SystemExit('验收证据未通过，拒绝生成完成报告')
    stamp=datetime.now(timezone(timedelta(hours=8))).isoformat()
    runtime=ROOT.parent/'_CODEX_V425/local-pilot/runtime';conn=sqlite3.connect((runtime/'governance.sqlite3').resolve().as_uri()+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
    documents=[]
    for doc in conn.execute('SELECT * FROM ocr_document ORDER BY priority,source_path'):
        d=dict(doc);pages=conn.execute('SELECT page_id,page_number,image_sha256 FROM ocr_page WHERE document_id=? ORDER BY page_number',(d['document_id'],)).fetchall()
        d['pages']=[dict(p) for p in pages];d['review_status']='NEEDS_EXPERT_REVIEW';d['review_events']=conn.execute('SELECT count(*) FROM ocr_review_event e JOIN ocr_page p USING(page_id) WHERE p.document_id=?',(d['document_id'],)).fetchone()[0]
        if d['review_events']:raise SystemExit('真实复核状态发生变化，必须重新汇总，不能声称全部待复核')
        documents.append(d)
    conn.close()
    (OUT/'OCR_REVIEW_STATUS_V425.json').write_text(json.dumps({'generated_at':stamp,'files':documents,'count':len(documents),'pages':366,'reviewed':0,'signed':0,'production_accepted':False},ensure_ascii=False,indent=2),encoding='utf-8')
    routes=[
      ('GET','/ocr','OcrReviewPage.vue','ocr_document / ocr_page / ocr_review_event','READ，先ACL后分页'),
      ('GET','/ocr/documents/{document_id}','OcrReviewPage.vue','ocr_document / ocr_page','READ，逐文档项目权限'),
      ('GET','/ocr/pages/{page_id}','OcrReviewPage.vue','ocr_page / ocr_review_event','READ，历史哈希链'),
      ('GET','/ocr/pages/{page_id}/image','OcrReviewPage.vue','ocr_page / 派生PNG','READ，下载前SHA复核'),
      ('POST','/ocr/pages/{page_id}/reviews','OcrReviewPage.vue','ocr_review_event','WRITE，版本CAS及追加事务'),
      ('POST','/ocr/pages/{page_id}/signoff','OcrReviewPage.vue','ocr_review_event / 私有专业身份登记','REVIEW+核验专业身份；未核验403'),
      ('GET','/projects','ProjectReadinessPage.vue','governance_catalog_current / snapshot','逐项目READ过滤'),
      ('GET','/sources','ProjectReadinessPage.vue','governance_catalog_current / snapshot','逐来源项目READ过滤后分页'),
      ('GET','/sources/{source_id}','ProjectReadinessPage.vue','governance_catalog_current / snapshot','逐来源项目READ过滤')]
    mapping=[{'method':m,'path':'/v1/governance'+p,'frontend':ui,'storage':'V425 governance.sqlite3: '+tables,'authorization':acl,'automated_evidence':['test_v425_ocr_review.py','test_v425_legacy_source_acl.py','governance.spec.ts']} for m,p,ui,tables,acl in routes]
    (OUT/'ENDPOINT_MAPPING_V425.json').write_text(json.dumps({'mount':'独立治理子应用；保留旧V424扩展写入口清单','routes':mapping,'count':9,'production_accepted':False},ensure_ascii=False,indent=2),encoding='utf-8')
    unresolved='''# V4.2.5 未解决问题

| 范围 | 状态 | 缺口与下一步 |
|---|---|---|
| 40份OCR，366页 | NEEDS_EXPERT_REVIEW | 真实人工复核0、正式签认0。安排真实专业人员逐页核对数字和条文，再完成资格核验；AI不代签 |
| SRC-0132 / SRC-0137 / SRC-0220 | INCOMPLETE | 本地说明、摘录及上一轮派生清单，不编造直接URL；父来源和逐页/逐行关系已登记，仍缺原始业务材料或独立专业基准 |
| 其他来源/授权 | PARTIAL | 220项完成状态审计，217项有直接URL，不等于全部VERIFIED或电子数据库使用许可已确认；历史SECONDARY/UNVERIFIED等保留 |
| 2份DOC | FAILED / CHECKPOINTED | SRC-0136西峡中标附件招标文件、候选平顶山招标DOC被antiword拒绝；CFB扇区不齐仅为结构异常线索，不能确定原因。需完整原件或可核对的DOCX/PDF，不修补猜测正文 |
| 4份ZIP扫描PDF | NEEDS_EXPERT_REVIEW | 安全解包、成员SHA及旧OCR页图核验通过；正文只是OCR草稿，未完成人工校对 |
| 18套项目完整L1验证 | BLOCKED | 均无匹配的完整施工图+完整计价清单+签署合同+独立专业计算基准组合。RAR内图纸/清单“另附”，合同模板不计签署合同 |
| 统一PostgreSQL运行切换 | BLOCKED | 隔离PoC通过；SQLite门面、认证持久化和FileStore适配、跨源事务/文件outbox、正式连接身份及停机/双写切换演练尚未完成，不直接改DSN |
| 正式费率、定额/机械台班与精度 | BLOCKED | 缺授权正文、适用口径、真实专业独立基准及签认；原来的V424 CHECKPOINTED/BLOCKED任务未升级 |
| 价格期次及地区 | MISSING | 只有现有2026Q2/Q3/9月郑州四环内价格；缺期次/其他地区不补造，Q2/9月通知口径仍待核实 |

生产验收始终为 False。以上资料缺口不会因自动化测试、界面可用或迁移PoC通过而消失。未采购数据库、未绕过保护、未向正式区导入TEST_ONLY资料。
'''
    (ROOT/'V425_UNRESOLVED.md').write_text(unresolved,encoding='utf-8')
    report=f'''# 天寻 Buddy V4.2.5 数据治理与人工复核交付报告

生成：{stamp}。分支：codex/v4.2.5-data-governance。基线HEAD：c1b466ba2b4cbf153ef01a3079863c4626dc7bbd。

**生产验收 False；E级工程量、定额及组价精度 BLOCKED。** 本轮范围为 DATA_GOVERNANCE_SAMPLE。

## 验收结果

- V424隔离基线869 passed / 0 failed / 0 skipped；完整工作树快照11465文件、763560000字节，逐项SHA一致。
- V425隔离全量：{regression['tests']} tests，{regression['tests']-regression['failures']-regression['errors']-regression['skipped']} passed / {regression['failures']} failed / {regression['skipped']} skipped；原53个测试文件逐字节保留。
- 浏览器3/3通过，包含原2用例及新增十场景；前端真实构建。截图和HTML保留于reports/v425。
- 安全打包专项12项通过；独立验收队列：{counts}。文件兼容继续CHECKPOINTED，专业复核/E级/正式切换继续BLOCKED。
- 真实数据仓251/251字节哈希一致；原250项SHA清单保持；10989价格、3条勘误与历史、148行B_REFERENCE参考清单守恒。未自动补缺期或扩大地区。

## 最终问题逐项回答

1. **OCR工作台能实际使用吗？能。** 已启动本机入口 http://127.0.0.1:4185，真实登录、40文件列表、原始页图、区域定位、缩放/换页、编辑、前后对比、持久化复核和历史均连接后端及治理SQLite。真实账号冒烟通过；并发旧版本返回409；跨项目403；未经核验签认403。临时测试运行库中的编辑带TEST_ONLY，不影响真实资料。
2. **40份资料是什么状态？** 40份366页全部NEEDS_EXPERT_REVIEW，真实人工复核0、签认0。导入不自动创建review事件。173号、23号、2024指数及计价/税费扫描件按优先级排队。逐文件状态见reports/v425/OCR_REVIEW_STATUS_V425.json。
3. **220项全部核验通过了吗？没有。** 220项状态审计完成；217项有直接URL。SRC-0155已由日志原链接重新取得官方文件，完整SHA匹配；其授权仍UNVERIFIED。另3项派生件保留INCOMPLETE，以父来源链说明，不伪造直接链接；其他历史来源状态、授权和内容适用性继续分别核验。
4. **DOC和ZIP是否能解析？部分具备。** DOC7/9成功、2失败，失败保留引擎和结构诊断；4ZIP安全解包，扫描PDF显式复用经SHA复核的既有OCR草稿，尚未人工校对。RAR通过本机签名系统工具安全提取2PDF并原生解析；无可靠工具时明确UNSUPPORTED。原件均不改写，所有派生另存，安全/限时/资源/编码等55项新测试通过。
5. **18套有完整L1算量验证项目吗？没有。** 70份原件及2份RAR派生PDF逐项登记；完整施工图、对应完整清单/签署合同及独立专业基准组合均缺。已发布清单和他项目业绩载体保持B_REFERENCE。详见REAL_PROJECT_READINESS_MATRIX_V425.xlsx及对应审查报告。
6. **PostgreSQL能正式切换吗？不能。** 独立PoC39张表、{db['checks']['data_count']['rows']}行，{len(db['checks'])}项检查通过；369文件/页图引用、348去重对象均哈希核对，备份在另一独立库恢复。数据量、哈希、关系、幂等、并发、事务回滚、备份恢复、权限均实跑；原始整份治理快照不开放业务角色。当前业务/认证/FileStore门面和切换演练仍未完成，运行连接未切换。
7. **现有资料还能做什么？** 资料登记与版本治理、文本和OCR草稿解析、人工复核、来源/文件/页行追溯、现有期次郑州四环以内材料价格及勘误查询、148行参考清单的导入/查询/结构化/导出、样本业务流程、权限和数据库迁移试验。不能认定为正式费率验证或独立算量GoldenSet。
8. **还需要提供什么？** 至少一套有合法使用权、完整版本/变更链的施工图，匹配完整清单和签署合同/补充条款、项目地点/阶段、结算验收资料及材料/定额计价依据；另需真实专业人员的独立工程量/定额/组价基准、差异解释和签认。两份失败DOC需要完整可读原件或对应DOCX/PDF。本轮不自动采购。

## 交付索引

| 交付 | 位置 |
|---|---|
| 完整源码、治理路由和工作台 | backend/txbuddy/governance、frontend/src/pages、scripts |
| 运行及验收说明 | docs/22_V425运行与验收.md；RUN_V425_GOVERNANCE.cmd；RUN_V425_VERIFY.cmd |
| OCR导入/状态/十场景证据 | reports/v425/OCR_WORKBENCH_IMPORT.json、OCR_REVIEW_STATUS_V425.json、GOVERNANCE_E2E_EVIDENCE.json |
| 220来源状态 | SOURCE_VERIFICATION_REPORT_V425.md；reports/v425/SOURCE_STATUS.json |
| 项目矩阵和审查 | REAL_PROJECT_READINESS_MATRIX_V425.xlsx；REAL_PROJECT_READINESS_REPORT_V425.md |
| 文件解析与安全边界 | reports/v425/PARSING_WORKER_RESULT.md；PARSING_RESULTS_NATIVE.json；PARSING_RESULTS_OCR.json |
| 75表架构目录与迁移PoC | DATABASE_UNIFICATION_PLAN_V425.md；reports/v425/DATABASE_TABLE_CATALOG.json；DATABASE_POC_RESULT.json |
| 全量、浏览器及截图 | reports/v425/REGRESSION_RESULT.json；playwright-report/index.html；playwright-results.json；screenshots |
| 数据完整性及剩余问题 | DATA_INTEGRITY_REPORT_V425.md；V425_UNRESOLVED.md |
| 持续执行与独立验收 | scripts/V425_TASK_QUEUE.json、v425_tasks.py；tasks/V25-*；失败诊断tasks/D-V25-08-REGRESSION |
| 安全ZIP、包哈希与逐文件清单 | release/V425_PACKAGE_REPORT.json、V425_SHA256SUMS.txt、V425_FILE_SHA256SUMS.txt；ZIP内SHA256SUMS |

真实仓、账号/密钥/连接串、运行DB、备份、原始OCR全文和页图、认证trace不进入安全源码ZIP；完整私有快照和迁移备份留在仅当前Windows用户及SYSTEM可读的_CODEX_V425目录。旧工作树及V424成果未重置、删除或覆盖。浏览器历史输出在独立副本中写入，旧V424报告继续保留。

本报告由自动化证据汇总，不构成真实专业人员签认。
'''
    (ROOT/'V425_DELIVERY_REPORT.md').write_text(report,encoding='utf-8')
    status=f'''# EXECUTION_STATE — V4.2.5 数据治理与人工复核

更新：{stamp}。队列：{counts}。全量{regression['tests']}通过，无失败无skip；浏览器3通过。生产验收False，E级BLOCKED。真实40份OCR仍待人工校对；2份DOC转换失败；没有切换业务数据库。

权威状态：scripts/V425_TASK_QUEUE.json；报告：V425_DELIVERY_REPORT.md；运行：docs/22_V425运行与验收.md；缺口：V425_UNRESOLVED.md。

---

'''
    state=ROOT/'EXECUTION_STATE.md';old=state.read_text(encoding='utf-8')
    if old.startswith('# EXECUTION_STATE — V4.2.5'):old=old.split('\n---\n\n',1)[1]
    state.write_text(status+old,encoding='utf-8')
    readme=ROOT/'README.md';text=readme.read_text(encoding='utf-8')
    if not text.startswith('# 天寻 Buddy V4.2.5'):
        readme.write_text('# 天寻 Buddy V4.2.5\n\n数据治理与人工复核工作台已接真实数据库。运行 `RUN_V425_GOVERNANCE.cmd`，打开 http://127.0.0.1:4185 。说明见 `docs/22_V425运行与验收.md`，完整交付见 `V425_DELIVERY_REPORT.md`。\n\n生产验收False；40份OCR待人工复核；E级精度及正式数据库切换BLOCKED。安全源码包不含真实数据仓、账号及运行库。以下历史README完整保留。\n\n---\n\n'+text,encoding='utf-8')
    (ROOT/'V425_EXECUTION_LOG.md').write_text('# V4.2.5 执行与验收记录\n\n'+
      '- 基线完整快照和869无skip复现通过。\n- 三个独立开发Worker完成解析、语料治理、数据库PoC；主Worker完成OCR服务、页面、真实导入及浏览器。\n- 初次浏览器启动超时及导航定位冲突均定位修复；旧浏览器用例和断言保留。\n- 全量旧扩展POST精确清单失败已按独立治理挂载修复，诊断及失败日志保留。\n'+
      f'- 最终全量{regression["tests"]}无失败无skip；旧53测试文件逐字节保留。浏览器3通过；独立队列{counts}。\n- 数据库最终39表/{db["checks"]["data_count"]["rows"]}行迁移、恢复及隔离通过；未切换原环境。\n- 未采购数据库、未生成正式费率、未真实签认。\n',encoding='utf-8')
    (OUT/'DELIVERY_SUMMARY.json').write_text(json.dumps({'version':'V4.2.5','generated_at':stamp,'queue_counts':counts,
      'regression_tests':regression['tests'],'browser_tests':3,'production_accepted':False,'accuracy_E':'BLOCKED',
      'ocr_files':40,'ocr_pages':366,'human_review_events':0,'sources':220,'direct_urls':217,'projects':18,'l1_ready':0,
      'doc_parsed':7,'doc_failed':2,'zip_safe_extracted':4,'rar_parsed':1,'postgresql_switch_ready':False},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'report':'V425_DELIVERY_REPORT.md','queue':counts,'production_accepted':False},ensure_ascii=False))

if __name__=='__main__':generate()
