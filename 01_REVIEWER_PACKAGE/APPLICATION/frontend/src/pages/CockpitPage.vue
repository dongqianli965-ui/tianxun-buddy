<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { api } from "../api";
import EndpointPanel from "../components/EndpointPanel.vue";
import PageHeader from "../components/PageHeader.vue";
import ResultPanel from "../components/ResultPanel.vue";
import { go, session } from "../session";
import { useApi } from "../useApi";
import type { Json } from "../types";

const { busy, error, result, run } = useApi();
const activeLine = ref<string>(session.job?.business_line || "L1");
const userMode = ref<"NOVICE" | "EXPERT">("NOVICE");
const activeGateTemplate = ref<"BUDGET" | "SETTLEMENT" | "TENDER">("BUDGET");
const overview = ref<Json | null>(null);

// 九字段返工单表单
const rwForm = reactive({
  project_id: session.job?.project_id || "P-20261117",
  source: "GATE",
  business_type: "L1",
  owner: "李工（安装编制人）",
  major: "INSTALLATION",
  issue: "消防水泵房防水套管与土建预埋件跨专业重复计取",
  location: "清单项 030402001001 / 结施-08",
  clause: "GB/T 50856-2024 第4.2条",
  expected_action: "核减安装专业重复计取的刚性防水套管 12 个",
  deadline: "2026-11-20",
  before_value: "24 个",
  after_value: "12 个（已扣除土建已含12个）",
});
const lastReworkId = ref("");

// 双轨提取与合成表单（PRD 7.1.5）
const dtForm = reactive({
  project_id: session.job?.project_id || "P-20261117",
  document_id: "DOC-CONTRACT-001",
  manual_amount: "12500000.00",
  manual_days: "365",
  ai_amount: "12500000.00",
  ai_days: "360",
  adopted_from: "MANUAL",
  reason: "按补充协议第2条工期明确修正为365日历天",
  page_ref: "补充协议第3页第2款",
});

// 广联达 L2 任务书与回读偏差 >5% 校验表单（PRD 6.3 & 7.4.4）
const gldForm = reactive({
  project_id: session.job?.project_id || "P-20261117",
  taskbook_type: "MODELING",
  item_code: "010401001001",
  name: "现浇混凝土矩形柱 C30",
  estimate_qty: "100.00",
  qty_tool: "108.50",
  diff_reason: "定额口径含损耗及柱帽节点并入计算（偏差 +8.50% 已核实）",
});
const lastTaskbookId = ref("");
const lastInstructionId = ref("");

async function loadCockpit(): Promise<void> {
  const pid = session.job?.project_id || rwForm.project_id || "P-20261117";
  const data = await run("GET /v1/cockpit/overview", () => api.cockpitOverview(pid, activeLine.value, userMode.value));
  if (data) overview.value = data;
}

async function createRework(): Promise<void> {
  const out = await run("POST /v1/rework/create", () => api.reworkCreate({ ...rwForm }));
  if (out && out.rework_order && typeof (out.rework_order as Json).id === "string") {
    lastReworkId.value = String((out.rework_order as Json).id);
    await loadCockpit();
  }
}

async function rectifyAndCloseRework(): Promise<void> {
  if (!lastReworkId.value) return;
  await run("POST /v1/rework/rectify", () => api.reworkRectify({ id: lastReworkId.value, after_value: rwForm.after_value }));
  await run("POST /v1/rework/close", () => api.reworkClose({ id: lastReworkId.value, approved: true, reviewer: session.actor }));
  await loadCockpit();
}

async function runDualTrackSynthesis(): Promise<void> {
  await run("POST /v1/doc/dual-extract (MANUAL)", () => api.docDualExtract({
    project_id: dtForm.project_id,
    document_id: dtForm.document_id,
    track_mode: "MANUAL",
    actor: session.actor,
    extracted_fields: { contract_amount: dtForm.manual_amount, duration_days: dtForm.manual_days },
  }));
  await run("POST /v1/doc/dual-extract (AI)", () => api.docDualExtract({
    project_id: dtForm.project_id,
    document_id: dtForm.document_id,
    track_mode: "AI",
    actor: "DOCUMENT_AGENT",
    extracted_fields: { contract_amount: dtForm.ai_amount, duration_days: dtForm.ai_days },
  }));
  await run("POST /v1/doc/final-synthesize", () => api.docFinalSynthesize({
    project_id: dtForm.project_id,
    document_id: dtForm.document_id,
    actor: session.actor,
    field_decisions: {
      duration_days: {
        adopted_from: dtForm.adopted_from,
        reason: dtForm.reason,
        page_ref: dtForm.page_ref,
      },
    },
  }));
}

