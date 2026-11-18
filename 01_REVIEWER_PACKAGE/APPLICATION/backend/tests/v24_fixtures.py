from pathlib import Path
import os, pytest
from txbuddy.pilot import PilotDatabase
from txbuddy.pilot.importer import import_corpus

CORPUS=Path(os.environ.get('TXBUDDY_CORPUS_ROOT',str(Path(__file__).resolve().parents[3]/'REAL_DATA_CORPUS/HN_L1_V1_20261116')))

@pytest.fixture(scope='session')
def corpus():
    assert (CORPUS/'00_INDEX/SHA256SUMS.txt').is_file(),'真实数据仓必须通过 TXBUDDY_CORPUS_ROOT 配置'
    return CORPUS

@pytest.fixture(scope='session')
def price_db(tmp_path_factory,corpus):
    db=PilotDatabase(tmp_path_factory.mktemp('v24-prices')/'sample.sqlite3');db.migrate();import_corpus(db,corpus);return db
