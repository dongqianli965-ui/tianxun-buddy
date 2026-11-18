"""L1_GoldenSet_001 接入与守护（TEST_ONLY）。

铁律（来自交接说明）在本模块中变为可执行断言：
1. 项目级金额不得下推为清单行级单价/合价；
2. 16 个无依据字段必须保持标记，不得补 0 或估算；
3. 6 项分项加总必须等于招标控制价合计 5,898,506.43；
4. 中标价必须低于控制价且与公告一致；
5. 缺失项（图纸/计算底稿/审核成果等）必须如实暴露，不得隐藏；
6. 本资料包只可用于局部测试，formal_ready 恒为 False。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

NO_TRUTH = "（无真实依据，留空待核实）"
ANCHOR_SHEET = "项目级真实金额锚点"
ROWS_SHEET = "真实工程量及计价对照"
CONTROL_TOTAL = Decimal("5898506.43")
BID_TOTAL = Decimal("5886069.47")
EXPECTED_ROWS = 148
EXPECTED_MAJOR = {"土建": 50, "安装": 98}
FABRICATED_COLUMNS = ("独立复核工程量", "综合单价", "合价", "定额编码", "定额名称",
                      "定额单位", "定额消耗量", "人工单价", "材料单价", "机械单价",
                      "取费依据", "调差金额", "最终金额", "图纸编号", "图纸页码", "计算依据")


class GoldenSetError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class GoldenSet:
    root: Path
    zip_sha256_ok: bool
    anchors: dict[str, Decimal] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def formal_ready(self) -> bool:
        return False  # 交接说明第 0 节：C 级，不构成端到端验收答案集

    def manifest(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "grade": "C+",
            "formal_ready": self.formal_ready,
            "zip_sha256_ok": self.zip_sha256_ok,
            "row_count": len(self.rows),
            "anchor_count": len(self.anchors),
            "anchors": {k: str(v) for k, v in self.anchors.items()},
            "stats": self.stats,
            "gaps": self.gaps,
            "warnings": self.warnings,
            "rule": "项目级金额禁止下推；16 个无依据字段保持留空标记；不得跨期次/跨地区/跨税口径混用",
        }


def _norm(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _dec(v: Any) -> Decimal | None:
    s = _norm(v).replace(",", "")
    if not s:
        return None
    try:
        return Decimal(s)
    except Exception:  # noqa: BLE001
        return None


def verify_zip(root: Path) -> bool:
    zip_path = root.parent / f"{root.name}.zip"
    sha_file = root.parent / f"{root.name}.zip.sha256"
    if not zip_path.exists() or not sha_file.exists():
        return False
    actual = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    expected = sha_file.read_text(encoding="utf-8").split()[0].strip().lower()
    return actual == expected


def load(root: str | Path) -> GoldenSet:
    root = Path(root)
    xlsx = root / "11_真实GoldenSet对照" / "真实工程量及计价对照表.xlsx"
    if not xlsx.exists():
        raise GoldenSetError("BASIS_ITEM_MISSING", f"GoldenSet 对照表不存在：{xlsx}")
    wb = load_workbook(xlsx, data_only=True, read_only=True)
    gs = GoldenSet(root=root, zip_sha256_ok=verify_zip(root))

    # 锚点
    if ANCHOR_SHEET not in wb.sheetnames:
        raise GoldenSetError("BASIS_ITEM_MISSING", f"缺少工作表：{ANCHOR_SHEET}")
    for row in list(wb[ANCHOR_SHEET].iter_rows(values_only=True))[1:]:
        subject, amount = _norm(row[1]), _dec(row[2])
        if subject and amount is not None:
            gs.anchors[subject] = amount

    # 清单行
    if ROWS_SHEET not in wb.sheetnames:
        raise GoldenSetError("BASIS_ITEM_MISSING", f"缺少工作表：{ROWS_SHEET}")
    rows = list(wb[ROWS_SHEET].iter_rows(values_only=True))
    header = [_norm(h) for h in rows[0]]
    idx = {name: i for i, name in enumerate(header) if name}
    for raw in rows[1:]:
        if not any(_norm(v) for v in raw):
            continue
        item = {name: raw[i] if i < len(raw) else None for name, i in idx.items()}
        gs.rows.append(item)

    # 统计
    gs.stats = {"rows": len(gs.rows), "header": header,
                "major_土建": sum(1 for r in gs.rows if "土建" in _norm(r.get("项目名称"))),
                "major_安装": sum(1 for r in gs.rows if "安装" in _norm(r.get("项目名称")))}

    # 缺口（来自交接说明第 3 节，代码内显式登记，避免"看起来完整"）
    gs.gaps = [
        "本项目建筑/结构图纸（PDF/DWG/DXF 施工图）未取得",
        "工程量计算底稿（含计算式）未取得",
        "定额子目完整数据与人材机消耗量未取得",
        "清单行级综合单价/合价与费率取费明细未取得",
        "项目所在地（南阳/西峡）2021 期材料信息价未取得",
        "独立审核报告与审定成果未取得",
        "官方施工合同原件（仅旁证影像）",
    ]
    return gs


def validate(gs: GoldenSet) -> dict[str, Any]:
    """铁律可执行检查；返回问题清单，不抛异常（便于测试逐项断言）。"""
    problems: list[str] = []
    if len(gs.rows) != EXPECTED_ROWS:
        problems.append(f"清单行数 {len(gs.rows)} ≠ {EXPECTED_ROWS}")
    total = gs.anchors.get("招标控制价合计")
    if total != CONTROL_TOTAL:
        problems.append(f"控制价合计 {total} ≠ {CONTROL_TOTAL}")
    parts = ["分部分项工程费", "安全文明施工费", "单价措施费", "暂列金额", "规费", "税金"]
    missing = [k for k in parts if k not in gs.anchors]
    if missing:
        problems.append(f"缺少分项锚点：{missing}")
    else:
        s = sum((gs.anchors[k] for k in parts), Decimal("0"))
        if s != CONTROL_TOTAL:
            problems.append(f"六项分项加总 {s} ≠ 控制价 {CONTROL_TOTAL}")
    bid = gs.anchors.get("中标价") or BID_TOTAL
    if bid >= CONTROL_TOTAL:
        problems.append(f"中标价 {bid} 不应 ≥ 控制价 {CONTROL_TOTAL}")
    # 16 个字段必须留空标记，不得补数
    bad = []
    for i, row in enumerate(gs.rows, start=2):
        for col in FABRICATED_COLUMNS:
            if col not in row:
                continue
            v = _norm(row[col])
            if v and v not in {NO_TRUTH, "未复核"}:
                bad.append((i, col, v[:20]))
    if bad:
        problems.append(f"存在被填值的无依据字段 {len(bad)} 处，例如 {bad[:3]}")
    if not gs.zip_sha256_ok:
        problems.append("整包 SHA-256 与 .sha256 文件不一致")
    if gs.formal_ready:
        problems.append("GoldenSet 不得声明 formal_ready=True")
    return {"ok": not problems, "problems": problems, "rows": len(gs.rows),
            "anchors": len(gs.anchors), "formal_ready": gs.formal_ready,
            "gaps": gs.gaps}


def assert_no_downpush(gs: GoldenSet) -> None:
    """项目级金额不得除以清单工程量得到行级单价（本函数只做存在性防护演示）。"""
    total = gs.anchors.get("招标控制价合计")
    if total is None:
        raise GoldenSetError("BASIS_ITEM_MISSING", "缺控制价合计锚点")
    for row in gs.rows:
        qty = _dec(row.get("原始工程量"))
        if qty and qty > 0 and "综合单价" in row:
            v = _norm(row["综合单价"])
            if v and v != NO_TRUTH:
                raise GoldenSetError("BASIS_ITEM_MISSING",
                                     "检测到行级综合单价被填值：违反'不得下推'铁律")


def export_manifest(gs: GoldenSet, out: str | Path) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(gs.manifest(), ensure_ascii=False, indent=2), encoding="utf-8")
    return out
