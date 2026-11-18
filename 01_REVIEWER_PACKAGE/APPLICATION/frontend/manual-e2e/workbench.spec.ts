import {test,expect,Page} from '@playwright/test';
import {readFileSync,readdirSync,mkdirSync,writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {createHash} from 'node:crypto';
const session=process.env.TXBUDDY_MANUAL_SESSION!;
if(!session)throw new Error('TXBUDDY_MANUAL_SESSION required: external TEST_ONLY runtime');
const fixture=JSON.parse(readFileSync(join(session,'TEST_ACCOUNTS.private.json'),'utf8'));
const out=resolve('../reports/v426_manual');mkdirSync(join(out,'visuals'),{recursive:true});
const http:any[]=[],consoleErrors:string[]=[],pageErrors:string[]=[];
test.beforeEach(async({page})=>{
  page.on('pageerror',e=>pageErrors.push(e.message));
  page.on('console',m=>{if(m.type()==='error')consoleErrors.push(m.text());});
  page.on('response',r=>{const u=new URL(r.url());if(/^\/(v1|auth|health|files|projects)/.test(u.pathname))http.push({at:new Date().toISOString(),method:r.request().method(),path:u.pathname,status:r.status()});});
});
test.afterAll(()=>{writeFileSync(join(out,'BROWSER_NETWORK.json'),JSON.stringify({requests:http,console_errors:consoleErrors,page_errors:pageErrors,note:'Expected 401/403/422 negative cases are retained; no credentials or bodies captured.'},null,2));});
async function login(page:Page,role='OWNER'){await page.goto('/');await page.getByTestId('login-user').fill(fixture.accounts[role].username);await page.getByTestId('login-password').fill(fixture.accounts[role].password);await page.getByTestId('login-submit').click();await expect(page.getByTestId('logout')).toBeVisible();}
async function nav(page:Page,name:string){await page.locator('nav button').filter({has:page.locator('.nav-label',{hasText:new RegExp('^'+name+'$')})}).click();}
async function create(page:Page,name='TEST_ONLY 浏览器技术验证'){await nav(page,'项目');await page.getByLabel('项目编号',{exact:true}).fill('TEST_ONLY-MANUAL-'+Date.now());await page.getByLabel('项目名称',{exact:true}).fill(name);const response=page.waitForResponse(r=>r.url().endsWith('/v1/job')&&r.request().method()==='POST');await page.getByRole('button',{name:'创建项目',exact:true}).click();const r=await response;expect(r.status()).toBe(200);return await r.json();}
const pages=['OCR 人工复核','资料完整性','样本试点','项目','资料','清单','算量','定额','组价','复核','成果','证据'];
for(const size of [{width:1920,height:1080},{width:1366,height:768}]){
  test(`全部12入口显示与页面溢出 ${size.width}x${size.height}`,async({page})=>{
    await page.setViewportSize(size);await login(page);
    await expect(page.getByTestId('frontend-version')).toContainText('V4.2.6');
    await expect(page.getByTestId('runtime-environment')).toContainText('TEST_ONLY');
    await expect(page.getByTestId('backend-status')).toContainText('后端已连接');
    for(let i=0;i<pages.length;i++){await nav(page,pages[i]);await expect(page.locator('main h2').first()).toBeVisible();
      expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1)).toBeTruthy();
      await page.screenshot({path:join(out,'visuals',`${size.width}-${String(i+1).padStart(2,'0')}.png`),fullPage:false});}
    expect(pageErrors).toEqual([]);
  });
}
test('登录失败、刷新保持、失效会话、退出',async({page})=>{
  await page.goto('/');await page.getByTestId('login-user').fill('owner');await page.getByTestId('login-password').fill('WRONG-TEST-ONLY');await page.getByTestId('login-submit').click();await expect(page.getByTestId('login-submit')).toBeVisible();
  await login(page);await page.reload();await expect(page.getByTestId('logout')).toBeVisible();
  await page.evaluate(()=>sessionStorage.setItem('txbuddy.accessToken','invalid-expired-test-token'));await page.reload();await expect(page.getByTestId('login-submit')).toBeVisible();
  await login(page);await page.getByTestId('logout').click();await expect(page.getByTestId('login-submit')).toBeVisible();
});
test('项目创建查询切换、真实样本上传解析、清单148行与导出',async({page})=>{
  await login(page);const first=await create(page,'TEST_ONLY 首个项目');const second=await create(page,'TEST_ONLY 第二项目');
  await page.getByLabel('当前项目 Job ID').fill(first.job_id);await page.getByRole('button',{name:'载入',exact:true}).click();await expect(page.locator('.job-chip')).toContainText(first.name);
  await page.getByLabel('当前项目 Job ID').fill(second.job_id);await page.getByRole('button',{name:'载入',exact:true}).click();await expect(page.locator('.job-chip')).toContainText(second.name);
  await nav(page,'样本试点');await page.getByTestId('list-jobs').click();
  for(const [suffix,count] of [['土建_工程量清单.xlsx',50],['安装_工程量清单.xlsx',98]] as const){
    const path=join(fixture.boq_folder,readdirSync(fixture.boq_folder).find((f:string)=>f.endsWith(suffix))!);
    const sha=createHash('sha256').update(readFileSync(path)).digest('hex');await page.getByTestId('pilot-file').setInputFiles(path);await page.getByTestId('upload-file').click();await expect(page.getByTestId('upload-hash')).toContainText(sha);
    await page.getByTestId('parse-file').click();await expect(page.getByTestId('structured-result')).toContainText('header_mapping');
    const imported=page.waitForResponse(r=>r.url().includes('/v1/pilot/boq-import'));await page.getByTestId('import-sample-boq').click();const data=await(await imported).json();writeFileSync(join(out,`BOQ_${count}.json`),JSON.stringify(data,null,2));await expect(page.getByTestId('structured-result')).toContainText(String(count));
  }
  // PDF and DOCX use originals copied into this isolated session.
  function files(dir:string):string[]{return readdirSync(dir,{withFileTypes:true}).flatMap(d=>d.isDirectory()?files(join(dir,d.name)):[join(dir,d.name)]);}
  const available=files(fixture.sample_copies);
  for(const ext of ['.pdf','.docx']){const path=available.find(p=>p.toLowerCase().endsWith(ext));expect(path,ext+' fixture').toBeTruthy();await page.getByTestId('pilot-file').setInputFiles(path!);await page.getByTestId('upload-file').click();await expect(page.getByTestId('upload-hash')).toContainText(createHash('sha256').update(readFileSync(path!)).digest('hex'));await page.getByTestId('parse-file').click();await expect(page.getByTestId('structured-result')).not.toHaveText('');}
  await page.getByTestId('sample-flow').click();await expect(page.getByTestId('task-state')).toContainText('DONE');await page.getByTestId('generate-sample').click();await expect(page.getByTestId('structured-result')).toContainText('148');
  const download=page.waitForEvent('download');await page.getByTestId('download-sample').click();await(await download).saveAs(join(out,'TEST_ONLY_SAMPLE.xlsx'));
  await page.getByTestId('logout').click();await login(page,'UNAUTHORIZED');await page.getByLabel('当前项目 Job ID').fill(first.job_id);await page.getByRole('button',{name:'载入',exact:true}).click();await expect(page.locator('.settings')).toContainText('无权访问');
});
test('OCR真实页图、版本提交审批退回与角色拒绝',async({page})=>{
  async function open(role:string){await login(page,role);await nav(page,'OCR 人工复核');await page.getByTestId('ocr-search').fill('173');await page.getByTestId('ocr-refresh').click();await page.getByTestId('ocr-document').first().click();await expect(page.getByTestId('ocr-original-image')).toBeVisible();}
  await open('EDITOR');const original=await page.getByTestId('ocr-original-text').inputValue();expect(original.length).toBeGreaterThan(0);
  await page.getByTestId('ocr-corrected-text').fill(original+'\nTEST_ONLY 自动化工作流验证，不计真实人工复核');await page.getByTestId('ocr-fields').fill('{"test_only":true,"human_review":false}');await page.getByTestId('ocr-reason').fill('TEST_ONLY 技术验收');await page.getByTestId('ocr-save').click();await expect(page.getByTestId('ocr-saved')).toBeVisible();await page.getByTestId('ocr-submit').click();await expect(page.getByTestId('ocr-workflow-state')).toContainText('已提交版本');await expect(page.getByTestId('ocr-reviewed')).toBeDisabled();await page.getByTestId('logout').click();
  await open('REVIEWER');await page.getByTestId('ocr-human-confirm').check();await page.getByTestId('ocr-reason').fill('TEST_ONLY 自动化审批，不计真人复核');await page.getByTestId('ocr-reviewed').click();await expect(page.getByTestId('ocr-saved')).toBeVisible();await page.getByTestId('ocr-signoff').click();await expect(page.getByTestId('ocr-error')).toContainText('专业签认');await page.getByTestId('logout').click();
  await open('EDITOR');await page.getByTestId('ocr-reason').fill('TEST_ONLY 退回测试提交');await page.getByTestId('ocr-submit').click();await expect(page.getByTestId('ocr-workflow-state')).toContainText('已提交版本');await page.getByTestId('logout').click();
  await open('REVIEWER');await page.getByTestId('ocr-reason').fill('TEST_ONLY 退回原因');await page.getByTestId('ocr-return').click();await expect(page.getByTestId('ocr-saved')).toContainText('退回');await expect(page.getByTestId('ocr-history')).toContainText('TEST_ONLY');await page.getByTestId('ocr-page-number').selectOption({index:1});await expect(page.getByTestId('ocr-original-image')).toBeVisible();await page.getByTestId('logout').click();
  await open('VIEWER');await expect(page.getByTestId('ocr-save')).toBeDisabled();await expect(page.getByTestId('ocr-corrected-text')).toBeDisabled();
});
test('算量、定额、组价、复核、成果及证据真实接口',async({page})=>{
  await login(page);await create(page);await nav(page,'算量');await page.getByRole('button',{name:'固定算子 /v1/quantity/calc',exact:true}).click();await page.getByLabel('固定算子参数 params',{exact:false}).fill('{"length":"2","width":"3","height":"4","count":"1"}');
  const calculated=page.waitForResponse(r=>r.url().endsWith('/v1/quantity/calc'));await page.getByRole('button',{name:'执行固定算子',exact:true}).click();const calc=await(await calculated).json();expect(Number(calc.result)).toBe(24);expect(calc.evidence_id).toBeTruthy();
  await page.getByLabel('formal_calculation（正式）',{exact:true}).check();const denied=page.waitForResponse(r=>r.url().endsWith('/v1/quantity/calc'));await page.getByRole('button',{name:'执行固定算子',exact:true}).click();expect((await denied).status()).toBe(422);
  await page.getByLabel('formal_calculation（正式）',{exact:true}).uncheck();await page.getByRole('button',{name:'规则算量 /v1/quantity/rule-calc',exact:true}).click();const params=page.getByLabel('算量参数 params（变量名取自规则表达式，值必填）');await expect(params).not.toHaveValue('{}');const template=JSON.parse(await params.inputValue());await params.fill(JSON.stringify(Object.fromEntries(Object.keys(template).map(k=>[k,'2']))));const ruleResponse=page.waitForResponse(r=>r.url().endsWith('/v1/quantity/rule-calc'));await page.getByRole('button',{name:'执行规则算量',exact:true}).click();const ruleResult=await ruleResponse;expect(ruleResult.status()).toBe(200);expect((await ruleResult.json()).calc_run_id).toBeTruthy();
  await nav(page,'定额');await page.getByLabel('boq_item（清单项）').fill('[]');await page.getByRole('button',{name:'执行匹配',exact:true}).click();await expect(page.locator('main')).toContainText('必须是 JSON 对象');
  await page.getByLabel('consumption[]（消耗量）').fill('[{"resource_code":"TEST_ONLY","consumption":"2"}]');await page.getByLabel('notes[]（附注：FACTOR / REPLACE / FORBID / INFO）').fill('[]');await page.getByLabel('formal_calculation（正式）',{exact:true}).check();const quota=page.waitForResponse(r=>r.url().endsWith('/v1/quota/apply-notes'));await page.getByRole('button',{name:'执行换算',exact:true}).click();expect((await quota).status()).toBe(422);
  await nav(page,'组价');for(const [tab,button,path] of [['综合单价','计算综合单价','/v1/price/unit'],['指数调差','计算调差','/v1/price/index-adjust'],['项目汇总','计算项目总价','/v1/price/project-total']]){await page.getByRole('button',{name:tab,exact:true}).click();const r=page.waitForResponse(r=>r.url().endsWith(path));await page.getByRole('button',{name:button,exact:true}).click();expect((await r).status()).toBe(422);}
  await nav(page,'复核');await page.getByRole('button',{name:'判定',exact:true}).click();await expect(page.locator('main')).toContainText('FAIL');await page.getByRole('button',{name:'用最近 calc_run 填充',exact:true}).click();await page.getByRole('button',{name:'执行复算',exact:true}).click();await expect(page.locator('main')).toContainText('recomputed');
  await nav(page,'成果');await page.getByRole('button',{name:'刷新前置条件',exact:true}).click();const formal=page.waitForResponse(r=>r.url().endsWith('/generate'));await page.getByRole('button',{name:'检查正式生成条件',exact:true}).click();expect((await formal).status()).toBe(422);
  await nav(page,'证据');await page.getByRole('button',{name:'读取证据',exact:true}).click();await expect(page.getByLabel('evidence 对象（读取证据后自动填充，可粘贴）')).not.toHaveValue('');await page.getByRole('button',{name:'重新计算并比对摘要',exact:true}).click();await expect(page.locator('main')).toContainText('true');await page.getByRole('button',{name:'刷新审计流水',exact:true}).click();expect(pageErrors).toEqual([]);
});

