<script setup lang="ts">
import { onMounted, ref } from "vue";
import { api } from "../api";
import EndpointPanel from "../components/EndpointPanel.vue";
import JsonField from "../components/JsonField.vue";
import PageHeader from "../components/PageHeader.vue";
import { session } from "../session";
import { useApi } from "../useApi";
import type { AuditItem, EvidenceRecord, Json } from "../types";

const { busy, error, result, run } = useApi();
const evidenceId = ref(session.lastEvidenceId);
const evidence = ref<EvidenceRecord | null>(null);
const verifyText = ref("");
const audit = ref<AuditItem[]>([]);
const calcRunId = ref(session.lastCalcRunId);
const localError = ref("");

async function loadEvidence(): Promise<void> {
  localError.value = "";
  const id = evidenceId.value.trim();
  if (!id) { localError.value = "请输入 evidence_id"; return; }
  const out = await run("GET /v1/evidence/{eid}", () => api.evidence(id));
  if (out) {
    evidence.value = out as EvidenceRecord;
    verifyText.value = JSON.stringify(out, null, 2);
    session.lastEvidenceId = id;
  }
}

async function verify(): Promise<void> {
  localError.value = "";
  let payload: Json;
  try {
    const parsed = JSON.parse(verifyText.value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("证据必须是 JSON 对象");
    payload = parsed as Json;
  } catch (e) {
    localError.value = "证据 JSON 解析失败：" + (e instanceof Error ? e.message : String(e));
    return;
  }
  await run("POST /v1/evidence/verify", () => api.verifyEvidence(payload));
}

async function loadAudit(): Promise<void> {
  const out = await run("GET /v1/audit?limit=50", () => api.audit(50));
  if (out) audit.value = out.items;
}

async function loadCalcRun(): Promise<void> {
  const id = calcRunId.value.trim();
  if (!id) { localError.value = "请输入 calc_run_id"; return; }
  await run("GET /v1/calc/{run_id}", () => api.getCalcRun(id));
}

onMounted(() => {
  if (session.lastEvidenceId) evidenceId.value = session.lastEvidenceId;
});

function fmt(v: unknown): string {
  if (v === null || v === undefined || v === "") return "-";
  return typeof v === "object" ? JSON.stringify(v) : String(v);
}
</script>

<template>
  <section>
    <PageHeader title="证据" desc="证据详情、SHA-256 验签、审计流水与 calc_run 追溯；验签由后端重算摘要。">
      <button class="btn" :disabled="busy" @click="loadAudit">刷新审计流水</button>
    </PageHeader>

    <EndpointPanel :keys="['evidenceGet', 'evidenceVerify', 'audit', 'calcGet']" />

    <div class="card">
      <div class="form-row">
        <label class="grow">evidence_id
          <input v-model="evidenceId" placeholder="evidence_id（24 位摘要前缀）" />
        </label>
        <button class="btn primary" :disabled="busy || !evidenceId.trim()" @click="loadEvidence">读取证据</button>
      </div>
      <p class="hint">最近一次算量产生的 evidence_id：{{ session.lastEvidenceId || "（无）" }}</p>
      <p v-if="localError" class="err">{{ localError }}</p>
      <div v-if="evidence" class="kv">
        <div><span>subject</span><b>{{ fmt(evidence.subject_type) }} / {{ fmt(evidence.subject_id) }}</b></div>
        <div><span>结果</span><b>{{ fmt(evidence.result) }} {{ fmt(evidence.unit) }}</b></div>
        <div><span>公式</span><b class="mono">{{ fmt(evidence.formula) }}</b></div>
        <div><span>sha256</span><b class="mono">{{ fmt(evidence.sha256) }}</b></div>
        <div><span>生产时间</span><b>{{ fmt(evidence.created_at) }}</b></div>
      </div>
      <pre v-if="evidence">{{ JSON.stringify(evidence, null, 2) }}</pre>
    </div>

    <div class="grid-2">
      <div class="card">
        <h3>证据验签 <code>POST /v1/evidence/verify</code></h3>
        <JsonField v-model="verifyText" label="evidence 对象（读取证据后自动填充，可粘贴）" :rows="12" />
        <button class="btn primary" :disabled="busy || !verifyText.trim()" @click="verify">重新计算并比对摘要</button>
        <p class="hint">验签会重算规范化载荷 SHA-256，并检查文件绑定/定位信息是否完整。</p>
        <pre v-if="result">{{ JSON.stringify(result, null, 2) }}</pre>
      </div>

      <div class="card">
        <h3>calc_run 追溯 <code>GET /v1/calc/{run_id}</code></h3>
        <div class="form-row">
          <label class="grow">calc_run_id<input v-model="calcRunId" /></label>
          <button class="btn" :disabled="busy || !calcRunId.trim()" @click="loadCalcRun">读取</button>
        </div>
        <p class="hint">calc_run 保存输入快照与 snapshot_sha256，是复算与勾稽的上游凭据。</p>
        <h3>审计流水（最新 50 条）</h3>
        <div class="table-wrap short">
          <table>
            <thead><tr><th>时间</th><th>操作者</th><th>动作</th><th>对象</th><th>详情</th></tr></thead>
            <tbody>
              <tr v-for="(a, i) in audit" :key="i">
                <td class="small">{{ String(a.at || "").replace('T', ' ').slice(0, 19) }}</td>
                <td>{{ a.actor }}</td><td>{{ a.action }}</td>
                <td class="mono small">{{ a.object_id }}</td><td class="small ellipsis">{{ a.detail }}</td>
              </tr>
              <tr v-if="!audit.length"><td colspan="5" class="hint">点击右上角“刷新审计流水”。</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <p class="hint">本页的读取、验签、审计与追溯结果均来自后端响应；页面不生成、不缓存任何本地证据。</p>
  </section>
</template>
