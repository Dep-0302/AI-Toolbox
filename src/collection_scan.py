"""Read-only inventory for the user's manually collected AI assets.

The collection catalog is intentionally separate from ``toolbox_scan``:
``toolbox_scan`` observes host-installed capabilities, while this module indexes
the user's source collection without claiming installation, activation, or use.
It never extracts archives, executes collected code, follows collection
symlinks, or writes outside the caller-owned generated snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import stat
import sys
import zipfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator
from urllib.parse import urlsplit


SCHEMA_VERSION = 3
DEFAULT_SOURCE_ROOT = Path(
    os.environ.get(
        "AI_TOOLBOX_COLLECTION_ROOT",
        str(Path.home() / "AI-Toolbox-Collection"),
    )
).expanduser()
DEFAULT_TAXONOMY_PATH = Path(__file__).resolve().parents[1] / "registry" / "collection_taxonomy.json"
DEFAULT_HOST_ROOTS = {
    "codex": Path.home() / ".codex" / "skills",
    "claude": Path.home() / ".claude" / "skills",
    "hermes": Path.home() / ".hermes" / "skills",
    "workbuddy": Path.home() / ".workbuddy" / "skills",
}
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".venv",
        "venv",
        "node_modules",
        "target",
        "dist",
        "build",
    }
)
IGNORE_FILES = frozenset({".DS_Store"})
DOCUMENT_SUFFIXES = frozenset({".md", ".txt", ".pdf", ".doc", ".docx", ".html", ".htm"})
SCRIPT_SUFFIXES = frozenset({".py", ".sh", ".js", ".mjs", ".ts", ".command", ".ps1"})
ZIP_ARCHIVE_SUFFIXES = frozenset({".zip", ".skill"})
OPAQUE_ARCHIVE_SUFFIXES = frozenset({".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz"})
MEDIA_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".mp3", ".wav", ".mp4", ".mov"})
CONFIG_SUFFIXES = frozenset({".json", ".yaml", ".yml", ".toml", ".conf", ".ini"})
MAX_MANIFEST_BYTES = 512 * 1024
MAX_MANIFEST_READS = 512
MAX_ARCHIVE_ENTRIES = 20_000
MAX_ARCHIVE_MEMBER_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 100.0
MAX_SCAN_ENTRIES = 100_000
SOURCE_STATE_VERSION = 1
INPUT_STATE_VERSION = 1
COLLECTION_SCANNER_REVISION = "2026-08-11-anchored-io-v3"
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SKILL_FRONTMATTER = re.compile(
    r"\A---\s*\n(?=[\s\S]{0,12000}?^name\s*:)(?=[\s\S]{0,12000}?^description\s*:)",
    re.M,
)
WORKFLOW_SIGNAL = re.compile(
    r"(?:workflow|pipeline|orchestrat|router|routing|hub|编排|路由|流水线|总控)",
    re.I,
)
CLI_FILENAME_SIGNAL = re.compile(r"(?:^|[-_])cli(?:$|[-_])", re.I)
EXPLICIT_VERSION_SIGNAL = re.compile(
    r"(?:^|[\s_.(+-])v(?:ersion)?[\s._-]*(\d+(?:\.\d+){0,3}(?:[-+][0-9a-z.-]+)?)",
    re.I,
)
ORIGIN_KINDS = frozenset({"github", "website", "chat", "local", "unknown"})
ORIGIN_BASES = frozenset(
    {"manual", "git_remote", "download_metadata", "collection_path", "unavailable"}
)
CHAT_PATH_SEGMENTS = frozenset({"inbox", "微信", "微信文件", "聊天", "聊天记录"})
WHERE_FROM_XATTR = "com.apple.metadata:kMDItemWhereFroms"
MAX_ORIGIN_METADATA_BYTES = 64 * 1024
CLASSIFICATION_TYPES = frozenset(
    {
        "project",
        "skill",
        "skill_bundle",
        "plugin",
        "workflow",
        "agent_collection",
        "prompt_pack",
        "knowledge_base",
        "case_archive",
        "document",
        "tool",
        "resource",
        "composite_asset",
        "unknown",
    }
)
CLASSIFICATION_STATUSES = frozenset({"classified", "reviewed", "needs_review"})
CLASSIFICATION_CONFIDENCE = frozenset({"high", "medium", "low"})
ENTRY_STATUSES = frozenset({"standard", "candidate", "supporting"})
CAPABILITY_TYPES = frozenset(
    {
        "agent_collection",
        "agent_profile",
        "case_study",
        "cli",
        "document",
        "knowledge_base",
        "mcp",
        "plugin",
        "prompt",
        "prompt_pack",
        "resource",
        "script",
        "skill",
        "skill_candidate",
        "workflow",
    }
)
READINESS_VALUES = frozenset({"not_assessed", "needs_adaptation", "reference_only"})
MOVE_SAFETY_VALUES = frozenset({"review", "keep_anchor"})
RELATION_TYPES = frozenset(
    {"packaged_as", "packaged_copy_of", "portable_variant", "supersedes"}
)
SCAN_ERROR_CODES = frozenset(
    {
        "archive_compression_ratio",
        "archive_duplicate_path",
        "archive_encrypted_entry",
        "archive_entry_limit",
        "archive_member_too_large",
        "archive_path_invalid",
        "archive_total_size_limit",
        "archive_unreadable",
        "entry_limit",
        "manifest_read_limit",
        "manifest_too_large",
        "manifest_unreadable",
        "source_root_missing",
        "source_root_unsafe",
        "symlink_skipped",
    }
)
FUNCTIONAL_TYPES = frozenset(
    {"skill", "project", "composite_skill_bundle", "plugin", "workflow", "other_material"}
)
SUPPORTING_ENTRY_DIRS = frozenset(
    {
        "asset",
        "assets",
        "example",
        "examples",
        "fixture",
        "fixtures",
        "reference",
        "references",
        "template",
        "templates",
        "test",
        "tests",
    }
)


class _UnsafeCollectionSource(ValueError):
    """Raised when the authorized tree cannot be held inside its root."""


class _CollectionEntryLimit(ValueError):
    """Raised when a source tree exceeds the bounded observation budget."""


@dataclass(frozen=True)
class _AnchoredEntry:
    relative_path: str
    kind: str
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(
        cls,
        relative_path: str,
        kind: str,
        info: os.stat_result,
    ) -> "_AnchoredEntry":
        return cls(
            relative_path=relative_path,
            kind=kind,
            device=info.st_dev,
            inode=info.st_ino,
            mode=info.st_mode,
            size=info.st_size,
            mtime_ns=info.st_mtime_ns,
            ctime_ns=info.st_ctime_ns,
        )

    def matches(self, info: os.stat_result, *, metadata: bool = False) -> bool:
        if (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)) != (
            self.device,
            self.inode,
            stat.S_IFMT(self.mode),
        ):
            return False
        if metadata and (
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        ) != (self.size, self.mtime_ns, self.ctime_ns):
            return False
        return True


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _regular_open_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _normalized_relative_parts(relative_path: str) -> tuple[str, ...]:
    candidate = PurePosixPath(relative_path)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise _UnsafeCollectionSource("collection relative path is unsafe")
    return tuple(candidate.parts)


def _open_absolute_directory(path: Path) -> int:
    """Open an absolute directory one no-follow segment at a time."""

    absolute = path.expanduser().absolute()
    if not absolute.is_absolute():
        raise _UnsafeCollectionSource("collection source root must be absolute")
    parts = absolute.parts
    if not parts or parts[0] != os.sep or any(part in {"", ".", ".."} for part in parts[1:]):
        raise _UnsafeCollectionSource("collection source root path is unsafe")
    descriptor = os.open(os.sep, _directory_open_flags())
    try:
        for part in parts[1:]:
            next_descriptor = os.open(part, _directory_open_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise _UnsafeCollectionSource("collection source root is not a directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class _AnchoredTree:
    """Immutable metadata view anchored to a no-follow root descriptor.

    Enumeration uses directory file descriptors, and every later read reopens
    each path segment relative to that descriptor while matching the recorded
    device/inode.  Replacements therefore invalidate the scan instead of
    redirecting it outside the authorized collection root.
    """

    def __init__(self, root: Path):
        self.root = root.expanduser().absolute()
        self._root_fd = _open_absolute_directory(self.root)
        root_info = os.fstat(self._root_fd)
        self.root_entry = _AnchoredEntry.from_stat("", "directory", root_info)
        self.entries: dict[str, _AnchoredEntry] = {"": self.root_entry}
        self._children: defaultdict[str, list[str]] = defaultdict(list)
        self.symlinks: dict[str, str] = {}
        self.warnings: list[dict[str, str]] = []
        self._closed = False
        try:
            self._snapshot_directory("", os.dup(self._root_fd), [0])
            self.verify_stable()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> "_AnchoredTree":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            os.close(self._root_fd)
            self._closed = True

    @staticmethod
    def _join(parent: str, name: str) -> str:
        return f"{parent}/{name}" if parent else name

    def _snapshot_directory(self, relative: str, descriptor: int, counter: list[int]) -> None:
        try:
            with os.scandir(descriptor) as iterator:
                rows = sorted(iterator, key=lambda row: row.name)
            for row in rows:
                name = row.name
                if name in {".", ".."} or name in IGNORE_FILES:
                    continue
                child_relative = self._join(relative, name)
                try:
                    info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                except OSError as exc:
                    raise _UnsafeCollectionSource("collection entry changed during enumeration") from exc
                if stat.S_ISLNK(info.st_mode):
                    # Descendant aliases are observable but never resolved,
                    # opened, or traversed. One alias must not make the user's
                    # entire default collection unavailable.
                    counter[0] += 1
                    if counter[0] > MAX_SCAN_ENTRIES:
                        raise _CollectionEntryLimit("collection entry budget exceeded")
                    self.warnings.append(
                        {"code": "symlink_skipped", "path": child_relative}
                    )
                    try:
                        self.symlinks[child_relative] = os.readlink(name, dir_fd=descriptor)
                    except OSError as exc:
                        raise _UnsafeCollectionSource(
                            "collection symlink changed during enumeration"
                        ) from exc
                    continue
                if stat.S_ISDIR(info.st_mode):
                    child_fd = os.open(name, _directory_open_flags(), dir_fd=descriptor)
                    try:
                        opened_info = os.fstat(child_fd)
                        entry = _AnchoredEntry.from_stat(child_relative, "directory", opened_info)
                        if not entry.matches(info):
                            raise _UnsafeCollectionSource(
                                "collection directory changed during enumeration"
                            )
                        counter[0] += 1
                        if counter[0] > MAX_SCAN_ENTRIES:
                            raise _CollectionEntryLimit("collection entry budget exceeded")
                        if name == ".git":
                            self.entries[child_relative] = entry
                            self._children[relative].append(child_relative)
                            self._snapshot_git_config(child_relative, child_fd, counter)
                            continue
                        if name in SKIP_DIRS:
                            continue
                        self.entries[child_relative] = entry
                        self._children[relative].append(child_relative)
                        recursive_fd = child_fd
                        child_fd = -1
                        self._snapshot_directory(child_relative, recursive_fd, counter)
                    finally:
                        if child_fd >= 0:
                            os.close(child_fd)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                counter[0] += 1
                if counter[0] > MAX_SCAN_ENTRIES:
                    raise _CollectionEntryLimit("collection entry budget exceeded")
                entry = _AnchoredEntry.from_stat(child_relative, "file", info)
                self.entries[child_relative] = entry
                self._children[relative].append(child_relative)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _snapshot_git_config(
        self,
        git_relative: str,
        git_fd: int,
        counter: list[int],
    ) -> None:
        try:
            info = os.stat("config", dir_fd=git_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise _UnsafeCollectionSource(".git metadata changed during enumeration") from exc
        if stat.S_ISLNK(info.st_mode):
            raise _UnsafeCollectionSource(".git/config symlink is forbidden")
        if not stat.S_ISREG(info.st_mode):
            return
        counter[0] += 1
        if counter[0] > MAX_SCAN_ENTRIES:
            raise _CollectionEntryLimit("collection entry budget exceeded")
        relative = self._join(git_relative, "config")
        self.entries[relative] = _AnchoredEntry.from_stat(relative, "file", info)
        self._children[git_relative].append(relative)

    def verify_stable(self) -> None:
        if self._closed:
            raise _UnsafeCollectionSource("collection root descriptor is closed")
        current_info = os.fstat(self._root_fd)
        if not self.root_entry.matches(current_info, metadata=True):
            raise _UnsafeCollectionSource("collection source root changed during scan")
        reopened = _open_absolute_directory(self.root)
        try:
            if not self.root_entry.matches(os.fstat(reopened), metadata=True):
                raise _UnsafeCollectionSource("collection source root was replaced during scan")
        finally:
            os.close(reopened)

    def verify_snapshot(self) -> None:
        """Revalidate every recorded directory and file without following links."""

        self.verify_stable()
        for relative_path, expected in sorted(self.entries.items()):
            if not relative_path:
                continue
            if expected.kind == "directory":
                descriptor = self._open_directory_relative(relative_path)
                os.close(descriptor)
                continue
            parts = _normalized_relative_parts(relative_path)
            parent_fd = self._open_directory_relative("/".join(parts[:-1]))
            try:
                info = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            finally:
                os.close(parent_fd)
            if not stat.S_ISREG(info.st_mode) or not expected.matches(info, metadata=True):
                raise _UnsafeCollectionSource("collection file was replaced during scan")
        for relative_path, expected_target in sorted(self.symlinks.items()):
            parts = _normalized_relative_parts(relative_path)
            parent_fd = self._open_directory_relative("/".join(parts[:-1]))
            try:
                info = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                if not stat.S_ISLNK(info.st_mode):
                    raise _UnsafeCollectionSource("collection symlink was replaced during scan")
                observed_target = os.readlink(parts[-1], dir_fd=parent_fd)
            finally:
                os.close(parent_fd)
            if observed_target != expected_target:
                raise _UnsafeCollectionSource("collection symlink changed during scan")
        self.verify_stable()

    def entry(self, relative_path: str) -> _AnchoredEntry:
        try:
            return self.entries[relative_path]
        except KeyError as exc:
            raise _UnsafeCollectionSource("collection entry is outside the anchored snapshot") from exc

    def has_directory(self, relative_path: str) -> bool:
        row = self.entries.get(relative_path)
        return row is not None and row.kind == "directory"

    def has_file(self, relative_path: str) -> bool:
        row = self.entries.get(relative_path)
        return row is not None and row.kind == "file"

    def walk(self) -> Iterator[tuple[str, list[str], list[str]]]:
        stack = [""]
        while stack:
            relative = stack.pop()
            children = self._children.get(relative, [])
            directories = [
                Path(value).name
                for value in children
                if self.entries[value].kind == "directory" and Path(value).name not in SKIP_DIRS
            ]
            files = [Path(value).name for value in children if self.entries[value].kind == "file"]
            yield relative, sorted(directories), sorted(files)
            for name in reversed(sorted(directories)):
                stack.append(self._join(relative, name))

    def subtree_files(self, directory_relative: str) -> list[str]:
        prefix = f"{directory_relative}/" if directory_relative else ""
        return sorted(
            relative
            for relative, entry in self.entries.items()
            if entry.kind == "file"
            and relative.startswith(prefix)
            and not any(part in SKIP_DIRS for part in PurePosixPath(relative).parts)
        )

    def directory_size(self, directory_relative: str) -> int:
        return sum(self.entries[path].size for path in self.subtree_files(directory_relative))

    def _open_directory_relative(self, relative_path: str) -> int:
        descriptor = os.dup(self._root_fd)
        if not relative_path:
            return descriptor
        try:
            current_relative = ""
            for part in _normalized_relative_parts(relative_path):
                next_descriptor = os.open(part, _directory_open_flags(), dir_fd=descriptor)
                current_relative = self._join(current_relative, part)
                expected = self.entry(current_relative)
                if expected.kind != "directory" or not expected.matches(
                    os.fstat(next_descriptor), metadata=True
                ):
                    os.close(next_descriptor)
                    raise _UnsafeCollectionSource("collection directory was replaced during scan")
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def open_regular_fd(self, relative_path: str, *, limit: int | None = None) -> int:
        parts = _normalized_relative_parts(relative_path)
        parent = "/".join(parts[:-1])
        parent_fd = self._open_directory_relative(parent)
        try:
            descriptor = os.open(parts[-1], _regular_open_flags(), dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        try:
            info = os.fstat(descriptor)
            expected = self.entry(relative_path)
            if expected.kind != "file" or not stat.S_ISREG(info.st_mode) or not expected.matches(
                info, metadata=True
            ):
                raise _UnsafeCollectionSource("collection file was replaced during scan")
            if limit is not None and info.st_size > limit:
                raise ValueError("file is outside the bounded read contract")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def open_entry_fd(self, relative_path: str) -> int:
        entry = self.entry(relative_path)
        if entry.kind == "directory":
            return self._open_directory_relative(relative_path)
        return self.open_regular_fd(relative_path)

    def read_file(self, relative_path: str, limit: int = MAX_MANIFEST_BYTES) -> bytes:
        descriptor = self.open_regular_fd(relative_path, limit=limit)
        try:
            data = bytearray()
            while len(data) <= limit:
                chunk = os.read(descriptor, min(64 * 1024, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > limit:
                raise ValueError("file exceeds the read limit")
            if not self.entry(relative_path).matches(os.fstat(descriptor), metadata=True):
                raise _UnsafeCollectionSource("collection file changed while it was read")
            self.verify_stable()
            return bytes(data)
        finally:
            os.close(descriptor)

    def read_text(self, relative_path: str, limit: int = MAX_MANIFEST_BYTES) -> str:
        return self.read_file(relative_path, limit).decode("utf-8", "replace")

    @contextmanager
    def regular_file(self, relative_path: str) -> Iterator[Any]:
        descriptor = self.open_regular_fd(relative_path)
        file_object = os.fdopen(descriptor, "rb", closefd=True)
        try:
            yield file_object
            if not self.entry(relative_path).matches(os.fstat(file_object.fileno()), metadata=True):
                raise _UnsafeCollectionSource("collection file changed while it was read")
            self.verify_stable()
        finally:
            file_object.close()


def _iso_now(now: Any = None) -> str:
    value = now() if callable(now) else now
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        return value
    if not isinstance(value, datetime):
        raise TypeError("now must be a datetime, ISO string, callable, or None")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_collection_source_state(source_root: Path | None = None) -> dict[str, Any]:
    """Hash collection-tree metadata without opening file bodies or archives.

    This probe walks the same non-symlink, non-build tree as the catalog and
    hashes path, kind, size and nanosecond mtime metadata. It is deliberately
    cheaper than parsing Skill manifests, documents or archive entries.
    """

    root = (source_root or DEFAULT_SOURCE_ROOT).expanduser().absolute()
    try:
        with _AnchoredTree(root) as tree:
            digest = hashlib.sha256(b"collection-source-state-v1\0")
            entry_count = 0
            for relative, entry in sorted(tree.entries.items()):
                if not relative:
                    continue
                parts = PurePosixPath(relative).parts
                is_git_config = len(parts) >= 2 and parts[-2:] == (".git", "config")
                if entry.kind == "directory" and any(part in SKIP_DIRS for part in parts):
                    continue
                if entry.kind == "file" and any(part in SKIP_DIRS for part in parts) and not is_git_config:
                    continue
                fields: list[Any] = [entry.kind, relative]
                if entry.kind == "file":
                    fields.extend(
                        [entry.size, entry.mtime_ns, entry.ctime_ns, stat.S_IFMT(entry.mode)]
                    )
                digest.update(
                    json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
                digest.update(b"\0")
                entry_count += 1
            for warning in sorted(tree.warnings, key=lambda row: row["path"]):
                digest.update(
                    json.dumps(
                        [warning["code"], warning["path"]],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                digest.update(b"\0")
                entry_count += 1
            tree.verify_snapshot()
            return {
                "version": SOURCE_STATE_VERSION,
                "status": "observed",
                "signature": digest.hexdigest(),
                "entry_count": entry_count,
                "error_count": len(tree.warnings),
            }
    except FileNotFoundError:
        status = "missing"
        errors = 0
    except NotADirectoryError:
        status = "not_directory"
        errors = 0
    except (_UnsafeCollectionSource, _CollectionEntryLimit, OSError):
        status = "partial"
        errors = 1
    return {
        "version": SOURCE_STATE_VERSION,
        "status": status,
        "signature": None,
        "entry_count": 0,
        "error_count": errors,
    }


def _safe_text(value: Any, limit: int = 500) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return CONTROL_CHARS.sub("", str(value)).strip()[:limit]


def _stable_id(prefix: str, *parts: str) -> str:
    raw = "\0".join((prefix, *parts)).encode("utf-8", "replace")
    return f"{prefix}:{hashlib.sha256(raw).hexdigest()[:20]}"


def _read_regular_file(path: Path, limit: int = MAX_MANIFEST_BYTES) -> bytes:
    absolute = path.expanduser().absolute()
    parent_descriptor = _open_absolute_directory(absolute.parent)
    try:
        descriptor = os.open(absolute.name, _regular_open_flags(), dir_fd=parent_descriptor)
    finally:
        os.close(parent_descriptor)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise ValueError("file is not a bounded regular file")
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(descriptor, min(64 * 1024, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if len(data) > limit:
            raise ValueError("file exceeds the read limit")
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise _UnsafeCollectionSource("file changed while it was read")
        return bytes(data)
    finally:
        os.close(descriptor)


def _read_text(path: Path, limit: int = MAX_MANIFEST_BYTES) -> str:
    return _read_regular_file(path, limit).decode("utf-8", "replace")


def _frontmatter_value(text: str, key: str, limit: int = 120) -> str:
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    header = text[3:end if end >= 0 else min(len(text), 4000)]
    match = re.search(rf"(?m)^{re.escape(key)}:\s*[\"']?([^\n\"']+)", header)
    return _safe_text(match.group(1), limit) if match else ""


def _reference_host(reference: str) -> str:
    value = _safe_text(reference, 2048)
    if not value:
        return ""
    try:
        host = (urlsplit(value).hostname or "").lower().rstrip(".")
    except ValueError:
        host = ""
    if host:
        return host
    scp_match = re.match(r"^(?:[^@\s]+@)?([^:/\s]+):[^\\]", value)
    if scp_match and "." in scp_match.group(1):
        return scp_match.group(1).lower().rstrip(".")
    return ""


def _origin_kind_from_reference(reference: str) -> str:
    lowered = _safe_text(reference, 2048).lower()
    if lowered.startswith(("weixin://", "wechat://")):
        return "chat"
    host = _reference_host(reference)
    if not host:
        return ""
    if host == "github.com" or host.endswith(".github.com"):
        return "github"
    if host in {"weixin.qq.com", "mp.weixin.qq.com", "wx.qq.com"} or host.endswith(
        ".weixin.qq.com"
    ):
        return "chat"
    return "website"


@lru_cache(maxsize=1)
def _darwin_fgetxattr_function() -> Any:
    if sys.platform != "darwin":
        return None
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        native_fgetxattr = libc.fgetxattr
        native_fgetxattr.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        native_fgetxattr.restype = ctypes.c_ssize_t
        return native_fgetxattr
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _download_metadata_origin_descriptor(descriptor: int) -> str:
    """Read WhereFroms through an already anchored descriptor."""

    getxattr = getattr(os, "getxattr", None)
    raw = b""
    if callable(getxattr):
        try:
            raw = getxattr(descriptor, WHERE_FROM_XATTR)
        except (OSError, TypeError, ValueError):
            raw = b""
    elif sys.platform == "darwin":
        try:
            import ctypes

            native_fgetxattr = _darwin_fgetxattr_function()
            if native_fgetxattr is None:
                return ""
            encoded_name = WHERE_FROM_XATTR.encode("utf-8")
            size = native_fgetxattr(descriptor, encoded_name, None, 0, 0, 0)
            if 0 < size <= MAX_ORIGIN_METADATA_BYTES:
                buffer = ctypes.create_string_buffer(size)
                read_size = native_fgetxattr(
                    descriptor,
                    encoded_name,
                    buffer,
                    size,
                    0,
                    0,
                )
                if read_size == size:
                    raw = buffer.raw
        except (AttributeError, OSError, TypeError, ValueError):
            raw = b""
    if not raw or len(raw) > MAX_ORIGIN_METADATA_BYTES:
        return ""
    try:
        payload = plistlib.loads(raw)
    except (plistlib.InvalidFileException, ValueError, TypeError):
        return ""
    references = payload if isinstance(payload, list) else [payload]
    for reference in references:
        if not isinstance(reference, str):
            continue
        origin_kind = _origin_kind_from_reference(reference)
        if origin_kind:
            return origin_kind
    return ""



def _parse_frontmatter(text: str, fallback: str) -> tuple[str, str]:
    name = fallback
    description = ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        header = text[3:end if end >= 0 else min(len(text), 4000)]
        name_match = re.search(r"(?m)^name:\s*[\"']?([^\n\"']+)", header)
        description_match = re.search(r"(?m)^description:\s*[\"']?([^\n\"']+)", header)
        if name_match:
            name = _safe_text(name_match.group(1), 160) or fallback
        if description_match:
            description = _safe_text(description_match.group(1), 500)
    if not description:
        body = re.sub(r"\A---.*?\n---\s*", "", text, count=1, flags=re.S)
        for line in body.splitlines():
            candidate = re.sub(r"^#{1,6}\s*", "", line).strip()
            if len(candidate) >= 20:
                description = _safe_text(candidate, 500)
                break
    return name, description


def _parse_frontmatter_version(text: str) -> str:
    return _frontmatter_value(text, "version", 80)


def _load_taxonomy(taxonomy_path: Path | None) -> dict[str, Any]:
    empty = {"scenario": {"overrides": {}, "rules": []}, "assets": {"overrides": {}}}
    if taxonomy_path is None or not taxonomy_path.is_file() or taxonomy_path.is_symlink():
        return empty
    try:
        payload = json.loads(_read_text(taxonomy_path, 2 * 1024 * 1024))
    except (OSError, ValueError, json.JSONDecodeError):
        return empty
    if not isinstance(payload, dict):
        return empty
    scenario = payload.get("scenario")
    assets = payload.get("assets")
    if not isinstance(scenario, dict) or not isinstance(assets, dict):
        return empty
    if not isinstance(assets.get("overrides"), dict):
        assets = {"overrides": {}}
    return {"scenario": scenario, "assets": assets}


def _scenario_for(name: str, description: str, rules: dict[str, Any]) -> str:
    overrides = rules.get("overrides", {})
    if isinstance(overrides, dict) and isinstance(overrides.get(name), str):
        return overrides[name]
    lowered_name = name.lower()
    lowered_description = description.lower()
    for row in rules.get("rules", []):
        if not isinstance(row, dict) or not isinstance(row.get("group"), str):
            continue
        try:
            excluded = any(re.search(pattern, lowered_name) for pattern in row.get("exclude", []))
            name_hit = any(re.search(pattern, lowered_name) for pattern in row.get("name_any", []))
            description_hit = any(
                re.search(pattern, lowered_description) for pattern in row.get("desc_any", [])
            )
        except (re.error, TypeError):
            continue
        if not excluded and (name_hit or description_hit):
            return row["group"]
    combined = f"{lowered_name} {lowered_description}"
    broad = (
        ("编剧与剧本", ("剧本", "编剧", "screenplay", "scriptwriter")),
        ("Seedance 提示词 · 通用型", ("seedance", "即梦", "视频提示词")),
        ("工具类", ("agent", "cli", "tool", "代码", "开发", "research", "调查")),
    )
    for label, needles in broad:
        if any(needle in combined for needle in needles):
            return label
    return "待识别"


def _is_supporting_entry_path(path: Path) -> bool:
    return any(part.lower() in SUPPORTING_ENTRY_DIRS for part in path.parts[:-1])


def _fingerprint_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _capability(
    *,
    capability_type: str,
    name: str,
    relative_path: str,
    description: str = "",
    scenario: str = "待识别",
    version: str = "",
    fingerprint: str | None = None,
    entry_status: str = "standard",
) -> dict[str, Any]:
    safe_version = _safe_text(version, 80)
    identity = f"{capability_type}\0{name}\0{relative_path}\0{description}"
    if safe_version:
        identity = f"{identity}\0{safe_version}"
    digest = fingerprint or _fingerprint_text(identity)
    return {
        "capability_id": _stable_id("cap", capability_type, digest),
        "type": capability_type,
        "name": _safe_text(name, 180) or Path(relative_path).stem,
        "description": _safe_text(description, 500),
        "version": safe_version,
        "relative_path": relative_path,
        "scenario": scenario,
        "fingerprint": digest,
        "entry_status": entry_status if entry_status in ENTRY_STATUSES else "supporting",
    }



def _scan_skill_candidate_text(
    text: str,
    *,
    fallback: str,
    relative_path: str,
    rules: dict[str, Any],
    capability_type: str = "skill_candidate",
) -> dict[str, Any] | None:
    if not SKILL_FRONTMATTER.search(text):
        return None
    name, description = _parse_frontmatter(text, fallback)
    return _capability(
        capability_type=capability_type,
        name=name,
        description=description,
        version=_parse_frontmatter_version(text),
        relative_path=relative_path,
        scenario=_scenario_for(name, description, rules),
        fingerprint=_fingerprint_text(text),
        entry_status="candidate",
    )


def _plugin_from_payload(payload: Any, *, relative_path: str, fallback: str) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    name = _safe_text(payload.get("name"), 180) or fallback
    description = _safe_text(payload.get("description"), 500)
    return _capability(
        capability_type="plugin",
        name=name,
        description=description,
        version=_safe_text(payload.get("version"), 80),
        relative_path=relative_path,
        scenario="工具类",
        fingerprint=_fingerprint_text(json.dumps(payload, ensure_ascii=False, sort_keys=True)),
    )


def _cli_from_package_payload(
    payload: Any,
    *,
    relative_path: str,
    fallback: str,
) -> list[dict[str, Any]]:
    capabilities: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return capabilities
    raw_bin = payload.get("bin")
    if isinstance(raw_bin, str):
        package_name = _safe_text(payload.get("name"), 160) or fallback
        raw_bin = {package_name: raw_bin}
    if isinstance(raw_bin, dict):
        for name, target in sorted(raw_bin.items()):
            if not isinstance(target, str):
                continue
            capabilities.append(
                _capability(
                    capability_type="cli",
                    name=_safe_text(name, 160) or fallback,
                    description=_safe_text(payload.get("description"), 500),
                    version=_safe_text(payload.get("version"), 80),
                    relative_path=f"{relative_path}#{target}",
                    scenario="工具类",
                )
            )
    return capabilities


def _relative_under(relative_path: str, directory_relative: str) -> str:
    path = PurePosixPath(relative_path)
    if not directory_relative:
        return path.as_posix()
    return path.relative_to(PurePosixPath(directory_relative)).as_posix()


def _scan_directory_capabilities_anchored(
    tree: _AnchoredTree,
    directory_relative: str,
    *,
    rules: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, str]]]:
    capabilities: list[dict[str, Any]] = []
    shape = Counter(files=0, documents=0, scripts=0, images=0, other=0)
    errors: list[dict[str, str]] = []
    for relative_path in tree.subtree_files(directory_relative):
        local_relative = _relative_under(relative_path, directory_relative)
        local_path = PurePosixPath(local_relative)
        filename = local_path.name
        if filename in IGNORE_FILES:
            continue
        shape["files"] += 1
        suffix = local_path.suffix.lower()
        if suffix in DOCUMENT_SUFFIXES:
            shape["documents"] += 1
        elif suffix in SCRIPT_SUFFIXES:
            shape["scripts"] += 1
        elif suffix in MEDIA_SUFFIXES:
            shape["images"] += 1
        else:
            shape["other"] += 1
        try:
            if filename == "SKILL.md":
                relative_entry = Path(local_relative)
                if not _is_supporting_entry_path(relative_entry):
                    text = tree.read_text(relative_path)
                    name, description = _parse_frontmatter(
                        text, PurePosixPath(relative_path).parent.name
                    )
                    capabilities.append(
                        _capability(
                            capability_type="skill",
                            name=name,
                            description=description,
                            version=_parse_frontmatter_version(text),
                            relative_path=local_relative,
                            scenario=_scenario_for(name, description, rules),
                            fingerprint=_fingerprint_text(text),
                        )
                    )
            elif filename == "plugin.json":
                payload = json.loads(tree.read_text(relative_path))
                plugin = _plugin_from_payload(
                    payload,
                    relative_path=local_relative,
                    fallback=PurePosixPath(relative_path).parent.name,
                )
                if plugin:
                    capabilities.append(plugin)
            elif filename == ".mcp.json":
                manifest = tree.entry(relative_path)
                capabilities.append(
                    _capability(
                        capability_type="mcp",
                        name=PurePosixPath(relative_path).parent.name,
                        relative_path=local_relative,
                        scenario="工具类",
                        fingerprint=_fingerprint_text(f"{local_relative}\0{manifest.size}"),
                    )
                )
            elif filename == "package.json":
                package_payload = json.loads(tree.read_text(relative_path))
                capabilities.extend(
                    _cli_from_package_payload(
                        package_payload,
                        relative_path=local_relative,
                        fallback=PurePosixPath(relative_path).parent.name,
                    )
                )
            elif filename == "pyproject.toml":
                text = tree.read_text(relative_path)
                section_match = re.search(
                    r"(?ms)^\[project\.scripts\]\s*$\n(.*?)(?=^\[|\Z)", text
                )
                if section_match:
                    for name, target in re.findall(
                        r"(?m)^\s*([A-Za-z0-9_.-]+)\s*=\s*[\"']([^\"']+)[\"']\s*$",
                        section_match.group(1),
                    ):
                        capabilities.append(
                            _capability(
                                capability_type="cli",
                                name=_safe_text(name, 160)
                                or PurePosixPath(relative_path).parent.name,
                                relative_path=f"{local_relative}#{target}",
                                scenario="工具类",
                            )
                        )
            elif suffix == ".md" and filename not in {"README.md", "AGENTS.md", "PLAN.md"}:
                relative_candidate = Path(local_relative)
                if _is_supporting_entry_path(relative_candidate):
                    continue
                candidate = _scan_skill_candidate_text(
                    tree.read_text(relative_path),
                    fallback=local_path.stem,
                    relative_path=local_relative,
                    rules=rules,
                    capability_type=(
                        "agent_profile"
                        if any(
                            part.lower() in {"agents", ".agents"}
                            for part in (
                                *PurePosixPath(directory_relative).parts,
                                *local_path.parts[:-1],
                            )
                        )
                        else "skill_candidate"
                    ),
                )
                if candidate:
                    capabilities.append(candidate)
            elif suffix in SCRIPT_SUFFIXES and (
                CLI_FILENAME_SIGNAL.search(local_path.stem) or "bin" in local_path.parts
            ):
                cli_name = re.sub(r"[-_]?cli$", "", local_path.stem, flags=re.I) or local_path.stem
                capabilities.append(
                    _capability(
                        capability_type="cli",
                        name=cli_name,
                        relative_path=local_relative,
                        scenario="工具类",
                        entry_status="candidate",
                    )
                )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            if isinstance(exc, _UnsafeCollectionSource):
                raise
            errors.append(
                {
                    "code": "manifest_unreadable",
                    "path": local_relative,
                    "detail": exc.__class__.__name__,
                }
            )
    agents_relative = _AnchoredTree._join(directory_relative, "agents")
    hidden_agents_relative = _AnchoredTree._join(directory_relative, ".agents")
    if tree.has_directory(agents_relative) or tree.has_directory(hidden_agents_relative):
        capabilities.append(
            _capability(
                capability_type="agent_collection",
                name=f"{PurePosixPath(directory_relative).name} Agent 集合",
                relative_path="agents",
                scenario="工具类",
            )
        )
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in capabilities:
        relative_key = "" if row["type"] == "cli" else row["relative_path"]
        unique.setdefault((row["type"], row["name"], relative_key), row)
    return list(unique.values()), dict(shape), errors


def _valid_archive_member_path(name: str) -> str:
    normalized = re.sub(r"/+", "/", name.replace("\\", "/"))
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:/", normalized)
        or CONTROL_CHARS.search(normalized)
    ):
        raise ValueError("archive member path is unsafe")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("archive member path is unsafe")
    return path.as_posix()


def _read_zip_member_bounded(
    bundle: zipfile.ZipFile,
    entry: zipfile.ZipInfo,
    limit: int,
) -> bytes:
    data = bytearray()
    with bundle.open(entry, "r") as source:
        while len(data) <= limit:
            chunk = source.read(min(64 * 1024, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
    if len(data) > limit:
        raise ValueError("archive member exceeds the read limit")
    return bytes(data)


def _scan_archive_anchored(
    tree: _AnchoredTree,
    archive_relative: str,
    *,
    rules: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, str]]]:
    capabilities: list[dict[str, Any]] = []
    shape = Counter(files=0, documents=0, scripts=0, images=0, other=0)
    errors: list[dict[str, str]] = []
    manifest_reads = 0
    archive_name = PurePosixPath(archive_relative).name
    try:
        with tree.regular_file(archive_relative) as archive_file:
            with zipfile.ZipFile(archive_file) as bundle:
                entries = [
                    entry
                    for entry in bundle.infolist()
                    if not entry.is_dir()
                    or stat.S_ISLNK((entry.external_attr >> 16) & 0xFFFF)
                ]
                if len(entries) > MAX_ARCHIVE_ENTRIES:
                    return capabilities, dict(shape), [
                        {"code": "archive_entry_limit", "path": archive_name}
                    ]
                total_uncompressed = 0
                normalized_entries: list[tuple[zipfile.ZipInfo, str]] = []
                normalized_paths: set[str] = set()
                for entry in entries:
                    try:
                        normalized = _valid_archive_member_path(entry.filename)
                    except ValueError:
                        return capabilities, dict(shape), errors + [
                            {"code": "archive_path_invalid", "path": archive_name}
                        ]
                    if normalized in normalized_paths:
                        return capabilities, dict(shape), errors + [
                            {"code": "archive_duplicate_path", "path": normalized}
                        ]
                    normalized_paths.add(normalized)
                    if entry.flag_bits & 0x1:
                        return capabilities, dict(shape), errors + [
                            {"code": "archive_encrypted_entry", "path": normalized}
                        ]
                    member_mode = (entry.external_attr >> 16) & 0xFFFF
                    if stat.S_ISLNK(member_mode):
                        errors.append(
                            {
                                "code": "symlink_skipped",
                                "path": f"{archive_name}/{normalized}",
                            }
                        )
                        continue
                    if entry.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                        return capabilities, dict(shape), errors + [
                            {"code": "archive_member_too_large", "path": normalized}
                        ]
                    total_uncompressed += entry.file_size
                    if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                        return capabilities, dict(shape), errors + [
                            {"code": "archive_total_size_limit", "path": archive_name}
                        ]
                    if entry.file_size:
                        ratio = entry.file_size / max(entry.compress_size, 1)
                        if ratio > MAX_ARCHIVE_COMPRESSION_RATIO:
                            return capabilities, dict(shape), errors + [
                                {"code": "archive_compression_ratio", "path": normalized}
                            ]
                    normalized_entries.append((entry, normalized))

                for entry, normalized in normalized_entries:
                    if normalized.startswith("__MACOSX/"):
                        continue
                    shape["files"] += 1
                    entry_path = PurePosixPath(normalized)
                    basename = entry_path.name
                    suffix = entry_path.suffix.lower()
                    if suffix in DOCUMENT_SUFFIXES:
                        shape["documents"] += 1
                    elif suffix in SCRIPT_SUFFIXES:
                        shape["scripts"] += 1
                    elif suffix in MEDIA_SUFFIXES:
                        shape["images"] += 1
                    else:
                        shape["other"] += 1
                    inspect_text = basename in {"SKILL.md", "plugin.json", "package.json"} or (
                        suffix == ".md" and basename not in {"README.md", "AGENTS.md", "PLAN.md"}
                    )
                    if inspect_text and manifest_reads >= MAX_MANIFEST_READS:
                        errors.append({"code": "manifest_read_limit", "path": archive_name})
                        continue
                    if inspect_text and entry.file_size > MAX_MANIFEST_BYTES:
                        errors.append({"code": "manifest_too_large", "path": normalized})
                        continue
                    text = ""
                    if inspect_text:
                        manifest_reads += 1
                        text = _read_zip_member_bounded(
                            bundle, entry, MAX_MANIFEST_BYTES
                        ).decode("utf-8", "replace")
                    if basename == "SKILL.md" and not _is_supporting_entry_path(Path(normalized)):
                        name, description = _parse_frontmatter(text, entry_path.parent.name)
                        capabilities.append(
                            _capability(
                                capability_type="skill",
                                name=name,
                                description=description,
                                version=_parse_frontmatter_version(text),
                                relative_path=normalized,
                                scenario=_scenario_for(name, description, rules),
                                fingerprint=_fingerprint_text(text),
                            )
                        )
                    elif basename == "plugin.json":
                        try:
                            plugin = _plugin_from_payload(
                                json.loads(text),
                                relative_path=normalized,
                                fallback=entry_path.parent.name,
                            )
                        except json.JSONDecodeError:
                            plugin = None
                        if plugin:
                            capabilities.append(plugin)
                    elif basename == ".mcp.json":
                        capabilities.append(
                            _capability(
                                capability_type="mcp",
                                name=entry_path.parent.name or PurePosixPath(archive_name).stem,
                                relative_path=normalized,
                                scenario="工具类",
                                fingerprint=_fingerprint_text(f"{normalized}\0{entry.file_size}"),
                            )
                        )
                    elif basename == "package.json":
                        try:
                            package_payload = json.loads(text)
                        except json.JSONDecodeError:
                            package_payload = None
                        capabilities.extend(
                            _cli_from_package_payload(
                                package_payload,
                                relative_path=normalized,
                                fallback=entry_path.parent.name
                                or PurePosixPath(archive_name).stem,
                            )
                        )
                    elif suffix == ".md" and not _is_supporting_entry_path(Path(normalized)):
                        candidate = _scan_skill_candidate_text(
                            text,
                            fallback=entry_path.stem,
                            relative_path=normalized,
                            rules=rules,
                            capability_type=(
                                "agent_profile"
                                if any(
                                    part.lower() in {"agents", ".agents"}
                                    for part in entry_path.parts[:-1]
                                )
                                else "skill_candidate"
                            ),
                        )
                        if candidate:
                            capabilities.append(candidate)
                    elif suffix in SCRIPT_SUFFIXES and (
                        CLI_FILENAME_SIGNAL.search(entry_path.stem) or "bin" in entry_path.parts
                    ):
                        cli_name = (
                            re.sub(r"[-_]?cli$", "", entry_path.stem, flags=re.I)
                            or entry_path.stem
                        )
                        capabilities.append(
                            _capability(
                                capability_type="cli",
                                name=cli_name,
                                relative_path=normalized,
                                scenario="工具类",
                                entry_status="candidate",
                            )
                        )
    except _UnsafeCollectionSource:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
        errors.append(
            {"code": "archive_unreadable", "path": archive_name, "detail": exc.__class__.__name__}
        )
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in capabilities:
        relative_key = "" if row["type"] == "cli" else row["relative_path"]
        unique.setdefault((row["type"], row["name"], relative_key), row)
    return list(unique.values()), dict(shape), errors


def _manual_capabilities(
    override: dict[str, Any] | None,
    *,
    rules: dict[str, Any],
) -> list[dict[str, Any]]:
    capabilities: list[dict[str, Any]] = []
    if not isinstance(override, dict):
        return capabilities
    for index, row in enumerate(override.get("components", [])):
        if not isinstance(row, dict):
            continue
        capability_type = _safe_text(row.get("type"), 80) or "resource"
        name = _safe_text(row.get("name"), 180)
        if not name:
            continue
        description = _safe_text(row.get("description"), 500)
        relative_path = _safe_text(row.get("relative_path"), 500) or f"manual/{index + 1}"
        scenario = _safe_text(row.get("scenario"), 180) or _scenario_for(name, description, rules)
        identity = _safe_text(row.get("identity"), 240) or f"{capability_type}:{name}"
        capabilities.append(
            _capability(
                capability_type=capability_type,
                name=name,
                description=description,
                version=_safe_text(row.get("version"), 80),
                relative_path=relative_path,
                scenario=scenario,
                fingerprint=_fingerprint_text(f"reviewed-component\0{identity}"),
                entry_status=_safe_text(row.get("entry_status"), 40) or "supporting",
            )
        )
    return capabilities


def _component_types(capabilities: Iterable[dict[str, Any]]) -> list[str]:
    mapping = {
        "skill": "skill_bundle",
        "skill_candidate": "skill_bundle",
        "plugin": "plugin",
        "workflow": "workflow",
        "agent_collection": "agent_collection",
        "agent_profile": "agent_collection",
        "prompt": "prompt_pack",
        "prompt_pack": "prompt_pack",
        "knowledge_base": "knowledge_base",
        "case_study": "case_archive",
        "cli": "tool",
        "mcp": "tool",
        "script": "tool",
        "document": "document",
        "resource": "resource",
    }
    rows = list(capabilities)
    result = {mapping.get(row["type"], row["type"]) for row in rows}
    if any(
        WORKFLOW_SIGNAL.search(f"{row.get('name', '')} {row.get('description', '')}")
        for row in rows
    ):
        result.add("workflow")
    return sorted(result)


def _functional_type_for(
    *,
    kind: str,
    capabilities: list[dict[str, Any]],
    primary_type: str,
    component_types: list[str],
    override: dict[str, Any],
) -> str:
    requested = _safe_text(override.get("functional_type"), 80)
    if requested in FUNCTIONAL_TYPES:
        return requested
    capability_types = {row["type"] for row in capabilities}
    skill_names = {
        _safe_text(row.get("name"), 180).lower()
        for row in capabilities
        if row.get("type") in {"skill", "skill_candidate"}
        and _safe_text(row.get("name"), 180)
    }
    components = set(component_types)
    if "plugin" in capability_types:
        return "plugin"
    if primary_type == "composite_asset" and "skill_bundle" in components:
        return "composite_skill_bundle"
    if len(skill_names) >= 2:
        return "composite_skill_bundle"
    if len(skill_names) == 1 and (
        primary_type in {"skill", "skill_bundle"}
        or kind in {"skill_directory", "skill_archive", "document"}
    ):
        return "skill"
    if primary_type in {"workflow", "agent_collection", "prompt_pack", "tool"} or components & {
        "workflow",
        "agent_collection",
        "prompt_pack",
    }:
        return "workflow"
    if kind in {"repository", "project_directory", "collection_directory"} or primary_type == "project":
        return "project"
    if len(skill_names) == 1 or primary_type in {"skill", "skill_bundle"}:
        return "skill"
    return "other_material"


def _primary_scenario_for(
    *,
    functional_type: str,
    capabilities: list[dict[str, Any]],
    rules: dict[str, Any],
    override: dict[str, Any],
) -> str | None:
    if functional_type == "other_material":
        return None
    valid_groups = [
        _safe_text(row.get("name"), 180)
        for row in rules.get("groups", [])
        if isinstance(row, dict) and _safe_text(row.get("name"), 180)
    ]
    valid_group_set = set(valid_groups)
    requested = _safe_text(override.get("primary_scenario"), 180)
    if requested in valid_group_set or requested == "未归类":
        return requested
    counts = Counter(
        row.get("scenario")
        for row in capabilities
        if row.get("scenario") in valid_group_set
    )
    if not counts:
        return "未归类"
    top_count = max(counts.values())
    winners = {name for name, count in counts.items() if count == top_count}
    if len(winners) != 1:
        return "未归类"
    return next(iter(winners))


def _classification_for(
    *,
    kind: str,
    capabilities: list[dict[str, Any]],
    override: dict[str, Any] | None,
    rules: dict[str, Any],
) -> dict[str, Any]:
    labels = {
        "project": "项目 / 整库",
        "skill": "标准 Skill",
        "skill_bundle": "Skill 包 / Skill 组",
        "plugin": "Plugin 包",
        "workflow": "工作流包",
        "agent_collection": "Agent 集合",
        "prompt_pack": "Prompt 配方包",
        "knowledge_base": "知识库 / 参考资料包",
        "case_archive": "案例 / 对话归档",
        "document": "文档资料",
        "tool": "脚本 / 工具",
        "resource": "资源文件",
        "composite_asset": "复合资产",
        "unknown": "待辨认对象",
    }
    component_types = _component_types(capabilities)
    capability_types = {row["type"] for row in capabilities}
    if kind in {"repository", "project_directory"}:
        primary_type = "project"
        basis = ["检测到 Git 仓库或项目 manifest"]
        confidence = "high"
    elif kind == "skill_directory":
        primary_type = "skill"
        basis = ["检测到标准 SKILL.md"]
        confidence = "high"
    elif kind == "skill_archive" or capability_types & {"skill", "skill_candidate"}:
        primary_type = "skill_bundle"
        basis = ["包内检测到 Skill 入口或带 Skill frontmatter 的候选源文件"]
        confidence = "high" if "skill" in capability_types else "medium"
    elif "plugin" in capability_types:
        primary_type = "plugin"
        basis = ["检测到 Plugin manifest"]
        confidence = "high"
    elif "workflow" in component_types:
        primary_type = "workflow"
        basis = ["检测到明确的编排、路由或工作流信号"]
        confidence = "medium"
    elif "agent_collection" in component_types:
        primary_type = "agent_collection"
        basis = ["检测到 Agent 集合结构"]
        confidence = "medium"
    elif kind == "document":
        primary_type = "document"
        basis = ["独立文档文件"]
        confidence = "high"
    elif kind == "script":
        primary_type = "tool"
        basis = ["独立脚本文件"]
        confidence = "high"
    elif kind == "resource_file":
        primary_type = "resource"
        basis = ["当前仅确认文件形态，未执行或解析正文"]
        confidence = "medium"
    else:
        primary_type = "unknown"
        basis = ["未发现足够的标准入口或人工复核结论"]
        confidence = "low"

    status = "needs_review" if primary_type == "unknown" else "classified"
    readiness = "not_assessed"
    summary = labels[primary_type]
    override = override if isinstance(override, dict) else {}
    requested_type = _safe_text(override.get("primary_type"), 80)
    if requested_type in CLASSIFICATION_TYPES:
        primary_type = requested_type
        summary = labels[primary_type]
    extra_components = {
        _safe_text(value, 80)
        for value in override.get("component_types", [])
        if _safe_text(value, 80)
    }
    component_types = sorted(set(component_types) | extra_components)
    requested_status = _safe_text(override.get("status"), 40)
    requested_confidence = _safe_text(override.get("confidence"), 40)
    if requested_status in CLASSIFICATION_STATUSES:
        status = requested_status
    if requested_confidence in CLASSIFICATION_CONFIDENCE:
        confidence = requested_confidence
    readiness = _safe_text(override.get("readiness"), 80) or readiness
    summary = _safe_text(override.get("summary"), 500) or summary
    display_label = _safe_text(override.get("display_label"), 180) or labels[primary_type]
    override_basis = [
        _safe_text(value, 300)
        for value in override.get("basis", [])
        if _safe_text(value, 300)
    ]
    if override_basis:
        basis = override_basis
    is_composite = bool(override.get("is_composite")) or primary_type == "composite_asset" or len(
        {value for value in component_types if value not in {"document", "resource"}}
    ) > 1
    functional_type = _functional_type_for(
        kind=kind,
        capabilities=capabilities,
        primary_type=primary_type,
        component_types=component_types,
        override=override,
    )
    primary_scenario = _primary_scenario_for(
        functional_type=functional_type,
        capabilities=capabilities,
        rules=rules,
        override=override,
    )
    return {
        "primary_type": primary_type,
        "functional_type": functional_type,
        "primary_scenario": primary_scenario,
        "display_label": display_label,
        "component_types": component_types,
        "is_composite": is_composite,
        "status": status,
        "confidence": confidence,
        "readiness": readiness,
        "summary": summary,
        "basis": basis[:6],
    }


def _proposed_bucket(classification: dict[str, Any]) -> str:
    primary_type = classification["primary_type"]
    if primary_type in {"skill", "skill_bundle"}:
        return "10_Skill与技能包"
    if primary_type in {"project", "composite_asset"}:
        return "20_整库与项目"
    if primary_type in {"document", "knowledge_base", "case_archive"}:
        return "30_文档与方法资料"
    if primary_type in {"plugin", "workflow", "agent_collection", "prompt_pack", "tool"}:
        return "40_Agent与工作流"
    return "99_待识别"


def _source_version(
    path: Path,
    capabilities: list[dict[str, Any]],
    override: dict[str, Any] | None,
) -> str:
    manual = _safe_text(override.get("version"), 80) if isinstance(override, dict) else ""
    if manual:
        return manual
    declared = {
        _safe_text(capability.get("version"), 80)
        for capability in capabilities
        if _safe_text(capability.get("version"), 80)
    }
    if len(declared) == 1:
        return declared.pop()
    match = EXPLICIT_VERSION_SIGNAL.search(path.stem)
    return _safe_text(match.group(1), 80) if match else ""


def _git_remote_origin_anchored(tree: _AnchoredTree, source_relative: str) -> str:
    config_relative = _AnchoredTree._join(
        _AnchoredTree._join(source_relative, ".git"), "config"
    )
    if not tree.has_file(config_relative):
        return ""
    try:
        text = tree.read_text(config_relative, 128 * 1024)
    except _UnsafeCollectionSource:
        raise
    except (OSError, ValueError):
        return ""
    for reference in re.findall(r"(?m)^\s*url\s*=\s*(\S.*?)\s*$", text):
        origin_kind = _origin_kind_from_reference(reference)
        if origin_kind:
            return origin_kind
    return ""


def _source_origin_anchored(
    tree: _AnchoredTree,
    relative: str,
    *,
    kind: str,
    override: dict[str, Any] | None,
) -> tuple[str, str]:
    manual = _safe_text(override.get("source_kind"), 40) if isinstance(override, dict) else ""
    if manual in ORIGIN_KINDS:
        return manual, "manual"
    if kind == "repository":
        repository_origin = _git_remote_origin_anchored(tree, relative)
        if repository_origin:
            return repository_origin, "git_remote"
    descriptor = tree.open_entry_fd(relative)
    try:
        download_origin = _download_metadata_origin_descriptor(descriptor)
        if not tree.entry(relative).matches(os.fstat(descriptor), metadata=True):
            raise _UnsafeCollectionSource("collection entry changed during metadata read")
    finally:
        os.close(descriptor)
    tree.verify_stable()
    if download_origin:
        return download_origin, "download_metadata"
    if any(part in CHAT_PATH_SEGMENTS for part in PurePosixPath(relative).parts):
        return "chat", "collection_path"
    return "unknown", "unavailable"


def _source_item_anchored(
    *,
    tree: _AnchoredTree,
    relative: str,
    kind: str,
    capabilities: list[dict[str, Any]],
    shape: dict[str, int],
    errors: list[dict[str, str]],
    size_bytes: int | None = None,
    rules: dict[str, Any] | None = None,
    classification_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = tree.root / Path(relative)
    entry = tree.entry(relative)
    scenario_rules = rules or {"overrides": {}, "rules": []}
    combined_capabilities = list(capabilities) + _manual_capabilities(
        classification_override,
        rules=scenario_rules,
    )
    unique_capabilities: dict[tuple[str, str, str], dict[str, Any]] = {}
    for capability in combined_capabilities:
        key = (capability["type"], capability["name"], capability["fingerprint"])
        unique_capabilities.setdefault(key, capability)
    capabilities = list(unique_capabilities.values())
    classification = _classification_for(
        kind=kind,
        capabilities=capabilities,
        override=classification_override,
        rules=scenario_rules,
    )
    scenarios = sorted({row["scenario"] for row in capabilities if row["scenario"]})
    if not scenarios and kind == "document":
        scenarios = [_scenario_for(path.stem, "", {"overrides": {}, "rules": []})]
    modified_at = datetime.fromtimestamp(
        entry.mtime_ns / 1_000_000_000,
        timezone.utc,
    ).isoformat().replace("+00:00", "Z")
    resolved_size = entry.size if entry.kind == "file" else tree.directory_size(relative)
    proposed_bucket = _safe_text(
        classification_override.get("proposed_bucket")
        if isinstance(classification_override, dict)
        else "",
        120,
    ) or _proposed_bucket(classification)
    relations = []
    if isinstance(classification_override, dict):
        for relation in classification_override.get("relations", []):
            if not isinstance(relation, dict):
                continue
            relation_type = _safe_text(relation.get("type"), 80)
            target = _safe_text(relation.get("target_relative_path"), 500)
            if relation_type and target:
                relations.append(
                    {
                        "type": relation_type,
                        "target_relative_path": target,
                        "note": _safe_text(relation.get("note"), 300),
                    }
                )
    origin_kind, origin_basis = _source_origin_anchored(
        tree,
        relative,
        kind=kind,
        override=classification_override,
    )
    tree.verify_stable()
    return {
        "source_id": _stable_id("src", kind, relative),
        "name": path.name,
        "relative_path": relative,
        "kind": kind,
        "size_bytes": resolved_size if size_bytes is None else size_bytes,
        "modified_at": modified_at,
        "origin_kind": origin_kind,
        "origin_basis": origin_basis,
        "version": _source_version(path, capabilities, classification_override),
        "scenarios": scenarios or ["待识别"],
        "capabilities": sorted(
            capabilities, key=lambda row: (row["type"], row["name"].lower(), row["relative_path"])
        ),
        "shape": {
            "files": int(shape.get("files", 0)),
            "documents": int(shape.get("documents", 0)),
            "scripts": int(shape.get("scripts", 0)),
            "images": int(shape.get("images", 0)),
            "other": int(shape.get("other", 0)),
        },
        "host_links": [],
        "installation_status": "not_observed",
        "usage_status": "unrecorded",
        "classification": classification,
        "relations": relations,
        "proposed_bucket": proposed_bucket,
        "move_safety": "review",
        "duplicate_capability_count": 0,
        "scan_errors": errors,
    }


def _host_links(
    source_root: Path,
    host_roots: dict[str, Path],
    *,
    source_tree: _AnchoredTree | None = None,
) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    owned_source_tree: _AnchoredTree | None = None
    if source_tree is None:
        owned_source_tree = _AnchoredTree(source_root)
        source_tree = owned_source_tree
    source_absolute = source_tree.root
    try:
        for host_id, host_root in host_roots.items():
            try:
                host_tree_context = _AnchoredTree(host_root)
            except (FileNotFoundError, NotADirectoryError, _UnsafeCollectionSource, OSError):
                continue
            with host_tree_context as host_tree:
                for relative_link, raw_target in sorted(host_tree.symlinks.items()):
                    link_path = host_tree.root / Path(relative_link)
                    if os.path.isabs(raw_target):
                        target_text = os.path.normpath(raw_target)
                    else:
                        target_text = os.path.abspath(
                            os.path.join(link_path.parent.as_posix(), raw_target)
                        )
                    target_path = Path(target_text)
                    try:
                        relative_target = target_path.relative_to(source_absolute).as_posix()
                    except ValueError:
                        continue
                    if relative_target not in source_tree.entries:
                        continue
                    links.append(
                        {
                            "host_id": host_id,
                            "link_path": link_path.as_posix(),
                            "target_path": target_path.as_posix(),
                            "target_relative_path": relative_target,
                        }
                    )
                host_tree.verify_snapshot()
        source_tree.verify_snapshot()
        return links
    finally:
        if owned_source_tree is not None:
            owned_source_tree.close()


def build_collection_input_state(
    *,
    source_root: Path | None = None,
    host_roots: dict[str, Path] | None = None,
    taxonomy_path: Path | None = None,
) -> dict[str, Any]:
    """Fingerprint every bounded input that can change the collection snapshot."""

    root = (source_root or DEFAULT_SOURCE_ROOT).expanduser().absolute()
    source_state = build_collection_source_state(root)
    if source_state["status"] != "observed":
        return {
            "version": INPUT_STATE_VERSION,
            "status": "indeterminate",
            "signature": None,
            "source_entry_count": source_state["entry_count"],
            "host_link_count": 0,
        }

    effective_taxonomy = (taxonomy_path or DEFAULT_TAXONOMY_PATH).expanduser().absolute()
    try:
        taxonomy_bytes = _read_regular_file(effective_taxonomy, MAX_MANIFEST_BYTES)
    except FileNotFoundError:
        taxonomy_signature = hashlib.sha256(b"taxonomy-missing").hexdigest()
    except (OSError, ValueError):
        return {
                "version": INPUT_STATE_VERSION,
                "status": "indeterminate",
                "signature": None,
                "source_entry_count": source_state["entry_count"],
                "host_link_count": 0,
            }
    else:
        taxonomy_signature = hashlib.sha256(taxonomy_bytes).hexdigest()

    try:
        links = _host_links(root, host_roots or DEFAULT_HOST_ROOTS)
    except OSError:
        return {
            "version": INPUT_STATE_VERSION,
            "status": "indeterminate",
            "signature": None,
            "source_entry_count": source_state["entry_count"],
            "host_link_count": 0,
        }
    normalized_links = sorted(
        (
            link["host_id"],
            link["link_path"],
            link["target_relative_path"],
        )
        for link in links
    )
    canonical = json.dumps(
        {
            "version": INPUT_STATE_VERSION,
            "scanner_revision": COLLECTION_SCANNER_REVISION,
            "source_signature": source_state["signature"],
            "taxonomy_signature": taxonomy_signature,
            "host_links": normalized_links,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "version": INPUT_STATE_VERSION,
        "status": "observed",
        "signature": hashlib.sha256(canonical).hexdigest(),
        "source_entry_count": source_state["entry_count"],
        "host_link_count": len(normalized_links),
    }


def _attach_host_links(items: list[dict[str, Any]], links: list[dict[str, str]]) -> None:
    for link in links:
        target = link["target_relative_path"]
        matches = [
            item
            for item in items
            if target == item["relative_path"] or target.startswith(item["relative_path"] + "/")
        ]
        if not matches:
            continue
        owner = max(matches, key=lambda item: len(item["relative_path"]))
        owner["host_links"].append(
            {
                "host_id": link["host_id"],
                "link_path": link["link_path"],
                "target_path": link["target_path"],
            }
        )
    for item in items:
        if item["host_links"]:
            item["host_links"].sort(key=lambda row: (row["host_id"], row["link_path"]))
            item["installation_status"] = "linked"
            item["move_safety"] = "keep_anchor"
            item["proposed_bucket"] = "原位保留_活动锚点"


def _mark_duplicates(items: list[dict[str, Any]]) -> int:
    occurrences: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        for capability in item["capabilities"]:
            occurrences[capability["fingerprint"]].append(item)
    duplicate_groups = 0
    for rows in occurrences.values():
        source_ids = {row["source_id"] for row in rows}
        if len(source_ids) < 2:
            continue
        duplicate_groups += 1
        for item in {row["source_id"]: row for row in rows}.values():
            item["duplicate_capability_count"] += 1
    return duplicate_groups


def _empty_collection_payload(
    root: Path,
    *,
    now: Any,
    error_code: str,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _iso_now(now),
        "mode": "collection-observe",
        "source_root": root.as_posix(),
        "summary": {
            "source_count": 0,
            "capability_count": 0,
            "unique_capability_count": 0,
            "repository_count": 0,
            "document_count": 0,
            "archive_count": 0,
            "classified_count": 0,
            "unclassified_count": 0,
            "project_count": 0,
            "composite_count": 0,
            "anchored_source_count": 0,
            "duplicate_group_count": 0,
            "container_count": 0,
            "scan_error_count": 1,
        },
        "items": [],
        "scan_errors": [{"code": error_code, "path": root.as_posix()}],
    }


def build_collection_payload(
    *,
    source_root: Path | None = None,
    host_roots: dict[str, Path] | None = None,
    taxonomy_path: Path | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Build the catalog from a descriptor-anchored immutable observation."""

    root = (source_root or DEFAULT_SOURCE_ROOT).expanduser().absolute()
    taxonomy = _load_taxonomy(taxonomy_path or DEFAULT_TAXONOMY_PATH)
    rules = taxonomy["scenario"]
    asset_overrides = taxonomy["assets"].get("overrides", {})
    if not isinstance(asset_overrides, dict):
        asset_overrides = {}

    def override_for(relative_path: str) -> dict[str, Any]:
        candidate = asset_overrides.get(relative_path)
        return candidate if isinstance(candidate, dict) else {}

    try:
        tree_context = _AnchoredTree(root)
    except FileNotFoundError:
        return _empty_collection_payload(root, now=now, error_code="source_root_missing")
    except _CollectionEntryLimit:
        return _empty_collection_payload(root, now=now, error_code="entry_limit")
    except (NotADirectoryError, _UnsafeCollectionSource, OSError):
        return _empty_collection_payload(root, now=now, error_code="source_root_unsafe")

    with tree_context as tree:
        items: list[dict[str, Any]] = []
        containers: set[str] = set()
        grouped_prefixes: list[str] = []

        def is_grouped(relative: str) -> bool:
            return any(
                relative == prefix or relative.startswith(f"{prefix}/")
                for prefix in grouped_prefixes
            )

        for current_relative, dirnames, filenames in tree.walk():
            if is_grouped(current_relative):
                continue
            current_override = override_for(current_relative)
            kind = ""
            if current_relative and current_override.get("group_as_source") is True:
                kind = "collection_directory"
            elif current_relative and tree.has_directory(
                _AnchoredTree._join(current_relative, ".git")
            ):
                kind = "repository"
            elif current_relative and "SKILL.md" in filenames:
                kind = "skill_directory"
            elif current_relative and any(
                marker in filenames for marker in ("package.json", "pyproject.toml", "Cargo.toml")
            ):
                kind = "project_directory"

            if kind:
                capabilities, shape, errors = _scan_directory_capabilities_anchored(
                    tree,
                    current_relative,
                    rules=rules,
                )
                items.append(
                    _source_item_anchored(
                        tree=tree,
                        relative=current_relative,
                        kind=kind,
                        capabilities=capabilities,
                        shape=shape,
                        errors=errors,
                        rules=rules,
                        classification_override=current_override,
                    )
                )
                grouped_prefixes.append(current_relative)
                continue

            if current_relative and dirnames:
                containers.add(current_relative)
            for filename in filenames:
                if filename == "SKILL.md":
                    continue
                relative_path = _AnchoredTree._join(current_relative, filename)
                entry = tree.entry(relative_path)
                suffix = PurePosixPath(relative_path).suffix.lower()
                file_override = override_for(relative_path)
                if suffix in ZIP_ARCHIVE_SUFFIXES:
                    capabilities, shape, errors = _scan_archive_anchored(
                        tree,
                        relative_path,
                        rules=rules,
                    )
                    item_kind = (
                        "skill_archive"
                        if any(row["type"] == "skill" for row in capabilities)
                        else "archive"
                    )
                    items.append(
                        _source_item_anchored(
                            tree=tree,
                            relative=relative_path,
                            kind=item_kind,
                            capabilities=capabilities,
                            shape=shape,
                            errors=errors,
                            rules=rules,
                            classification_override=file_override,
                        )
                    )
                elif suffix in OPAQUE_ARCHIVE_SUFFIXES:
                    items.append(
                        _source_item_anchored(
                            tree=tree,
                            relative=relative_path,
                            kind="archive",
                            capabilities=[],
                            shape={"files": 1, "other": 1},
                            errors=[],
                            size_bytes=entry.size,
                            rules=rules,
                            classification_override=file_override,
                        )
                    )
                elif suffix in DOCUMENT_SUFFIXES:
                    items.append(
                        _source_item_anchored(
                            tree=tree,
                            relative=relative_path,
                            kind="document",
                            capabilities=[],
                            shape={"files": 1, "documents": 1},
                            errors=[],
                            size_bytes=entry.size,
                            rules=rules,
                            classification_override=file_override,
                        )
                    )
                elif suffix in SCRIPT_SUFFIXES:
                    capability = _capability(
                        capability_type="script",
                        name=PurePosixPath(relative_path).stem,
                        relative_path=filename,
                        scenario="工具类",
                    )
                    items.append(
                        _source_item_anchored(
                            tree=tree,
                            relative=relative_path,
                            kind="script",
                            capabilities=[capability],
                            shape={"files": 1, "scripts": 1},
                            errors=[],
                            size_bytes=entry.size,
                            rules=rules,
                            classification_override=file_override,
                        )
                    )
                else:
                    is_image = suffix in MEDIA_SUFFIXES
                    items.append(
                        _source_item_anchored(
                            tree=tree,
                            relative=relative_path,
                            kind="resource_file",
                            capabilities=[],
                            shape={
                                "files": 1,
                                "images": 1 if is_image else 0,
                                "other": 0 if is_image else 1,
                            },
                            errors=[],
                            size_bytes=entry.size,
                            rules=rules,
                            classification_override=file_override,
                        )
                    )

        effective_host_roots = host_roots or DEFAULT_HOST_ROOTS
        links = _host_links(root, effective_host_roots, source_tree=tree)
        tree.verify_snapshot()
        _attach_host_links(items, links)
        duplicate_groups = _mark_duplicates(items)
        capabilities = [capability for item in items for capability in item["capabilities"]]
        unique_capabilities = {row["fingerprint"] for row in capabilities}
        items.sort(
            key=lambda item: (
                item["classification"]["primary_type"],
                item["relative_path"].lower(),
            )
        )
        all_errors = list(tree.warnings) + [
            error for item in items for error in item["scan_errors"]
        ]
        tree.verify_snapshot()
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _iso_now(now),
            "mode": "collection-observe",
            "source_root": root.as_posix(),
            "summary": {
                "source_count": len(items),
                "capability_count": len(capabilities),
                "unique_capability_count": len(unique_capabilities),
                "repository_count": sum(item["kind"] == "repository" for item in items),
                "document_count": sum(item["kind"] == "document" for item in items),
                "archive_count": sum(
                    item["kind"] in {"archive", "skill_archive"} for item in items
                ),
                "classified_count": sum(
                    item["classification"]["status"] != "needs_review" for item in items
                ),
                "unclassified_count": sum(
                    item["classification"]["status"] == "needs_review" for item in items
                ),
                "project_count": sum(
                    item["classification"]["primary_type"] in {"project", "composite_asset"}
                    for item in items
                ),
                "composite_count": sum(
                    bool(item["classification"]["is_composite"]) for item in items
                ),
                "anchored_source_count": sum(bool(item["host_links"]) for item in items),
                "duplicate_group_count": duplicate_groups,
                "container_count": len(containers),
                "scan_error_count": len(all_errors),
            },
            "items": items,
            "scan_errors": all_errors,
        }


