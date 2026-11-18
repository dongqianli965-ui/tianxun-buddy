"""P1-11-AUTH / P1-07-ACL: 文件访问控制（防目录穿越 + 项目角色 ACL + 拒识退让）。

所有读写判定都委托给 :class:`txbuddy.security.acl.ProjectAcl`——文件层不再自行
比较 ``identity.projects``，只把身份交给 ACL，保证「API 与文件访问统一走它」。
"""
from __future__ import annotations

import mimetypes
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol

from .acl import AccessDecision, AclError, Permission, ProjectAcl, ProjectRole

__all__ = [
    "FileAccessError",
    "FileGrant",
    "FileAccessPolicy",
    "IdentityLike",
]

_SAFE_SUFFIXES = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".txt", ".json", ".md",
    ".xml", ".yaml", ".yml", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip",
    ".dxf", ".dwg", ".boq", ".log", ".sha256",
}


class IdentityLike(Protocol):
    subject: str

    @property
    def roles(self) -> tuple[str, ...]: ...

    @property
    def projects(self) -> tuple[str, ...]: ...


class FileAccessError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 403) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class FileGrant:
    path: Path
    media_type: str
    size: int
    project_id: str
    role: ProjectRole | None = None
    decision: AccessDecision | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class FileAccessPolicy:
    """按 ``root/<project_id>/<...>`` 布局授权的文件访问策略。

    * 拒绝绝对路径、Windows 盘符、``..``/``.`` 段、NUL 与空段；
    * 解析后必须仍位于 root 内（同时防符号链接逃逸）；
    * 读写一律经 ``ProjectAcl`` 判定：读取需 READ，写入需 WRITE；
    * 未知后缀一律按 ``application/octet-stream`` 返回，不执行内容。
    """

    root: Path
    allow_suffixes: tuple[str, ...] = tuple(sorted(_SAFE_SUFFIXES))
    deny_hidden: bool = True
    acl: ProjectAcl = field(default_factory=ProjectAcl)
    max_write_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())
        object.__setattr__(
            self, "allow_suffixes",
            tuple(sorted({str(item).lower() for item in self.allow_suffixes})),
        )

    # ---------------------------------------------------------------- 路径规范化
    @staticmethod
    def normalize(relative_path: str) -> PurePosixPath:
        if not isinstance(relative_path, str) or not relative_path.strip():
            raise FileAccessError("AUTH_FILE_PATH_INVALID", "文件路径不能为空")
        if "\x00" in relative_path:
            raise FileAccessError("AUTH_FILE_PATH_INVALID", "文件路径含 NUL")
        candidate = relative_path.replace("\\", "/").strip()
        if candidate.startswith("/") or (len(candidate) > 1 and candidate[1] == ":"):
            raise FileAccessError("AUTH_FILE_PATH_FORBIDDEN", "禁止绝对路径")
        pure = PurePosixPath(candidate)
        if pure.is_absolute() or pure.anchor:
            raise FileAccessError("AUTH_FILE_PATH_FORBIDDEN", "禁止绝对路径")
        parts = pure.parts
        if not parts:
            raise FileAccessError("AUTH_FILE_PATH_INVALID", "文件路径不能为空")
        for part in parts:
            if part in ("", ".", ".."):
                raise FileAccessError("AUTH_FILE_PATH_FORBIDDEN", "路径穿越被拒绝")
        return pure

    def project_of(self, relative_path: str) -> str:
        pure = self.normalize(relative_path)
        if len(pure.parts) < 2:
            raise FileAccessError("AUTH_FILE_PATH_FORBIDDEN", "文件必须位于项目目录内")
        return pure.parts[0]

    # ---------------------------------------------------------------- 授权
    def _authorize(self, identity: IdentityLike, project_id: str, permission: Permission) -> AccessDecision:
        """唯一的授权入口：文件读写与 API 守卫共用 ``ProjectAcl.decide_identity``。"""
        try:
            decision = self.acl.decide_identity(identity, project_id, permission)
        except AclError as exc:
            raise FileAccessError(exc.code, exc.message, exc.status_code) from None
        if not decision.allowed:
            raise FileAccessError("AUTH_FILE_FORBIDDEN", decision.message, decision.status_code)
        return decision

    def _checked_parts(
        self, identity: IdentityLike, relative_path: str, permission: Permission
    ) -> tuple[PurePosixPath, str, AccessDecision]:
        pure = self.normalize(relative_path)
        if len(pure.parts) < 2:
            raise FileAccessError("AUTH_FILE_PATH_FORBIDDEN", "文件必须位于项目目录内")
        project_id = pure.parts[0]
        if self.deny_hidden and any(part.startswith(".") for part in pure.parts):
            raise FileAccessError("AUTH_FILE_PATH_FORBIDDEN", "禁止访问隐藏文件")
        decision = self._authorize(identity, project_id, permission)
        suffix = pure.suffix.lower()
        if suffix and suffix not in self.allow_suffixes:
            raise FileAccessError("AUTH_FILE_TYPE_FORBIDDEN", "文件类型不在白名单")
        return pure, project_id, decision

    def _inside_root(self, target: Path) -> bool:
        try:
            return os.path.commonpath([str(self.root), str(target)]) == str(self.root)
        except ValueError:  # 不同盘符，必然在 root 之外
            return False

    # ---------------------------------------------------------------- 授权解析
    def resolve(self, identity: IdentityLike, relative_path: str) -> FileGrant:
        pure, project_id, decision = self._checked_parts(identity, relative_path, Permission.READ)
        try:
            target = (self.root / Path(*pure.parts)).resolve(strict=True)
        except (OSError, RuntimeError):
            raise FileAccessError("AUTH_FILE_NOT_FOUND", "文件不存在", status_code=404) from None
        if not self._inside_root(target):
            # 符号链接逃逸或 root 前缀同名目录（如 /root 与 /root-evil）
            raise FileAccessError("AUTH_FILE_PATH_FORBIDDEN", "路径逃逸被拒绝")
        if not target.is_file():
            raise FileAccessError("AUTH_FILE_NOT_FOUND", "文件不存在", status_code=404)

        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return FileGrant(
            path=target,
            media_type=media_type,
            size=target.stat().st_size,
            project_id=project_id,
            role=decision.role,
            decision=decision,
        )

    # ---------------------------------------------------------------- 写入
    def write(
        self,
        identity: IdentityLike,
        relative_path: str,
        data: bytes,
        *,
        max_bytes: int | None = None,
    ) -> FileGrant:
        """受控写入：需 ``WRITE``（EDITOR/OWNER），父目录必须已存在且不得逃逸 root。

        先写同目录临时文件再 ``os.replace``，避免半截文件被读取。
        """
        pure, project_id, decision = self._checked_parts(identity, relative_path, Permission.WRITE)
        if not isinstance(data, (bytes, bytearray)):
            raise FileAccessError("AUTH_FILE_PAYLOAD_INVALID", "文件内容必须是字节流", status_code=400)
        payload = bytes(data)
        limit = self.max_write_bytes if max_bytes is None else max(0, int(max_bytes))
        if len(payload) > limit:
            raise FileAccessError("AUTH_FILE_TOO_LARGE", f"文件超出上限 {limit} 字节", status_code=413)

        parent = (self.root / Path(*pure.parts[:-1]))
        try:
            parent_resolved = parent.resolve(strict=True)
        except (OSError, RuntimeError):
            raise FileAccessError("AUTH_FILE_DIR_NOT_FOUND", "目标目录不存在", status_code=404) from None
        if not self._inside_root(parent_resolved) or not parent_resolved.is_dir():
            raise FileAccessError("AUTH_FILE_PATH_FORBIDDEN", "路径逃逸被拒绝")

        target = parent_resolved / pure.name
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise FileAccessError("AUTH_FILE_PATH_FORBIDDEN", "目标不是普通文件")
        handle, temp_name = tempfile.mkstemp(dir=str(parent_resolved), prefix=".upload-", suffix=".tmp")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, target)
        except OSError:
            Path(temp_name).unlink(missing_ok=True)
            raise FileAccessError("AUTH_FILE_WRITE_FAILED", "文件写入失败", status_code=500) from None

        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return FileGrant(
            path=target,
            media_type=media_type,
            size=len(payload),
            project_id=project_id,
            role=decision.role,
            decision=decision,
        )
