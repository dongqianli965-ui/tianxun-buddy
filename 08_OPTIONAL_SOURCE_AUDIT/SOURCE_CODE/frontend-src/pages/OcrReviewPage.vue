<script setup lang="ts">
import {ref,computed,onMounted,onUnmounted} from 'vue';
import {governance,reviewLabels} from '../governance';
import {authorizedFetch} from '../api';
const documents=ref<any[]>([]),doc=ref<any>(null),page=ref<any>(null);
const query=ref(''),filter=ref(''),selectedPage=ref(''),text=ref(''),fields=ref('{}'),reason=ref('');
const human=ref(false),busy=ref(false),error=ref(''),message=ref(''),imageUrl=ref(''),zoom=ref(1),selectedToken=ref(-1);
let sequence=0;
const diff=computed(()=>{
  const old=(page.value?.original_text||'').split('\n'),next=text.value.split('\n');
  return Array.from({length:Math.max(old.length,next.length)},(_,i)=>({line:i+1,before:old[i]??'',after:next[i]??'',changed:old[i]!==next[i]}));
});
const changed=computed(()=>diff.value.filter(r=>r.changed).length);
const canEdit=computed(()=>page.value?.permissions?.can_edit===true);
const canReview=computed(()=>page.value?.permissions?.can_review===true);
const submitted=computed(()=>page.value?.workflow_state==='SUBMITTED');
const pendingChanges=computed(()=>{
  if(!page.value)return false;
  try{return text.value!==page.value.corrected_text||JSON.stringify(JSON.parse(fields.value))!==JSON.stringify(page.value.structured_fields);}
  catch{return true;}
});
async function refresh(){
  try{const r=await governance('/ocr?'+new URLSearchParams({q:query.value,review_status:filter.value,limit:'200'}));documents.value=r.items;}
  catch(e){error.value=String(e);}
}
function release(){if(imageUrl.value)URL.revokeObjectURL(imageUrl.value);imageUrl.value='';}
async function loadPage(){
  const seq=++sequence;busy.value=true;error.value='';message.value='';page.value=null;release();selectedToken.value=-1;human.value=false;
  try{
    const p=await governance('/ocr/pages/'+encodeURIComponent(selectedPage.value));
    const r=await authorizedFetch(p.image_url);const objectUrl=URL.createObjectURL(await r.blob());
    if(seq!==sequence){URL.revokeObjectURL(objectUrl);return;}
    page.value=p;text.value=p.corrected_text;fields.value=JSON.stringify(p.structured_fields,null,2);reason.value='';imageUrl.value=objectUrl;
  }catch(e){if(seq===sequence)error.value=String(e);}
  finally{if(seq===sequence)busy.value=false;}
}
async function openDocument(d:any){
  if(busy.value)return;error.value='';
  try{doc.value=await governance('/ocr/documents/'+encodeURIComponent(d.document_id));selectedPage.value=doc.value.pages[0].page_id;await loadPage();}
  catch(e){error.value=String(e);}
}
async function save(status:string,signoff=false){
  if(!page.value||busy.value)return;busy.value=true;error.value='';message.value='';
  try{
    const structured=JSON.parse(fields.value);
    if(!structured||Array.isArray(structured)||typeof structured!=='object')throw new Error('结构化字段必须是 JSON 对象');
    const result=await governance('/ocr/pages/'+page.value.page_id+(signoff?'/signoff':'/reviews'),{
      expected_version:page.value.version,corrected_text:text.value,structured_fields:structured,
      review_status:status,reason:reason.value,human_confirmed:human.value,
      ...(signoff?{expected_content_sha256:page.value.content_sha256}:{})});
    page.value=result;message.value='复核记录已保存，版本 '+result.version+'；正式计价仍须独立验收';
    text.value=result.corrected_text;fields.value=JSON.stringify(result.structured_fields,null,2);human.value=false;await refresh();
  }catch(e){error.value=String(e);}
  finally{busy.value=false;}
}
async function submit(){
  if(!page.value||busy.value)return;error.value='';message.value='';
  if(pendingChanges.value){error.value='请先保存校对草稿，再提交该版本复核';return;}
  busy.value=true;
  try{
    const result=await governance('/ocr/pages/'+page.value.page_id+'/submit',{
      expected_version:page.value.version,expected_content_sha256:page.value.content_sha256,reason:reason.value});
    page.value=result;human.value=false;message.value='已提交准确版本及内容 SHA256，等待有复核权限的人员决定';await refresh();
  }catch(e){error.value=String(e);}
  finally{busy.value=false;}
}
async function decide(decision:'APPROVE'|'RETURN'){
  if(!page.value||busy.value)return;error.value='';message.value='';
  if(pendingChanges.value){error.value='复核决定只能针对已提交内容，请先保存并重新提交修改';return;}
  busy.value=true;
  try{
    const result=await governance('/ocr/pages/'+page.value.page_id+'/decisions',{
      expected_version:page.value.version,submitted_version:page.value.submitted_version,
      expected_content_sha256:page.value.submitted_content_sha256,decision,reason:reason.value,human_confirmed:human.value});
    page.value=result;human.value=false;message.value=decision==='APPROVE'?'已保存本次人工复核决定；正式签认另需资格核验':'已退回该提交版本，请按原因修改后重新提交';await refresh();
  }catch(e){error.value=String(e);}
  finally{busy.value=false;}
}
function points(token:any){return token.bbox.map((p:number[])=>p.join(',')).join(' ');}
onMounted(refresh);onUnmounted(()=>{sequence++;release();});
</script>
<template>
  <section class="ocr-workbench">
    <div class="panel">
      <h2>OCR 人工复核工作台</h2>
      <p class="hint">原文永久保留。保存校对记录不构成专业签认；未核验内容不得用于正式费率。生产验收：False。</p>
      <div class="ocr-toolbar">
        <label>资料搜索<input v-model="query" placeholder="文件名或 SHA256" @keyup.enter="refresh" data-testid="ocr-search" /></label>
        <label>复核状态<select v-model="filter" data-testid="ocr-filter"><option value="">全部</option><option v-for="(label,status) in reviewLabels" :key="status" :value="status">{{label}}</option></select></label>
        <button class="btn" @click="refresh" :disabled="busy" data-testid="ocr-refresh">查询待复核资料</button>
        <span data-testid="ocr-total">有权访问 {{documents.length}} 份</span>
      </div>
      <div class="ocr-file-list" data-testid="ocr-documents">
        <button v-for="d in documents" :key="d.document_id" class="ocr-file" :class="{active:doc?.document_id===d.document_id}" @click="openDocument(d)" :disabled="busy" data-testid="ocr-document">
          <b>{{d.source_path.split('/').pop()}}</b><span>{{reviewLabels[d.review_status]}} · {{d.page_count}} 页 · 优先级 {{d.priority}}</span>
        </button>
        <p v-if="!documents.length" class="hint">当前查询没有可访问资料。资料须由本机导入并分配项目权限。</p>
      </div>
    </div>
    <p v-if="error" role="alert" class="ocr-error" data-testid="ocr-error">{{error}}</p>
    <p v-if="message" role="status" class="ocr-message" data-testid="ocr-saved">{{message}}</p>
    <div v-if="doc" class="panel ocr-provenance">
      <b>{{doc.source_path}}</b><span>原件 SHA256：<code>{{doc.source_file_sha256}}</code></span>
      <span>{{doc.OCR_engine}} · 项目 {{doc.project_id}}</span>
    </div>
    <div v-if="doc" class="ocr-columns">
      <section class="panel ocr-original">
        <div class="ocr-toolbar">
          <h3>原始页面</h3>
          <label>页码<select v-model="selectedPage" @change="loadPage" :disabled="busy" data-testid="ocr-page-number"><option v-for="p in doc.pages" :key="p.page_id" :value="p.page_id">第 {{p.page_number}} 页</option></select></label>
          <button class="btn" @click="zoom=Math.max(.5,zoom-.25)" aria-label="缩小原页">−</button>
          <span>{{Math.round(zoom*100)}}%</span><button class="btn" @click="zoom=Math.min(3,zoom+.25)" aria-label="放大原页">＋</button>
        </div>
        <div class="ocr-image-scroll" data-testid="ocr-image-panel">
          <p v-if="busy&&!page">载入原始页图及 OCR 记录…</p>
          <div v-if="page&&imageUrl" class="ocr-image-layer" :style="{width:zoom*100+'%'}">
            <img :src="imageUrl" alt="原始资料页面图像" data-testid="ocr-original-image" />
            <svg :viewBox="`0 0 ${page.image_width} ${page.image_height}`" aria-label="OCR 文字定位框">
              <polygon v-for="(token,i) in page.tokens" :key="i" :points="points(token)" :class="{selected:selectedToken===i}" @click="selectedToken=Number(i)"><title>{{token.text}} · {{(token.confidence*100).toFixed(1)}}%</title></polygon>
            </svg>
          </div>
        </div>
        <p v-if="page" class="hint hash-line">页图 SHA256：{{page.image_sha256}}</p>
      </section>
      <section class="panel ocr-editor" v-if="page">
        <div class="ocr-toolbar"><h3>第 {{page.page_number}} 页复核</h3><span class="badge">{{reviewLabels[page.review_status]}}</span><span data-testid="ocr-version">版本 {{page.version}}</span></div>
        <p>OCR 平均置信度：{{page.confidence===null?'无文字':(page.confidence*100).toFixed(1)+'%'}} <span class="hint">该值仅来自识别引擎</span></p>
        <label>OCR 原始识别文本<textarea :value="page.original_text" readonly rows="6" data-testid="ocr-original-text"></textarea></label>
        <details class="ocr-token-list"><summary>文字区域定位与逐项置信度（{{page.tokens.length}} 项）</summary><button v-for="(t,i) in page.tokens" :key="i" @click="selectedToken=Number(i)" :class="{selected:selectedToken===i}">{{i+1}} · {{(t.confidence*100).toFixed(1)}}% · {{t.text}}</button></details>
        <label>校对草稿<textarea v-model="text" rows="9" data-testid="ocr-corrected-text" :disabled="busy||!canEdit"></textarea></label>
        <label>结构化字段（JSON，人工核对后填写）<textarea v-model="fields" rows="4" data-testid="ocr-fields" :disabled="busy||!canEdit"></textarea></label>
        <details class="ocr-diff" open><summary>修改前后对比 · {{changed}} 行变化</summary><div class="table-wrap"><table data-testid="ocr-diff"><thead><tr><th>行</th><th>原始识别</th><th>校对文本</th></tr></thead><tbody><tr v-for="r in diff" :key="r.line" :class="{changed:r.changed}"><td>{{r.line}}</td><td>{{r.before}}</td><td>{{r.after}}</td></tr></tbody></table></div></details>
        <label>修改 / 退回说明<input v-model="reason" data-testid="ocr-reason" :disabled="busy" /></label>
        <label class="ocr-attestation"><input type="checkbox" v-model="human" data-testid="ocr-human-confirm" :disabled="busy" />我已逐项核对本页原件和校对文本</label>
        <div class="ocr-status-actions">
          <button class="btn primary" @click="save('IN_REVIEW')" :disabled="busy||!canEdit" data-testid="ocr-save">保存校对草稿</button>
          <button class="btn" @click="submit" :disabled="busy||!canEdit||submitted" data-testid="ocr-submit">提交当前版本复核</button>
          <button class="btn" @click="decide('APPROVE')" :disabled="busy||!canReview||!submitted" data-testid="ocr-reviewed">确认复核通过</button>
          <button class="btn" @click="decide('RETURN')" :disabled="busy||!canReview||!submitted" data-testid="ocr-return">退回复核并填写原因</button>
          <button class="btn" @click="save('PROFESSIONALLY_SIGNED',true)" :disabled="busy||!canReview" data-testid="ocr-signoff">正式专业签认</button>
        </div>
        <p class="hint" data-testid="ocr-workflow-state">{{submitted?'已提交版本 '+page.submitted_version+'，等待复核决定':'修改须保存后提交；编辑、复核与正式签认分别授权'}} · 内容版本 {{page.content_version}}</p>
        <p class="hint">正式签认另需服务端登记的真实专业身份和资格。本机样本操作员没有该资格。</p>
        <details open class="ocr-history"><summary>历史版本（{{page.change_history.length}}）· 校验链{{page.history_chain_valid?'有效':'异常'}}</summary><div data-testid="ocr-history"><article v-for="event in page.change_history" :key="event.event_id"><b>版本 {{event.version}} · {{reviewLabels[event.review_status]}}</b><p>{{event.reviewer_id}} · {{event.reviewed_at}} · {{event.reason}}</p><pre>{{event.corrected_text}}</pre><code>{{event.event_sha256}}</code></article><p v-if="!page.change_history.length">尚无人工复核记录。</p></div></details>
      </section>
    </div>
  </section>
