"""DWG 授权转换适配层。

DWG 是 Autodesk 私有二进制格式，本项目**不自研逆向解析**，一律通过外部授权
转换器（ODA File Converter / AutoCAD 脚本 / 商业 SDK CLI）转成 DXF 后再解析。

配置方式（环境变量，或显式传入 ``command``）::

    TXBUDDY_DWG_CONVERTER = "C:\\ODA\\ODAFileConverter.exe --in {input} --out {output}"
    TXBUDDY_DWG_TIMEOUT   = "180"      # 秒，可选，默认 120

* 命令模板必须是参数数组语义（按 shell 规则切分），必须含 ``{input}`` / ``{output}``；
  不走 ``shell=True``，避免注入。
* 未配置 → ``status=BLOCKED`` + ``blocked_reason``，**不返回任何伪造文本块**。
* 配置了但执行失败/超时/无输出 → ``status=FAILED``，带上退出码与 stderr 摘要。
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .base import (
    ConverterFailed,
    ConverterNotConfigured,
    ParsedDocument,
    ParseStatus,
    SourceNotFound,
    normalise_suffix,
    sha256_file,
    source_facts,
)
from .dxf_parser import parse_dxf

__all__ = [
    "CONVERTER_ENV",
    "TIMEOUT_ENV",
    "ConverterConfig",
    "DwgConverterAdapter",
    "split_command",
]

CONVERTER_ENV = "TXBUDDY_DWG_CONVERTER"
TIMEOUT_ENV = "TXBUDDY_DWG_TIMEOUT"
DEFAULT_TIMEOUT = 120.0
_STDERR_LIMIT = 800


def split_command(command: str) -> list[str]:
    """把命令模板切成参数数组；Windows 下保留反斜杠路径。"""
    posix = os.name != "nt"
    parts = shlex.split(command, posix=posix)
    cleaned: list[str] = []
    for part in parts:
        if len(part) >= 2 and part[0] == part[-1] and part[0] in {'"', "'"}:
            part = part[1:-1]
        cleaned.append(part)
    return cleaned


@dataclass(frozen=True)
class ConverterConfig:
    """外部转换器配置。``command`` 为含 ``{input}``/``{output}`` 的模板。"""

    command: str
    timeout: float = DEFAULT_TIMEOUT
    name: str = "external-dwg-converter"

    def __post_init__(self) -> None:
        if "{input}" not in self.command:
            raise ConverterNotConfigured(f"{CONVERTER_ENV} 命令模板缺少 {{input}}: {self.command!r}")
        if "{output}" not in self.command:
            raise ConverterNotConfigured(f"{CONVERTER_ENV} 命令模板缺少 {{output}}: {self.command!r}")
        if self.timeout <= 0:
            raise ConverterNotConfigured("DWG 转换超时时间必须大于 0")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ConverterConfig | None":
        env = os.environ if environ is None else environ
        raw = (env.get(CONVERTER_ENV) or "").strip()
        if not raw:
            return None
        timeout_raw = (env.get(TIMEOUT_ENV) or "").strip()
        timeout = DEFAULT_TIMEOUT
        if timeout_raw:
            try:
                timeout = float(timeout_raw)
            except ValueError as exc:
                raise ConverterNotConfigured(
                    f"{TIMEOUT_ENV} 不是合法数字: {timeout_raw!r}"
                ) from exc
        return cls(command=raw, timeout=timeout)


class DwgConverterAdapter:
    """把 DWG 交给外部授权转换器，再复用 DXF 解析器。"""

    def __init__(
        self,
        config: ConverterConfig | None = None,
        *,
        command: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        keep_artifacts: bool = False,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if config is None and command is not None:
            config = ConverterConfig(command=command, timeout=timeout)
        if config is None:
            config = ConverterConfig.from_env(environ)
        self.config = config
        self.keep_artifacts = keep_artifacts

    # ------------------------------------------------------------------ 状态
    @property
    def configured(self) -> bool:
        return self.config is not None

    def describe(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "env_var": CONVERTER_ENV,
            "timeout_env": TIMEOUT_ENV,
            "command": self.config.command if self.config else None,
            "timeout": self.config.timeout if self.config else None,
        }

    # ---------------------------------------------------------------- 转换执行
    def _build_argv(self, source: Path, output: Path) -> list[str]:
        assert self.config is not None
        tokens = split_command(self.config.command)
        if not tokens:
            raise ConverterNotConfigured("DWG 转换命令为空")
        argv = [
            token.replace("{input}", str(source)).replace("{output}", str(output))
            for token in tokens
        ]
        executable = argv[0].lower()
        if executable.endswith((".bat", ".cmd")):
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            argv = [comspec, "/c", *argv]
        return argv

    def convert(self, source_path: str | Path, output_path: str | Path) -> Path:
        """调用外部转换器，成功返回输出路径；失败抛 ConverterFailed。"""
        if self.config is None:
            raise ConverterNotConfigured(
                f"未配置 DWG 转换器（环境变量 {CONVERTER_ENV}），DWG 解析 BLOCKED"
            )
        source = Path(source_path)
        output = Path(output_path)
        if not source.is_file():
            raise SourceNotFound(f"源文件不存在: {source}")
        output.parent.mkdir(parents=True, exist_ok=True)
        argv = self._build_argv(source, output)
        try:
            completed = subprocess.run(  # noqa: S603 - 参数数组，shell=False
                argv,
                capture_output=True,
                text=True,
                timeout=self.config.timeout,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ConverterFailed(
                f"DWG 转换超时（>{self.config.timeout:g}s）: {argv[0]}"
            ) from exc
        except OSError as exc:
            raise ConverterFailed(f"DWG 转换器无法启动: {type(exc).__name__}: {exc}") from exc

        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()[:_STDERR_LIMIT]
            raise ConverterFailed(
                f"DWG 转换器退出码 {completed.returncode}: {stderr or '<无 stderr>'}"
            )
        if not output.is_file() or output.stat().st_size == 0:
            raise ConverterFailed(f"DWG 转换器未产出有效文件: {output}")
        return output

    def _cleanup(self, temp_dir: Path) -> None:
        """清理临时转换产物；keep_artifacts=True 时保留供审计。"""
        if self.keep_artifacts:
            return
        shutil.rmtree(temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------ 解析入口
    def parse(self, path: str | Path) -> ParsedDocument:
        source = Path(path)
        digest, size = source_facts(path)
        base = ParsedDocument(
            source=str(source),
            format="dwg",
            status=ParseStatus.BLOCKED,
            sha256=digest,
            size_bytes=size,
            metadata={"adapter": self.describe(), "original_preserved": True},
            converter=self.config.name if self.config else None,
        )
        if normalise_suffix(source) != "dwg":
            return ParsedDocument(
                source=str(source), format=normalise_suffix(source),
                status=ParseStatus.FAILED, sha256=digest, size_bytes=size,
                error=f"DwgConverterAdapter 只接受 .dwg，收到 {source.suffix!r}",
                metadata=base.metadata,
            )
        if self.config is None:
            base.blocked_reason = (
                f"未配置 DWG 转换器：请设置环境变量 {CONVERTER_ENV} 为授权转换器命令模板"
                "（含 {input}/{output} 占位符）。DWG 为专有格式，本项目不自研逆向解析。"
            )
            base.warnings.append("DWG 解析 BLOCKED：外部授权转换器未配置")
            return base

        temp_dir = Path(tempfile.mkdtemp(prefix="txbuddy-dwg-"))
        output = temp_dir / f"{source.stem}.dxf"
        try:
            self.convert(source, output)
        except ConverterNotConfigured as exc:
            self._cleanup(temp_dir)
            base.blocked_reason = str(exc)
            return base
        except ConverterFailed as exc:
            self._cleanup(temp_dir)
            return ParsedDocument(
                source=str(source), format="dwg", status=ParseStatus.FAILED,
                sha256=digest, size_bytes=size, error=str(exc),
                metadata=base.metadata, converter=base.converter,
                warnings=[f"转换器: {self.config.command}"],
            )

        dxf_sha = sha256_file(output)
        dxf_size = output.stat().st_size
        parsed = parse_dxf(output)
        blocks = parsed.blocks
        warnings = list(parsed.warnings)
        if parsed.status is ParseStatus.FAILED:
            warnings.append(f"转换产物 DXF 解析失败: {parsed.error}")

        metadata = {
            **base.metadata,
            "conversion": {
                "converted_suffix": ".dxf",
                "converted_sha256": dxf_sha,
                "converted_size_bytes": dxf_size,
                "artifact_kept": self.keep_artifacts,
                "artifact_path": str(output) if self.keep_artifacts else None,
                "dxf_metadata": parsed.metadata,
            },
        }
        if not self.keep_artifacts:
            self._cleanup(temp_dir)

        status = ParseStatus.PARSED if parsed.status is ParseStatus.PARSED else ParseStatus.FAILED
        return ParsedDocument(
            source=str(source),
            format="dwg",
            status=status,
            blocks=blocks,
            sha256=digest,
            size_bytes=size,
            metadata=metadata,
            warnings=warnings,
            error=None if status is ParseStatus.PARSED else parsed.error,
            converter=self.config.name,
        )
