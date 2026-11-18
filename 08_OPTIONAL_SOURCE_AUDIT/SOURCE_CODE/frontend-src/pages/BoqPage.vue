<script setup lang="ts">
import { computed, ref } from "vue";
import { api } from "../api";
import EndpointPanel from "../components/EndpointPanel.vue";
import PageHeader from "../components/PageHeader.vue";
import ResultPanel from "../components/ResultPanel.vue";
import { loadJob, session, setBoqItem } from "../session";
import { useApi } from "../useApi";
import type { Json } from "../types";

interface BoqItem {
  row?: number; code?: string; name?: string; feature?: string; unit?: string; quantity?: string; section?: string;
}
interface BoqResult {
  items?: BoqItem[]; errors?: Json[]; warnings?: Json[];
  stats?: { rows?: number; valid?: number; invalid?: number };
  content_sha256?: string; file_sha256?: string; sheet?: string;
  [key: string]: unknown;
}

const { busy, error, result, run } = useApi();
const file = ref<File | null>(null);
const imported = ref<BoqResult | null>(null);
const savedMsg = ref("");
const stored = ref<Json[]>([]);
const filter = ref("");

const items = computed<BoqItem[]>(() => imported.value?.items || []);
const visibleItems = computed(() => {
  const q = filter.value.trim().toLowerCase();
  const list = q
    ? items.value.filter((i) => `${i.code} ${i.name} ${i.feature} ${i.section}`.toLowerCase().includes(q))
    : items.value;
  return list.slice(0, 300);
});
const errorCount = computed(() => imported.value?.errors?.length || 0);
const warningCount = computed(() => imported.value?.warnings?.length || 0);

function pick(ev: Event): void {
  const input = ev.target as HTMLInputElement;
  file.value = input.files?.[0] || null;
  imported.value = null;
  savedMsg.value = "";
}

async function doImport(): Promise<void> {
  if (!file.value) return;
  savedMsg.value = "";
  const data = await run("POST /v1/boq/import", () => api.importBoq(file.value as File));
  if (data) imported.value = data as BoqResult;
}

async function saveToJob(): Promise<void> {
  if (!imported.value) return;
  const jobId = session.jobId;
  if (!jobId) { savedMsg.value = "请先在「项目」页创建或载入项目"; return; }
  savedMsg.value = "";
  await run("POST /v1/job/{job_id}/records (kind=boq)", () => api.putRecord(jobId, "boq", {
    schema: "boq.norm.v1",
    sheet: imported.value?.sheet,
    content_sha256: imported.value?.content_sha256,
    file_sha256: imported.value?.file_sha256,
    stats: imported.value?.stats,
    items: imported.value?.items || [],
    errors: imported.value?.errors || [],
    warnings: imported.value?.warnings || [],
  }).then(async (out) => { savedMsg.value = "清单已写入 job 记录（kind=boq，CLIENT 来源）"; await loadJob(jobId); return out; }));
}

async function loadStored(): Promise<void> {
  const jobId = session.jobId;
  if (!jobId) { savedMsg.value = "请先在「项目」页创建或载入项目"; return; }
  const out = await run("GET /v1/job/{job_id}/records?kind=boq", () => api.getRecords(jobId, "boq"));
  if (out) stored.value = out.records;
}

function choose(item: BoqItem): void {
  setBoqItem({
    code: item.code, name: item.name, feature: item.feature,
    unit: item.unit, quantity: item.quantity, section: item.section,
  });
  savedMsg.value = `已选中 ${item.code} ${item.name}，可切换到「定额」页带入匹配。`;
}
</script>

<template>
  <section>
    <PageHeader title="清单" desc="上传工程量清单（xlsx），由后端 /v1/boq/import 真实解析并给出校验报告与内容哈希。">
      <button class="btn" :disabled="busy || !session.jobId" @click="loadStored">读取项目已存清单</button>
    </PageHeader>

    <EndpointPanel :keys="['boqImport', 'recordsPut', 'recordsGet', 'jobGet']" />

    <div class="card">
      <div class="form-row">
        <label class="grow">清单文件（.xlsx；必需列：项目编码/项目名称/计量单位/工程量）
          <input type="file" accept=".xlsx,.xlsm" @change="pick" />
        </label>
        <button class="btn primary" :disabled="busy || !file" @click="doImport">导入并校验</button>
      </div>
      <p class="hint">解析与校验全部在服务端完成；本页不内置任何示例清单数据。</p>
    </div>

    <div v-if="imported" class="card">
      <div class="stat-row">
        <div class="stat"><span>总行数</span><b>{{ imported.stats?.rows ?? "-" }}</b></div>
        <div class="stat"><span>有效行</span><b>{{ imported.stats?.valid ?? 0 }}</b></div>
        <div class="stat"><span>错误行</span><b class="bad-text">{{ errorCount }}</b></div>
        <div class="stat"><span>告警</span><b>{{ warningCount }}</b></div>
        <div class="stat wide"><span>内容哈希</span><b class="mono">{{ imported.content_sha256 || "-" }}</b></div>
      </div>
      <div class="form-row">
        <label class="grow">筛选<input v-model="filter" placeholder="编码 / 名称 / 特征 / 分部" /></label>
        <button class="btn" :disabled="busy || !items.length || !session.jobId" @click="saveToJob">写入项目记录</button>
      </div>
      <p v-if="savedMsg" class="ok-text">{{ savedMsg }}</p>
      <div class="table-wrap">
        <table>
          <thead><tr><th>行</th><th>编码</th><th>名称</th><th>特征</th><th>单位</th><th>工程量</th><th>分部</th><th></th></tr></thead>
          <tbody>
            <tr v-for="(it, idx) in visibleItems" :key="(it.code || '') + '-' + idx">
              <td>{{ it.row }}</td><td class="mono">{{ it.code }}</td><td>{{ it.name }}</td>
              <td class="ellipsis">{{ it.feature }}</td><td>{{ it.unit }}</td><td class="num">{{ it.quantity }}</td>
              <td>{{ it.section }}</td>
              <td><button class="link" @click="choose(it)">带入定额</button></td>
            </tr>
            <tr v-if="!visibleItems.length"><td colspan="8" class="hint">没有可显示的有效清单行。</td></tr>
          </tbody>
        </table>
      </div>
      <details v-if="imported.errors?.length">
        <summary>错误明细（{{ imported.errors.length }}）</summary>
        <pre>{{ JSON.stringify(imported.errors, null, 2) }}</pre>
      </details>
      <details v-if="imported.warnings?.length">
        <summary>告警明细（{{ imported.warnings.length }}）</summary>
        <pre>{{ JSON.stringify(imported.warnings, null, 2) }}</pre>
      </details>
    </div>

    <div v-if="stored.length" class="card">
      <h3>项目已存清单记录（kind=boq）</h3>
      <div v-for="(rec, i) in stored" :key="i" class="stored-rec">
        <div class="hint">记录 {{ i + 1 }} · {{ String(rec._source || "") }} · 条目 {{ Array.isArray(rec.items) ? rec.items.length : 0 }}</div>
        <pre>{{ JSON.stringify(rec.stats || {}, null, 2) }}</pre>
      </div>
    </div>

    <ResultPanel :result="result" :error="error" :busy="busy" label="清单接口响应" />
  </section>
</template>
