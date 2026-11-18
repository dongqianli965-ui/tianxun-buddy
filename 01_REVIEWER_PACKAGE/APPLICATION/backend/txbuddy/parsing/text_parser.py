from pathlib import Path
import csv, io, zipfile
from .base import Locator, ParsedDocument, ParseStatus, TextBlock, source_facts
from .html_parser import decode_text


def parse_text(path: str | Path) -> ParsedDocument:
    digest,size=source_facts(path); suffix=Path(path).suffix.lower().lstrip('.')
    try:
        text,encoding=decode_text(Path(path).read_bytes())
        rows=list(csv.reader(io.StringIO(text),delimiter='\t' if suffix=='tsv' else ',')) if suffix in ('csv','tsv') else None
        blocks=[TextBlock(line,Locator(line=i), 'row' if rows is not None else 'text')
                for i,line in enumerate((' | '.join(row) for row in rows) if rows is not None else text.splitlines(),1) if line.strip()]
        return ParsedDocument(str(path),suffix,ParseStatus.PARSED if blocks else ParseStatus.FAILED,
            blocks,digest,size,metadata={'encoding':encoding,'rows':rows},error=None if blocks else '空文本')
    except Exception as exc:
        return ParsedDocument(str(path),suffix,ParseStatus.FAILED,sha256=digest,size_bytes=size,error=str(exc))


def inspect_archive(path: str | Path) -> ParsedDocument:
    digest,size=source_facts(path); suffix=Path(path).suffix.lower().lstrip('.')
    entries=[]
    if suffix=='zip':
        try:
            with zipfile.ZipFile(path) as z:
                entries=[{'name':x.filename,'bytes':x.file_size,'encrypted':bool(x.flag_bits&1)} for x in z.infolist()]
        except Exception as exc:
            return ParsedDocument(str(path),suffix,ParseStatus.FAILED,sha256=digest,size_bytes=size,error=str(exc))
    return ParsedDocument(str(path),suffix,ParseStatus.BLOCKED,sha256=digest,size_bytes=size,
        metadata={'archive_entries':entries,'content_parsed':False},
        blocked_reason='归档仅完成目录检查，未解析包内文件' if entries else '未配置获授权的 RAR 解压器')
