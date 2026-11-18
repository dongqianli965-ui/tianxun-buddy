/** 真实后端 API 客户端：所有方法都对应 backend/txbuddy/api.py 的已实现路由。 */
import { getActor, getApiBase } from "./config";
import {auth,clearAuth} from './auth';
import { observeHttp } from './diagnostics';
import type {
  AuditItem, BasisPack, CalcRun, EvidenceRecord, Health, Job, Json, Rule, RuleCalcResult,
} from "./types";

interface RequestOptions {
  body?: unknown;
  idem?: string;
  signal?: AbortSignal;
  timeoutMs?: number;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: unknown;
  readonly method: string;
  readonly path: string;

  constructor(method: string, path: string, status: number, code: string, message: string, detail: unknown) {
    super(message);
    this.name = "ApiError";
    this.method = method;
    this.path = path;
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

function detailParts(body: unknown): { code: string; message: string } {
  const b = body as Json | null;
  const detail = b && typeof b === "object" ? (b as Json).detail : undefined;
  if (Array.isArray(detail)) {
    const first = detail[0] as Json | undefined;
    const loc = Array.isArray(first?.loc) ? first?.loc.join(".") : "";
    return {
      code: String(first?.type || "VALIDATION_ERROR"),
      message: `${loc ? loc + "：" : ""}${String(first?.msg || "请求字段校验失败")}`,
    };
  }
  if (detail && typeof detail === "object") {
    const d = detail as Json;
    return {
      code: String(d.code || d.error_code || ""),
      message: String(d.message || d.detail || ""),
    };
  }
  if (typeof detail === "string") return { code: "", message: detail };
  return {
    code: String((b && (b as Json).code) || ""),
    message: String((b && (b as Json).message) || ""),
  };
}

async function request<T>(method: string, path: string, opts: RequestOptions = {}): Promise<T> {
  const base = getApiBase();
  const started = performance.now();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), opts.timeoutMs ?? 30000);
  const headers: Record<string, string> = { "X-Actor": getActor() };
  if(auth.token) headers['Authorization']='Bearer '+auth.token;
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (opts.idem) headers["X-Idem-Key"] = opts.idem;
  if (opts.signal) {
    opts.signal.addEventListener("abort", () => controller.abort(), { once: true });
  }
  let res: Response;
  try {
    res = await fetch(base + path, {
      method,
      headers,
      body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      signal: controller.signal,
    });
  } catch (e) {
    clearTimeout(timer);
    observeHttp(method, path, 0, started);
    const target = base || (typeof location !== "undefined" ? `${location.origin}（同源代理）` : "同源代理");
    const reason = e instanceof DOMException && e.name === "AbortError"
      ? `请求超时（${opts.timeoutMs ?? 30000}ms）`
      : `无法连接后端 ${target}（请启动 uvicorn txbuddy.api:app --port 8010；跨域直连需后端开启 CORS）`;
    throw new ApiError(method, path, 0, "NETWORK_ERROR", reason, String(e));
  }
  clearTimeout(timer);
  observeHttp(method, path, res.status, started);
  const text = await res.text();
  let body: unknown = text;
  try { body = text ? JSON.parse(text) : null; } catch { /* 保留文本 */ }
  if (!res.ok) {
    if(res.status===401 && path!='/auth/login') clearAuth('会话已失效，请重新登录');
    const parts = detailParts(body);
    throw new ApiError(
      method, path, res.status,
      parts.code || `HTTP_${res.status}`,
      parts.message || res.statusText || `HTTP ${res.status}`,
      body,
    );
  }
  return body as T;
}

const get = <T>(path: string, opts?: RequestOptions) => request<T>("GET", path, opts);
const post = <T>(path: string, body?: unknown, opts?: RequestOptions) =>
  request<T>("POST", path, { ...opts, body: body ?? {} });

