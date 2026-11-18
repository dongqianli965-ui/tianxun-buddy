<script setup lang="ts">
import { reactive, ref } from "vue";
import { api } from "../api";
import EndpointPanel from "../components/EndpointPanel.vue";
import PageHeader from "../components/PageHeader.vue";
import ResultPanel from "../components/ResultPanel.vue";
import { session } from "../session";
import { useApi } from "../useApi";
import type { Json } from "../types";

const { busy, error, result, run } = useApi();
const step = ref<number>(0);
const bidProjectId = ref<string>("");
const quoteResult = ref<Json | null>(null);
const sheetsResult = ref<Json | null>(null);

const t0Form = reactive({
  project_id: session.job?.project_id || "P-BID-20261117",
  tenderee: "郑州高新产业园建设开发有限公司",
  bid_deadline: "2026-11-30T09:30:00+08:00",
  control_price: "12500000.00",
  bond_amount: "200000.00",
  eplatform: "河南省公共资源交易中心（Excel/PDF + 电子签章）",
  require_glodon_format: false,
  eplatform_requires_software: false,
  has_new_quota_or_special_conversion: false,
});

const addendaClosed = ref(true);

const lockForm = reactive({
  item_code: "010401001001",
  tender_name: "现浇混凝土矩形柱",
  bid_name: "现浇混凝土矩形柱",
  tender_feature: "1.混凝土强度等级:C30 2.柱截面:400×400mm",
  bid_feature: "1.混凝土强度等级:C30 2.柱截面:400×400mm",
  tender_unit: "m³",
  bid_unit: "立方米",
  tender_qty: "450.00",
  bid_qty: "450.00",
});

const quoteForm = reactive({
  version: "v1",
  control_price: "12500000.00",
  labor_cost: "2400000.00",
  material_cost: "6100000.00",
  machine_cost: "850000.00",
  mgmt_cost: "450000.00",
  non_competitive_fee: "1400000.00",
  max_unbalanced_item_ratio: "0.08",
  down_rate: "0.05",
  decided_by: "张总（企业负责人）",
  reason: "参考中性档（得分约58.8~59.5分，毛利率约5.3%），兼顾中标概率与安全边际拍板下浮5%",
});

async function runT0Create(): Promise<void> {
  const res = await run("POST /v1/bid/create", () => api.bidCreate({ ...t0Form }));
  if (res && typeof res.id === "string") {
    bidProjectId.value = res.id;
    step.value = 1;
  }
}

async function runT1Redlines(): Promise<void> {
  if (!bidProjectId.value) await runT0Create();
  const categories = [
    [1, "最高投标限价", "最高投标限价为1250.00万元（含安全文明施工费及规费税金），超过限价直接否决", 8, "投标人须知3.2.1", "设置1250万元硬上限阻断"],
    [2, "工期与质量目标", "总工期365日历天，质量达到国家施工验收合格标准", 9, "投标人须知1.3.2", "响应工期365天"],
    [3, "暂列金额", "暂列金额60.00万元，按招标人给定金额计入总价不得变动", 14, "清单说明2.1", "锁定暂列金额60万元"],
    [4, "暂估价", "专业工程暂估价35.00万元，按给定金额计入", 15, "清单说明2.2", "锁定专业暂估价35万元"],
    [5, "甲供材", "无甲供材料，全部由承包人自行采购", 16, "合同专用条款11.1", "按市场/信息价组价"],
    [6, "不可竞争费", "安全文明施工费、规费、增值税必须按河南省现行标准足额计取，不得作为竞争性费用", 18, "评标办法2.1.3", "足额锁定140万元不可竞争费"],
    [7, "清单锁定要求", "投标人必须严格按招标工程量清单填报，编码、名称、特征、单位、工程量不得修改", 20, "投标人须知3.2.2", "执行七字段100%一致性比对"],
    [8, "成果格式", "提供标准六类投标报价表Excel及签章PDF文件", 22, "投标文件格式第4章", "调用六表样模板引擎"],
    [9, "签章要求", "封面及汇总表须加盖法定代表人印章、单位公章及一级注册造价师执业印章", 24, "投标人须知3.7", "出表前执行签章位完整性检查"],
    [10, "电子标平台", "通过市公共资源交易平台上传加密标书，截止时间前完成签到", 25, "投标人须知4.1", "校验平台文件格式白名单"],
    [11, "评标办法", "综合评估法，评标基准价采用有效投标报价算术平均下浮率法（满分60分）", 30, "评标办法第3章", "载入价格分反推模型"],
    [12, "废标条款", "符合《招标投标法实施条例》第51条之11类否决投标情形之一者一律废标", 32, "评标办法附件A", "出表前逐条跑否决性条款阻断检查"],
    [13, "澄清补遗", "第01号答疑补遗已更新地下室防水做法特征，已同步至最新清单", 35, "01号补遗通知", "确认补遗与清单版本已闭环"],
  ];
  const redlines = categories.map((c) => ({
    category_no: c[0],
    category_name: c[1],
    quote_text: c[2],
    page_no: c[3],
    clause_no: c[4],
    system_interpretation: c[1] + "合规约束",
    required_action: c[5],
    closed: true,
  }));
  const res = await run("POST /v1/bid/redline", () => api.bidRedline({
    bid_project_id: bidProjectId.value,
    redlines,
    addenda_closed: addendaClosed.value,
  }));
  if (res) step.value = 2;
}

