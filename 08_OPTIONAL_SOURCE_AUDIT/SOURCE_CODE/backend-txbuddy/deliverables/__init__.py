"""P1-10-DELIVER：十类真实可打开成果的生成入口。

用法::

    from txbuddy.deliverables import DeliverableDataset, DeliverableService

    dataset = DeliverableDataset.from_store(store, job_id)
    manifest = DeliverableService("data/deliverables", store.get).generate_all(dataset)
    for item in manifest.files:
        print(item.kind, item.path, item.sha256)

十类：预算书 XLSX / 预算书 PDF / 工程量计算书 / 定额套用明细 / 综合单价分析 /
人工材料机械汇总 / 费用计算 / 调差 / 审核差异 / 证据索引。
"""
from .model import DeliverableDataset, DeliverableError
from .service import (DeliverableFile, DeliverableManifest, DeliverableService,
                      sha256_file)
from .specs import SPECS, DeliverableSpec

__all__ = [
    "DeliverableDataset",
    "DeliverableError",
    "DeliverableFile",
    "DeliverableManifest",
    "DeliverableService",
    "DeliverableSpec",
    "SPECS",
    "sha256_file",
]
