<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import type { Component } from "vue";
import ProjectPage from "./pages/ProjectPage.vue";
import DocumentsPage from "./pages/DocumentsPage.vue";
import BoqPage from "./pages/BoqPage.vue";
import QuantityPage from "./pages/QuantityPage.vue";
import QuotaPage from "./pages/QuotaPage.vue";
import PricingPage from "./pages/PricingPage.vue";
import ReviewPage from "./pages/ReviewPage.vue";
import DeliverablePage from "./pages/DeliverablePage.vue";
import EvidencePage from "./pages/EvidencePage.vue";
import { ENDPOINTS } from "./endpoints";
import {
  applyActor, applyApiBase, clearCurrentJob, loadJob, refreshHealth, session,
} from "./session";
import { traceLog } from "./useApi";
import {api} from './api';
import {auth,clearAuth} from './auth';
import { clearRecentJobs } from './config';
import LoginPanel from './components/LoginPanel.vue';
import SamplePilotPage from './pages/SamplePilotPage.vue';
import OcrReviewPage from './pages/OcrReviewPage.vue';
import ProjectReadinessPage from './pages/ProjectReadinessPage.vue';
import CockpitPage from './pages/CockpitPage.vue';
import BidWorkbenchPage from './pages/BidWorkbenchPage.vue';
import { httpObservations, latestObservation, observationLabel } from './diagnostics';

const pages: Record<string, Component> = {
  cockpit:CockpitPage,
  bid:BidWorkbenchPage,
  ocr:OcrReviewPage,
  readiness:ProjectReadinessPage,
  pilot:SamplePilotPage,
  project: ProjectPage,
  documents: DocumentsPage,
  boq: BoqPage,
  quantity: QuantityPage,
  quota: QuotaPage,
  pricing: PricingPage,
  review: ReviewPage,
  deliverable: DeliverablePage,
  evidence: EvidencePage,
};

const tabs = [
  {key:'cockpit',label:'工序驾驶舱',hint:'五态 / 门禁 / 返工单'},
  {key:'bid',label:'投标与报价策略',hint:'13红线 / 七字段 / 六表样'},
  {key:'ocr',label:'OCR 人工复核',hint:'页图 / 校对 / 历史'},
  {key:'readiness',label:'资料完整性',hint:'资料矩阵 / 来源状态'},
  {key:'pilot',label:'样本试点',hint:'接入 / 追溯 / 导出'},
  { key: "project", label: "项目", hint: "Job / 依据包" },
  { key: "documents", label: "资料", hint: "指纹 / 版本" },
  { key: "boq", label: "清单", hint: "导入 / 校验" },
  { key: "quantity", label: "算量", hint: "规则 / 算子" },
  { key: "quota", label: "定额", hint: "匹配 / 附注" },
  { key: "pricing", label: "组价", hint: "单价 / 调差 / 汇总" },
  { key: "review", label: "复核", hint: "复算 / 门禁 / 审批" },
  { key: "deliverable", label: "成果", hint: "十类 / 前置条件" },
  { key: "evidence", label: "证据", hint: "验签 / 审计" },
];

