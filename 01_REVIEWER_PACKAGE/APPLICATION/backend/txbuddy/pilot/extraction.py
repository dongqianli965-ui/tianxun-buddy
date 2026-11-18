import hashlib
from .database import dumps
from .importer import ident


def save_fields(db,relative_path,parsed,import_job_id):
    rows=[];did=ident(relative_path)
    if parsed.format=='xlsx':
        for sheet in parsed.metadata['sheets']:
            for row in sheet['rows']:
                for cell in row['cells']:
                    if cell['raw_value'] is None:continue
                    loc={'sheet':sheet['name'],'row':cell['row'],'column':cell['column'],'cell':cell['coordinate']}
                    key=hashlib.sha256((did+str(parsed.sha256)+sheet['name']+cell['coordinate']).encode()).hexdigest()
                    rows.append((key,did,'workbook_cell',dumps(loc),dumps(cell),'UNREVIEWED',import_job_id))
    else:
        for i,b in enumerate(parsed.blocks):
            key=hashlib.sha256((did+str(parsed.sha256)+'block'+str(i)+parsed.format).encode()).hexdigest()
            rows.append((key,did,b.kind,dumps(b.locator.as_dict()),b.text,
                'NEEDS_EXPERT_REVIEW' if parsed.format=='ocr' else 'UNREVIEWED',import_job_id))
    with db.transaction() as c:
        c.many('INSERT INTO extracted_field VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',rows)
    return len(rows)
