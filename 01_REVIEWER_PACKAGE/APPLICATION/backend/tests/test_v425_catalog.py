"""Governance invariants; all generated fixtures remain in temporary test paths."""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from txbuddy.governance.catalog import PROJECT_PATHS, get_project, load_projects, load_sources, project_id_for_path


@pytest.fixture
def audit_module():
    location = Path(__file__).resolve().parents[2] / "scripts/v425_corpus_governance.py"
    spec = importlib.util.spec_from_file_location("v425_catalog_audit_test", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stable_acl_ids_cover_all_18_without_paths():
    assert len(PROJECT_PATHS) == len(set(PROJECT_PATHS)) == 18
    for index, path in enumerate(PROJECT_PATHS, 1):
        expected = f"CORPUS-S{index:02d}"
        assert project_id_for_path("05_REAL_PROJECTS/" + path + "/原件.pdf") == expected
        assert project_id_for_path(path + "/A_INPUT/原件.pdf") == expected
        assert project_id_for_path("F:\\corpus\\05_REAL_PROJECTS\\" + path.replace("/", "\\") + "\\原件.pdf") == expected
        assert "/" not in expected and "\\" not in expected


@pytest.mark.parametrize("path", [
    "02_HENAN_QUOTA/ADJUSTMENTS/通知.pdf",
    "05_REAL_PROJECTS/L1_BUDGET/L1_001_OTHER/原件.pdf",
    "05_REAL_PROJECTS/L1_BUDGET/_CANDIDATES/其他项目/清单.xlsx",
    "05_REAL_PROJECTS/L5_TENDER/_CANDIDATE_西峡城区第一小学/投标.pdf",
])
def test_candidates_and_regulations_not_silently_joined_to_registered_projects(path):
    assert project_id_for_path(path) == "CORPUS_GOVERNANCE"


@pytest.mark.parametrize("path", ["../05_REAL_PROJECTS/L1_BUDGET/L1_001/a.pdf", "05_REAL_PROJECTS/L1_BUDGET/L1_001/../b.pdf", "原件\x00.pdf"])
def test_invalid_catalogue_path_rejected(path):
    with pytest.raises(ValueError):
        project_id_for_path(path)


def test_catalogue_read_is_read_only_and_does_not_claim_professional_acceptance(tmp_path):
    projects = {"schema_version": "1.0", "projects": [{"project_id": "CORPUS-S01", "l1_quantity_status": "BLOCKED", "production_accepted": False}]}
    sources = {"schema_version": "1.0", "sources": [{"source_id": "SRC-0001", "authorization_status": "UNVERIFIED"}]}
    for name, value in (("PROJECT_READINESS.json", projects), ("SOURCE_STATUS.json", sources)):
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}
    assert load_projects(tmp_path) == projects
    assert load_sources(tmp_path) == sources
    assert get_project("CORPUS-S01", tmp_path)["l1_quantity_status"] == "BLOCKED"
    assert get_project("CORPUS-S99", tmp_path) is None
    assert before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}