const current = computed<Component>(() => pages[session.page] || ProjectPage);
const baseDraft = ref(session.apiBase);
const actorDraft = ref(session.actor);
const jobDraft = ref(session.jobId);
const connCount = computed(() => Object.values(ENDPOINTS).filter((e) => e.state === "connected").length);
const totalCount = computed(() => Object.keys(ENDPOINTS).length);
const allEndpoints = Object.values(ENDPOINTS);
const healthOk = computed(() => session.health?.status === "ok");
const testOnly = computed(() => session.health?.test_only === true && session.health?.environment === 'TEST_ONLY');
const recentTrace = computed(() => traceLog.value.slice(0, 4));
const identityRoles = ref<string[]>([]);
const identityError = ref('');
const projectRole = ref('');
const probedCount = computed(() => allEndpoints.filter(e => latestObservation(e)).length);
const respondedCount = computed(() => allEndpoints.filter(e => latestObservation(e)?.ok).length);
async function refreshIdentity(){
  identityError.value='';identityRoles.value=[];
  if(!auth.token)return;
  try{const me=await api.me();identityRoles.value=Array.isArray(me.roles)?me.roles.map(String):[];}
  catch(e){identityError.value=e instanceof Error?e.message:String(e);}
}
async function logout(){try{await api.logout();}finally{clearAuth();clearCurrentJob();}}
watch(()=>auth.token,token=>{
  projectRole.value='';
  if(!token){clearCurrentJob();clearRecentJobs();identityRoles.value=[];}
  else{void refreshIdentity();}
});
watch(()=>session.jobId,id=>{jobDraft.value=id;});
watch(()=>session.job?.project_id,async projectId=>{
  projectRole.value='';
  if(!projectId||!auth.token)return;
  try{const result=await api.projectAcl(projectId);if(session.job?.project_id===projectId)projectRole.value=String(result.role||'无项目角色');}
  catch{if(session.job?.project_id===projectId)projectRole.value='权限状态未确认';}
});

function saveBase(): void {
  applyApiBase(baseDraft.value);
  baseDraft.value = session.apiBase;
  refreshHealth();
}

function saveActor(): void {
  applyActor(actorDraft.value);
  actorDraft.value = session.actor;
}

async function openJob(): Promise<void> {
  await loadJob(jobDraft.value);
  jobDraft.value = session.jobId;
}

let timer = 0;
onMounted(async () => {
  timer = window.setInterval(() => {void refreshHealth();if(auth.token)void refreshIdentity();}, 30000);
  await refreshHealth();
  await refreshIdentity();
  if (auth.token && session.jobId) await loadJob();
});
onUnmounted(() => { if (timer) window.clearInterval(timer); });
</script>

