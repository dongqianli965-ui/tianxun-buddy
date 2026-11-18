from .v24_fixtures import corpus
def test_current_goldenset_has_no_independent_professional_signoff(corpus):
    text=(corpus/'09_REPORTS/GOLDEN_SET_BUILD_REPORT.md').read_text(encoding='utf-8-sig')
    assert 'BLOCKED' in text and '0 套' in text
    assert not list(corpus.rglob('*.dwg')) and not list(corpus.rglob('*.dxf'))
