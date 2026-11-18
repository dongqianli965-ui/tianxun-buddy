def test_all_37_frontend_endpoints_resolve_and_extended_post_routes_are_classified():
    from txbuddy.api import app
    from starlette.routing import Mount
    import re
    from pathlib import Path
    def paths(application,prefix=''):
        for r in application.routes:
            if hasattr(r,'original_router'):
                yield from paths(r.original_router,prefix+r.include_context.prefix);continue
            path=(prefix+getattr(r,'path','')).replace('//','/')
            if hasattr(r,'methods'):yield from ((m,path) for m in r.methods)
            if isinstance(r,Mount) and hasattr(r.app,'routes'):yield from paths(r.app,path.rstrip('/'))
    normalize=lambda p:re.sub(r'\{[^}]+\}','{}',p.split('?')[0])
    actual={(m,normalize(p)) for m,p in paths(app)}
    text=(Path(__file__).resolve().parents[2]/'frontend/src/endpoints.ts').read_text(encoding='utf-8-sig')
    entries=re.findall(r'^\s*(\w+): E\("[^"]+", "[^"]+", "(GET|POST|PUT|DELETE)", "([^"]+)"',text,re.M)
    assert len(entries)==38
    assert ('materialPrices', 'GET', '/v1/material-prices') in entries
    for key,method,path in entries:
        candidates=[path.replace('{action}',a) for a in ('start','complete','approve','fail','retry')] if key=='taskAction' else [path]
        assert all((method,normalize(p)) in actual for p in candidates),key
    extension=next(r.app for r in app.routes if isinstance(r,Mount) and r.path=='')
    declared={'/auth/login','/auth/logout','/projects/{project_id}/check','/projects/{project_id}/members','/v1/parse','/v1/pilot/boq-import','/v1/quota-library/import','/v1/quota-library/match','/v1/deliverables/{job_id}/generate'}
    assert {p for m,p in paths(extension) if m=='POST'}==declared
