from datetime import date
from decimal import Decimal
import json, re
from .database import PilotError, dumps
from ..core.quota import match_quota, apply_quota_notes


def query_prices(db,*,region,period,q='',spec='',unit='',offset=0,limit=50,usage='SAMPLE_REFERENCE',_exact=False):
    if region!='410100-INNER-4RING': raise PilotError('REGION_MISMATCH','仅有郑州市区四环以内依据，其他地区缺少依据')
    if not period: raise PilotError('PERIOD_REQUIRED','必须明确指定价格期次')
    if not re.fullmatch(r'20\d\d-(Q[1-4]|0[1-9]|1[0-2])',period): raise PilotError('PERIOD_INVALID','期次使用 YYYY-Qn 或 YYYY-MM')
    today=date.today(); current_q=f'{today.year}-Q{(today.month-1)//3+1}'; current_m=today.strftime('%Y-%m')
    if usage not in ('SAMPLE_REFERENCE','CURRENT'): raise PilotError('USAGE_INVALID','未知价格用途')
    if usage=='CURRENT' and period not in (current_q,current_m): raise PilotError('PERIOD_NOT_CURRENT','历史或过往期次不得作为当前价格')
    with db.transaction() as c:
        if not c.one('SELECT 1 AS n FROM material_price WHERE period=?',(period,)):
            raise PilotError('PRICE_PERIOD_MISSING',f'{period} 缺少数据，不自动回退或补造')
        where="region=? AND period=? AND scope_status<>'ERRATA_FOR_Q2'"; args=[region,period]
        for field,value in [('material_name',q),('spec_model',spec)]:
            if _exact: where+=' AND '+field+'=?'; args.append(value)
            elif value: where+=' AND '+field+' LIKE ?'; args.append('%'+value+'%')
        if unit: where+=' AND unit=?'; args.append(unit)
        total=c.one('SELECT COUNT(*) AS n FROM material_price WHERE '+where,tuple(args))['n']
        rows=c.rows('SELECT * FROM material_price WHERE '+where+' ORDER BY record_id LIMIT ? OFFSET ?',tuple([*args,min(max(int(limit),1),200),max(int(offset),0)]))
        for row in rows:
            original={'price_incl_tax':str(row['price_incl_tax']),'price_excl_tax':str(row['price_excl_tax'])}
            corrections=c.rows('SELECT * FROM material_price_correction WHERE record_id=? ORDER BY correction_id',(row['record_id'],))
            row['original_prices']=original; row['version_history']=[{'version':'ORIGINAL',**original}]
            for corr in corrections:
                corr['price_incl_tax']=str(corr['price_incl_tax']);corr['price_excl_tax']=str(corr['price_excl_tax'])
                row['version_history'].append(corr)
            latest=corrections[-1] if corrections else row
            row['price_incl_tax']=str(latest['price_incl_tax']);row['price_excl_tax']=str(latest['price_excl_tax'])
            row['corrected']=bool(corrections); row['raw_fields']=json.loads(row.pop('raw_payload'))
            row['source_url']='/v1/sources/'+row['document_id']
            row['tax_rate']=None;row['tax_rate_inferred']=False
    return {'items':rows,'total':total,'period':period,'region':region,'usage':usage,
        'formal_usable':False,'warning':'资料样本查询；Q2、9月通知正文待核实；材料税率未按价比推定'}