test('定额匹配候选、附注换算与组价 TEST_ONLY 结果',async({page})=>{
  await login(page);await create(page,'TEST_ONLY 定额组价验证');
  await nav(page,'定额');
  // A complete candidate with an exact mapping must still be marked need_human.
  await page.getByLabel('boq_item（清单项）').fill(JSON.stringify({code:'A1',name:'现浇混凝土矩形柱',unit:'m3',major:'BUILDING',tags:['混凝土'],quantity:'2'}));
  await page.getByLabel('quota_items[]（定额子目候选）').fill(JSON.stringify([{id:'Q1',item_code:'Q-001',name:'现浇混凝土矩形柱',unit:'m3',major:'BUILDING',mapped_boq_code:'A1',work_tags:['混凝土'],source_ref:'TEST_ONLY-QUOTA',license_status:'TEST_ONLY',status:'ACTIVE',note_effects:[]} ]));
  await page.getByLabel('exact_mappings（清单编码 → 定额 item_code）').fill(JSON.stringify({A1:'Q-001'}));
  const match=page.waitForResponse(r=>r.url().endsWith('/v1/quota/match'));await page.getByRole('button',{name:'执行匹配',exact:true}).click();const matchResponse=await match;expect(matchResponse.status()).toBe(200);await expect(page.locator('.result-panel')).toContainText('need_human');
  // A sourced FACTOR note is executable in TEST_ONLY mode; formal=true remains gated.
  await page.getByLabel('consumption[]（消耗量）').fill(JSON.stringify([{resource_code:'R1',consumption:'2'}]));
  await page.getByLabel('notes[]（附注：FACTOR / REPLACE / FORBID / INFO）').fill(JSON.stringify([{id:'N1',scope_level:'ITEM',priority:100,effect_type:'FACTOR',target_resource_code:'R1',factor:'1.5',source_ref:'TEST_ONLY-NOTE'}]));
  await page.getByLabel('conversion（换算上下文）').fill(JSON.stringify({allowed:true}));
  const notes=page.waitForResponse(r=>r.url().endsWith('/v1/quota/apply-notes'));await page.getByRole('button',{name:'执行换算',exact:true}).click();const notesResponse=await notes;expect(notesResponse.status()).toBe(200);await expect(page.locator('.result-panel')).toContainText('3.0');
  await page.getByLabel('formal_calculation（正式）',{exact:true}).check();const formalNotes=page.waitForResponse(r=>r.url().endsWith('/v1/quota/apply-notes'));await page.getByRole('button',{name:'执行换算',exact:true}).click();expect((await formalNotes).status()).toBe(422);await page.getByLabel('formal_calculation（正式）',{exact:true}).uncheck();

  await nav(page,'组价');
  await page.getByLabel('resources[]（resource_type 仅 LABOR/MATERIAL/MACHINE）').fill(JSON.stringify([{resource_code:'M1',resource_type:'MATERIAL',consumption:'2',unit_price:'10',price_ref:'TEST_ONLY-PRICE'}]));
  await page.getByLabel('管理费率 management_rate').fill('0.1');await page.getByLabel('利润率 profit_rate').fill('0.05');await page.getByLabel('管理费依据 management_source_ref').fill('TEST_ONLY-MGMT');await page.getByLabel('利润依据 profit_source_ref').fill('TEST_ONLY-PROFIT');
  const unit=page.waitForResponse(r=>r.url().endsWith('/v1/price/unit'));await page.getByRole('button',{name:'计算综合单价',exact:true}).click();const unitResponse=await unit;expect(unitResponse.status()).toBe(200);await expect(page.locator('.result-panel')).toContainText('23.10');
  await page.getByRole('button',{name:'指数调差',exact:true}).click();await page.getByLabel('调差参数（mode: WITHIN_BAND_ONLY / FULL_EXCESS / PASS_THROUGH）').fill(JSON.stringify({base_amount:'1000',base_index:'100',current_index:'110',risk_rate:'0.05',mode:'FULL_EXCESS',contract_clause_ref:'TEST_ONLY-C-1'}));const index=page.waitForResponse(r=>r.url().endsWith('/v1/price/index-adjust'));await page.getByRole('button',{name:'计算调差',exact:true}).click();const indexResponse=await index;expect(indexResponse.status()).toBe(200);await expect(page.locator('.result-panel')).toContainText('100');
  await page.getByRole('button',{name:'项目汇总',exact:true}).click();await page.getByLabel('汇总参数（HENAN_2025 正式计价必须提供不可竞争安全生产措施费）').fill(JSON.stringify({boq_total:'100',measure_total:'10',other_total:'0',fee_items:[],safety_production_fee:{fee_item:'SAFETY_PRODUCTION_FEE',amount:'3',is_noncompetitive:true,source_ref:'TEST_ONLY-SAFETY'},tax_rate:'0.09'}));const total=page.waitForResponse(r=>r.url().endsWith('/v1/price/project-total'));await page.getByRole('button',{name:'计算项目总价',exact:true}).click();const totalResponse=await total;expect(totalResponse.status()).toBe(200);await expect(page.locator('.result-panel')).toContainText('123.17');
  await page.getByLabel('formal_calculation').check();const formalTotal=page.waitForResponse(r=>r.url().endsWith('/v1/price/project-total'));await page.getByRole('button',{name:'计算项目总价',exact:true}).click();expect((await formalTotal).status()).toBe(422);
});

test('任务审批与鉴定线影子运行保持后端门禁',async({page})=>{
  await login(page);await create(page,'TEST_ONLY 复核门禁验证');await nav(page,'复核');
  // No task is silently fabricated by the UI; an unknown task must remain a 404/422.
  await page.getByLabel('task_id').fill('task-does-not-exist');const missing=page.waitForResponse(r=>r.url().endsWith('/v1/task/task-does-not-exist'));await page.getByRole('button',{name:'读取任务',exact:true}).click();expect([403,404,422]).toContain((await missing).status());
  // L1 is intentionally rejected by the J-line endpoint and must never be reported as shadow success.
  const jStatus=page.waitForResponse(r=>r.url().includes('/v1/jgate/')&&!r.url().endsWith('/run'));await page.getByRole('button',{name:'读取 J0–J5 状态',exact:true}).click();expect((await jStatus).status()).toBe(422);
  // Gate check on a real L1 project is executed and shown as FAIL when prerequisites are absent.
  await page.getByRole('button',{name:'判定',exact:true}).click();await expect(page.locator('main')).toContainText('FAIL');
});
