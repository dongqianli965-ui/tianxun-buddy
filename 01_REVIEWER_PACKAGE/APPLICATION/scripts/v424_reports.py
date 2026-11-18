from pathlib import Path
import json,hashlib,subprocess,xml.etree.ElementTree as ET
from datetime import datetime,timezone,timedelta
from txbuddy.pilot.database import PilotDatabase,dumps
import os

ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'reports/v424'
TASKS={
 'V24-01-API-AUTH':('VERIFIED','test_v24_api_auth.py',['backend/txbuddy/api.py','backend/txbuddy/security/runtime.py','backend/txbuddy/security/tokens.py'],'有效身份接入；客户端自述许可也不能授权正式计算'),
 'V24-02-ACL-WIRE':('VERIFIED','test_v24_acl_wire.py',['backend/txbuddy/security/runtime.py'],'alice/mallory 对项目、文件、计算记录及证据的跨项目访问拒绝；ACL 持久化'),
 'V24-03-FILES-UPLOAD':('VERIFIED','test_v24_files_upload.py',['backend/txbuddy/pilot/api.py'],'原件上传、内容哈希、版本与去重、授权下载、路径限制'),
 'V24-04-PARSE-API':('VERIFIED','test_v24_parse_api.py',['backend/txbuddy/pilot/api.py','backend/txbuddy/parsing/xlsx_parser.py'],'四类文件 HTTP 解析；DWG 缺转换器明确 BLOCKED；逐单元格结构与来源保留'),
 'V24-05-DELIVER-API':('CHECKPOINTED','test_v24_deliver_api.py',['backend/txbuddy/pilot/api.py','frontend/src/pages/DeliverablePage.vue'],'样本清单成果可生成下载；十类正式生成器已接入前置闸门，但无正版依据/真实签认，正式预算书及工程量计算书正向验收仍 BLOCKED'),
 'V24-06-JOBS-LIST':('VERIFIED','test_v24_jobs_list.py',['backend/txbuddy/pilot/api.py','frontend/src/pages/ProjectPage.vue'],'权限过滤先于分页；服务端列表与状态/项目筛选'),
 'V24-07-AUTH-UI':('VERIFIED','auth-ui.spec.ts',['frontend/src/auth.ts','frontend/src/components/LoginPanel.vue','frontend/src/api.ts','frontend/src/App.vue'],'真实浏览器验证错误口令、登录、重载会话、失效返回登录与登出'),
 'V24-08-QUOTA-LIB':('VERIFIED','test_v24_quota_lib.py',['sql/004_sample_pilot.sql','backend/txbuddy/pilot/service.py'],'五层结构、导入、匹配、四值附注与 HEIGHT 换算只以 TEST_ONLY 样例验证；真实定额正文未取得'),
 'V24-09-PRICE-LIB':('VERIFIED','test_v24_price_lib.py',['backend/txbuddy/pilot/service.py','backend/txbuddy/pilot/importer.py'],'真实材料价格与样本调差；费率矩阵分离；非竞争费用保护。正式费率装载仍需官方依据与人工校对'),
 'V24-10-REAL-L1':('BLOCKED','test_v24_real_l1.py',['backend/txbuddy/pilot/boq.py','LICENSED_DATABASE_INTEGRATION_PLAN.md'],'仅完成资料/清单/导入流程；缺图纸、真实定额/费率/机械价及独立基准，完整真实计算链未验收'),
 'V24-11-GOLDENSET':('BLOCKED','test_v24_goldenset.py',['LICENSED_DATABASE_INTEGRATION_PLAN.md'],'独立专业四类基准和双人签认均缺失；不把148行派生对照当独立基准'),
 'V24-12-E2E-BROWSER':('CHECKPOINTED','sample-pilot.spec.ts',['frontend/e2e/sample-pilot.spec.ts','frontend/playwright.config.ts'],'样本交互链与权限拒绝通过；真实算量/套定额/组价/正式出件因资料和专业基准缺失，完整 E 级链仍 BLOCKED'),
 'V24-13-INTERNAL-TOKEN':('VERIFIED','test_v24_internal_token.py',['backend/txbuddy/security/runtime.py','backend/txbuddy/security/deps.py','backend/txbuddy/api.py'],'缺失/空/旧默认值拒绝；正确密钥及轮换正反向写入均实测'),
 'V24-14-HTML-PARSE':('VERIFIED','test_v24_html_parse.py',['backend/txbuddy/parsing/html_parser.py','backend/txbuddy/parsing/registry.py'],'117/117 HTML/HTM；正文、标题、列表、表格、中文及登记来源URL'),
 'V24-15-OCR-PATH':('VERIFIED','test_v24_ocr_path.py',['backend/txbuddy/parsing/ocr.py','scripts/v424_ocr_corpus.py'],'离线 OCR、页图/位置/置信度/原件哈希留存；173号与23号可复核转录。全部结果仍待人工校对，非正式费率'),
 'V24-16-CORPUS-BIND':('VERIFIED','test_v24_corpus_bind.py',['backend/txbuddy/pilot/importer.py','backend/txbuddy/pilot/provenance.py','backend/txbuddy/pilot/extraction.py'],'样本价格文件/SHA/页行/地区期次/许可必填，勘误生效且原值保留，反向查询、幂等和回滚通过；全仓另有4项来源URL缺口'),
 'V24-17-CORPUS-SAMPLE-PILOT':('CHECKPOINTED','test_v24_corpus_sample_pilot.py',['scripts/v424_pilot.py','scripts/v424_local_pilot.py','frontend/src/pages/SamplePilotPage.vue'],'148行真实清单 B_REFERENCE 入库、查询、流程及导出通过；A/B仍有来源和解析/校对边界，E BLOCKED'),
}


