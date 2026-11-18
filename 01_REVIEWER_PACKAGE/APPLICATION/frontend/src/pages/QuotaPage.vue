<script setup lang="ts">
import { reactive, ref, watch } from "vue";
import { api } from "../api";
import EndpointPanel from "../components/EndpointPanel.vue";
import JsonField from "../components/JsonField.vue";
import PageHeader from "../components/PageHeader.vue";
import ResultPanel from "../components/ResultPanel.vue";
import { getBoqItem, withJob } from "../session";
import { useApi } from "../useApi";

const { busy, error, result, run } = useApi();
const picked = getBoqItem();

const boqForm = reactive({
  code: String(picked?.code || "010401001001"),
  name: String(picked?.name || "矩形柱"),
  unit: String(picked?.unit || "m3"),
  major: String(picked?.major || "BUILDING"),
  quantity: String(picked?.quantity || "120.50"),
});

const quotaForm = reactive({
  id: "Q-HN-4-15",
  item_code: "4-15",
  name: "现浇商品混凝土矩形柱",
  unit: "10m3",
  major: "BUILDING",
  mapped_boq_code: "010401001001",
  source_ref: "S-HN-QUOTA-2025",
  license_status: "LICENSED",
});

const noteForm = reactive({
  resource_code: "R-CONCRETE-C30",
  consumption: "10.15",
  note_id: "N-4-15-01",
  effect_type: "FACTOR",
  factor: "1.02",
  source_ref: "豫建科〔2025〕173号第4章说明第3条",
});

const boqText = ref(JSON.stringify({ ...boqForm, tags: ["柱", "C30"] }, null, 2));
const quotaText = ref(JSON.stringify([{ ...quotaForm, status: "ACTIVE", work_tags: ["柱", "C30"], note_effects: [] }], null, 2));
const mappingText = ref(JSON.stringify({ [boqForm.code]: quotaForm.item_code }, null, 2));
const consumptionText = ref(JSON.stringify([{ resource_code: noteForm.resource_code, consumption: noteForm.consumption }], null, 2));
const notesText = ref(JSON.stringify([{
  id: noteForm.note_id, scope_level: "ITEM", priority: 100, effect_type: noteForm.effect_type,
  target_resource_code: noteForm.resource_code, factor: noteForm.factor, source_ref: noteForm.source_ref,
}], null, 2));
const conversionText = ref(JSON.stringify({ allowed: true }, null, 2));
const formal = ref(false);
const localError = ref("");

watch([boqForm, quotaForm], () => {
  boqText.value = JSON.stringify({ ...boqForm, tags: ["柱", "C30"] }, null, 2);
  quotaText.value = JSON.stringify([{ ...quotaForm, status: "ACTIVE", work_tags: ["柱", "C30"], note_effects: [] }], null, 2);
  mappingText.value = JSON.stringify({ [boqForm.code]: quotaForm.item_code }, null, 2);
}, { deep: true });

watch(noteForm, () => {
  consumptionText.value = JSON.stringify([{ resource_code: noteForm.resource_code, consumption: noteForm.consumption }], null, 2);
  notesText.value = JSON.stringify([{
    id: noteForm.note_id, scope_level: "ITEM", priority: 100, effect_type: noteForm.effect_type,
    target_resource_code: noteForm.resource_code, factor: noteForm.factor, source_ref: noteForm.source_ref,
  }], null, 2);
}, { deep: true });

function parse(text: string, label: string): unknown | null {
  try { return JSON.parse(text); } catch (e) {
    if (!localError.value) localError.value = `${label} JSON 解析失败：${e instanceof Error ? e.message : String(e)}`;
    return null;
  }
}

function parseObject(text: string, label: string): Record<string, unknown> | null {
  const v = parse(text, label);
  if (v === null) return null;
  if (!v || typeof v !== "object" || Array.isArray(v)) { if (!localError.value) localError.value = label + " 必须是 JSON 对象"; return null; }
  return v as Record<string, unknown>;
}

function parseArray(text: string, label: string): Record<string, unknown>[] | null {
  const v = parse(text, label);
  if (v === null) return null;
  if (!Array.isArray(v)) { if (!localError.value) localError.value = label + " 必须是 JSON 数组"; return null; }
  return v as Record<string, unknown>[];
}

