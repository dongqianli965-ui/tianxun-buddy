import hashlib
from PIL import Image,ImageDraw
from txbuddy.parsing.ocr import offline_ocr
def test_real_offline_engine_has_page_image_confidence_review_state(tmp_path):
    p=tmp_path/'source.png';im=Image.new('RGB',(600,160),'white');ImageDraw.Draw(im).text((25,40),'TEST 12345',fill='black',font_size=45);im.save(p)
    sha=hashlib.sha256(p.read_bytes()).hexdigest();d=offline_ocr(p,tmp_path/'derived')
    assert d.status.value=='PARSED',d.blocked_reason
    assert d.metadata['tokens'] and all(t['page']==1 and t['bbox'] and 0<=t['confidence']<=1 for t in d.metadata['tokens'])
    assert d.metadata['review_status']=='NEEDS_EXPERT_REVIEW' and d.metadata['formal_usable'] is False
    assert hashlib.sha256(p.read_bytes()).hexdigest()==sha
