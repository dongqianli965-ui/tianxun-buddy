from .v24_fixtures import corpus
from txbuddy.pilot.boq import workbook_boq
def test_real_sample_is_reference_import_only(corpus):
    paths=list((corpus/'05_REAL_PROJECTS/L1_BUDGET/L1_001/B_REFERENCE').glob('*土建_工程量清单.xlsx'))
    assert len(paths)==1
    r=workbook_boq(paths[0]);assert r['count']>0
    assert r['accuracy_verification']=='BLOCKED' and r['independently_calculated'] is False
