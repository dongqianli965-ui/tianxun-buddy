from pathlib import Path
import json,re,os
from txbuddy.api import app

ROOT=Path(__file__).resolve().parents[1]


def routes(application,prefix=''):
    output=[]
    for route in application.routes:
        if hasattr(route,'original_router'):
            output.extend(routes(route.original_router,prefix+route.include_context.prefix));continue
        path=(prefix+getattr(route,'path','')).replace('//','/')
        if hasattr(route,'methods'):
            for method in sorted(route.methods):output.append({'method':method,'path':path,'service':route.endpoint.__module__+'.'+route.endpoint.__name__})
        if hasattr(route,'app') and hasattr(route.app,'routes'):output.extend(routes(route.app,path.rstrip('/')))
    return output


PAGES={
    'health':'App.vue','precision':'QuantityPage.vue','rules':'QuantityPage.vue','rulesValidate':'QuantityPage.vue','basisValidate':'ProjectPage.vue',
    'jobCreate':'ProjectPage.vue','jobGet':'ProjectPage.vue','jobList':'ProjectPage.vue / SamplePilotPage.vue','docAdd':'DocumentsPage.vue',
    'filePut':'SamplePilotPage.vue','parseFile':'SamplePilotPage.vue','boqImport':'BoqPage.vue',
    'recordsGet':'ReviewPage.vue','recordsPut':'ReviewPage.vue','taskCreate':'ReviewPage.vue / SamplePilotPage.vue','taskGet':'ReviewPage.vue','taskAction':'ReviewPage.vue / SamplePilotPage.vue',
    'quantityRule':'QuantityPage.vue','quantityCalc':'QuantityPage.vue','quotaMatch':'QuotaPage.vue','quotaNotes':'QuotaPage.vue',
    'priceUnit':'PricingPage.vue','priceIndex':'PricingPage.vue','priceTotal':'PricingPage.vue','recalc':'ReviewPage.vue','calcGet':'ReviewPage.vue',
    'gateCheck':'ReviewPage.vue','gateSequence':'ReviewPage.vue','jgateStatus':'DeliverablePage.vue','jgateRun':'ReviewPage.vue',
    'deliverableList':'DeliverablePage.vue','deliverableGenerate':'SamplePilotPage.vue / DeliverablePage.vue',
    'evidenceGet':'EvidencePage.vue','evidenceVerify':'EvidencePage.vue','audit':'EvidencePage.vue','authLogin':'LoginPanel.vue','aclProjects':'SamplePilotPage.vue',
}
TABLES={
    'jobCreate':['runtime.job','auth.acl_project','auth.acl_member'],'jobGet':['runtime.job','runtime.job_document','runtime.job_task','runtime.gate_record','runtime.calc_run'],
    'jobList':['runtime.job','auth.acl_member'],'docAdd':['runtime.job_document'],'recordsGet':['runtime.job_record'],'recordsPut':['runtime.job_record','runtime.audit_log'],
    'filePut':['filestore.file_blob','filestore.file_version'],'parseFile':['filestore.file_version','runtime.job_record'],
    'taskCreate':['runtime.job_task'],'taskGet':['runtime.job_task'],'taskAction':['runtime.job_task','runtime.audit_log'],
    'quantityRule':['runtime.calc_run','runtime.evidence','runtime.idem'],'recalc':['runtime.calc_run','runtime.evidence','runtime.idem'],'calcGet':['runtime.calc_run'],
    'gateCheck':['runtime.job_record','runtime.job_document','runtime.gate_record','runtime.evidence'],'jgateStatus':['runtime.gate_record','runtime.job_record'],'jgateRun':['runtime.gate_record','runtime.job_record'],
    'deliverableList':['runtime.job_record'],'deliverableGenerate':['runtime.job_record','filestore.file_blob','filestore.file_version'],
    'evidenceGet':['runtime.evidence'],'audit':['runtime.audit_log'],'authLogin':['private UserDirectory (PBKDF2)','auth.auth_revoked'],'aclProjects':['auth.acl_member'],
}


def audit():
    source=(ROOT/'frontend/src/endpoints.ts').read_text(encoding='utf-8-sig')
    pattern=r'^\s*(\w+): E\("[^"]+", "[^"]+", "(GET|POST|PUT|DELETE)", "([^"]+)"'
    all_routes=routes(app);rows=[]
    normalize=lambda p:re.sub(r'\{[^}]+\}','{}',p.split('?')[0])
    for key,method,path in re.findall(pattern,source,re.M):
        paths=[path.replace('{action}',a) for a in ('start','complete','approve','fail','retry')] if key=='taskAction' else [path]
        matches=[r for r in all_routes if r['method']==method and any(normalize(r['path'])==normalize(p) for p in paths)]
        row={'key':key,'frontend':PAGES.get(key),'method':method,'path':path,'backend_routes':matches,
            'database':TABLES.get(key,['无：确定性内存函数或规则配置读取']),
            'response':'真实 HTTP 返回；业务拒绝保留状态码和原因','route_resolved':bool(matches),
            'validation':'全量 API 回归；交互覆盖另见 E2E_HTTP_EVIDENCE.json'}
        if key=='deliverableGenerate':row.update(response='sample_report 可生成下载；正式预算书/工程量计算书 422 BLOCKED',acceptance='PARTIAL_FORMAL_BLOCKED')
        elif key=='audit':row.update(response='admin 角色可读；普通用户 403',acceptance='ROLE_RESTRICTED')
        elif key in ('quantityCalc','quantityRule','quotaMatch','quotaNotes','priceUnit','priceIndex','priceTotal'):
            row.update(acceptance='TEST_ONLY_OR_FORMAL_ADMISSION_GUARDED')
        else:row['acceptance']='HTTP_WIRED'
        rows.append(row)
    assert len(rows)==37
    assert all(r['route_resolved'] and r['frontend'] for r in rows)
    out={'endpoint_count':len(rows),'rows':rows,'all_post_routes':all_routes,
        'note':'路由存在、权限测试、样本流程与正式业务验收分别记录，不以 connected 标记替代验收。'}
    target=ROOT/'reports/v424/ENDPOINT_MAPPING.json';target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
    md=['# 37 个端点逐项映射','', '| 前端页面 | 方法与API | 后端服务 | 数据库/配置 | 验收口径 |','|---|---|---|---|---|']
    for r in rows:md.append('|'+ '|'.join([r['frontend'],r['method']+' '+r['path'],', '.join(x['service'] for x in r['backend_routes']),', '.join(r['database']),r['acceptance']])+'|')
    (target.with_suffix('.md')).write_text('\n'.join(md)+'\n',encoding='utf-8');return out

if __name__=='__main__':print('ENDPOINTS',audit()['endpoint_count'])
