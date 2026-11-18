"""P1-09-PARSE：XLSX / PDF / DOCX / DXF 解析 + DWG 授权转换适配。

覆盖 TASK.md 的四条硬要求：
1. 统一结构：所有格式都输出 ``TextBlock`` + ``Locator``（page/row/sheet/entity_id）；
2. DWG 未配置转换器 → BLOCKED（blocks 为空，绝不伪造），配置后走真实子进程转换；
3. 每类格式的测试样本在测试内生成，并断言定位信息存在；
4. 解析不改写原始文件（解析前后 SHA-256 一致）。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from txbuddy.parsing import (
    ConverterConfig,
    ConverterNotConfigured,
    DwgConverterAdapter,
    Locator,
    ParseStatus,
    SourceNotFound,
    TextBlock,
    normalise_suffix,
    parse_docx,
    parse_dxf,
    parse_file,
    parse_pdf,
    parse_xlsx,
    sha256_file,
    supported_extensions,
)


# --------------------------------------------------------------------------- 样本生成
def build_xlsx(path: Path) -> Path:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "分部分项"
    sheet.append(["序号", "项目名称", "工程量"])
    sheet.append([1, "现浇混凝土基础", 12.5])
    sheet.append([2, "钢筋 HRB400", 3])
    sheet.append([])
    sheet.append([3, "模板工程", 88.25])
    other = workbook.create_sheet("措施项目")
    other.append(["安全文明施工费", 10000])
    workbook.save(path)
    return path


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def build_pdf(path: Path, pages: list[list[str]]) -> Path:
    """手写最小合法 PDF（Helvetica 文本层），避免额外依赖。"""
    objects: list[bytes] = []
    page_ids = [4 + 2 * index for index in range(len(pages))]
    content_ids = [5 + 2 * index for index in range(len(pages))]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for page_id, content_id, lines in zip(page_ids, content_ids, pages):
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
            ).encode()
        )
        ops = ["BT", "/F1 12 Tf", "72 780 Td", "14 TL"]
        for line in lines:
            ops.append(f"({_pdf_escape(line)}) Tj")
            ops.append("T*")
        ops.append("ET")
        stream = "\n".join(ops).encode("latin-1")
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_start = len(out)
    count = len(objects) + 1
    out += f"xref\n0 {count}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {count} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF\n"
    ).encode()
    path.write_bytes(bytes(out))
    return path


# --------------------------------------------------------------------------- XLSX
def test_xlsx_blocks_carry_sheet_row_and_cell(tmp_path: Path) -> None:
    sample = build_xlsx(tmp_path / "boq.xlsx")
    before = hashlib.sha256(sample.read_bytes()).hexdigest()

    document = parse_xlsx(sample)

    assert document.status is ParseStatus.PARSED
    assert document.format == "xlsx"
    assert document.block_count() > 0
    assert document.sha256 == before
    assert document.size_bytes == sample.stat().st_size
    assert document.unlocated_blocks() == []

    rows = [block for block in document.blocks if block.locator.sheet == "分部分项"]
    assert rows, "分部分项工作表应有文本块"
    header = rows[0]
    assert header.locator.row == 1
    assert header.locator.cell == "A1"
    assert "项目名称" in header.text and "工程量" in header.text

    concrete = next(block for block in rows if "现浇混凝土基础" in block.text)
    assert concrete.locator.row == 2
    assert concrete.locator.sheet == "分部分项"
    assert concrete.locator.column == 1
    # 数字单元格按文本保留（12.5 不应被截断/改写）
    assert "12.5" in concrete.text


def test_xlsx_multi_sheet_and_blank_rows(tmp_path: Path) -> None:
    sample = build_xlsx(tmp_path / "boq_multi.xlsx")
    document = parse_xlsx(sample)

    sheets = {item["name"]: item for item in document.metadata["sheets"]}
    assert sheets["分部分项"]["non_empty_rows"] == 4  # 第 4 行是空行，跳过
    assert sheets["措施项目"]["non_empty_rows"] == 1
    assert [block.locator.sheet for block in document.blocks] == (
        ["分部分项"] * 4 + ["措施项目"]
    )
    assert all(block.locator.row is not None for block in document.blocks)


# --------------------------------------------------------------------------- PDF
def test_pdf_pages_and_lines_are_located(tmp_path: Path) -> None:
    sample = build_pdf(
        tmp_path / "spec.pdf",
        [
            ["Chapter 1 BOQ notes", "1.1 unit price basis"],
            ["Chapter 2 pricing basis", "2.1 Henan 2025 quota"],
        ],
    )
    before = sha256_file(sample)

    document = parse_pdf(sample)

    assert document.status is ParseStatus.PARSED
    assert document.format == "pdf"
    assert document.metadata["pages"] == 2
    assert document.sha256 == before
    assert document.unlocated_blocks() == []

    page1 = [block for block in document.blocks if block.locator.page == 1]
    page2 = [block for block in document.blocks if block.locator.page == 2]
    assert [block.locator.line for block in page1] == [1, 2]
    assert page1[0].text.startswith("Chapter 1")
    assert page2[0].text.startswith("Chapter 2")
    assert all(block.locator.page is not None for block in document.blocks)


def test_pdf_page_without_text_layer_is_reported_not_invented(tmp_path: Path) -> None:
    sample = build_pdf(tmp_path / "scan.pdf", [["page with text layer"], []])
    document = parse_pdf(sample)

    assert document.status is ParseStatus.PARSED
    assert document.metadata["empty_pages"] == [2]
    assert all(block.locator.page == 1 for block in document.blocks)
    assert any("OCR" in warning and "2" in warning for warning in document.warnings)


def test_pdf_without_any_text_is_failed(tmp_path: Path) -> None:
    sample = build_pdf(tmp_path / "blank.pdf", [[]])
    document = parse_pdf(sample)

    assert document.status is ParseStatus.FAILED
    assert document.blocks == []
    assert document.error and "文本层" in document.error

# --------------------------------------------------------------------------- DOCX
def build_docx(path: Path) -> Path:
    from docx import Document

    document = Document()
    document.add_heading("工程量清单编制说明", level=1)
    document.add_paragraph("本工程采用清单计价，依据 GB/T 50500-2024。")
    document.add_paragraph("")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "项目编码"
    table.cell(0, 1).text = "项目名称"
    table.cell(1, 0).text = "010101001"
    table.cell(1, 1).text = "平整场地"
    document.save(path)
    return path


def test_docx_paragraphs_and_table_cells_are_located(tmp_path: Path) -> None:
    sample = build_docx(tmp_path / "spec.docx")
    before = sha256_file(sample)

    document = parse_docx(sample)

    assert document.status is ParseStatus.PARSED
    assert document.format == "docx"
    assert document.sha256 == before
    assert document.unlocated_blocks() == []
    assert document.metadata["tables"] == 1

    paragraphs = [block for block in document.blocks if block.kind == "paragraph"]
    assert paragraphs[0].text == "工程量清单编制说明"
    assert paragraphs[0].locator.paragraph == 1
    assert "GB/T 50500-2024" in paragraphs[1].text
    assert paragraphs[1].locator.paragraph == 2
    assert all(block.locator.paragraph is not None for block in paragraphs)

    cells = [block for block in document.blocks if block.kind == "table_cell"]
    assert [(cell.locator.row, cell.locator.column) for cell in cells] == [
        (1, 1), (1, 2), (2, 1), (2, 2)
    ]
    assert all(cell.locator.table_index == 0 for cell in cells)
    assert any(cell.text == "平整场地" and cell.locator.row == 2 for cell in cells)


# --------------------------------------------------------------------------- DXF
def build_dxf(path: Path) -> Path:
    import ezdxf

    document = ezdxf.new("R2010")
    modelspace = document.modelspace()
    modelspace.add_text("给排水管道 DN100").set_placement((120.5, 340.25))
    modelspace.add_mtext("混凝土强度等级 C30").set_location((10.0, 20.0))
    modelspace.add_line((0, 0), (100, 0))
    paperspace = document.layouts.get("Layout1")
    paperspace.add_text("图框标题：一层平面图").set_placement((5.0, 5.0))
    document.saveas(path)
    return path


def test_dxf_entities_carry_entity_id_layer_and_coordinates(tmp_path: Path) -> None:
    sample = build_dxf(tmp_path / "plan.dxf")
    before = sha256_file(sample)

    document = parse_dxf(sample)

    assert document.status is ParseStatus.PARSED
    assert document.format == "dxf"
    assert document.sha256 == before
    assert document.unlocated_blocks() == []
    assert "LINE" in document.metadata["entity_counts"]

    texts = {block.text: block for block in document.blocks}
    assert "给排水管道 DN100" in texts
    pipe = texts["给排水管道 DN100"]
    assert pipe.locator.entity_id.startswith("TEXT:")
    assert pipe.locator.entity_type == "TEXT"
    assert pipe.locator.layer == "0"
    assert pipe.locator.space == "model"
    assert pipe.locator.x == pytest.approx(120.5)
    assert pipe.locator.y == pytest.approx(340.25)

    mtext = texts["混凝土强度等级 C30"]
    assert mtext.locator.entity_id.startswith("MTEXT:")
    assert (mtext.locator.x, mtext.locator.y) == pytest.approx((10.0, 20.0))

    title = texts["图框标题：一层平面图"]
    assert title.locator.space == "Layout1"
    assert title.locator.entity_id.startswith("TEXT:")


# --------------------------------------------------------------------------- 统一模型
def test_text_block_without_locator_is_rejected() -> None:
    with pytest.raises(ValueError):
        TextBlock(text="无定位", locator=Locator())


def test_locator_describe_and_dict_include_position() -> None:
    locator = Locator(sheet="分部分项", row=3, cell="A3")
    assert "sheet=分部分项" in locator.describe()
    assert locator.as_dict()["row"] == 3
    assert locator.is_located()

# --------------------------------------------------------------------------- DWG 适配层
CONVERTER_SCRIPT = """\
import sys
import time
from pathlib import Path

