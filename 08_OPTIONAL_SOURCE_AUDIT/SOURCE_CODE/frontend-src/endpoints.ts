/**
 * 端点登记表：前端只允许调用这里登记的方法。
 * state=connected 表示存在 HTTP 接线；逐项验证以 reports/v424/ENDPOINT_MAPPING.json 为准；
 * state=unconnected 表示业务需要但后端尚未暴露，页面必须显式标注“未接通”，禁止用假数据冒充。
 */
export type EndpointState = "connected" | "unconnected";

export interface EndpointMeta {
  key: string;
  label: string;
  method: "GET" | "POST" | "PUT" | "DELETE";
  path: string;
  state: EndpointState;
  note?: string;
}

const E = (
  key: string, label: string, method: EndpointMeta["method"], path: string,
  state: EndpointState = "connected", note?: string,
): EndpointMeta => ({ key, label, method, path, state, note });

export const ENDPOINTS: Record<string, EndpointMeta> = {
  health: E("health", "健康检查", "GET", "/health"),
  precision: E("precision", "精度规则", "GET", "/v1/precision"),
  rules: E("rules", "规则库", "GET", "/v1/rules"),
  rulesValidate: E("rulesValidate", "规则自检", "POST", "/v1/rules/validate"),
  basisValidate: E("basisValidate", "依据包校验", "POST", "/v1/basis/validate"),

  jobCreate: E("jobCreate", "创建项目", "POST", "/v1/job"),
  jobGet: E("jobGet", "项目详情", "GET", "/v1/job/{job_id}"),
  jobList: E("jobList", "项目列表", "GET", "/v1/jobs"),
  docAdd: E("docAdd", "登记资料", "POST", "/v1/job/{job_id}/documents"),
  recordsGet: E("recordsGet", "读取业务记录", "GET", "/v1/job/{job_id}/records?kind="),
  recordsPut: E("recordsPut", "写入业务记录", "POST", "/v1/job/{job_id}/records"),
  filePut: E("filePut", "资料二进制上传", "PUT", "/files/{path}"),
  taskCreate: E("taskCreate", "创建任务", "POST", "/v1/job/{job_id}/tasks"),
  taskGet: E("taskGet", "任务详情", "GET", "/v1/task/{task_id}"),
  taskAction: E("taskAction", "任务流转", "POST", "/v1/task/{task_id}/{action}"),

  boqImport: E("boqImport", "清单导入", "POST", "/v1/boq/import"),
  parseFile: E("parseFile", "文档解析", "POST", "/v1/parse"),
  materialPrices: E("materialPrices", "材料价格查询", "GET", "/v1/material-prices"),
  quantityRule: E("quantityRule", "规则算量", "POST", "/v1/quantity/rule-calc"),
  quantityCalc: E("quantityCalc", "固定算子算量", "POST", "/v1/quantity/calc"),
  quotaMatch: E("quotaMatch", "定额匹配", "POST", "/v1/quota/match"),
  quotaNotes: E("quotaNotes", "定额换算", "POST", "/v1/quota/apply-notes"),
  priceUnit: E("priceUnit", "综合单价", "POST", "/v1/price/unit"),
  priceIndex: E("priceIndex", "指数调差", "POST", "/v1/price/index-adjust"),
  priceTotal: E("priceTotal", "项目汇总", "POST", "/v1/price/project-total"),

  recalc: E("recalc", "复算", "POST", "/v1/calc/recalc"),
  calcGet: E("calcGet", "计算运行详情", "GET", "/v1/calc/{run_id}"),
  gateCheck: E("gateCheck", "门禁判定", "POST", "/v1/gate/check"),
  gateSequence: E("gateSequence", "门禁序列", "GET", "/v1/gate/sequence/{line}"),
  jgateStatus: E("jgateStatus", "鉴定门状态", "GET", "/v1/jgate/{job_id}"),
  jgateRun: E("jgateRun", "鉴定门影子运行", "POST", "/v1/jgate/{job_id}/run"),

  deliverableList: E("deliverableList", "成果清单", "GET", "/v1/deliverables/{job_id}"),
  deliverableGenerate: E("deliverableGenerate", "成果生成（正式成果受门禁限制）", "POST", "/v1/deliverables/{job_id}/generate"),
  evidenceGet: E("evidenceGet", "证据详情", "GET", "/v1/evidence/{eid}"),
  evidenceVerify: E("evidenceVerify", "证据验签", "POST", "/v1/evidence/verify"),
  audit: E("audit", "审计流水", "GET", "/v1/audit?limit="),
  authLogin: E("authLogin", "登录", "POST", "/auth/login"),
  aclProjects: E("aclProjects", "项目成员权限", "GET", "/projects/{project_id}"),
  cockpitOverview: E("cockpitOverview", "工序驾驶舱与门禁视图", "GET", "/v1/cockpit/overview"),
  bidCreate: E("bidCreate", "投标接标与广联达四条判定", "POST", "/v1/bid/create"),
  bidRedline: E("bidRedline", "招标文件13类红线解析(T1门)", "POST", "/v1/bid/redline"),
  bidListLock: E("bidListLock", "清单七字段锁定校验(T2门)", "POST", "/v1/bid/list-lock"),
  bidQuote: E("bidQuote", "三档报价区间与决策留痕(D-09)", "POST", "/v1/bid/quote"),
  bidCompliance: E("bidCompliance", "合规自检与六表样生成(T5门)", "POST", "/v1/bid/compliance"),
  reworkCreate: E("reworkCreate", "创建九字段返工单", "POST", "/v1/rework/create"),
  reworkRectify: E("reworkRectify", "返工单改前改后整改", "POST", "/v1/rework/rectify"),
  reworkClose: E("reworkClose", "复核关闭/重开返工单", "POST", "/v1/rework/close"),
  reworkList: E("reworkList", "返工单与候选规则列表", "GET", "/v1/rework/list"),
  docDualExtract: E("docDualExtract", "资料人工/AI双轨独立提取", "POST", "/v1/doc/dual-extract"),
  docFinalSynthesize: E("docFinalSynthesize", "双轨逐字段合成唯一最终稿", "POST", "/v1/doc/final-synthesize"),
  gldTaskbook: E("gldTaskbook", "广联达L2任务书生成", "POST", "/v1/gld/taskbook"),
  gldReadback: E("gldReadback", "广联达读回偏差(>5%)校验", "POST", "/v1/gld/readback"),
  visaDuplicate: E("visaDuplicate", "签证变更四维查重", "POST", "/v1/visa/check-duplicate"),
  archivePackage: E("archivePackage", "河南23项/投标15项立卷归档包", "POST", "/v1/archive/package"),
};

export function endpoint(key: string): EndpointMeta {
  const meta = ENDPOINTS[key];
  if (!meta) throw new Error(`未登记的端点：${key}`);
  return meta;
}

export function isConnected(key: string): boolean {
  return endpoint(key).state === "connected";
}
