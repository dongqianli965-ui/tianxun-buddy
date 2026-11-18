<script setup lang="ts">
import { onMounted, ref, watch } from "vue";
import { api } from "../api";
import EndpointPanel from "../components/EndpointPanel.vue";
import JsonField from "../components/JsonField.vue";
import PageHeader from "../components/PageHeader.vue";
import ResultPanel from "../components/ResultPanel.vue";
import { DEFAULT_PACK, session, setPack, loadJob, refreshHealth, useJob } from "../session";
import { formatError, useApi } from "../useApi";
import { BUSINESS_LINES, type BasisPack, type Job } from "../types";
import { listRecentJobs, type RecentJob } from "../config";

const { busy, error, result, run } = useApi();
const packText = ref(JSON.stringify(session.pack, null, 2));
const packError = ref("");
const projectId = ref("P-" + new Date().toISOString().slice(0, 10).replace(/-/g, ""));
const name = ref("新建造价项目");
const line = ref("L1");
const jobIdInput = ref(session.jobId);
const recent = ref<RecentJob[]>(listRecentJobs());
const createMsg = ref("");
const serverJobs = ref<Job[]>([]);
const listBusy = ref(false);
const listError = ref("");

// 结构化依据包表单字段（告别造价师手写 JSON）
const regionCode = ref(session.pack.region_code || "410100");
const majorCode = ref(session.pack.major || "BUILDING");
const ruleScope = ref<"HENAN_2025" | "HENAN_LEGACY" | "NATIONAL">(session.pack.rule_version_scope || "HENAN_2025");
const packMode = ref<"TEST_DRAFT" | "FORMAL_LOCKED">(session.pack.test_only ? "TEST_DRAFT" : "FORMAL_LOCKED");
const effectiveDate = ref(session.pack.effective_date || "2026-01-01");

function syncFormToPackJson(): void {
  const isFormal = packMode.value === "FORMAL_LOCKED";
  const built: BasisPack = {
    basis_pack_id: isFormal ? `B-HN-${regionCode.value}-${majorCode.value}-FORMAL` : "B-WEB-TEST",
    region_code: regionCode.value,
    region_level: regionCode.value.endsWith("00") ? "CITY" : "COUNTY",
    major: majorCode.value,
    effective_date: effectiveDate.value,
    measurement_standard_version: "GB/T 50854-2024",
    pricing_standard_version: "GB/T 50500-2024",
    quota_pack_version: isFormal ? "HA-QUOTA-2025-V1" : null,
    rule_version_scope: ruleScope.value,
    precision_rule_version: "P-V1",
    status: isFormal ? "LOCKED" : "DRAFT",
    test_only: !isFormal,
    sources: isFormal
      ? [
          {
            source_id: "S-HN-QUOTA-2025",
            title: "河南省建设工程工程量清单计价实施细则及消耗量定额（已核验授权）",
            doc_no: "豫建科〔2025〕173号",
            source_type: "QUOTA",
            license_status: "LICENSED",
            verified: true,
          },
          {
            source_id: "S-HN-PRICE-2026Q3",
            title: "郑州市及各地市建设工程材料基准价格信息（已核验）",
            doc_no: "2026-Q3",
            source_type: "PRICE",
            license_status: "LICENSED",
            verified: true,
          },
        ]
      : [
          {
            source_id: "S-WEB-TEST",
            title: "前端联调用测试依据（非授权）",
            source_type: "TEST",
            license_status: "TEST_ONLY",
            verified: false,
          },
        ],
  };
  packText.value = JSON.stringify(built, null, 2);
  setPack(built);
}

watch([regionCode, majorCode, ruleScope, packMode, effectiveDate], () => {
  syncFormToPackJson();
});

async function refreshProjects(){
  listBusy.value=true;listError.value='';
  try{const r=await api.jobs();serverJobs.value=Array.isArray(r.items)?r.items as Job[]:[];}
  catch(e){serverJobs.value=[];listError.value=formatError(e);}
  finally{listBusy.value=false;}
}