@pytest.mark.parametrize("payload", [
    {"schema_version": "99", "sources": []},
    {"schema_version": "1.0", "sources": {}},
    {"schema_version": "1.0", "sources": [{"source_id": "SRC-1"}, {"source_id": "SRC-1"}]},
])
def test_invalid_or_ambiguous_source_catalogue_fails_closed(tmp_path, payload):
    (tmp_path / "SOURCE_STATUS.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_sources(tmp_path)


def test_reference_boq_and_other_project_carrier_do_not_become_independent_inputs(audit_module):
    role, kinds = audit_module.classify_file("05_REAL_PROJECTS/L1_BUDGET/L1_001/B_REFERENCE/土建_工程量清单.xlsx")
    assert role == "B_REFERENCE" and "boq" in kinds
    role, kinds = audit_module.classify_file("05_REAL_PROJECTS/L1_BUDGET/L1_001/A_INPUT/agentA2_旁证_他项目投标业绩合同.pdf")
    assert role == "B_REFERENCE" and "contract" in kinds
    role, kinds = audit_module.classify_file("05_REAL_PROJECTS/L1_BUDGET/L1_001/A_INPUT/中标公告附件_招标文件1.doc")
    assert role == "A_INPUT" and "tender" in kinds


def test_whole_corpus_scan_includes_candidates_and_hashes_raw_bytes_without_mutation(tmp_path, audit_module):
    entries = {"00_INDEX/登记.txt": b"TEST_ONLY-fixture", "05_REAL_PROJECTS/L1_BUDGET/_CANDIDATES/线索/a.pdf": b"TEST_ONLY-not-a-pdf"}
    for name, raw in entries.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    records = audit_module.scan_corpus(tmp_path)
    assert len(records) == 2
    assert {row["path"] for row in records} == set(entries)
    for row in records:
        assert row["sha256"] == hashlib.sha256(entries[row["path"]]).hexdigest()
        assert (tmp_path / row["path"]).read_bytes() == entries[row["path"]]


def test_material_and_quota_missing_cannot_be_hidden_by_published_reference(tmp_path, audit_module):
    index = tmp_path / "00_INDEX"
    index.mkdir()
    with (index / "PROJECT_SAMPLE_MATRIX.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["project_id", "sample_id", "business_line"])
        writer.writeheader()
        for number, path in enumerate(PROJECT_PATHS, 1):
            writer.writerow({"project_id": path, "sample_id": f"S{number:02d}", "business_line": "TEST_ONLY"})
    raw = {"path": "05_REAL_PROJECTS/L1_BUDGET/L1_001/B_REFERENCE/土建_工程量清单.xlsx", "sha256": "0" * 64, "size": 1, "format": "xlsx"}
    source = {"local_path": raw["path"], "source_id": "TEST_ONLY_SOURCE", "source_url": None,
              "source_kind": "ORIGINAL", "verification_status": "UNVERIFIED", "authorization_status": "UNVERIFIED", "hash_match": True}
    result = audit_module.build_projects(tmp_path, [raw], {"sources": [source]})
    assert len(result["projects"]) == 18 and result["summary"]["l1_ready"] == 0
    first = result["projects"][0]
    assert first["b_reference_count"] == 1 and first["a_input_count"] == 0
    assert first["documents"]["drawings"] == []
    assert first["document_status"]["material_price_basis"]["status"] == "MISSING"
    assert first["document_status"]["quota_basis"]["status"] == "MISSING"
    assert first["l1_quantity_status"] == "BLOCKED" and first["independent_goldenset"] is False
    assert all(row["production_accepted"] is False for row in result["projects"])


def test_changed_original_project_register_requires_explicit_review(tmp_path, audit_module):
    path = tmp_path / "00_INDEX/PROJECT_SAMPLE_MATRIX.csv"
    path.parent.mkdir()
    path.write_text("project_id,sample_id,business_line\nOTHER,S01,TEST_ONLY\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Original sample register changed"):
        audit_module.build_projects(tmp_path, [], {"sources": []})


def test_parsed_archive_cannot_fill_missing_drawings_or_create_a_signed_contract(tmp_path, audit_module):
    path = tmp_path / "00_INDEX/PROJECT_SAMPLE_MATRIX.csv"
    path.parent.mkdir()
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=["project_id", "sample_id", "business_line"])
        writer.writeheader()
        for number, project in enumerate(PROJECT_PATHS, 1):
            writer.writerow({"project_id": project, "sample_id": f"S{number:02d}", "business_line": "TEST_ONLY"})
    archive_path = "05_REAL_PROJECTS/L5_TENDER/新密市助泉寺村民宿建设/招标文件.rar"
    original = {"path": archive_path, "sha256": "a" * 64, "size": 20, "format": "rar"}
    source = {"local_path": archive_path, "source_id": "TEST_ONLY_ARCHIVE", "source_url": None,
              "source_kind": "ORIGINAL", "verification_status": "UNVERIFIED", "authorization_status": "UNVERIFIED", "hash_match": True}
    derived = tmp_path / "TEST_ONLY_DERIVED.pdf"
    derived.write_bytes(b"TEST_ONLY_ARCHIVE_MEMBER_FIXTURE")
    member = {"name": "招标文件/施工标段磋商文件.pdf", "derived_path": str(derived),
              "sha256": hashlib.sha256(derived.read_bytes()).hexdigest(), "size_bytes": derived.stat().st_size,
              "format": "pdf", "status": "PARSED", "parse_summary": {"pages": 1}}
    parser = {"records": [{"path": archive_path, "sha256": "a" * 64, "status": "PARSED", "content_parsed": True, "members": [member]}]}
    result = audit_module.build_projects(tmp_path, [original], {"sources": [source]}, parser)
    project = result["projects"][12]
    assert project["file_count"] == 1 and project["derived_document_count"] == 1
    assert project["document_status"]["tender"]["status"] == "ARCHIVE_PARSED_UNREVIEWED"
    assert len(project["documents"]["tender"]) == 1
    assert project["documents"]["drawings"] == project["documents"]["boq"] == project["documents"]["contract"] == []
    assert project["l1_quantity_status"] == "BLOCKED" and project["production_accepted"] is False
    # A changed derived file must fail, even when the report still says PARSED.
    derived.write_bytes(b"TEST_ONLY_TAMPERED")
    with pytest.raises(ValueError, match="Archive child no longer matches"):
        audit_module.build_projects(tmp_path, [original], {"sources": [source]}, parser)
