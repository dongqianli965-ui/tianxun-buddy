from .v24_fixtures import corpus,price_db
from txbuddy.pilot.service import query_prices,import_fee
from txbuddy.pilot import PilotError
from decimal import Decimal
import pytest

def test_all_real_rows_and_corrected_q2_history(price_db):
    assert price_db.counts()['material_price']==10989
    with price_db.transaction() as c:
        assert c.one("SELECT COUNT(*) AS n FROM material_price WHERE period='2026-Q2'")['n']==5026
        assert c.one("SELECT COUNT(*) AS n FROM material_price WHERE period='2026-Q3'")['n']==5024
        assert c.one("SELECT COUNT(*) AS n FROM material_price WHERE period='2026-09'")['n']==939
    q=query_prices(price_db,region='410100-INNER-4RING',period='2026-Q2',q='沥青混凝土')
    for seq,value,excl in [('1782','492.34','435.70'),('1783','470.95','416.77'),('1784','439.83','389.23')]:
        row=next(x for x in q['items'] if x['record_id']=='ZZ-20262-'+seq)
        assert Decimal(row['price_incl_tax'])==Decimal(value)
        assert Decimal(row['price_excl_tax'])==Decimal(excl)
        assert len(row['version_history'])==2 and row['original_prices']
        assert row['version_history'][-1]['source_page']==137
    assert query_prices(price_db,region='410100-INNER-4RING',period='2026-Q3')['total']==5021

@pytest.mark.parametrize('region,period,usage,code',[
    ('410300','2026-Q3','SAMPLE_REFERENCE','REGION_MISMATCH'),
    ('410100-INNER-4RING','2026-Q4','SAMPLE_REFERENCE','PRICE_PERIOD_MISSING'),
    ('410100-INNER-4RING','2026-10','SAMPLE_REFERENCE','PRICE_PERIOD_MISSING'),
    ('410100-INNER-4RING','2025-Q3','CURRENT','PERIOD_NOT_CURRENT')])
def test_scope_period_guards(price_db,region,period,usage,code):
    with pytest.raises(PilotError,match=code): query_prices(price_db,region=region,period=period,usage=usage)

def test_fees_separate_unreviewed_blocked_and_noncompetitive_protected(price_db):
    rate={'rate_id':'TEST_ONLY-FEE','region':'TEST_ONLY','period':'TEST_ONLY','major':'TEST_ONLY','noncompetitive':True,'test_only':True}
    assert import_fee(price_db,rate)['formal_usable'] is False
    with pytest.raises(PilotError,match='NONCOMPETITIVE_IMMUTABLE'):import_fee(price_db,{**rate,'value':'.99'})
    with pytest.raises(PilotError,match='FEE_EXPERT_SIGNOFF_REQUIRED'):import_fee(price_db,{**rate,'formal_usable':True})

def test_real_price_adjustment_reuses_numeric_engine_with_sources(price_db):
    from txbuddy.pilot.service import adjust_price_sample
    r=adjust_price_sample(price_db,region='410100-INNER-4RING',base_period='2026-Q2',current_period='2026-Q3',material_name='预应力钢筋',spec_model='Φ10mm以内（综合）',unit='T',quantity='2')
    assert abs(Decimal(r['delta_amount'])-Decimal('2')*(Decimal('3611.60')-Decimal('3730.58')))<Decimal('.000001')
