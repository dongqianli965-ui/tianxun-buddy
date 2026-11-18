"""DXF 解析器：抽取 TEXT/MTEXT/ATTRIB/DIMENSION 文字，保留 entity_id / layer / 坐标。"""
from __future__ import annotations

from pathlib import Path

from .base import Locator, ParsedDocument, ParseStatus, TextBlock, source_facts

__all__ = ["parse_dxf"]

_TEXT_TYPES = ("TEXT", "MTEXT", "ATTRIB", "DIMENSION", "MULTILEADER")


def _entity_text(entity) -> str:
    dxftype = entity.dxftype()
    try:
        if dxftype == "MTEXT":
            return (entity.plain_text() or "").strip()
        if dxftype == "MULTILEADER":
            getter = getattr(entity, "get_mtext", None)
            return (getter() if callable(getter) else "").strip()
        return str(entity.dxf.get("text", "") or "").strip()
    except Exception:  # noqa: BLE001 - 单个图元异常不拖垮整图
        return ""


def _entity_point(entity):
    for attribute in ("insert", "align_point", "location"):
        point = entity.dxf.get(attribute, None) if entity.dxf.is_supported(attribute) else None
        if point is not None:
            try:
                return float(point[0]), float(point[1])
            except (TypeError, IndexError):
                continue
    return None


def parse_dxf(path: str | Path) -> ParsedDocument:
    """解析 ASCII/Binary DXF，每个文字图元一个块，定位以图元 handle 为准。"""
    digest, size = source_facts(path)
    source = str(Path(path))

    try:
        import ezdxf
    except ImportError as exc:  # pragma: no cover
        return ParsedDocument(
            source=source, format="dxf", status=ParseStatus.FAILED,
            sha256=digest, size_bytes=size, error=f"ezdxf 不可用: {exc}",
        )

    try:
        document = ezdxf.readfile(source)
    except Exception as exc:  # noqa: BLE001
        return ParsedDocument(
            source=source, format="dxf", status=ParseStatus.FAILED,
            sha256=digest, size_bytes=size, error=f"{type(exc).__name__}: {exc}",
        )

    blocks: list[TextBlock] = []
    warnings: list[str] = []
    entity_counts: dict[str, int] = {}
    text_entities = 0
    try:
        for layout in document.layouts:
            space = "model" if layout.name.lower() == "model" else layout.name
            for entity in layout:
                dxftype = entity.dxftype()
                entity_counts[dxftype] = entity_counts.get(dxftype, 0) + 1
                if dxftype not in _TEXT_TYPES:
                    continue
                text = _entity_text(entity)
                if not text:
                    continue
                text_entities += 1
                point = _entity_point(entity)
                locator = Locator(
                    entity_id=f"{dxftype}:{entity.dxf.get('handle', '?')}",
                    entity_type=dxftype,
                    layer=str(entity.dxf.get("layer", "") or "") or None,
                    space=space,
                    x=point[0] if point else None,
                    y=point[1] if point else None,
                )
                blocks.append(TextBlock(text=text, locator=locator, kind="entity"))
    except Exception as exc:  # noqa: BLE001
        return ParsedDocument(
            source=source, format="dxf", status=ParseStatus.FAILED,
            sha256=digest, size_bytes=size, error=f"{type(exc).__name__}: {exc}",
            blocks=blocks,
        )

    if not text_entities:
        warnings.append("图纸中没有 TEXT/MTEXT/ATTRIB 文字实体")

    return ParsedDocument(
        source=source,
        format="dxf",
        status=ParseStatus.PARSED if blocks else ParseStatus.FAILED,
        blocks=blocks,
        sha256=digest,
        size_bytes=size,
        metadata={
            "dxf_version": document.dxfversion,
            "layouts": [layout.name for layout in document.layouts],
            "entity_counts": entity_counts,
            "text_entities": text_entities,
        },
        warnings=warnings,
        error=None if blocks else "DXF 中没有可提取的文字实体",
    )
