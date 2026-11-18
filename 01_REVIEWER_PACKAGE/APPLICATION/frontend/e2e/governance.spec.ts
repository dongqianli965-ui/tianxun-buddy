import {test,expect} from '@playwright/test';
import {mkdirSync,writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';
const shots=resolve('../reports/v425/screenshots');mkdirSync(shots,{recursive:true});

test('OCR真实页图、校对存档、历史、拒绝签认、项目矩阵和跨项目权限',async({page,request})=>{
  const errors:string[]=[],http:{method:string,path:string,status:number}[]=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('response',r=>{const p=new URL(r.url()).pathname;if(p.includes('/governance/'))http.push({method:r.request().method(),path:p,status:r.status()});});
  async function login(user:string){await page.getByTestId('login-user').fill(user);await page.getByTestId('login-password').fill('E2E-TEST-ONLY-password');await page.getByTestId('login-submit').click();await expect(page.getByTestId('logout')).toBeVisible();}
  await page.goto('/');await login('alice');
  await page.locator('nav button').filter({hasText:'OCR 人工复核'}).click();await expect(page.getByTestId('ocr-total')).toContainText('40 份');
  await page.getByTestId('ocr-search').fill('173');await page.getByTestId('ocr-refresh').click();
  await page.getByTestId('ocr-document').filter({hasText:'官方PDF'}).first().click();
  await expect(page.getByTestId('ocr-original-image')).toBeVisible();await expect(page.getByTestId('ocr-original-text')).not.toHaveValue('');
  const original=await page.getByTestId('ocr-original-text').inputValue();const originalImage=await page.getByTestId('ocr-original-image').evaluate((e:HTMLImageElement)=>e.naturalWidth);
  expect(originalImage).toBeGreaterThan(100);await page.getByRole('button',{name:'放大原页'}).click();
  await page.screenshot({path:join(shots,'01-ocr-original.png'),fullPage:true});
  const revised=original+'\n【TEST_ONLY 浏览器校对操作验证，不构成真实专业复核】';
  await page.getByTestId('ocr-corrected-text').fill(revised);await page.getByTestId('ocr-fields').fill('{"test_only":true,"purpose":"浏览器接口验证"}');
  await page.getByTestId('ocr-reason').fill('TEST_ONLY 隔离运行库验证');await expect(page.getByTestId('ocr-diff')).toContainText('TEST_ONLY');
  const saving=page.waitForResponse(r=>r.url().endsWith('/reviews')&&r.request().method()==='POST');await page.getByTestId('ocr-save').click();const saved=await saving;expect(saved.status()).toBe(200);
  const revision=await saved.json();expect(revision.original_text).toBe(original);expect(revision.reviewer_id).toBe('alice');expect(revision.version).toBe(1);expect(revision.history_chain_valid).toBeTruthy();
  await expect(page.getByTestId('ocr-history')).toContainText('TEST_ONLY');await expect(page.getByTestId('ocr-version')).toHaveText('版本 1');
  await page.screenshot({path:join(shots,'02-ocr-saved-history.png'),fullPage:true});
  await page.getByTestId('ocr-signoff').click();await expect(page.getByTestId('ocr-error')).toContainText('真实专业人员');
  await page.screenshot({path:join(shots,'03-professional-signoff-denied.png'),fullPage:true});
  await page.getByTestId('ocr-page-number').selectOption({index:1});await expect(page.getByTestId('ocr-original-image')).toBeVisible();await expect(page.getByTestId('ocr-version')).toHaveText('版本 0');
  await page.getByTestId('ocr-page-number').selectOption({index:0});await expect(page.getByTestId('ocr-version')).toHaveText('版本 1');await expect(page.getByTestId('ocr-corrected-text')).toHaveValue(revised);
  await page.locator('nav button').filter({hasText:'资料完整性'}).click();await expect(page.getByTestId('readiness-count')).toContainText('18 套');
  await page.getByTestId('project-detail').first().click();await expect(page.getByTestId('readiness-detail')).toContainText('B_REFERENCE');await expect(page.getByTestId('project-matrix')).toContainText('BLOCKED');
  await page.getByTestId('source-search').fill('SRC-0155');await expect(page.getByTestId('source-status')).toContainText('OFFICIAL_BYTES_VERIFIED');await page.getByRole('button',{name:'SRC-0155',exact:true}).click();await expect(page.getByTestId('source-audit-detail')).toContainText('134b017f5afa1cf3');
  await page.screenshot({path:join(shots,'04-project-matrix-source.png'),fullPage:true});
  await page.getByTestId('logout').click();await login('mallory');
  await page.locator('nav button').filter({hasText:'OCR 人工复核'}).click();await expect(page.getByTestId('ocr-total')).toContainText('0 份');
  const token=await page.evaluate(()=>sessionStorage.getItem('txbuddy.accessToken'));const headers={Authorization:'Bearer '+token};
  for(const path of ['/v1/governance/ocr/pages/'+revision.page_id,'/v1/governance/ocr/pages/'+revision.page_id+'/image','/v1/governance/ocr/documents/'+revision.document_id,'/v1/governance/sources/SRC-0155','/v1/governance/projects?project_id=CORPUS-S01']){
    const response=await request.get(path,{headers});expect(response.status()).toBe(403);expect((await response.json()).detail.code).toBe('ACL_FORBIDDEN');
  }
  const denied=await request.post('/v1/governance/ocr/pages/'+revision.page_id+'/reviews',{headers,data:{expected_version:1,corrected_text:'forbidden',structured_fields:{},review_status:'REVIEWED',human_confirmed:true}});expect(denied.status()).toBe(403);
  await page.locator('nav button').filter({hasText:'资料完整性'}).click();await expect(page.getByTestId('readiness-count')).toContainText('0 套');
  await page.screenshot({path:join(shots,'05-cross-project-denied.png'),fullPage:true});expect(errors).toEqual([]);
  writeFileSync(resolve('../reports/v425/GOVERNANCE_E2E_EVIDENCE.json'),JSON.stringify({fixture:'TEST_ONLY disposable runtime; real source read-only',reviewed_source_files_in_real_runtime:0,requests:http,console_errors:errors,all_ten_scenarios:true,production_accepted:false},null,2));
});