def validate_collection_payload(payload: Any) -> dict[str, Any]:
    """Validate the compact collection contract before it reaches the UI."""

    def is_nonnegative_int(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def validate_iso(value: Any, label: str) -> None:
        if not isinstance(value, str) or not value or CONTROL_CHARS.search(value):
            raise ValueError(f"{label} must be an ISO timestamp")
        candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError(f"{label} must be an ISO timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{label} must include a timezone")

    def validate_relative(value: Any, label: str, *, allow_fragment: bool = False) -> None:
        if not isinstance(value, str) or not value or CONTROL_CHARS.search(value):
            raise ValueError(f"{label} is invalid")
        path_value, separator, fragment = value.partition("#") if allow_fragment else (value, "", "")
        path = PurePosixPath(path_value.replace("\\", "/"))
        if (
            path.is_absolute()
            or re.match(r"^[A-Za-z]:/", path_value)
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError(f"{label} must stay relative")
        if separator:
            if not fragment or CONTROL_CHARS.search(fragment):
                raise ValueError(f"{label} fragment is invalid")
            normalized_fragment = fragment.replace("\\", "/")
            if normalized_fragment.startswith("/") or any(
                part == ".." for part in PurePosixPath(normalized_fragment).parts
            ):
                raise ValueError(f"{label} fragment is unsafe")

    def validate_absolute(value: Any, label: str) -> None:
        if not isinstance(value, str) or not value or CONTROL_CHARS.search(value):
            raise ValueError(f"{label} is invalid")
        path = Path(value)
        if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
            raise ValueError(f"{label} must be absolute")

    def validate_error(error: Any, *, allow_absolute_source: bool = False) -> None:
        if not isinstance(error, dict) or set(error) not in (
            {"code", "path"},
            {"code", "path", "detail"},
        ):
            raise ValueError("collection scan error shape is invalid")
        if not isinstance(error["code"], str) or error["code"] not in SCAN_ERROR_CODES:
            raise ValueError("collection scan error code is invalid")
        if (
            allow_absolute_source
            and error["code"] in {"source_root_missing", "source_root_unsafe", "entry_limit"}
            and isinstance(error["path"], str)
            and Path(error["path"]).is_absolute()
        ):
            validate_absolute(error["path"], "collection scan error path")
        else:
            validate_relative(error["path"], "collection scan error path")
        if "detail" in error and (
            not isinstance(error["detail"], str)
            or not error["detail"]
            or CONTROL_CHARS.search(error["detail"])
        ):
            raise ValueError("collection scan error detail is invalid")

    if not isinstance(payload, dict):
        raise ValueError("collection payload must be an object")
    required = {
        "schema_version",
        "generated_at",
        "mode",
        "source_root",
        "summary",
        "items",
        "scan_errors",
    }
    if set(payload) != required:
        raise ValueError("collection payload fields do not match the contract")
    if payload["schema_version"] != SCHEMA_VERSION or payload["mode"] != "collection-observe":
        raise ValueError("collection payload identity is invalid")
    validate_iso(payload["generated_at"], "collection generated_at")
    validate_absolute(payload["source_root"], "collection source_root")
    if not isinstance(payload["items"], list):
        raise ValueError("collection payload shape is invalid")
    if not isinstance(payload["scan_errors"], list) or not isinstance(payload["summary"], dict):
        raise ValueError("collection payload collections are invalid")
    summary_fields = {
        "source_count",
        "capability_count",
        "unique_capability_count",
        "repository_count",
        "document_count",
        "archive_count",
        "classified_count",
        "unclassified_count",
        "project_count",
        "composite_count",
        "anchored_source_count",
        "duplicate_group_count",
        "container_count",
        "scan_error_count",
    }
    if set(payload["summary"]) != summary_fields or not all(
        is_nonnegative_int(value) for value in payload["summary"].values()
    ):
        raise ValueError("collection summary fields are invalid")
    for error in payload["scan_errors"]:
        validate_error(error, allow_absolute_source=True)

    source_ids: set[str] = set()
    source_paths: set[str] = set()
    capability_rows: list[dict[str, Any]] = []
    expected_item_errors: list[dict[str, Any]] = []
    for item in payload["items"]:
        item_fields = {
            "source_id",
            "name",
            "relative_path",
            "kind",
            "size_bytes",
            "modified_at",
            "origin_kind",
            "origin_basis",
            "version",
            "scenarios",
            "capabilities",
            "shape",
            "host_links",
            "installation_status",
            "usage_status",
            "classification",
            "relations",
            "proposed_bucket",
            "move_safety",
            "duplicate_capability_count",
            "scan_errors",
        }
        if not isinstance(item, dict) or set(item) != item_fields:
            raise ValueError("collection source row is invalid")
        validate_relative(item["relative_path"], "collection source relative_path")
        if item["relative_path"] in source_paths:
            raise ValueError("collection source paths are duplicated")
        source_paths.add(item["relative_path"])
        if not isinstance(item.get("kind"), str) or item.get("kind") not in {
            "repository",
            "project_directory",
            "collection_directory",
            "skill_directory",
            "skill_archive",
            "archive",
            "document",
            "script",
            "resource_file",
        }:
            raise ValueError("collection source kind is invalid")
        if (
            not isinstance(item.get("source_id"), str)
            or item.get("source_id") in source_ids
            or item["source_id"] != _stable_id("src", item["kind"], item["relative_path"])
        ):
            raise ValueError("collection source IDs are invalid")
        source_ids.add(item["source_id"])
        if (
            not isinstance(item.get("origin_kind"), str)
            or item.get("origin_kind") not in ORIGIN_KINDS
            or not isinstance(item.get("origin_basis"), str)
            or item.get("origin_basis") not in ORIGIN_BASES
        ):
            raise ValueError("collection source origin is invalid")
        if (
            not isinstance(item.get("name"), str)
            or not item["name"]
            or CONTROL_CHARS.search(item["name"])
            or item["name"] != PurePosixPath(item["relative_path"]).name
            or not isinstance(item.get("version"), str)
            or CONTROL_CHARS.search(item["version"])
            or not is_nonnegative_int(item.get("size_bytes"))
        ):
            raise ValueError("collection source version is invalid")
        validate_iso(item["modified_at"], "collection source modified_at")
        if (
            not isinstance(item.get("capabilities"), list)
            or not isinstance(item.get("host_links"), list)
            or not isinstance(item.get("relations"), list)
            or not isinstance(item.get("scenarios"), list)
            or not item["scenarios"]
            or not all(
                isinstance(value, str) and value and not CONTROL_CHARS.search(value)
                for value in item["scenarios"]
            )
        ):
            raise ValueError("collection source relations are invalid")
        if not isinstance(item.get("shape"), dict) or set(item["shape"]) != {
            "files",
            "documents",
            "scripts",
            "images",
            "other",
        }:
            raise ValueError("collection source shape is invalid")
        if not all(is_nonnegative_int(value) for value in item["shape"].values()) or item[
            "shape"
        ]["files"] != sum(item["shape"][key] for key in ("documents", "scripts", "images", "other")):
            raise ValueError("collection source shape counts are inconsistent")
        classification = item.get("classification")
        if not isinstance(classification, dict) or set(classification) != {
            "primary_type",
            "functional_type",
            "primary_scenario",
            "display_label",
            "component_types",
            "is_composite",
            "status",
            "confidence",
            "readiness",
            "summary",
            "basis",
        }:
            raise ValueError("collection classification shape is invalid")
        if (
            not isinstance(classification["primary_type"], str)
            or classification["primary_type"] not in CLASSIFICATION_TYPES
        ):
            raise ValueError("collection classification type is invalid")
        if (
            not isinstance(classification["functional_type"], str)
            or classification["functional_type"] not in FUNCTIONAL_TYPES
        ):
            raise ValueError("collection functional type is invalid")
        if classification["functional_type"] == "other_material":
            if classification["primary_scenario"] is not None:
                raise ValueError("other material must not have a primary scenario")
        elif (
            not isinstance(classification["primary_scenario"], str)
            or not classification["primary_scenario"]
            or CONTROL_CHARS.search(classification["primary_scenario"])
        ):
            raise ValueError("functional collection must have one primary scenario")
        if (
            not isinstance(classification["status"], str)
            or classification["status"] not in CLASSIFICATION_STATUSES
        ):
            raise ValueError("collection classification status is invalid")
        if (
            not isinstance(classification["confidence"], str)
            or classification["confidence"] not in CLASSIFICATION_CONFIDENCE
        ):
            raise ValueError("collection classification confidence is invalid")
        if (
            not isinstance(classification["component_types"], list)
            or not all(isinstance(value, str) for value in classification["component_types"])
            or len(classification["component_types"]) != len(set(classification["component_types"]))
            or not all(value in CLASSIFICATION_TYPES for value in classification["component_types"])
            or not isinstance(classification["basis"], list)
            or not classification["basis"]
            or not all(
                isinstance(value, str) and value and not CONTROL_CHARS.search(value)
                for value in classification["basis"]
            )
            or type(classification["is_composite"]) is not bool
            or not isinstance(classification["readiness"], str)
            or classification["readiness"] not in READINESS_VALUES
            or not all(
                isinstance(classification[key], str) and classification[key]
                and not CONTROL_CHARS.search(classification[key])
                for key in ("display_label", "summary")
            )
        ):
            raise ValueError("collection classification evidence is invalid")
        item_capability_ids: set[str] = set()
        for capability in item["capabilities"]:
            if not isinstance(capability, dict) or set(capability) != {
                "capability_id",
                "type",
                "name",
                "description",
                "version",
                "relative_path",
                "scenario",
                "fingerprint",
                "entry_status",
            }:
                raise ValueError("collection capability shape is invalid")
            if (
                not isinstance(capability["type"], str)
                or capability["type"] not in CAPABILITY_TYPES
                or not isinstance(capability.get("entry_status"), str)
                or capability.get("entry_status") not in ENTRY_STATUSES
                or not isinstance(capability.get("name"), str)
                or not capability["name"]
                or CONTROL_CHARS.search(capability["name"])
                or not isinstance(capability.get("description"), str)
                or CONTROL_CHARS.search(capability["description"])
                or not isinstance(capability.get("scenario"), str)
                or not capability["scenario"]
                or CONTROL_CHARS.search(capability["scenario"])
            ):
                raise ValueError("collection capability entry status is invalid")
            if (
                not isinstance(capability.get("version"), str)
                or CONTROL_CHARS.search(capability["version"])
                or not isinstance(capability.get("fingerprint"), str)
                or re.fullmatch(r"[0-9a-f]{64}", capability["fingerprint"]) is None
            ):
                raise ValueError("collection capability version is invalid")
            validate_relative(
                capability["relative_path"],
                "collection capability relative_path",
                allow_fragment=True,
            )
            expected_capability_id = _stable_id(
                "cap", capability["type"], capability["fingerprint"]
            )
            if (
                capability["capability_id"] != expected_capability_id
                or capability["capability_id"] in item_capability_ids
            ):
                raise ValueError("collection capability IDs are invalid")
            item_capability_ids.add(capability["capability_id"])
            capability_rows.append({**capability, "source_id": item["source_id"]})

        for host_link in item["host_links"]:
            if not isinstance(host_link, dict) or set(host_link) != {
                "host_id",
                "link_path",
                "target_path",
            }:
                raise ValueError("collection host link shape is invalid")
            if (
                not isinstance(host_link["host_id"], str)
                or re.fullmatch(r"[A-Za-z0-9_.-]+", host_link["host_id"]) is None
            ):
                raise ValueError("collection host link host is invalid")
            validate_absolute(host_link["link_path"], "collection host link path")
            validate_absolute(host_link["target_path"], "collection host target path")
            try:
                Path(host_link["target_path"]).relative_to(Path(payload["source_root"]))
            except ValueError as exc:
                raise ValueError("collection host target escapes source root") from exc

        for relation in item["relations"]:
            if not isinstance(relation, dict) or set(relation) != {
                "type",
                "target_relative_path",
                "note",
            }:
                raise ValueError("collection relation shape is invalid")
            if (
                not isinstance(relation["type"], str)
                or relation["type"] not in RELATION_TYPES
                or not isinstance(relation["note"], str)
                or CONTROL_CHARS.search(relation["note"])
            ):
                raise ValueError("collection relation type is invalid")
            validate_relative(
                relation["target_relative_path"], "collection relation target_relative_path"
            )

        if (
            not isinstance(item.get("installation_status"), str)
            or item.get("installation_status") not in {"linked", "not_observed"}
        ):
            raise ValueError("collection installation status is invalid")
        if item.get("usage_status") != "unrecorded":
            raise ValueError("collection usage status must remain evidence-neutral")
        if (
            not isinstance(item.get("move_safety"), str)
            or item.get("move_safety") not in MOVE_SAFETY_VALUES
        ):
            raise ValueError("collection move safety is invalid")
        if bool(item["host_links"]) != (item["installation_status"] == "linked") or bool(
            item["host_links"]
        ) != (item["move_safety"] == "keep_anchor"):
            raise ValueError("collection anchor state is inconsistent")
        if (
            not isinstance(item.get("proposed_bucket"), str)
            or not item["proposed_bucket"]
            or CONTROL_CHARS.search(item["proposed_bucket"])
            or not is_nonnegative_int(item.get("duplicate_capability_count"))
            or not isinstance(item.get("scan_errors"), list)
        ):
            raise ValueError("collection source bookkeeping is invalid")
        for error in item["scan_errors"]:
            validate_error(error)
            expected_item_errors.append(error)

    duplicate_fingerprints = {
        fingerprint
        for fingerprint, source_set in (
            (
                fingerprint,
                {row["source_id"] for row in capability_rows if row["fingerprint"] == fingerprint},
            )
            for fingerprint in {row["fingerprint"] for row in capability_rows}
        )
        if len(source_set) > 1
    }
    for item in payload["items"]:
        expected_duplicates = len(
            {row["fingerprint"] for row in item["capabilities"]} & duplicate_fingerprints
        )
        if item["duplicate_capability_count"] != expected_duplicates:
            raise ValueError("collection duplicate counts are inconsistent")

    expected_summary = {
        "source_count": len(payload["items"]),
        "capability_count": len(capability_rows),
        "unique_capability_count": len({row["fingerprint"] for row in capability_rows}),
        "repository_count": sum(item["kind"] == "repository" for item in payload["items"]),
        "document_count": sum(item["kind"] == "document" for item in payload["items"]),
        "archive_count": sum(
            item["kind"] in {"archive", "skill_archive"} for item in payload["items"]
        ),
        "classified_count": sum(
            item["classification"]["status"] != "needs_review" for item in payload["items"]
        ),
        "unclassified_count": sum(
            item["classification"]["status"] == "needs_review" for item in payload["items"]
        ),
        "project_count": sum(
            item["classification"]["primary_type"] in {"project", "composite_asset"}
            for item in payload["items"]
        ),
        "composite_count": sum(
            bool(item["classification"]["is_composite"]) for item in payload["items"]
        ),
        "anchored_source_count": sum(bool(item["host_links"]) for item in payload["items"]),
        "duplicate_group_count": len(duplicate_fingerprints),
        "scan_error_count": len(payload["scan_errors"]),
    }
    for key, expected in expected_summary.items():
        if payload["summary"][key] != expected:
            raise ValueError(f"collection {key} is inconsistent")
    terminal_codes = {"source_root_missing", "source_root_unsafe", "entry_limit"}
    terminal_errors = [
        error for error in payload["scan_errors"] if error["code"] in terminal_codes
    ]
    if terminal_errors:
        if payload["items"] or len(payload["scan_errors"]) != 1:
            raise ValueError("empty collection error state is invalid")
    else:
        prefix_length = len(payload["scan_errors"]) - len(expected_item_errors)
        if prefix_length < 0:
            raise ValueError("collection scan errors are inconsistent")
        root_warnings = payload["scan_errors"][:prefix_length]
        if any(error["code"] != "symlink_skipped" for error in root_warnings) or payload[
            "scan_errors"
        ][prefix_length:] != expected_item_errors:
            raise ValueError("collection scan errors are inconsistent")
    return payload
