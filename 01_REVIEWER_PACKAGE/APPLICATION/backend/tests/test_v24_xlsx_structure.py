from openpyxl import Workbook
from txbuddy.parsing import parse_file
from txbuddy.pilot.boq import workbook_boq
from .v24_fixtures import corpus

def test_formula_merged_hidden_blank_summary_cells(tmp_path):
    w=Workbook();s=w.active;s.title='清单';s.append(['项目编码','项目名称','单位','工程量']);s.append(['010101001001','柱','m3',2]);s.append(['','','','=D2*2']);s.append([]);s.append(['合计']);s.merge_cells('A5:C5');w.create_sheet('隐藏').sheet_state='hidden';p=tmp_path/'book.xlsx';w.save(p)
    d=parse_file(p);s=d.metadata['sheets'][0]
    assert s['rows'][2]['cells'][3]['formula']=='=D2*2'
    assert s['rows'][2]['cells'][3]['coordinate']=='D3'
    assert s['rows'][3]['empty'] and s['rows'][4]['summary']
    assert s['merged_cells']==['A5:C5'] and d.metadata['sheets'][1]['state']=='hidden'
    assert s['rows'][1]['cells'][3]['unit']=='m3'

def test_real_l1_detail_and_cover_are_distinguished(corpus):
    folder=corpus/'05_REAL_PROJECTS/L1_BUDGET/L1_001/B_REFERENCE'
    cover=next(folder.glob('_备用*.xlsx'));detail=next(folder.glob('*土建_工程量清单.xlsx'))
    assert workbook_boq(cover)['status']=='BLOCKED'
    r=workbook_boq(detail)
    assert r['count']>0 and r['items'][0]['cells']['quantity']
    assert r['items'][0]['data_role']=='B_REFERENCE'
    assert r['items'][0]['independently_calculated'] is False