def resolve_price(db,*,region,period,material_name,spec_model,unit,usage='SAMPLE_REFERENCE'):
    """Return only a unique exact match with a registered, traceable source."""
    if not material_name or spec_model is None or not unit:
        raise PilotError('MATERIAL_CONDITIONS_REQUIRED','必须提供材料名称、规格和单位；无规格时明确传入空字符串')
    found=query_prices(db,region=region,period=period,q=material_name,spec=spec_model,unit=unit,usage=usage,_exact=True)
    if not found['total']: raise PilotError('MATERIAL_PRICE_MISSING','没有完全匹配的材料价格')
    if found['total']!=1: raise PilotError('MATERIAL_AMBIGUOUS','完全匹配多条价格，须人工核查来源')
    row=found['items'][0]
    with db.transaction() as c:
        source=c.one('SELECT d.sha256,s.official_url FROM document_inventory d JOIN source_register s ON s.source_id=d.source_id WHERE d.document_id=?',(row['document_id'],))
        correction_sources=[(entry,c.one('SELECT d.sha256,s.official_url FROM document_inventory d JOIN source_register s ON s.source_id=d.source_id WHERE d.document_id=?',(entry['source_document_id'],))) for entry in row['version_history'][1:]]
    if not source or not source['official_url'] or source['sha256']!=row['sha256']:
        raise PilotError('PRICE_SOURCE_UNVERIFIED','材料价格来源或文件哈希缺失、不一致')
    if any(not s or not s['official_url'] or s['sha256']!=entry['sha256'] for entry,s in correction_sources):
        raise PilotError('PRICE_SOURCE_UNVERIFIED','勘误来源或文件哈希缺失、不一致')
    from ..core.numeric import to_decimal
    try:
        for field in ('price_incl_tax','price_excl_tax'):
            to_decimal(row[field],field,allow_negative=False)
    except (ValueError,TypeError):
        raise PilotError('PRICE_INVALID','材料价格缺失或非法') from None
    return row


def source_detail(db,document_id):
    with db.transaction() as c:
        d=c.one('SELECT * FROM document_inventory WHERE document_id=?',(document_id,))
        if not d: raise PilotError('SOURCE_NOT_FOUND','资料未登记')
        s=c.one('SELECT * FROM source_register WHERE source_id=?',(d['source_id'],)) if d['source_id'] else None
        d['payload']=json.loads(d['payload'])
        if s:s['payload']=json.loads(s['payload'])
        prices=c.rows('SELECT record_id,period,source_page,source_row FROM material_price WHERE document_id=? ORDER BY record_id',(document_id,))
        corrections=c.rows('SELECT correction_id,record_id,source_page FROM material_price_correction WHERE source_document_id=?',(document_id,))
        return {'document':d,'source':s,'price_records':prices,'corrections':corrections,'original_read_only':True}


def import_test_quota(db,payload):
    if payload.get('license_status')!='TEST_ONLY' or payload.get('test_only') is not True:
        raise PilotError('QUOTA_AUTHORIZATION_REQUIRED','本轮只接收明确 TEST_ONLY 样例，正式定额正文尚未授权')
    chapters=payload.get('chapters') or []
    with db.transaction() as c:
        for ch in chapters:
            c.execute('INSERT INTO quota_chapter VALUES (?,?,?,?) ON CONFLICT(chapter_id) DO NOTHING',(ch['id'],payload['version'],'TEST_ONLY',dumps(ch)))
            for item in ch['items']:
                if item.get('license_status')!='TEST_ONLY' or not item.get('source_ref'):
                    raise PilotError('QUOTA_SOURCE_REQUIRED','子目须有 TEST_ONLY 来源')
                c.execute('INSERT INTO quota_item VALUES (?,?,?,?,?) ON CONFLICT(item_id) DO NOTHING',(item['id'],ch['id'],'TEST_ONLY',1,dumps(item)))
                for table,key in [('quota_resource','resources'),('quota_note','notes'),('quota_conversion','conversions')]:
                    for row in item.get(key,[]):
                        c.execute('INSERT INTO '+table+' VALUES (?,?,?) ON CONFLICT DO NOTHING',(row['id'],item['id'],dumps(row)))
    return {'status':'IMPORTED','test_only':True,'formal_usable':False}


def quota_candidates(db,boq):
    with db.transaction() as c: rows=c.rows('SELECT * FROM quota_item')
    result=match_quota(boq,[json.loads(r['payload']) for r in rows])
    return {**result,'test_only':True,'formal_usable':False}


