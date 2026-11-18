"""``txbuddy.storage``：原始文件存储（P1-06-FILESTORE）。

用法::

    from txbuddy.storage import FileStore

    store = FileStore("data/files")
    ref = store.put(b"%PDF-1.7 ...", "doc-001", project_id="P-001", filename="a.pdf")
    store.get("doc-001")                  # 原始字节（读时校验哈希）
    store.list_versions("doc-001")        # 版本 1..N
    store.verify()                        # True / False + 失败明细
"""
from __future__ import annotations

from .filestore import (
    ChecksumMismatch,
    DEFAULT_META_NAME,
    DEFAULT_ROOT,
    DocumentNotFound,
    FileStore,
    FileStoreError,
    FileVersion,
    StorageCorruption,
    VerifyReport,
    sha256_bytes,
    sha256_file,
)

__all__ = [
    "FileStore",
    "FileStoreError",
    "ChecksumMismatch",
    "DocumentNotFound",
    "StorageCorruption",
    "FileVersion",
    "VerifyReport",
    "sha256_bytes",
    "sha256_file",
    "DEFAULT_ROOT",
    "DEFAULT_META_NAME",
]
