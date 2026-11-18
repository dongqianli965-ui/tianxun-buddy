import { test, expect, type Page, type Route } from '@playwright/test';

type Json = Record<string, unknown>;

const health = {
  status: 'ok', service: 'txbuddy-core', version: '4.2.6', rules_loaded: 12,
  precision: 'P-V1', environment: 'TEST_ONLY', test_only: true,
  production_accepted: false, accuracy_E: 'BLOCKED',
};

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function installShellMocks(page: Page): Promise<void> {
  await page.route('**/health', route => json(route, health));
  await page.route('**/auth/login', route => json(route, { access_token: 'TEST_ONLY_UI_TOKEN' }));
  await page.route('**/auth/me', route => json(route, { username: 'owner', roles: ['owner'] }));
  await page.route('**/auth/logout', route => json(route, { status: 'ok' }));
}

async function login(page: Page, username = 'owner'): Promise<void> {
  await page.getByTestId('login-user').fill(username);
  await page.getByTestId('login-password').fill('TEST_ONLY_UI_PASSWORD');
  await page.getByTestId('login-submit').click();
  await expect(page.getByTestId('logout')).toBeVisible();
}

test('人工验收壳层显示真实环境与请求观察状态', async ({ page }) => {
  const pageErrors: string[] = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await installShellMocks(page);
  await page.route('**/v1/jobs', route => json(route, { detail: { code: 'TEST_ONLY_LIST_FAILURE', message: '测试列表失败' } }, 503));

  await page.goto('/');
  await expect(page.getByTestId('login-version')).toContainText('V4.2.6');
  await login(page);
  await expect(page.getByTestId('runtime-environment')).toContainText('TEST_ONLY');
  await expect(page.getByTestId('frontend-version')).toContainText('V4.2.6');
  await expect(page.getByTestId('current-identity')).toContainText('owner');

  await page.locator('nav button').filter({ hasText: '项目' }).click();
  await expect(page.getByTestId('project-list-error')).toContainText('HTTP 503');
  await expect(page.getByTestId('project-list-error')).toContainText('测试列表失败');

  await page.locator('.registry summary').click();
  await expect(page.locator('.registry')).toContainText('已登记接线');
  await expect(page.locator('.registry')).toContainText('HTTP 503');
  await expect(page.getByTestId('endpoint-probe-count')).toContainText('本次已请求');
  await expect(page.getByText('请求观察值，不代表完整可用率')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.setViewportSize({ width: 1920, height: 1080 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  expect(pageErrors).toEqual([]);
});

test('项目创建后同步 Job ID，并在登出时清理活动项目', async ({ page }) => {
  await installShellMocks(page);
  const job: Json = {
    job_id: 'job_manual_ui_1', project_id: 'P-MANUAL-UI', name: '人工验收项目',
    business_line: 'L1', business_line_label: '房建', status: 'DRAFT', basis_pack_id: 'B-WEB-TEST',
    documents: [], tasks: [], gate_records: [], calc_runs: [],
  };
  await page.route('**/v1/jobs', route => json(route, { items: [] }));
  await page.route('**/v1/job', async route => {
    if (route.request().method() === 'POST') return json(route, job);
    return route.continue();
  });
  await page.route('**/v1/job/job_manual_ui_1', route => json(route, job));
  await page.route('**/projects/P-MANUAL-UI', route => json(route, { project_id: 'P-MANUAL-UI', role: 'owner' }));

  await page.goto('/');
  await login(page);
  await page.locator('nav button').filter({ hasText: '项目' }).click();
  await page.getByLabel('项目编号', { exact: true }).fill('P-MANUAL-UI');
  await page.getByLabel('项目名称', { exact: true }).fill('人工验收项目');
  await page.getByRole('button', { name: '创建项目', exact: true }).click();
  await expect(page.locator('.job-chip')).toContainText('人工验收项目');
  await expect(page.getByLabel('当前项目 Job ID')).toHaveValue('job_manual_ui_1');
  await expect(page.locator('.job-chip')).toContainText('owner');

  await page.getByTestId('logout').click();
  await expect(page.getByTestId('login-submit')).toBeVisible();
  await expect.poll(async () => page.evaluate(() => localStorage.getItem('txbuddy.jobId'))).toBeNull();
  await expect.poll(async () => page.evaluate(() => localStorage.getItem('txbuddy.recentJobs'))).toBeNull();
});

test('无效材料条件清空旧价格并保留可理解错误', async ({ page }) => {
  await installShellMocks(page);
  let queryCount = 0;
  await page.route('**/v1/jobs', route => json(route, { items: [] }));
  await page.route('**/v1/material-prices**', async route => {
    queryCount += 1;
    if (queryCount === 1) {
      return json(route, { total: 1, items: [{
        record_id: 'price-1', material_name: '沥青混凝土', spec_model: 'AC-13', unit: 't',
        price_incl_tax: 492.34, price_excl_tax: 453.52, scope_status: 'VALID', version_history: [{}],
      }] });
    }
    return json(route, { detail: { code: 'NO_PRICE_BASIS', message: '其他地区缺少依据' } }, 422);
  });

  await page.goto('/');
  await login(page);
  await page.locator('nav button').filter({ hasText: '样本试点' }).click();
  await page.getByTestId('query-prices').click();
  await expect(page.getByTestId('price-results')).toContainText('沥青混凝土');

  await page.getByTestId('price-region').selectOption('410300');
  await page.getByTestId('query-prices').click();
  await expect(page.getByTestId('pilot-error')).toContainText('其他地区缺少依据');
  await page.locator('.registry summary').click();
  await expect(page.locator('.registry')).toContainText('HTTP 422');
  await expect(page.getByTestId('price-results')).not.toContainText('沥青混凝土');
  await expect(page.getByTestId('price-results')).toContainText('错误条件不会显示上次价格');
});
