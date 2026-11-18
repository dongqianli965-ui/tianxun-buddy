<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api } from "../api";
import EndpointPanel from "../components/EndpointPanel.vue";
import JsonField from "../components/JsonField.vue";
import PageHeader from "../components/PageHeader.vue";
import ResultPanel from "../components/ResultPanel.vue";
import { loadJob, session, withJob } from "../session";
import { useApi } from "../useApi";
import type { GateRecord, Json } from "../types";

const { busy, error, result, run } = useApi();
const gates = ref<string[]>([]);
const gateCode = ref("");
const gateResults = ref<GateRecord[]>([]);
const sequenceError = ref("");
const snapshotText = ref("{}");
const storedValue = ref("");
const formal = ref(false);
const taskId = ref("");
const taskActor = ref(session.actor);
const taskRole = ref("REVIEWER");
const taskLicense = ref("");
const comment = ref("");
const jgate = ref<Json | null>(null);

const line = computed(() => session.job?.business_line || "L1");
const availableDocs = computed(() => (session.job?.documents || []).map((d) => d.doc_key));

onMounted(async () => {
  await loadSequence(line.value);
});

async function loadSequence(businessLine: string): Promise<void> {
  sequenceError.value = "";
  try {
    const out = await api.gateSequence(businessLine);
    gates.value = out.gates;
    gateCode.value = out.gates[0] || "";
  } catch (e) {
    sequenceError.value = e instanceof Error ? e.message : String(e);
  }
}

async function checkGate(): Promise<void> {
  const body = withJob({
    business_line: line.value,
    gate_code: gateCode.value,
    available_documents: availableDocs.value,
    context: {},
  });
  const out = await run(`POST /v1/gate/check ${gateCode.value}`, () => api.gateCheck(body));
  if (out) gateResults.value = [out as GateRecord, ...gateResults.value].slice(0, 20);
}

function failedText(g: GateRecord): string {
  const failed = g.failed_rules || [];
  return failed.map((f) => `${f.rule_id || ""} ${f.error_code || ""} ${f.message || ""}`.trim()).join("；");
}

async function fillSnapshot(): Promise<void> {
  const runId = session.lastCalcRunId.trim();
  if (!runId) { sequenceError.value = "请先在「算量」页产生 calc_run"; return; }
  const out = await run("GET /v1/calc/{run_id}", () => api.getCalcRun(runId));
  const rec = out as Record<string, unknown> | null;
  if (rec) {
    snapshotText.value = JSON.stringify(rec.snapshot || rec, null, 2);
    storedValue.value = String(rec.result ?? "");
  }
}