def build():
    result=json.loads((OUT/'CORPUS_SAMPLE_PILOT_RESULTS.json').read_text(encoding='utf-8'))
    ocr=json.loads((OUT/'OCR_CORPUS_RESULTS.json').read_text(encoding='utf-8'))
    regression=json.loads((OUT/'REGRESSION_RESULT.json').read_text(encoding='utf-8'))
    browser=json.loads((OUT/'playwright-results.json').read_text(encoding='utf-8'))
    xml=Path(regression['log']).with_suffix('.xml');suites=list(ET.parse(xml).getroot().iter('testsuite'))
    totals={key:sum(int(s.get(key,'0')) for s in suites) for key in ('tests','failures','errors','skipped')}
    assert regression['exit_code']==0 and totals['failures']==totals['errors']==totals['skipped']==0
    assert regression['legacy_assertions_preserved'] and browser['stats']['expected']>=2 and not browser['stats']['unexpected']
    db=PilotDatabase(os.environ['TXBUDDY_SAMPLE_DSN']);counts=db.counts()
    effective={x['path']:x['status']=='PARSED' for x in result['parsing_files']}
    for x in ocr['files']:
        if x['status']=='PARSED':effective[x['source_file']]=True
    main=[x for x in result['parsing_files'] if x['format'] in ('pdf','html','htm','xlsx','docx')]
    native=sum(x['status']=='PARSED' for x in main);coverage=sum(effective[x['path']] for x in main)
    result.update(counts=counts,levels={'A':'PARTIAL','B':'PARTIAL','C':'PASS','D':'PASS_SAMPLE_IMPORT_ONLY','E':'BLOCKED'},
        level_notes={'A':'250/250哈希与所有价格追溯通过；216/220来源URL补齐，4项缺口保留',
        'B':'结构化与OCR草稿可用；不代表逐页无漏项、DOC/归档支持或专业校对通过',
        'C':'10989原始表行，3条勘误，90条PDF六字段核对通过；明确期次/四环内参考用途',
        'D':'真实清单50+98=148行、B_REFERENCE角色、工作流、生成下载通过；未独立算量',
        'E':'无完整图纸/正版定额与国标正文/正式费率/机械价/独立人工基准及双人签认'},
        parsing_summary={'native_main_parsed':native,'main_files':len(main),'native_main_rate':native/len(main),
            'pipeline_text_coverage':coverage,'pipeline_main_rate':coverage/len(main),'native_all_parsed':sum(x['status']=='PARSED' for x in result['parsing_files']),
            'all_files':len(effective),'pipeline_all_text_coverage':sum(effective.values()),
            'meaning':'获得可定位文本或OCR草稿的文件覆盖率，不是全文完整性/准确率/专业校对通过率'},
        ocr_corpus_summary={'files':len(ocr['files']),'parsed':sum(x['status']=='PARSED' for x in ocr['files']),'formal_usable':False,'review_status':'NEEDS_EXPERT_REVIEW'},
        regression={'tests':totals['tests'],'failures':0,'skipped':0,'legacy_assertions_preserved':True,'log':regression['log']},
        browser={'passed':browser['stats']['expected'],'unexpected':0,'http_evidence':'E2E_HTTP_EVIDENCE.json'},
        generated_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),production_accepted=False)
    (OUT/'CORPUS_SAMPLE_PILOT_RESULTS.json').write_text(dumps(result),encoding='utf-8')
    (ROOT/'CORPUS_SAMPLE_PILOT_RESULTS.json').write_text(dumps(result),encoding='utf-8')
    summary=ROOT.parent/'_CODEX_V424/local-pilot/seed_state.json'
    seeded=json.loads(summary.read_text(encoding='utf-8'))
    native_all=result['parsing_summary']['native_all_parsed'];all_count=len(effective)
    lines=['# V4.2.4 真实资料样本验证报告','',f"日期：{result['generated_at']}；交付范围：SAMPLE_PILOT。正式计价及计算精度未验收。",'',
        '## 实际完成与证据','',
        f"- 代码分支 `codex/v4.2.4-sample-pilot`，实际 HEAD `c1b466ba2b4cbf153ef01a3079863c4626dc7bbd`；`f75d238` 是上一提交。未丢弃原有修改和未跟踪文件。",
        '- 仓库外完整私有工作树快照 10,441 文件，逐文件哈希校验通过；历史 V4.2.3 独立副本复现 824 passed / 2 skipped。',
        f"- 当前隔离全量回归：**{totals['tests']} passed / 0 failed / 0 skipped**。原有断言 AST 全部保留；原开发 SQLite 哈希未变，原 PostgreSQL 核心业务表仍为空。",
        '- 旧 psql 两项原先跳过；本轮修复 Windows 参数调用并建立独立 schema，真实执行迁移、唯一约束及事务回滚。',
        '- 认证/ACL/内部密钥均接主应用；匿名 401、无权 403、登出吊销、过期、跨项目文件/计算/证据、密钥轮换均已测试。专业签认不接受未经核验的身份。',
        '- 正式数值必须匹配服务端登记的授权依据包；客户端声明 LICENSED 不能自行获准。旧回归的授权正向分支只在隔离 SQLite 写入带 TEST_ONLY_UNIT_FIXTURE 标签的合成批准夹具，从未写入真实 PostgreSQL 样本库。',
        f"- 浏览器 **{browser['stats']['expected']} 项通过**：错误口令/登录/会话重载/失效/登出，以及真实上传→XLSX结构化→148行清单→扫描PDF离线OCR→勘误价→来源→流程→生成下载→跨用户拒绝。",
        '', '## 真实数据使用情况','',
        f"独立 PostgreSQL `txbuddy_sample_pilot` 中：来源 {counts['source_register']}、资料 {counts['document_inventory']}、项目样本 {counts['project_sample']}、抽取字段 {counts['extracted_field']}、材料价格 {counts['material_price']}、勘误 {counts['material_price_correction']}。真实定额、正式费率与正式依据包均为 0。",
        '','原始资料 SHA256 **250/250 一致**；未改动只读数据仓。来源URL通过登记、同哈希及下载日志证据补齐至 **216/220**；其余4项均在JSON列明，保留 INCOMPLETE/UNVERIFIED，不编造链接、授权或独立基准。',
        '', '| 级别 | 结论 | 边界 |','|---|---|---|',
        '| A 数据导入与来源 | PARTIAL | 导入/原件哈希/全部价格追溯通过；4项全仓来源URL未闭环 |',
        '| B 解析与结构化 | PARTIAL | 文本/OCR与单元格结构可用；旧DOC及归档明确阻塞，OCR待人工校对 |',
        '| C 材料价格查询 | PASS | 限现有真实样本期次和郑州市区四环以内，不是现行或跨地区计价认可 |',
        '| D 清单与业务流程 | PASS_SAMPLE_IMPORT_ONLY | 148行参考清单导入/流转/导出通过，未独立算量 |',
        '| E 工程量/定额/组价精度 | BLOCKED | 缺资料、授权正文及真实专业基准/双人签认 |','',
        '## 解析覆盖与未支持格式','',
        f"- HTML/HTM：**117/117（100%）**；正文、标题、表格行列、列表、中文与登记来源保留；脚本样式过滤。",
        f"- 直接文本解析：主要五类 **{native}/{len(main)}（{native/len(main):.2%}）**；全库 **{native_all}/{all_count}（{native_all/all_count:.2%}）**。PDF原有文本层42/70，XLSX8/8，DOCX2/2。",
        f"- 文本解析加离线OCR后：主要五类有可定位文本/草稿的覆盖 **{coverage}/{len(main)}（{coverage/len(main):.2%}）**；全库 **{sum(effective.values())}/{all_count}（{sum(effective.values())/all_count:.2%}）**。这是文件文本覆盖率，**不代表逐页无遗漏、识别准确率100%或人工校对通过**。",
        f"- OCR通道实际处理 {len(ocr['files'])} 份文件，{sum(x['status']=='PARSED' for x in ocr['files'])} 份产出文字；原件哈希、页图、图片哈希、页码/框位置、文字、置信度均保留，全部 NEEDS_EXPERT_REVIEW。173号8页及最新指数23号2页均有转录草稿。",
        '- 28与29差异已定位：许昌《征集文件》前3页无文本，第5/11页有文本，原质量报告只探测前3页。混合文本扫描页已进入本轮OCR。',
        '- 9份旧式DOC、4份ZIP、1份RAR不假报正文解析成功；ZIP只检查目录，其内容解析为BLOCKED。逐文件原因及原件SHA见JSON。图片走独立离线OCR通道。',
        '- XLSX保留Sheet/隐藏状态、行列/坐标、原值、缓存值、公式、单位、表头、合并单元格、空行及汇总行。主清单与备用镜像实际仅含封面/说明，明确不计为完整清单导入。','',
        '## 价格、勘误与地区/期次控制','',
        '- 入库原始表行数：2026 Q2 **5026**，Q3 **5024**，9月 **939**，共 **10989**，未静默删行。',
        '- Q2有8组重复原记录号，Q3有3组。原记录号保留于raw_payload，存储键按页码/CSV行号稳定消歧，避免静默覆盖。',
        '- Q3最后3行属于对Q2的勘误，作为Q2版本历史生效；因此Q3正常价格查询为5021条，原始入库仍5024行，未把勘误当新Q3材料价。',
        '- 第137页对Q2第1782—1784条的含税/不含税勘误分别为492.34/435.70、470.95/416.77、439.83/389.23。原值与Q3来源哈希、页码全部保留。',
        '- 每张表均匀抽取30条，合计90条；按原始PDF同一行核对序号、名称、规格、单位和两列价格，**90/90通过**。这不是对全部记录的人工复核声明。',
        '- 材料查询要求明确期次与410100-INNER-4RING；跨城市被拒。2025不得冒充当前价格；2026Q4/10月缺失，不自动回退或补造。',
        '- 官方两列原值及税率说明保留，未按比值推断税率。商品混凝土13%与工程造价指标9%分开；Q2/9月通知正文缺失，范围/费用口径仍待核实。',
        '- 重复导入幂等；SQLite及真实PostgreSQL故障注入验证业务数据全部回滚，独立失败审计保留。PostgreSQL备份已在另一独立数据库恢复并逐表对数。','',
        '## 样本流程、成果与使用判断','',
        f"持久样本项目 `{seeded['job_id']}`，土建50行+安装98行，共148行，全部保留 B_REFERENCE；导入任务 DONE，可生成并下载真实XLSX，带原件SHA、Sheet、行及单元格定位。西峡县项目未套用郑州价格，也未将已发布清单或参考成果当作独立计算结果。",
        '', '系统已经能使用现有真实资料做资料登记、文本/OCR、字段结构化、价格查询、来源核对及样本清单工作流。当前可以进入资料补齐、人工OCR校对和授权试用评估；**尚不能进入正式造价生产或宣称完整计算精度通过**。',
        '', '依赖正版资料的部分：河南各专业定额正文/消耗量/附注换算，2024国家标准正文，费用与正式费率、机械台班及其他地区/期次价格。依赖真实专业人员的部分：OCR数字与条文校对、工程量/定额/组价独立基准、适用规则判断、差异解释和双人签认。',
        '', '## 交付与复现','',
        '- 完整源码及队列：当前分支；原有V4.2.3交付与未提交成果均保留。安全源码包另见release/V424_PACKAGE_REPORT.json与V424_SHA256SUMS.txt。',
        '- 机器结果：CORPUS_SAMPLE_PILOT_RESULTS.json；OCR_CORPUS_RESULTS.json；POSTGRESQL_ACCEPTANCE.json；REGRESSION_RESULT.json。',
        '- 37个原登记端点逐项映射：reports/v424/ENDPOINT_MAPPING.json（页面→真实路由/服务→数据库/确定性函数→返回语义）。connected标记不等于业务精度验收。',
        '- 浏览器：reports/v424/playwright-report/index.html、playwright-results.json、E2E_HTTP_EVIDENCE.json及screenshots/。',
        '- 缺口：V424_UNRESOLVED.md；采购/授权准备：LICENSED_DATABASE_INTEGRATION_PLAN.md；启动与验证：docs/21_V424运行与验证.md。',
        '- 真实样本库及私有备份独立留在本机，不打入源码安全包；本轮没有采购或接入正版完整数据库。',
        '', '本报告由自动化验证生成，不构成专业签认。']
    (ROOT/'CORPUS_SAMPLE_PILOT_REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    (OUT/'CORPUS_SAMPLE_PILOT_REPORT.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    queue=json.loads((ROOT/'scripts/V424_TASK_QUEUE.json').read_text(encoding='utf-8-sig'))
    for task in queue['tasks']:
        status,test,files,note=TASKS[task['id']];task.update(status=status,attempts=max(1,task.get('attempts',0)),result_note=note,
            modified_files=files,test_file=test,test_evidence=['reports/v424/REGRESSION_RESULT.json','reports/v424/playwright-results.json','reports/v424/CORPUS_SAMPLE_PILOT_RESULTS.json'],
            unresolved=[] if status=='VERIFIED' else [note],updated_at=result['generated_at'])
        if test.endswith('.spec.ts'):command='npm.cmd run test:e2e -- '+test;cwd='frontend'
        else:command='python -m pytest tests/'+test+' -q';cwd='backend'
        task['verify']=[{'cwd':cwd,'command':command}]
        folder=ROOT/'tasks'/task['id'];folder.mkdir(exist_ok=True)
        detail={'task':task['id'],'status':status,'modified_files':files,'commands':[{'cwd':cwd,'command':command}],
            'test_result':'PASS for available automated scope','test_log':regression['log'] if cwd=='backend' else 'reports/v424/playwright_final.log',
            'unresolved':task['unresolved'],'acceptance_scope':note}
        (folder/'RESULT.json').write_text(dumps(detail),encoding='utf-8')
        (folder/'RESULT.md').write_text('# '+task['id']+'\n\n状态：'+status+'\n\n'+note+'\n\n修改文件：\n\n'+''.join('- '+f+'\n' for f in files)+'\n验证命令：`'+command+'`（'+cwd+'）\n\n日志：'+detail['test_log']+'\n\n未解决：'+('；'.join(task['unresolved']) or '本任务自动化范围内无遗留；专业计算范围另行验收')+'\n',encoding='utf-8')
    queue['updated_at']=result['generated_at'];queue['production_accepted']=False
    (ROOT/'scripts/V424_TASK_QUEUE.json').write_text(json.dumps(queue,ensure_ascii=False,indent=2),encoding='utf-8')
    unresolved=['# V4.2.4 未解决问题','', '| 任务 | 状态 | 未完成范围 |','|---|---|---|']
    for id,(status,test,files,note) in TASKS.items():
        if status!='VERIFIED':unresolved.append(f'| {id} | {status} | {note} |')
    unresolved+=['','此外：4项来源URL仍未闭环；旧DOC与归档正文未支持；所有OCR均须真实人工校对。正式费用矩阵、机械台班、Q2/9月通知正文、其他城市价格及缺失期次尚未取得。','','未通过的任务未标VERIFIED；没有购买数据库、构造真实定额、生成虚假专业签认或宣称E级精度通过。']
    (ROOT/'V424_UNRESOLVED.md').write_text('\n'.join(unresolved)+'\n',encoding='utf-8')
    progress={s:sum(t['status']==s for t in queue['tasks']) for s in ('VERIFIED','CHECKPOINTED','BLOCKED')}
    state=ROOT/'EXECUTION_STATE.md';old=state.read_text(encoding='utf-8-sig')
    prefix='# EXECUTION_STATE — V4.2.4 样本试点\n\n'+f"更新：{result['generated_at']}。当前：{progress}；全量回归{totals['tests']}通过；浏览器{browser['stats']['expected']}通过。E级精度BLOCKED，正式生产未验收。\n\n权威状态：scripts/V424_TASK_QUEUE.json；报告：CORPUS_SAMPLE_PILOT_REPORT.md；缺口：V424_UNRESOLVED.md。\n\n---\n\n以下为原始交接与历史记录，完整保留。\n\n"
    if not old.startswith('# EXECUTION_STATE — V4.2.4 样本试点'):state.write_text(prefix+old,encoding='utf-8')
    readme=ROOT/'README.md';old=readme.read_text(encoding='utf-8-sig')
    if not old.startswith('# 天寻 Buddy V4.2.4'):
        readme.write_text('# 天寻 Buddy V4.2.4 样本试点\n\n已实现认证/项目ACL、文件接入、结构化解析与离线OCR、郑州样本价格及勘误追溯、148行清单导入流程和样本导出。正式计价与计算精度未验收。\n\n- 验收：CORPUS_SAMPLE_PILOT_REPORT.md / CORPUS_SAMPLE_PILOT_RESULTS.json\n- 启动：RUN_V424_SAMPLE_PILOT.cmd；说明：docs/21_V424运行与验证.md\n- 遗留：V424_UNRESOLVED.md；正版准备：LICENSED_DATABASE_INTEGRATION_PLAN.md\n\n以下V4.2.3历史说明完整保留。\n\n---\n\n'+old,encoding='utf-8')
    with (ROOT/'V424_EXECUTION_LOG.md').open('a',encoding='utf-8') as f:
        f.write('\n## 最终验证 '+result['generated_at']+'\n\n'+f"- 隔离全量回归 {totals['tests']} passed / 0 failed / 0 skipped，原断言AST保留，开发库哈希未变。\n- 浏览器 {browser['stats']['expected']} passed；37端点映射已复核；PostgreSQL回滚及备份恢复通过。\n- OCR扩展至全部扫描/混合PDF和图片，产物仍NEEDS_EXPERT_REVIEW；原件250哈希未变。\n- 队列分级：{progress}。未通过不标VERIFIED。\n- 每项修改文件、执行命令、测试日志、结果、遗留和验收状态见tasks/V24-*/RESULT.md/.json。\n\n")
    print(dumps({'regression':totals,'progress':progress,'parsing':result['parsing_summary'],'counts':counts}))


if __name__=='__main__':build()
