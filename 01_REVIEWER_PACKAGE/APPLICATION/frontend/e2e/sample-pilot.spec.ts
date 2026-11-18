import {test,expect} from '@playwright/test';
import {readdirSync,readFileSync,mkdirSync,writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';
import {createHash} from 'node:crypto';
const corpus=process.env.TXBUDDY_CORPUS_ROOT||resolve('../../REAL_DATA_CORPUS/HN_L1_V1_20261116');
const source=join(corpus,'05_REAL_PROJECTS/L1_BUDGET/L1_001/B_REFERENCE');
const shots=resolve('../reports/v424/screenshots');mkdirSync(shots,{recursive:true});

test('真实原件→结构化→清单→价格及勘误→来源→流程→导出→权限拒绝',async({page})=>{
  const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message));
  const requests:{method:string,path:string,status:number}[]=[];
  page.on('response',r=>{const u=new URL(r.url());if(/^\/(v1|files|projects|auth|health)/.test(u.pathname))requests.push({method:r.request().method(),path:u.pathname,status:r.status()});});
  async function login(user:string){await page.getByTestId('login-user').fill(user);await page.getByTestId('login-password').fill('E2E-TEST-ONLY-password');await page.getByTestId('login-submit').click();await expect(page.getByTestId('logout')).toBeVisible();}
  await page.goto('/');await page.screenshot({path:join(shots,'01-login.png'),fullPage:true});await login('alice');
  await page.locator('nav button').filter({hasText:'项目'}).click();
  await page.getByLabel('项目编号',{exact:true}).fill('E2E-'+Date.now());await page.getByLabel('项目名称',{exact:true}).fill('L1_001 真实清单导入试点');
  const created=page.waitForResponse(r=>r.url().endsWith('/v1/job')&&r.request().method()==='POST');
  await page.getByRole('button',{name:'创建项目',exact:true}).click();const response=await created;expect(response.status()).toBe(200);const job=await response.json();
  await expect(page.locator('.job-chip')).toContainText(job.name);await page.locator('nav button').filter({hasText:'样本试点'}).click();
  await page.getByTestId('list-jobs').click();await expect(page.getByTestId('upload-file')).toBeDisabled();
  for(const suffix of ['土建_工程量清单.xlsx','安装_工程量清单.xlsx']){
    const path=join(source,readdirSync(source).find(x=>x.endsWith(suffix))!);const sha=createHash('sha256').update(readFileSync(path)).digest('hex');
    await page.getByTestId('pilot-file').setInputFiles(path);await page.getByTestId('upload-file').click();await expect(page.getByTestId('upload-hash')).toContainText(sha);
    await page.getByTestId('parse-file').click();await expect(page.getByTestId('structured-result')).toContainText('header_mapping');
    await page.getByTestId('import-sample-boq').click();await expect(page.getByTestId('structured-result')).toContainText('B_REFERENCE');
  }
  await page.screenshot({path:join(shots,'02-upload-structure.png'),fullPage:true});
  const indexFolder=join(corpus,'03_HENAN_FEE_RULES/2026_INDEX');const scanned=join(indexFolder,readdirSync(indexFolder).find(x=>x.includes('23')&&x.toLowerCase().endsWith('.pdf'))!);
  const scanSha=createHash('sha256').update(readFileSync(scanned)).digest('hex');await page.getByTestId('pilot-file').setInputFiles(scanned);await page.getByTestId('upload-file').click();await expect(page.getByTestId('upload-hash')).toContainText(scanSha);
  await page.getByTestId('offline-ocr').click();await expect(page.getByTestId('structured-result')).toContainText('NEEDS_EXPERT_REVIEW',{timeout:60000});
  await page.screenshot({path:join(shots,'02b-offline-ocr.png'),fullPage:true});
  await page.getByTestId('query-prices').click();await expect(page.getByTestId('price-results')).toContainText('492.34');await expect(page.getByTestId('price-results')).toContainText('已勘误');
  await page.getByRole('button',{name:'查看来源',exact:true}).first().click();await expect(page.getByTestId('source-detail')).toContainText('sha256');await page.screenshot({path:join(shots,'03-price-source.png'),fullPage:true});
  await page.getByTestId('price-region').selectOption('410300');await page.getByTestId('query-prices').click();await expect(page.getByTestId('pilot-error')).toContainText('其他地区缺少依据');
  await page.getByTestId('price-region').selectOption('410100-INNER-4RING');await page.getByTestId('price-period').selectOption('2026-Q4');await page.getByTestId('query-prices').click();await expect(page.getByTestId('pilot-error')).toContainText('缺少数据');
  await page.getByTestId('sample-flow').click();await expect(page.getByTestId('task-state')).toContainText('DONE');await page.getByTestId('generate-sample').click();await expect(page.getByTestId('structured-result')).toContainText('148');
  const downloadEvent=page.waitForEvent('download');await page.getByTestId('download-sample').click();const download=await downloadEvent;await download.saveAs(resolve('../reports/v424/E2E_SAMPLE_RESULT.xlsx'));
  await page.screenshot({path:join(shots,'04-flow-export.png'),fullPage:true});
  await page.getByTestId('logout').click();await expect(page.getByTestId('login-submit')).toBeVisible();await login('mallory');
  await page.getByLabel('当前项目 Job ID').fill(job.job_id);await page.getByRole('button',{name:'载入',exact:true}).click();await expect(page.locator('.settings')).toContainText('无权访问该项目');
  await page.screenshot({path:join(shots,'05-permission-denied.png'),fullPage:true});await page.getByTestId('logout').click();
  expect(errors).toEqual([]);writeFileSync(resolve('../reports/v424/E2E_HTTP_EVIDENCE.json'),JSON.stringify({job_id:job.job_id,requests,console_errors:errors,export_rows:148,accuracy_verification:'BLOCKED'},null,2));
});
