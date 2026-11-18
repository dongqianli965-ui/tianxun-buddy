<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from "vue";
import { api } from "../api";
import EndpointPanel from "../components/EndpointPanel.vue";
import JsonField from "../components/JsonField.vue";
import PageHeader from "../components/PageHeader.vue";
import ResultPanel from "../components/ResultPanel.vue";
import { go, rememberCalcRun, rememberEvidence, withJob } from "../session";
import { useApi } from "../useApi";
import type { Rule } from "../types";

const { busy, error, result, run } = useApi();
const mode = ref<"rule" | "fixed">("rule");
const rules = ref<Rule[]>([]);
const rulesError = ref("");
const ruleCode = ref("");
const ruleVersion = ref("");
const paramsText = ref("{}");
const ruleVars = reactive<Record<string, string>>({});
const formal = ref(false);
const idemKey = ref("");
const fixedOp = ref("RECTANGULAR_VOLUME");

const fixedForm = reactive({
  length: "6.0",
  width: "0.4",
  height: "3.6",
  count: "10",
  gross: "86.4",
  deductionFirst: "0",
  additionFirst: "0",
});

const fixedParams = ref(JSON.stringify({
  length: "6.0", width: "0.4", height: "3.6", count: "10", gross: "86.4", deductions: [], additions: [],
}, null, 2));
const fixedUnit = ref("m3");
const fixedRuleCode = ref("QTY.WEB.FIXED");
const fixedStandardRef = ref("GB/T 50854-2024 现浇混凝土矩形柱体积计算规则");
const calcRunId = ref("");
const evidenceId = ref("");

const selectedRule = computed(() => rules.value.find(
  (r) => r.rule_code === ruleCode.value && (!ruleVersion.value || r.version === ruleVersion.value),
) || null);
const selectValue = computed(() => ruleCode.value
  ? `${ruleCode.value}@${ruleVersion.value}` : "");

function syncRuleVarsToJson(): void {
  paramsText.value = JSON.stringify({ ...ruleVars }, null, 2);
}

watch(fixedForm, () => {
  fixedParams.value = JSON.stringify({
    length: fixedForm.length,
    width: fixedForm.width,
    height: fixedForm.height,
    count: fixedForm.count,
    gross: fixedForm.gross,
    deductions: fixedForm.deductionFirst && fixedForm.deductionFirst !== "0" ? [fixedForm.deductionFirst] : [],
    additions: fixedForm.additionFirst && fixedForm.additionFirst !== "0" ? [fixedForm.additionFirst] : [],
  }, null, 2);
}, { deep: true });

function pickRule(value: string): void {
  const at = value.lastIndexOf("@");
  ruleCode.value = at >= 0 ? value.slice(0, at) : value;
  ruleVersion.value = at >= 0 ? value.slice(at + 1) : "";
  const rule = selectedRule.value;
  if (!rule) return;
  const tpl = templateFromExpression(rule.expression || "");
  for (const k of Object.keys(ruleVars)) delete ruleVars[k];
  for (const [k, v] of Object.entries(tpl)) ruleVars[k] = v;
  paramsText.value = JSON.stringify(tpl, null, 2);
}

function templateFromExpression(expression: string): Record<string, string> {
  const names = Array.from(new Set(expression.match(/[A-Za-z_][A-Za-z0-9_]*/g) || []))
    .filter((n) => !["Decimal", "decimal"].includes(n));
  return Object.fromEntries(names.map((n) => [n, "1.0"]));
}

onMounted(async () => {
  try {
    const out = await api.rules();
    rules.value = out.rules;
    if (out.rules.length) pickRule(`${out.rules[0].rule_code}@${out.rules[0].version}`);
  } catch (e) {
    rulesError.value = e instanceof Error ? e.message : String(e);
  }
});

function parseJson(text: string, label: string): Record<string, unknown> | null {
  try {
    const v = JSON.parse(text);
    if (!v || typeof v !== "object" || Array.isArray(v)) throw new Error(label + "必须是 JSON 对象");
    return v as Record<string, unknown>;
  } catch (e) {
    rulesError.value = label + " JSON 解析失败：" + (e instanceof Error ? e.message : String(e));
    return null;
  }
}

async function runRuleCalc(): Promise<void> {
  rulesError.value = "";
  const params = parseJson(paramsText.value, "算量参数");
  if (!params) return;
  const rule = selectedRule.value;
  if (!rule) { rulesError.value = "请选择规则"; return; }
  const body = withJob({
    rule_code: rule.rule_code,
    rule_version: rule.version,
    params,
    component_ids: [],
    formal_calculation: formal.value,
  });
  const out = await run("POST /v1/quantity/rule-calc", () => api.ruleCalc(body, idemKey.value.trim() || undefined));
  if (out) {
    calcRunId.value = out.calc_run_id;
    evidenceId.value = out.evidence_id;
    rememberCalcRun(out.calc_run_id);
    rememberEvidence(out.evidence_id);
  }
}

async function runFixedCalc(): Promise<void> {
  rulesError.value = "";
  const params = parseJson(fixedParams.value, "固定算子参数");
  if (!params) return;
  const body = withJob({
    operation: fixedOp.value,
    params,
    unit: fixedUnit.value,
    rule_code: fixedRuleCode.value,
    rule_version: "web.1",
    standard_ref: fixedStandardRef.value,
    formal_calculation: formal.value,
  });
  const out = await run("POST /v1/quantity/calc", () => api.quantityCalc(body));
  const record = out as Record<string, unknown> | null;
  if (record && typeof record.evidence_id === "string") {
    evidenceId.value = record.evidence_id;
    rememberEvidence(record.evidence_id);
  }
}