export const api = {
  login:(username:string,password:string)=>post<{access_token:string}>('/auth/login',{username,password}),
  testAutoLogin:(username:string,bootstrap:string)=>get<{access_token:string}>(`/auth/test-auto-login?username=${encodeURIComponent(username)}&bootstrap=${encodeURIComponent(bootstrap)}`),
  logout:()=>post<Json>('/auth/logout'),
  me:()=>get<Json>('/auth/me'),
  jobs:()=>get<Json>('/v1/jobs'),
  parseFile:(body:Json)=>post<Json>('/v1/parse',body,{timeoutMs:body.ocr?600000:30000}),
  importSampleBoq:(body:Json)=>post<Json>('/v1/pilot/boq-import',body),
  materialPrices:(filters:Record<string,string>)=>get<Json>('/v1/material-prices?'+new URLSearchParams(filters)),
  source:(id:string)=>get<Json>('/v1/sources/'+encodeURIComponent(id)),
  projectAcl:(id:string)=>get<Json>('/projects/'+encodeURIComponent(id)),
  generateDeliverable:(id:string,body:Json)=>post<Json>('/v1/deliverables/'+encodeURIComponent(id)+'/generate',body),
  deliverables:(id:string)=>get<Json>('/v1/deliverables/'+encodeURIComponent(id)),
  base: getApiBase,

  health: () => get<Health>("/health"),
  precision: () => get<Json>("/v1/precision"),
  rules: () => get<{ count: number; rules: Rule[] }>("/v1/rules"),
  validateRules: () => post<Json>("/v1/rules/validate"),
  validateBasis: (pack: BasisPack) => post<Json>("/v1/basis/validate", pack),

  createJob: (body: {
    project_id: string; name: string; business_line: string; basis_pack: BasisPack; created_by: string;
  }) => post<Job>("/v1/job", body),
  getJob: (jobId: string) => get<Job>(`/v1/job/${encodeURIComponent(jobId)}`),
  addDocument: (jobId: string, body: {
    doc_key: string; version: string; file_sha256: string; verified: boolean;
  }) => post<{ job_id: string; documents: unknown[] }>(`/v1/job/${encodeURIComponent(jobId)}/documents`, body),
  getRecords: (jobId: string, kind: string) =>
    get<{ job_id: string; kind: string; records: Json[] }>(
      `/v1/job/${encodeURIComponent(jobId)}/records?kind=${encodeURIComponent(kind)}`),
  putRecord: (jobId: string, kind: string, payload: Json) =>
    post<Json>(`/v1/job/${encodeURIComponent(jobId)}/records`, { kind, payload }),
  createTask: (jobId: string, node: string, payload: Json = {}, assignee = getActor()) =>
    post<Json>(`/v1/job/${encodeURIComponent(jobId)}/tasks`, { node, payload, assignee }),
  getTask: (taskId: string) => get<Json>(`/v1/task/${encodeURIComponent(taskId)}`),
  taskAction: (taskId: string, action: string, body: Json = {}) =>
    post<Json>(`/v1/task/${encodeURIComponent(taskId)}/${action}`, body),

  importBoq: async (file: File): Promise<Json> => {
    const form = new FormData();
    form.append("file", file);
    let res: Response;
    try {
        res = await authorizedFetch("/v1/boq/import", {
        method: "POST", body: form, headers: { "X-Actor": getActor() },
      });
    } catch (e) {
      throw new ApiError("POST", "/v1/boq/import", 0, "NETWORK_ERROR",
        `无法连接后端 ${getApiBase() || "同源代理"}`, String(e));
    }
    const text = await res.text();
    let body: unknown = text;
    try { body = text ? JSON.parse(text) : null; } catch { /* 保留文本 */ }
    if (!res.ok) {
      const parts = detailParts(body);
      throw new ApiError("POST", "/v1/boq/import", res.status, parts.code || `HTTP_${res.status}`,
        parts.message || res.statusText, body);
    }
    return body as Json;
  },

  ruleCalc: (body: Json, idem?: string) => post<RuleCalcResult>("/v1/quantity/rule-calc", body, { idem }),
  quantityCalc: (body: Json) => post<Json>("/v1/quantity/calc", body),

  quotaMatch: (body: Json) => post<Json>("/v1/quota/match", body),
  quotaApplyNotes: (body: Json) => post<Json>("/v1/quota/apply-notes", body),

  unitPrice: (body: Json) => post<Json>("/v1/price/unit", body),
  indexAdjust: (body: Json) => post<Json>("/v1/price/index-adjust", body),
  projectTotal: (body: Json) => post<Json>("/v1/price/project-total", body),

  recalc: (body: Json, idem?: string) => post<Json>("/v1/calc/recalc", body, { idem }),
  getCalcRun: (runId: string) => get<CalcRun>(`/v1/calc/${encodeURIComponent(runId)}`),
  gateCheck: (body: Json) => post<Json>("/v1/gate/check", body),
  gateSequence: (line: string) => get<{ business_line: string; gates: string[] }>(
    `/v1/gate/sequence/${encodeURIComponent(line)}`),
  jgateStatus: (jobId: string) => get<Json>(`/v1/jgate/${encodeURIComponent(jobId)}`),
  jgateRun: (jobId: string, actor = getActor()) =>
    post<Json>(`/v1/jgate/${encodeURIComponent(jobId)}/run`, { actor }),

  evidence: (evidenceId: string) => get<EvidenceRecord>(`/v1/evidence/${encodeURIComponent(evidenceId)}`),
  verifyEvidence: (payload: Json) => post<Json>("/v1/evidence/verify", payload),
  audit: (limit = 50) => get<{ items: AuditItem[] }>(`/v1/audit?limit=${limit}`),
  cockpitOverview: (projectId = "P-DEFAULT", line = "L1", mode = "NOVICE") =>
    get<Json>(`/v1/cockpit/overview?project_id=${encodeURIComponent(projectId)}&business_line=${encodeURIComponent(line)}&mode=${encodeURIComponent(mode)}`),
  bidCreate: (body: Json) => post<Json>("/v1/bid/create", body),
  bidRedline: (body: Json) => post<Json>("/v1/bid/redline", body),
  bidListLock: (body: Json) => post<Json>("/v1/bid/list-lock", body),
  bidQuote: (body: Json) => post<Json>("/v1/bid/quote", body),
  bidCompliance: (body: Json) => post<Json>("/v1/bid/compliance", body),
  reworkCreate: (body: Json) => post<Json>("/v1/rework/create", body),
  reworkRectify: (body: Json) => post<Json>("/v1/rework/rectify", body),
  reworkClose: (body: Json) => post<Json>("/v1/rework/close", body),
  reworkList: (projectId = "") => get<Json>(`/v1/rework/list${projectId ? "?project_id=" + encodeURIComponent(projectId) : ""}`),
  docDualExtract: (body: Json) => post<Json>("/v1/doc/dual-extract", body),
  docFinalSynthesize: (body: Json) => post<Json>("/v1/doc/final-synthesize", body),
  gldTaskbook: (body: Json) => post<Json>("/v1/gld/taskbook", body),
  gldReadback: (body: Json) => post<Json>("/v1/gld/readback", body),
  visaDuplicate: (body: Json) => post<Json>("/v1/visa/check-duplicate", body),
  archivePackage: (body: Json) => post<Json>("/v1/archive/package", body),
};

export async function authorizedFetch(path:string,init:RequestInit={}):Promise<Response>{
  const headers=new Headers(init.headers);if(auth.token)headers.set('Authorization','Bearer '+auth.token);
  const started=performance.now();
  let r:Response;
  try{r=await fetch(getApiBase()+path,{...init,headers});}
  catch(e){observeHttp(init.method||'GET',path,0,started);throw new ApiError(init.method||'GET',path,0,'NETWORK_ERROR','无法连接后端，请检查连接状态后重试',String(e));}
  observeHttp(init.method||'GET',path,r.status,started);
  if(!r.ok){if(r.status===401)clearAuth('会话已失效，请重新登录');let data:unknown;try{data=await r.json();}catch{data=null;}const parts=detailParts(data);throw new ApiError(init.method||'GET',path,r.status,parts.code,parts.message||r.statusText,data);}
  return r;
}
