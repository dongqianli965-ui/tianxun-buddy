"""P1-10：零依赖的 PDF 1.4 生成器（预算书用）。

为什么自己写：运行环境没有 reportlab/fpdf/weasyprint，不能为一次出件新增未入库依赖。
生成结果是标准 PDF：``%PDF-1.4`` 头、交叉引用表、页面树、内容流齐全，pypdf 能
重新解析页数与文本。中文正文使用 Adobe 标准 CJK 字体 ``STSong-Light`` +
``UniGB-UCS2-H`` 编码（不嵌入字体文件，阅读器按标准 CMap 取字）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .xlsx_util import display_width

A4_WIDTH = 595.28
A4_HEIGHT = 841.89
MARGIN_X = 42.0
MARGIN_TOP = 46.0
MARGIN_BOTTOM = 48.0


def _pdf_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _hex_text(text: str) -> str:
    return text.encode("utf-16-be").hex().upper()


def _pdf_string(text: str) -> str:
    """Info 字典字符串：ASCII 用字面量，中文用带 BOM 的 UTF-16BE 十六进制串。"""
    if text.isascii():
        return f"({_pdf_escape(text)})"
    return f"<FEFF{_hex_text(text)}>"


def _fit(text: object, width: int) -> str:
    value = "" if text is None else str(text)
    if display_width(value) <= width:
        return value
    out = ""
    for char in value:
        if display_width(out + char) > width - 2:
            break
        out += char
    return out + ".."


def _pad(text: object, width: int, align: str) -> str:
    value = _fit(text, width)
    gap = max(0, width - display_width(value))
    return (" " * gap + value) if align == "r" else (value + " " * gap)


class PdfPage:
    def __init__(self) -> None:
        self.ops: list[str] = []
        self.y = A4_HEIGHT - MARGIN_TOP
        self.rules: list[float] = []
        self.codes: set[int] = set()

    @property
    def bottom(self) -> float:
        return MARGIN_BOTTOM

    def room(self, lines: int = 1, size: float = 9.5) -> bool:
        return self.y - (lines - 1) * size * 1.55 >= self.bottom

    def text(self, value: str, *, size: float = 9.5, x: float = MARGIN_X,
             font: str | None = None, advance: bool = True) -> None:
        if not self.room():
            raise ValueError("PDF page is full")
        if value:
            use = font or ("F1" if value.isascii() else "F2")
            if use == "F1" and any(ord(c) > 0x7E or ord(c) < 32 for c in value):
                use = "F2"
            if use == "F1":
                literal = f"({_pdf_escape(value)}) Tj"
            else:
                literal = f"<{_hex_text(value)}> Tj"
                self.codes.update(ord(char) for char in value if ord(char) > 0x7E)
            self.ops.append(f"BT /{use} {size:g} Tf 1 0 0 1 {x:.2f} {self.y:.2f} Tm {literal} ET")
        if advance:
            self.y -= size * 1.55

    def rule(self, *, x1: float = MARGIN_X, x2: float = A4_WIDTH - MARGIN_X,
             width: float = 0.6, gray: float = 0.55) -> None:
        self.ops.append(f"{gray:g} G {width:g} w {x1:.2f} {self.y + 3:.2f} m "
                        f"{x2:.2f} {self.y + 3:.2f} l S")

    def content(self, page_no: int, page_count: int) -> bytes:
        ops = list(self.ops)
        ops.append(f"BT /F1 8 Tf 1 0 0 1 {A4_WIDTH / 2 - 28:.2f} 22 Tm "
                   f"(Page {page_no} of {page_count}) Tj ET")
        return ("\n".join(ops) + "\n").encode("ascii")


class PdfWriter:
    def __init__(self, title: str, *, subject: str = "", author: str = "Tianxun Buddy") -> None:
        self.title = title
        self.subject = subject
        self.author = author
        self.pages: list[PdfPage] = []
        self.new_page()

    @property
    def current(self) -> PdfPage:
        return self.pages[-1]

    def new_page(self) -> PdfPage:
        page = PdfPage()
        self.pages.append(page)
        return page

    def line(self, value: str = "", *, size: float = 9.5, x: float = MARGIN_X,
             advance: bool = True) -> None:
        if not self.current.room(size=size):
            self.new_page()
        self.current.text(value, size=size, x=x, advance=advance)

    def rule(self) -> None:
        self.current.rule()

    def table(self, headers: list[str], widths: list[int], rows: list[list[object]],
              *, size: float = 8.5, aligns: list[str] | None = None,
              repeat_header: bool = True, page_header: tuple[str, ...] = ()) -> None:
        aligns = aligns or ["l"] * len(headers)
        column_widths = [w for w in widths]

        def draw_header() -> None:
            self.line(self._row(headers, column_widths, aligns), size=size)
            self.rule()

        draw_header()
        for row in rows:
            if not self.current.room(size=size):
                self.new_page()
                for text in page_header:
                    self.line(text, size=size + 2)
                if repeat_header:
                    draw_header()
            self.line(self._row(row, column_widths, aligns), size=size)

    @staticmethod
    def _row(values: list[object], widths: list[int], aligns: list[str]) -> str:
        cells = [_pad(values[i] if i < len(values) else "", widths[i], aligns[i])
                 for i in range(len(widths))]
        return " ".join(cells)

    def _content(self, page_no: int, page_count: int) -> bytes:
        return self.pages[page_no - 1].content(page_no, page_count)

    def _tounicode(self) -> bytes:
        """UniGB-UCS2-H 的字符码就是 UCS-2 码位，ToUnicode 用恒等映射即可。"""
        codes = sorted({code for page in self.pages for code in page.codes
                        if 0 < code <= 0xFFFF})
        entries = "\n".join(f"<{code:04X}> <{code:04X}>" for code in codes)
        return (
            "/CIDInit /ProcSet findresource begin\n"
            "12 dict begin\nbegincmap\n"
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
            "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
            f"{len(codes)} beginbfchar\n{entries}\nendbfchar\n"
            "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
        ).encode("ascii")

    def write(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        count = len(self.pages)
        page_ids = [3 + i for i in range(count)]
        content_ids = [3 + count + i for i in range(count)]
        font1 = 3 + 2 * count
        font2, cid, tounicode, info = font1 + 1, font1 + 2, font1 + 3, font1 + 4

        objects: list[tuple[int, bytes]] = []
        objects.append((1, b"<< /Type /Catalog /Pages 2 0 R >>"))
        kids = " ".join(f"{obj} 0 R" for obj in page_ids)
        objects.append((2, f"<< /Type /Pages /Kids [{kids}] /Count {count} >>".encode("ascii")))
        for index, page_id in enumerate(page_ids):
            body = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {A4_WIDTH} {A4_HEIGHT}] "
                    f"/Resources << /Font << /F1 {font1} 0 R /F2 {font2} 0 R >> >> "
                    f"/Contents {content_ids[index]} 0 R >>")
            objects.append((page_id, body.encode("ascii")))
        for index, content_id in enumerate(content_ids):
            stream = self._content(index + 1, count)
            objects.append((content_id, b"<< /Length " + str(len(stream)).encode("ascii")
                            + b" >>\nstream\n" + stream + b"endstream"))
        objects.append((font1, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                              b"/Encoding /WinAnsiEncoding >>"))
        objects.append((font2, (f"<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light "
                                f"/Encoding /UniGB-UTF16-H /DescendantFonts [{cid} 0 R] "
                                f"/ToUnicode {tounicode} 0 R >>")
                        .encode("ascii")))
        objects.append((cid, b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light "
                              b"/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 2 >> "
                              b"/DW 1000 /W [1 95 500] >>"))
        cmap = self._tounicode()
        objects.append((tounicode, b"<< /Length " + str(len(cmap)).encode("ascii")
                        + b" >>\nstream\n" + cmap + b"endstream"))
        created = datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%S+00'00'")
        info_body = (f"<< /Title {_pdf_string(self.title)} /Subject {_pdf_string(self.subject)} "
                     f"/Author {_pdf_string(self.author)} /Creator (Tianxun Buddy V4.2.3) "
                     f"/Producer (txbuddy-deliverables) /CreationDate ({created}) >>")
        objects.append((info, info_body.encode("ascii")))
        objects.sort(key=lambda item: item[0])

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: dict[int, int] = {}
        for obj_id, body in objects:
            offsets[obj_id] = len(out)
            out += f"{obj_id} 0 obj\n".encode("ascii") + body + b"\nendobj\n"
        size = max(offsets) + 1
        xref_at = len(out)
        out += f"xref\n0 {size}\n".encode("ascii")
        out += b"0000000000 65535 f \n"
        for obj_id in range(1, size):
            out += f"{offsets[obj_id]:010d} 00000 n \n".encode("ascii")
        out += (f"trailer\n<< /Size {size} /Root 1 0 R /Info {info} 0 R >>\n"
                f"startxref\n{xref_at}\n%%EOF\n").encode("ascii")
        target.write_bytes(bytes(out))
        return target
