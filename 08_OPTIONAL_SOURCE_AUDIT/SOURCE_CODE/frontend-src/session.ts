/** 全局会话：后端地址 / 操作者 / 当前项目 / 依据包，全部与真实后端同步。 */
import { reactive } from "vue";
import { api } from "./api";
import {
  getActor, getApiBase, getJobId, rememberJob, setActor, setApiBase, setJobId,
} from "./config";
import type { BasisPack, Health, Job } from "./types";

const K_PACK = "txbuddy.basisPack";

/** 默认依据包是一份 TEST_ONLY 包：formal_calculation=true 时后端会如实拒绝，前端不伪造授权。 */
export const DEFAULT_PACK: BasisPack = {
  basis_pack_id: "B-WEB-TEST",
  region_code: "410100",
  region_level: "CITY",
  major: "BUILDING",
  effective_date: "2026-01-01",
  measurement_standard_version: "GB/T 50854-2024",
  pricing_standard_version: "GB/T 50500-2024",
  quota_pack_version: null,
  rule_version_scope: "HENAN_2025",
  precision_rule_version: "P-V1",
  status: "DRAFT",
  test_only: true,
  sources: [{
    source_id: "S-WEB-TEST",
    title: "前端联调用测试依据（非授权）",
    source_type: "TEST",
    license_status: "TEST_ONLY",
    verified: false,
  }],
};

function readPack(): BasisPack {
  try {
    const raw = localStorage.getItem(K_PACK);
    if (!raw) return { ...DEFAULT_PACK };
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed as BasisPack : { ...DEFAULT_PACK };
  } catch { return { ...DEFAULT_PACK }; }
}

export const session = reactive({
  page: "project",
  apiBase: getApiBase(),
  actor: getActor(),
  jobId: getJobId(),
  job: null as Job | null,
  jobError: "",
  health: null as Health | null,
  healthError: "",
  healthCheckedAt: "",
  pack: readPack(),
  loadingJob: false,
  lastEvidenceId: "",
  lastCalcRunId: "",
});

function persistPack(): void {
  try { localStorage.setItem(K_PACK, JSON.stringify(session.pack)); } catch { /* ignore */ }
}

export function setPack(pack: BasisPack): void {
  session.pack = pack;
  persistPack();
}

export function setPackJson(text: string): string {
  try {
    const parsed = JSON.parse(text);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return "依据包必须是 JSON 对象";
    session.pack = parsed as BasisPack;
    persistPack();
    return "";
  } catch (e) {
    return "依据包 JSON 解析失败：" + (e instanceof Error ? e.message : String(e));
  }
}

export async function refreshHealth(): Promise<void> {
  session.healthError = "";
  try {
    session.health = await api.health();
  } catch (e) {
    session.health = null;
    session.healthError = e instanceof Error ? e.message : String(e);
  } finally {
    session.healthCheckedAt = new Date().toLocaleTimeString("zh-CN");
  }
}

export async function loadJob(jobId?: string): Promise<Job | null> {
  const id = (jobId ?? session.jobId).trim();
  if (!id) { clearCurrentJob(); session.jobError = "尚未选择项目"; return null; }
  session.loadingJob = true;
  session.jobError = "";
  try {
    const job = await api.getJob(id);
    session.jobId = id;
    setJobId(id);
    session.job = job;
    rememberJob(job);
    return job;
  } catch (e) {
    session.job = null;
    session.jobError = e instanceof Error ? e.message : String(e);
    return null;
  } finally {
    session.loadingJob = false;
  }
}

export function useJob(job: Job): void {
  session.job = job;
  session.jobId = job.job_id;
  setJobId(job.job_id);
  rememberJob(job);
}

/** 登出/失效时清除活动项目，防止下个身份继承上个身份的操作上下文。 */
export function clearCurrentJob(): void {
  session.job = null;
  session.jobId = "";
  session.jobError = "";
  session.lastEvidenceId = "";
  session.lastCalcRunId = "";
  setJobId("");
  setBoqItem(null);
}

export function applyApiBase(value: string): void {
  setApiBase(value);
  session.apiBase = getApiBase();
}

export function applyActor(value: string): void {
  setActor(value);
  session.actor = getActor();
}

/** 请求体里统一附带当前 job_id / 依据包；basis_pack 必须整包发送（extra=forbid）。 */
export function withJob(body: Record<string, unknown>): Record<string, unknown> {
  return { ...body, job_id: session.jobId || null, basis_pack: session.pack };
}

const K_BOQ = "txbuddy.boqItem";

export function go(page: string): void {
  session.page = page;
}

export function rememberEvidence(evidenceId: string): void {
  session.lastEvidenceId = evidenceId;
}

export function rememberCalcRun(calcRunId: string): void {
  session.lastCalcRunId = calcRunId;
  session.lastEvidenceId = calcRunId;
}

/** 清单→定额的页面间传参：只传用户从真实导入结果中选中的那一行。 */
export function setBoqItem(item: Record<string, unknown> | null): void {
  try {
    if (item) localStorage.setItem(K_BOQ, JSON.stringify(item));
    else localStorage.removeItem(K_BOQ);
  } catch { /* ignore */ }
}

export function getBoqItem(): Record<string, unknown> | null {
  try {
    const raw = localStorage.getItem(K_BOQ);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed as Record<string, unknown> : null;
  } catch { return null; }
}