<template>
  <LoginPanel v-if="!auth.token" />
  <div v-else class="app">
    <header class="topbar">
      <div class="brand">
        <div class="logo">TX</div>
        <div>
          <h1>天寻 Buddy 智能造价工作台</h1>
          <p class="sub" data-testid="frontend-version">V4.2.6 · 数据治理与人工复核工作台</p>
        </div>
      </div>
      <div class="health">
        <span data-testid="current-identity">当前身份：{{auth.username}}<template v-if="identityRoles.length"> · {{identityRoles.join(' / ')}}</template></span><button class="btn" @click="logout" data-testid="logout">退出登录</button>
        <span class="badge" :class="healthOk ? 'ok' : 'bad'" data-testid="backend-status">
          {{ healthOk ? "后端已连接" : "后端未连接" }}
        </span>
        <span class="hint" v-if="healthOk">
          后端 v{{ session.health?.version }} · 规则 {{ session.health?.rules_loaded }} · 精度 {{ session.health?.precision }} · 探测 {{session.healthCheckedAt}}
        </span>
        <span class="hint" v-else>{{ session.healthError || "请启动 backend：uvicorn txbuddy.api:app --port 8010" }}</span>
      </div>
    </header>
    <div class="runtime-status" data-testid="runtime-status" role="status">
      <span class="badge" :class="testOnly ? 'pending' : 'bad'" data-testid="runtime-environment">{{testOnly?'TEST_ONLY · 后端已声明隔离测试环境':'环境未确认 · 请通过专用启动脚本启动'}}</span>
      <span>正式业务验收：未通过 · production_accepted=false · accuracy_E=BLOCKED</span>
      <span v-if="identityError" class="bad-text">身份查询：{{identityError}}</span>
    </div>

    <div class="settings">
      <label>API 地址<input v-model="baseDraft" placeholder="留空 = 同源代理（vite preview → 8010）" @keyup.enter="saveBase" /></label>
      <button class="btn" @click="saveBase">保存并重连</button>
      <label>审计备注 X-Actor（不授予权限）<input v-model="actorDraft" @blur="saveActor" /></label>
      <label class="grow">当前项目 Job ID<input v-model="jobDraft" @keyup.enter="openJob" /></label>
      <button class="btn" :disabled="session.loadingJob" @click="openJob">载入</button>
      <span v-if="session.job" class="job-chip">
        <b>{{ session.job.name }}</b> · {{ session.job.business_line }} · {{ session.job.status }} · {{projectRole||'正在读取项目角色…'}}
      </span>
      <span v-else class="hint">{{ session.jobError || "未选择项目" }}</span>
    </div>

    <div class="layout">
      <nav class="sidenav">
        <button
          v-for="t in tabs" :key="t.key"
          :class="{ on: session.page === t.key }"
          @click="session.page = t.key"
        >
          <span class="nav-label">{{ t.label }}</span>
          <span class="nav-hint">{{ t.hint }}</span>
        </button>
        <div class="nav-foot">
          <div>静态登记 {{ totalCount }} 项</div>
          <div>有接线 {{ connCount }} · 未实现 {{ totalCount - connCount }}</div>
          <div data-testid="endpoint-probe-count">本次已请求 {{probedCount}} 项 · 最近成功 {{respondedCount}} 项</div>
          <div>请求观察值，不代表完整可用率</div>
        </div>
              <div style="margin-top: 12px; padding-top: 10px; border-top: 1px dashed #cbd5e1; font-size: 12px; color: #64748b;">
          <div style="font-weight: 600; margin-bottom: 4px;">二期预留模块（PRD 1.3 占位）</div>
          <div>🔒 智能招标 · <span class="hint">模块已预留，待开发</span></div>
          <div>🔒 智能标书 · <span class="hint">模块已预留，待开发</span></div>
          <div>🔒 智能咨询 · <span class="hint">模块已预留，待开发</span></div>
          <div>🔒 智能设计 · <span class="hint">模块已预留，待开发</span></div>
        </div>
      </nav>

      <main class="main">
        <component :is="current" :key="session.page+':'+auth.username" />
        <details class="registry">
          <summary>端点静态登记与本次请求观察（登记 {{ totalCount }} 项 · 已请求 {{probedCount}} 项）</summary>
          <div class="table-wrap short">
            <table>
              <thead><tr><th>静态登记</th><th>方法 / 路径</th><th>本次最近请求</th><th>说明</th></tr></thead>
              <tbody>
                <tr v-for="e in allEndpoints" :key="e.key">
                  <td><span class="badge" :class="e.state">{{ e.state === "connected" ? "已登记接线" : "尚未实现" }}</span></td>
                  <td><code>{{ e.method }} {{ e.path }}</code></td>
                  <td>{{observationLabel(e)}}<span v-if="latestObservation(e)" class="hint"> · {{latestObservation(e)?.ms}}ms</span></td>
                  <td>
                    {{ e.label }}
                    <div v-if="e.state === 'unconnected'" class="ep-note">未接通原因：{{ e.note }}</div>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
        </details>
        <div class="trace">
          <div class="trace-head">最近请求</div>
          <p class="hint">本次已观察 {{httpObservations.length}} 次 HTTP 请求；权限拒绝与业务阻断保留真实状态，不计作接口可用率。</p>
          <div v-for="(t, i) in recentTrace" :key="i" class="trace-row">
            <span class="badge" :class="t.ok ? 'ok' : 'bad'">{{ t.ok ? "OK" : "ERR" }}</span>
            <span class="mono">{{ t.label }}</span>
            <span class="hint">{{ t.status || "-" }} · {{ t.ms }}ms</span>
            <span v-if="t.message" class="hint ellipsis">{{ t.message }}</span>
          </div>
          <div v-if="!recentTrace.length" class="hint">本页尚未发起请求。</div>
        </div>
      </main>
    </div>
  </div>
</template>