async function runT2ListLock(): Promise<void> {
  if (!bidProjectId.value) await runT1Redlines();
  const res = await run("POST /v1/bid/list-lock", () => api.bidListLock({
    bid_project_id: bidProjectId.value,
    tender_items: [{
      item_code: lockForm.item_code,
      item_name: lockForm.tender_name,
      feature: lockForm.tender_feature,
      unit: lockForm.tender_unit,
      qty: lockForm.tender_qty,
    }],
    bid_items: [{
      item_code: lockForm.item_code,
      item_name: lockForm.bid_name,
      feature: lockForm.bid_feature,
      unit: lockForm.bid_unit,
      qty: lockForm.bid_qty,
    }],
  }));
  if (res) step.value = 3;
}

async function runT3QuoteStrategy(): Promise<void> {
  if (!bidProjectId.value) await runT2ListLock();
  const res = await run("POST /v1/bid/quote", () => api.bidQuote({
    bid_project_id: bidProjectId.value,
    ...quoteForm,
  }));
  if (res) {
    quoteResult.value = res;
    step.value = 4;
  }
}

async function runT4ComplianceAndSheets(): Promise<void> {
  if (!quoteResult.value) await runT3QuoteStrategy();
  const targetAmt = (quoteResult.value?.human_decision as any)?.target_amount || "11875000.00";
  const costFloor = (quoteResult.value?.cost_composition as any)?.cost_floor_total || "11200000.00";
  const res = await run("POST /v1/bid/compliance", () => api.bidCompliance({
    bid_project_id: bidProjectId.value,
    bid_total: targetAmt,
    control_price: quoteForm.control_price,
    cost_floor: costFloor,
    non_competitive_fee_full: true,
    seal_positions_complete: true,
    substantial_response: true,
  }));
  if (res) {
    sheetsResult.value = res;
    step.value = 5;
  }
}
</script>

