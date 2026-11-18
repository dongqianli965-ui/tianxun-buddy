import {test,expect} from '@playwright/test';
test('登录错误、会话保持、真实失效和登出',async({page})=>{
  await page.goto('/');await page.getByTestId('login-user').fill('alice');await page.getByTestId('login-password').fill('wrong-password');await page.getByTestId('login-submit').click();
  await expect(page.getByRole('alert')).toContainText('用户名或口令错误');
  await page.getByTestId('login-password').fill('E2E-TEST-ONLY-password');await page.getByTestId('login-submit').click();await expect(page.getByTestId('logout')).toBeVisible();
  await page.reload();await expect(page.getByTestId('logout')).toBeVisible();
  await page.evaluate(()=>sessionStorage.setItem('txbuddy.accessToken','invalid-expired-token'));await page.reload();await expect(page.getByTestId('login-submit')).toBeVisible();await expect(page.getByRole('alert')).toContainText('会话已失效');
  await page.getByTestId('login-password').fill('E2E-TEST-ONLY-password');await page.getByTestId('login-user').fill('alice');await page.getByTestId('login-submit').click();await expect(page.getByTestId('logout')).toBeVisible();await page.getByTestId('logout').click();await expect(page.getByTestId('login-submit')).toBeVisible();
});
