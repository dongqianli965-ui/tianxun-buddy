"""Read-only V4.2.5 catalogue reports. Authorization belongs to the API caller.

Identifiers are stable aliases of the original sample register, never filesystem
paths. A source URL proves neither permission to reuse a file nor professional
acceptance of its contents.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath


PROJECT_PATHS = (
    "L1_BUDGET/L1_001",
    "L1_BUDGET/L1_002",
    "L1_BUDGET/L1_003",
    "L2_REVIEW/河南省地下水监测站建设",
    "L2_REVIEW/河南省绿城监狱安防升级改造",
    "L2_REVIEW/河南科技大学开元会议中心改造",
    "L3_SETTLEMENT/内乡县产业路建设及修复",
    "L3_SETTLEMENT/安阳中院中央空调管道更换改造",
    "L3_SETTLEMENT/平顶山石龙区排水防涝管网新建",
    "L4_SETTLEMENT_REVIEW/焦作职校外墙真石漆修缮",
    "L4_SETTLEMENT_REVIEW/许昌示范区造价咨询审计服务",
    "L4_SETTLEMENT_REVIEW/郑州疾控门诊部改造",
    "L5_TENDER/新密市助泉寺村民宿建设",
    "L5_TENDER/河南大学金明校区学生宿舍楼",
    "L5_TENDER/河南科技学院东校区学生宿舍D项目",
    "L6_FORENSIC/周口市第一人民医院产科病房改造",
    "L6_FORENSIC/民权县省道公路安全设施提升",
    "L6_FORENSIC/灵宝市苏村乡公路改建",
)


def project_id_for_path(local_path: str) -> str:
    text = local_path.replace("\\", "/")
    if "\x00" in text or ".." in PurePosixPath(text).parts:
        raise ValueError("Invalid corpus path")
    # Accept both a corpus-relative path and a local absolute corpus path.
    marker = "05_REAL_PROJECTS/"
    tail = text.split(marker, 1)[1] if marker in text else text
    for index, prefix in enumerate(PROJECT_PATHS, 1):
        if tail == prefix or tail.startswith(prefix + "/"):
            return f"CORPUS-S{index:02d}"
    return "CORPUS_GOVERNANCE"


def _load(name: str, collection: str, report_dir: str | Path | None) -> dict:
    root = Path(report_dir) if report_dir is not None else Path(__file__).resolve().parents[3] / "reports/v425"
    value = json.loads((root / name).read_text(encoding="utf-8-sig"))
    if value.get("schema_version") != "1.0" or not isinstance(value.get(collection), list):
        raise ValueError("Unsupported governance catalogue schema")
    key = "project_id" if collection == "projects" else "source_id"
    ids = [row[key] for row in value[collection]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate catalogue identifier")
    return value


def load_projects(report_dir: str | Path | None = None) -> dict:
    return _load("PROJECT_READINESS.json", "projects", report_dir)


def load_sources(report_dir: str | Path | None = None) -> dict:
    return _load("SOURCE_STATUS.json", "sources", report_dir)


def get_project(project_id: str, report_dir: str | Path | None = None) -> dict | None:
    return next((row for row in load_projects(report_dir)["projects"] if row["project_id"] == project_id), None)
