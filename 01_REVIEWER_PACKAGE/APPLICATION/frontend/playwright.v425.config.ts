import {defineConfig} from '@playwright/test';
export default defineConfig({
  testDir:'./e2e',testMatch:'*.spec.ts',workers:1,retries:0,timeout:180000,expect:{timeout:30000},
  reporter:[['list'],['html',{outputFolder:'../reports/v425/playwright-report',open:'never'}],['json',{outputFile:'../reports/v425/playwright-results.json'}]],
  outputDir:'../reports/v425/playwright-artifacts',
  use:{baseURL:'http://127.0.0.1:4186',channel:'chrome',headless:true,viewport:{width:1600,height:1150},screenshot:'only-on-failure',trace:'retain-on-failure'},
  webServer:{command:'node e2e/server-v425.mjs',url:'http://127.0.0.1:4186/health',reuseExistingServer:false,timeout:120000},
});