def import_fee(db,rate):
    if rate.get('formal_usable'):
        raise PilotError('FEE_EXPERT_SIGNOFF_REQUIRED','样本通道不接受正式费率；须官方逐条依据及人工签认')
    with db.transaction() as c:
        old=c.one('SELECT * FROM fee_rate_matrix WHERE rate_id=?',(rate['rate_id'],))
        if old and old['noncompetitive'] and old['payload']!=dumps(rate):
            raise PilotError('NONCOMPETITIVE_IMMUTABLE','不可竞争费用不能被覆盖')
        c.execute('INSERT INTO fee_rate_matrix VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(rate_id) DO NOTHING',(
            rate['rate_id'],rate['region'],rate['period'],rate['major'],rate.get('official_source'),
            rate.get('review_status','NEEDS_EXPERT_REVIEW'),0,int(rate.get('noncompetitive',False)),dumps(rate)))
    return {'status':'IMPORTED','formal_usable':False}


def apply_library_item(db,item_id,*,height=None):
    from ..core.numeric import to_decimal,ensure_finite
    with db.transaction() as c:
        row=c.one('SELECT * FROM quota_item WHERE item_id=?',(item_id,))
        if not row:raise PilotError('QUOTA_ITEM_MISSING','定额子目不存在')
        item=json.loads(row['payload'])
        conversions=[json.loads(r['payload']) for r in c.rows('SELECT payload FROM quota_conversion WHERE item_id=?',(item_id,))]
    result=apply_quota_notes(item.get('resources',[]),item.get('notes',[]))
    for conv in sorted(conversions,key=lambda x:(int(x.get('priority',100)),x['id'])):
        if not conv.get('source_ref'):raise PilotError('CONVERSION_SOURCE_REQUIRED','换算缺少来源')
        if conv.get('type')!='HEIGHT':raise PilotError('CONVERSION_TYPE_UNSUPPORTED','只支持显式 HEIGHT 换算')
        if height is None:raise PilotError('CONVERSION_INPUT_REQUIRED','HEIGHT 换算须提供实际高度')
        actual=to_decimal(height,'height',allow_negative=False);threshold=to_decimal(conv['threshold'],'threshold',allow_negative=False)
        if actual>threshold:
            factor=to_decimal(conv['factor'],'factor',allow_negative=False,allow_zero=False)
            for r in result['consumption']:r['consumption']=str(ensure_finite(to_decimal(r['consumption'],'consumption')*factor,'consumption'))
            result['execution_trace'].append({'effect':'HEIGHT','height':str(actual),'threshold':str(threshold),'factor':str(factor),'source_ref':conv['source_ref']})
    return {**result,'test_only':bool(row['test_only']),'formal_usable':False,'license_status':row['license_status']}


def adjust_price_sample(db,*,region,base_period,current_period,material_name,spec_model,unit,quantity):
    from ..core.numeric import to_decimal
    from ..core.pricing import index_adjustment
    base=query_prices(db,region=region,period=base_period,q=material_name,spec=spec_model,unit=unit,_exact=True)
    current=query_prices(db,region=region,period=current_period,q=material_name,spec=spec_model,unit=unit,_exact=True)
    if base['total']!=1 or current['total']!=1: raise PilotError('MATERIAL_AMBIGUOUS','两期材料必须各有唯一精确匹配')
    b,cur=base['items'][0],current['items'][0];qty=to_decimal(quantity,'quantity',allow_negative=False)
    old=to_decimal(b['price_excl_tax'],'base_price');new=to_decimal(cur['price_excl_tax'],'current_price')
    adjustment=index_adjustment(qty*old,old,new,'0','PASS_THROUGH')
    return {**{k:str(v) for k,v in adjustment.items()},'test_only':True,'formal_usable':False,
        'basis':'SAMPLE_REFERENCE_WITHOUT_CONTRACT_ADMISSION','base_source':b,'current_source':cur}