</template>
<style scoped>
.ocr-toolbar{display:flex;flex-wrap:wrap;gap:12px;align-items:center}.ocr-toolbar h3{margin-right:auto}.ocr-toolbar label{min-width:150px}.ocr-workbench label{display:flex;flex-direction:column;gap:6px;margin:12px 0}.ocr-workbench textarea{width:100%;font:14px/1.65 inherit;resize:vertical;padding:10px;box-sizing:border-box}.ocr-file-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:8px;max-height:215px;overflow:auto;margin-top:14px}.ocr-file{background:#f7f9fc;border:1px solid #dbe4ef;border-radius:8px;padding:10px;text-align:left;color:#26364d;cursor:pointer;overflow-wrap:anywhere}.ocr-file b,.ocr-file span{display:block}.ocr-file span{font-size:12px;margin-top:6px;color:#61708a}.ocr-file.active{border-color:#2563eb;background:#eff6ff}.ocr-columns{display:grid;grid-template-columns:minmax(320px,1fr) minmax(360px,1fr);gap:16px;align-items:start}.ocr-provenance{display:flex;flex-direction:column;gap:8px;overflow-wrap:anywhere}.ocr-image-scroll{height:850px;overflow:auto;background:#e8edf3;border:1px solid #dbe2ed}.ocr-image-layer{position:relative;min-width:50%}.ocr-image-layer img{width:100%;height:auto;display:block}.ocr-image-layer svg{position:absolute;inset:0;width:100%;height:100%}.ocr-image-layer polygon{fill:transparent;stroke:rgba(37,99,235,.3);stroke-width:1;cursor:pointer}.ocr-image-layer polygon.selected{fill:rgba(255,190,0,.22);stroke:#f59e0b;stroke-width:4}.hash-line{overflow-wrap:anywhere}.ocr-token-list{max-height:230px;overflow:auto}.ocr-token-list button{display:block;width:100%;background:#fff;border:1px solid #e2e8f0;text-align:left;padding:5px}.ocr-token-list .selected{background:#fef3c7}.ocr-diff .table-wrap{max-height:260px}.ocr-diff td{white-space:pre-wrap;overflow-wrap:anywhere;max-width:320px}.ocr-diff .changed{background:#fffbeb}.ocr-workbench .ocr-attestation{flex-direction:row;align-items:center}.ocr-attestation input{width:auto}.ocr-status-actions{display:flex;flex-wrap:wrap;gap:8px}.ocr-error{padding:12px;background:#fff1f2;color:#9f1239;overflow-wrap:anywhere}.ocr-message{padding:12px;background:#ecfdf5;color:#065f46}.ocr-history article{border-top:1px solid #e2e8f0;padding:12px 0}.ocr-history pre{white-space:pre-wrap;max-height:180px;overflow:auto}.ocr-history code{font-size:11px;overflow-wrap:anywhere}@media(max-width:1100px){.ocr-columns{grid-template-columns:1fr}.ocr-image-scroll{height:620px}}
</style>
