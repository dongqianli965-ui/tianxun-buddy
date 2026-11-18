"""P1-06-FILESTORE：文件原始存储 + SHA-256 去重 + 版本管理。

设计约束
--------
1. **原始文件不改一个字节**：``put`` 只做哈希、落盘、登记元数据；``get`` 返回
   落盘时的原始字节（读时再次校验哈希，防止静默损坏）。
2. **内容寻址**：所有文件按 SHA-256 存放，路径 ``<root>/<sha256[:2]>/<sha256>``。
   相同内容只占一份磁盘（跨 doc_key 共享），元数据里按 ``doc_key`` 记版本。
3. **版本语义**：同一 ``doc_key`` 每次写入不同内容就产生新版本（version 从 1 递增）；
   与最新版本内容相同时视为幂等重复写入，**不**产生新版本，直接返回既有版本。
4. **可校验**：``verify`` 重新计算磁盘文件的 SHA-256 并与元数据比对，被篡改返回
   ``ok=False`` 并给出失败明细，绝不静默通过。
5. **项目关联**：版本记录携带 ``project_id``，可按项目检索（``list_documents``）。

元数据落在独立的 SQLite 库（默认 ``<root>/meta.sqlite3``）；DDL 在本模块内自建，
不触碰 ``txbuddy.repository`` 的权威迁移文件（本任务的写权限只到 ``storage/``）。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
]

DEFAULT_ROOT = "data/files"
DEFAULT_META_NAME = "meta.sqlite3"
_CHUNK = 1024 * 1024


class FileStoreError(Exception):
    """文件存储的基类异常。"""


class ChecksumMismatch(FileStoreError):
    """调用方声明的哈希与实际内容不一致。"""


class DocumentNotFound(FileStoreError):
    """doc_key / version 不存在。"""


class StorageCorruption(FileStoreError):
    """磁盘内容与元数据登记不一致（被篡改或写坏）。"""


def sha256_bytes(content: bytes) -> str:
    """字节串的 SHA-256 十六进制小写摘要。"""
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: str | Path) -> str:
    """流式计算文件 SHA-256，避免把大文件读进内存。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class FileVersion:
    """一条文档版本记录（元数据 + 内容寻址路径）。"""

    doc_key: str
    version: int
    sha256: str
    size: int
    project_id: str | None = None
    filename: str | None = None
    content_type: str | None = None
    actor: str | None = None
    created_at: str = ""
    storage_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VerifyReport:
    """``verify`` 的结果：``ok`` 为真才代表全部字节与登记哈希一致。"""

    ok: bool
    checked: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)

    def __bool__(self) -> bool:  # 允许 ``if not store.verify(...)``
        return self.ok

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_blob(
    sha256 TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    storage_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS file_version(
    doc_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    project_id TEXT,
    filename TEXT,
    content_type TEXT,
    actor TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(doc_key, version)
);
CREATE INDEX IF NOT EXISTS idx_file_version_project ON file_version(project_id);
CREATE INDEX IF NOT EXISTS idx_file_version_sha ON file_version(sha256);
"""


def _is_sha256(value: str) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(char in "0123456789abcdef" for char in value)


def _fsync_dir(path: Path) -> None:
    """目录项 fsync（Windows 不支持打开目录，失败即忽略）。"""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except (OSError, ValueError):  # pragma: no cover - Windows
        return
    try:
        os.fsync(fd)
    except OSError:  # pragma: no cover - 部分文件系统不支持
        pass
    finally:
        os.close(fd)


def _blob_state(target: Path, sha256: str) -> bool | None:
    """返回 True=内容一致 / False=内容被占位污染 / None=当前读不到（并发换名）。"""
    for attempt in range(20):
        try:
            return sha256_file(target) == sha256
        except OSError:
            if not target.exists():
                return None
            time.sleep(0.01 * (attempt + 1))
    return None


def _replace_with_retry(tmp_path: Path, target: Path, sha256: str) -> None:
    """原子换名；并发写同一内容时 Windows 可能拒绝替换正在被读的文件。

    - 对方已成功落盘且哈希一致 → 丢掉自己的临时文件，视为去重成功；
    - 其他错误短暂退避重试，仍失败才抛 ``StorageCorruption``。
    """
    last_error: BaseException | None = None
    for attempt in range(40):
        try:
            os.replace(tmp_path, target)
            return
        except OSError as exc:  # Windows: PermissionError(13) / FileExistsError
            last_error = exc
            if target.exists():
                try:
                    if sha256_file(target) == sha256:
                        tmp_path.unlink(missing_ok=True)
                        return
                except OSError as read_exc:  # pragma: no cover - 极端竞争
                    last_error = read_exc
            time.sleep(0.01 * min(attempt + 1, 5))
    raise StorageCorruption(f"原子落盘失败: {target} ({last_error})")


def _row_to_version(row: sqlite3.Row, root: Path | None = None) -> FileVersion:
    storage_path = row["storage_path"] if "storage_path" in row.keys() else ""
    if not storage_path and root is not None:
        storage_path = str(root / row["sha256"][:2] / row["sha256"])
    return FileVersion(
        doc_key=row["doc_key"],
        version=int(row["version"]),
        sha256=row["sha256"],
        size=int(row["size"]),
        project_id=row["project_id"],
        filename=row["filename"],
        content_type=row["content_type"],
        actor=row["actor"],
        created_at=row["created_at"],
        storage_path=storage_path,
    )


class FileStore:
    """原始文件仓库：``put`` / ``get`` / ``list_versions`` / ``verify``。

    :param root: 文件根目录（默认 ``data/files``）。
    :param db_path: 元数据库路径；默认 ``<root>/meta.sqlite3``。
    :param fsync: 落盘后是否 fsync（测试可关，生产默认开）。
    """

    def __init__(
        self,
        root: str | Path = DEFAULT_ROOT,
        db_path: str | Path | None = None,
        *,
        fsync: bool = True,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path) if db_path is not None else self.root / DEFAULT_META_NAME
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fsync = fsync
        self._lock = threading.RLock()
        self._init_schema()

    # ------------------------------------------------------------ 基础连接
    def connect(self) -> sqlite3.Connection:
        """独立连接：WAL + busy timeout，便于多线程/多进程共用同一目录。"""
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    # ------------------------------------------------------------ 路径与落盘
    def blob_path(self, sha256: str) -> Path:
        """内容寻址路径：``<root>/<sha256 前 2 位>/<sha256>``。"""
        if not _is_sha256(sha256):
            raise ValueError(f"非法 SHA-256: {sha256!r}")
        return self.root / sha256[:2] / sha256

    def _write_blob(self, content: bytes, sha256: str) -> Path:
        """原子写入（临时文件 + os.replace），已存在则只校验不重写。"""
        target = self.blob_path(sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            # 去重命中：内容寻址下同一路径可能被前人写过，必须核对而不是覆盖。
            state = _blob_state(target, sha256)
            if state is True:
                return target
            if state is False:
                raise StorageCorruption(f"已有文件内容与哈希不符: {target}")
            # None：并发换名瞬间读不到，继续走临时文件 + 重试落盘
        handle = tempfile.NamedTemporaryFile(
            dir=str(target.parent), prefix=f".{sha256[:8]}.", suffix=".tmp", delete=False
        )
        tmp_path = Path(handle.name)
        try:
            with handle:
                handle.write(content)
                handle.flush()
                if self._fsync:
                    os.fsync(handle.fileno())
            _replace_with_retry(tmp_path, target, sha256)
            if self._fsync:
                _fsync_dir(target.parent)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        return target

    # ------------------------------------------------------------ put
    def put(
        self,
        content: bytes,
        doc_key: str,
        *,
        project_id: str | None = None,
        filename: str | None = None,
        content_type: str | None = None,
        actor: str | None = None,
        expected_sha256: str | None = None,
    ) -> FileVersion:
        """原样保存一份文件，返回其版本记录。

        - 同 doc_key 最新内容相同 → 幂等，返回既有版本，不新增版本号；
        - 相同内容不同 doc_key → 共享同一份磁盘文件（去重），各自记版本；
        - 不同内容 → 该 doc_key 的新版本（version + 1）。
        """
        if isinstance(content, (bytearray, memoryview)):
            content = bytes(content)
        if isinstance(content, str):
            content = content.encode("utf-8")
        if not isinstance(content, bytes):
            raise TypeError("content 必须是 bytes / bytearray / str")
        if not doc_key or not isinstance(doc_key, str):
            raise ValueError("doc_key 必须是非空字符串")
        sha256 = sha256_bytes(content)
        if expected_sha256 is not None and expected_sha256.lower() != sha256:
            raise ChecksumMismatch(
                f"内容哈希不符: expected={expected_sha256.lower()} actual={sha256}"
            )
        with self._lock:
            stored_path = self._write_blob(content, sha256)
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                now = _now()
                conn.execute(
                    """INSERT INTO file_blob(sha256, size, storage_path, created_at)
                       VALUES(?,?,?,?)
                       ON CONFLICT(sha256) DO NOTHING""",
                    (sha256, len(content), str(stored_path), now),
                )
                row = conn.execute(
                    "SELECT * FROM file_version WHERE doc_key=? ORDER BY version DESC LIMIT 1",
                    (doc_key,),
                ).fetchone()
                # 项目/文件名/类型是文档级属性：新版本未显式给出时沿用上一版本，
                # 否则「换一版文件」会把文档从项目里丢掉。
                effective: dict[str, Any] = {}
                for column, value in (
                    ("project_id", project_id),
                    ("filename", filename),
                    ("content_type", content_type),
                ):
                    if value is not None:
                        effective[column] = value
                    elif row is not None:
                        effective[column] = row[column]
                    else:
                        effective[column] = None
                if row is not None and row["sha256"] == sha256:
                    # 幂等重复写入：内容没变就不造版本，只允许补齐全空的展示元数据。
                    updates = {
                        column: value
                        for column, value in effective.items()
                        if value is not None and row[column] != value
                    }
                    if updates:
                        assignments = ", ".join(f"{key}=?" for key in updates)
                        conn.execute(
                            f"UPDATE file_version SET {assignments} WHERE doc_key=? AND version=?",
                            (*updates.values(), doc_key, row["version"]),
                        )
                        row = conn.execute(
                            "SELECT * FROM file_version WHERE doc_key=? AND version=?",
                            (doc_key, row["version"]),
                        ).fetchone()
                    conn.execute("COMMIT")
                    return _row_to_version(row, self.root)
                next_version = 1 if row is None else int(row["version"]) + 1
                conn.execute(
                    """INSERT INTO file_version(
                           doc_key, version, sha256, size, project_id, filename,
                           content_type, actor, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (doc_key, next_version, sha256, len(content),
                     effective["project_id"], effective["filename"],
                     effective["content_type"], actor, now),
                )
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()
            return FileVersion(
                doc_key=doc_key,
                version=next_version,
                sha256=sha256,
                size=len(content),
                project_id=effective["project_id"],
                filename=effective["filename"],
                content_type=effective["content_type"],
                actor=actor,
                created_at=now,
                storage_path=str(stored_path),
            )

    # ------------------------------------------------------------ 查询
    def get_version(self, doc_key: str, version: int | None = None) -> FileVersion:
        """取版本元数据；``version=None`` 表示最新版本。"""
        conn = self.connect()
        try:
            if version is None:
                row = conn.execute(
                    "SELECT * FROM file_version WHERE doc_key=? ORDER BY version DESC LIMIT 1",
                    (doc_key,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM file_version WHERE doc_key=? AND version=?",
                    (doc_key, int(version)),
                ).fetchone()
        finally:
            conn.close()
        if row is None:
            label = f"{doc_key}@{version}" if version is not None else doc_key
            raise DocumentNotFound(f"文档版本不存在: {label}")
        return _row_to_version(row, self.root)

    def latest(self, doc_key: str) -> FileVersion:
        """最新版本记录。"""
        return self.get_version(doc_key)

    def get(
        self,
        doc_key: str,
        version: int | None = None,
        *,
        check: bool = True,
    ) -> bytes:
        """读出原始字节。``check=True`` 时先校验哈希，被篡改抛 ``StorageCorruption``。"""
        ref = self.get_version(doc_key, version)
        path = self.blob_path(ref.sha256)
        try:
            content = path.read_bytes()
        except FileNotFoundError as exc:
            raise StorageCorruption(f"文件缺失: {path}") from exc
        if check:
            actual = sha256_bytes(content)
            if actual != ref.sha256:
                raise StorageCorruption(
                    f"文件内容被篡改: {path} expected={ref.sha256} actual={actual}"
                )
        return content

    def list_versions(self, doc_key: str, *, limit: int | None = None) -> list[FileVersion]:
        """按 version 升序列出某个 doc_key 的全部版本。"""
        sql = "SELECT * FROM file_version WHERE doc_key=? ORDER BY version ASC"
        params: list[Any] = [doc_key]
        if limit is not None:
            if limit < 0:
                raise ValueError("limit 不能为负")
            sql += " LIMIT ?"
            params.append(int(limit))
        conn = self.connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [_row_to_version(row, self.root) for row in rows]

    def list_documents(self, project_id: str | None = None) -> list[FileVersion]:
        """列出每个 doc_key 的最新版本，可按 project_id 过滤（项目关联）。"""
        sql = """SELECT v.* FROM file_version v
                 JOIN (SELECT doc_key, MAX(version) AS latest
                       FROM file_version GROUP BY doc_key) m
                   ON v.doc_key = m.doc_key AND v.version = m.latest"""
        params: list[Any] = []
        if project_id is not None:
            sql += " WHERE v.project_id = ?"
            params.append(project_id)
        sql += " ORDER BY v.doc_key ASC"
        conn = self.connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [_row_to_version(row, self.root) for row in rows]

    def stats(self) -> dict[str, int]:
        """磁盘去重效果：blob 数 vs 版本数 vs 文档数。"""
        conn = self.connect()
        try:
            blobs = conn.execute("SELECT COUNT(*) FROM file_blob").fetchone()[0]
            versions = conn.execute("SELECT COUNT(*) FROM file_version").fetchone()[0]
            docs = conn.execute("SELECT COUNT(DISTINCT doc_key) FROM file_version").fetchone()[0]
            total = conn.execute("SELECT COALESCE(SUM(size),0) FROM file_blob").fetchone()[0]
        finally:
            conn.close()
        return {
            "blob_count": int(blobs),
            "version_count": int(versions),
            "doc_count": int(docs),
            "total_bytes": int(total),
        }

    # ------------------------------------------------------------ verify
    def verify(
        self,
        doc_key: str | None = None,
        version: int | None = None,
        *,
        project_id: str | None = None,
    ) -> VerifyReport:
        """重算磁盘文件的 SHA-256 并与元数据比对。

        - ``doc_key`` 为空 → 校验全部版本；
        - 指定 ``version`` → 只校验该版本；
        - 返回 :class:`VerifyReport`，任何不一致都使 ``ok=False``。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if doc_key is not None:
            clauses.append("doc_key=?")
            params.append(doc_key)
        if version is not None:
            clauses.append("version=?")
            params.append(int(version))
        if project_id is not None:
            clauses.append("project_id=?")
            params.append(project_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        conn = self.connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM file_version{where} ORDER BY doc_key, version", params
            ).fetchall()
        finally:
            conn.close()
        report = VerifyReport(ok=True)
        # 内容寻址下多个 doc_key 可能指向同一 blob，同一哈希只算一次。
        digest_cache: dict[str, str] = {}
        for row in rows:
            report.checked += 1
            ref = _row_to_version(row, self.root)
            path = self.blob_path(ref.sha256)
            if not path.exists():
                report.ok = False
                report.failures.append(
                    {"doc_key": ref.doc_key, "version": ref.version,
                     "sha256": ref.sha256, "reason": "missing", "path": str(path)}
                )
                continue
            if ref.sha256 not in digest_cache:
                digest_cache[ref.sha256] = sha256_file(path)
            actual = digest_cache[ref.sha256]
            if actual != ref.sha256 or path.stat().st_size != ref.size:
                report.ok = False
                report.failures.append(
                    {"doc_key": ref.doc_key, "version": ref.version,
                     "sha256": ref.sha256, "actual_sha256": actual,
                     "expected_size": ref.size, "actual_size": path.stat().st_size,
                     "reason": "sha256_mismatch", "path": str(path)}
                )
        return report

    # ------------------------------------------------------------ 便捷入口
    def put_file(
        self,
        path: str | Path,
        doc_key: str,
        **kwargs: Any,
    ) -> FileVersion:
        """从磁盘文件导入（读原始字节，不改内容）。"""
        source = Path(path)
        content = source.read_bytes()
        kwargs.setdefault("filename", source.name)
        return self.put(content, doc_key, **kwargs)

    def export(self, doc_key: str, target: str | Path, version: int | None = None) -> Path:
        """把某个版本原样导出到目标路径（先校验哈希再写）。"""
        content = self.get(doc_key, version)
        destination = Path(target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return destination

    def register_with_job(
        self,
        document_repo: Any,
        job_id: str,
        doc_key: str,
        version: int | None = None,
        *,
        check: bool = True,
    ) -> FileVersion:
        """把版本登记进既有 ``job_document`` 仓储（``DocumentRepository``）。

        文件本体与版本元数据仍归 FileStore；Job 侧只保存引用
        ``(doc_key, version, file_sha256, verified)``，避免两处各存一份字节。
        ``verified`` 由 :meth:`verify` 的实时结果决定，不做乐观假设。
        """
        ref = self.get_version(doc_key, version)
        verified = self.verify(ref.doc_key, ref.version).ok if check else False
        document_repo.add(
            job_id, ref.doc_key, str(ref.version), ref.sha256, verified, ref.created_at
        )
        return ref

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"FileStore(root={str(self.root)!r}, db_path={str(self.db_path)!r})"
