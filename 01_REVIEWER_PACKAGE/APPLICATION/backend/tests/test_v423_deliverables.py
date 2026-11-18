"""P1-10-DELIVER 验收：十类成果逐个生成并用 openpyxl / pypdf 重新打开校验。

覆盖 TASK.md 的硬要求：
1. 十类成果各有生成器，落到 data/deliverables/<job_id>/，文件名固定且可重新打开；
2. XLSX 用 openpyxl.load_workbook 重开，金额单元格是数值；
3. PDF 校验 %PDF 头、%%EOF 尾与 pypdf 页数，正文含可提取的 ASCII 标题；
4. 证据索引逐条反查到 evidence 记录 id，未知证据拒绝出件；
5. 清单 manifest 的 SHA-256 与磁盘文件一致。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from openpyxl import load_workbook
from pypdf import PdfReader

from txbuddy.core.evidence import build_evidence
from txbuddy.deliverables import (DeliverableDataset, DeliverableError, DeliverableService,
                                  SPECS, sha256_file)
from txbuddy.deliverables.model import EvidenceRow
from txbuddy.store import EvidenceStore

PACK = {
    "basis_pack_id": "B-P110",
    "region_code": "410100",
    "region_level": "CITY",
    "major": "BUILDING",
    "effective_date": "2026-01-01",
    "measurement_standard_version": "GB/T 50854-2024",
    "pricing_standard_version": "GB/T 50500-2024",
    "rule_version_scope": "HENAN_2025",
    "precision_rule_version": "P-V1",
    "status": "APPROVED",
    "test_only": True,
}

JOB_ID = "job-p110-demo"


@pytest.fixture()
def store(tmp_path: Path) -> EvidenceStore:
    db = EvidenceStore(str(tmp_path / "p110.sqlite3"))
    db.create_job(job_id=JOB_ID, project_id="P-P110", name="某中学教学楼改造工程",
                  business_line="L1", basis_pack=PACK, created_by="tester")
    evidence_ids = []
    for index, (name, subject_type, subject_id, result) in enumerate([
        ("清单-土建.txt", "QUANTITY", "A1", "120.500"),
        ("合同-调差条款.txt", "CONTRACT", "C-01", "WITHIN_BAND_ONLY"),
        ("审核-差异说明.txt", "AUDIT", "A2", "-2749.13"),
    ], start=1):
        path = tmp_path / name
        path.write_text(f"清单第{index}行\n证据文件 {name}\n", encoding="utf-8")
        record = build_evidence(
            job_id=JOB_ID,
            subject_type=subject_type, subject_id=subject_id, result=result,
            input_snapshot={"line": index}, basis_pack_id="B-P110",
            rule_version="R-1", precision_rule_version="P-V1",
            file_path=path, locator={"page": 1, "row": index})
        db.save(record)
        evidence_ids.append(record["evidence_id"])
    db.put_record(JOB_ID, "boq", {"items": [
        {"item_code": "A1", "name": "土方开挖", "unit": "m3", "quantity": "120.5",
         "unit_price": "45.6", "section": "土建"},
        {"item_code": "A2", "name": "混凝土基础", "unit": "m3", "quantity": "88.25",
         "unit_price": "620.5", "section": "土建"},
        {"item_code": "A3", "name": "钢筋制安", "unit": "t", "quantity": "12.6",
         "unit_price": "4850", "section": "土建"},
    ]})
    db.put_record(JOB_ID, "quantity_calc", {"items": [
        {"item_code": "A1", "name": "土方开挖", "formula": "长×宽×深 = 12.5×4.2×2.3",
         "unit": "m3", "quantity": "120.5", "rule_code": "Q-001", "rule_version": "R-1",
         "standard_ref": "GB/T 50854-2024 4.2", "note": "放坡系数 1:0.33"},
        {"item_code": "A2", "name": "混凝土基础", "formula": "轴线面积×厚度 = 35.3×2.5",
         "unit": "m3", "quantity": "88.25", "rule_code": "Q-014", "rule_version": "R-1",
         "standard_ref": "GB/T 50854-2024 5.3", "note": ""},
    ]})
    db.put_record(JOB_ID, "quota_match", {"items": [
        {"item_code": "A1", "name": "土方开挖", "quota_code": "1-1-12", "quota_name": "人工挖土方",
         "unit": "100m3", "quantity": "1.205", "labor": "3200.00", "material": "0",
         "machine": "480.00"},
        {"item_code": "A2", "name": "混凝土基础", "quota_code": "5-2-8", "quota_name": "现浇混凝土基础",
         "unit": "10m3", "quantity": "8.825", "labor": "5600.00", "material": "38500.00",
         "machine": "2400.00"},
    ]})
    db.put_record(JOB_ID, "unit_price", {"items": [
        {"item_code": "A1", "name": "土方开挖", "unit": "m3", "quantity": "120.5",
         "labor": "12.0", "material": "1.5", "machine": "4.0", "management": "1.75",
         "profit": "1.2", "risk_fee": "0.5", "management_source_ref": "依据包-管理费",
         "profit_source_ref": "依据包-利润", "resource_codes": ["R-LAB-01", "R-MAC-01"]},
        {"item_code": "A2", "name": "混凝土基础", "unit": "m3", "quantity": "88.25",
         "labor": "80.0", "material": "480.0", "machine": "35.0", "management": "59.5",
         "profit": "40.0", "risk_fee": "0", "management_source_ref": "依据包-管理费",
         "profit_source_ref": "依据包-利润", "resource_codes": ["R-LAB-01", "R-MAT-01"]},
    ]})
    db.put_record(JOB_ID, "resources", {"items": [
        {"resource_type": "LABOR", "resource_code": "R-LAB-01", "name": "综合工日", "unit": "工日",
         "quantity": "260", "unit_price": "180", "price_ref": "信息价2026-01", "item_code": "A1"},
        {"resource_type": "MATERIAL", "resource_code": "R-MAT-01", "name": "C30 混凝土", "unit": "m3",
         "quantity": "92", "unit_price": "520", "price_ref": "信息价2026-01", "item_code": "A2"},
        {"resource_type": "MATERIAL", "resource_code": "R-MAT-02", "name": "钢筋 HRB400", "unit": "t",
         "quantity": "13.2", "unit_price": "4600", "price_ref": "信息价2026-01", "item_code": "A3"},
        {"resource_type": "MACHINE", "resource_code": "R-MAC-01", "name": "挖掘机", "unit": "台班",
         "quantity": "6", "unit_price": "1200", "price_ref": "租赁合同", "item_code": "A1"},
    ]})
    db.put_record(JOB_ID, "fees", {
        "measure_total": "15000", "other_total": "8000", "tax_rate": "0.09",
        "remark": "按河南省 2025 计价依据包编制",
        "items": [
            {"fee_item": "安全文明施工费", "base_def": "BOQ_TOTAL", "base_amount": "121363.93",
             "rate": "0.032", "amount": "3883.65", "source_ref": "豫建价〔2025〕1号",
             "noncompetitive": True},
            {"fee_item": "总承包服务费", "base_def": "BOQ_PLUS_MEASURES", "base_amount": "136353.93",
             "rate": "0", "amount": "5000.00", "source_ref": "合同专用条款 12.3",
             "noncompetitive": False},
        ],
    })
    db.put_record(JOB_ID, "index_adjustment", {"items": [
        {"resource_code": "R-MAT-02", "name": "钢筋 HRB400", "base_amount": "61110.00",
         "base_index": "100", "current_index": "106.5", "risk_rate": "0.05",
         "mode": "WITHIN_BAND_ONLY", "delta_amount": "916.65",
         "contract_clause_ref": "合同 13.2 条"},
        {"resource_code": "R-MAT-01", "name": "C30 混凝土", "base_amount": "54759.13",
         "base_index": "100", "current_index": "92", "risk_rate": "0.05",
         "mode": "FULL_EXCESS", "delta_amount": "-4380.73",
         "contract_clause_ref": "合同 13.3 条"},
    ]})
    db.put_record(JOB_ID, "audit_diff", {"items": [
        {"item_code": "A2", "name": "混凝土基础", "unit": "m3", "submitted_qty": "88.25",
         "audited_qty": "83.8", "submitted_amount": "54759.13", "audited_amount": "52000.00",
         "reason": "工程量按实核减", "evidence_id": evidence_ids[2]},
        {"item_code": "A3", "name": "钢筋制安", "unit": "t", "submitted_qty": "12.6",
         "audited_qty": "12.78", "submitted_amount": "61110.00", "audited_amount": "62000.00",
         "reason": "钢材价格调整", "evidence_id": evidence_ids[1]},
    ]})
    return db


@pytest.fixture()
def dataset(store: EvidenceStore) -> DeliverableDataset:
    return DeliverableDataset.from_store(
        store, JOB_ID, prepared_by="tester",
        prepared_at=datetime(2026, 11, 16, 9, 30, tzinfo=timezone.utc))


@pytest.fixture()
def service(tmp_path: Path, store: EvidenceStore) -> DeliverableService:
    return DeliverableService(tmp_path / "deliverables", store.get)


def find_row(ws, text: str, column: int = 1) -> int:
    for row in range(1, ws.max_row + 1):
        if ws.cell(row=row, column=column).value == text:
            return row
    raise AssertionError(f"工作表中找不到 {text!r}")


def test_dataset_from_store_reads_records_and_evidence(dataset, store):
    assert dataset.job_id == JOB_ID
    assert dataset.project_name == "某中学教学楼改造工程"
    assert len(dataset.boq) == 3
    assert len(dataset.quantities) == 2
    assert len(dataset.quotas) == 2
    assert len(dataset.unit_prices) == 2
    assert len(dataset.resources) == 4
    assert len(dataset.fees) == 2
    assert len(dataset.adjustments) == 2
    assert len(dataset.audit_diffs) == 2
    assert len(dataset.evidence) == 3
    assert {row.evidence_id for row in dataset.evidence} == {
        row["evidence_id"] for row in store.list_evidence(JOB_ID)}
    totals = dataset.totals()
    assert totals["boq_total"] == Decimal("121363.93")
    assert totals["fee_total"] == Decimal("8883.65")
    assert totals["tax_base"] == Decimal("153247.58")
    assert totals["tax_amount"] == Decimal("13792.28")
    assert totals["project_total"] == Decimal("167039.86")


def test_budget_xlsx_reopens_with_real_sheets_and_numeric_totals(dataset, service):
    item = service.generate(dataset, "budget_xlsx")
    assert item.path.name == "01_预算书.xlsx"
    wb = load_workbook(item.path)
    assert wb.sheetnames == ["封面", "总预算表", "单位工程预算表"]
    cover = wb["封面"]
    assert find_row(cover, "某中学教学楼改造工程", column=2)
    detail = wb["单位工程预算表"]
    row = find_row(detail, "A2", column=2)
    assert detail.cell(row=row, column=3).value == "混凝土基础"
    assert isinstance(detail.cell(row=row, column=7).value, (int, float))
    assert detail.cell(row=row, column=7).value == pytest.approx(54759.13)
    total_row = find_row(detail, "合计")
    assert detail.cell(row=total_row, column=7).value == pytest.approx(121363.93)
    summary = wb["总预算表"]
    total_row = find_row(summary, "含税工程造价", column=2)
    assert summary.cell(row=total_row, column=4).value == pytest.approx(167039.86)


def test_budget_pdf_reopens_with_header_pages_and_text(dataset, service):
    item = service.generate(dataset, "budget_pdf")
    raw = item.path.read_bytes()
    assert raw.startswith(b"%PDF-1.4")
    assert raw.rstrip().endswith(b"%%EOF")
    reader = PdfReader(str(item.path))
    assert len(reader.pages) >= 1
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert "BUDGET BOOK" in text
    assert JOB_ID in text
    assert f"Page 1 of {len(reader.pages)}" in text
    assert item.details["pages"] == len(reader.pages)


@pytest.mark.parametrize("kind,sheet_name", [
    ("quantity_calc", "工程量计算书"),
    ("quota_detail", "定额套用明细"),
    ("unit_price", "综合单价分析"),
    ("resource_summary", "人材机汇总"),
    ("fee_calculation", "费用计算表"),
    ("index_adjustment", "调差表"),
    ("audit_difference", "审核差异表"),
    ("evidence_index", "证据索引"),
])
def test_each_xlsx_generator_writes_reopenable_workbook(dataset, service, kind, sheet_name):
    spec = next(spec for spec in SPECS if spec.kind == kind)
    item = service.generate(dataset, kind)
    assert item.path.name == spec.file_name
    assert item.path.exists() and item.size > 0
    wb = load_workbook(item.path)
    assert sheet_name in wb.sheetnames
    assert wb[sheet_name].max_row >= 2
    assert wb[sheet_name].max_column >= 2


def test_registry_has_exactly_ten_distinct_real_files():
    assert len(SPECS) == 10
    assert [spec.kind for spec in SPECS] == [
        "budget_xlsx", "budget_pdf", "quantity_calc", "quota_detail", "unit_price",
        "resource_summary", "fee_calculation", "index_adjustment", "audit_difference",
        "evidence_index"]
    assert len({spec.kind for spec in SPECS}) == 10
    assert len({spec.file_name for spec in SPECS}) == 10
    assert sum(spec.file_name.endswith(".xlsx") for spec in SPECS) == 9
    assert [spec.file_name for spec in SPECS if spec.file_name.endswith(".pdf")] == ["02_预算书.pdf"]


def test_generate_all_writes_ten_files_and_a_verifiable_manifest(dataset, service, store):
    manifest = service.generate_all(dataset)
    assert manifest.job_id == JOB_ID
    assert manifest.output_dir == service.output_root / JOB_ID
    assert len(manifest.files) == 10
    on_disk = sorted(path.name for path in manifest.output_dir.iterdir()
                     if path.is_file() and path.name != "manifest.json")
    assert on_disk == sorted(spec.file_name for spec in SPECS)
    for item in manifest.files:
        assert item.path.is_file()
        assert item.size == item.path.stat().st_size
        assert item.sha256 == sha256_file(item.path)
    assert manifest.manifest_path.is_file()
    body = json.loads(manifest.manifest_path.read_text(encoding="utf-8"))
    assert body["schema"] == "deliverable_manifest/1"
    assert body["count"] == 10
    assert body["job_id"] == JOB_ID
    assert {entry["kind"] for entry in body["files"]} == {spec.kind for spec in SPECS}
    assert hashlib.sha256(manifest.manifest_path.read_bytes()).hexdigest() == manifest.manifest_sha256
    assert manifest.by_kind("evidence_index").file_name == "10_证据索引.xlsx"
    with pytest.raises(DeliverableError) as exc:
        manifest.by_kind("no-such-kind")
    assert exc.value.code == "DELIVERABLE_KIND_UNKNOWN"


def test_default_output_root_is_data_deliverables(tmp_path, dataset, monkeypatch):
    service = DeliverableService()
    assert service.output_dir(JOB_ID) == Path("data/deliverables") / JOB_ID
    monkeypatch.chdir(tmp_path)
    item = service.generate(dataset, "fee_calculation")
    expected = tmp_path / "data" / "deliverables" / JOB_ID / "07_费用计算表.xlsx"
    assert item.path == Path("data/deliverables") / JOB_ID / "07_费用计算表.xlsx"
    assert item.path.resolve() == expected.resolve()
    assert expected.is_file()
    load_workbook(expected)


def test_evidence_index_resolves_every_evidence_record(dataset, service, store):
    item = service.generate(dataset, "evidence_index")
    wb = load_workbook(item.path)
    ws = wb["证据索引"]
    values = {ws.cell(row=row, column=2).value for row in range(1, ws.max_row + 1)}
    file_names = {ws.cell(row=row, column=5).value for row in range(1, ws.max_row + 1)}
    statuses = [ws.cell(row=row, column=9).value for row in range(1, ws.max_row + 1)]
    assert "清单-土建.txt" in file_names
    assert "合同-调差条款.txt" in file_names
    for evidence in dataset.evidence:
        assert evidence.evidence_id in values
        record = store.get(evidence.evidence_id)
        assert record is not None
        assert record["evidence_id"] == evidence.evidence_id
        assert record["sha256"]
    assert statuses.count("FOUND") == len(dataset.evidence)
    guide = wb["反查说明"]
    assert find_row(guide, "已反查条数", column=1)
    row = find_row(guide, "已反查条数", column=1)
    assert guide.cell(row=row, column=2).value == len(dataset.evidence)
    assert item.details["found"] == len(dataset.evidence)
    assert item.details["index_sha256"] == hashlib.sha256(
        "\n".join(row.evidence_id for row in dataset.evidence).encode("utf-8")).hexdigest()


def test_unknown_evidence_is_rejected_before_any_file_is_written(dataset, service):
    bad = dataset.model_copy(deep=True)
    bad.evidence = [EvidenceRow(evidence_id="deadbeefdeadbeefdeadbeef")]
    bad.audit_diffs = []
    with pytest.raises(DeliverableError) as exc:
        service.generate_all(bad)
    assert exc.value.code == "EVIDENCE_NOT_FOUND"
    out_dir = service.output_root / JOB_ID
    assert not out_dir.exists() or not list(out_dir.iterdir())


def test_audit_difference_links_submitted_audited_and_evidence(dataset, service):
    item = service.generate(dataset, "audit_difference")
    wb = load_workbook(item.path)
    ws = wb["审核差异表"]
    evidence_ids = set()
    for row in range(1, ws.max_row + 1):
        value = ws.cell(row=row, column=11).value
        if value and re.fullmatch(r"[0-9a-f]{24}", str(value)):
            evidence_ids.add(value)
    assert evidence_ids == {row.evidence_id for row in dataset.audit_diffs}
    total_row = find_row(ws, "合计")
    assert ws.cell(row=total_row, column=7).value == pytest.approx(115869.13)
    assert ws.cell(row=total_row, column=8).value == pytest.approx(114000.00)
    assert ws.cell(row=total_row, column=9).value == pytest.approx(-1869.13)
    assert item.details["delta"] == "-1869.13"
    briefs = [ws.cell(row=row, column=12).value for row in range(1, ws.max_row + 1)]
    assert any("subject_type=" in str(value) for value in briefs if value)


def test_pdf_paginates_long_budget_and_repeats_page_numbers(dataset, service):
    rows = [dataset.boq[0].model_copy(update={"item_code": f"B{index:03d}",
                                              "name": f"清单项目 {index}"})
            for index in range(1, 81)]
    many = dataset.model_copy(update={"boq": rows})
    item = service.generate(many, "budget_pdf")
    reader = PdfReader(str(item.path))
    assert len(reader.pages) >= 2
    for page_no, page in enumerate(reader.pages, start=1):
        assert f"Page {page_no} of {len(reader.pages)}" in (page.extract_text() or "")
    assert item.details["pages"] == len(reader.pages)
    assert item.details["boq_rows"] == 80


def test_output_directory_rejects_path_traversal(service):
    for bad in ("../escape", "a/b", "a\\b", "..", ".", "C:evil", "trailing "):
        with pytest.raises(DeliverableError) as exc:
            service.output_dir(bad)
        assert exc.value.code == "OUTPUT_DIR_INVALID"

