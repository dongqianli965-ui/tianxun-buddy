/** 与后端 txbuddy.models / txbuddy.api 保持同签名的前端类型（只描述前端真正用到的字段）。 */
export type Json = Record<string, unknown>;

export type BusinessLine = "L1" | "L2" | "L3" | "L4" | "L5" | "L6";

export const BUSINESS_LINES: { code: BusinessLine; label: string }[] = [
  { code: "L1", label: "L1 预算编制" },
  { code: "L2", label: "L2 结算审核" },
  { code: "L3", label: "L3 过程控制" },
  { code: "L4", label: "L4 投标报价" },
  { code: "L5", label: "L5 成本测算" },
  { code: "L6", label: "L6 造价鉴定" },
];

export interface BasisSource {
  source_id: string;
  title: string;
  doc_no?: string | null;
  source_type: "LAW" | "STANDARD" | "LOCAL_RULE" | "QUOTA" | "PRICE" | "CONTRACT" | "TEST";
  license_status?: "PUBLIC" | "LICENSED" | "PENDING" | "UNKNOWN" | "TEST_ONLY";
  verified?: boolean;
}

/** 与 models.BasisPack 同字段；extra=forbid，前端不得擅自加字段。 */
export interface BasisPack {
  basis_pack_id: string;
  region_code: string;
  region_level: "PROVINCE" | "CITY" | "COUNTY";
  major: string;
  effective_date: string;
  measurement_standard_version: string;
  pricing_standard_version: string;
  quota_pack_version?: string | null;
  rule_version_scope: "HENAN_2025" | "HENAN_LEGACY" | "NATIONAL";
  precision_rule_version: string;
  status: "DRAFT" | "APPROVED" | "LOCKED";
  sources: BasisSource[];
  test_only: boolean;
}

export interface JobDocument {
  doc_key: string;
  version: string;
  file_sha256: string;
  verified: boolean;
  added_at?: string | null;
}

export interface JobTask {
  task_id: string;
  node: string;
  status: string;
  payload?: Json;
  result?: Json | null;
  assignee?: string | null;
  approver?: string | null;
  approved_at?: string | null;
  updated_at?: string | null;
}

export interface GateRecord {
  gate?: string;
  gate_code?: string;
  result?: string;
  status?: string;
  failed_rules?: { rule_id?: string; error_code?: string; message?: string }[];
  [key: string]: unknown;
}

export interface CalcRun {
  calc_run_id: string;
  job_id?: string | null;
  rule_code?: string;
  rule_version?: string;
  result?: string;
  unit?: string;
  evidence_id?: string;
  snapshot_sha256?: string;
  created_at?: string;
  [key: string]: unknown;
}

export interface Job {
  job_id: string;
  project_id: string;
  name: string;
  business_line: BusinessLine;
  business_line_label?: string;
  status: string;
  basis_pack_id?: string;
  revision?: number;
  created_by?: string;
  created_at?: string;
  updated_at?: string;
  documents?: JobDocument[];
  tasks?: JobTask[];
  gate_records?: GateRecord[];
  calc_runs?: CalcRun[];
  [key: string]: unknown;
}

export interface Rule {
  rule_code: string;
  version: string;
  name?: string;
  component_type?: string;
  caliber?: string;
  unit?: string;
  expression?: string;
  standard_ref?: string;
  license_status?: string;
  source_verified?: boolean;
  status?: string;
}

export interface RuleCalcResult {
  calc_run_id: string;
  result: string;
  unit: string;
  formula?: string;
  intermediate_values?: Json;
  standard_ref?: string;
  snapshot_sha256?: string;
  evidence_id: string;
  evidence_sha256?: string;
  formal_calculation?: boolean;
  formal_ready?: boolean;
  test_only?: boolean;
  admission_reasons?: string[];
  warning?: string;
  [key: string]: unknown;
}

export interface Health {
  status: string;
  service?: string;
  version?: string;
  mode?: string;
  rules_loaded?: number;
  precision?: string;
  environment?: string;
  test_only?: boolean;
  production_accepted?: boolean;
  accuracy_E?: string;
}

export interface EvidenceRecord {
  evidence_id: string;
  subject_type?: string;
  subject_id?: string;
  result?: string;
  unit?: string;
  formula?: string;
  sha256?: string;
  created_at?: string;
  [key: string]: unknown;
}

export interface AuditItem {
  at?: string;
  actor?: string;
  action?: string;
  object_id?: string;
  detail?: string;
  trace_id?: string;
  [key: string]: unknown;
}
