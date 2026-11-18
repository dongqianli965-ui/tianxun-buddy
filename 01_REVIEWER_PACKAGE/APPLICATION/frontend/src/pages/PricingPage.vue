<script setup lang="ts">
import { reactive, ref, watch } from "vue";
import { api } from "../api";
import EndpointPanel from "../components/EndpointPanel.vue";
import JsonField from "../components/JsonField.vue";
import PageHeader from "../components/PageHeader.vue";
import ResultPanel from "../components/ResultPanel.vue";
import { withJob } from "../session";
import { useApi } from "../useApi";

const { busy, error, result, run } = useApi();
const mode = ref<"unit" | "index" | "total">("unit");
const localError = ref("");
const formal = ref(false);

const unitForm = reactive({
  labor_code: "R-LABOR-01",
  labor_cons: "1.85",
  labor_price: "145.00",
  mat_code: "R-MAT-C30",
  mat_cons: "1.015",
  mat_price: "389.23",
  mach_code: "R-MACH-PUMP",
  mach_cons: "0.12",
  mach_price: "620.00",
  price_ref: "郑州市2026-Q3信息价第137页",
});

const resourcesText = ref("");
const managementRate = ref("0.05");
const profitRate = ref("0.03");
const managementRef = ref("豫建科〔2025〕173号费用表");
const profitRef = ref("豫建科〔2025〕173号费用表");

const idxForm = reactive({
  base_amount: "100000.00",
  base_index: "100.0",
  current_index: "108.5",
  risk_rate: "0.05",
  mode: "WITHIN_BAND_ONLY",
  contract_clause_ref: "施工合同专用条款第16.1条（±5%风险幅度）",
});
const indexText = ref("");

const totForm = reactive({
  boq_total: "8500000.00",
  measure_total: "620000.00",
  other_total: "300000.00",
  safety_production_fee: "280000.00",
  tax_rate: "0.09",
});
const totalText = ref("");

function syncPricingForms(): void {
  resourcesText.value = JSON.stringify([
    { resource_code: unitForm.labor_code, resource_type: "LABOR", consumption: unitForm.labor_cons, unit_price: unitForm.labor_price, price_ref: unitForm.price_ref },
    { resource_code: unitForm.mat_code, resource_type: "MATERIAL", consumption: unitForm.mat_cons, unit_price: unitForm.mat_price, price_ref: unitForm.price_ref },
    { resource_code: unitForm.mach_code, resource_type: "MACHINE", consumption: unitForm.mach_cons, unit_price: unitForm.mach_price, price_ref: unitForm.price_ref },
  ], null, 2);

  indexText.value = JSON.stringify({ ...idxForm }, null, 2);

  totalText.value = JSON.stringify({
    boq_total: totForm.boq_total,
    measure_total: totForm.measure_total,
    other_total: totForm.other_total,
    fee_items: [],
    safety_production_fee: totForm.safety_production_fee,
    tax_rate: totForm.tax_rate,
  }, null, 2);
}

syncPricingForms();
watch([unitForm, idxForm, totForm], syncPricingForms, { deep: true });

function parse(text: string, label: string): unknown | null {
  try {
    localError.value = "";
    return JSON.parse(text);
  } catch (e) {
    localError.value = `${label} JSON 解析失败：${e instanceof Error ? e.message : String(e)}`;
    return null;
  }
}

function asObject(text: string, label: string): Record<string, unknown> | null {
  const v = parse(text, label);
  if (v === null) return null;
  if (!v || typeof v !== "object" || Array.isArray(v)) { localError.value = label + " 必须是 JSON 对象"; return null; }
  return v as Record<string, unknown>;
}

async function runUnit(): Promise<void> {
  const resources = parse(resourcesText.value, "resources");
  if (!Array.isArray(resources)) { localError.value = "resources 必须是 JSON 数组"; return; }
  await run("POST /v1/price/unit", () => api.unitPrice(withJob({
    resources,
    management_rate: managementRate.value.trim(),
    profit_rate: profitRate.value.trim(),
    management_base: "DIRECT_COST",
    profit_base: "DIRECT_COST_PLUS_MANAGEMENT",
    explicit_risk_fee: null,
    adjusted_resource_codes: [],
    risk_resource_codes: [],
    management_source_ref: managementRef.value.trim(),
    profit_source_ref: profitRef.value.trim(),
    formal_calculation: formal.value,
  })));
}

async function runIndex(): Promise<void> {
  const body = asObject(indexText.value, "指数调差");
  if (!body) return;
  await run("POST /v1/price/index-adjust", () => api.indexAdjust(withJob({ ...body, formal_calculation: formal.value })));
}

