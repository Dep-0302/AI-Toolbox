#!/usr/bin/env python3
"""技能库存看板 —— 清点收藏夹里到底有什么，纯只读。

不解压、不落盘、不修改收藏夹里任何文件。
压缩包里的 SKILL.md 直接在内存里读出来。

用法：  python3 check-inventory.py [--open]
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import html
import json
import os
import re
import stat
import struct
import subprocess
import warnings
import zipfile
from pathlib import Path, PurePosixPath
from typing import NamedTuple

DUMP = Path(
    os.environ.get(
        "AI_TOOLBOX_COLLECTION_ROOT",
        str(Path.home() / "AI-Toolbox-Collection"),
    )
).expanduser()
HOME = Path.home()
HOSTS = {
    "codex": HOME / ".codex" / "skills",
    "claude": HOME / ".claude" / "skills",
    "hermes": HOME / ".hermes" / "skills",
    "workbuddy": HOME / ".workbuddy" / "skills",
}
HOST_LABEL = {"codex": "Codex", "claude": "Claude", "hermes": "Hermes", "workbuddy": "WorkBuddy"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "dist"}
SCRIPT_EXT = {".py", ".sh", ".js", ".mjs", ".ts", ".rb", ".pl", ".command"}
MCP_HINTS = {"mcp.json", ".mcp.json", "server.json"}
OUT = Path(__file__).resolve().parent / "inventory.html"
MAX_ARCHIVE_ENTRIES = 20_000
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 200.0
MAX_ARCHIVE_MANIFEST_READS = 512
MAX_ARCHIVE_CENTRAL_DIRECTORY_BYTES = 16 * 1024 * 1024
MAX_SKILL_BYTES = 512 * 1024
MAX_SCAN_ENTRIES = 100_000
MAX_TOTAL_READ_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_READS = 4_096


class ScanSafetyError(ValueError):
    """The selected tree cannot be observed without crossing a safety boundary."""


class ScanLimitError(ScanSafetyError):
    """A bounded scan exhausted one of its explicit resource limits."""


class RepoObservation(NamedTuple):
    """One repository found during the same anchored collection pass."""

    path: Path
    skills_inside: int


class ScanBudget:
    """One shared budget for a complete candidate-catalog build."""

    def __init__(
        self,
        *,
        max_entries: int = MAX_SCAN_ENTRIES,
        max_read_bytes: int = MAX_TOTAL_READ_BYTES,
        max_manifest_reads: int = MAX_MANIFEST_READS,
    ) -> None:
        self.max_entries = max_entries
        self.max_read_bytes = max_read_bytes
        self.max_manifest_reads = max_manifest_reads
        self.entries = 0
        self.read_bytes = 0
        self.manifest_reads = 0

    def consume_entries(self, amount: int) -> None:
        if amount < 0 or self.entries + amount > self.max_entries:
            raise ScanLimitError(f"扫描条目超过 {self.max_entries} 个")
        self.entries += amount

    def consume_read_bytes(self, amount: int) -> None:
        if amount < 0 or self.read_bytes + amount > self.max_read_bytes:
            raise ScanLimitError(f"扫描读取量超过 {self.max_read_bytes} 字节")
        self.read_bytes += amount

    def consume_manifest(self) -> None:
        if self.manifest_reads + 1 > self.max_manifest_reads:
            raise ScanLimitError(f"manifest 读取超过 {self.max_manifest_reads} 个")
        self.manifest_reads += 1


def _absolute_lexical_path(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _directory_flags() -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _regular_flags() -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        stat.S_IFMT(left.st_mode),
    ) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


def _same_entry_state(left: os.stat_result, right: os.stat_result) -> bool:
    return _same_identity(left, right) and (
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


@contextlib.contextmanager
def _open_directory_path(path: Path | str):
    """Open every absolute-path component without following symlinks."""

    absolute = _absolute_lexical_path(path)
    descriptor = os.open(os.path.sep, _directory_flags())
    try:
        for part in absolute.parts[1:]:
            if part in {"", "."}:
                continue
            before = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                raise ScanSafetyError(f"路径包含软链接：{absolute}")
            if not stat.S_ISDIR(before.st_mode):
                raise ScanSafetyError(f"路径分量不是普通目录：{absolute}")
            child = os.open(part, _directory_flags(), dir_fd=descriptor)
            after = os.fstat(child)
            if not stat.S_ISDIR(after.st_mode) or not _same_identity(before, after):
                os.close(child)
                raise ScanSafetyError(f"目录在检查期间发生变化：{absolute}")
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _open_directory_at(
    parent_fd: int,
    name: str,
    *,
    expected: os.stat_result | None = None,
):
    before = expected or os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode):
        raise ScanSafetyError(f"目录软链接被拒绝：{name}")
    if not stat.S_ISDIR(before.st_mode):
        raise ScanSafetyError(f"不是普通目录：{name}")
    descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    try:
        after = os.fstat(descriptor)
        if not stat.S_ISDIR(after.st_mode) or not _same_identity(before, after):
            raise ScanSafetyError(f"目录在检查期间发生变化：{name}")
        yield descriptor
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _open_regular_at(
    parent_fd: int,
    name: str,
    *,
    expected: os.stat_result | None = None,
):
    before = expected or os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode):
        raise ScanSafetyError(f"文件软链接被拒绝：{name}")
    if not stat.S_ISREG(before.st_mode):
        raise ScanSafetyError(f"不是普通文件：{name}")
    descriptor = os.open(name, _regular_flags(), dir_fd=parent_fd)
    try:
        after = os.fstat(descriptor)
        if not stat.S_ISREG(after.st_mode) or not _same_identity(before, after):
            raise ScanSafetyError(f"文件在检查期间发生变化：{name}")
        yield descriptor, after
    finally:
        os.close(descriptor)


def _read_fd(
    descriptor: int,
    info: os.stat_result,
    *,
    limit: int,
    budget: ScanBudget,
    allow_prefix: bool,
    manifest: bool,
) -> bytes:
    if limit < 0:
        raise ScanLimitError("读取上限无效")
    if not allow_prefix and info.st_size > limit:
        raise ScanLimitError(f"文件超过读取上限 {limit} 字节")
    if manifest:
        budget.consume_manifest()
    planned = min(info.st_size, limit) if allow_prefix else info.st_size
    budget.consume_read_bytes(planned)
    remaining = limit if allow_prefix else limit + 1
    chunks: list[bytes] = []
    while remaining > 0:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    raw = b"".join(chunks)
    if not allow_prefix and len(raw) > limit:
        raise ScanLimitError(f"文件超过读取上限 {limit} 字节")
    after = os.fstat(descriptor)
    if not _same_identity(info, after) or (
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ScanSafetyError("文件在读取期间发生变化")
    return raw


def _read_regular_at(
    parent_fd: int,
    name: str,
    *,
    limit: int,
    budget: ScanBudget,
    expected: os.stat_result | None = None,
    allow_prefix: bool = False,
    manifest: bool = False,
) -> str:
    with _open_regular_at(parent_fd, name, expected=expected) as (descriptor, info):
        raw = _read_fd(
            descriptor,
            info,
            limit=limit,
            budget=budget,
            allow_prefix=allow_prefix,
            manifest=manifest,
        )
    return raw.decode("utf-8", "replace")


def _list_entries(
    descriptor: int,
    budget: ScanBudget,
    *,
    fail_on_symlink: bool,
) -> list[tuple[str, os.stat_result]]:
    try:
        names = sorted(os.listdir(descriptor))
    except OSError as exc:
        raise ScanSafetyError("目录无法稳定枚举") from exc
    budget.consume_entries(len(names))
    entries: list[tuple[str, os.stat_result]] = []
    for name in names:
        try:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as exc:
            raise ScanSafetyError(f"目录项在枚举期间消失：{name}") from exc
        if stat.S_ISLNK(info.st_mode):
            if fail_on_symlink:
                raise ScanSafetyError(f"收藏目录包含软链接：{name}")
            warnings.warn("跳过软链接且未读取目标", RuntimeWarning, stacklevel=3)
            continue
        entries.append((name, info))
    return entries


def _verify_entries_unchanged(
    descriptor: int,
    original: list[tuple[str, os.stat_result]],
    budget: ScanBudget,
    *,
    fail_on_symlink: bool,
) -> None:
    current = _list_entries(descriptor, budget, fail_on_symlink=fail_on_symlink)
    original_by_name = dict(original)
    current_by_name = dict(current)
    if set(original_by_name) != set(current_by_name):
        raise ScanSafetyError("目录成员在扫描期间发生变化")
    if any(
        not _same_entry_state(original_by_name[name], current_by_name[name])
        for name in original_by_name
    ):
        raise ScanSafetyError("目录项在扫描期间发生变化")


def normalize_source_root(source_root: Path | str | None = None) -> Path:
    if source_root is None:
        candidate = DUMP
    elif not os.fspath(source_root).strip():
        raise ScanSafetyError("收藏夹路径不能为空")
    else:
        candidate = source_root
    root = _absolute_lexical_path(candidate)
    try:
        with _open_directory_path(root):
            pass
    except (OSError, ScanSafetyError) as exc:
        raise ScanSafetyError(f"收藏夹路径不是无软链接的普通目录：{root}") from exc
    return root


def read_bounded_text(
    path: Path,
    limit: int = MAX_SKILL_BYTES,
    *,
    budget: ScanBudget | None = None,
    manifest: bool = False,
) -> str:
    effective_budget = budget or ScanBudget()
    absolute = _absolute_lexical_path(path)
    with _open_directory_path(absolute.parent) as parent_fd:
        return _read_regular_at(
            parent_fd,
            absolute.name,
            limit=limit,
            budget=effective_budget,
            manifest=manifest,
        )


def read_bounded_prefix(
    path: Path,
    limit: int,
    *,
    budget: ScanBudget | None = None,
) -> str:
    effective_budget = budget or ScanBudget()
    absolute = _absolute_lexical_path(path)
    with _open_directory_path(absolute.parent) as parent_fd:
        return _read_regular_at(
            parent_fd,
            absolute.name,
            limit=limit,
            budget=effective_budget,
            allow_prefix=True,
        )


def parse_front(text: str) -> tuple[str, str]:
    """从 SKILL.md 取 name / description。取不到就退回首个标题和首段。"""
    name = desc = ""
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if m:
        body = m.group(1)
        for key in ("name", "description"):
            k = re.search(rf"^{key}:\s*(.+?)(?=\n[\w-]+:(?:\s|$)|\Z)", body, re.S | re.M)
            if k:
                v = " ".join(k.group(1).split()).strip("\"'")
                if key == "name":
                    name = v
                else:
                    desc = v
    if not name:
        h = re.search(r"^#\s+(.+)$", text, re.M)
        name = h.group(1).strip() if h else ""
    if not desc:
        for line in text.splitlines():
            s = line.strip()
            if s and not s.startswith(("#", "-", "---", "*", ">", "|")):
                desc = s
                break
    return name, desc


def norm(text: str) -> str:
    """内容指纹：去掉 frontmatter 与空白差异，让改名/换宿主的同一技能能对上。"""
    body = re.sub(r"^---\s*\n.*?\n---\s*\n", "", text, flags=re.S)
    return hashlib.md5(re.sub(r"\s+", " ", body).strip().encode()).hexdigest()


def _normalized_zip_name(raw_name: str) -> str:
    name = raw_name.replace("\\", "/")
    if not name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise ScanSafetyError(f"压缩包包含绝对路径：{raw_name}")
    parts = name.split("/")
    if any(part == ".." for part in parts):
        raise ScanSafetyError(f"压缩包路径越界：{raw_name}")
    normalized_parts = [part for part in parts if part not in {"", "."}]
    if not normalized_parts:
        raise ScanSafetyError(f"压缩包条目路径无效：{raw_name}")
    return PurePosixPath(*normalized_parts).as_posix()


def _zip_info_is_symlink(entry: zipfile.ZipInfo) -> bool:
    mode = (entry.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


def _archive_entry_is_skipped(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return bool(parts and parts[0] == "__MACOSX") or any(part in SKIP_DIRS for part in parts)


_EOCD_SIGNATURE = b"PK\x05\x06"
_ZIP64_EOCD_SIGNATURE = b"PK\x06\x06"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_CENTRAL_ENTRY_SIGNATURE = b"PK\x01\x02"
_EOCD = struct.Struct("<4s4H2IH")
_ZIP64_LOCATOR = struct.Struct("<4sIQI")
_ZIP64_EOCD = struct.Struct("<4sQ2H2I4Q")


def _pread_exact(descriptor: int, offset: int, size: int) -> bytes:
    if offset < 0 or size < 0:
        raise ScanSafetyError("压缩包目录偏移无效")
    try:
        data = os.pread(descriptor, size, offset)
    except (AttributeError, OSError) as exc:
        raise ScanSafetyError("无法安全读取压缩包目录元数据") from exc
    if len(data) != size:
        raise ScanSafetyError("压缩包目录元数据被截断")
    return data


def _find_eocd(descriptor: int, archive_size: int) -> tuple[int, tuple]:
    if archive_size < _EOCD.size:
        raise zipfile.BadZipFile("压缩包缺少 EOCD")
    tail_size = min(archive_size, _EOCD.size + 0xFFFF)
    tail_offset = archive_size - tail_size
    tail = _pread_exact(descriptor, tail_offset, tail_size)
    search_end = len(tail)
    while True:
        index = tail.rfind(_EOCD_SIGNATURE, 0, search_end)
        if index < 0:
            raise zipfile.BadZipFile("压缩包缺少 EOCD")
        if index + _EOCD.size <= len(tail):
            fields = _EOCD.unpack_from(tail, index)
            comment_size = fields[-1]
            if index + _EOCD.size + comment_size == len(tail):
                return tail_offset + index, fields
        search_end = index


def _preflight_zip_directory(descriptor: int, archive_size: int) -> None:
    """Reject oversized/forged central directories before ``ZipFile`` parses them."""

    eocd_offset, fields = _find_eocd(descriptor, archive_size)
    (
        _signature,
        disk_number,
        central_disk,
        entries_on_disk,
        entry_count,
        central_size,
        central_offset,
        _comment_size,
    ) = fields
    central_boundary = eocd_offset

    uses_zip64 = (
        entries_on_disk == 0xFFFF
        or entry_count == 0xFFFF
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
    )
    if uses_zip64:
        locator_offset = eocd_offset - _ZIP64_LOCATOR.size
        locator = _ZIP64_LOCATOR.unpack(
            _pread_exact(descriptor, locator_offset, _ZIP64_LOCATOR.size)
        )
        locator_signature, zip64_disk, zip64_offset, disk_count = locator
        if locator_signature != _ZIP64_LOCATOR_SIGNATURE:
            raise ScanSafetyError("ZIP64 locator 缺失或伪造")
        if zip64_disk != 0 or disk_count != 1:
            raise ScanSafetyError("不支持分卷 ZIP64")
        zip64_fields = _ZIP64_EOCD.unpack(
            _pread_exact(descriptor, zip64_offset, _ZIP64_EOCD.size)
        )
        (
            zip64_signature,
            zip64_record_size,
            _made_by,
            _needed,
            zip64_disk_number,
            zip64_central_disk,
            zip64_entries_on_disk,
            entry_count,
            central_size,
            central_offset,
        ) = zip64_fields
        if zip64_signature != _ZIP64_EOCD_SIGNATURE or zip64_record_size < 44:
            raise ScanSafetyError("ZIP64 EOCD 缺失或伪造")
        if zip64_offset + 12 + zip64_record_size > locator_offset:
            raise ScanSafetyError("ZIP64 EOCD 长度越界")
        if (
            zip64_disk_number != 0
            or zip64_central_disk != 0
            or zip64_entries_on_disk != entry_count
        ):
            raise ScanSafetyError("不支持分卷 ZIP64")
        central_boundary = zip64_offset
    elif disk_number != 0 or central_disk != 0 or entries_on_disk != entry_count:
        raise ScanSafetyError("不支持分卷 ZIP")

    if entry_count > MAX_ARCHIVE_ENTRIES:
        raise ScanLimitError(f"包内条目超过 {MAX_ARCHIVE_ENTRIES} 个")
    if central_size > MAX_ARCHIVE_CENTRAL_DIRECTORY_BYTES:
        raise ScanLimitError(
            f"压缩包中央目录超过 {MAX_ARCHIVE_CENTRAL_DIRECTORY_BYTES} 字节"
        )
    if entry_count == 0:
        if central_size != 0:
            raise ScanSafetyError("空 ZIP 的中央目录大小伪造")
        return
    if central_size < entry_count * 46:
        raise ScanSafetyError("压缩包中央目录与条目数不一致")
    prefix_size = central_boundary - central_size - central_offset
    central_start = central_offset + prefix_size
    if prefix_size < 0 or central_start < 0 or central_start + central_size != central_boundary:
        raise ScanSafetyError("压缩包中央目录偏移越界")
    if _pread_exact(descriptor, central_start, 4) != _CENTRAL_ENTRY_SIGNATURE:
        raise ScanSafetyError("压缩包中央目录签名无效")


def _validate_archive_entries(
    bundle: zipfile.ZipFile,
    budget: ScanBudget,
) -> list[tuple[zipfile.ZipInfo, str]]:
    raw_entries = bundle.infolist()
    if len(raw_entries) > MAX_ARCHIVE_ENTRIES:
        raise ScanLimitError(f"包内条目超过 {MAX_ARCHIVE_ENTRIES} 个")
    budget.consume_entries(len(raw_entries))
    total_uncompressed = 0
    total_compressed = 0
    normalized: list[tuple[zipfile.ZipInfo, str]] = []
    seen: set[str] = set()
    for entry in raw_entries:
        name = _normalized_zip_name(entry.filename)
        if name in seen:
            raise ScanSafetyError(f"压缩包包含重复路径：{name}")
        seen.add(name)
        if _zip_info_is_symlink(entry):
            warnings.warn(
                "跳过压缩包软链接且未读取目标",
                RuntimeWarning,
                stacklevel=3,
            )
            continue
        if entry.flag_bits & 0x1:
            raise ScanSafetyError(f"压缩包包含加密条目：{name}")
        if entry.file_size < 0 or entry.compress_size < 0:
            raise ScanSafetyError(f"压缩包条目大小无效：{name}")
        if not entry.is_dir():
            if entry.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                raise ScanLimitError(f"压缩包单项超过 {MAX_ARCHIVE_MEMBER_BYTES} 字节：{name}")
            total_uncompressed += entry.file_size
            total_compressed += entry.compress_size
            if total_uncompressed > MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES:
                raise ScanLimitError(
                    f"压缩包展开总量超过 {MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES} 字节"
                )
            ratio = entry.file_size / max(1, entry.compress_size)
            if ratio > MAX_ARCHIVE_COMPRESSION_RATIO:
                raise ScanLimitError(f"压缩比超过 {MAX_ARCHIVE_COMPRESSION_RATIO:g}：{name}")
        normalized.append((entry, name))
    if total_uncompressed / max(1, total_compressed) > MAX_ARCHIVE_COMPRESSION_RATIO:
        raise ScanLimitError(f"压缩包总压缩比超过 {MAX_ARCHIVE_COMPRESSION_RATIO:g}")
    return normalized


def _broken_zip_item(name: str, exc: Exception) -> dict:
    message = f"（包读不开：{exc}）"
    return {
        "name": name,
        "desc": message,
        "desc_full": message,
        "fp": "",
        "inner": "",
        "files": 0,
        "scripts": [],
        "mcp": [],
        "broken": True,
        "_text": "",
        "_file_names": [],
    }


def _zip_entries_from_fd(
    descriptor: int,
    archive_name: str,
    budget: ScanBudget,
) -> list[dict]:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise ScanSafetyError(f"压缩包不是普通文件：{archive_name}")
    if info.st_size > MAX_ARCHIVE_BYTES:
        raise ScanLimitError(f"压缩包超过 {MAX_ARCHIVE_BYTES} 字节：{archive_name}")
    budget.consume_read_bytes(info.st_size)
    out: list[dict] = []
    broken: list[dict] | None = None
    try:
        _preflight_zip_directory(descriptor, info.st_size)
        with os.fdopen(os.dup(descriptor), "rb") as source:
            with zipfile.ZipFile(source) as bundle:
                entries = _validate_archive_entries(bundle, budget)
                file_names = [name for entry, name in entries if not entry.is_dir()]
                visible_names = [name for name in file_names if not _archive_entry_is_skipped(name)]
                scripts = sorted(
                    {
                        PurePosixPath(name).suffix.lower()
                        for name in visible_names
                        if PurePosixPath(name).suffix.lower() in SCRIPT_EXT
                    }
                )
                mcp = sorted(
                    {
                        PurePosixPath(name).name
                        for name in visible_names
                        if PurePosixPath(name).name in MCP_HINTS
                    }
                )
                archive_manifest_reads = 0
                for entry, name in entries:
                    if entry.is_dir() or _archive_entry_is_skipped(name):
                        continue
                    if PurePosixPath(name).name != "SKILL.md":
                        continue
                    archive_manifest_reads += 1
                    if archive_manifest_reads > MAX_ARCHIVE_MANIFEST_READS:
                        raise ScanLimitError(
                            f"单个压缩包 manifest 超过 {MAX_ARCHIVE_MANIFEST_READS} 个"
                        )
                    if entry.file_size > MAX_SKILL_BYTES:
                        raise ScanLimitError(f"SKILL.md 超过 {MAX_SKILL_BYTES} 字节：{name}")
                    budget.consume_manifest()
                    budget.consume_read_bytes(entry.file_size)
                    with bundle.open(entry) as stream:
                        raw = stream.read(MAX_SKILL_BYTES + 1)
                    if len(raw) > MAX_SKILL_BYTES or len(raw) != entry.file_size:
                        raise ScanSafetyError(f"压缩包成员读取不稳定：{name}")
                    text = raw.decode("utf-8", "replace")
                    parsed_name, description = parse_front(text)
                    out.append(
                        {
                            "name": parsed_name or PurePosixPath(name).parent.name or archive_name,
                            "desc": description[:220],
                            "desc_full": description,
                            "fp": norm(text),
                            "inner": name,
                            "files": len(file_names),
                            "scripts": scripts,
                            "mcp": mcp,
                            "_text": text,
                            "_file_names": visible_names,
                        }
                    )
    except ScanSafetyError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        broken = [_broken_zip_item(archive_name, exc)]
    after = os.fstat(descriptor)
    if not _same_identity(info, after) or (
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ScanSafetyError(f"压缩包在读取期间发生变化：{archive_name}")
    return broken if broken is not None else out


def _zip_entries_at(
    parent_fd: int,
    name: str,
    *,
    expected: os.stat_result,
    budget: ScanBudget,
) -> list[dict]:
    with _open_regular_at(parent_fd, name, expected=expected) as (descriptor, _info):
        return _zip_entries_from_fd(descriptor, Path(name).stem, budget)


def zip_entries(p: Path, *, budget: ScanBudget | None = None) -> list[dict]:
    """从压缩包里有界读取 SKILL.md，不解压、不跟随路径软链接。"""

    effective_budget = budget or ScanBudget()
    absolute = _absolute_lexical_path(p)
    with _open_directory_path(absolute.parent) as parent_fd:
        info = os.stat(absolute.name, dir_fd=parent_fd, follow_symlinks=False)
        return _zip_entries_at(
            parent_fd,
            absolute.name,
            expected=info,
            budget=effective_budget,
        )


def _directory_file_names(
    descriptor: int,
    budget: ScanBudget,
    *,
    prefix: tuple[str, ...] = (),
    entries: list[tuple[str, os.stat_result]] | None = None,
    fail_on_symlink: bool = True,
) -> list[str]:
    rows = entries if entries is not None else _list_entries(
        descriptor,
        budget,
        fail_on_symlink=fail_on_symlink,
    )
    names: list[str] = []
    for name, info in rows:
        if stat.S_ISLNK(info.st_mode):
            continue
        if stat.S_ISDIR(info.st_mode):
            if name in SKIP_DIRS:
                continue
            with _open_directory_at(descriptor, name, expected=info) as child_fd:
                names.extend(
                    _directory_file_names(
                        child_fd,
                        budget,
                        prefix=(*prefix, name),
                        fail_on_symlink=fail_on_symlink,
                    )
                )
        elif stat.S_ISREG(info.st_mode):
            names.append(PurePosixPath(*prefix, name).as_posix())
    _verify_entries_unchanged(
        descriptor,
        rows,
        budget,
        fail_on_symlink=fail_on_symlink,
    )
    return names


def _dir_entry_from_fd(
    descriptor: int,
    fallback_name: str,
    budget: ScanBudget,
    *,
    entries: list[tuple[str, os.stat_result]] | None = None,
) -> dict:
    rows = entries or _list_entries(descriptor, budget, fail_on_symlink=False)
    by_name = dict(rows)
    manifest_info = by_name.get("SKILL.md")
    if manifest_info is None or not stat.S_ISREG(manifest_info.st_mode):
        raise ScanSafetyError("SKILL.md 不是普通文件")
    text = _read_regular_at(
        descriptor,
        "SKILL.md",
        limit=MAX_SKILL_BYTES,
        budget=budget,
        expected=manifest_info,
        manifest=True,
    )
    parsed_name, description = parse_front(text)
    file_names = _directory_file_names(
        descriptor,
        budget,
        entries=rows,
        fail_on_symlink=False,
    )
    scripts = sorted(
        {PurePosixPath(name).suffix.lower() for name in file_names if PurePosixPath(name).suffix.lower() in SCRIPT_EXT}
    )
    mcp = sorted(
        {PurePosixPath(name).name for name in file_names if PurePosixPath(name).name in MCP_HINTS}
    )
    return {
        "name": parsed_name or fallback_name,
        "desc": description[:220],
        "desc_full": description,
        "fp": norm(text),
        "inner": "",
        "files": len(file_names),
        "scripts": scripts,
        "mcp": mcp,
        "_text": text,
        "_file_names": file_names,
    }


def dir_entry(d: Path, *, budget: ScanBudget | None = None) -> dict:
    effective_budget = budget or ScanBudget()
    directory = normalize_source_root(d)
    with _open_directory_path(directory) as descriptor:
        return _dir_entry_from_fd(descriptor, directory.name, effective_budget)


def _scan_installed_directory(
    descriptor: int,
    directory_name: str,
    host_id: str,
    budget: ScanBudget,
    output: dict[str, dict],
) -> None:
    rows = _list_entries(descriptor, budget, fail_on_symlink=False)
    by_name = dict(rows)
    manifest_info = by_name.get("SKILL.md")
    if manifest_info is not None and stat.S_ISREG(manifest_info.st_mode):
        text = _read_regular_at(
            descriptor,
            "SKILL.md",
            limit=MAX_SKILL_BYTES,
            budget=budget,
            expected=manifest_info,
            manifest=True,
        )
        record = output.setdefault(norm(text), {"name": directory_name, "hosts": set()})
        record["hosts"].add(host_id)
        _verify_entries_unchanged(
            descriptor,
            rows,
            budget,
            fail_on_symlink=False,
        )
        return
    for name, info in rows:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or name in SKIP_DIRS:
            continue
        with _open_directory_at(descriptor, name, expected=info) as child_fd:
            _scan_installed_directory(child_fd, name, host_id, budget, output)
    _verify_entries_unchanged(
        descriptor,
        rows,
        budget,
        fail_on_symlink=False,
    )


def scan_installed(
    *,
    budget: ScanBudget | None = None,
    host_roots: dict[str, Path] | None = None,
) -> dict[str, dict]:
    """已装技能：内容指纹 -> {name, hosts}。宿主内软链接不跟随。"""

    effective_budget = budget or ScanBudget()
    output: dict[str, dict] = {}
    effective_roots = HOSTS if host_roots is None else host_roots
    for host_id, root in effective_roots.items():
        try:
            with _open_directory_path(root) as descriptor:
                _scan_installed_directory(
                    descriptor,
                    _absolute_lexical_path(root).name,
                    host_id,
                    effective_budget,
                    output,
                )
        except FileNotFoundError:
            continue
    return output


def _relative_text(parts: tuple[str, ...]) -> str:
    return PurePosixPath(*parts).as_posix() if parts else "."


def _collect_directory(
    descriptor: int,
    *,
    root: Path,
    relative_parts: tuple[str, ...],
    budget: ScanBudget,
    items: list[dict],
    loose: list[dict],
    repos: list[RepoObservation],
) -> None:
    rows = _list_entries(descriptor, budget, fail_on_symlink=False)
    by_name = dict(rows)
    git_info = by_name.get(".git")
    if git_info is not None:
        if not (stat.S_ISDIR(git_info.st_mode) or stat.S_ISREG(git_info.st_mode)):
            raise ScanSafetyError(".git 不是普通目录或文件")
        # Count repository skills in this same anchored pass.  Returning the
        # count with the observation avoids reopening a mutable path later.
        skills_inside = _count_skill_directories(descriptor, budget, entries=rows)
        repos.append(
            RepoObservation(
                path=root.joinpath(*relative_parts),
                skills_inside=skills_inside,
            )
        )
        return

    manifest_info = by_name.get("SKILL.md")
    if manifest_info is not None:
        if not stat.S_ISREG(manifest_info.st_mode):
            raise ScanSafetyError("SKILL.md 不是普通文件")
        entry = _dir_entry_from_fd(
            descriptor,
            relative_parts[-1] if relative_parts else root.name,
            budget,
            entries=rows,
        )
        entry.update(src=_relative_text(relative_parts), kind="已解包", size=0)
        items.append(entry)
        return

    for name, info in rows:
        if not stat.S_ISREG(info.st_mode):
            continue
        relative = (*relative_parts, name)
        relative_text = _relative_text(relative)
        suffix = Path(name).suffix.lower()
        if suffix in {".zip", ".skill"}:
            for entry in _zip_entries_at(
                descriptor,
                name,
                expected=info,
                budget=budget,
            ):
                entry.update(src=relative_text, kind="压缩包", size=info.st_size)
                items.append(entry)
        elif suffix in {".md", ".txt"} and name != "SKILL.md":
            if len(relative) <= 2 and info.st_size > 2000:
                text = _read_regular_at(
                    descriptor,
                    name,
                    limit=6000,
                    budget=budget,
                    expected=info,
                    allow_prefix=True,
                )
                parsed_name, description = parse_front(text)
                loose.append(
                    {
                        "name": parsed_name or Path(name).stem,
                        "desc": description[:220],
                        "src": relative_text,
                        "size": info.st_size,
                    }
                )

    for name, info in rows:
        if not stat.S_ISDIR(info.st_mode) or name in SKIP_DIRS:
            continue
        with _open_directory_at(descriptor, name, expected=info) as child_fd:
            _collect_directory(
                child_fd,
                root=root,
                relative_parts=(*relative_parts, name),
                budget=budget,
                items=items,
                loose=loose,
                repos=repos,
            )
    _verify_entries_unchanged(
        descriptor,
        rows,
        budget,
        fail_on_symlink=False,
    )


def collect(
    source_root: Path | str | None = None,
    *,
    budget: ScanBudget | None = None,
) -> tuple[list[dict], list[dict], list[RepoObservation]]:
    root = normalize_source_root(source_root)
    effective_budget = budget or ScanBudget()
    items: list[dict] = []
    loose: list[dict] = []
    repos: list[RepoObservation] = []
    with _open_directory_path(root) as descriptor:
        _collect_directory(
            descriptor,
            root=root,
            relative_parts=(),
            budget=effective_budget,
            items=items,
            loose=loose,
            repos=repos,
        )
    return items, loose, repos


def repo_roots(
    source_root: Path | str | None = None,
    *,
    budget: ScanBudget | None = None,
) -> list[Path]:
    """clone 下来的仓库根目录 —— 里面的 skill 是参考资料，不是你的收藏。"""

    return [repo.path for repo in collect(source_root, budget=budget)[2]]


@contextlib.contextmanager
def _open_relative_directory(root_fd: int, relative_parts: tuple[str, ...]):
    descriptor = os.dup(root_fd)
    try:
        for part in relative_parts:
            if part in {"", ".", ".."} or "/" in part:
                raise ScanSafetyError("相对目录路径无效")
            before = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            child = os.open(part, _directory_flags(), dir_fd=descriptor)
            after = os.fstat(child)
            if stat.S_ISLNK(before.st_mode) or not _same_identity(before, after):
                os.close(child)
                raise ScanSafetyError(f"目录路径不稳定：{part}")
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _count_skill_directories(
    descriptor: int,
    budget: ScanBudget,
    *,
    entries: list[tuple[str, os.stat_result]] | None = None,
) -> int:
    rows = entries if entries is not None else _list_entries(
        descriptor,
        budget,
        fail_on_symlink=False,
    )
    by_name = dict(rows)
    manifest = by_name.get("SKILL.md")
    if manifest is not None:
        if not stat.S_ISREG(manifest.st_mode):
            raise ScanSafetyError("SKILL.md 不是普通文件")
        _verify_entries_unchanged(
            descriptor,
            rows,
            budget,
            fail_on_symlink=False,
        )
        return 1
    count = 0
    for name, info in rows:
        if not stat.S_ISDIR(info.st_mode) or name in SKIP_DIRS:
            continue
        with _open_directory_at(descriptor, name, expected=info) as child_fd:
            count += _count_skill_directories(child_fd, budget)
    _verify_entries_unchanged(
        descriptor,
        rows,
        budget,
        fail_on_symlink=False,
    )
    return count


def count_repo_skills(
    source_root: Path | str,
    repo_path: Path,
    *,
    budget: ScanBudget | None = None,
) -> int:
    root = normalize_source_root(source_root)
    repo = _absolute_lexical_path(repo_path)
    try:
        relative = repo.relative_to(root)
    except ValueError as exc:
        raise ScanSafetyError("仓库路径越出收藏根") from exc
    effective_budget = budget or ScanBudget()
    with _open_directory_path(root) as root_fd:
        with _open_relative_directory(root_fd, relative.parts) as repo_fd:
            return _count_skill_directories(repo_fd, effective_budget)


def read_skill_text_from_source(
    source: Path,
    inner: str = "",
    *,
    budget: ScanBudget | None = None,
) -> str:
    effective_budget = budget or ScanBudget()
    absolute = _absolute_lexical_path(source)
    if absolute.suffix.lower() in {".zip", ".skill"}:
        target = _normalized_zip_name(inner)
        for entry in zip_entries(absolute, budget=effective_budget):
            if entry.get("inner") == target:
                return entry.get("_text", "")
        return ""
    return read_bounded_text(
        absolute / "SKILL.md",
        MAX_SKILL_BYTES,
        budget=effective_budget,
        manifest=True,
    )


def package_file_names(
    source: Path,
    *,
    budget: ScanBudget | None = None,
) -> list[str]:
    effective_budget = budget or ScanBudget()
    absolute = _absolute_lexical_path(source)
    if absolute.suffix.lower() in {".zip", ".skill"}:
        entries = zip_entries(absolute, budget=effective_budget)
        return list(entries[0].get("_file_names", [])) if entries else []
    directory = normalize_source_root(absolute)
    with _open_directory_path(directory) as descriptor:
        return _directory_file_names(descriptor, effective_budget, fail_on_symlink=False)


CSS = """
:root{--bg:#fbfaf8;--card:#fff;--line:#e6e2dc;--ink:#1f1d1b;--dim:#78716c;
--amber:#b45309;--amber-bg:#fdf6e8;--green:#15803d;--green-bg:#eef7f0;
--blue:#1d4ed8;--blue-bg:#eef2ff;--gray-bg:#f4f2ef;}
*{box-sizing:border-box}
body{margin:0;padding:40px 24px 80px;background:var(--bg);color:var(--ink);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif}
.wrap{max-width:940px;margin:0 auto}
h1{font-size:26px;margin:0 0 6px;letter-spacing:-.01em}
.sub{color:var(--dim);font-size:13px;margin-bottom:28px}
.verdict{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:22px 24px;margin-bottom:34px}
.verdict .big{font-size:19px;font-weight:600;margin-bottom:8px}
.verdict p{margin:0;color:var(--dim);font-size:14px}
h2{font-size:15px;margin:38px 0 6px;padding-bottom:8px;border-bottom:1px solid var(--line);
color:var(--dim);font-weight:600;letter-spacing:.02em}
.note{color:var(--dim);font-size:13px;margin:0 0 14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:15px 18px;margin-bottom:9px}
.card.dup{background:var(--amber-bg);border-color:#f0dcb4}
.card.done{background:var(--gray-bg);opacity:.85}
.nm{font-weight:600;font-size:15.5px;margin-bottom:4px}
.ds{color:#57534e;font-size:13.5px;margin-bottom:8px}
.meta{color:var(--dim);font-size:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.meta code{background:#f2efea;padding:1px 7px;border-radius:4px;font-size:11.5px;
font-family:ui-monospace,Menlo,monospace}
.tag{font-size:11px;padding:2px 8px;border-radius:20px;font-weight:600}
.tag.risk{background:#fbe4e2;color:#9b1c1c}
.tag.mcp{background:var(--blue-bg);color:var(--blue)}
.tag.ok{background:var(--green-bg);color:var(--green)}
.tag.dup{background:#fbe9c8;color:var(--amber)}
.dupgroup{margin-bottom:9px}
.dupgroup .sub2{color:var(--amber);font-size:12px;font-weight:600;margin-bottom:5px}
.dupgroup .card{margin-bottom:5px}
footer{margin-top:56px;color:var(--dim);font-size:12px;border-top:1px solid var(--line);padding-top:16px}
footer code{background:#f2efea;padding:1px 6px;border-radius:4px}
"""


def tags(e: dict) -> str:
    t = []
    if e.get("scripts"):
        t.append(f"<span class='tag risk'>含脚本 {' '.join(e['scripts'])}</span>")
    if e.get("mcp"):
        t.append("<span class='tag mcp'>带 MCP 配置</span>")
    return "".join(t)


def card(e: dict, cls: str = "", extra: str = "") -> str:
    return (f"<div class='card {cls}'><div class='nm'>{html.escape(e['name'])}</div>"
            f"<div class='ds'>{html.escape(e.get('desc') or '（没有描述）')}</div>"
            f"<div class='meta'><code>{html.escape(e['src'])}</code>"
            f"<span>{e.get('files', 0)} 个文件</span>{tags(e)}{extra}</div></div>")


def render(items, loose, repos, installed, source_root: Path | str | None = None) -> str:
    root = normalize_source_root(source_root)
    groups: dict[str, list[dict]] = {}
    for e in items:
        groups.setdefault(e["fp"] or e["src"], []).append(e)

    done, dup, fresh = [], [], []
    for fp, g in groups.items():
        if fp in installed:
            done.append((installed[fp], g))
        elif len(g) > 1:
            dup.append(g)
        else:
            fresh.append(g[0])
    fresh.sort(key=lambda x: x["name"])
    dup.sort(key=lambda g: g[0]["name"])

    P = ['<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width,initial-scale=1">',
         "<title>技能库存看板</title><style>" + CSS + "</style></head><body><div class='wrap'>"]
    P.append("<h1>技能库存看板</h1>")
    P.append(f"<div class='sub'>{datetime.datetime.now():%Y-%m-%d %H:%M} · 只读清点 "
             f"<code>{html.escape(root.name)}</code> 收藏夹，未解压任何文件</div>")

    risky = sum(1 for e in items if e.get("scripts"))
    P.append(
        "<div class='verdict'><div class='big'>"
        f"收藏夹里一共 {len(groups)} 个不重复的技能，其中 {len(fresh)} 个是真·还没装的新货</div>"
        f"<p>另有 {len(dup)} 组是同一个技能的多份拷贝（挑一份装就行），"
        f"{len(done)} 个其实已经装在宿主里了。{risky} 个带可执行脚本 —— 这些装之前值得点开看一眼。"
        f"另有 {len(loose)} 份散装文档还不是 skill 格式，{len(repos)} 个是 clone 来的仓库（参考资料，别装）。</p></div>")

    P.append("<h2>可以直接上架 · 还没装过</h2>")
    P.append("<p class='note'>按你自己的判断挑，装一个验一个。带红标的先看脚本内容。</p>")
    for e in fresh:
        P.append(card(e))

    if dup:
        P.append("<h2>同一个技能的多份拷贝 · 每组挑一份</h2>")
        P.append("<p class='note'>内容指纹相同（已忽略改名和 frontmatter 差异），留最新的那份就好。</p>")
        for g in dup:
            P.append(f"<div class='dupgroup'><div class='sub2'>{html.escape(g[0]['name'])} —— {len(g)} 份</div>")
            for e in g:
                P.append(card(e, "dup"))
            P.append("</div>")

    if done:
        P.append("<h2>已经装过了 · 不用再装</h2>")
        for inst, g in sorted(done, key=lambda x: x[0]["name"]):
            hosts = "、".join(HOST_LABEL[h] for h in sorted(inst["hosts"]))
            for e in g:
                P.append(card(e, "done", f"<span class='tag ok'>已在 {hosts}</span>"))

    if loose:
        P.append("<h2>散装文档 · 想用得先转成 skill 格式</h2>")
        P.append("<p class='note'>这些是 .md/.txt，没有 SKILL.md 结构，宿主认不出来。"
                 "你机器上已经装了 skill-factory / skill-forge，可以拿它们来转。</p>")
        for e in sorted(loose, key=lambda x: -x["size"]):
            P.append(f"<div class='card'><div class='nm'>{html.escape(e['name'])}</div>"
                     f"<div class='ds'>{html.escape(e['desc'] or '')}</div>"
                     f"<div class='meta'><code>{html.escape(e['src'])}</code>"
                     f"<span>{e['size']/1024:.0f} KB</span></div></div>")

    if repos:
        P.append("<h2>clone 来的仓库 · 参考资料，别装</h2>")
        P.append("<p class='note'>整个仓库里的示例技能不是给你装的，需要哪个单独拎出来。</p>")
        for repo in repos:
            P.append(
                "<div class='card done'><div class='nm'>"
                f"{html.escape(str(repo.path.relative_to(root)))}"
                "</div></div>"
            )

    P.append("<footer>由 check-inventory.py 只读生成，不解压、不修改收藏夹。"
             "重跑：<code>python3 check-inventory.py --open</code></footer></div></body></html>")
    return "".join(P)


def main() -> int:
    parser = argparse.ArgumentParser(description="只读清点收藏文件夹")
    parser.add_argument("--source-root", default=str(DUMP))
    parser.add_argument("--open", action="store_true")
    arguments = parser.parse_args()
    try:
        source_root = normalize_source_root(arguments.source_root)
    except ValueError as exc:
        print(exc)
        return 1
    budget = ScanBudget()
    items, loose, repos = collect(source_root, budget=budget)
    installed = scan_installed(budget=budget)
    OUT.write_text(render(items, loose, repos, installed, source_root), encoding="utf-8")
    print(f"清点到 {len(items)} 个技能条目、{len(loose)} 份散装文档、{len(repos)} 个仓库")
    print(f"看板 → {OUT}")
    if arguments.open:
        subprocess.run(["open", str(OUT)], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