function parsePack(): BasisPack | null {
  try {
    const parsed = JSON.parse(packText.value);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      packError.value = "依据包必须是 JSON 对象"; return null;
    }
    packError.value = "";
    return parsed as BasisPack;
  } catch (e) {
    packError.value = "JSON 解析失败：" + (e instanceof Error ? e.message : String(e));
    return null;
  }
}

function savePackLocal(): void {
  const pack = parsePack();
  if (pack) { setPack(pack); packError.value = ""; }
}

function resetPack(): void {
  setPack(JSON.parse(JSON.stringify(DEFAULT_PACK)) as BasisPack);
  packText.value = JSON.stringify(session.pack, null, 2);
  regionCode.value = "410100";
  majorCode.value = "BUILDING";
  ruleScope.value = "HENAN_2025";
  packMode.value = "TEST_DRAFT";
}

async function createProject(): Promise<void> {
  createMsg.value = "";
  const pack = parsePack();
  if (!pack) return;
  setPack(pack);
  const job = await run("POST /v1/job", () => api.createJob({
    project_id: projectId.value.trim(),
    name: name.value.trim(),
    business_line: line.value,
    basis_pack: pack,
    created_by: session.actor,
  }));
  if (job) {
    useJob(job as Job);
    jobIdInput.value = job.job_id;
    recent.value = listRecentJobs();
    createMsg.value = `已创建 ${job.job_id}，正在读取聚合详情…`;
    await loadJob(job.job_id);
    await refreshHealth();
    await refreshProjects();
  }
}

async function openJob(id: string): Promise<void> {
  jobIdInput.value = id;
  await run("GET /v1/job/{job_id}", () => loadJob(id).then((j) => j ?? Promise.reject(new Error(session.jobError))));
  recent.value = listRecentJobs();
}

onMounted(() => {
  refreshProjects();
  recent.value = listRecentJobs();
  if (session.job) jobIdInput.value = session.job.job_id;
});
</script>

