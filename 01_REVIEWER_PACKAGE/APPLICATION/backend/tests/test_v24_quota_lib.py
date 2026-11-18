from txbuddy.pilot import PilotDatabase,PilotError
from txbuddy.pilot.service import import_test_quota,quota_candidates
import pytest

def test_five_layers_test_only_source_and_authorization(tmp_path):
    db=PilotDatabase(tmp_path/'quota-sample.sqlite3');db.migrate()
    item={'id':'TEST_ONLY-ITEM','item_code':'TEST_ONLY-001','name':'TEST_ONLY 合成样例','unit':'m3','major':'BUILDING','source_ref':'TEST_ONLY fixture','license_status':'TEST_ONLY',
        'resources':[{'id':'TEST_ONLY-R','consumption':'1'}],'notes':[{'id':'TEST_ONLY-N','effect_type':'INFO','source_ref':'TEST_ONLY'}],
        'conversions':[{'id':'TEST_ONLY-C','type':'HEIGHT','source_ref':'TEST_ONLY'}]}
    p={'test_only':True,'license_status':'TEST_ONLY','version':'TEST_ONLY-v1','chapters':[{'id':'TEST_ONLY-CH','items':[item]}]}
    assert import_test_quota(db,p)['test_only'] is True
    for table in ('quota_chapter','quota_item','quota_resource','quota_note','quota_conversion'):
        with db.transaction() as c:assert c.one('SELECT COUNT(*) AS n FROM '+table)['n']==1
    assert quota_candidates(db,{'name':item['name'],'major':'BUILDING','unit':'m3'})['formal_usable'] is False
    with pytest.raises(PilotError,match='QUOTA_AUTHORIZATION_REQUIRED'):import_test_quota(db,{**p,'test_only':False})

def test_height_conversion_and_four_note_effects(tmp_path):
    from txbuddy.pilot.service import apply_library_item
    from txbuddy.core.quota import apply_quota_notes
    from decimal import Decimal
    db=PilotDatabase(tmp_path/'height-sample.sqlite3');db.migrate()
    item={'id':'TEST_ONLY-H','item_code':'TEST_ONLY-H','name':'TEST_ONLY高度','unit':'m3','source_ref':'TEST_ONLY','license_status':'TEST_ONLY',
        'resources':[{'id':'TEST_ONLY-HR','resource_code':'A','consumption':'2'}],
        'notes':[{'id':'N','effect_type':'FACTOR','scope_level':'CHAPTER','factor':'1.5','source_ref':'TEST_ONLY'}],
        'conversions':[{'id':'C','type':'HEIGHT','threshold':'3','factor':'1.1','source_ref':'TEST_ONLY'}]}
    import_test_quota(db,{'test_only':True,'license_status':'TEST_ONLY','version':'TEST_ONLY','chapters':[{'id':'TEST_ONLY','items':[item]}]})
    assert Decimal(apply_library_item(db,item['id'],height='4')['consumption'][0]['consumption'])==Decimal('3.30')
    with pytest.raises(PilotError,match='CONVERSION_INPUT_REQUIRED'):apply_library_item(db,item['id'])
    with pytest.raises(ValueError,match='禁止套用'):apply_quota_notes([], [{'id':'X','effect_type':'FORBID','source_ref':'TEST_ONLY'}])
    r=apply_quota_notes([{'resource_code':'A','consumption':'1'}],[{'id':'R','effect_type':'REPLACE','source_ref':'TEST_ONLY','target_resource_code':'A','replacement_resource_code':'B','replacement_source_ref':'TEST_ONLY'},{'id':'I','effect_type':'INFO','source_ref':'TEST_ONLY','note_text':'TEST_ONLY'}])
    assert r['consumption'][0]['resource_code']=='B' and len(r['execution_trace'])==2