async function match(): Promise<void> {
  localError.value = "";
  const boq = parseObject(boqText.value, "boq_item");
  const quotas = parseArray(quotaText.value, "quota_items");
  const mapping = parseObject(mappingText.value, "exact_mappings");
  if (!boq || !quotas || !mapping) return;
  await run("POST /v1/quota/match", () => api.quotaMatch({
    boq_item: boq, quota_items: quotas, exact_mappings: mapping,
  }));
}

async function applyNotes(): Promise<void> {
  localError.value = "";
  const consumption = parseArray(consumptionText.value, "consumption");
  const notes = parseArray(notesText.value, "notes");
  const conversion = parseObject(conversionText.value, "conversion");
  if (!consumption || !notes || !conversion) return;
  await run("POST /v1/quota/apply-notes", () => api.quotaApplyNotes(withJob({
    consumption, notes, conversion, formal_calculation: formal.value,
  })));
}
</script>

<template>
  <section>
    <PageHeader title="定额" desc="定额匹配与附注换算（结构化表单向导，无需手写 JSON；候选须人工确认，附注换算自动绑定依据出处）。">
      <span v-if="picked" class="badge ok">已从清单带入 {{ picked.code }}</span>
    </PageHeader>

    <EndpointPanel :keys="['quotaMatch', 'quotaNotes', 'jobGet']" />
    <p v-if="localError" class="err">{{ localError }}</p>

    <div class="grid-2">
      <div class="card">
        <h3>定额匹配 <code>POST /v1/quota/match</code></h3>
        <div class="form-row">
          <label>清单编码<input v-model="boqForm.code" /></label>
          <label>清单名称<input v-model="boqForm.name" /></label>
          <label>单位<input v-model="boqForm.unit" /></label>
          <label>工程量<input v-model="boqForm.quantity" /></label>
        </div>
        <div class="form-row">
          <label>候选定额子目号<input v-model="quotaForm.item_code" /></label>
          <label class="grow">定额子目名称<input v-model="quotaForm.name" /></label>
          <label>定额单位<input v-model="quotaForm.unit" /></label>
        </div>
        <details style="margin: 8px 0;">
          <summary style="cursor: pointer; font-weight: 600; color: #334155;">高级：查看/编辑定额匹配 JSON</summary>
          <JsonField v-model="boqText" label="boq_item（清单项）" :rows="5" />
          <JsonField v-model="quotaText" label="quota_items[]（定额子目候选）" :rows="6" />
          <JsonField v-model="mappingText" label="exact_mappings" :rows="3" />
        </details>
        <button class="btn primary" :disabled="busy" @click="match">执行匹配</button>
      </div>

      <div class="card">
        <h3>附注换算 <code>POST /v1/quota/apply-notes</code></h3>
        <div class="form-row">
          <label>人材机编码<input v-model="noteForm.resource_code" /></label>
          <label>基准消耗量<input v-model="noteForm.consumption" /></label>
          <label>换算类型
            <select v-model="noteForm.effect_type">
              <option value="FACTOR">FACTOR（系数乘算）</option>
              <option value="REPLACE">REPLACE（含量替换）</option>
              <option value="INFO">INFO（仅提示）</option>
            </select>
          </label>
          <label>换算系数/值<input v-model="noteForm.factor" /></label>
        </div>
        <label>定额章说明/附注出处条款<input v-model="noteForm.source_ref" /></label>
        <details style="margin: 8px 0;">
          <summary style="cursor: pointer; font-weight: 600; color: #334155;">高级：查看/编辑附注换算 JSON</summary>
          <JsonField v-model="consumptionText" label="consumption[]" :rows="4" />
          <JsonField v-model="notesText" label="notes[]" :rows="6" />
          <JsonField v-model="conversionText" label="conversion" :rows="2" />
        </details>
        <label class="check"><input v-model="formal" type="checkbox" /> formal_calculation（正式）</label>
        <button class="btn primary" :disabled="busy" @click="applyNotes">执行换算</button>
      </div>
    </div>

    <ResultPanel :result="result" :error="error" :busy="busy" label="定额接口响应" />
  </section>
</template>