<template>
  <section>
    <PageHeader title="项目" desc="六业务线共用 Job 聚合根：通过下拉表单一键组装造价依据包、创建项目、绑定文档/任务/门禁/算量关联。">
      <button class="btn" :disabled="busy" @click="openJob(jobIdInput)">载入当前项目</button>
    </PageHeader>

    <EndpointPanel :keys="['jobCreate', 'jobGet', 'jobList', 'basisValidate', 'authLogin', 'aclProjects']" />
    <div class="card"><h3>服务端项目列表</h3><button class="btn" :disabled="listBusy" @click="refreshProjects">{{listBusy?'正在查询项目…':'刷新项目'}}</button><p v-if="listError" class="err" role="alert" data-testid="project-list-error">{{listError}}</p><p v-if="!listBusy&&!listError&&!serverJobs.length" class="hint">当前身份没有可访问项目。</p><button class="btn" v-for="job in serverJobs" :key="job.job_id" @click="openJob(job.job_id)">{{job.name}} · {{job.status}}</button></div>

    <div class="grid-2">
      <div class="card">
        <h3>新建项目（结构化依据包向导）</h3>
        <div class="form-row">
          <label>项目编号<input v-model="projectId" /></label>
          <label>项目名称<input v-model="name" /></label>
        </div>
        <div class="form-row">
          <label>业务线
            <select v-model="line">
              <option v-for="l in BUSINESS_LINES" :key="l.code" :value="l.code">{{ l.label }}</option>
            </select>
          </label>
          <label>创建人<input :value="session.actor" disabled /></label>
        </div>

        <div class="form-row">
          <label>所属地区（定额/信息价区域）
            <select v-model="regionCode">
              <option value="410100">410100 · 郑州市（四环内）</option>
              <option value="411323">411323 · 南阳市西峡县（第二地域）</option>
              <option value="410300">410300 · 洛阳市市区</option>
              <option value="410000">410000 · 河南省通用标准</option>
            </select>
          </label>
          <label>工程专业（P0/P1 全专业矩阵）
            <select v-model="majorCode">
              <option value="BUILDING">BUILDING · 房屋建筑与装饰工程（P0）</option>
              <option value="INSTALLATION">INSTALLATION · 通用安装工程（P0）</option>
              <option value="MUNICIPAL">MUNICIPAL · 市政工程（P1）</option>
              <option value="LANDSCAPE">LANDSCAPE · 园林绿化工程（P1）</option>
            </select>
          </label>
        </div>

        <div class="form-row">
          <label>规则与费用体系
            <select v-model="ruleScope">
              <option value="HENAN_2025">HENAN_2025 · 河南省 2025 现行计价规则</option>
              <option value="HENAN_LEGACY">HENAN_LEGACY · 河南省 2016 旧版定额口径</option>
              <option value="NATIONAL">NATIONAL · GB/T 50500-2024 国标通用</option>
            </select>
          </label>
          <label>依据包准入状态
            <select v-model="packMode">
              <option value="TEST_DRAFT">测试演练包（DRAFT + TEST_ONLY）</option>
              <option value="FORMAL_LOCKED">已核验授权包（LOCKED + LICENSED）</option>
            </select>
          </label>
          <label>基准生效日期<input v-model="effectiveDate" type="date" /></label>
        </div>

        <details style="margin: 8px 0;">
          <summary style="cursor: pointer; font-weight: 600; color: #334155;">高级：查看或手动微调依据包 JSON（已随上方表单自动生成）</summary>
          <JsonField v-model="packText" label="依据包 basis_pack（完整对象，后端 extra=forbid）" :rows="10"
            hint="上方下拉框改动会自动同步至此 JSON；无需手写 JSON。" />
        </details>

        <p v-if="packError" class="err">{{ packError }}</p>
        <div class="btn-row">
          <button class="btn primary" :disabled="busy || !projectId.trim() || !name.trim()" @click="createProject">
            创建项目
          </button>
          <button class="btn" @click="savePackLocal">保存依据包到本机</button>
          <button class="btn" @click="resetPack">恢复测试依据包</button>
          <button class="btn" @click="run('POST /v1/basis/validate', () => api.validateBasis(parsePack() || session.pack))">
            校验依据包
          </button>
        </div>
        <p v-if="createMsg" class="ok-text">{{ createMsg }}</p>
      </div>

      <div class="card">
        <h3>当前项目</h3>
        <div class="form-row">
          <label class="grow">Job ID<input v-model="jobIdInput" placeholder="job_..." /></label>
          <button class="btn" :disabled="busy" @click="openJob(jobIdInput)">读取</button>
          <button class="btn" :disabled="busy" @click="run('GET /v1/job/{job_id}', () => loadJob())">刷新</button>
        </div>
        <p v-if="session.jobError" class="err">{{ session.jobError }}</p>
        <div v-if="session.job" class="kv">
          <div><span>名称</span><b>{{ session.job.name }}</b></div>
          <div><span>状态</span><b>{{ session.job.status }}</b></div>
          <div><span>业务线</span><b>{{ session.job.business_line }} {{ session.job.business_line_label || "" }}</b></div>
          <div><span>依据包</span><b>{{ session.job.basis_pack_id }}</b></div>
          <div><span>资料 / 任务</span><b>{{ session.job.documents?.length || 0 }} / {{ session.job.tasks?.length || 0 }}</b></div>
          <div><span>门禁 / 算量</span><b>{{ session.job.gate_records?.length || 0 }} / {{ session.job.calc_runs?.length || 0 }}</b></div>
        </div>
        <h4>本机最近项目（仅缓存 job_id，详情实时读取）</h4>
        <ul class="recent">
          <li v-for="r in recent" :key="r.job_id">
            <button class="link" @click="openJob(r.job_id)">{{ r.name || r.job_id }}</button>
            <span class="hint">{{ r.business_line }} · {{ r.job_id }}</span>
          </li>
          <li v-if="!recent.length" class="hint">暂无记录。创建项目后会写入本机索引。</li>
        </ul>
      </div>
    </div>

    <ResultPanel :result="result" :error="error" :busy="busy" label="项目接口响应" />
  </section>
</template>