args = sys.argv[1:]
if "--sleep" in args:
    time.sleep(30)
source = Path(args[args.index("--in") + 1])
target = Path(args[args.index("--out") + 1])
if "--exit-code" in args:
    sys.exit(int(args[args.index("--exit-code") + 1]))
if "--silent" in args:
    sys.exit(0)
import ezdxf

document = ezdxf.new("R2010")
document.modelspace().add_text("DWGCONVERTED DN150").set_placement((7.0, 8.0))
document.saveas(target)
"""


def make_converter(tmp_path: Path, mode: str = "ok") -> str:
    script = tmp_path / f"converter_{mode}.py"
    script.write_text(CONVERTER_SCRIPT, encoding="utf-8")
    extra = {
        "ok": "",
        "fail": " --exit-code 3",
        "silent": " --silent",
        "slow": " --sleep",
    }[mode]
    return f'"{sys.executable}" "{script}" --in "{{input}}" --out "{{output}}"{extra}'


def fake_dwg(tmp_path: Path) -> Path:
    path = tmp_path / "plan.dwg"
    path.write_bytes(b"AC1032" + bytes(range(64)))
    return path


def test_dwg_without_converter_is_blocked_not_faked(tmp_path: Path) -> None:
    sample = fake_dwg(tmp_path)
    before = sha256_file(sample)
    adapter = DwgConverterAdapter(environ={})

    assert adapter.configured is False
    document = adapter.parse(sample)

    assert document.status is ParseStatus.BLOCKED
    assert document.blocks == []
    assert document.error is None
    assert "TXBUDDY_DWG_CONVERTER" in document.blocked_reason
    assert document.sha256 == before
    assert document.metadata["original_preserved"] is True
    assert document.unlocated_blocks() == []


def test_parse_file_dwg_blocked_when_env_missing(tmp_path: Path, monkeypatch) -> None:
    sample = fake_dwg(tmp_path)
    monkeypatch.delenv("TXBUDDY_DWG_CONVERTER", raising=False)

    document = parse_file(sample)

    assert document.status is ParseStatus.BLOCKED
    assert document.format == "dwg"
    assert document.blocks == []
    assert "未配置 DWG 转换器" in document.blocked_reason


def test_dwg_converter_produces_located_blocks(tmp_path: Path) -> None:
    sample = fake_dwg(tmp_path)
    before = sha256_file(sample)
    adapter = DwgConverterAdapter(command=make_converter(tmp_path))

    document = adapter.parse(sample)

    assert document.status is ParseStatus.PARSED, document.error
    assert document.format == "dwg"
    assert document.block_count() == 1
    assert document.unlocated_blocks() == []
    block = document.blocks[0]
    assert block.text == "DWGCONVERTED DN150"
    assert block.locator.entity_id.startswith("TEXT:")
    assert (block.locator.x, block.locator.y) == pytest.approx((7.0, 8.0))
    assert document.converter == "external-dwg-converter"
    conversion = document.metadata["conversion"]
    assert conversion["converted_suffix"] == ".dxf"
    assert len(conversion["converted_sha256"]) == 64
    assert conversion["artifact_kept"] is False
    assert conversion["artifact_path"] is None
    # 原始 DWG 不被改写
    assert document.sha256 == before
    assert sha256_file(sample) == before


def test_dwg_converter_failure_is_failed_without_blocks(tmp_path: Path) -> None:
    sample = fake_dwg(tmp_path)
    adapter = DwgConverterAdapter(command=make_converter(tmp_path, "fail"))

    document = adapter.parse(sample)

    assert document.status is ParseStatus.FAILED
    assert document.blocks == []
    assert "退出码 3" in document.error
    assert document.unlocated_blocks() == []


def test_dwg_converter_without_output_is_failed(tmp_path: Path) -> None:
    sample = fake_dwg(tmp_path)
    adapter = DwgConverterAdapter(command=make_converter(tmp_path, "silent"))

    document = adapter.parse(sample)

    assert document.status is ParseStatus.FAILED
    assert document.blocks == []
    assert "未产出有效文件" in document.error


def test_dwg_converter_timeout_is_failed(tmp_path: Path) -> None:
    sample = fake_dwg(tmp_path)
    adapter = DwgConverterAdapter(command=make_converter(tmp_path, "slow"), timeout=0.5)

    document = adapter.parse(sample)

    assert document.status is ParseStatus.FAILED
    assert document.blocks == []
    assert "超时" in document.error


def test_converter_config_requires_placeholders() -> None:
    with pytest.raises(ConverterNotConfigured):
        ConverterConfig(command="converter.exe {input}")
    with pytest.raises(ConverterNotConfigured):
        ConverterConfig(command="converter.exe {output}")
    config = ConverterConfig(command='"C:\\ODA\\ODAFileConverter.exe" {input} {output}')
    assert config.timeout > 0

# --------------------------------------------------------------------------- 统一入口 / 注册表
def test_parse_file_dispatch_all_formats_returns_located_blocks(tmp_path: Path) -> None:
    samples = {
        "xlsx": build_xlsx(tmp_path / "all.xlsx"),
        "pdf": build_pdf(tmp_path / "all.pdf", [["dispatch smoke test"]]),
        "docx": build_docx(tmp_path / "all.docx"),
        "dxf": build_dxf(tmp_path / "all.dxf"),
    }
    expected = {"xlsx": "sheet", "pdf": "page", "docx": "paragraph", "dxf": "entity_id"}

    for suffix, sample in samples.items():
        document = parse_file(sample)
        assert document.status is ParseStatus.PARSED, (suffix, document.error)
        assert document.format == suffix
        assert document.block_count() > 0
        assert document.unlocated_blocks() == []
        assert document.sha256 == sha256_file(sample)
        key = expected[suffix]
        for block in document.blocks:
            data = block.locator.as_dict()
            if suffix == "docx":
                # 正文段落用 paragraph，表格单元用 table_index/row/column
                assert data.get("paragraph") is not None or data.get("table_index") is not None
            else:
                assert data.get(key) is not None
        payload = json.loads(json.dumps(document.to_dict(), ensure_ascii=False))
        assert payload["status"] == "PARSED"
        assert payload["blocks"][0]["locator_desc"] != "<unlocated>"


def test_parse_file_unknown_format_and_missing_source(tmp_path: Path) -> None:
    other = tmp_path / "note.unknown"
    other.write_text("plain text", encoding="utf-8")

    document = parse_file(other)
    assert document.status is ParseStatus.FAILED
    assert document.blocks == []
    assert "不支持的格式" in document.error

    with pytest.raises(SourceNotFound):
        parse_file(tmp_path / "missing.xlsx")


def test_parse_file_dwg_uses_environment_converter(tmp_path: Path, monkeypatch) -> None:
    sample = fake_dwg(tmp_path)
    monkeypatch.setenv("TXBUDDY_DWG_CONVERTER", make_converter(tmp_path))
    monkeypatch.delenv("TXBUDDY_DWG_TIMEOUT", raising=False)

    document = parse_file(sample)

    assert document.status is ParseStatus.PARSED, document.error
    assert document.blocks[0].locator.entity_id.startswith("TEXT:")
    assert document.metadata["adapter"]["configured"] is True


def test_parse_file_dwg_with_bad_timeout_env_is_blocked(tmp_path: Path, monkeypatch) -> None:
    sample = fake_dwg(tmp_path)
    monkeypatch.setenv("TXBUDDY_DWG_CONVERTER", make_converter(tmp_path))
    monkeypatch.setenv("TXBUDDY_DWG_TIMEOUT", "not-a-number")

    document = parse_file(sample)

    assert document.status is ParseStatus.BLOCKED
    assert document.blocks == []
    assert "配置非法" in document.blocked_reason


def test_dwg_keep_artifacts_exposes_converted_dxf(tmp_path: Path) -> None:
    import shutil

    sample = fake_dwg(tmp_path)
    adapter = DwgConverterAdapter(command=make_converter(tmp_path), keep_artifacts=True)
    document = adapter.parse(sample)

    assert document.status is ParseStatus.PARSED
    artifact = Path(document.metadata["conversion"]["artifact_path"])
    try:
        assert artifact.is_file()
        assert artifact.suffix == ".dxf"
        assert sha256_file(artifact) == document.metadata["conversion"]["converted_sha256"]
    finally:
        shutil.rmtree(artifact.parent, ignore_errors=True)


def test_supported_extensions_cover_all_required_formats() -> None:
    extensions = set(supported_extensions())
    assert {"xlsx", "pdf", "docx", "dxf", "dwg"} <= extensions
    assert normalise_suffix(Path("A.DWG")) == "dwg"


def test_corrupt_xlsx_is_failed_not_raised(tmp_path: Path) -> None:
    broken = tmp_path / "broken.xlsx"
    broken.write_bytes(b"not a real workbook")

    document = parse_xlsx(broken)

    assert document.status is ParseStatus.FAILED
    assert document.blocks == []
    assert document.error
