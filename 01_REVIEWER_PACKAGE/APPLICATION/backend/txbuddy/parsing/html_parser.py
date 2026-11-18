"""Read local HTML snapshots without executing content or fetching remote resources."""
from pathlib import Path
import re
from .base import Locator, ParsedDocument, ParseStatus, TextBlock, source_facts


def decode_text(raw: bytes) -> tuple[str, str]:
    declared = re.search(br'charset\s*=\s*["\']?([a-zA-Z0-9_-]+)', raw[:8192], re.I)
    candidates = ['utf-8-sig']
    if declared:
        candidates.append(declared.group(1).decode('ascii'))
    candidates.extend(['gb18030', 'utf-16'])
    for enc in candidates:
        try:
            return raw.decode(enc), enc
        except (UnicodeError, LookupError):
            continue
    raise ValueError('无法无损解码文本；需要人工确认编码')


def parse_html(path: str | Path, *, source_url: str | None = None) -> ParsedDocument:
    digest, size = source_facts(path)
    try:
        from bs4 import BeautifulSoup
        text, encoding = decode_text(Path(path).read_bytes())
        soup = BeautifulSoup(text, 'html.parser')
        title = soup.title.get_text(' ', strip=True) if soup.title else ''
        canonical = soup.find('link', rel='canonical')
        url = source_url or (canonical.get('href') if canonical else None)
        meta = {str(x.get('name') or x.get('property')): x.get('content')
                for x in soup.find_all('meta') if x.get('content')}
        for node in soup(['script', 'style', 'noscript', 'template']):
            node.decompose()
        tables = []
        blocks = []
        for ti, table in enumerate(soup.find_all('table')):
            grid = []
            occupied = {}
            for ri, tr in enumerate(table.find_all('tr')):
                row = []; ci = 0
                for cell in tr.find_all(['td', 'th'], recursive=False):
                    while (ri, ci) in occupied:
                        row.append(occupied[(ri, ci)]); ci += 1
                    value = cell.get_text(' ', strip=True)
                    rs = min(1000, max(1, int(cell.get('rowspan', 1))))
                    cs = min(1000, max(1, int(cell.get('colspan', 1))))
                    for dr in range(rs):
                        for dc in range(cs):
                            occupied[(ri+dr, ci+dc)] = value
                    row.extend([value]*cs); ci += cs
                    if value:
                        blocks.append(TextBlock(value, Locator(table_index=ti, row=ri+1, column=ci-cs+1), 'table_cell'))
                while (ri, ci) in occupied:
                    row.append(occupied[(ri, ci)]); ci += 1
                if row:
                    grid.append(row)
            tables.append({'index':ti, 'rows':grid})
        for node in (soup.body or soup).find_all(['h1','h2','h3','h4','p','li']):
            value = node.get_text(' ',strip=True)
            if value and not node.find_parent('table'):
                kind = 'heading' if node.name.startswith('h') else ('list_item' if node.name=='li' else 'text')
                blocks.append(TextBlock(value, Locator(paragraph=len(blocks)+1), kind))
        if not blocks:
            for i,line in enumerate((soup.body or soup).get_text('\n',strip=True).splitlines(),1):
                if line.strip(): blocks.append(TextBlock(line.strip(),Locator(line=i)))
        return ParsedDocument(str(path), Path(path).suffix.lstrip('.').lower(),
            ParseStatus.PARSED if blocks else ParseStatus.FAILED, blocks, digest, size,
            metadata={'title':title,'source_url':url,'encoding':encoding,'html_metadata':meta,'tables':tables},
            error=None if blocks else 'HTML 正文为空')
    except Exception as exc:
        return ParsedDocument(str(path),'html',ParseStatus.FAILED,sha256=digest,size_bytes=size,error=str(exc))
