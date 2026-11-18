import { defineConfig, loadEnv } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig(({mode})=>{
  const target=loadEnv(mode,'.','').TXBUDDY_API_TARGET||'http://127.0.0.1:8010';
  const proxy=Object.fromEntries(['/auth','/files','/projects','/v1','/health','/internal'].map(p=>[p,target]));
  return {
  plugins: [vue()],
  // 相对路径产物：dist/index.html 可直接从任意静态目录打开（API 地址仍可运行期配置）。
  base: "./",
  server: { port: 5173, proxy },
  preview: { port: 4173, proxy },
  };
});