async function recalc(): Promise<void> {
  let snapshot: Json;
  try {
    const parsed = JSON.parse(snapshotText.value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("快照必须是 JSON 对象");
    snapshot = parsed as Json;
  } catch (e) {
    sequenceError.value = "快照 JSON 解析失败：" + (e instanceof Error ? e.message : String(e));
    return;
  }
  await run("POST /v1/calc/recalc", () => api.recalc({
    ...withJob({ snapshot, stored_value: storedValue.value.trim(), formal_calculation: formal.value }),
  }));
}

async function approveTask(): Promise<void> {
  await run("POST /v1/task/{task_id}/approve", () => api.taskAction(taskId.value.trim(), "approve", {
    actor: taskActor.value.trim(),
    role: taskRole.value,
    license: taskLicense.value.trim() || null,
    comment: comment.value,
    result: {},
    reason: "",
  }));
}

async function loadJgate(): Promise<void> {
  if (!session.jobId) { sequenceError.value = "请先选择项目"; return; }
  const out = await run("GET /v1/jgate/{job_id}", () => api.jgateStatus(session.jobId));
  if (out) jgate.value = out as Json;
}

async function runJgate(): Promise<void> {
  if (!session.jobId) { sequenceError.value = "请先选择项目"; return; }
  const out = await run("POST /v1/jgate/{job_id}/run", () => api.jgateRun(session.jobId, session.actor));
  if (out) jgate.value = out as Json;
}
</script>

<template>
  <section>
    <PageHeader title="复核" desc="复算、门禁判定、任务审批与鉴定门影子运行；门禁必须绑定 job_id 由服务端读真实状态。">
      <button class="btn" :disabled="busy" @click="run('GET /v1/job/{job_id}', () => loadJob())">刷新项目状态</button>
    </PageHeader>

    <EndpointPanel :keys="['recalc', 'calcGet', 'gateCheck', 'gateSequence', 'taskGet', 'taskAction', 'jgateStatus', 'jgateRun']" />

    <div class="grid-2">
      <div class="card">
        <h3>门禁判定 <code>POST /v1/gate/check</code></h3>
        <div class="form-row">
          <label>业务线<input :value="line" disabled /></label>
          <label class="grow">门禁
            <select v-model="gateCode">
              <option v-for="g in gates" :key="g" :value="g">{{ g }}</option>
            </select>
          </label>
          <button class="btn primary" :disabled="busy || !gateCode" @click="checkGate">判定</button>
        </div>
        <p v-if="sequenceError" class="err">{{ sequenceError }}</p>
        <p class="hint">
          可判定文档：{{ availableDocs.length ? availableDocs.join(" / ") : "（当前项目未登记资料）" }}；
          job_id：{{ session.jobId || "未选择（权威门将返回 FAIL）" }}
        </p>
        <div class="table-wrap">
          <table>
            <thead><tr><th>门</th><th>结果</th><th>失败规则</th></tr></thead>
            <tbody>
              <tr v-for="(g, i) in gateResults" :key="i">
                <td>{{ g.gate || g.gate_code }}</td>
                <td><span class="badge" :class="g.result === 'PASS' ? 'ok' : 'bad'">{{ g.result || g.status }}</span></td>
                <td class="small">{{ failedText(g) || "-" }}</td>
              </tr>
              <tr v-if="!gateResults.length"><td colspan="3" class="hint">尚未判定。</td></tr>
            </tbody>
          </table>
        </div>
        <p class="hint">门禁记录由服务端写入 job.gate_records，可在项目页看到累计条数。</p>
      </div>

      <div class="card">
        <h3>复算 <code>POST /v1/calc/recalc</code></h3>
        <JsonField v-model="snapshotText" label="snapshot（从 calc_run 快照读取，或手工粘贴）" :rows="10" />
        <div class="form-row">
          <label class="grow">存档值 stored_value<input v-model="storedValue" /></label>
          <button class="btn" :disabled="busy" @click="fillSnapshot">用最近 calc_run 填充</button>
        </div>
        <label class="check"><input v-model="formal" type="checkbox" /> formal_calculation（正式）</label>
        <button class="btn primary" :disabled="busy || !storedValue.trim()" @click="recalc">执行复算</button>
        <p class="hint">最近 calc_run：{{ session.lastCalcRunId || "（无）" }}</p>
      </div>
    </div>

    <div class="grid-2">
      <div class="card">
        <h3>任务审批 <code>POST /v1/task/{task_id}/approve</code></h3>
        <div class="form-row">
          <label class="grow">task_id<input v-model="taskId" placeholder="task_..." /></label>
          <button class="btn" :disabled="busy || !taskId.trim()" @click="run('GET /v1/task/{task_id}', () => api.getTask(taskId.trim()))">
            读取任务
          </button>
        </div>
        <div class="form-row">
          <label>审批人<input v-model="taskActor" /></label>
          <label>角色<select v-model="taskRole"><option>REVIEWER</option><option>APPROVER</option><option>EDITOR</option></select></label>
          <label class="grow">执业资格 license<input v-model="taskLicense" placeholder="非鉴定线可留空" /></label>
        </div>
        <label>审批意见<input v-model="comment" /></label>
        <button class="btn primary" :disabled="busy || !taskId.trim()" @click="approveTask">提交审批</button>
        <p class="hint">任务节点与状态由后端 workflow 校验；前端不做任何本地状态推进。</p>
      </div>

      <div class="card">
        <h3>鉴定线影子运行 <code>/v1/jgate/{job_id}</code></h3>
        <p class="hint">仅 L6 鉴定业务使用；六门只做影子运行，不自动签发成果。</p>
        <div class="btn-row">
          <button class="btn" :disabled="busy || !session.jobId" @click="loadJgate">读取 J0–J5 状态</button>
          <button class="btn" :disabled="busy || !session.jobId" @click="runJgate">执行影子运行</button>
        </div>
        <pre v-if="jgate">{{ JSON.stringify(jgate, null, 2).slice(0, 4000) }}</pre>
      </div>
    </div>

    <ResultPanel :result="result" :error="error" :busy="busy" label="复核接口响应" />
  </section>
</template>
