from .v24_fixtures import corpus
from txbuddy.parsing import parse_file

def test_all_117_real_html_are_readable(corpus):
    files=[f for f in corpus.rglob('*') if f.suffix.lower() in ('.html','.htm')]
    results=[parse_file(f) for f in files]
    assert len(files)==117
    assert sum(x.status.value=='PARSED' for x in results)/117>=.95
    assert all(x.sha256 and (x.blocks or x.error) for x in results)

def test_html_chinese_tables_lists_and_script_filter(tmp_path):
    p=tmp_path/'chinese.htm';p.write_bytes('<html><head><meta charset="gbk"><title>价格通知</title><link rel="canonical" href="https://example.org/source"></head><body><h1>中文标题</h1><script>BAD_SCRIPT</script><style>BAD_STYLE</style><ul><li>材料清单</li></ul><table><tr><th>名称</th><th>价格</th></tr><tr><td>钢筋</td><td>4000</td></tr></table></body></html>'.encode('gbk'))
    d=parse_file(p)
    assert '中文标题' in d.text and 'BAD_SCRIPT' not in d.text and 'BAD_STYLE' not in d.text
    assert d.metadata['tables'][0]['rows'][1]==['钢筋','4000']
    assert d.metadata['source_url']=='https://example.org/source'
    assert any(b.kind=='list_item' for b in d.blocks)
