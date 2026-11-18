import {test,expect} from '@playwright/test';
import {mkdirSync,writeFileSync} from 'node:fs';
import {resolve,join} from 'node:path';

test('OCR角色隔离：编辑提交、复核通过与退回，绑定准确内容',async({page,request})=>{
  const evidence:any[]=[];const directory=resolve('../reports/v426/screenshots');mkdirSync(directory,{recursive:true});
  async function login(user:string){
    await page.getByTestId('login-user').fill(user);await page.getByTestId('login-password').fill('E2E-TEST-ONLY-password');
    await page.getByTestId('login-submit').click();await expect(page.getByTestId('logout')).toBeVisible();
    await page.locator('nav button').filter({hasText:'OCR 人工复核'}).click();
    await page.getByTestId('ocr-search').fill('173');await page.getByTestId('ocr-refresh').click();
    await page.getByTestId('ocr-document').filter({hasText:'官方PDF'}).first().click();
    await page.getByTestId('ocr-page-number').selectOption({index:2});
    await expect(page.getByTestId('ocr-original-image')).toBeVisible();
  }
  async function action(button:string,suffix:string){
    const waiting=page.waitForResponse(r=>r.url().endsWith(suffix)&&r.request().method()==='POST');
    await page.getByTestId(button).click();const response=await waiting;
    expect(response.status()).toBe(200);const body=await response.json();
    evidence.push({suffix,status:response.status(),page_id:body.page_id,version:body.version,
      content_sha256:body.content_sha256,submitted_version:body.submitted_version,review_status:body.review_status});return body;
  }
  await page.goto('/');await login('editor');
  await expect(page.getByTestId('ocr-corrected-text')).toBeEnabled();await expect(page.getByTestId('ocr-reviewed')).toBeDisabled();
  const revised='TEST_ONLY 合成校对工作流验证。仅隔离数据库中的自动化操作，不构成真实人员复核。';
  await page.getByTestId('ocr-corrected-text').fill(revised);await page.getByTestId('ocr-fields').fill(JSON.stringify({test_only:true}));
  await page.getByTestId('ocr-reason').fill('TEST_ONLY 编辑草稿');await action('ocr-save','/reviews');
  const submitted=await action('ocr-submit','/submit');expect(submitted.workflow_state).toBe('SUBMITTED');
  const token=await page.evaluate(()=>sessionStorage.getItem('txbuddy.accessToken'));
  const denial=await request.post('/v1/governance/ocr/pages/'+submitted.page_id+'/decisions',{
    headers:{Authorization:'Bearer '+token},data:{expected_version:submitted.version,submitted_version:submitted.submitted_version,
      expected_content_sha256:submitted.content_sha256,decision:'APPROVE',human_confirmed:true,reason:'TEST_ONLY 越权请求'}});
  expect(denial.status()).toBe(403);evidence.push({action:'editor_forged_approval',status:denial.status()});
  await page.screenshot({path:join(directory,'editor-submit.png'),fullPage:true});
  await page.getByTestId('logout').click();await login('reviewer');
  await expect(page.getByTestId('ocr-corrected-text')).toBeDisabled();await expect(page.getByTestId('ocr-save')).toBeDisabled();
  await expect(page.getByTestId('ocr-reviewed')).toBeEnabled();await page.getByTestId('ocr-human-confirm').check();
  await page.getByTestId('ocr-reason').fill('TEST_ONLY 浏览器角色工作流，非真实专业复核');
  const approved=await action('ocr-reviewed','/decisions');expect(approved.review_status).toBe('REVIEWED');
  expect(approved.corrected_text).toBe(revised);expect(approved.content_sha256).toBe(submitted.content_sha256);
  await page.screenshot({path:join(directory,'reviewer-approved.png'),fullPage:true});
  await page.getByTestId('logout').click();await login('editor');
  await page.getByTestId('ocr-corrected-text').fill(revised+' TEST_ONLY 第二次草稿');await action('ocr-save','/reviews');
  await expect(page.getByTestId('ocr-workflow-state')).toContainText('修改须保存后提交');await action('ocr-submit','/submit');
  await page.getByTestId('logout').click();await login('reviewer');
  await page.getByTestId('ocr-reason').fill('TEST_ONLY 请核对第二次草稿中的数字和单位');
  const returned=await action('ocr-return','/decisions');expect(returned.review_status).toBe('RETURNED');
  expect(returned.change_history.at(-1).reason).toContain('数字和单位');
  await page.getByTestId('logout').click();await login('viewer');
  await expect(page.getByTestId('ocr-corrected-text')).toBeDisabled();await expect(page.getByTestId('ocr-save')).toBeDisabled();
  await expect(page.getByTestId('ocr-reviewed')).toBeDisabled();await expect(page.getByTestId('ocr-return')).toBeDisabled();
  await page.screenshot({path:join(directory,'viewer-read-only.png'),fullPage:true});
  writeFileSync(resolve('../reports/v426/OCR_ROLE_BROWSER_EVIDENCE.json'),JSON.stringify({
    fixture:'TEST_ONLY disposable browser runtime',human_review_of_real_documents:false,production_accepted:false,evidence},null,2));
});
