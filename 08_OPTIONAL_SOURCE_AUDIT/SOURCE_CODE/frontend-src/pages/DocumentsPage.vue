<script setup lang="ts">
import { computed, ref } from "vue";
import { api } from "../api";
import EndpointPanel from "../components/EndpointPanel.vue";
import PageHeader from "../components/PageHeader.vue";
import ResultPanel from "../components/ResultPanel.vue";
import { loadJob, session } from "../session";
import { formatError, useApi } from "../useApi";

const { busy, error, result, run } = useApi();
const docKey = ref("BOQ_XLSX");
const version = ref("v1");
const sha = ref("");
const verified = ref(false);
const fileNote = ref("");
const docs = computed(() => session.job?.documents || []);

async function pickFile(ev: Event): Promise<void> {
  const input = ev.target as HTMLInputElement;
  const file = input.files?.[0];
  fileNote.value = "";
  if (!file) return;
  docKey.value = file.name;
  try {
    const buffer = await file.arrayBuffer();
    const digest = await crypto.subtle.digest("SHA-256", buffer);
    sha.value = Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, "0")).join("");
    fileNote.value = `已计算 SHA-256（${(file.size / 1024).toFixed(1)} KiB）。原件上传和结构化请打开「样本试点」。`;
  } catch (e) {
    fileNote.value = "浏览器无法计算指纹（非安全上下文）：" + formatError(e) + "，请手工粘贴 SHA-256。";
  }
}

async function register(): Promise<void> {
  const jobId = session.jobId;
  if (!jobId) { fileNote.value = "请先在「项目」页创建或载入项目"; return; }
  await run("POST /v1/job/{job_id}/documents", () => api.addDocument(jobId, {
    doc_key: docKey.value.trim(),
    version: version.value.trim(),
    file_sha256: sha.value.trim(),
    verified: verified.value,
  }).then(async (out) => { await loadJob(jobId); return out; }));
}

async function refresh(): Promise<void> {
  await run("GET /v1/job/{job_id}", () => loadJob());
}
</script>

<template>
  <section>
    <PageHeader title="资料" desc="登记资料元数据与版本；原件上传、SHA256 校验及结构化请使用样本试点。">
      <button class="btn primary" @click="session.page='pilot'">上传原件与解析</button>
      <button class="btn" :disabled="busy" @click="refresh">刷新资料</button>
    </PageHeader>

    <EndpointPanel :keys="['docAdd', 'jobGet', 'filePut', 'parseFile']" />

    <div class="notice warn">原件接入、结构化结果和清单样本导出已接通。<button class="btn" @click="session.page='pilot'">打开样本试点</button> 人工核验状态须由真实人员确认。</div>

    <div class="grid-2">
      <div class="card">
        <h3>登记资料</h3>
        <label class="file-pick">
          <span class="field-label">选择本地文件（仅算指纹，不上传）</span>
          <input type="file" @change="pickFile" />
        </label>
        <p v-if="fileNote" class="hint">{{ fileNote }}</p>
        <div class="form-row">
          <label class="grow">资料键 doc_key<input v-model="docKey" placeholder="BOQ_XLSX" /></label>
          <label>版本<input v-model="version" placeholder="v1" /></label>
        </div>
        <label>文件 SHA-256<input v-model="sha" placeholder="64 位十六进制" /></label>
        <label class="check"><input v-model="verified" type="checkbox" /> 已人工核验（verified）</label>
        <div class="btn-row">
          <button class="btn primary" :disabled="busy || !session.jobId || !docKey.trim() || !sha.trim() || !version.trim()"
            @click="register">登记资料</button>
        </div>
        <p v-if="!session.jobId" class="err">请先在「项目」页创建或载入项目。</p>
      </div>

      <div class="card">
        <h3>项目资料清单</h3>
        <div class="table-wrap"><table>
          <thead><tr><th>doc_key</th><th>版本</th><th>指纹</th><th>核验</th><th>登记时间</th></tr></thead>
          <tbody>
            <tr v-for="d in docs" :key="d.doc_key + d.version">
              <td>{{ d.doc_key }}</td>
              <td>{{ d.version }}</td>
              <td class="mono">{{ (d.file_sha256 || "").slice(0, 16) }}…</td>
              <td>{{ d.verified ? "是" : "否" }}</td>
              <td>{{ d.added_at || "-" }}</td>
            </tr>
            <tr v-if="!docs.length"><td colspan="5" class="hint">当前项目无资料记录。</td></tr>
          </tbody>
        </table></div>
        <p class="hint">表格数据来自 <code>GET /v1/job/{job_id}</code> 的 documents 字段，刷新页面后仍以服务端为准。</p>
      </div>
    </div>

    <ResultPanel :result="result" :error="error" :busy="busy" label="资料接口响应" />
  </section>
</template>
