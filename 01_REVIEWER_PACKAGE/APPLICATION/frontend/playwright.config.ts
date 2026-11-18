import {defineConfig} from '@playwright/test';
export default defineConfig({
  testDir:'./e2e',testMatch:'*.spec.ts',workers:1,retries:0,timeout:120000,
  reporter:[['list'],['html',{outputFolder:'../reports/v424/playwright-report',open:'never'}],['json',{outputFile:'../reports/v424/playwright-results.json'}]],
  outputDir:'../reports/v424/playwright-artifacts',
  use:{baseURL:'http://127.0.0.1:4173',channel:'chrome',headless:true,viewport:{width:1440,height:1100},screenshot:'only-on-failure',trace:'retain-on-failure'},
  webServer:{command:'node e2e/server.mjs',url:'http://127.0.0.1:4173/health',reuseExistingServer:false,timeout:60000},
});
