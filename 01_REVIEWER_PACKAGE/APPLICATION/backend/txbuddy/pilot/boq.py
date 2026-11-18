"""Workbook BOQ import preserves cell provenance and reference role; it performs no quantity calculation."""
from pathlib import Path
from decimal import Decimal, InvalidOperation
import re
from ..parsing import parse_file
from .database import PilotError


def workbook_boq(path,*,data_role='B_REFERENCE'):
    parsed=parse_file(path)
    if parsed.status.value!='PARSED': raise PilotError('BOQ_PARSE_FAILED',parsed.error or parsed.blocked_reason)
    items=[]; rejected=[]; skipped=[]
    for sheet in parsed.metadata['sheets']:
        mapping=sheet['header_mapping']
        if not mapping:
            skipped.append({'sheet':sheet['name'],'reason':'HEADER_UNMAPPED'}); continue
        start=max(v['header_row'] for v in mapping.values())
        for row in sheet['rows']:
            if row['row']<=start or row['empty']: continue
            cells={c['column']:c for c in row['cells']}
            def get(field):
                col=mapping.get(field,{}).get('column'); c=cells.get(col,{})
                return c.get('cached_value') if c.get('formula') else c.get('raw_value')
            code=str(get('code') or '').strip()
            name=str(get('name') or '').strip()
            unit=str(get('unit') or '').strip()
            feature=str(get('feature') or '').strip()
            raw=get('quantity')
            # Repeated page headers, sections and summaries are structural rows.
            if code in ('项目编码','清单编码','编码'):
                skipped.append({'sheet':sheet['name'],'row':row['row'],'reason':'REPEATED_HEADER'}); continue
            # A summary row may carry a subtotal quantity, but without any
            # detail identity (code/unit/feature) it is structural, not a
            # malformed BOQ item.  Keep detail names containing "小计" when
            # they still have code/unit/feature evidence.
            if (row['summary'] and not any((code,unit,feature))) or not any((code,unit,feature,raw is not None)):
                skipped.append({'sheet':sheet['name'],'row':row['row'],'reason':'SUMMARY' if row['summary'] else 'STRUCTURAL'}); continue
            errors=[]
            if not re.fullmatch(r'\d{9,15}',code): errors.append('CODE_INVALID')
            if not name: errors.append('NAME_MISSING')
            if not unit: errors.append('UNIT_MISSING')
            try:
                quantity=Decimal(str(raw).replace(',',''))
                if not quantity.is_finite() or quantity<0: raise InvalidOperation()
            except (InvalidOperation,ValueError):
                quantity=None; errors.append('QUANTITY_INVALID')
            loc={k:cells[v['column']]['coordinate'] for k,v in mapping.items() if v['column'] in cells}
            if errors: rejected.append({'sheet':sheet['name'],'row':row['row'],'code':code,'reasons':errors})
            items.append({'code':code,'name':name,'feature':feature,
                'unit':unit,'quantity':str(quantity) if quantity is not None else None,'sheet':sheet['name'],'row':row['row'],
                'cells':loc,'file_sha256':parsed.sha256,'data_role':data_role,
                'raw_cells':row['cells'],'validation_errors':errors,
                'calculation_origin':'PUBLISHED_BOQ_IMPORT','independently_calculated':False})
    return {'items':items,'errors':rejected,'skipped_rows':skipped,'count':len(items),'sha256':parsed.sha256,
        'status':('IMPORTED_WITH_ERRORS' if rejected else 'IMPORTED') if items else 'BLOCKED','blocked_reason':None if items else 'BOQ_DETAIL_MISSING: 仅封面或无清单明细',
        'data_role':data_role,'independently_calculated':False,'accuracy_verification':'BLOCKED'}
