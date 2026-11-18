"""P1-06-FILESTORE 验收测试：原始存储 + SHA-256 + 版本管理。

覆盖 TASK.md 的四条硬要求：
1. 写入后哈希一致（内容原样，二进制含 NUL 也不变）；
2. 相同内容去重（同/不同 doc_key 都不重复占盘、不重复造版本）；
3. 不同内容成新版本（旧版本仍可取回且不变）；
4. 篡改后 verify 失败（并给出失败明细，get 也不会静默返回脏数据）。

另外覆盖：只读校验、缺失文件、期望哈希校验、项目关联、跨实例持久化、
并发写入版本号唯一、导入/导出、与 job_document 仓储的登记集成。
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path

import pytest

from txbuddy.storage import (
    ChecksumMismatch,
    DocumentNotFound,
    FileStore,
    FileStoreError,
    StorageCorruption,
    sha256_bytes,
    sha256_file,
)

BINARY = b"%PDF-1.7\n\x00\x01\x02\xff\xfe binary payload \x00 end"
TEXT = "河南定额 2025 测试文件".encode("utf-8")


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "files"


@pytest.fixture()
def store(data_root: Path) -> FileStore:
    return FileStore(data_root, fsync=False)


# --------------------------------------------------------------- 1 写入后哈希一致
class TestPutGetIntegrity:
    def test_put_returns_sha256_of_content(self, store: FileStore) -> None:
        ref = store.put(BINARY, "doc-001", project_id="P-001", filename="a.pdf")
        assert ref.sha256 == sha256_bytes(BINARY)
        assert ref.sha256 == hashlib.sha256(BINARY).hexdigest()
        assert ref.size == len(BINARY)
        assert ref.version == 1
        assert ref.doc_key == "doc-001"
        assert ref.project_id == "P-001"

    def test_blob_lands_under_sha256_prefix(self, store: FileStore, data_root: Path) -> None:
        ref = store.put(BINARY, "doc-001")
        expected = data_root / ref.sha256[:2] / ref.sha256
        assert Path(ref.storage_path) == expected
        assert expected.exists()
        assert expected.read_bytes() == BINARY
        assert expected.name == ref.sha256
        assert sha256_file(expected) == ref.sha256

    def test_get_returns_original_bytes(self, store: FileStore) -> None:
        store.put(BINARY, "doc-001")
        assert store.get("doc-001") == BINARY
        assert store.get("doc-001", 1) == BINARY

    def test_get_does_not_decode_or_normalise(self, store: FileStore) -> None:
        payload = bytes(range(256))
        store.put(payload, "doc-bytes")
        assert store.get("doc-bytes") == payload

    def test_utf8_text_roundtrip(self, store: FileStore) -> None:
        ref = store.put(TEXT, "doc-cn")
        assert store.get("doc-cn") == TEXT
        assert ref.sha256 == sha256_bytes(TEXT)

    def test_put_accepts_str_as_utf8(self, store: FileStore) -> None:
        ref = store.put("中文内容", "doc-str")
        assert store.get("doc-str") == "中文内容".encode("utf-8")
        assert ref.sha256 == sha256_bytes("中文内容".encode("utf-8"))

    def test_put_file_imports_without_mutation(self, store: FileStore, tmp_path: Path) -> None:
        source = tmp_path / "原始文件.pdf"
        source.write_bytes(BINARY)
        ref = store.put_file(source, "doc-import")
        assert ref.filename == "原始文件.pdf"
        assert ref.sha256 == sha256_bytes(BINARY)
        assert store.get("doc-import") == BINARY

    def test_export_writes_original(self, store: FileStore, tmp_path: Path) -> None:
        store.put(BINARY, "doc-export")
        target = tmp_path / "out" / "copy.pdf"
        assert store.export("doc-export", target) == target
        assert target.read_bytes() == BINARY


# --------------------------------------------------------------- 2 相同内容去重
class TestDedup:
    def test_same_content_same_doc_key_is_idempotent(self, store: FileStore) -> None:
        first = store.put(BINARY, "doc-001", project_id="P-001")
        second = store.put(BINARY, "doc-001", project_id="P-001")
        assert second.version == 1
        assert second.sha256 == first.sha256
        assert len(store.list_versions("doc-001")) == 1
        assert store.stats()["version_count"] == 1

    def test_same_content_different_doc_key_shares_blob(self, store: FileStore) -> None:
        first = store.put(BINARY, "doc-A")
        second = store.put(BINARY, "doc-B", project_id="P-002")
        assert first.version == 1 and second.version == 1
        assert first.sha256 == second.sha256
        assert first.storage_path == second.storage_path
        blobs = [p for p in store.root.glob("*/*") if p.is_file()]
        assert len(blobs) == 1
        assert store.stats()["blob_count"] == 1
        assert store.stats()["doc_count"] == 2
        assert store.stats()["total_bytes"] == len(BINARY)
        assert store.get("doc-A") == store.get("doc-B") == BINARY

    def test_repeated_put_keeps_single_physical_file(self, store: FileStore) -> None:
        for _ in range(5):
            store.put(TEXT, "doc-repeat")
        assert store.stats()["version_count"] == 1
        assert len(list(store.root.glob("*/*"))) == 1

    def test_idempotent_put_backfills_metadata(self, store: FileStore) -> None:
        store.put(BINARY, "doc-meta")
        again = store.put(BINARY, "doc-meta", project_id="P-LATE", filename="late.pdf")
        assert again.version == 1
        assert again.project_id == "P-LATE"
        assert again.filename == "late.pdf"
        assert store.get("doc-meta") == BINARY


# --------------------------------------------------------------- 3 不同内容成新版本
class TestVersioning:
    def test_new_content_creates_next_version(self, store: FileStore) -> None:
        v1 = store.put(b"v1-bytes", "doc-v", project_id="P-1")
        v2 = store.put(b"v2-bytes", "doc-v")
        v3 = store.put(b"v3-bytes", "doc-v")
        assert [v1.version, v2.version, v3.version] == [1, 2, 3]
        assert len({v1.sha256, v2.sha256, v3.sha256}) == 3
        assert [v.version for v in store.list_versions("doc-v")] == [1, 2, 3]
        assert store.get("doc-v") == b"v3-bytes"
        assert store.get("doc-v", 1) == b"v1-bytes"
        assert store.get("doc-v", 2) == b"v2-bytes"

    def test_versions_keep_their_own_hashes(self, store: FileStore) -> None:
        store.put(b"alpha", "doc-h")
        store.put(b"beta", "doc-h")
        versions = store.list_versions("doc-h")
        assert versions[0].sha256 == sha256_bytes(b"alpha")
        assert versions[1].sha256 == sha256_bytes(b"beta")
        assert versions[0].size == 5 and versions[1].size == 4

    def test_versions_are_isolated_per_doc_key(self, store: FileStore) -> None:
        store.put(b"a1", "doc-X")
        store.put(b"b1", "doc-Y")
        store.put(b"a2", "doc-X")
        assert [v.version for v in store.list_versions("doc-X")] == [1, 2]
        assert [v.version for v in store.list_versions("doc-Y")] == [1]
        assert store.latest("doc-X").sha256 == sha256_bytes(b"a2")

    def test_reverting_to_old_content_makes_new_version(self, store: FileStore) -> None:
        store.put(b"round-1", "doc-revert")
        store.put(b"round-2", "doc-revert")
        third = store.put(b"round-1", "doc-revert")
        assert third.version == 3
        assert store.get("doc-revert", 3) == b"round-1"

    def test_list_versions_limit_and_unknown_doc(self, store: FileStore) -> None:
        for index in range(3):
            store.put(f"payload-{index}".encode(), "doc-limit")
        assert [v.version for v in store.list_versions("doc-limit", limit=2)] == [1, 2]
        assert store.list_versions("never-written") == []
        with pytest.raises(DocumentNotFound):
            store.get("never-written")
        with pytest.raises(DocumentNotFound):
            store.get_version("doc-limit", 99)


# --------------------------------------------------------------- 4 篡改后 verify 失败
class TestTamperDetection:
    def test_clean_store_verify_passes(self, store: FileStore) -> None:
        store.put(b"clean-1", "doc-clean")
        store.put(b"clean-2", "doc-clean")
        store.put(b"other", "doc-other")
        report = store.verify()
        assert report.ok is True
        assert report.checked == 3
        assert report.failures == []
        assert bool(report) is True

    def test_tampered_blob_fails_verify(self, store: FileStore) -> None:
        ref = store.put(BINARY, "doc-tamper")
        Path(ref.storage_path).write_bytes(b"malicious replacement")
        report = store.verify()
        assert report.ok is False
        assert report.checked == 1
        assert len(report.failures) == 1
        failure = report.failures[0]
        assert failure["doc_key"] == "doc-tamper"
        assert failure["version"] == 1
        assert failure["reason"] == "sha256_mismatch"
        assert failure["sha256"] == ref.sha256
        assert failure["actual_sha256"] == sha256_bytes(b"malicious replacement")
        assert failure["actual_sha256"] != ref.sha256
        assert bool(report) is False

    def test_same_size_tamper_still_detected(self, store: FileStore) -> None:
        ref = store.put(b"AAAAAAAA", "doc-size")
        Path(ref.storage_path).write_bytes(b"BBBBBBBB")
        report = store.verify("doc-size", 1)
        assert report.ok is False
        assert report.failures[0]["actual_size"] == report.failures[0]["expected_size"] == 8
        assert report.failures[0]["reason"] == "sha256_mismatch"

    def test_tampered_get_raises(self, store: FileStore) -> None:
        ref = store.put(BINARY, "doc-get-tamper")
        Path(ref.storage_path).write_bytes(b"changed")
        with pytest.raises(StorageCorruption):
            store.get("doc-get-tamper")
        # 显式关闭校验时才返回磁盘现状（供取证用，不作为正常读取路径）
        assert store.get("doc-get-tamper", check=False) == b"changed"

    def test_missing_blob_reported(self, store: FileStore) -> None:
        ref = store.put(b"will-be-deleted", "doc-missing")
        Path(ref.storage_path).unlink()
        report = store.verify()
        assert report.ok is False
        assert report.failures[0]["reason"] == "missing"
        with pytest.raises(StorageCorruption):
            store.get("doc-missing")

    def test_verify_specific_doc_and_version_only(self, store: FileStore) -> None:
        good_v1 = store.put(b"good-1", "doc-mix")
        bad_v2 = store.put(b"bad-2", "doc-mix")
        untouched = store.put(b"untouched", "doc-untouched")
        Path(bad_v2.storage_path).write_bytes(b"tampered")
        scoped = store.verify("doc-mix", 1)
        assert scoped.ok is True and scoped.checked == 1
        doc_scope = store.verify("doc-mix")
        assert doc_scope.ok is False and doc_scope.checked == 2
        assert doc_scope.failures[0]["version"] == 2
        assert store.verify("doc-untouched").ok is True
        assert store.verify("doc-untouched").checked == 1
        assert untouched.sha256 == sha256_bytes(b"untouched")
        assert good_v1.version == 1

    def test_one_tamper_does_not_hide_other_clean_files(self, store: FileStore) -> None:
        bad = store.put(b"victim", "doc-bad")
        store.put(b"innocent", "doc-good")
        Path(bad.storage_path).write_bytes(b"x")
        report = store.verify()
        assert report.checked == 2
        assert [f["doc_key"] for f in report.failures] == ["doc-bad"]


# --------------------------------------------------------------- 5 项目关联 / 持久化
class TestProjectAndPersistence:
    def test_list_documents_filters_by_project(self, store: FileStore) -> None:
        store.put(b"p-a-1", "doc-a", project_id="P-A")
        store.put(b"p-b-1", "doc-b", project_id="P-B")
        store.put(b"p-a-2", "doc-a")
        docs_a = store.list_documents("P-A")
        docs_b = store.list_documents("P-B")
        assert [d.doc_key for d in docs_a] == ["doc-a"]
        assert docs_a[0].version == 2
        assert docs_a[0].sha256 == sha256_bytes(b"p-a-2")
        assert [d.doc_key for d in docs_b] == ["doc-b"]
        assert {d.doc_key for d in store.list_documents()} == {"doc-a", "doc-b"}

    def test_project_and_filename_inherited_by_new_version(self, store: FileStore) -> None:
        store.put(b"v1", "doc-inherit", project_id="P-X", filename="orig.pdf")
        second = store.put(b"v2", "doc-inherit")
        assert second.project_id == "P-X"
        assert second.filename == "orig.pdf"
        assert store.get_version("doc-inherit", 1).project_id == "P-X"

    def test_reopen_store_sees_versions(self, data_root: Path) -> None:
        first = FileStore(data_root, fsync=False)
        ref = first.put(BINARY, "doc-persist", project_id="P-DB")
        first.put(b"second", "doc-persist")
        reopened = FileStore(data_root, fsync=False)
        versions = reopened.list_versions("doc-persist")
        assert [v.version for v in versions] == [1, 2]
        assert versions[0].sha256 == ref.sha256
        assert reopened.get("doc-persist", 1) == BINARY
        assert reopened.verify().ok is True
        assert reopened.stats()["blob_count"] == 2

    def test_metadata_rows_recorded_in_sqlite(self, store: FileStore) -> None:
        ref = store.put(BINARY, "doc-db", project_id="P-DB", actor="tester")
        conn = sqlite3.connect(store.db_path)
        try:
            blob = conn.execute(
                "SELECT size, storage_path FROM file_blob WHERE sha256=?", (ref.sha256,)
            ).fetchone()
            version = conn.execute(
                "SELECT version, sha256, project_id, actor FROM file_version WHERE doc_key=?",
                ("doc-db",),
            ).fetchone()
        finally:
            conn.close()
        assert blob == (len(BINARY), ref.storage_path)
        assert version == (1, ref.sha256, "P-DB", "tester")


# --------------------------------------------------------------- 6 入参校验
class TestValidation:
    def test_verify_can_scope_to_project(self, store: FileStore) -> None:
        bad = store.put(b"project-a", "doc-pa", project_id="P-A")
        store.put(b"project-b", "doc-pb", project_id="P-B")
        Path(bad.storage_path).write_bytes(b"corrupted")
        assert store.verify(project_id="P-A").ok is False
        report = store.verify(project_id="P-B")
        assert report.ok is True and report.checked == 1

    def test_rejects_empty_doc_key(self, store: FileStore) -> None:
        with pytest.raises(ValueError):
            store.put(b"data", "")
        with pytest.raises(ValueError):
            store.put(b"data", None)  # type: ignore[arg-type]

    def test_rejects_non_bytes_content(self, store: FileStore) -> None:
        with pytest.raises(TypeError):
            store.put(123, "doc-int")  # type: ignore[arg-type]

    def test_expected_sha256_mismatch_writes_nothing(self, store: FileStore) -> None:
        with pytest.raises(ChecksumMismatch):
            store.put(b"payload", "doc-mismatch", expected_sha256="0" * 64)
        assert store.list_versions("doc-mismatch") == []
        assert store.stats()["version_count"] == 0
        assert list(store.root.glob("*/*")) == []

    def test_expected_sha256_match_is_accepted(self, store: FileStore) -> None:
        expected = sha256_bytes(b"payload")
        ref = store.put(b"payload", "doc-expected", expected_sha256=expected.upper())
        assert ref.sha256 == expected
        assert store.get("doc-expected") == b"payload"

    def test_blob_path_rejects_invalid_hash(self, store: FileStore) -> None:
        for bad in ("", "abc", "g" * 64, "../escape", "A" * 64):
            with pytest.raises(ValueError):
                store.blob_path(bad)

    def test_sha256_file_matches_hashlib(self, store: FileStore, tmp_path: Path) -> None:
        source = tmp_path / "large.bin"
        payload = b"\x00\xff" * 5000
        source.write_bytes(payload)
        assert sha256_file(source) == hashlib.sha256(payload).hexdigest()
        assert sha256_bytes(b"") == hashlib.sha256(b"").hexdigest()
        assert issubclass(ChecksumMismatch, FileStoreError)
        assert issubclass(DocumentNotFound, FileStoreError)
        assert issubclass(StorageCorruption, FileStoreError)


# --------------------------------------------------------------- 7 并发写入
class TestConcurrency:
    def test_parallel_writers_get_unique_consecutive_versions(self, data_root: Path) -> None:
        stores = [FileStore(data_root, fsync=False) for _ in range(6)]
        results: list[int] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            try:
                ref = stores[index].put(f"payload-{index}".encode(), "doc-race")
                with lock:
                    results.append(ref.version)
            except BaseException as exc:  # pragma: no cover - 失败时把异常带回主线程
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert errors == []
        assert sorted(results) == [1, 2, 3, 4, 5, 6]
        reader = FileStore(data_root, fsync=False)
        assert [v.version for v in reader.list_versions("doc-race")] == [1, 2, 3, 4, 5, 6]
        assert reader.verify().ok is True
        assert reader.stats()["blob_count"] == 6

    def test_parallel_same_content_stays_single_version(self, data_root: Path) -> None:
        stores = [FileStore(data_root, fsync=False) for _ in range(5)]
        results: list[int] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            try:
                ref = stores[index].put(b"same-bytes", "doc-same-race")
                with lock:
                    results.append(ref.version)
            except BaseException as exc:  # pragma: no cover
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert errors == []
        assert results == [1] * 5
        reader = FileStore(data_root, fsync=False)
        assert len(reader.list_versions("doc-same-race")) == 1
        assert reader.stats()["blob_count"] == 1
        assert list(data_root.rglob("*.tmp")) == []
        assert len([p for p in data_root.glob("*/*") if p.is_file()]) == 1


# --------------------------------------------------------------- 8 与 Job 文档引用集成
class TestJobDocumentLink:
    def test_register_with_real_document_repository(self, store: FileStore, tmp_path: Path) -> None:
        from txbuddy.repository import build_sqlite_repositories

        _database, repos = build_sqlite_repositories(tmp_path / "jobs.sqlite3")
        ref = store.put(BINARY, "doc-job", project_id="P-JOB", filename="施工图.pdf")
        store.register_with_job(repos.documents, "job-001", "doc-job")

        rows = repos.documents.list_by_job("job-001")
        assert len(rows) == 1
        assert rows[0]["doc_key"] == "doc-job"
        assert rows[0]["version"] == "1"
        assert rows[0]["file_sha256"] == ref.sha256
        assert rows[0]["verified"] == 1
        assert rows[0]["added_at"] == ref.created_at

    def test_register_marks_tampered_version_unverified(
        self, store: FileStore, tmp_path: Path
    ) -> None:
        from txbuddy.repository import build_sqlite_repositories

        _database, repos = build_sqlite_repositories(tmp_path / "jobs2.sqlite3")
        ref = store.put(b"original", "doc-tampered", project_id="P-JOB")
        store.put(b"version-two", "doc-tampered")
        Path(ref.storage_path).write_bytes(b"evil")
        store.register_with_job(repos.documents, "job-002", "doc-tampered", 1)
        rows = repos.documents.list_by_job("job-002")
        assert rows[0]["version"] == "1"
        assert rows[0]["verified"] == 0
