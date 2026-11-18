<script setup lang="ts">
import {ref,computed,watch} from 'vue';
import {api,authorizedFetch} from '../api';
import {session,loadJob} from '../session';
import type {Json} from '../types';
const chosen=ref<File|null>(null);const uploaded=ref<Json|null>(null);const result=ref<Json|null>(null);const prices=ref<Json[]>([]);
const source=ref<Json|null>(null);const error=ref('');const message=ref('');const busy=ref(false);const q=ref('沥青混凝土');const spec=ref('');const unit=ref('');const period=ref('2026-Q2');const region=ref('410100-INNER-4RING');
const download=ref('');const jobs=ref<Json[]>([]);const task=ref<Json|null>(null);
const sourceDocument=computed(()=>(source.value?.document as Json)||{});
const sourceRegister=computed(()=>(source.value?.source as Json)||{});
const structuredItems=computed(()=>(result.value?.items as Json[]||[]).slice(0,15));
async function run(fn:()=>Promise<void>){busy.value=true;error.value='';try{await fn();}catch(e){error.value=e instanceof Error?e.message:String(e);}finally{busy.value=false;}}
function pick(e:Event){chosen.value=(e.target as HTMLInputElement).files?.[0]||null;uploaded.value=null;result.value=null;message.value='';}
watch(()=>session.jobId,()=>{uploaded.value=null;result.value=null;download.value='';task.value=null;message.value='';});
async function upload(){await run(async()=>{
  if(!chosen.value||!session.job)throw new Error('请先创建或选择项目，再选择文件');
  const path=[session.job.project_id,chosen.value.name].map(encodeURIComponent).join('/');
  const bytes=await chosen.value.arrayBuffer();const sha=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))).map(x=>x.toString(16).padStart(2,'0')).join('');
  const r=await authorizedFetch('/files/'+path+'?job_id='+session.jobId,{method:'PUT',headers:{'X-Content-SHA256':sha},body:bytes});
  uploaded.value=await r.json();message.value='上传成功，SHA256 已校验';await loadJob();
});}
function fileBody(){if(!uploaded.value)throw new Error('请先上传原件');return {job_id:session.jobId,path:uploaded.value.doc_key,version:uploaded.value.version};}
async function parse(){await run(async()=>{const p=await api.parseFile(fileBody());result.value={...p,blocks:(p.blocks as Json[]||[]).slice(0,15),metadata:{...(p.metadata as Json),sheets:((p.metadata as Json)?.sheets as Json[]||[]).map(s=>({name:s.name,state:s.state,header_mapping:s.header_mapping,merged_cells:s.merged_cells,rows:(s.rows as Json[]||[]).slice(0,3)}))}};});}
async function ocr(){await run(async()=>{message.value='正在离线识别，扫描资料可能需要数分钟';const p=await api.parseFile({...fileBody(),ocr:true});result.value={...p,blocks:(p.blocks as Json[]||[]).slice(0,15),metadata:{...(p.metadata as Json),tokens:((p.metadata as Json)?.tokens as Json[]||[]).slice(0,15)}};message.value='OCR 返回：'+p.status+'；识别结果须由专业人员人工校对';});}
async function boq(){await run(async()=>{result.value=await api.importSampleBoq(fileBody());message.value='清单导入完成，保留 B_REFERENCE 来源角色，未独立算量';});}
async function query(){prices.value=[];source.value=null;message.value='';await run(async()=>{const r=await api.materialPrices({q:q.value,spec:spec.value,unit:unit.value,region:region.value,period:period.value});prices.value=r.items as Json[];message.value=`匹配 ${r.total} 条，展示前 ${prices.value.length} 条`;});}
async function trace(row:Json){await run(async()=>{const r=await api.source(String(row.document_id));source.value={...r,price_records:(r.price_records as Json[]).slice(0,5),price_record_count:(r.price_records as Json[]).length};});}
async function generate(){await run(async()=>{result.value=await api.generateDeliverable(session.jobId,{kind:'sample_report'});download.value=String(result.value.download_url);message.value='样本结果已生成，可下载核对';});}
async function downloadFile(){await run(async()=>{const r=await authorizedFetch(download.value);const blob=await r.blob();const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download='样本清单导入结果.xlsx';link.click();URL.revokeObjectURL(link.href);});}
async function list(){await run(async()=>{jobs.value=(await api.jobs()).items as Json[];});}
async function select(j:Json){await loadJob(String(j.job_id));uploaded.value=null;download.value='';}
async function flow(){await run(async()=>{const t=await api.createTask(session.jobId,'IMPORT',{scope:'SAMPLE_IMPORT_ONLY'});task.value=await api.taskAction(String(t.task_id),'start',{});task.value=await api.taskAction(String(t.task_id),'complete',{result:{sample_only:true,accuracy_verification:'BLOCKED'}});message.value='样本导入流程已完成';});}
async function acl(){await run(async()=>{if(!session.job)throw new Error('请先选择项目');result.value=await api.projectAcl(session.job.project_id);});}
</script>
<template>
<section>
  <div class="page-header"><div><h2>真实资料样本试点</h2><p class="hint">资料接入、清单导入及郑州价格查询；工程量、定额、组价精度仍待专业验证。</p></div><button class="btn" @click="list" data-testid="list-jobs">刷新项目列表</button></div>
  <p v-if="error" class="notice bad" role="alert" data-testid="pilot-error">{{error}}</p><p v-if="message" class="notice" data-testid="pilot-message">{{message}}</p>
  <div v-if="jobs.length" class="card"><button v-for="j in jobs" :key="String(j.job_id)" class="btn" @click="select(j)">{{j.name}} · {{j.status}}</button></div>
  <div class="grid-2">
    <div class="card"><h3>原件上传与结构化</h3><p>{{session.job?.name||'请在「项目」页创建项目'}}</p>
      <label>选择原始资料<input type="file" @change="pick" data-testid="pilot-file" /></label>
      <div class="btn-row"><button class="btn primary" :disabled="busy||!chosen||!session.job" @click="upload" data-testid="upload-file">上传并校验</button><button class="btn" :disabled="busy||!uploaded" @click="parse" data-testid="parse-file">解析资料</button><button class="btn" :disabled="busy||!uploaded" @click="boq" data-testid="import-sample-boq">导入样本清单</button></div>
      <p v-if="uploaded" class="mono" data-testid="upload-hash">SHA256 {{uploaded.file_sha256}} · 版本 {{uploaded.version}}</p>
      <button class="btn" :disabled="busy||!uploaded||!chosen||!(/\.(pdf|png|jpe?g)$/i.test(chosen.name))" @click="ocr" data-testid="offline-ocr">扫描件离线识别（待人工校对）</button>
      <div class="btn-row"><button class="btn" :disabled="busy||!session.job" @click="flow" data-testid="sample-flow">运行导入流程</button><button class="btn" :disabled="busy||!session.job" @click="acl">查看成员权限</button></div>
      <p v-if="task" data-testid="task-state">流程状态：{{task.status}}</p>
    </div>
    <div class="card"><h3>样本结果导出</h3><p class="hint">导出清单原始工程量及逐单元格来源。专业计算精度验证：BLOCKED。</p><button class="btn primary" :disabled="busy||!session.job" @click="generate" data-testid="generate-sample">生成样本结果</button><button v-if="download" class="btn" @click="downloadFile" data-testid="download-sample">下载结果 XLSX</button></div>
  </div>
  <div class="card"><h3>官方材料价格查询</h3><div class="form-row">
    <label>名称<input v-model="q" data-testid="price-name" /></label><label>规格<input v-model="spec" data-testid="price-spec" /></label><label>单位<input v-model="unit" data-testid="price-unit" /></label>
    <label>期次<select v-model="period" data-testid="price-period"><option>2026-Q2</option><option>2026-Q3</option><option>2026-09</option><option>2026-Q4</option><option>2026-10</option><option>2025-Q3</option></select></label>
    <label>地区<select v-model="region" data-testid="price-region"><option value="410100-INNER-4RING">郑州市区四环以内</option><option value="410300">洛阳（缺少依据）</option></select></label><button class="btn primary" @click="query" :disabled="busy" data-testid="query-prices">查询价格</button>
  </div><p class="hint">指定期次的样本参考价。Q2、9月通知口径待核实；不按两列比值推定税率。勘误保留原值和版本历史。</p>
    <div class="table-wrap"><table data-testid="price-results"><thead><tr><th>材料 / 规格</th><th>单位</th><th>含税价</th><th>不含税价</th><th>版本</th><th>来源</th></tr></thead><tbody><tr v-for="p in prices" :key="String(p.record_id)"><td>{{p.material_name}} · {{p.spec_model}}</td><td>{{p.unit}}</td><td>{{p.price_incl_tax}}</td><td>{{p.price_excl_tax}}</td><td>{{(p.version_history as unknown[]).length>1?'已勘误，原值保留':'原始'}} · {{p.scope_status}}</td><td><button class="btn" @click="trace(p)">查看来源</button></td></tr><tr v-if="!prices.length"><td colspan="6" class="hint">{{busy?'正在查询…':'暂无本次查询结果，请填写条件后查询；错误条件不会显示上次价格。'}}</td></tr></tbody></table></div>
  </div>
  <div v-if="source" class="card"><h3>来源追溯</h3>
    <p><b>原始文件：</b>{{sourceDocument.local_path}}</p><p class="mono"><b>SHA256：</b>{{sourceDocument.sha256}}</p>
    <p><b>登记状态：</b>{{sourceRegister.verification_status}} · {{sourceRegister.license_status}} · 关联 {{source.price_record_count}} 条记录</p>
    <p v-if="sourceRegister.official_url"><a :href="String(sourceRegister.official_url)" target="_blank" rel="noopener noreferrer">查看登记的官方来源</a></p>
    <details><summary>逐行定位与完整登记字段</summary><pre data-testid="source-detail">{{JSON.stringify(source,null,2)}}</pre></details>
  </div>
  <div v-if="result" class="card"><h3>结构化结果</h3>
    <p>状态：{{result.status||result.kind||'已返回'}} · 明细 {{(result.items as unknown[]||[]).length}} 条 · {{result.data_role||''}}</p>
    <div v-if="structuredItems.length" class="table-wrap"><table><thead><tr><th>清单编码</th><th>名称</th><th>单位</th><th>原始工程量</th><th>工作表 / 行</th></tr></thead><tbody><tr v-for="(item,i) in structuredItems" :key="i"><td>{{item.code}}</td><td>{{item.name}}</td><td>{{item.unit}}</td><td>{{item.quantity}}</td><td>{{item.sheet}} / {{item.row}}</td></tr></tbody></table><p class="hint">预览前15条；导出保留全部明细与单元格定位。</p></div>
    <details><summary>查看结构化字段与追溯位置</summary><pre data-testid="structured-result">{{JSON.stringify(result,null,2)}}</pre></details>
  </div>
</section>
</template>
<style scoped>pre{max-height:520px;overflow:auto;white-space:pre-wrap;word-break:break-all;font-size:12px}.page-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}.card{margin-bottom:20px}.mono{overflow-wrap:anywhere}</style>
