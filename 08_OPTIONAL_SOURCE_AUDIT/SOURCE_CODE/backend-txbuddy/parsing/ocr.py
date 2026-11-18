"""Offline OCR. Original bytes are never written; every token retains its page image and box."""
from pathlib import Path
import hashlib, json, os
from .base import Locator, ParsedDocument, ParseStatus, TextBlock, source_facts

_engine = None


def offline_ocr(path: str | Path, derived_root: str | Path, *, pages: list[int] | None = None,
                scale: float = 2.0) -> ParsedDocument:
    global _engine
    digest,size=source_facts(path); root=Path(derived_root).resolve(); source=Path(path).resolve()
    # Never place derived output inside the corpus or replace the original.
    if root==source or root in source.parents or source.parent==root:
        raise ValueError('OCR 派生目录必须与原件目录分离')
    target=root/digest; target.mkdir(parents=True,exist_ok=True)
    cache=target/'ocr.json'
    if cache.exists():
        saved=json.loads(cache.read_text(encoding='utf-8'))
        if saved['sha256']==digest and saved.get('requested_pages')==pages:
            return _result(path,digest,size,saved)
    try:
        import pymupdf
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR
        if _engine is None:
            _engine=RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
        page_records=[]; tokens=[]
        if source.suffix.lower()=='.pdf':
            doc=pymupdf.open(source)
            selected=pages or list(range(1,len(doc)+1))
            for number in selected:
                if number<1 or number>len(doc): raise ValueError('页码越界')
                pix=doc[number-1].get_pixmap(matrix=pymupdf.Matrix(scale,scale),alpha=False)
                png=target/f'page_{number:04}.png'; pix.save(str(png))
                rows,_=_engine(str(png)); rows=rows or []
                page_records.append({'page':number,'image':str(png),'image_sha256':hashlib.sha256(png.read_bytes()).hexdigest(),'tokens':len(rows)})
                for box,text,confidence in rows:
                    tokens.append({'page':number,'image':str(png),'bbox':[[float(v) for v in pt] for pt in box],
                        'text':text,'confidence':float(confidence),'review_status':'NEEDS_EXPERT_REVIEW',
                        'reviewer':None,'source_sha256':digest})
            doc.close()
        else:
            from PIL import Image
            Image.open(source).convert('RGB').save(target/'page_0001.png')
            png=target/'page_0001.png'; rows,_=_engine(str(png)); rows=rows or []
            page_records=[{'page':1,'image':str(png),'image_sha256':hashlib.sha256(png.read_bytes()).hexdigest(),'tokens':len(rows)}]
            for box,text,confidence in rows:
                tokens.append({'page':1,'image':str(png),'bbox':[[float(v) for v in pt] for pt in box],
                    'text':text,'confidence':float(confidence),'review_status':'NEEDS_EXPERT_REVIEW','reviewer':None,'source_sha256':digest})
        saved={'sha256':digest,'source':str(source),'engine':'rapidocr_onnxruntime/1.4.4',
            'requested_pages':pages,'pages':page_records,'tokens':tokens,'formal_usable':False,
            'review_status':'NEEDS_EXPERT_REVIEW'}
        tmp=cache.with_suffix('.tmp'); tmp.write_text(json.dumps(saved,ensure_ascii=False,indent=2),encoding='utf-8'); os.replace(tmp,cache)
        assert source_facts(path)[0]==digest
        return _result(path,digest,size,saved)
    except Exception as exc:
        return ParsedDocument(str(path),'ocr',ParseStatus.BLOCKED,sha256=digest,size_bytes=size,
            blocked_reason=f'OCR_ENGINE_FAILED: {type(exc).__name__}: {exc}')


def _result(path,digest,size,saved):
    blocks=[TextBlock(t['text'],Locator(page=t['page'],line=i+1,
        bbox=(t['bbox'][0][0],t['bbox'][0][1],t['bbox'][2][0],t['bbox'][2][1])),'ocr') for i,t in enumerate(saved['tokens'])]
    return ParsedDocument(str(path),'ocr',ParseStatus.PARSED if blocks else ParseStatus.BLOCKED,blocks,digest,size,
        metadata=saved,blocked_reason=None if blocks else 'OCR_EMPTY: 未识别到文本，需人工转录',
        warnings=['OCR 尚未人工校对，不得直接进入正式费率或计算'])
