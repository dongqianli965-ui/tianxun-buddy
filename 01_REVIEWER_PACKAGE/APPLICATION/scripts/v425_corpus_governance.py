"""Re-scan the read-only corpus and audit all source relationships for V4.2.5.

Writes only a separate reports directory and V4.2.5 Markdown reports. Does not
update the original register, source files, SQLite or PostgreSQL.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from txbuddy.governance.catalog import PROJECT_PATHS, project_id_for_path

FOCUS = ("SRC-0132", "SRC-0137", "SRC-0155", "SRC-0220")
DIRECT_0155 = "https://zfcg.henan.gov.cn/cmsweb35rc67w/nanyang/rootfiles/2026/11/13/b60f483c5677419aacf5705ab4b7af40.pdf"
SHANGHAI = timezone(timedelta(hours=8))


def digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def read_csv(path: Path, delimiter: str = ",") -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file, delimiter=delimiter))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def evidence(path: Path, root: Path, method: str, **facts) -> dict:
    return {"file": str(path.relative_to(root)) if path.is_relative_to(root) else str(path),
            "sha256": digest(path), "method": method, **facts}


def scan_corpus(corpus: Path) -> list[dict]:
    """Enumerate every actual file, including candidates and non-project areas."""
    return [{"path": path.relative_to(corpus).as_posix(), "size": path.stat().st_size,
             "sha256": digest(path), "format": path.suffix.lower().lstrip(".")}
            for path in sorted(corpus.rglob("*")) if path.is_file()]


def validate_excerpt(corpus: Path, parent: dict, child: dict) -> dict:
    import pymupdf
    original = pymupdf.open(corpus / parent["local_path"])
    excerpt = pymupdf.open(corpus / child["local_path"])
    checks = []
    if len(original) == 895 and len(excerpt) == 16:
        for index in range(16):
            a, b = original[121 + index], excerpt[index]
            pa = a.get_pixmap(matrix=pymupdf.Matrix(0.5, 0.5), alpha=False)
            pb = b.get_pixmap(matrix=pymupdf.Matrix(0.5, 0.5), alpha=False)
            ha = hashlib.sha256(pa.samples).hexdigest()
            hb = hashlib.sha256(pb.samples).hexdigest()
            checks.append({"parent_page": 122 + index, "derived_page": index + 1,
                           "parent_render_sha256": ha, "derived_render_sha256": hb,
                           "same_geometry": tuple(a.rect) == tuple(b.rect), "render_equal": ha == hb})
    result = {"method": "PDF_PAGE_RENDER_COMPARISON_0.5_SCALE", "parent_pages": len(original),
              "derived_pages": len(excerpt), "pages": checks,
              "all_16_pages_equivalent": len(checks) == 16 and all(p["same_geometry"] and p["render_equal"] for p in checks),
              "meaning": "仅验证摘录页影像对应关系，不是合同真实性、有效性或专业签认"}
    original.close()
    excerpt.close()
    return result


def validate_reference_workbook(corpus: Path, sources: list[dict]) -> dict:
    import openpyxl
    by_name = {Path(row["local_path"]).name: row for row in sources}
    derived = next(row for row in sources if row["source_id"] == "SRC-0220")
    workbook = openpyxl.load_workbook(corpus / derived["local_path"], read_only=True, data_only=True)
    rows = list(workbook["真实工程量及计价对照"].iter_rows(values_only=True))[1:]
    checks, parents, source_books = [], {}, {}
    for row in rows:
        # The old names are retained inside the derived file. Resolve the exact
        # basename, then compare each recorded sheet/row against its actual file.
        name = str(row[25]).replace("\\", "/").rsplit("/", 1)[-1]
        source = by_name.get(name)
        if source is None:
            checks.append({"derived_row": row[0], "matches": False, "reason": "SOURCE_FILE_MISSING"})
            continue
        parents[source["source_id"]] = {"source_id": source["source_id"], "local_path": source["local_path"], "sha256": source["sha256"]}
        if name not in source_books:
            source_books[name] = openpyxl.load_workbook(corpus / source["local_path"], read_only=True, data_only=True)
        sheet, number = source_books[name][row[26]], int(row[27])
        original = list(sheet.iter_rows(min_row=number, max_row=number, values_only=True))[0]
        matches = (str(row[3]) == str(original[1]) and str(row[4]) == str(original[2])
                   and str(row[6]) == str(original[5]) and Decimal(str(row[7])) == Decimal(str(original[6])))
        checks.append({"derived_row": row[0], "parent_source_id": source["source_id"],
                       "sheet": row[26], "row": number, "matches": matches})
    workbook.close()
    for book in source_books.values():
        book.close()
    return {"method": "XLSX_SOURCE_SHEET_ROW_COMPARISON", "rows": len(rows),
            "matched": sum(r["matches"] for r in checks), "parents": list(parents.values()), "checks": checks,
            "checked_fields": ["code", "name", "unit", "quantity"],
            "independently_calculated": False, "professional_signoff": False,
            "meaning": "核对148行参考清单的派生链；不验证独立工程量或正式组价"}


def audit_sources(corpus: Path, scan: list[dict], *, remote: bool) -> dict:
    original = read_csv(corpus / "00_INDEX/SOURCE_REGISTER.csv")
    current = {row["path"]: row for row in scan}
    logs = corpus.parent / "_logs"
    links = {}
    for row in original:
        url = row.get("official_url") or row.get("download_url")
        if url:
            links[row["sha256"]] = {"url": url, **evidence(corpus / "00_INDEX/SOURCE_REGISTER.csv", corpus, "SOURCE_REGISTER_SHA256")}
    reuse = logs / "reuse_manifest.csv"
    if reuse.exists():
        for row in read_csv(reuse):
            url = row.get("official_url") or row.get("download_url")
            if url:
                links[row["sha256"]] = {"url": url, **evidence(reuse, corpus, "REUSE_MANIFEST_SHA256")}
    downloads = logs / "download_log.jsonl"
    if downloads.exists():
        for line_number, line in enumerate(downloads.read_text(encoding="utf-8-sig").splitlines(), 1):
            row = json.loads(line)
            if row.get("ok") and row.get("sha256"):
                links[row["sha256"]] = {"url": row.get("final_url") or row["url"],
                    **evidence(downloads, corpus, "DOWNLOAD_LOG_SHA256", line=line_number, http_status=row.get("http_status"))}
    collection = corpus / "09_REPORTS/agentD_真实项目采集清单_20261116.tsv"
    # The one absent download-log row was retained in the collector's independent
    # TSV. Require exact relative filename, size and recorded hash prefix.
    with collection.open(encoding="utf-8-sig", newline="") as file:
        records = list(csv.reader(file, delimiter="\t"))
    src0155 = next(row for row in original if row["source_id"] == "SRC-0155")
    collection_match = None
    for line_number, cells in enumerate(records, 1):
        if len(cells) >= 5 and cells[0] == src0155["local_path"] and cells[4] == "OK":
            actual = current[cells[0]]
            if int(cells[2]) == actual["size"] and actual["sha256"].startswith(cells[3]) and len(cells[3]) >= 16:
                collection_match = {"url": cells[1], **evidence(collection, corpus, "COLLECTOR_TSV_PATH_SIZE_SHA_PREFIX", line=line_number)}
                links[actual["sha256"]] = collection_match
    remote_check = {"attempted": False, "verified": False}
    if remote and collection_match:
        remote_check = {"attempted": True, "source_id": "SRC-0155", "url": collection_match["url"], "verified": False}
        try:
            request = urllib.request.Request(collection_match["url"], headers={"User-Agent": "TianxunBuddy-SourceAudit/4.2.5"})
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read(5 * 1024 * 1024 + 1)
                remote_check.update(http_status=response.status, final_url=response.url, content_type=response.headers.get("Content-Type"),
                                    bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            remote_check["verified"] = (remote_check["http_status"] == 200 and raw.startswith(b"%PDF-")
                                          and len(raw) == current[src0155["local_path"]]["size"]
                                          and remote_check["sha256"] == src0155["sha256"])
        except Exception as exc:
            remote_check["error"] = type(exc).__name__ + ": " + str(exc)
    rows = []
    for row in original:
        actual = current.get(row["local_path"])
        link = links.get(row["sha256"])
        local_derived = row["source_id"] in ("SRC-0132", "SRC-0137", "SRC-0220")
        url = None if local_derived else (link["url"] if link else None)
        status = row["verification_status"]
        limitations = []
        if row["license_status"] in ("PUBLIC_OFFICIAL", "PUBLIC", "OFFICIAL_VERIFIED"):
            authorization = "PUBLIC_ACCESS_REUSE_RIGHTS_NOT_CONFIRMED"
            limitations.append("公开可下载不等于获得数据库存储、再分发及正式业务使用授权")
        else:
            authorization = row["license_status"] or "UNVERIFIED"
        if local_derived:
            limitations.append("本地派生文件无独立官方URL；仅登记父来源关系，保留INCOMPLETE")
        elif not url:
            limitations.append("未找到可证实的直接来源URL")
        if row["source_id"] == "SRC-0155":
            status = "OFFICIAL_BYTES_VERIFIED" if remote_check.get("verified") else "COLLECTOR_LINK_CONFIRMED_REMOTE_UNVERIFIED"
            limitations.append("官方文件字节与原件一致，不改变原UNVERIFIED授权状态")
        if "METADATA_ONLY" in status or "标准" in row.get("major", ""):
            limitations.append("元数据/发布公告不替代正版标准正文及计算规则")
        if not actual or actual["sha256"] != row["sha256"]:
            status = "HASH_MISMATCH"
        rows.append({"source_id": row["source_id"], "title": row["title"], "local_path": row["local_path"],
                     "project_id": project_id_for_path(row["local_path"]), "sha256": row["sha256"],
                     "original_sha256": row["sha256"], "actual_sha256": actual["sha256"] if actual else None,
                     "hash_match": bool(actual and actual["sha256"] == row["sha256"]), "size": actual["size"] if actual else None,
                     "source_url": url, "official_url": url, "registered_url": row.get("official_url") or None,
                     "original_verification_status": row["verification_status"], "verification_status": status,
                     "license_status": row["license_status"], "authorization_status": authorization,
                     "source_kind": "LOCAL_DERIVED" if local_derived else "ORIGINAL",
                     "provenance_evidence": [link] if link and not local_derived else [], "parent_sources": [],
                     "limitations": limitations, "audit_status": "AUDITED", "professional_signoff": False})
    by_id = {row["source_id"]: row for row in rows}
    parent = next(row for row in rows if row["sha256"] == "ebb760b7efbe793bef39598787cc5aa1a0e862e10e03ea6da812e80947396956")
    excerpt_check = validate_excerpt(corpus, parent, by_id["SRC-0137"])
    parent_ref = {key: parent[key] for key in ("source_id", "local_path", "sha256", "source_url")}
    parent_ref.update(relation="PUBLIC_BID_DOCUMENT_CARRIER", physical_pages=[122, 137])
    for identifier in ("SRC-0132", "SRC-0137"):
        by_id[identifier]["parent_sources"] = [parent_ref]
        by_id[identifier]["provenance_evidence"] = [evidence(corpus / by_id["SRC-0132"]["local_path"], corpus,
            "LOCAL_DERIVATION_NOTE", parent_sha256=parent["sha256"], physical_pages=[122, 137])]
        by_id[identifier]["derivation_status"] = "PAGE_RELATION_VERIFIED" if excerpt_check["all_16_pages_equivalent"] else "DERIVATION_UNVERIFIED"
        by_id[identifier]["authorization_status"] = "DERIVED_INTERNAL_ONLY_NO_ENTERPRISE_AUTHORIZATION"
        by_id[identifier]["limitations"].append("载体属于另一项目的投标业绩旁证，不能替代主项目合同原件；含个人信息，不得公开分发")
    reference_check = validate_reference_workbook(corpus, rows)
    by_id["SRC-0220"]["parent_sources"] = [{**ref, "source_url": by_id[ref["source_id"]]["source_url"], "relation": "PUBLISHED_BOQ_REFERENCE"} for ref in reference_check["parents"]]
    by_id["SRC-0220"]["derivation_status"] = "148_REFERENCE_ROWS_TRACEABLE" if reference_check["matched"] == reference_check["rows"] == 148 else "DERIVATION_UNVERIFIED"
    by_id["SRC-0220"]["authorization_status"] = "DERIVED_REFERENCE_ONLY_REUSE_REQUIRES_REVIEW"
    by_id["SRC-0220"]["limitations"].append("148行来自已发布招标清单，未独立算量；无专业人员独立复核或签认")
    if remote_check.get("attempted"):
        by_id["SRC-0155"]["provenance_evidence"].append({"method": "BOUNDED_HTTP_BYTE_HASH_COMPARISON", **remote_check})
    return {"schema_version": "1.0", "generated_at": datetime.now(SHANGHAI).isoformat(),
            "summary": {"total": len(rows), "audited": len(rows), "hash_matched": sum(r["hash_match"] for r in rows),
                        "direct_url_count": sum(bool(r["source_url"]) for r in rows), "local_derived_without_direct_url": sum(r["source_kind"] == "LOCAL_DERIVED" and not r["source_url"] for r in rows),
                        "all_sources_verified": False, "production_accepted": False,
                        "verification_status_counts": dict(Counter(r["verification_status"] for r in rows)),
                        "meaning": "220项来源状态审计完成；不代表220项来源、授权或内容全部VERIFIED"},
            "sources": rows, "focus_source_ids": list(FOCUS), "remote_source_0155": remote_check,
            "derived_excerpt_check": excerpt_check, "reference_workbook_check": reference_check}


# Metadata below is based on actual notices and document contents. Unconfirmed
# detailed locations are explicitly retained as unknown instead of inferred
# from a purchaser's postal address.
METADATA = (
    ("西峡县五里桥镇初级中学教师周转宿舍项目", "南阳市西峡县五里桥镇初级中学院内", "房屋建筑、土建及安装", "招标/中标；另有合同旁证", "公开招标公告正文；合同派生件对应父PDF124页"),
    ("河南女子职业学院大学生活动中心建设项目（二次）", "新乡市获嘉县亢村镇黄河大道路西河南女子职业学院北校区院内", "约800平方米室内装饰改造", "采购成交", "磋商文件_二次.pdf第6、36页"),
    ("西峡县建设路两条道路管网提升改造工程项目", "南阳市西峡县", "市政道路及管网", "施工/监理招标", "招标公告正文；一标段招标文件第10页"),
    ("河南省2026年地下水监测站建设项目", "河南省多地，详见分项最高限价表", "水文监测基础设施及仪器采购安装", "招标更正", "分项最高限价详表.pdf第1页"),
    ("河南省绿城监狱安防设施升级改造项目", "详细实施地址待核实", "安防设备采购安装", "公开招标", "公开招标公告正文及采购技术需求"),
    ("河南科技大学开元会议中心附属配套设施提升改造项目", "河南科技大学开元校区", "音视频及舞台设备采购安装", "采购成交", "招标文件正文.pdf第8页交货地点"),
    ("内乡县2026年常态化帮扶产业路建设及修复项目", "南阳市内乡县，具体道路待核实", "公路建设及修复", "合同签订公告", "合同公告正文；扫描合同待专业校对"),
    ("安阳市中级人民法院2026年中央空调管道系统更换改造项目", "安阳市，详细施工地址待核实", "暖通管道改造", "履约/验收公示及付款申请", "验收报告公示正文及支付申请表"),
    ("平顶山市石龙区城市排水防涝地下管网新建工程项目", "平顶山市石龙区，具体范围待核实", "市政排水管网", "履约/分期计量与验收公示", "验收报告公示正文；第5/6/7期计量扫描件"),
    ("焦作市职业技术学校教学楼等外墙真石漆修缮项目", "焦作市职业技术学校，具体楼栋见公示", "房屋修缮及装饰", "验收公示", "验收报告公示正文及验收报告"),
    ("许昌市城乡一体化示范区财政局工程造价咨询、审计服务框架协议项目", "许昌市城乡一体化示范区", "造价咨询与审计服务采购", "框架协议征集", "公开招标公告正文及征集文件"),
    ("郑州市疾病预防控制中心（卫生监督所）门诊部改造项目", "郑州市，详细施工地址待核实", "房屋改造", "验收公示", "验收报告公示正文及门诊部改造验收表"),
    ("新密市牛店镇助泉寺村特色民宿建设项目", "新密市牛店镇助泉寺村，详细范围待核实", "民宿建设", "采购成交；RAR正文待解包", "成交公告正文；招标文件.rar仍待核验"),
    ("河南大学金明校区5#至9#学生宿舍楼及地下车库项目", "河南大学金明校区，详细地址待核实", "房屋建筑及地下车库", "开标/中标候选人公示", "中标候选人公示一、二标段及开标记录"),
    ("河南科技学院东校区学生宿舍D项目二标段施工", "河南科技学院东校区，详细地址待核实", "房屋建筑", "开标/中标结果", "中标候选人公示及二标段施工中标结果公告"),
    ("周口市第一人民医院产科病房升级改造工程项目", "周口市第一人民医院，详细施工地址待核实", "医疗用房改造", "废标/终止", "废标终止公告正文；扫描改造说明待校对"),
    ("商丘市民权县普通省道公路交通安全设施精细化提升工程项目", "商丘市民权县S213、S314线", "公路交通安全设施", "采购更正", "更正公告正文及更正公告.pdf第1页"),
    ("灵宝市苏村乡田川至灵卢县界段公路改建工程", "灵宝市苏村乡田川至灵卢县界段", "公路改建", "成交结果更正及争议处理", "结果更正公告正文、回复函及问询函"),
)


def classify_file(path: str) -> tuple[str, list[str]]:
    name = Path(path).name
    reference = ("/B_REFERENCE/" in path or any(x in name for x in ("清单", "报价", "限价", "开标记录", "中标候选", "中标结果", "成交公告", "中标公告", "被推荐", "业绩", "旁证")))
    role = "B_REFERENCE" if reference else "A_INPUT"
    if "/B_REFERENCE/" not in path and any(x in name for x in ("招标文件", "磋商文件", "征集文件")) and not any(x in name for x in ("业绩", "旁证")):
        # A tender document stays an input even when its filename records the
        # award notice from which the official attachment was downloaded.
        role = "A_INPUT"
    if "README_" in name:
        role = "B_REFERENCE"
    kinds = []
    if any(x in name for x in ("招标文件", "磋商文件", "征集文件")):
        kinds.append("tender")
    if "招标公告" in name:
        kinds.append("tender_notice")
    if any(x in name for x in ("清单", "报价明细", "最高限价", "计价对照")):
        kinds.append("boq")
    if "合同" in name and "README" not in name:
        kinds.append("contract")
    if any(x in name for x in ("计量", "支付申请", "结算")):
        kinds.append("settlement")
    if "验收" in name:
        kinds.append("acceptance")
    if any(x in name for x in ("回复函", "问询函", "举报", "更正", "废标", "改造说明")):
        kinds.append("issue")
    if role == "B_REFERENCE":
        kinds.append("reference")
    return role, kinds


def build_projects(corpus: Path, scan: list[dict], source_report: dict, parser_report: dict | None = None) -> dict:
    sources = {row["local_path"]: row for row in source_report["sources"]}
    parsed_files = {row["path"]: row for row in (parser_report or {}).get("records", [])}
    matrix = read_csv(corpus / "00_INDEX/PROJECT_SAMPLE_MATRIX.csv")
    if tuple(row["project_id"] for row in matrix) != PROJECT_PATHS:
        raise ValueError("Original sample register changed; review project-ID mapping before importing")
    projects = []
    for index, old in enumerate(matrix):
        name, location, kind, stage, metadata_evidence = METADATA[index]
        prefix = "05_REAL_PROJECTS/" + old["project_id"] + "/"
        files, archive_children, documents = [], [], {key: [] for key in ("tender", "tender_notice", "drawings", "boq", "contract", "settlement", "acceptance", "material_price_basis", "quota_basis", "reference", "issue")}
        for record in scan:
            if not record["path"].startswith(prefix):
                continue
            source = sources.get(record["path"])
            parsed = parsed_files.get(record["path"])
            if parsed and parsed.get("sha256") != record["sha256"]:
                raise ValueError("Parser evidence belongs to a different original file")
            role, kinds = classify_file(record["path"])
            detail = {**record, "project_id": f"CORPUS-S{index+1:02d}", "data_role": role,
                      "folder_role": "A_INPUT" if "/A_INPUT/" in record["path"] else "B_REFERENCE" if "/B_REFERENCE/" in record["path"] else None,
                      "source_id": source["source_id"] if source else None,
                      "source_url": source["source_url"] if source else None,
                      "source_kind": source["source_kind"] if source else "UNREGISTERED",
                      "verification_status": source["verification_status"] if source else "UNVERIFIED",
                      "authorization_status": source["authorization_status"] if source else "UNVERIFIED",
                      "hash_match": source["hash_match"] if source else False,
                      "document_kinds": kinds, "professionally_reviewed": False}
            if parsed:
                detail["parse_status"] = parsed["status"]
                detail["parse_engine"] = parsed.get("converter")
                detail["parse_error_code"] = (parsed.get("error") or parsed.get("blocked_reason") or "").split(":", 1)[0] or None
            if "agentA2_" in Path(record["path"]).name:
                detail["scope_warning"] = "另一项目投标文件仅提供主项目业绩旁证，不得把载体其他图纸/清单拼入本项目"
            for key in kinds:
                documents[key].append(detail)
            files.append(detail)
            if parsed and parsed.get("content_parsed") and parsed.get("members"):
                for member in parsed["members"]:
                    if member.get("directory"):
                        continue
                    derived = Path(member["derived_path"])
                    if not derived.is_file() or digest(derived) != member["sha256"]:
                        raise ValueError("Archive child no longer matches parser evidence")
                    child_role, child_kinds = classify_file(member["name"])
                    archive_children.append({"path": record["path"] + "::" + member["name"],
                        "filename": member["name"], "derived_path": str(derived), "sha256": member["sha256"],
                        "size": member["size_bytes"], "format": member["format"], "project_id": detail["project_id"],
                        "data_role": child_role, "source_role": "DERIVED_FROM_ARCHIVE", "source_id": detail["source_id"],
                        "parent_archive_path": record["path"], "parent_archive_sha256": record["sha256"],
                        "source_url": detail["source_url"], "parse_status": member["status"],
                        "verification_status": "DERIVED_FROM_VERIFIED_ARCHIVE", "authorization_status": detail["authorization_status"],
                        "scope_warning": "归档解压派生；图纸/清单另附件未取得，合同章节是待签参考文本",
                        "page_count": member.get("parse_summary", {}).get("pages"),
                        "document_kinds": child_kinds, "professionally_reviewed": False, "formal_usable": False})
        if archive_children:
            for child in archive_children:
                for key in child["document_kinds"]:
                    if key in documents:
                        documents[key].append(child)
            # Count the extracted tender PDFs, not the archive again, as readable
            # tender documents. Keep the original archive in `files` unchanged.
            documents["tender"] = [d for d in documents["tender"] if d["format"] not in ("zip", "rar")]
            if index == 12:
                stage = "采购成交；施工/监理磋商文件可读取"
                metadata_evidence += "；RAR施工PDF第30、51、52、78页"
        statuses = {}
        for key in documents:
            state = "PRESENT_UNREVIEWED" if documents[key] else "MISSING"
            detail = "资料存在，尚未经真实专业人员核对完整性"
            if key == "drawings":
                detail = "未取得可核验的本项目成套施工图；正文提到图纸/图纸另见附件不代表附件已取得"
            elif key == "contract":
                if index == 0:
                    state, detail = "DERIVED_CORROBORATION_ONLY", "仅他项目投标业绩中的合同影像及摘录，不是采购人发布的合同原件"
                elif documents[key]:
                    detail = "扫描施工合同存在，页码/签章/附件完整性待专业复核"
            elif key == "boq":
                if index == 0:
                    state, detail = "148_PUBLISHED_REFERENCE_ROWS", "土建50行、安装98行；已发布B_REFERENCE，主清单/镜像仅封面说明，均非独立算量结果"
                elif index == 3:
                    state, detail = "LIMIT_TABLE_ONLY", "仅1页分项最高限价，缺完整工程量清单及控制价成果书"
                elif index in (4, 5):
                    state, detail = "PROCUREMENT_OR_PRICE_REFERENCE", "采购设备数量/技术需求或报价明细，不等同于施工图独立算量基准"
            elif key in ("material_price_basis", "quota_basis"):
                detail = "缺本项目地区、期次适用且有使用许可的完整依据；不将2026郑州四环内价格套到其他项目"
            elif key == "tender" and index == 12:
                if archive_children:
                    state, detail = "ARCHIVE_PARSED_UNREVIEWED", "RAR解出施工89页/监理69页PDF；施工第51页清单另附、第52页图纸另附，归档不含所称附件；第30页合同为参考文本，第78页为报价模板"
                else:
                    state, detail = "ARCHIVE_UNVERIFIED", "仅有RAR归档，未解包核验，不能计为招标文件正文已取得"
            if key == "tender" and any(item.get("parse_status") == "FAILED" for item in documents[key]):
                state, detail = "FILE_PRESENT_PARSE_FAILED", "原始招标DOC存在，但可靠离线转换器返回DOC_CONVERSION_FAILED；不能把可检索金额片段当完整正文解析成功"
            statuses[key] = {"status": state, "count": len(documents[key]), "detail": detail}
        missing = ["本项目成套施工图及修订版本", "独立工程量计算底稿及真实专业人员复核签认", "项目适用的授权定额/国标正文、正式费率、机械台班与材料价格依据"]
        if index != 0:
            missing.append("完整工程量清单及可比较的参考造价成果")
        if index not in (6,):
            missing.append("完整合同原件、补充协议及计价条款")
        else:
            missing.append("合同扫描件及附件的专业完整性复核")
        if not documents["settlement"]:
            missing.append("竣工结算与审核资料")
        if not documents["acceptance"]:
            missing.append("验收资料")
        if statuses["tender"]["status"] == "FILE_PRESENT_PARSE_FAILED":
            missing.append("可由可靠转换器读取的完整招标文件副本，或授权离线转换工具")
        tests = ["资料登记、SHA256及来源定位", "公告/文档解析和字段结构化", "跨项目ACL隔离与缺失资料拒绝"]
        if index == 0:
            tests.extend(["148行B_REFERENCE清单导入、查询、结构化和导出", "合同旁证OCR校对与版本留痕"])
        if documents["settlement"] or documents["acceptance"]:
            tests.append("扫描计量/验收资料OCR及人工复核流程")
        if documents["contract"]:
            tests.append("合同扫描件/旁证OCR校对与条款定位")
        if index in (3, 4, 5):
            tests.append("分项限价/设备清单/报价表结构化与合计核对")
        if index in (13, 14):
            tests.append("开标/中标记录字段抽取与对照")
        if documents["issue"]:
            tests.append("更正/争议资料版本链与证据定位")
        project_sources = [sources[f["path"]] for f in files if f["path"] in sources]
        projects.append({"project_id": f"CORPUS-S{index+1:02d}", "sample_id": old["sample_id"],
                         "corpus_project_path": prefix.rstrip("/"), "original_project_id": old["project_id"],
                         "name": name, "project_name": name, "location": location, "engineering_type": kind, "stage": stage,
                         "business_line": old["business_line"], "metadata_evidence": metadata_evidence,
                         "file_count": len(files), "a_input_count": sum(f["data_role"] == "A_INPUT" for f in files),
                         "b_reference_count": sum(f["data_role"] == "B_REFERENCE" for f in files),
                         "documents": documents, "document_status": statuses, "files": files,
                         "archive_children": archive_children, "derived_document_count": len(archive_children),
                         "source_completeness": {"status": "PARTIAL", "files": len(files),
                            "registered": len(project_sources), "hash_matched": sum(s["hash_match"] for s in project_sources),
                            "with_direct_url": sum(bool(s["source_url"]) for s in project_sources),
                            "local_derived": sum(s["source_kind"] == "LOCAL_DERIVED" for s in project_sources),
                            "note": "来源可追溯不等于项目全套资料齐全或授权已核验"},
                         "authorization_status": "PUBLIC_OR_DERIVED_REUSE_RIGHTS_NOT_CONFIRMED",
                         "allowed_tests": tests, "missing": missing, "l1_quantity_status": "BLOCKED",
                         "professional_signoff_status": "NEEDS_EXPERT_REVIEW", "independent_goldenset": False,
                         "production_accepted": False})
    project_paths = {f["path"] for project in projects for f in project["files"]}
    candidates = [r for r in scan if r["path"].startswith("05_REAL_PROJECTS/") and r["path"] not in project_paths]
    return {"schema_version": "1.0", "generated_at": datetime.now(SHANGHAI).isoformat(),
            "corpus_root": str(corpus), "scan": {"files": len(scan), "bytes": sum(r["size"] for r in scan),
                "project_files": len(project_paths), "candidate_files": len(candidates), "sha256_manifest": "CORPUS_RESCAN_SHA256.json"},
            "summary": {"projects": len(projects), "l1_ready": 0, "production_accepted": False,
                        "formal_precision": "BLOCKED", "independent_goldenset_projects": 0},
            "projects": projects, "candidates_outside_18": candidates,
            "scope_notes": ["候选资料已被全仓扫描，未把候选项目与18个登记项目拼接", "A_INPUT/B_REFERENCE是业务使用角色；folder_role另外保留旧目录标签", "未发现可核验成套施工图，不能以招标文本引用图纸或他项目投标附件代替", "原件70份与归档派生子文件分别计数，派生件不增列为新的官方原件"]}


def markdown_reports(root: Path, source: dict, projects: dict) -> None:
    summary = source["summary"]
    lines = ["# V4.2.5 220项来源状态审查", "", f"审查时间：{source['generated_at']}。220项逐条记录已完成自动证据核对；不构成专业签认。", "",
             f"原始SHA256匹配 {summary['hash_matched']}/{summary['total']}；直接来源URL {summary['direct_url_count']}/220；3项本地派生件保留无独立官方URL。生产验收 False。", "",
             "## 4项缺口调查", "", "| 来源 | 当前状态 | 已核查证据与限制 |", "|---|---|---|"]
    for row in source["sources"]:
        if row["source_id"] not in FOCUS:
            continue
        details = "；".join(row["limitations"])
        if row["source_id"] == "SRC-0155":
            details = "采集TSV精确路径、字节数及SHA前16位匹配；本轮官方HTTP字节完整SHA匹配" if source["remote_source_0155"].get("verified") else "采集TSV关联确认；远程字节未在本次运行确认"
            details += "；原UNVERIFIED授权状态保留"
        elif row["source_id"] in ("SRC-0132", "SRC-0137"):
            details = f"README登记895页公开载体SHA；物理122–137页与16页摘录逐页渲染一致={source['derived_excerpt_check']['all_16_pages_equivalent']}；仍非官方合同原件，未获企业授权"
        else:
            details = f"148行来源工作表/行号、编码/名称/单位/工程量匹配 {source['reference_workbook_check']['matched']}/148；保留B_REFERENCE，未独立复核"
        lines.append(f"| {row['source_id']} | {row['verification_status']} | {details} |")
    lines.extend(["", "URL存在、来源关系、文件内容和授权分开记录。SRC-0132/0137/0220保留INCOMPLETE；父来源URL仅存在parent_sources，不假填成派生文件自身官方URL。未强行将220项升级为VERIFIED。", "",
                  "## 全部220项", "", "每项完整SHA、原登记状态、URL解析证据、父子关系及授权限制见 reports/v425/SOURCE_STATUS.json；原数据仓登记未修改。", "",
                  "| 来源 | 资料 | 状态 | 直接URL | 授权状态 | SHA256 |", "|---|---|---|---|---|---|"])
    for row in source["sources"]:
        url = f"[来源]({row['source_url']})" if row["source_url"] else "无（本地派生）" if row["source_kind"] == "LOCAL_DERIVED" else "缺失"
        lines.append(f"| {row['source_id']} | {row['local_path']} | {row['verification_status']} | {url} | {row['authorization_status']} | `{row['sha256']}` |")
    (root / "SOURCE_VERIFICATION_REPORT_V425.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    lines = ["# V4.2.5 真实项目资料就绪度审查", "", f"审查时间：{projects['generated_at']}。全仓重扫 {projects['scan']['files']} 文件，18套登记项目 {projects['scan']['project_files']} 文件；候选资料另计 {projects['scan']['candidate_files']} 文件。所有文件逐份计算SHA256，原仓只读。", "",
             "**18套中，具备本项目完整施工图、清单、合同和独立参考成果，可进入L1独立算量精度验证的项目：0套。E级精度BLOCKED，生产验收False。**", "",
             "S01最适合清单导入/追溯流程测试：真实土建50行与安装98行，均为已发布B_REFERENCE，不能当成系统独立计算结果。其A_INPUT目录内895页PDF属于另一项目的投标文件，只有122–137页提供主项目合同旁证，不能拼接其其他图纸或清单来补齐本项目。", "",
             "S02磋商文件第36页称图纸及清单详见附件（PDF无内嵌附件）；S03招标文件第68–71页是清单/图纸章节说明，第71页称图纸详见附件，不视为实物齐全。S04只有1页限价详表；S05为设备采购清单；S06为设备报价明细。", "",
             ("S13的RAR已通过获验证离线工具解出施工89页、监理69页PDF，原件及两个子文件SHA均核对。施工PDF第51页清单『另附』、第52页图纸『另附』，实际归档仅有这两个PDF；第30页是合同参考文本，第78页是待填报价清单模板。仍缺真正施工图、清单附件和签署合同。" if projects["projects"][12]["archive_children"] else "S13仅有RAR归档，未核验正文不能计为招标文件已取得。"), "",
             ("S01中标公告附件招标文件1 DOC存在，但可靠离线转换器解析失败；原文件保留，只能使用已确认的参考清单及旁证，不把可检索金额片段当成完整招标正文已解析。" if projects["projects"][0]["document_status"]["tender"]["status"] == "FILE_PRESENT_PARSE_FAILED" else "S01招标文件完整解析状态以独立解析报告为准。"), "",
             "## 18项目矩阵", "", "| 项目ID | 项目/地点/类型/阶段 | 文件(A/B) | 可开展测试 | 尚缺资料 |", "|---|---|---|---|---|"]
    for row in projects["projects"]:
        lines.append(f"| {row['project_id']} | {row['name']}；{row['location']}；{row['engineering_type']}；{row['stage']} | {row['file_count']} ({row['a_input_count']}/{row['b_reference_count']}) | {'；'.join(row['allowed_tests'])} | {'；'.join(row['missing'])} |")
    lines.extend(["", "## 来源与授权", "", "来源文件直接URL、父来源、SHA及原登记状态按项目逐条保留。公开下载权不自动证明本地数据库存储、派生、再分发或正式生产使用权；18项目的完整业务授权均待核实。扫描合同、计量、验收资料仍需真实人员校对，未自动签认。", "",
                  "完整字段和逐文件证据见 reports/v425/PROJECT_READINESS.json；可筛选矩阵为 REAL_PROJECT_READINESS_MATRIX_V425.xlsx。项目地点无法由文件确定时保留待核实，不把采购人邮寄地址自动当作施工地点。", "",
                  "## 下一步所需资料", "", "优先补交同一项目成套施工图（建筑、结构、安装各专业及修订记录）、完整清单、施工合同及补充协议、预算/控制价或结算审核成果、工程量计算底稿。需同时明确资料使用授权，并由真实专业人员独立算量、套定额、组价及双人签认。现有郑州2026Q2/Q3/9月价格仅限四环以内样本查询，不适用于西峡等其他地区，也不补造缺失期次。", ""])
    (root / "REAL_PROJECT_READINESS_REPORT_V425.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=ROOT.parent / "REAL_DATA_CORPUS/HN_L1_V1_20261116")
    parser.add_argument("--out", type=Path, default=ROOT / "reports/v425")
    parser.add_argument("--verify-remote", action="store_true")
    parser.add_argument("--parser-report", type=Path, default=ROOT / "reports/v425/PARSING_RESULTS_NATIVE.json")
    args = parser.parse_args()
    corpus, out = args.corpus.resolve(), args.out.resolve()
    if out == corpus or out.is_relative_to(corpus):
        raise SystemExit("Output must remain outside the read-only corpus")
    records = scan_corpus(corpus)
    source = audit_sources(corpus, records, remote=args.verify_remote)
    parsed = json.loads(args.parser_report.read_text(encoding="utf-8-sig")) if args.parser_report.is_file() else None
    projects = build_projects(corpus, records, source, parsed)
    if parsed:
        projects["parser_evidence"] = {"report": str(args.parser_report), "sha256": digest(args.parser_report)}
    after = scan_corpus(corpus)
    if records != after:
        raise RuntimeError("Corpus changed during read-only audit")
    write_json(out / "CORPUS_RESCAN_SHA256.json", {"schema_version": "1.0", "files": records, "unchanged_during_audit": True})
    write_json(out / "SOURCE_STATUS.json", source)
    write_json(out / "PROJECT_READINESS.json", projects)
    markdown_reports(ROOT, source, projects)
    print(json.dumps({"sources": source["summary"], "projects": projects["summary"], "scan": projects["scan"]}, ensure_ascii=False))
    return 0 if source["summary"]["hash_matched"] == 220 and len(projects["projects"]) == 18 else 2


if __name__ == "__main__":
    raise SystemExit(main())