function newIdem(): void {
  idemKey.value = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()));
}
</script>

<template>
  <section>
    <PageHeader title="算量" desc="规则算量与固定算子算量（支持直接填写构件尺寸表单，自动生成计算式、evidence_id 与 calc_run 快照）。">
      <button class="btn" @click="go('evidence')">查看证据</button>
    </PageHeader>

    <EndpointPanel :keys="['quantityRule', 'quantityCalc', 'rules', 'evidenceGet', 'calcGet']" />

    <div class="tabs">
      <button :class="{ on: mode === 'rule' }" @click="mode = 'rule'">规则算量 /v1/quantity/rule-calc</button>
      <button :class="{ on: mode === 'fixed' }" @click="mode = 'fixed'">固定算子 /v1/quantity/calc</button>
    </div>

    <div class="card">
      <div class="form-row">
        <label class="grow">规则（GET /v1/rules：{{ rules.length }} 条）
          <select :value="selectValue" @change="pickRule(($event.target as HTMLSelectElement).value)">
            <option v-for="r in rules" :key="r.rule_code + r.version" :value="r.rule_code + '@' + r.version">
              {{ r.rule_code }}@{{ r.version }} · {{ r.name }}
            </option>
          </select>
        </label>
        <label class="check"><input v-model="formal" type="checkbox" /> formal_calculation（正式）</label>
      </div>
      <p v-if="rulesError" class="err">{{ rulesError }}</p>
      <p v-if="selectedRule" class="hint">
        表达式：<code>{{ selectedRule.expression || "-" }}</code> · 单位：{{ selectedRule.unit }} ·
        授权：{{ selectedRule.license_status }} · 依据：{{ selectedRule.standard_ref }}
      </p>

      <template v-if="mode === 'rule'">
        <div class="form-row" v-if="Object.keys(ruleVars).length">
          <label v-for="(_, k) in ruleVars" :key="k">
            构件参数 <code>{{ k }}</code>
            <input v-model="ruleVars[k]" @input="syncRuleVarsToJson" placeholder="输入数值" />
          </label>
        </div>
        <details style="margin: 8px 0;">
          <summary style="cursor: pointer; font-weight: 600; color: #334155;">高级：查看/编辑算量参数 JSON</summary>
          <JsonField v-model="paramsText" label="算量参数 params（变量名取自规则表达式）" :rows="6" />
        </details>
        <div class="form-row">
          <label class="grow">幂等键 X-Idem-Key（可选）<input v-model="idemKey" placeholder="留空则不发送幂等头" /></label>
          <button class="btn" @click="newIdem">生成</button>
        </div>
        <div class="btn-row">
          <button class="btn primary" :disabled="busy || !selectedRule" @click="runRuleCalc">执行规则算量</button>
          <button class="btn" :disabled="!calcRunId" @click="run('GET /v1/calc/{run_id}', () => api.getCalcRun(calcRunId))">
            读取 calc_run
          </button>
        </div>
      </template>

      <template v-else>
        <div class="form-row">
          <label>固定算子
            <select v-model="fixedOp">
              <option value="RECTANGULAR_AREA">RECTANGULAR_AREA（矩形面积）</option>
              <option value="RECTANGULAR_VOLUME">RECTANGULAR_VOLUME（矩形体积）</option>
              <option value="LINEAR_LENGTH">LINEAR_LENGTH（延长米线性长度）</option>
              <option value="NET_VALUE">NET_VALUE（毛量加减扣减净量）</option>
            </select>
          </label>
          <label>单位<input v-model="fixedUnit" /></label>
          <label class="grow">rule_code<input v-model="fixedRuleCode" /></label>
        </div>
        <div class="form-row">
          <label>长度 length<input v-model="fixedForm.length" /></label>
          <label>宽度 width<input v-model="fixedForm.width" /></label>
          <label>高度 height<input v-model="fixedForm.height" /></label>
          <label>构件数量 count<input v-model="fixedForm.count" /></label>
          <label>毛量 gross<input v-model="fixedForm.gross" /></label>
        </div>
        <details style="margin: 8px 0;">
          <summary style="cursor: pointer; font-weight: 600; color: #334155;">高级：查看/编辑固定算子 JSON</summary>
          <JsonField v-model="fixedParams" label="固定算子参数 params" :rows="6" />
        </details>
        <label>规范依据条款 standard_ref<input v-model="fixedStandardRef" /></label>
        <div class="btn-row">
          <button class="btn primary" :disabled="busy" @click="runFixedCalc">执行固定算子</button>
        </div>
      </template>
    </div>

    <div v-if="evidenceId || calcRunId" class="card">
      <div class="kv">
        <div><span>calc_run_id</span><b class="mono">{{ calcRunId || "-" }}</b></div>
        <div><span>evidence_id</span><b class="mono">{{ evidenceId || "-" }}</b></div>
      </div>
      <button class="btn" @click="go('evidence')">到证据页查看</button>
    </div>

    <ResultPanel :result="result" :error="error" :busy="busy" label="算量接口响应" />
  </section>
</template>