<template>
  <section>
    <PageHeader title="投标预算生产线与报价策略页（PRD 4.10 & 12.7）" desc="T0接标判定 → T1招标文件13类红线门 → T2清单七字段锁定门 → T3三档报价区间与人工拍板（严守D-09不给单一建议价）→ T4/T5合规自检与六类标准表样生成。">
      <span class="badge ok">当前步骤：T{{ step }} / T5</span>
    </PageHeader>

    <EndpointPanel :keys="['bidCreate', 'bidRedline', 'bidListLock', 'bidQuote', 'bidCompliance']" />

    <div class="tabs">
      <button :class="{ on: step === 0 }" @click="step = 0">T0 接标与广联达四条判定</button>
      <button :class="{ on: step === 1 }" @click="step = 1">T1 红线门（13类红线+11类否决）</button>
      <button :class="{ on: step === 2 }" @click="step = 2">T2 清单门（七字段锁定）</button>
      <button :class="{ on: step === 3 || step === 4 }" @click="step = 3">T3 报价策略页（D-09 三档区间）</button>
      <button :class="{ on: step === 5 }" @click="step = 5">T4/T5 合规自检与六表样出表</button>
    </div>

    <div class="card">
      <h3>1. T0 接标立项与“是否必须进广联达”四条规则判定（PRD 4.10.9）</h3>
      <div class="form-row">
        <label>项目编号<input v-model="t0Form.project_id" /></label>
        <label class="grow">招标人名称<input v-model="t0Form.tenderee" /></label>
        <label>最高投标限价(元)<input v-model="t0Form.control_price" /></label>
        <label>投标保证金(元)<input v-model="t0Form.bond_amount" /></label>
      </div>
      <div class="form-row">
        <label class="check"><input v-model="t0Form.require_glodon_format" type="checkbox" /> 条件1：招标文件要求交广联达格式</label>
        <label class="check"><input v-model="t0Form.eplatform_requires_software" type="checkbox" /> 条件2：电子标平台只收软件加密格式</label>
        <label class="check"><input v-model="t0Form.has_new_quota_or_special_conversion" type="checkbox" /> 条件3：含新增重新组价或特殊换算</label>
        <label class="check"><input v-model="addendaClosed" type="checkbox" /> 补遗文件已100%反映至清单</label>
      </div>
      <div class="btn-row">
        <button class="btn primary" :disabled="busy" @click="runT0Create">步骤1：执行 T0 接标与广联达出口判定</button>
        <button class="btn primary" :disabled="busy" @click="runT1Redlines">步骤2：抽取13类红线并过 T1 红线门</button>
      </div>
    </div>

    <div class="card">
      <h3>2. T2 清单七字段锁定校验（GB/T 50500-2024：改码/改名/删特征/改量 100% 硬阻断）</h3>
      <div class="form-row">
        <label>项目编码<input v-model="lockForm.item_code" /></label>
        <label>招标名称<input v-model="lockForm.tender_name" /></label>
        <label>投标名称<input v-model="lockForm.bid_name" /></label>
        <label>招标单位<input v-model="lockForm.tender_unit" /></label>
        <label>投标单位<input v-model="lockForm.bid_unit" /></label>
        <label>招标锁定工程量<input v-model="lockForm.tender_qty" /></label>
        <label>投标填报工程量<input v-model="lockForm.bid_qty" /></label>
      </div>
      <div class="btn-row">
        <button class="btn primary" :disabled="busy" @click="runT2ListLock">步骤3：执行清单七字段一致性校验（过 T2 门）</button>
      </div>
    </div>

    <div class="card">
      <h3>3. T3 报价策略决策页（PRD 12.7 三栏布局 · 严格执行 D-09 口径约束）</h3>
      <p class="notice" style="background: #eff6ff; border-left: 4px solid #1d4ed8; padding: 10px;">
        <strong>口径约束卡（产品硬规则 D-09）：</strong>本页<strong>绝不输出“建议报 XXX 万元”这类单一结论</strong>。原因是投标报价属企业经营决策，系统仅提供保本成本线、激进/中性/保守三档区间、价格分反推与废标风险提示；最终下浮率必须由企业负责人拍板并形成 v1–v3 决策留痕。
      </p>
      <div class="form-row">
        <label>人工成本(元)<input v-model="quoteForm.labor_cost" /></label>
        <label>材料成本(元)<input v-model="quoteForm.material_cost" /></label>
        <label>机械成本(元)<input v-model="quoteForm.machine_cost" /></label>
        <label>管理费(元)<input v-model="quoteForm.mgmt_cost" /></label>
        <label>不可竞争费(安全文明+规费+税金)<input v-model="quoteForm.non_competitive_fee" /></label>
      </div>
      <div class="form-row">
        <label>决策版本号
          <select v-model="quoteForm.version">
            <option value="v1">v1 初算区间版</option>
            <option value="v2">v2 竞争对手博弈调整版</option>
            <option value="v3">v3 最终拍板定案版</option>
          </select>
        </label>
        <label>负责人拍板下浮率（如 0.05 即 5%）<input v-model="quoteForm.down_rate" /></label>
        <label>拍板负责人<input v-model="quoteForm.decided_by" /></label>
        <label class="grow">拍板决策理由（必填入卷）<input v-model="quoteForm.reason" /></label>
      </div>
      <div class="btn-row">
        <button class="btn primary" :disabled="busy" @click="runT3QuoteStrategy">步骤4：测算三档区间并记录人工拍板版本</button>
        <button class="btn primary" :disabled="busy" @click="runT4ComplianceAndSheets">步骤5：执行 T5 合规自检并一键生成六类投标表样</button>
      </div>

      <div v-if="quoteResult && quoteResult.three_tier_ranges" class="form-row" style="gap: 12px; margin-top: 14px;">
        <div v-for="(tier, key) in (quoteResult.three_tier_ranges as any)" :key="key"
          style="flex: 1; padding: 12px; border: 1px solid #cbd5e1; border-radius: 6px; background: #f8fafc;">
          <h4>{{ tier.label }}</h4>
          <p>报价区间：<b class="mono">{{ tier.amount_range[0] }} ~ {{ tier.amount_range[1] }} 元</b></p>
          <p class="hint">中标概率区间：{{ tier.win_probability_band }} · 预计毛利率区间：{{ tier.gross_margin_band }}</p>
        </div>
      </div>

      <div v-if="sheetsResult && Array.isArray(sheetsResult.six_standard_sheets)" style="margin-top: 14px;">
        <h4>已生成六类标准投标报价表样（T5 出表门已放行）：</h4>
        <div class="form-row">
          <div v-for="s in (sheetsResult.six_standard_sheets as any[])" :key="s.sheet_no"
            style="padding: 8px 12px; border: 1px solid #16a34a; border-radius: 6px; background: #f0fdf4;">
            <b>0{{ s.sheet_no }} · {{ s.sheet_name }}</b> ({{ s.status }})
          </div>
        </div>
      </div>
    </div>

    <ResultPanel :result="result" :error="error" :busy="busy" label="投标预算生产线响应" />
  </section>
</template>
