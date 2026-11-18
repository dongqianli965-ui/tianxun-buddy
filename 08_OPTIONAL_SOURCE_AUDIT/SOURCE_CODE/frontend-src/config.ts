/** 运行期配置：全部落 localStorage，后端地址可在页头随时切换（构建产物不写死）。 */
import type { Job } from "./types";

const K_BASE = "txbuddy.apiBase";
const K_ACTOR = "txbuddy.actor";
const K_JOB = "txbuddy.jobId";
const K_RECENT = "txbuddy.recentJobs";

const env = import.meta.env as Record<string, string | undefined>;
const servedOverHttp = typeof location !== "undefined" && /^https?:$/.test(location.protocol);
// 经 vite dev/preview 打开时默认走同源代理（/v1、/health → 127.0.0.1:8010），避免后端无 CORS 时浏览器拦截；
// 以 file:// 或其它静态服务器打开时直连后端，地址可在页头修改。
const DEFAULT_BASE = (env.VITE_API_BASE ?? (servedOverHttp ? "" : "http://127.0.0.1:8010"))
  .replace(/\/+$/, "");

function read(key: string): string {
  try { return localStorage.getItem(key) || ""; } catch { return ""; }
}
function write(key: string, value: string): void {
  try { localStorage.setItem(key, value); } catch { /* 隐私模式下忽略 */ }
}

export function getApiBase(): string {
  return (read(K_BASE) || DEFAULT_BASE).replace(/\/+$/, "");
}
export function setApiBase(value: string): void {
  const v = value.trim().replace(/\/+$/, "");
  if (v) write(K_BASE, v); else write(K_BASE, "");
}
export function defaultApiBase(): string { return DEFAULT_BASE; }

export function getActor(): string { return read(K_ACTOR) || "web"; }
export function setActor(value: string): void { write(K_ACTOR, value.trim() || "web"); }

export function getJobId(): string { return read(K_JOB); }
export function setJobId(value: string): void {
  const jobId = value.trim();
  if (!jobId) {
    try { localStorage.removeItem(K_JOB); } catch { /* 隐私模式下忽略 */ }
    return;
  }
  write(K_JOB, jobId);
}

export interface RecentJob {
  job_id: string;
  project_id: string;
  name: string;
  business_line: string;
  at: string;
}

export function listRecentJobs(): RecentJob[] {
  try {
    const raw = JSON.parse(read(K_RECENT) || "[]");
    return Array.isArray(raw) ? raw.filter((x) => x && typeof x.job_id === "string") : [];
  } catch { return []; }
}

/** Recent project labels are identity-scoped; clear the local index at logout/session expiry. */
export function clearRecentJobs(): void {
  try { localStorage.removeItem(K_RECENT); } catch { /* 隐私模式下忽略 */ }
}

/** 本机最近项目索引：仅缓存 job_id 与标题，项目事实始终由 GET /v1/job/{id} 读取。 */
export function rememberJob(job: Job): void {
  if (!job?.job_id) return;
  const rest = listRecentJobs().filter((x) => x.job_id !== job.job_id);
  const entry: RecentJob = {
    job_id: job.job_id,
    project_id: String(job.project_id || ""),
    name: String(job.name || ""),
    business_line: String(job.business_line || ""),
    at: new Date().toISOString(),
  };
  write(K_RECENT, JSON.stringify([entry, ...rest].slice(0, 20)));
}