async function runGlodonL2Loop(): Promise<void> {
  const tb = await run("POST /v1/gld/taskbook", () => api.gldTaskbook({
    project_id: gldForm.project_id,
    taskbook_type: gldForm.taskbook_type,
    items: [{ item_code: gldForm.item_code, name: gldForm.name, estimate_qty: gldForm.estimate_qty, unit: "m3" }],
  }));
  if (tb && typeof tb.taskbook_id === "string") {
    lastTaskbookId.value = tb.taskbook_id;
    const insts = Array.isArray(tb.instructions) ? tb.instructions as Json[] : [];
    lastInstructionId.value = insts.length ? String(insts[0].instruction_id) : "";
    await run("POST /v1/gld/readback", () => api.gldReadback({
      taskbook_id: lastTaskbookId.value,
      readback_items: [{
        instruction_id: lastInstructionId.value,
        qty_tool: gldForm.qty_tool,
        diff_reason: gldForm.diff_reason,
      }],
    }));
  }
}

onMounted(() => {
  loadCockpit();
});
</script>

<template>
  <section>
    <PageHeader title="工序驾驶舱与闭环中心（PRD 第 12–13 章）" desc="五区布局、工序节点五态、三套业务线质量门模板、九字段返工单闭环、人工+AI双轨独立提取合成与广联达 L2 任务书回读校验。">
      <div class="btn-row">
        <button class="btn" :class="{ primary: userMode === 'NOVICE' }" @click="userMode = 'NOVICE'; loadCockpit()">新人模式（带向导与门禁强拦）</button>
        <button class="btn" :class="{ primary: userMode === 'EXPERT' }" @click="userMode = 'EXPERT'; loadCockpit()">专家模式（精简批量视图）</button>
      </div>
    </PageHeader>

    <EndpointPanel :keys="['cockpitOverview', 'reworkCreate', 'reworkRectify', 'reworkClose', 'docDualExtract', 'docFinalSynthesize', 'gldTaskbook', 'gldReadback']" />

    <div class="card">
      <div class="form-row" style="align-items: center; justify-content: space-between;">
        <div>
          <strong>当前业务线工序带（节点五态）：</strong>
          <select v-model="activeLine" @change="loadCockpit" style="margin-left: 8px;">
            <option value="L1">L1 · 预算编制生产线</option>
            <option value="L2">L2 · 预算审核生产线</option>
            <option value="L3">L3 · 结算编制生产线</option>
            <option value="L4">L4 · 结算审核生产线</option>
            <option value="L5">L5 · 投标预算生产线（代做标书）</option>
          </select>
        </div>
        <button class="btn" @click="go('bid')">进入投标预算与报价策略台 →</button>
      </div>

      <div v-if="overview && Array.isArray(overview.nodes_five_states)" class="form-row" style="gap: 10px; margin-top: 12px;">
        <div v-for="n in (overview.nodes_five_states as any[])" :key="n.id"
          style="flex: 1; padding: 10px; border-radius: 6px; border: 1px solid #cbd5e1; background: #f8fafc;">
          <div style="font-weight: 700;">{{ n.id }} · {{ n.name }}</div>
          <span class="badge" :class="n.state === 'DONE' ? 'ok' : (n.state === 'FAILED' ? 'err' : '')">
            {{ n.label }} ({{ n.state }})
          </span>
        </div>
      </div>

      <div v-if="overview && overview.six_kpis" class="kv" style="margin-top: 12px;">
        <div><span>工序达成率</span><b>{{ (overview.six_kpis as any).process_completion_rate }}</b></div>
        <div><span>门禁一次通过率</span><b>{{ (overview.six_kpis as any).gate_first_pass_rate }}</b></div>
        <div><span>当前返工单数</span><b>{{ (overview.six_kpis as any).rework_rate }}</b></div>
        <div><span>任务超期率</span><b>{{ (overview.six_kpis as any).task_overdue_rate }}</b></div>
        <div><span>待拍板平均停留</span><b>{{ (overview.six_kpis as any).pending_decision_hours }}</b></div>
        <div><span>投标偏差控制目标</span><b>{{ (overview.six_kpis as any).bid_win_rate_and_deviation }}</b></div>
      </div>
    </div>

    <div class="card">
      <h3>按业务线分模板的质量门视图（共用 12 个标准错误码）</h3>
      <div class="tabs">
        <button :class="{ on: activeGateTemplate === 'BUDGET' }" @click="activeGateTemplate = 'BUDGET'">预算四门（Q1/Q2/Q4/Q5）</button>
        <button :class="{ on: activeGateTemplate === 'SETTLEMENT' }" @click="activeGateTemplate = 'SETTLEMENT'">结算四门（Q1/Q2核对/Q4/Q5）</button>
        <button :class="{ on: activeGateTemplate === 'TENDER' }" @click="activeGateTemplate = 'TENDER'">投标三门（T1红线/T2清单/T5出表）</button>
      </div>
      <div v-if="overview && overview.gate_templates" style="margin-top: 10px;">
        <p class="hint"><strong>{{ (overview.gate_templates as any)[activeGateTemplate]?.label }}</strong></p>
        <div class="form-row">
          <div v-for="g in ((overview.gate_templates as any)[activeGateTemplate]?.gates || [])" :key="g.code"
            style="flex: 1; padding: 10px; border: 1px solid #e2e8f0; border-radius: 6px;">
            <div><strong>{{ g.code }} · {{ g.name }}</strong> <code style="color: #b91c1c;">{{ g.error_code }}</code></div>
            <p class="hint" style="margin-top: 4px;">放行条件：{{ g.condition }}</p>
          </div>
        </div>
      </div>
    </div>

    <div class="grid-2">
      <div class="card">
        <h3>九字段返工闭环（PRD 12.6：同一问题满 3 次自动入规则候选库）</h3>
        <div class="form-row">
          <label>项目编号<input v-model="rwForm.project_id" /></label>
          <label>来源
            <select v-model="rwForm.source">
              <option value="GATE">GATE（门禁阻断触发）</option>
              <option value="REVIEW">REVIEW（三级复核退回）</option>
            </select>
          </label>
          <label>专业<input v-model="rwForm.major" /></label>
        </div>
        <div class="form-row">
          <label>责任人<input v-model="rwForm.owner" /></label>
          <label>定位（清单项/图号）<input v-model="rwForm.location" /></label>
          <label>依据条款<input v-model="rwForm.clause" /></label>
        </div>
        <div class="form-row">
          <label class="grow">问题描述<input v-model="rwForm.issue" /></label>
          <label class="grow">期望整改动作<input v-model="rwForm.expected_action" /></label>
        </div>
        <div class="form-row">
          <label>改前原值<input v-model="rwForm.before_value" /></label>
          <label class="grow">改后新值（保留对照）<input v-model="rwForm.after_value" /></label>
          <label>期限<input v-model="rwForm.deadline" type="date" /></label>
        </div>
        <div class="btn-row">
          <button class="btn primary" :disabled="busy" @click="createRework">生成九字段返工单</button>
          <button class="btn" :disabled="busy || !lastReworkId" @click="rectifyAndCloseRework">
            提交改前改后对照并复核关闭 ({{ lastReworkId || "先建单" }})
          </button>
        </div>
      </div>

      <div class="card">
        <h3>资料双轨物理隔离提取与合成（PRD 7.1.5 五条强制规则）</h3>
        <p class="hint">人工轨迹与 AI 轨迹独立存储，缺一不可；存在差异的字段必须指明采纳来源、理由与原文页码出处。</p>
        <div class="form-row">
          <label>资料编号<input v-model="dtForm.document_id" /></label>
          <label>人工提取工期(天)<input v-model="dtForm.manual_days" /></label>
          <label>AI提取工期(天)<input v-model="dtForm.ai_days" /></label>
        </div>
        <div class="form-row">
          <label>差异采纳路径
            <select v-model="dtForm.adopted_from">
              <option value="MANUAL">采自人工（MANUAL）</option>
              <option value="AI">采自 AI（AI）</option>
              <option value="MANUAL_REVISED">人工修正值（MANUAL_REVISED）</option>
            </select>
          </label>
          <label class="grow">合成理由<input v-model="dtForm.reason" /></label>
          <label>原文页码出处<input v-model="dtForm.page_ref" /></label>
        </div>
        <div class="btn-row">
          <button class="btn primary" :disabled="busy" @click="runDualTrackSynthesis">
            执行双轨提取并合成唯一最终稿（过 Q1 门）
          </button>
        </div>

        <hr style="margin: 14px 0; border: 0; border-top: 1px solid #e2e8f0;" />
        <h3>广联达 L2 参数级任务书与读回校验（偏差 &gt;5% 强制说明）</h3>
        <div class="form-row">
          <label>清单项<input v-model="gldForm.name" /></label>
          <label>系统预估量(m3)<input v-model="gldForm.estimate_qty" /></label>
          <label>广联达读回量(m3)<input v-model="gldForm.qty_tool" /></label>
        </div>
        <label>偏差超 5% 差异原因说明（留空将触发 422 硬阻断）<input v-model="gldForm.diff_reason" /></label>
        <div class="btn-row" style="margin-top: 8px;">
          <button class="btn primary" :disabled="busy" @click="runGlodonL2Loop">
            生成广联达任务书并校验读回结果
          </button>
        </div>
      </div>
    </div>

    <ResultPanel :result="result" :error="error" :busy="busy" label="驾驶舱与闭环接口响应" />
  </section>
</template>
