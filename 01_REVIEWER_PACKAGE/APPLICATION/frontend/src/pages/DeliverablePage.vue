<script setup lang="ts">
import { computed, ref } from "vue";
import { api } from "../api";
import EndpointPanel from "../components/EndpointPanel.vue";
import PageHeader from "../components/PageHeader.vue";
import ResultPanel from "../components/ResultPanel.vue";
import { loadJob, session } from "../session";
import { formatError, useApi } from "../useApi";
import type { Json } from "../types";

const { busy, error, result, run } = useApi();
const jgate = ref<Json | null>(null);
const localError = ref("");
const manifests=ref<Json[]>([]);

/** 能力清单与 backend/txbuddy/deliverables/specs.py 的 SPECS 一一对应（元数据，不是成果文件）。 */
const SPECS = [
  { kind: "budget_xlsx", label: "预算书（XLSX）", file: "01_预算书.xlsx" },
  { kind: "budget_pdf", label: "预算书（PDF）", file: "02_预算书.pdf" },
  { kind: "quantity_calc", label: "工程量计算书", file: "03_工程量计算书.xlsx" },
  { kind: "quota_detail", label: "定额套用明细", file: "04_定额套用明细.xlsx" },
  { kind: "unit_price", label: "综合单价分析", file: "05_综合单价分析.xlsx" },
  { kind: "resource_summary", label: "人工材料机械汇总", file: "06_人工材料机械汇总.xlsx" },
  { kind: "fee_calculation", label: "费用计算", file: "07_费用计算表.xlsx" },
  { kind: "index_adjustment", label: "调差", file: "08_调差表.xlsx" },
  { kind: "audit_difference", label: "审核差异", file: "09_审核差异表.xlsx" },
  { kind: "evidence_index", label: "证据索引", file: "10_证据索引.xlsx" },
];

const gateRows = computed(() => session.job?.gate_records || []);
const calcRuns = computed(() => session.job?.calc_runs || []);

async function refresh(): Promise<void> {
  localError.value = "";
  await loadJob();
  try {
    if(session.jobId) manifests.value=(await api.deliverables(session.jobId)).items as Json[];
  } catch(e) { manifests.value=[]; localError.value=formatError(e); return; }
  if (!session.job) { localError.value = session.jobError; return; }
  if (session.job.business_line === "L6") {
    try { jgate.value = await api.jgateStatus(session.job.job_id); }
    catch (e) { localError.value = formatError(e); }
  }
}

async function generate(): Promise<void> {
  localError.value='';
  try { await api.generateDeliverable(session.jobId,{kind:'budget_xlsx'}); }
  catch(e){ localError.value=e instanceof Error?e.message:String(e); }
}
</script>

<template>
  <section>
    <PageHeader title="成果" desc="成果接口已接通；正式预算书与工程量计算书须正版依据、G4及真实专业签认。">
      <button class="btn" :disabled="busy" @click="refresh">刷新前置条件</button>
    </PageHeader>

    <EndpointPanel :keys="['deliverableList', 'deliverableGenerate', 'jobGet', 'gateSequence', 'jgateStatus']" />

    <div class="notice warn">当前正式计算精度验收为 BLOCKED。样本清单、来源与流程结果可在「样本试点」导出。<button class="btn" @click="session.page='pilot'">样本结果导出</button></div>
<div class="card" v-if="manifests.length"><h3>已生成样本结果</h3><pre>{{JSON.stringify(manifests,null,2)}}</pre></div>
    <p v-if="localError" class="err">{{ localError }}</p>

    <div class="grid-2">
      <div class="card">
        <h3>成果能力清单（元数据）</h3>
        <table>
          <thead><tr><th>kind</th><th>名称</th><th>文件名</th><th>接通状态</th></tr></thead>
          <tbody>
            <tr v-for="s in SPECS" :key="s.kind">
              <td class="mono">{{ s.kind }}</td><td>{{ s.label }}</td><td class="mono">{{ s.file }}</td>
              <td><span class="badge warn">正式成果待验收</span></td>
            </tr>
          </tbody>
        </table>
        <p class="hint">
          清单来自 deliverables/specs.py 的 SPECS，仅表示后端能力；生成动作需要 API 路由与 job 数据装配，
          正式成果的门禁状态由服务端返回；不得把样本导入结果作为独立计算结果。
        </p>
        <button class="btn" :disabled="!session.jobId" title="检查正式生成前置条件" @click="generate">
          检查正式生成条件
        </button>
      </div>

      <div class="card">
        <h3>服务端前置条件</h3>
        <div class="kv">
          <div><span>当前项目</span><b>{{ session.job?.job_id || "未选择" }}</b></div>
          <div><span>项目状态</span><b>{{ session.job?.status || "-" }}</b></div>
          <div><span>门禁记录</span><b>{{ gateRows.length }}</b></div>
          <div><span>算量运行</span><b>{{ calcRuns.length }}</b></div>
          <div><span>资料</span><b>{{ session.job?.documents?.length || 0 }}</b></div>
        </div>
        <h4>门禁记录</h4>
        <table>
          <thead><tr><th>门</th><th>结果</th><th>时间</th></tr></thead>
          <tbody>
            <tr v-for="(g, i) in gateRows" :key="i">
              <td>{{ g.gate || g.gate_code }}</td>
              <td>{{ g.result || g.status }}</td>
              <td>{{ g.at || g.created_at || "-" }}</td>
            </tr>
            <tr v-if="!gateRows.length"><td colspan="3" class="hint">无门禁记录；请先在「复核」页执行门禁判定。</td></tr>
          </tbody>
        </table>
        <h4>算量运行（可追溯到证据）</h4>
        <table>
          <thead><tr><th>calc_run_id</th><th>规则</th><th>结果</th><th>证据</th></tr></thead>
          <tbody>
            <tr v-for="c in calcRuns.slice(0, 20)" :key="c.calc_run_id">
              <td class="mono">{{ c.calc_run_id }}</td><td>{{ c.rule_code }}</td>
              <td>{{ c.result }} {{ c.unit }}</td><td class="mono">{{ c.evidence_id }}</td>
            </tr>
            <tr v-if="!calcRuns.length"><td colspan="4" class="hint">无算量运行；请先在「算量」页执行。</td></tr>
          </tbody>
        </table>
        <template v-if="jgate">
          <h4>鉴定线出件状态（L6）</h4>
          <pre>{{ JSON.stringify({
            deliverable_state: jgate.deliverable_state,
            all_gates_passed: jgate.all_gates_passed,
            issuable: jgate.issuable,
            auto_issue: jgate.auto_issue,
          }, null, 2) }}</pre>
        </template>
      </div>
    </div>

    <ResultPanel :result="result" :error="error" :busy="busy" label="成果页状态" />
  </section>
</template>