async function runTotal(): Promise<void> {
  const body = asObject(totalText.value, "项目汇总");
  if (!body) return;
  await run("POST /v1/price/project-total", () => api.projectTotal(withJob({ ...body, formal_calculation: formal.value })));
}
</script>

<template>
  <section>
    <PageHeader title="组价" desc="综合单价、指数调差、项目汇总三条组价链路（造价师结构化表格录入，自动组装底层 JSON 请求）。">
      <label class="check"><input v-model="formal" type="checkbox" /> formal_calculation</label>
    </PageHeader>

    <EndpointPanel :keys="['priceUnit', 'priceIndex', 'priceTotal', 'basisValidate']" />
    <p v-if="localError" class="err">{{ localError }}</p>

    <div class="tabs">
      <button :class="{ on: mode === 'unit' }" @click="mode = 'unit'">综合单价</button>
      <button :class="{ on: mode === 'index' }" @click="mode = 'index'">指数调差</button>
      <button :class="{ on: mode === 'total' }" @click="mode = 'total'">项目汇总</button>
    </div>

    <div v-if="mode === 'unit'" class="card">
      <h3>综合单价计算 <code>POST /v1/price/unit</code></h3>
      <div class="form-row">
        <label>人工消耗量<input v-model="unitForm.labor_cons" /></label>
        <label>人工单价(元)<input v-model="unitForm.labor_price" /></label>
        <label>材料消耗量<input v-model="unitForm.mat_cons" /></label>
        <label>材料单价(元)<input v-model="unitForm.mat_price" /></label>
        <label>机械消耗量<input v-model="unitForm.mach_cons" /></label>
        <label>机械单价(元)<input v-model="unitForm.mach_price" /></label>
      </div>
      <div class="form-row">
        <label>管理费率<input v-model="managementRate" /></label>
        <label>利润率<input v-model="profitRate" /></label>
        <label class="grow">价格/信息价来源<input v-model="unitForm.price_ref" /></label>
        <label class="grow">费率依据<input v-model="managementRef" /></label>
      </div>
      <details style="margin: 8px 0;">
        <summary style="cursor: pointer; font-weight: 600; color: #334155;">高级：查看/编辑 resources[] JSON</summary>
        <JsonField v-model="resourcesText" label="resources[]" :rows="6" />
      </details>
      <button class="btn primary" :disabled="busy" @click="runUnit">计算综合单价</button>
    </div>

    <div v-if="mode === 'index'" class="card">
      <h3>材料/人工指数调差 <code>POST /v1/price/index-adjust</code></h3>
      <div class="form-row">
        <label>基期合价 base_amount<input v-model="idxForm.base_amount" /></label>
        <label>基准期指数 base_index<input v-model="idxForm.base_index" /></label>
        <label>施工期指数 current_index<input v-model="idxForm.current_index" /></label>
        <label>风险幅度 risk_rate<input v-model="idxForm.risk_rate" /></label>
        <label>调差模式
          <select v-model="idxForm.mode">
            <option value="WITHIN_BAND_ONLY">WITHIN_BAND_ONLY（仅超幅度部分调差）</option>
            <option value="FULL_EXCESS">FULL_EXCESS（超幅度全额调差）</option>
            <option value="PASS_THROUGH">PASS_THROUGH（零风险直通调差）</option>
          </select>
        </label>
      </div>
      <label>合同调差条款出处<input v-model="idxForm.contract_clause_ref" /></label>
      <details style="margin: 8px 0;">
        <summary style="cursor: pointer; font-weight: 600; color: #334155;">高级：查看/编辑调差 JSON</summary>
        <JsonField v-model="indexText" label="调差参数" :rows="6" />
      </details>
      <button class="btn primary" :disabled="busy" @click="runIndex">计算调差</button>
    </div>

    <div v-if="mode === 'total'" class="card">
      <h3>单位工程/项目总价汇总 <code>POST /v1/price/project-total</code></h3>
      <div class="form-row">
        <label>分部分项合计(元)<input v-model="totForm.boq_total" /></label>
        <label>措施项目合计(元)<input v-model="totForm.measure_total" /></label>
        <label>其他项目合计(元)<input v-model="totForm.other_total" /></label>
        <label>不可竞争安全文明施工费(元)<input v-model="totForm.safety_production_fee" /></label>
        <label>增值税率 tax_rate<input v-model="totForm.tax_rate" /></label>
      </div>
      <details style="margin: 8px 0;">
        <summary style="cursor: pointer; font-weight: 600; color: #334155;">高级：查看/编辑项目汇总 JSON</summary>
        <JsonField v-model="totalText" label="汇总参数" :rows="6" />
      </details>
      <button class="btn primary" :disabled="busy" @click="runTotal">计算项目总价</button>
    </div>

    <ResultPanel :result="result" :error="error" :busy="busy" label="组价接口响应" />
  </section>
</template>
