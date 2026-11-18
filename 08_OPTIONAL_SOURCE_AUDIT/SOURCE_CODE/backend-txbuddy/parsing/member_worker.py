"""Isolated archive member parser; stdout is a bounded JSON protocol."""
import json
import os
from pathlib import Path
import sys


def verified_ocr_cache(source, native):
    from .base import sha256_file
    cache_root = os.environ.get('TXBUDDY_ARCHIVE_OCR_CACHE')
    if not cache_root:
        return None
    digest = sha256_file(source)
    cache = Path(cache_root).resolve() / digest / 'ocr.json'
    if not cache.is_file():
        return None
    saved = json.loads(cache.read_text(encoding='utf-8'))
    if saved.get('sha256') != digest or saved.get('requested_pages') is not None:
        return None
    if saved.get('formal_usable') is not False or saved.get('review_status') != 'NEEDS_EXPERT_REVIEW':
        return None
    pages = saved.get('pages', [])
    expected_pages = native.metadata.get('pages')
    if isinstance(expected_pages, int) and {p['page'] for p in pages} != set(range(1, expected_pages + 1)):
        return None
    for page in pages:
        image = Path(page['image']).resolve()
        if not image.is_file() or sha256_file(image) != page['image_sha256']:
            return None
    if not pages or not saved.get('tokens'):
        return None
    return Path(cache_root).resolve()


def main():
    from . import parse_file
    result = parse_file(Path(sys.argv[1]))
    if os.environ.get('TXBUDDY_ARCHIVE_OCR') == '1' and result.status.value != 'PARSED' and result.format in ('pdf', 'png', 'jpg', 'jpeg'):
        from .ocr import offline_ocr
        # Only a draft is produced; no review record or formal fee is created.
        if '加密' not in (result.blocked_reason or ''):
            cache = verified_ocr_cache(Path(sys.argv[1]), result)
            result = offline_ocr(Path(sys.argv[1]), cache or Path(os.environ['TXBUDDY_DERIVED_ROOT']) / 'ocr')
            if cache is not None:
                result.metadata['cache_reuse_verification'] = 'source SHA256 and every page-image SHA256 matched; complete cached pages'
    print(json.dumps(result.to_dict(), ensure_ascii=False))


if __name__ == '__main__':
    main()
