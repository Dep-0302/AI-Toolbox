"""Independent, read-only project Skill observation preview.

The production command is deliberately narrow: ``--preview`` scans the one
root and the two registries frozen by the project observation boundary and
prints JSON to stdout.  It has no output-path or source-root option and this
module never writes, executes a process, opens a network connection, or imports
the host inventory scanners.

Tests may call :func:`build_project_skill_snapshot` with an explicit temporary
root and in-memory registries.  Every filesystem read remains descriptor
anchored and no-follow even in that mode.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import errno
import hashlib
import html
import json
import os
import re
import stat
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping


SCHEMA_VERSION = 1
BOUNDARY_ID = "project-skill-observation-boundary-v1"
PROJECTION_VERSION = "projection_sha256_v1"
PRODUCTION_ROOT = Path.home() / "Documents"
PRODUCTION_ROOT_DECLARATION = "~/Documents"
TOOLBOX_ROOT = Path(__file__).resolve().parents[1]
BOUNDARY_PATH = TOOLBOX_ROOT / "registry" / "project_skill_observation_boundary.json"
PROJECTS_PATH = TOOLBOX_ROOT / "registry" / "project_skill_projects.json"
ASSOCIATIONS_PATH = TOOLBOX_ROOT / "registry" / "project_skill_associations.json"
CHINESE_METADATA_PATH = TOOLBOX_ROOT / "registry" / "chinese_metadata.json"
BOUNDARY_DIGESTS = {
    "non_persistent_preview": "3a19faf941ed273409ce4162d0509b4a90455b959f78e1ea700cf6d0fef652bf",
    "persistent_readonly_api": "f4d83b0d7d067a420b40411dfa83bce10f8c5cc5747912d9994c5d6bc9945fc9",
    "readonly_workbench": "a4e689fe9ade14eddce6f2c4bf7b1e9b26b7fb227f0da3077f036e8b421d9efe",
}

LIMITS: dict[str, int | float] = {
    "max_project_candidates": 256,
    "max_entries_per_project": 1000,
    "max_depth": 5,
    "max_manifest_bytes": 262144,
    "max_frontmatter_bytes": 32768,
    "max_text_length": 4000,
    "scan_timeout_seconds": 12.0,
}
FRONTMATTER_FIELDS = (
    "name",
    "description",
    "version",
    "author",
    "license",
    "agent_created",
)
EXCLUDED_NAMES = frozenset(
    {
        ".git",
        "node_modules",
        "dist",
        "build",
        ".cache",
        "__pycache__",
        "tmp",
        "temp",
        "fixtures",
        "test-fixtures",
        "90_归档",
        "archive",
        "archives",
    }
)
ENTRY_POINTS: tuple[dict[str, Any], ...] = (
    {
        "relative_path": ".agents/skills",
        "kind": "host_entry",
        "host_hint": "codex_compatible",
        "binding_semantics": "project_binding_candidate",
    },
    {
        "relative_path": ".claude/skills",
        "kind": "host_specific_entry",
        "host_hint": "claude",
        "binding_semantics": "project_binding_candidate",
    },
    {
        "relative_path": ".codex/skills",
        "kind": "host_specific_or_legacy_entry",
        "host_hint": "codex",
        "binding_semantics": "observed_path_only",
    },
    {
        "relative_path": ".hermes/skills",
        "kind": "host_specific_entry",
        "host_hint": "hermes",
        "binding_semantics": "project_binding_candidate",
    },
    {
        "relative_path": ".workbuddy/skills",
        "kind": "host_specific_entry",
        "host_hint": "workbuddy",
        "binding_semantics": "project_binding_candidate",
    },
    {
        "relative_path": "skills",
        "kind": "generic_source",
        "host_hint": None,
        "binding_semantics": "source_only",
    },
    {
        "relative_path": ".agents/plugins",
        "kind": "plugin_container",
        "host_hint": None,
        "binding_semantics": "plugin_bundled_source",
    },
)

_STABLE_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,239}$")
_FRONTMATTER_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(?:[ \t]*(.*))?$")
_YAML_BLOCK_HEADER = re.compile(r"^([|>])([+-]?)$")
_PLAIN_SCALAR_RESERVED_INITIAL = frozenset("-?:,[]{}#&*!|>'\"%@`")
_SENSITIVE_TEXT = re.compile(
    r"(?:/[U]sers/|file://|https?://|-----BEGIN|<script|api[_ -]?key|authorization|bearer[ \t])",
    re.IGNORECASE,
)


class ScanRejected(Exception):
    """An unsafe scope or path was rejected before target content was read."""

    def __init__(self, code: str, status: str = "security_reject"):
        super().__init__(code)
        self.code = code
        self.status = status


class ObservationFailure(Exception):
    """A bounded observation failed without exposing an OS error or path."""

    def __init__(self, code: str, *, declaration_reason: str | None = None):
        super().__init__(code)
        self.code = code
        self.declaration_reason = declaration_reason


class PayloadValidationError(ValueError):
    pass


def _require_descriptor_safety() -> None:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ScanRejected("descriptor_safety_unavailable")
    if os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd:
        raise ScanRejected("descriptor_safety_unavailable")


@dataclass(frozen=True)
class StatStamp:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, info: os.stat_result) -> "StatStamp":
        return cls(
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    def matches(self, info: os.stat_result) -> bool:
        return self == StatStamp.from_stat(info)


@dataclass
class ManifestProjection:
    projection: dict[str, Any]
    frontmatter_bytes_read: int
    manifest_size: int
    modified_at: str
    stamp: StatStamp


@dataclass
class ProjectContext:
    project_id: str
    project_relative: str
    project_fd: int
    deadline: float
    observed_at: str
    clock: Callable[[], float] = time.monotonic
    entry_budget: int = 0

    def count(self, amount: int = 1) -> None:
        self.entry_budget += amount
        if self.entry_budget > int(LIMITS["max_entries_per_project"]):
            raise ObservationFailure("entry_budget_exceeded")
        _check_deadline(self.deadline, self.clock)


@dataclass
class LinkWitness:
    parent_fd: int
    name: str
    raw_target: str
    stamp: StatStamp

    def close(self) -> None:
        os.close(self.parent_fd)


class IssueLog(list[dict[str, Any]]):
    """Bounded public issues plus an unbounded status-only event ledger."""

    def __init__(self) -> None:
        super().__init__()
        self.status_events: list[str] = []

    def mark(self, status: str) -> None:
        self.status_events.append(status)

    def event_count(self) -> int:
        return len(self.status_events)

    def statuses_since(self, index: int) -> tuple[str, ...]:
        return tuple(self.status_events[index:])


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _regular_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _check_deadline(deadline: float, clock: Callable[[], float] = time.monotonic) -> None:
    if clock() > deadline:
        raise ObservationFailure("scan_timeout")


def _iso_time(value: datetime | str | Callable[[], datetime | str] | None = None) -> str:
    if callable(value):
        value = value()
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_relative(value: str) -> tuple[str, ...]:
    path = PurePosixPath(value)
    if (
        not value
        or len(value) > int(LIMITS["max_text_length"])
        or path.is_absolute()
        or re.match(r"^[A-Za-z]:", value)
        or "\\" in value
        or "\x00" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ScanRejected("unsafe_registry_path")
    return tuple(path.parts)


def _schema_safe_text(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= int(LIMITS["max_text_length"])
        and not _SENSITIVE_TEXT.search(value)
    )


def _valid_date(value: Any) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _valid_datetime(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _safe_component(value: str) -> bool:
    return (
        bool(value)
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
    )


def _safe_output_text(value: str) -> str:
    if len(value) > int(LIMITS["max_text_length"]):
        raise ObservationFailure("text_length_exceeded")
    escaped = html.escape(value.strip(), quote=True)
    if not escaped:
        raise ObservationFailure("invalid_frontmatter")
    if _SENSITIVE_TEXT.search(escaped):
        return "[redacted]"
    return escaped


def _slug_hash(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}:{digest}"


def _open_absolute_directory(path: Path) -> int:
    absolute = path.expanduser().absolute()
    parts = absolute.parts
    if not absolute.is_absolute() or not parts or parts[0] != os.sep:
        raise ScanRejected("observation_root_invalid")
    descriptor = os.open(os.sep, _directory_flags())
    try:
        for part in parts[1:]:
            if not _safe_component(part):
                raise ScanRejected("observation_root_invalid")
            try:
                before = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if stat.S_ISLNK(before.st_mode):
                    raise ScanRejected("observation_root_symlink")
                if not stat.S_ISDIR(before.st_mode):
                    raise ScanRejected("observation_root_not_directory")
                next_descriptor = os.open(part, _directory_flags(), dir_fd=descriptor)
                try:
                    opened_info = os.fstat(next_descriptor)
                except OSError:
                    os.close(next_descriptor)
                    raise ScanRejected("observation_root_unavailable", "error") from None
                if not StatStamp.from_stat(before).matches(opened_info):
                    os.close(next_descriptor)
                    raise ScanRejected("observation_root_changed")
            except ScanRejected:
                raise
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.EMLINK}:
                    raise ScanRejected("observation_root_symlink") from None
                raise ScanRejected("observation_root_unavailable", "error") from None
            os.close(descriptor)
            descriptor = next_descriptor
        try:
            root_info = os.fstat(descriptor)
        except OSError:
            raise ScanRejected("observation_root_unavailable", "error") from None
        if not stat.S_ISDIR(root_info.st_mode):
            raise ScanRejected("observation_root_not_directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _verify_absolute_directory_identity(path: Path, expected: StatStamp) -> None:
    try:
        descriptor = _open_absolute_directory(path)
        try:
            try:
                after = os.fstat(descriptor)
            except OSError:
                raise ScanRejected("observation_root_changed") from None
            if not expected.matches(after):
                raise ScanRejected("observation_root_changed")
        finally:
            os.close(descriptor)
    except ScanRejected:
        raise ScanRejected("observation_root_changed") from None


def _open_directory_parts(
    root_fd: int,
    parts: Iterable[str],
    *,
    missing_ok: bool = False,
    symlink_code: str = "path_symlink",
    ancestor_symlink_code: str | None = None,
) -> int | None:
    components = tuple(parts)
    descriptor = os.dup(root_fd)
    try:
        for index, part in enumerate(components):
            if not _safe_component(part):
                raise ScanRejected("unsafe_relative_path")
            try:
                before = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                if missing_ok:
                    os.close(descriptor)
                    return None
                raise ObservationFailure("path_missing") from None
            except PermissionError:
                raise ObservationFailure("permission_denied") from None
            except OSError:
                raise ObservationFailure("path_unreadable") from None
            if stat.S_ISLNK(before.st_mode):
                code = (
                    ancestor_symlink_code
                    if ancestor_symlink_code is not None and index < len(components) - 1
                    else symlink_code
                )
                raise ScanRejected(code)
            if not stat.S_ISDIR(before.st_mode):
                raise ObservationFailure("path_not_directory")
            try:
                next_descriptor = os.open(part, _directory_flags(), dir_fd=descriptor)
            except FileNotFoundError:
                raise ObservationFailure("path_race") from None
            except PermissionError:
                raise ObservationFailure("permission_denied") from None
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.EMLINK}:
                    raise ObservationFailure("path_race") from None
                raise ObservationFailure("path_unreadable") from None
            try:
                opened_info = os.fstat(next_descriptor)
            except OSError:
                os.close(next_descriptor)
                raise ObservationFailure("path_unreadable") from None
            if not StatStamp.from_stat(before).matches(opened_info):
                os.close(next_descriptor)
                raise ObservationFailure("path_race")
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_regular_at(parent_fd: int, name: str, *, missing_ok: bool = False) -> tuple[int, StatStamp] | None:
    if not _safe_component(name):
        raise ScanRejected("unsafe_relative_path")
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ObservationFailure("manifest_missing") from None
    except PermissionError:
        raise ObservationFailure("permission_denied") from None
    except OSError:
        raise ObservationFailure("manifest_unreadable") from None
    if stat.S_ISLNK(before.st_mode):
        raise ObservationFailure("path_race")
    if not stat.S_ISREG(before.st_mode):
        raise ObservationFailure("manifest_not_regular")
    try:
        descriptor = os.open(name, _regular_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        raise ObservationFailure("path_race") from None
    except PermissionError:
        raise ObservationFailure("permission_denied") from None
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.EMLINK}:
            raise ObservationFailure("path_race") from None
        raise ObservationFailure("manifest_unreadable") from None
    stamp = StatStamp.from_stat(before)
    try:
        opened_info = os.fstat(descriptor)
    except OSError:
        os.close(descriptor)
        raise ObservationFailure("manifest_unreadable") from None
    if not stamp.matches(opened_info):
        os.close(descriptor)
        raise ObservationFailure("path_race")
    return descriptor, stamp


def _read_one_line(
    fd: int,
    budget_remaining: int,
    deadline: float,
    clock: Callable[[], float],
) -> bytes:
    if budget_remaining <= 0:
        raise ObservationFailure("frontmatter_budget_exceeded")
    data = bytearray()
    while len(data) < budget_remaining:
        if not len(data) % 256:
            _check_deadline(deadline, clock)
        try:
            piece = os.read(fd, 1)
        except PermissionError:
            raise ObservationFailure("permission_denied") from None
        except OSError:
            raise ObservationFailure("manifest_unreadable") from None
        if not piece:
            return bytes(data)
        data.extend(piece)
        if piece == b"\n":
            return bytes(data)
    raise ObservationFailure("frontmatter_budget_exceeded")


def _read_frontmatter(
    fd: int,
    stamp: StatStamp,
    deadline: float,
    clock: Callable[[], float],
) -> tuple[bytes, int]:
    if stamp.size > int(LIMITS["max_manifest_bytes"]):
        raise ObservationFailure("manifest_budget_exceeded")
    maximum = int(LIMITS["max_frontmatter_bytes"])
    consumed = 0
    first = _read_one_line(fd, maximum, deadline, clock)
    consumed += len(first)
    if first.rstrip(b"\r\n") != b"---":
        raise ObservationFailure("invalid_frontmatter")
    lines: list[bytes] = []
    while consumed <= maximum:
        line = _read_one_line(fd, maximum - consumed, deadline, clock)
        consumed += len(line)
        if consumed > maximum:
            raise ObservationFailure("frontmatter_budget_exceeded")
        if not line:
            raise ObservationFailure("invalid_frontmatter")
        if line.rstrip(b"\r\n") == b"---":
            try:
                after = os.fstat(fd)
            except OSError:
                raise ObservationFailure("manifest_unreadable") from None
            if not stamp.matches(after):
                raise ObservationFailure("path_race")
            return b"".join(lines), consumed
        lines.append(line)
    raise ObservationFailure("frontmatter_budget_exceeded")


def _parse_scalar(raw: str, key: str) -> Any:
    value = _validate_yaml_scalar(raw, reject_flow=True)
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ObservationFailure("invalid_frontmatter") from None
        if not isinstance(parsed, str):
            raise ObservationFailure("unsafe_frontmatter")
        value = parsed
    elif value.startswith("'"):
        value = value[1:-1].replace("''", "'")
    if key == "agent_created" and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return _safe_output_text(value)


def _yaml_syntax_view(value: str) -> str | None:
    """Mask quoted text so YAML control tokens are checked only as syntax."""

    output: list[str] = []
    quote: str | None = None
    first_content = len(value) - len(value.lstrip())
    index = 0
    while index < len(value):
        character = value[index]
        if quote == '"':
            output.append(" ")
            if character == "\\" and index + 1 < len(value):
                index += 1
                output.append(" ")
            elif character == '"':
                quote = None
        elif quote == "'":
            output.append(" ")
            if character == "'":
                if index + 1 < len(value) and value[index + 1] == "'":
                    index += 1
                    output.append(" ")
                else:
                    quote = None
        elif character in {'"', "'"} and index == first_content:
            quote = character
            output.append(" ")
        else:
            output.append(character)
        index += 1
    return None if quote is not None else "".join(output)


def _yaml_scalar_parts(value: str) -> tuple[str, str] | None:
    visible = _yaml_syntax_view(value)
    if visible is None:
        return None
    for index, character in enumerate(visible):
        if character == "#" and (index == 0 or visible[index - 1].isspace()):
            return value[:index].strip(), visible[:index].strip()
    return value.strip(), visible.strip()


def _has_disallowed_control(value: str, *, allow_line_breaks: bool) -> bool:
    for character in value:
        codepoint = ord(character)
        if codepoint < 0x20 and not (allow_line_breaks and character in "\r\n"):
            return True
        if 0x7F <= codepoint <= 0x9F:
            return True
        if 0xD800 <= codepoint <= 0xDFFF or 0xFDD0 <= codepoint <= 0xFDEF:
            return True
        if (codepoint & 0xFFFF) in {0xFFFE, 0xFFFF}:
            return True
    return False


def _unsafe_yaml_syntax(value: str, *, reject_flow: bool) -> bool:
    parts = _yaml_scalar_parts(value)
    if parts is None:
        return True
    _source, visible = parts
    stripped = visible.strip()
    if reject_flow and stripped.startswith(("[", "{")):
        return True
    if re.search(r"(?:^|[\s\[\]{},:])(?:!|&[A-Za-z0-9_-]|\*[A-Za-z0-9_-])", visible):
        return True
    return bool(re.search(r"(?:^|[\s,{])<<\s*:", visible))


def _validate_yaml_scalar(raw: str, *, reject_flow: bool) -> str:
    if _has_disallowed_control(raw, allow_line_breaks=False):
        raise ObservationFailure("unsafe_frontmatter")
    parts = _yaml_scalar_parts(raw)
    if parts is None:
        raise ObservationFailure("unsafe_frontmatter")
    value, visible = parts
    if not value:
        raise ObservationFailure("unsafe_frontmatter")
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ObservationFailure("unsafe_frontmatter") from None
        if not isinstance(decoded, str):
            raise ObservationFailure("unsafe_frontmatter")
        if _has_disallowed_control(decoded, allow_line_breaks=False):
            raise ObservationFailure("unsafe_frontmatter")
        return value
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ObservationFailure("unsafe_frontmatter")
        interior = value[1:-1]
        index = 0
        while index < len(interior):
            if interior[index] == "'":
                if index + 1 >= len(interior) or interior[index + 1] != "'":
                    raise ObservationFailure("unsafe_frontmatter")
                index += 2
                continue
            index += 1
        return value
    if value[0] in _PLAIN_SCALAR_RESERVED_INITIAL:
        raise ObservationFailure("unsafe_frontmatter")
    if re.search(r":(?:[ \t]|$)", visible):
        raise ObservationFailure("unsafe_frontmatter")
    if _unsafe_yaml_syntax(value, reject_flow=reject_flow):
        raise ObservationFailure("unsafe_frontmatter")
    return value


def _split_top_level_yaml(text: str) -> list[tuple[str, str, list[str]]]:
    lines = text.splitlines()
    items: list[tuple[str, str, list[str]]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        if "\t" in line or line[:1].isspace():
            raise ObservationFailure("unsafe_frontmatter")
        match = _FRONTMATTER_KEY.fullmatch(line)
        if not match:
            raise ObservationFailure("unsafe_frontmatter")
        key, raw = match.groups()
        index += 1
        children: list[str] = []
        while index < len(lines):
            child = lines[index]
            if "\t" in child:
                raise ObservationFailure("unsafe_frontmatter")
            if not child.strip() or child[:1].isspace():
                children.append(child)
                index += 1
                continue
            break
        items.append((key, raw or "", children))
    return items


def _block_scalar_text(lines: list[str], style: str) -> str:
    nonempty = [line for line in lines if line.strip()]
    if not nonempty:
        raise ObservationFailure("invalid_frontmatter")
    base_indent = len(nonempty[0]) - len(nonempty[0].lstrip(" "))
    if base_indent <= 0:
        raise ObservationFailure("unsafe_frontmatter")
    values: list[str] = []
    for line in lines:
        if "\t" in line:
            raise ObservationFailure("unsafe_frontmatter")
        if not line.strip():
            values.append("")
            continue
        indentation = len(line) - len(line.lstrip(" "))
        if indentation < base_indent:
            raise ObservationFailure("unsafe_frontmatter")
        values.append(line[base_indent:])
    if style == "|":
        return "\n".join(values)
    folded = ""
    previous_blank = False
    for value in values:
        if not value:
            folded += "\n"
            previous_blank = True
        else:
            if folded and not folded.endswith("\n") and not previous_blank:
                folded += " "
            folded += value
            previous_blank = False
    return folded


def _validate_ignored_yaml_subtree(lines: list[str], *, reject_flow: bool) -> None:
    indentation_levels: set[int] = set()
    previous_indent: int | None = None
    previous_allows_children = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if "\t" in line:
            raise ObservationFailure("unsafe_frontmatter")
        if not line.strip() or line.lstrip().startswith("#"):
            index += 1
            continue
        indentation = len(line) - len(line.lstrip(" "))
        if indentation <= 0:
            raise ObservationFailure("unsafe_frontmatter")
        if previous_indent is not None:
            if indentation > previous_indent:
                if not previous_allows_children:
                    raise ObservationFailure("unsafe_frontmatter")
            elif indentation < previous_indent:
                for level in tuple(indentation_levels):
                    if level > indentation:
                        indentation_levels.remove(level)
                if indentation not in indentation_levels:
                    raise ObservationFailure("unsafe_frontmatter")
        content = line[indentation:]
        if content == "-" or content.startswith("- "):
            raise ObservationFailure("unsafe_frontmatter")
        indentation_levels.add(indentation)
        if content.startswith("<<:"):
            raise ObservationFailure("unsafe_frontmatter")
        match = _FRONTMATTER_KEY.fullmatch(content)
        if match is None:
            raise ObservationFailure("unsafe_frontmatter")
        scalar = match.group(2) or ""
        parts = _yaml_scalar_parts(scalar)
        if parts is None:
            raise ObservationFailure("unsafe_frontmatter")
        scalar_source, _visible = parts
        header = _YAML_BLOCK_HEADER.fullmatch(scalar_source)
        if scalar_source and header is None:
            _validate_yaml_scalar(scalar, reject_flow=reject_flow)
        allows_children = not scalar_source
        index += 1
        if header:
            block_lines: list[str] = []
            while index < len(lines):
                block_line = lines[index]
                if "\t" in block_line:
                    raise ObservationFailure("unsafe_frontmatter")
                if block_line.strip():
                    block_indent = len(block_line) - len(block_line.lstrip(" "))
                    if block_indent <= indentation:
                        break
                block_lines.append(block_line)
                index += 1
            _block_scalar_text(block_lines, header.group(1))
            allows_children = False
        previous_indent = indentation
        previous_allows_children = allows_children


def _validate_ignored_yaml_value(
    raw: str,
    children: list[str],
    *,
    reject_flow: bool,
) -> None:
    parts = _yaml_scalar_parts(raw)
    if parts is None:
        raise ObservationFailure("unsafe_frontmatter")
    scalar_source, _visible = parts
    header = _YAML_BLOCK_HEADER.fullmatch(scalar_source)
    material_children = any(
        line.strip() and not line.lstrip().startswith("#") for line in children
    )
    if header:
        _block_scalar_text(children, header.group(1))
    else:
        if scalar_source:
            _validate_yaml_scalar(raw, reject_flow=reject_flow)
        if material_children:
            if scalar_source:
                raise ObservationFailure("unsafe_frontmatter")
            _validate_ignored_yaml_subtree(children, reject_flow=reject_flow)


def _plain_multiline_text(lines: list[str]) -> str:
    content_lines = [
        line for line in lines if line.strip() and not line.lstrip().startswith("#")
    ]
    if not content_lines:
        raise ObservationFailure("invalid_frontmatter")
    base_indent = len(content_lines[0]) - len(content_lines[0].lstrip(" "))
    if base_indent <= 0:
        raise ObservationFailure("unsafe_frontmatter")
    values: list[str] = []
    for line in lines:
        if "\t" in line:
            raise ObservationFailure("unsafe_frontmatter")
        if not line.strip():
            values.append("")
            continue
        if line.lstrip().startswith("#"):
            continue
        indentation = len(line) - len(line.lstrip(" "))
        if indentation != base_indent:
            raise ObservationFailure("unsafe_frontmatter")
        raw_value = line[base_indent:]
        parts = _yaml_scalar_parts(raw_value)
        if parts is None:
            raise ObservationFailure("unsafe_frontmatter")
        scalar_source, _visible = parts
        if not scalar_source:
            continue
        if scalar_source[0] in {'"', "'"}:
            raise ObservationFailure("unsafe_frontmatter")
        if _FRONTMATTER_KEY.fullmatch(scalar_source):
            raise ObservationFailure("unsafe_frontmatter")
        values.append(_validate_yaml_scalar(raw_value, reject_flow=True))
    return _block_scalar_text([" " + value if value else "" for value in values], ">")


def _parse_frontmatter(data: bytes) -> dict[str, Any]:
    try:
        text = data.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise ObservationFailure("invalid_encoding") from None
    if _has_disallowed_control(text, allow_line_breaks=True):
        raise ObservationFailure("unsafe_frontmatter")
    projection: dict[str, Any] = {field: None for field in FRONTMATTER_FIELDS}
    seen: set[str] = set()
    for key, raw, children in _split_top_level_yaml(text):
        if key not in projection:
            _validate_ignored_yaml_value(raw, children, reject_flow=False)
            continue
        if key in seen:
            raise ObservationFailure("unsafe_frontmatter")
        seen.add(key)
        parts = _yaml_scalar_parts(raw)
        if parts is None:
            raise ObservationFailure("unsafe_frontmatter")
        scalar_source, _visible = parts
        header = _YAML_BLOCK_HEADER.fullmatch(scalar_source)
        if header:
            if key == "agent_created":
                raise ObservationFailure("unsafe_frontmatter")
            projection[key] = _safe_output_text(
                _block_scalar_text(children, header.group(1))
            )
        elif not scalar_source and children:
            if key == "agent_created":
                raise ObservationFailure("unsafe_frontmatter")
            projection[key] = _safe_output_text(_plain_multiline_text(children))
        elif children:
            raise ObservationFailure("unsafe_frontmatter")
        else:
            projection[key] = _parse_scalar(raw, key)
    return projection


def _project_manifest(
    skill_fd: int,
    deadline: float,
    clock: Callable[[], float],
) -> ManifestProjection:
    opened = _open_regular_at(skill_fd, "SKILL.md")
    assert opened is not None
    descriptor, stamp = opened
    try:
        frontmatter, bytes_read = _read_frontmatter(descriptor, stamp, deadline, clock)
        projection = _parse_frontmatter(frontmatter)
        try:
            after = os.fstat(descriptor)
        except OSError:
            raise ObservationFailure("manifest_unreadable") from None
        if not stamp.matches(after):
            raise ObservationFailure("path_race")
        return ManifestProjection(
            projection=projection,
            frontmatter_bytes_read=bytes_read,
            manifest_size=stamp.size,
            modified_at=_iso_time(datetime.fromtimestamp(stamp.mtime_ns / 1_000_000_000, timezone.utc)),
            stamp=stamp,
        )
    finally:
        os.close(descriptor)


def _verify_manifest_identity(
    project_fd: int,
    canonical_parts: tuple[str, ...],
    expected: StatStamp,
) -> None:
    """Reopen the approved leaf from the project anchor after observation."""

    try:
        skill_fd = _open_directory_parts(project_fd, canonical_parts)
        assert skill_fd is not None
        try:
            opened = _open_regular_at(skill_fd, "SKILL.md")
            assert opened is not None
            manifest_fd, observed = opened
            try:
                try:
                    after = os.fstat(manifest_fd)
                except OSError:
                    raise ObservationFailure("path_race") from None
                if observed != expected or not expected.matches(after):
                    raise ObservationFailure("path_race")
            finally:
                os.close(manifest_fd)
        finally:
            os.close(skill_fd)
    except (ScanRejected, ObservationFailure):
        raise ObservationFailure("path_race") from None


def _verify_directory_identity(
    root_fd: int,
    parts: tuple[str, ...],
    expected: StatStamp,
) -> None:
    try:
        descriptor = _open_directory_parts(root_fd, parts)
        assert descriptor is not None
        try:
            try:
                after = os.fstat(descriptor)
            except OSError:
                raise ObservationFailure("path_race") from None
            if not expected.matches(after):
                raise ObservationFailure("path_race")
        finally:
            os.close(descriptor)
    except (ScanRejected, ObservationFailure):
        raise ObservationFailure("path_race") from None


def _read_bounded_regular(
    fd: int,
    stamp: StatStamp,
    limit: int,
    deadline: float,
    clock: Callable[[], float],
) -> bytes:
    if stamp.size > limit:
        raise ObservationFailure("over_limit")
    data = bytearray()
    while len(data) <= limit:
        _check_deadline(deadline, clock)
        try:
            piece = os.read(fd, min(4096, limit + 1 - len(data)))
        except PermissionError:
            raise ObservationFailure("permission_denied") from None
        except OSError:
            raise ObservationFailure("manifest_unreadable") from None
        if not piece:
            break
        data.extend(piece)
    if len(data) > limit:
        raise ObservationFailure("over_limit")
    try:
        after = os.fstat(fd)
    except OSError:
        raise ObservationFailure("manifest_unreadable") from None
    if not stamp.matches(after):
        raise ObservationFailure("path_race")
    return bytes(data)


def _read_openai_declaration(
    skill_fd: int,
    deadline: float,
    clock: Callable[[], float],
) -> tuple[dict[str, Any], str | None]:
    try:
        agents_fd = _open_directory_parts(skill_fd, ("agents",), missing_ok=True)
        if agents_fd is None:
            return {"status": "not_observed"}, None
        try:
            opened = _open_regular_at(agents_fd, "openai.yaml", missing_ok=True)
            if opened is None:
                return {"status": "not_observed"}, None
            descriptor, stamp = opened
            try:
                data = _read_bounded_regular(
                    descriptor,
                    stamp,
                    int(LIMITS["max_frontmatter_bytes"]),
                    deadline,
                    clock,
                )
            finally:
                os.close(descriptor)
        finally:
            os.close(agents_fd)
    except ScanRejected:
        return {"status": "unverified", "reason": "security_reject"}, "openai_security_reject"
    except ObservationFailure as exc:
        reasons = {
            "permission_denied": "permission_denied",
            "over_limit": "over_limit",
            "path_race": "path_race",
        }
        return {
            "status": "unverified",
            "reason": reasons.get(exc.code, "scan_incomplete"),
        }, f"openai_{exc.code}"
    try:
        text = data.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return {"status": "unverified", "reason": "invalid_yaml"}, "openai_invalid_yaml"
    if _has_disallowed_control(text, allow_line_breaks=True):
        return {"status": "unverified", "reason": "invalid_yaml"}, "openai_invalid_yaml"
    try:
        items = _split_top_level_yaml(text)
    except ObservationFailure:
        return {"status": "unverified", "reason": "invalid_yaml"}, "openai_invalid_yaml"
    policy_seen = False
    found: bool | None = None
    try:
        for key, raw, children in items:
            if key != "policy":
                _validate_ignored_yaml_value(raw, children, reject_flow=True)
                continue
            policy_parts = _yaml_scalar_parts(raw)
            if policy_parts is None or _unsafe_yaml_syntax(raw, reject_flow=True):
                return {"status": "unverified", "reason": "invalid_yaml"}, "openai_invalid_yaml"
            policy_source, _visible = policy_parts
            if policy_seen or policy_source:
                return {"status": "unverified", "reason": "invalid_yaml"}, "openai_invalid_yaml"
            policy_seen = True
            direct_rows = [
                (index, line)
                for index, line in enumerate(children)
                if line.strip() and not line.lstrip().startswith("#")
            ]
            if not direct_rows:
                continue
            direct_indent = len(direct_rows[0][1]) - len(direct_rows[0][1].lstrip(" "))
            if direct_indent <= 0:
                return {"status": "unverified", "reason": "invalid_yaml"}, "openai_invalid_yaml"
            child_index = 0
            while child_index < len(children):
                line = children[child_index]
                if not line.strip() or line.lstrip().startswith("#"):
                    child_index += 1
                    continue
                indentation = len(line) - len(line.lstrip(" "))
                if indentation != direct_indent:
                    return {"status": "unverified", "reason": "invalid_yaml"}, "openai_invalid_yaml"
                direct = line[indentation:]
                match = _FRONTMATTER_KEY.fullmatch(direct)
                if not match:
                    return {"status": "unverified", "reason": "invalid_yaml"}, "openai_invalid_yaml"
                direct_key, direct_raw = match.groups()
                child_index += 1
                nested: list[str] = []
                while child_index < len(children):
                    nested_line = children[child_index]
                    if not nested_line.strip():
                        nested.append(nested_line)
                        child_index += 1
                        continue
                    nested_indent = len(nested_line) - len(nested_line.lstrip(" "))
                    if nested_indent > direct_indent:
                        nested.append(nested_line)
                        child_index += 1
                        continue
                    break
                direct_raw = direct_raw or ""
                direct_parts = _yaml_scalar_parts(direct_raw)
                if direct_parts is None or _unsafe_yaml_syntax(direct_raw, reject_flow=True):
                    return {"status": "unverified", "reason": "invalid_yaml"}, "openai_invalid_yaml"
                direct_source, _visible = direct_parts
                if direct_key == "allow_implicit_invocation":
                    declaration = re.fullmatch(r"(true|false)", direct_source, re.I)
                    nested_material = any(
                        line.strip() and not line.lstrip().startswith("#")
                        for line in nested
                    )
                    if declaration is None or nested_material or found is not None:
                        return {"status": "unverified", "reason": "invalid_yaml"}, "openai_invalid_yaml"
                    found = declaration.group(1).lower() == "true"
                else:
                    _validate_ignored_yaml_value(
                        direct_raw,
                        nested,
                        reject_flow=True,
                    )
    except ObservationFailure:
        return {"status": "unverified", "reason": "invalid_yaml"}, "openai_invalid_yaml"
    if found is None:
        return {"status": "not_observed"}, None
    return {"status": "declared", "allow_implicit_invocation": found}, None


def _projection_digest(projection: Mapping[str, Any], declaration: Mapping[str, Any]) -> str:
    approved_declaration: dict[str, Any] = {"status": declaration["status"]}
    if declaration["status"] == "declared":
        approved_declaration["allow_implicit_invocation"] = declaration["allow_implicit_invocation"]
    elif declaration["status"] == "unverified":
        approved_declaration["reason"] = declaration["reason"]
    value = {
        "frontmatter": {field: projection.get(field) for field in FRONTMATTER_FIELDS},
        "openai_declaration": approved_declaration,
        "version": PROJECTION_VERSION,
    }
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _binding_status(entry: Mapping[str, Any]) -> str:
    return {
        "project_binding_candidate": "observed_binding",
        "source_only": "source_only",
        "plugin_bundled_source": "plugin_bundled_source",
        "observed_path_only": "observed_path_only",
    }[str(entry["binding_semantics"])]


def _source_kind(entry: Mapping[str, Any], is_link: bool) -> str:
    if is_link:
        return "same_project_symlink"
    return {
        "project_binding_candidate": "entity",
        "source_only": "generic_source",
        "plugin_bundled_source": "plugin_bundled_source",
        "observed_path_only": "observed_path_only",
    }[str(entry["binding_semantics"])]


def _issue(
    issues: list[dict[str, Any]],
    *,
    status: str,
    code: str,
    project_id: str | None = None,
    relative_path: str | None = None,
) -> str:
    if isinstance(issues, IssueLog):
        issues.mark(status)
    if len(issues) >= 999:
        for row in issues:
            if row["code"] == "issue_budget_exceeded":
                priority = {"partial": 1, "error": 2, "security_reject": 3}
                if priority[status] > priority[str(row["status"])]:
                    row["status"] = status
                return str(row["issue_id"])
        issue_id = _slug_hash("issue", "scope", "issue_budget_exceeded")
        issues.append(
            {
                "issue_id": issue_id,
                "status": status,
                "code": "issue_budget_exceeded",
                "message": "Additional failures were suppressed by the issue budget.",
            }
        )
        return issue_id
    issue_id = _slug_hash("issue", project_id or "scope", relative_path or "", code, str(len(issues)))
    messages = {
        "broken_link": "Skill entry link is broken; its target was not read.",
        "outside_project_link": "Skill entry link leaves the registered project; its target was not read.",
        "loop_link": "Skill entry link is cyclic; its target was not read.",
        "path_race": "An observed path changed during the read and was rejected.",
        "permission_denied": "An approved path could not be read with current permissions.",
        "scan_timeout": "The bounded foreground scan exceeded its time budget.",
        "entry_budget_exceeded": "The project entry budget was exceeded.",
        "manifest_budget_exceeded": "A manifest exceeded the approved file-size budget.",
        "frontmatter_budget_exceeded": "Manifest frontmatter exceeded the approved byte budget.",
        "text_length_exceeded": "An approved metadata value exceeded the text budget.",
        "invalid_frontmatter": "Manifest frontmatter is incomplete or invalid.",
        "unsafe_frontmatter": "Manifest frontmatter used unsupported YAML constructs.",
        "invalid_encoding": "Approved metadata was not valid UTF-8.",
        "openai_invalid_yaml": "The approved invocation declaration could not be safely parsed.",
        "openai_over_limit": "The approved invocation declaration exceeded its byte budget.",
        "openai_permission_denied": "The approved invocation declaration could not be read.",
        "openai_path_race": "The invocation declaration changed during observation.",
        "openai_security_reject": "The invocation declaration path was rejected by the link policy.",
        "project_root_missing": "The registered project root is missing.",
        "project_root_symlink": "The registered project root or ancestor is a symbolic link.",
        "observation_root_symlink": "The observation root or ancestor is a symbolic link.",
        "candidate_budget_exceeded": "The top-level candidate budget was exceeded.",
    }
    row: dict[str, Any] = {
        "issue_id": issue_id,
        "status": status,
        "code": code if _STABLE_ID.fullmatch(code) else "observation_failure",
        "message": messages.get(code, "An approved observation failed closed."),
    }
    if project_id:
        row["project_id"] = project_id
    if relative_path:
        row["relative_path"] = relative_path
    issues.append(row)
    return issue_id


def _status_from_issues(
    issue_rows: Iterable[Mapping[str, Any]],
    *,
    default: str = "complete",
    status_events: Iterable[str] | None = None,
) -> str:
    statuses = set(status_events or ())
    statuses.update(str(row["status"]) for row in issue_rows)
    if isinstance(issue_rows, IssueLog):
        statuses.update(issue_rows.status_events)
    if "security_reject" in statuses:
        return "security_reject"
    if "error" in statuses:
        return "error"
    if "partial" in statuses:
        return "partial"
    return default


def _normalize_link_target(link_parent: tuple[str, ...], raw_target: str) -> tuple[str, ...] | None:
    if not raw_target or raw_target.startswith(("/", "\\")) or "\x00" in raw_target or "\\" in raw_target:
        return None
    parts = list(link_parent)
    for part in PurePosixPath(raw_target).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif _safe_component(part):
            parts.append(part)
        else:
            return None
    return tuple(parts)


def _open_skill_directory(
    context: ProjectContext,
    entry_fd: int,
    entry_parts: tuple[str, ...],
    child_name: str,
) -> tuple[int, LinkWitness | None, tuple[str, ...] | None, str | None]:
    try:
        info = os.stat(child_name, dir_fd=entry_fd, follow_symlinks=False)
    except (FileNotFoundError, PermissionError, OSError):
        return -1, None, None, "path_race"
    if stat.S_ISDIR(info.st_mode):
        try:
            child_fd = os.open(child_name, _directory_flags(), dir_fd=entry_fd)
        except OSError:
            return -1, None, None, "path_race"
        try:
            opened_info = os.fstat(child_fd)
        except OSError:
            os.close(child_fd)
            return -1, None, None, "path_race"
        if not StatStamp.from_stat(info).matches(opened_info):
            os.close(child_fd)
            return -1, None, None, "path_race"
        return child_fd, None, entry_parts + (child_name,), None
    if not stat.S_ISLNK(info.st_mode):
        return -1, None, None, None
    try:
        raw_target = os.readlink(child_name, dir_fd=entry_fd)
    except OSError:
        return -1, None, None, "path_race"
    resolved = _normalize_link_target(entry_parts, raw_target)
    if resolved is None:
        return -1, None, None, "outside_project_link"
    try:
        target_fd = _open_directory_parts(context.project_fd, resolved)
    except ScanRejected:
        return -1, None, resolved, "loop_link"
    except ObservationFailure as exc:
        if exc.code == "path_missing":
            return -1, None, resolved, "broken_link"
        if exc.code in {"path_unreadable", "path_not_directory"}:
            return -1, None, resolved, "broken_link"
        return -1, None, resolved, exc.code
    assert target_fd is not None
    # The raw link itself must still be the same link after the target is open.
    try:
        after = os.stat(child_name, dir_fd=entry_fd, follow_symlinks=False)
        after_target = os.readlink(child_name, dir_fd=entry_fd)
    except OSError:
        os.close(target_fd)
        return -1, None, resolved, "path_race"
    if not StatStamp.from_stat(info).matches(after) or after_target != raw_target:
        os.close(target_fd)
        return -1, None, resolved, "path_race"
    try:
        witness_fd = os.dup(entry_fd)
    except OSError:
        os.close(target_fd)
        return -1, None, resolved, "path_race"
    return (
        target_fd,
        LinkWitness(witness_fd, child_name, raw_target, StatStamp.from_stat(info)),
        resolved,
        None,
    )


def _verify_link_witness(witness: LinkWitness) -> None:
    try:
        info = os.stat(witness.name, dir_fd=witness.parent_fd, follow_symlinks=False)
        raw_target = os.readlink(witness.name, dir_fd=witness.parent_fd)
    except OSError:
        raise ObservationFailure("path_race") from None
    if not witness.stamp.matches(info) or raw_target != witness.raw_target:
        raise ObservationFailure("path_race")


def _observe_skill(
    context: ProjectContext,
    entry: Mapping[str, Any],
    skill_fd: int,
    visible_parts: tuple[str, ...],
    canonical_parts: tuple[str, ...],
    *,
    is_link: bool,
    issues: list[dict[str, Any]],
) -> tuple[tuple[int, int], dict[str, Any]]:
    _check_deadline(context.deadline, context.clock)
    manifest = _project_manifest(skill_fd, context.deadline, context.clock)
    declaration, declaration_issue = _read_openai_declaration(
        skill_fd, context.deadline, context.clock
    )
    _verify_manifest_identity(context.project_fd, canonical_parts, manifest.stamp)
    if declaration_issue:
        _issue(
            issues,
            status="partial",
            code=declaration_issue,
            project_id=context.project_id,
            relative_path="/".join(visible_parts + ("agents", "openai.yaml")),
        )
    manifest_relative = "/".join(visible_parts + ("SKILL.md",))
    canonical_manifest = "/".join(canonical_parts + ("SKILL.md",))
    identity = (manifest.stamp.device, manifest.stamp.inode)
    source_kind = _source_kind(entry, is_link)
    observation: dict[str, Any] = {
        "observation_id": _slug_hash(
            "observation", context.project_id, manifest_relative, source_kind
        ),
        "entry": dict(entry),
        "manifest_relative_path": manifest_relative,
        "source_kind": source_kind,
        "file_type": "symlink" if is_link else "regular_file",
        "link_status": "healthy_same_project" if is_link else "not_applicable",
        # Depth is measured beneath the approved entry, including SKILL.md.
        # Normal entries are 2; the exact plugin shape is 4 (both within 5).
        "observation_depth": len(visible_parts) - len(str(entry["relative_path"]).split("/")) + 1,
        "manifest_size_bytes": manifest.manifest_size,
        "frontmatter_bytes_read": manifest.frontmatter_bytes_read,
        "manifest_modified_at": manifest.modified_at,
        "frontmatter_projection": manifest.projection,
        "projection_sha256_v1": _projection_digest(manifest.projection, declaration),
        "openai_declaration": declaration,
        "evidence": {
            "file_discovery": {"status": "observed"},
            "project_binding": {"status": _binding_status(entry)},
            "host_availability": {"status": "unverified"},
            "invocation_eligibility": {
                "status": {
                    "declared": "declaration_only",
                    "not_observed": "not_observed",
                    "unverified": "unverified",
                }[declaration["status"]]
            },
            "actual_use": {"status": "not_connected"},
        },
        "observed_at": context.observed_at,
        "_canonical_manifest": canonical_manifest,
    }
    if is_link:
        observation["resolved_relative_path"] = canonical_manifest
    return identity, observation


def _directory_names(descriptor: int, context: ProjectContext) -> list[str]:
    try:
        with os.scandir(descriptor) as iterator:
            names: list[str] = []
            for item in iterator:
                context.count()
                names.append(item.name)
    except PermissionError:
        raise ObservationFailure("permission_denied") from None
    except OSError:
        raise ObservationFailure("path_unreadable") from None
    return sorted(names, key=lambda value: (value.casefold(), value))


def _scan_regular_entry(
    context: ProjectContext,
    entry: Mapping[str, Any],
    entry_fd: int,
    issues: list[dict[str, Any]],
) -> list[tuple[tuple[int, int], dict[str, Any]]]:
    observations: list[tuple[tuple[int, int], dict[str, Any]]] = []
    entry_parts = tuple(str(entry["relative_path"]).split("/"))
    for name in _directory_names(entry_fd, context):
        if name in EXCLUDED_NAMES or not _safe_component(name):
            continue
        skill_fd, link_witness, resolved, failure = _open_skill_directory(
            context, entry_fd, entry_parts, name
        )
        is_link = link_witness is not None
        visible = entry_parts + (name,)
        if failure:
            _issue(
                issues,
                status="partial",
                code=failure,
                project_id=context.project_id,
                relative_path="/".join(visible),
            )
            continue
        if skill_fd < 0:
            continue
        try:
            try:
                observed = _observe_skill(
                    context,
                    entry,
                    skill_fd,
                    visible,
                    resolved if is_link and resolved is not None else visible,
                    is_link=is_link,
                    issues=issues,
                )
                if link_witness is not None:
                    _verify_link_witness(link_witness)
                observations.append(observed)
            except ObservationFailure as exc:
                _issue(
                    issues,
                    status="partial",
                    code=exc.code,
                    project_id=context.project_id,
                    relative_path="/".join(visible + ("SKILL.md",)),
                )
            except ScanRejected:
                _issue(
                    issues,
                    status="security_reject",
                    code="path_race",
                    project_id=context.project_id,
                    relative_path="/".join(visible),
                )
        finally:
            if link_witness is not None:
                link_witness.close()
            os.close(skill_fd)
    return observations


def _open_plugin_child_directory(
    parent_fd: int,
    name: str,
    issues: list[dict[str, Any]],
    project_id: str,
    relative_path: str,
    *,
    missing_is_ok: bool,
) -> int | None:
    """Open one exact plugin shape component; ordinary files/links are inert."""

    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        if missing_is_ok:
            return None
        _issue(
            issues,
            status="partial",
            code="path_race",
            project_id=project_id,
            relative_path=relative_path,
        )
        return None
    except PermissionError:
        _issue(
            issues,
            status="partial",
            code="permission_denied",
            project_id=project_id,
            relative_path=relative_path,
        )
        return None
    except OSError:
        _issue(
            issues,
            status="partial",
            code="path_unreadable",
            project_id=project_id,
            relative_path=relative_path,
        )
        return None
    if stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        return None
    if not stat.S_ISDIR(before.st_mode):
        _issue(
            issues,
            status="partial",
            code="unsupported_entry_type",
            project_id=project_id,
            relative_path=relative_path,
        )
        return None
    try:
        descriptor = _open_directory_parts(parent_fd, (name,))
    except ScanRejected:
        _issue(
            issues,
            status="partial",
            code="path_race",
            project_id=project_id,
            relative_path=relative_path,
        )
        return None
    except ObservationFailure as exc:
        _issue(
            issues,
            status="partial",
            code=exc.code,
            project_id=project_id,
            relative_path=relative_path,
        )
        return None
    assert descriptor is not None
    return descriptor


def _scan_plugin_entry(
    context: ProjectContext,
    entry: Mapping[str, Any],
    entry_fd: int,
    issues: list[dict[str, Any]],
) -> list[tuple[tuple[int, int], dict[str, Any]]]:
    """Scan only .agents/plugins/<bundle>/skills/<skill>/SKILL.md."""

    observations: list[tuple[tuple[int, int], dict[str, Any]]] = []
    entry_parts = (".agents", "plugins")
    for bundle in _directory_names(entry_fd, context):
        if bundle in EXCLUDED_NAMES or not _safe_component(bundle):
            continue
        bundle_relative = "/".join(entry_parts + (bundle,))
        bundle_fd = _open_plugin_child_directory(
            entry_fd,
            bundle,
            issues,
            context.project_id,
            bundle_relative,
            missing_is_ok=False,
        )
        if bundle_fd is None:
            # Files such as marketplace.json are intentionally not opened.
            continue
        try:
            skills_fd = _open_plugin_child_directory(
                bundle_fd,
                "skills",
                issues,
                context.project_id,
                f"{bundle_relative}/skills",
                missing_is_ok=True,
            )
            if skills_fd is None:
                continue
            try:
                for skill_name in _directory_names(skills_fd, context):
                    if skill_name in EXCLUDED_NAMES or not _safe_component(skill_name):
                        continue
                    visible = entry_parts + (bundle, "skills", skill_name)
                    skill_fd = _open_plugin_child_directory(
                        skills_fd,
                        skill_name,
                        issues,
                        context.project_id,
                        "/".join(visible),
                        missing_is_ok=False,
                    )
                    if skill_fd is None:
                        continue
                    try:
                        try:
                            observations.append(
                                _observe_skill(
                                    context,
                                    entry,
                                    skill_fd,
                                    visible,
                                    visible,
                                    is_link=False,
                                    issues=issues,
                                )
                            )
                        except ObservationFailure as exc:
                            _issue(
                                issues,
                                status="partial",
                                code=exc.code,
                                project_id=context.project_id,
                                relative_path="/".join(visible + ("SKILL.md",)),
                            )
                    finally:
                        os.close(skill_fd)
            finally:
                os.close(skills_fd)
        finally:
            os.close(bundle_fd)
    return observations


def _project_skill_localization(
    source_name: str,
    observations: list[dict[str, Any]],
    localizations: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    metadata = localizations.get(source_name.casefold())
    if metadata is None:
        return {"status": "missing", "coverage_complete": False}
    translated_hash = str(metadata["translated_from_projection_sha256_v1"])
    observed_hashes = {str(row[PROJECTION_VERSION]) for row in observations}
    if metadata["status"] == "stale" or observed_hashes != {translated_hash}:
        return {
            "status": "stale",
            "coverage_complete": False,
            "asset_id": metadata["asset_id"],
            "translated_from_projection_sha256_v1": translated_hash,
        }
    return {
        "status": metadata["status"],
        "coverage_complete": True,
        "asset_id": metadata["asset_id"],
        "zh_name": metadata["zh_name"],
        "summary": metadata["summary"],
        "use_cases": list(metadata["use_cases"]),
        "not_for": list(metadata["not_for"]),
        "examples": list(metadata["examples"]),
        "translated_from_projection_sha256_v1": translated_hash,
    }


def _logical_skills(
    project_id: str,
    rows: list[tuple[tuple[int, int], dict[str, Any]]],
    localizations: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for identity, observation in rows:
        grouped.setdefault(identity, []).append(observation)
    output: list[dict[str, Any]] = []
    for observations in grouped.values():
        observations.sort(key=lambda row: (row["manifest_relative_path"], row["source_kind"]))
        canonical = min(str(row.pop("_canonical_manifest")) for row in observations)
        projection_name = observations[0]["frontmatter_projection"].get("name")
        fallback = PurePosixPath(canonical).parent.name
        display = projection_name if isinstance(projection_name, str) and projection_name else fallback
        localization = _project_skill_localization(display, observations, localizations)
        output.append(
            {
                "logical_skill_id": _slug_hash("project-skill", project_id, canonical),
                "display_name": _safe_output_text(display),
                "localization": localization,
                "observations": observations,
            }
        )
    output.sort(key=lambda row: (str(row["display_name"]).casefold(), row["logical_skill_id"]))
    return output


def _scan_project(
    root_fd: int,
    project: Mapping[str, Any],
    deadline: float,
    observed_at: str,
    issues: list[dict[str, Any]],
    clock: Callable[[], float],
    localizations: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    project_id = str(project["project_id"])
    classification = str(project["classification"])
    base: dict[str, Any] = {
        "project_id": project_id,
        "relative_path": str(project["relative_path"]),
        "classification": classification,
        "display_name": _safe_output_text(str(project["display_name"])),
        "scan_status": "not_scanned",
        "entries": [],
        "logical_skills": [],
    }
    if "parent_container_id" in project:
        base["parent_container_id"] = str(project["parent_container_id"])
    if classification != "project":
        return base
    issue_start = len(issues)
    project_event_start = issues.event_count() if isinstance(issues, IssueLog) else 0
    try:
        project_parts = _safe_relative(str(project["relative_path"]))
        project_fd = _open_directory_parts(
            root_fd,
            project_parts,
            missing_ok=True,
            symlink_code="project_root_symlink",
            ancestor_symlink_code="project_ancestor_symlink",
        )
    except ScanRejected as exc:
        _issue(issues, status="security_reject", code=exc.code, project_id=project_id)
        base["scan_status"] = "security_reject"
        return base
    except ObservationFailure as exc:
        _issue(issues, status="error", code=exc.code, project_id=project_id)
        base["scan_status"] = "error"
        return base
    if project_fd is None:
        _issue(issues, status="error", code="project_root_missing", project_id=project_id)
        base["scan_status"] = "error"
        return base
    try:
        project_stamp = StatStamp.from_stat(os.fstat(project_fd))
    except OSError:
        os.close(project_fd)
        _issue(issues, status="error", code="path_unreadable", project_id=project_id)
        base["scan_status"] = "error"
        return base
    context = ProjectContext(
        project_id,
        str(project["relative_path"]),
        project_fd,
        deadline,
        observed_at,
        clock,
    )
    all_observations: list[tuple[tuple[int, int], dict[str, Any]]] = []
    try:
        for entry in ENTRY_POINTS:
            entry_issue_start = len(issues)
            entry_event_start = issues.event_count() if isinstance(issues, IssueLog) else 0
            observed_rows: list[tuple[tuple[int, int], dict[str, Any]]] = []
            try:
                entry_fd = _open_directory_parts(project_fd, str(entry["relative_path"]).split("/"), missing_ok=True)
                if entry_fd is None:
                    entry_status = "missing"
                else:
                    try:
                        if entry["binding_semantics"] == "plugin_bundled_source":
                            observed_rows = _scan_plugin_entry(context, entry, entry_fd, issues)
                        else:
                            observed_rows = _scan_regular_entry(context, entry, entry_fd, issues)
                    finally:
                        os.close(entry_fd)
                    new_issues = issues[entry_issue_start:]
                    new_status_events = (
                        issues.statuses_since(entry_event_start)
                        if isinstance(issues, IssueLog)
                        else ()
                    )
                    entry_status = (
                        _status_from_issues(
                            new_issues,
                            status_events=new_status_events,
                        )
                        if new_issues or new_status_events
                        else ("observed" if observed_rows else "empty")
                    )
            except ScanRejected as exc:
                _issue(
                    issues,
                    status="security_reject",
                    code=exc.code,
                    project_id=project_id,
                    relative_path=str(entry["relative_path"]),
                )
                entry_status = "security_reject"
            except ObservationFailure as exc:
                status = "partial"
                _issue(
                    issues,
                    status=status,
                    code=exc.code,
                    project_id=project_id,
                    relative_path=str(entry["relative_path"]),
                )
                entry_status = status
            all_observations.extend(observed_rows)
            issue_refs = [row["issue_id"] for row in issues[entry_issue_start:]]
            base["entries"].append(
                {
                    "entry": dict(entry),
                    "status": entry_status,
                    "observed_entry_count": len(observed_rows),
                    "issue_refs": issue_refs,
                }
            )
            if entry_status in {"partial", "error", "security_reject"} and clock() > deadline:
                break
        try:
            _verify_directory_identity(root_fd, project_parts, project_stamp)
        except ObservationFailure:
            _issue(
                issues,
                status="security_reject",
                code="path_race",
                project_id=project_id,
            )
            all_observations.clear()
            base["entries"].clear()
        base["logical_skills"] = _logical_skills(project_id, all_observations, localizations)
        base["scan_status"] = _status_from_issues(
            issues[issue_start:],
            status_events=(
                issues.statuses_since(project_event_start)
                if isinstance(issues, IssueLog)
                else None
            ),
        )
        return base
    finally:
        os.close(project_fd)


def _validate_registry_shape(projects: Mapping[str, Any], associations: Mapping[str, Any]) -> None:
    project_root_keys = {
        "schema_version",
        "registry_id",
        "observation_boundary_ref",
        "confirmed_on",
        "projects",
    }
    association_root_keys = {
        "schema_version",
        "registry_id",
        "observation_boundary_ref",
        "confirmed_on",
        "associations",
    }
    if set(projects) != project_root_keys:
        raise ScanRejected("projects_registry_invalid")
    if set(associations) != association_root_keys:
        raise ScanRejected("associations_registry_invalid")
    if type(projects.get("schema_version")) is not int or projects["schema_version"] != 1:
        raise ScanRejected("projects_registry_invalid")
    if type(associations.get("schema_version")) is not int or associations["schema_version"] != 1:
        raise ScanRejected("associations_registry_invalid")
    if not isinstance(projects.get("registry_id"), str) or not _STABLE_ID.fullmatch(
        projects["registry_id"]
    ):
        raise ScanRejected("projects_registry_invalid")
    if not isinstance(associations.get("registry_id"), str) or not _STABLE_ID.fullmatch(
        associations["registry_id"]
    ):
        raise ScanRejected("associations_registry_invalid")
    if projects.get("observation_boundary_ref") != BOUNDARY_ID:
        raise ScanRejected("projects_registry_boundary_mismatch")
    if associations.get("observation_boundary_ref") != BOUNDARY_ID:
        raise ScanRejected("associations_registry_boundary_mismatch")
    if not _valid_date(projects.get("confirmed_on")):
        raise ScanRejected("projects_registry_invalid")
    if not _valid_date(associations.get("confirmed_on")):
        raise ScanRejected("associations_registry_invalid")
    rows = projects.get("projects")
    if not isinstance(rows, list) or len(rows) > int(LIMITS["max_project_candidates"]):
        raise ScanRejected("projects_registry_invalid")
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ScanRejected("projects_registry_invalid")
        if not set(row).issubset(
            {
                "project_id",
                "relative_path",
                "classification",
                "display_name",
                "purpose",
                "parent_container_id",
            }
        ):
            raise ScanRejected("projects_registry_invalid")
        required = {"project_id", "relative_path", "classification", "display_name"}
        if not required.issubset(row):
            raise ScanRejected("projects_registry_invalid")
        if not isinstance(row["project_id"], str) or not _STABLE_ID.fullmatch(row["project_id"]):
            raise ScanRejected("projects_registry_invalid")
        if not isinstance(row["relative_path"], str):
            raise ScanRejected("projects_registry_invalid")
        parts = _safe_relative(row["relative_path"])
        if any(part in EXCLUDED_NAMES for part in parts):
            raise ScanRejected("projects_registry_invalid")
        if row["classification"] not in {"project", "container", "archive", "excluded", "unclassified"}:
            raise ScanRejected("projects_registry_invalid")
        if not _schema_safe_text(row["display_name"]):
            raise ScanRejected("projects_registry_invalid")
        if "purpose" in row and not _schema_safe_text(row["purpose"]):
            raise ScanRejected("projects_registry_invalid")
        if "parent_container_id" in row and (
            not isinstance(row["parent_container_id"], str)
            or not _STABLE_ID.fullmatch(row["parent_container_id"])
        ):
            raise ScanRejected("projects_registry_invalid")
        if row["project_id"] in seen_ids or row["relative_path"] in seen_paths:
            raise ScanRejected("projects_registry_invalid")
        seen_ids.add(str(row["project_id"]))
        seen_paths.add(str(row["relative_path"]))
    by_id = {row["project_id"]: row for row in rows}
    for row in rows:
        parent_id = row.get("parent_container_id")
        if parent_id is None:
            continue
        parent = by_id.get(parent_id)
        if (
            parent is None
            or parent["classification"] != "container"
            or row["classification"] != "project"
            or not row["relative_path"].startswith(parent["relative_path"] + "/")
        ):
            raise ScanRejected("projects_registry_invalid")
    for ancestor in rows:
        prefix = ancestor["relative_path"] + "/"
        for descendant in rows:
            if ancestor is descendant or not descendant["relative_path"].startswith(prefix):
                continue
            if not (
                ancestor["classification"] == "container"
                and descendant["classification"] == "project"
                and descendant.get("parent_container_id") == ancestor["project_id"]
            ):
                raise ScanRejected("projects_registry_invalid")
    association_rows = associations.get("associations")
    if not isinstance(association_rows, list) or len(association_rows) > 1000:
        raise ScanRejected("associations_registry_invalid")
    association_ids: set[str] = set()
    association_targets: set[tuple[str, str, str]] = set()
    for row in association_rows:
        if not isinstance(row, dict) or row.get("relationship") != "human_association":
            raise ScanRejected("associations_registry_invalid")
        if not set(row).issubset(
            {
                "association_id",
                "project_id",
                "relationship",
                "asset_id",
                "unresolved_name",
                "reason",
                "source",
            }
        ):
            raise ScanRejected("associations_registry_invalid")
        if row.get("project_id") not in seen_ids:
            raise ScanRejected("associations_registry_invalid")
        if not all(
            isinstance(row.get(key), str) and _STABLE_ID.fullmatch(row[key])
            for key in ("association_id", "project_id", "source")
        ):
            raise ScanRejected("associations_registry_invalid")
        if row["association_id"] in association_ids:
            raise ScanRejected("associations_registry_invalid")
        association_ids.add(row["association_id"])
        if ("asset_id" in row) == ("unresolved_name" in row):
            raise ScanRejected("associations_registry_invalid")
        if "asset_id" in row and (
            not isinstance(row["asset_id"], str) or not _STABLE_ID.fullmatch(row["asset_id"])
        ):
            raise ScanRejected("associations_registry_invalid")
        if "reason" not in row or not _schema_safe_text(row["reason"]):
            raise ScanRejected("associations_registry_invalid")
        if "unresolved_name" in row and not _schema_safe_text(row["unresolved_name"]):
            raise ScanRejected("associations_registry_invalid")
        target_kind = "asset" if "asset_id" in row else "name"
        target_value = row.get("asset_id", row.get("unresolved_name"))
        target_key = (row["project_id"], target_kind, target_value)
        if target_key in association_targets:
            raise ScanRejected("associations_registry_invalid")
        association_targets.add(target_key)


def _discover_candidates(
    root_fd: int,
    registered_top_level: set[str],
    observed_at: str,
    issues: list[dict[str, Any]],
    deadline: float,
    clock: Callable[[], float],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    try:
        with os.scandir(root_fd) as iterator:
            names: list[str] = []
            directory_count = 0
            for item in iterator:
                _check_deadline(deadline, clock)
                name = item.name
                if not _safe_component(name):
                    continue
                try:
                    info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                except OSError:
                    _issue(issues, status="partial", code="candidate_entry_changed")
                    continue
                if not stat.S_ISDIR(info.st_mode):
                    continue
                directory_count += 1
                if directory_count > int(LIMITS["max_project_candidates"]):
                    _issue(issues, status="partial", code="candidate_budget_exceeded")
                    break
                names.append(name)
    except OSError:
        _issue(issues, status="error", code="candidate_discovery_failed")
        return candidates
    except ObservationFailure as exc:
        _issue(issues, status="partial", code=exc.code)
        return candidates
    for name in sorted(names, key=lambda value: (value.casefold(), value)):
        if name in registered_top_level:
            continue
        candidates.append(
            {
                "candidate_id": _slug_hash("candidate", name),
                "relative_path": name,
                "status": "unclassified",
                "observed_at": observed_at,
            }
        )
    return candidates


def _load_fixed_json(path: Path) -> dict[str, Any]:
    try:
        descriptor = os.open(path, _regular_flags())
    except OSError:
        raise ScanRejected("registry_unavailable") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ScanRejected("registry_unavailable")
        data = bytearray()
        while len(data) <= 1024 * 1024:
            piece = os.read(descriptor, min(65536, 1024 * 1024 + 1 - len(data)))
            if not piece:
                break
            data.extend(piece)
        if len(data) > 1024 * 1024:
            raise ScanRejected("registry_unavailable")
        value = json.loads(bytes(data).decode("utf-8", "strict"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise ScanRejected("registry_unavailable") from None
    finally:
        os.close(descriptor)
    if not isinstance(value, dict):
        raise ScanRejected("registry_unavailable")
    return value


def _load_project_localizations(registry: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    if set(registry) != {"schema_version", "items"} or registry.get("schema_version") != 1:
        raise ScanRejected("chinese_metadata_invalid")
    raw_items = registry.get("items")
    if not isinstance(raw_items, list) or len(raw_items) > 2000:
        raise ScanRejected("chinese_metadata_invalid")

    def text_value(value: Any, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ScanRejected("chinese_metadata_invalid")
        if _SENSITIVE_TEXT.search(value):
            raise ScanRejected("chinese_metadata_invalid")
        return _safe_output_text(value)

    def text_list(value: Any, *, maximum_items: int, maximum_text: int) -> list[str]:
        if not isinstance(value, list) or not 1 <= len(value) <= maximum_items:
            raise ScanRejected("chinese_metadata_invalid")
        return [text_value(item, maximum_text) for item in value]

    by_source_name: dict[str, dict[str, Any]] = {}
    for raw in raw_items:
        if not isinstance(raw, dict) or "translated_from_projection_sha256_v1" not in raw:
            continue
        required = {
            "asset_id",
            "source_name",
            "zh_name",
            "summary",
            "use_cases",
            "not_for",
            "examples",
            "status",
            "translated_from_projection_sha256_v1",
        }
        if not required.issubset(raw):
            raise ScanRejected("chinese_metadata_invalid")
        asset_id = raw["asset_id"]
        source_name = raw["source_name"]
        status = raw["status"]
        translated_hash = raw["translated_from_projection_sha256_v1"]
        if (
            not isinstance(asset_id, str)
            or not asset_id.startswith("skill:")
            or not _STABLE_ID.fullmatch(asset_id)
            or not isinstance(source_name, str)
            or not source_name.strip()
            or len(source_name) > 200
            or status not in {"ai_draft", "reviewed", "stale"}
            or not isinstance(translated_hash, str)
            or not re.fullmatch(r"[a-f0-9]{64}", translated_hash)
        ):
            raise ScanRejected("chinese_metadata_invalid")
        key = source_name.casefold()
        if key in by_source_name:
            raise ScanRejected("chinese_metadata_ambiguous")
        by_source_name[key] = {
            "asset_id": asset_id,
            "source_name": source_name,
            "zh_name": text_value(raw["zh_name"], 120),
            "summary": text_value(raw["summary"], 1000),
            "use_cases": text_list(raw["use_cases"], maximum_items=64, maximum_text=500),
            "not_for": text_list(raw["not_for"], maximum_items=64, maximum_text=500),
            "examples": text_list(raw["examples"], maximum_items=3, maximum_text=500),
            "status": status,
            "translated_from_projection_sha256_v1": translated_hash,
        }
    return by_source_name


def load_project_skill_runtime_boundary(path: Path = BOUNDARY_PATH) -> dict[str, Any]:
    """Load the one fixed machine boundary with the scanner's no-follow budget."""

    return _load_fixed_json(path)


def _project_human_association(row: Mapping[str, Any]) -> dict[str, Any]:
    projected = {
        "association_id": row["association_id"],
        "project_id": row["project_id"],
        "relationship": "human_association",
        "reason": _safe_output_text(str(row["reason"])),
        "source": row["source"],
    }
    if "asset_id" in row:
        projected["asset_id"] = row["asset_id"]
    else:
        projected["unresolved_name"] = _safe_output_text(str(row["unresolved_name"]))
    return projected


def _object_shape(
    value: Any,
    required: set[str],
    optional: set[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(value, dict) or not required.issubset(value) or not set(value).issubset(
        required | set(optional)
    ):
        raise PayloadValidationError("invalid object shape")
    return value


def _payload_stable_id(value: Any) -> str:
    if not isinstance(value, str) or not _STABLE_ID.fullmatch(value):
        raise PayloadValidationError("invalid stable id")
    return value


def _payload_relative(value: Any) -> str:
    if not isinstance(value, str):
        raise PayloadValidationError("invalid relative path")
    try:
        _safe_relative(value)
    except ScanRejected:
        raise PayloadValidationError("invalid relative path") from None
    return value


def _payload_text(value: Any, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not _schema_safe_text(value):
        raise PayloadValidationError("invalid safe text")


def _validate_entry_semantics(value: Any) -> Mapping[str, Any]:
    row = _object_shape(
        value,
        {"relative_path", "kind", "host_hint", "binding_semantics"},
    )
    if dict(row) not in ENTRY_POINTS:
        raise PayloadValidationError("invalid entry semantics")
    return row


def _validate_openai_declaration(value: Any) -> str:
    row = _object_shape(value, {"status"}, {"allow_implicit_invocation", "reason"})
    status = row["status"]
    if status == "declared":
        if set(row) != {"status", "allow_implicit_invocation"} or type(
            row["allow_implicit_invocation"]
        ) is not bool:
            raise PayloadValidationError("invalid declaration")
    elif status == "not_observed":
        if set(row) != {"status"}:
            raise PayloadValidationError("invalid declaration")
    elif status == "unverified":
        if set(row) != {"status", "reason"} or row["reason"] not in {
            "scan_incomplete",
            "invalid_yaml",
            "over_limit",
            "permission_denied",
            "security_reject",
            "path_race",
        }:
            raise PayloadValidationError("invalid declaration")
    else:
        raise PayloadValidationError("invalid declaration")
    return str(status)


def _validate_evidence(value: Any, binding: str, declaration: str) -> None:
    row = _object_shape(
        value,
        {
            "file_discovery",
            "project_binding",
            "host_availability",
            "invocation_eligibility",
            "actual_use",
        },
    )
    expected = {
        "file_discovery": "observed",
        "project_binding": binding,
        "host_availability": "unverified",
        "invocation_eligibility": {
            "declared": "declaration_only",
            "not_observed": "not_observed",
            "unverified": "unverified",
        }[declaration],
        "actual_use": "not_connected",
    }
    for key, status in expected.items():
        nested = _object_shape(row[key], {"status"})
        if nested["status"] != status:
            raise PayloadValidationError("invalid evidence status")


def _validate_observation(value: Any) -> tuple[str, str]:
    required = {
        "observation_id",
        "entry",
        "manifest_relative_path",
        "source_kind",
        "file_type",
        "link_status",
        "observation_depth",
        "manifest_size_bytes",
        "frontmatter_bytes_read",
        "manifest_modified_at",
        "frontmatter_projection",
        "projection_sha256_v1",
        "openai_declaration",
        "evidence",
        "observed_at",
    }
    row = _object_shape(value, required, {"resolved_relative_path"})
    observation_id = _payload_stable_id(row["observation_id"])
    entry = _validate_entry_semantics(row["entry"])
    manifest_path = _payload_relative(row["manifest_relative_path"])
    if not manifest_path.endswith("/SKILL.md"):
        raise PayloadValidationError("invalid manifest path")
    source = row["source_kind"]
    binding = entry["binding_semantics"]
    expected_source = {
        "project_binding_candidate": {"entity", "same_project_symlink"},
        "source_only": {"generic_source"},
        "plugin_bundled_source": {"plugin_bundled_source"},
        "observed_path_only": {"observed_path_only"},
    }[binding]
    if source not in expected_source:
        raise PayloadValidationError("invalid source kind")
    if row["file_type"] == "regular_file":
        if row["link_status"] != "not_applicable" or "resolved_relative_path" in row:
            raise PayloadValidationError("invalid regular observation")
    elif row["file_type"] == "symlink":
        if (
            source != "same_project_symlink"
            or row["link_status"] != "healthy_same_project"
            or "resolved_relative_path" not in row
        ):
            raise PayloadValidationError("invalid link observation")
        _payload_relative(row["resolved_relative_path"])
    else:
        raise PayloadValidationError("invalid file type")
    if type(row["observation_depth"]) is not int or not 1 <= row["observation_depth"] <= 5:
        raise PayloadValidationError("invalid observation depth")
    for key, maximum in (
        ("manifest_size_bytes", 262144),
        ("frontmatter_bytes_read", 32768),
    ):
        if type(row[key]) is not int or not 0 <= row[key] <= maximum:
            raise PayloadValidationError("invalid byte count")
    if not _valid_datetime(row["manifest_modified_at"]) or not _valid_datetime(row["observed_at"]):
        raise PayloadValidationError("invalid observation time")
    projection = _object_shape(row["frontmatter_projection"], set(FRONTMATTER_FIELDS))
    for key in FRONTMATTER_FIELDS[:-1]:
        _payload_text(projection[key], nullable=True)
    agent_created = projection["agent_created"]
    if agent_created is not None and type(agent_created) is not bool:
        _payload_text(agent_created)
    if not isinstance(row["projection_sha256_v1"], str) or not re.fullmatch(
        r"[a-f0-9]{64}", row["projection_sha256_v1"]
    ):
        raise PayloadValidationError("invalid projection digest")
    declaration = _validate_openai_declaration(row["openai_declaration"])
    _validate_evidence(row["evidence"], _binding_status(entry), declaration)
    return observation_id, str(entry["relative_path"])


def _validate_human_association(value: Any, project_ids: set[str]) -> str:
    row = _object_shape(
        value,
        {"association_id", "project_id", "relationship", "reason", "source"},
        {"asset_id", "unresolved_name"},
    )
    association_id = _payload_stable_id(row["association_id"])
    if _payload_stable_id(row["project_id"]) not in project_ids:
        raise PayloadValidationError("unknown association project")
    if row["relationship"] != "human_association":
        raise PayloadValidationError("invalid association relationship")
    _payload_text(row["reason"])
    _payload_stable_id(row["source"])
    if ("asset_id" in row) == ("unresolved_name" in row):
        raise PayloadValidationError("invalid association target")
    if "asset_id" in row:
        _payload_stable_id(row["asset_id"])
    else:
        _payload_text(row["unresolved_name"])
    return association_id


def _validate_project_localization(value: Any) -> None:
    if not isinstance(value, dict):
        raise PayloadValidationError("invalid project localization")
    status = value.get("status")
    if status == "missing":
        if value != {"status": "missing", "coverage_complete": False}:
            raise PayloadValidationError("invalid missing localization")
        return
    if status == "stale":
        row = _object_shape(
            value,
            {
                "status",
                "coverage_complete",
                "asset_id",
                "translated_from_projection_sha256_v1",
            },
        )
        if row["coverage_complete"] is not False:
            raise PayloadValidationError("invalid stale localization")
    elif status in {"ai_draft", "reviewed"}:
        row = _object_shape(
            value,
            {
                "status",
                "coverage_complete",
                "asset_id",
                "zh_name",
                "summary",
                "use_cases",
                "not_for",
                "examples",
                "translated_from_projection_sha256_v1",
            },
        )
        if row["coverage_complete"] is not True:
            raise PayloadValidationError("invalid complete localization")
        _payload_text(row["zh_name"])
        _payload_text(row["summary"])
        for key, maximum in (("use_cases", 64), ("not_for", 64), ("examples", 3)):
            values = row[key]
            if not isinstance(values, list) or not 1 <= len(values) <= maximum:
                raise PayloadValidationError("invalid localization list")
            for item in values:
                _payload_text(item)
    else:
        raise PayloadValidationError("invalid project localization status")
    _payload_stable_id(row["asset_id"])
    translated_hash = row["translated_from_projection_sha256_v1"]
    if not isinstance(translated_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", translated_hash):
        raise PayloadValidationError("invalid localization projection digest")


def validate_project_skill_payload(payload: Any) -> None:
    """Strict standard-library validation of the complete snapshot contract."""

    row = _object_shape(
        payload,
        {
            "schema_version",
            "observation_boundary_ref",
            "generation_id",
            "generated_at",
            "scan_status",
            "scan_scope",
            "candidates",
            "projects",
            "human_associations",
            "issues",
        },
        {"changes"},
    )
    if type(row["schema_version"]) is not int or row["schema_version"] != 1:
        raise PayloadValidationError("invalid schema version")
    if row["observation_boundary_ref"] != BOUNDARY_ID:
        raise PayloadValidationError("invalid boundary ref")
    if not isinstance(row["generation_id"], str) or not re.fullmatch(
        r"[a-f0-9]{16}", row["generation_id"]
    ):
        raise PayloadValidationError("invalid generation id")
    if not _valid_datetime(row["generated_at"]):
        raise PayloadValidationError("invalid generated time")
    if row["scan_status"] not in {"complete", "partial", "error", "security_reject"}:
        raise PayloadValidationError("invalid scan status")
    scope = _object_shape(row["scan_scope"], {"trigger", "execution", "network", "limits"})
    if (scope["trigger"], scope["execution"], scope["network"]) != (
        "manual",
        "foreground",
        "disabled",
    ) or scope["limits"] != LIMITS:
        raise PayloadValidationError("invalid scan scope")
    candidates = row["candidates"]
    projects = row["projects"]
    issues = row["issues"]
    if not isinstance(candidates, list) or len(candidates) > 256:
        raise PayloadValidationError("invalid candidates")
    if not isinstance(projects, list) or len(projects) > 256:
        raise PayloadValidationError("invalid projects")
    if not isinstance(issues, list) or len(issues) > 1000:
        raise PayloadValidationError("invalid issues")
    candidate_ids: set[str] = set()
    candidate_paths: set[str] = set()
    for candidate in candidates:
        item = _object_shape(candidate, {"candidate_id", "relative_path", "status", "observed_at"})
        identifier = _payload_stable_id(item["candidate_id"])
        relative = _payload_relative(item["relative_path"])
        if item["status"] != "unclassified" or not _valid_datetime(item["observed_at"]):
            raise PayloadValidationError("invalid candidate")
        if identifier in candidate_ids or relative in candidate_paths or "/" in relative:
            raise PayloadValidationError("duplicate or nested candidate")
        candidate_ids.add(identifier)
        candidate_paths.add(relative)
    project_ids: set[str] = set()
    all_logical_ids: set[str] = set()
    observation_ids: set[str] = set()
    entry_issue_refs: set[str] = set()
    for project in projects:
        item = _object_shape(
            project,
            {
                "project_id",
                "relative_path",
                "classification",
                "display_name",
                "scan_status",
                "entries",
                "logical_skills",
            },
            {"parent_container_id"},
        )
        project_id = _payload_stable_id(item["project_id"])
        if project_id in project_ids:
            raise PayloadValidationError("duplicate project")
        project_ids.add(project_id)
        _payload_relative(item["relative_path"])
        _payload_text(item["display_name"])
        classification = item["classification"]
        if classification not in {"project", "container", "archive", "excluded", "unclassified"}:
            raise PayloadValidationError("invalid classification")
        if "parent_container_id" in item:
            _payload_stable_id(item["parent_container_id"])
        if not isinstance(item["entries"], list) or len(item["entries"]) > 7:
            raise PayloadValidationError("invalid entries")
        if not isinstance(item["logical_skills"], list) or len(item["logical_skills"]) > 1000:
            raise PayloadValidationError("invalid logical skills")
        if classification != "project":
            if item["scan_status"] != "not_scanned" or item["entries"] or item["logical_skills"]:
                raise PayloadValidationError("non-project was scanned")
        elif item["scan_status"] not in {"complete", "partial", "error", "security_reject"}:
            raise PayloadValidationError("invalid project status")
        entry_rows: dict[str, Mapping[str, Any]] = {}
        for entry_observation in item["entries"]:
            entry_row = _object_shape(
                entry_observation,
                {"entry", "status", "observed_entry_count", "issue_refs"},
            )
            semantics = _validate_entry_semantics(entry_row["entry"])
            entry_path = str(semantics["relative_path"])
            if entry_path in entry_rows:
                raise PayloadValidationError("duplicate entry")
            entry_rows[entry_path] = entry_row
            status = entry_row["status"]
            if status not in {"observed", "empty", "missing", "partial", "error", "security_reject"}:
                raise PayloadValidationError("invalid entry status")
            count = entry_row["observed_entry_count"]
            if type(count) is not int or not 0 <= count <= 1000:
                raise PayloadValidationError("invalid entry count")
            if status in {"empty", "missing"} and count != 0:
                raise PayloadValidationError("invalid empty entry count")
            if status == "observed" and count < 1:
                raise PayloadValidationError("invalid observed entry count")
            refs = entry_row["issue_refs"]
            if not isinstance(refs, list) or len(refs) > 1000 or len(refs) != len(set(refs)):
                raise PayloadValidationError("invalid issue refs")
            for ref in refs:
                entry_issue_refs.add(_payload_stable_id(ref))
        logical_ids: set[str] = set()
        observed_counts: dict[str, int] = {path: 0 for path in entry_rows}
        for logical in item["logical_skills"]:
            logical_row = _object_shape(
                logical,
                {"logical_skill_id", "display_name", "observations"},
                {"localization"},
            )
            logical_id = _payload_stable_id(logical_row["logical_skill_id"])
            if logical_id in logical_ids:
                raise PayloadValidationError("duplicate logical skill")
            logical_ids.add(logical_id)
            all_logical_ids.add(logical_id)
            _payload_text(logical_row["display_name"])
            if "localization" in logical_row:
                _validate_project_localization(logical_row["localization"])
            observations = logical_row["observations"]
            if not isinstance(observations, list) or not 1 <= len(observations) <= 1000:
                raise PayloadValidationError("invalid observations")
            for observation in observations:
                observation_id, entry_path = _validate_observation(observation)
                if observation_id in observation_ids or entry_path not in observed_counts:
                    raise PayloadValidationError("duplicate observation or unknown entry")
                observation_ids.add(observation_id)
                observed_counts[entry_path] += 1
        for entry_path, count in observed_counts.items():
            if entry_rows[entry_path]["observed_entry_count"] != count:
                raise PayloadValidationError("entry count mismatch")
    human = _object_shape(row["human_associations"], {"registry_id", "confirmed_on", "items"})
    _payload_stable_id(human["registry_id"])
    if not _valid_date(human["confirmed_on"]) or not isinstance(human["items"], list) or len(
        human["items"]
    ) > 1000:
        raise PayloadValidationError("invalid human association section")
    association_ids: set[str] = set()
    for association in human["items"]:
        association_id = _validate_human_association(association, project_ids)
        if association_id in association_ids:
            raise PayloadValidationError("duplicate association")
        association_ids.add(association_id)
    issue_ids: set[str] = set()
    for issue in issues:
        issue_row = _object_shape(
            issue,
            {"issue_id", "status", "code", "message"},
            {"project_id", "relative_path"},
        )
        issue_id = _payload_stable_id(issue_row["issue_id"])
        if issue_id in issue_ids or issue_row["status"] not in {
            "partial",
            "error",
            "security_reject",
        }:
            raise PayloadValidationError("invalid issue")
        issue_ids.add(issue_id)
        _payload_stable_id(issue_row["code"])
        _payload_text(issue_row["message"])
        if "project_id" in issue_row and issue_row["project_id"] not in project_ids:
            raise PayloadValidationError("unknown issue project")
        if "relative_path" in issue_row:
            _payload_relative(issue_row["relative_path"])
    if not entry_issue_refs.issubset(issue_ids):
        raise PayloadValidationError("dangling issue ref")
    if row["scan_status"] == "complete":
        changes_value = row.get("changes")
        if not isinstance(changes_value, dict):
            raise PayloadValidationError("invalid changes")
        status = changes_value.get("status")
        if status == "not_available":
            changes = _object_shape(
                changes_value, {"status", "reason", "fingerprint_basis"}
            )
            if changes != {
                "status": "not_available",
                "reason": "no_prior_complete_snapshot",
                "fingerprint_basis": PROJECTION_VERSION,
            }:
                raise PayloadValidationError("invalid changes")
        elif status == "not_comparable":
            changes = _object_shape(
                changes_value,
                {"status", "reason", "fingerprint_basis", "prior_fingerprint_basis"},
            )
            if (
                changes["reason"] != "fingerprint_version_mismatch"
                or changes["fingerprint_basis"] != PROJECTION_VERSION
                or _payload_stable_id(changes["prior_fingerprint_basis"])
                == PROJECTION_VERSION
            ):
                raise PayloadValidationError("invalid changes")
        elif status == "compared":
            changes = _object_shape(
                changes_value,
                {
                    "status",
                    "compared_to_generation_id",
                    "fingerprint_basis",
                    "added",
                    "changed",
                    "removed",
                },
            )
            if (
                not isinstance(changes["compared_to_generation_id"], str)
                or not re.fullmatch(r"[a-f0-9]{16}", changes["compared_to_generation_id"])
                or changes["compared_to_generation_id"] == row["generation_id"]
                or changes["fingerprint_basis"] != PROJECTION_VERSION
            ):
                raise PayloadValidationError("invalid changes")
            change_sets: dict[str, set[str]] = {}
            for key in ("added", "changed", "removed"):
                values = changes[key]
                if (
                    not isinstance(values, list)
                    or len(values) > 1000
                    or values != sorted(values)
                    or len(values) != len(set(values))
                ):
                    raise PayloadValidationError("invalid changes")
                change_sets[key] = {_payload_stable_id(value) for value in values}
            if (
                change_sets["added"] & change_sets["changed"]
                or change_sets["added"] & change_sets["removed"]
                or change_sets["changed"] & change_sets["removed"]
                or not (change_sets["added"] | change_sets["changed"]).issubset(
                    all_logical_ids
                )
                or change_sets["removed"] & all_logical_ids
            ):
                raise PayloadValidationError("invalid changes")
        else:
            raise PayloadValidationError("invalid changes")
    elif "changes" in row:
        raise PayloadValidationError("changes forbidden for incomplete scan")
    if row["generation_id"] != _generation_id_for_project_skill(row):
        raise PayloadValidationError("generation id does not match payload")


def _generation_id_for_project_skill(payload: Mapping[str, Any]) -> str:
    identity_source = dict(payload)
    identity_source.pop("generation_id", None)
    canonical = json.dumps(
        identity_source,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


def _logical_projection_signatures(
    payload: Mapping[str, Any],
) -> dict[str, tuple[tuple[str, str], ...]]:
    signatures: dict[str, tuple[tuple[str, str], ...]] = {}
    for project in payload["projects"]:
        for logical in project["logical_skills"]:
            signatures[logical["logical_skill_id"]] = tuple(
                sorted(
                    (observation["observation_id"], observation[PROJECTION_VERSION])
                    for observation in logical["observations"]
                )
            )
    return signatures


def finalize_project_skill_changes(
    current: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return a validated complete snapshot with LKG-relative changes and identity."""

    candidate = deepcopy(dict(current))
    validate_project_skill_payload(candidate)
    if candidate["scan_status"] != "complete":
        raise PayloadValidationError("incomplete snapshot cannot be finalized")
    if previous is None:
        candidate["changes"] = {
            "status": "not_available",
            "reason": "no_prior_complete_snapshot",
            "fingerprint_basis": PROJECTION_VERSION,
        }
    else:
        prior = deepcopy(dict(previous))
        validate_project_skill_payload(prior)
        if prior["scan_status"] != "complete":
            raise PayloadValidationError("prior snapshot is incomplete")
        prior_basis = prior["changes"].get("fingerprint_basis")
        if prior_basis != PROJECTION_VERSION:
            candidate["changes"] = {
                "status": "not_comparable",
                "reason": "fingerprint_version_mismatch",
                "fingerprint_basis": PROJECTION_VERSION,
                "prior_fingerprint_basis": prior_basis,
            }
        else:
            current_signatures = _logical_projection_signatures(candidate)
            prior_signatures = _logical_projection_signatures(prior)
            current_ids = set(current_signatures)
            prior_ids = set(prior_signatures)
            candidate["changes"] = {
                "status": "compared",
                "compared_to_generation_id": prior["generation_id"],
                "fingerprint_basis": PROJECTION_VERSION,
                "added": sorted(current_ids - prior_ids),
                "changed": sorted(
                    logical_id
                    for logical_id in current_ids & prior_ids
                    if current_signatures[logical_id] != prior_signatures[logical_id]
                ),
                "removed": sorted(prior_ids - current_ids),
            }
    candidate["generation_id"] = _generation_id_for_project_skill(candidate)
    validate_project_skill_payload(candidate)
    return candidate


def build_project_skill_snapshot(
    root: Path | str | None = None,
    projects_registry: Mapping[str, Any] | None = None,
    associations_registry: Mapping[str, Any] | None = None,
    chinese_metadata_registry: Mapping[str, Any] | None = None,
    *,
    discover_candidates: bool = True,
    generated_at: datetime | str | Callable[[], datetime | str] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Build one in-memory preview; an explicit root is intended for tests only."""

    _require_descriptor_safety()
    observed_at = _iso_time(generated_at)
    projects = dict(projects_registry) if projects_registry is not None else _load_fixed_json(PROJECTS_PATH)
    associations = (
        dict(associations_registry)
        if associations_registry is not None
        else _load_fixed_json(ASSOCIATIONS_PATH)
    )
    chinese_metadata = (
        dict(chinese_metadata_registry)
        if chinese_metadata_registry is not None
        else _load_fixed_json(CHINESE_METADATA_PATH)
    )
    _validate_registry_shape(projects, associations)
    localizations = _load_project_localizations(chinese_metadata)
    observation_root = Path(root) if root is not None else PRODUCTION_ROOT
    deadline = monotonic() + float(LIMITS["scan_timeout_seconds"])
    issues: IssueLog = IssueLog()
    project_rows: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    root_failure_status = "security_reject"
    try:
        root_fd = _open_absolute_directory(observation_root)
    except ScanRejected as exc:
        root_failure_status = exc.status
        _issue(issues, status=exc.status, code=exc.code)
        root_fd = -1
    if root_fd >= 0:
        try:
            root_stamp = StatStamp.from_stat(os.fstat(root_fd))
        except OSError:
            os.close(root_fd)
            _issue(issues, status="error", code="observation_root_unavailable")
            root_fd = -1
    if root_fd >= 0:
        try:
            registered_top = {str(row["relative_path"]).split("/", 1)[0] for row in projects["projects"]}
            if discover_candidates:
                candidates = _discover_candidates(
                    root_fd, registered_top, observed_at, issues, deadline, monotonic
                )
            for project in projects["projects"]:
                if monotonic() > deadline:
                    _issue(issues, status="partial", code="scan_timeout")
                    # Project-shaped security-safe failure avoids claiming legal zero.
                    project_rows.append(
                        {
                            "project_id": project["project_id"],
                            "relative_path": project["relative_path"],
                            "classification": project["classification"],
                            "display_name": _safe_output_text(project["display_name"]),
                            "scan_status": "error" if project["classification"] == "project" else "not_scanned",
                            "entries": [],
                            "logical_skills": [],
                        }
                    )
                    continue
                project_rows.append(
                    _scan_project(
                        root_fd,
                        project,
                        deadline,
                        observed_at,
                        issues,
                        monotonic,
                        localizations,
                    )
                )
            try:
                _verify_absolute_directory_identity(observation_root, root_stamp)
            except ScanRejected:
                _issue(issues, status="security_reject", code="observation_root_changed")
                candidates.clear()
                for project_row in project_rows:
                    if project_row["classification"] == "project":
                        project_row["scan_status"] = "security_reject"
                        project_row["entries"] = []
                        project_row["logical_skills"] = []
        finally:
            os.close(root_fd)
    else:
        for project in projects["projects"]:
            row = {
                "project_id": project["project_id"],
                "relative_path": project["relative_path"],
                "classification": project["classification"],
                "display_name": _safe_output_text(project["display_name"]),
                "scan_status": root_failure_status if project["classification"] == "project" else "not_scanned",
                "entries": [],
                "logical_skills": [],
            }
            if "parent_container_id" in project:
                row["parent_container_id"] = project["parent_container_id"]
            project_rows.append(row)
    scan_status = _status_from_issues(issues)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "observation_boundary_ref": BOUNDARY_ID,
        "generation_id": "0" * 16,
        "generated_at": observed_at,
        "scan_status": scan_status,
        "scan_scope": {
            "trigger": "manual",
            "execution": "foreground",
            "network": "disabled",
            "limits": dict(LIMITS),
        },
        "candidates": candidates,
        "projects": project_rows,
        "human_associations": {
            "registry_id": associations["registry_id"],
            "confirmed_on": associations["confirmed_on"],
            "items": [
                _project_human_association(row) for row in associations["associations"]
            ],
        },
        "issues": sorted(
            issues,
            key=lambda row: (row["status"], row["code"], row.get("project_id", ""), row.get("relative_path", "")),
        ),
    }
    if scan_status == "complete":
        payload["changes"] = {
            "status": "not_available",
            "reason": "no_prior_complete_snapshot",
            "fingerprint_basis": PROJECTION_VERSION,
        }
    payload["generation_id"] = _generation_id_for_project_skill(payload)
    validate_project_skill_payload(payload)
    return payload


# Small compatibility aliases for callers while this isolated adapter settles.
build_project_skill_payload = build_project_skill_snapshot
build_preview = build_project_skill_snapshot
scan_project_skills = build_project_skill_snapshot


def validate_project_skill_runtime_boundary(
    boundary: Mapping[str, Any], *, required_connection: str
) -> None:
    """Fail closed unless the fixed contract authorizes the requested surface."""

    if required_connection not in {"preview", "api"}:
        raise ScanRejected("boundary_contract_mismatch")
    phase = boundary.get("phase")
    try:
        canonical_boundary = json.dumps(
            boundary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ScanRejected("boundary_contract_mismatch") from None
    if hashlib.sha256(canonical_boundary).hexdigest() != BOUNDARY_DIGESTS.get(phase):
        raise ScanRejected("boundary_contract_mismatch")
    if phase == "non_persistent_preview":
        expected_connections = {
            "preview": True,
            "scanner": True,
            "api": False,
            "ui": False,
            "persistence": False,
        }
        expected_writes: list[str] = []
    elif phase in {"persistent_readonly_api", "readonly_workbench"}:
        expected_connections = {
            "preview": True,
            "scanner": True,
            "api": True,
            "ui": phase == "readonly_workbench",
            "persistence": True,
        }
        expected_writes = [
            "generated/project-skills/snapshot.json",
            "generated/project-skills/last-attempt.json",
            "generated/project-skills/项目Skill总览.md",
        ]
    else:
        raise ScanRejected("boundary_contract_mismatch")
    expected_runtime = {
        "manual_refresh_only": True,
        "foreground_only": True,
        "network": False,
        "process_execution": False,
        "model_calls": False,
        "reads_host_config_bodies": False,
        "reads_sessions_or_logs": False,
        "background_watchers": False,
        "host_mutations": False,
        "project_mutations": False,
    }
    expected_data_contract = {
        "registries": [
            "registry/project_skill_projects.json",
            "registry/project_skill_associations.json",
            "registry/chinese_metadata.json",
        ],
        "schemas": [
            "schemas/project_skill_projects.schema.json",
            "schemas/project_skill_associations.schema.json",
            "schemas/project_skill_snapshot.schema.json",
            "schemas/chinese_metadata.schema.json",
        ],
        "fixture_root": "tests/fixtures/project-skills",
        "contract_test": "tests/test_project_skill_contract.py",
        "connections": expected_connections,
    }
    expected_persistence = {
        "current_phase_writes": expected_writes,
        "future_write_root": "generated/project-skills",
        "future_allowed_outputs": [
            "generated/project-skills/snapshot.json",
            "generated/project-skills/last-attempt.json",
            "generated/project-skills/项目Skill总览.md",
        ],
        "atomic_writes": True,
        "file_mode": "0600",
        "write_observed_projects": False,
        "external_sync": False,
    }
    if (
        boundary.get("schema_version") != 1
        or boundary.get("contract_id") != BOUNDARY_ID
        or boundary.get("data_contract") != expected_data_contract
        or boundary.get("source_scope") != {
            "root": PRODUCTION_ROOT_DECLARATION,
            "candidate_discovery": "top_level_directories_only",
            "nested_projects": "explicit_registry_only",
            "new_candidate_status": "unclassified",
            "unclassified_policy": "list_only_no_skill_scan",
            "session_selected_project": {
                "enabled": True,
                "selection": "native_picker_token_only",
                "must_be_within_root": True,
                "classification": "project",
                "candidate_discovery": False,
                "persistence": False,
                "human_associations": False,
            },
        }
        or boundary.get("entry_points") != list(ENTRY_POINTS)
        or boundary.get("metadata_projection") != {
            "manifest_name": "SKILL.md",
            "read_manifest_body": False,
            "frontmatter_allowlist": list(FRONTMATTER_FIELDS),
            "optional_skill_metadata": {
                "relative_path": "agents/openai.yaml",
                "field_allowlist": ["policy.allow_implicit_invocation"],
                "evidence_semantics": "declaration_only",
            },
            "chinese_metadata_overlay": {
                "relative_path": "registry/chinese_metadata.json",
                "field_allowlist": [
                    "asset_id",
                    "source_name",
                    "zh_name",
                    "summary",
                    "use_cases",
                    "not_for",
                    "examples",
                    "status",
                    "translated_from_projection_sha256_v1",
                ],
                "freshness_basis": PROJECTION_VERSION,
                "evidence_semantics": "display_only_no_evidence_upgrade",
                "missing_or_stale_fallback": "frontmatter_original",
            },
            "read_scripts": False,
            "read_references": False,
            "read_credentials": False,
            "store_absolute_project_paths": False,
            "render_free_text_as": "escaped_plain_text",
        }
        or boundary.get("limits") != LIMITS
        or boundary.get("runtime_safety") != expected_runtime
        or boundary.get("persistence") != expected_persistence
        or not expected_connections.get(required_connection, False)
    ):
        raise ScanRejected("boundary_contract_mismatch")


def _validate_preview_boundary(boundary: Mapping[str, Any]) -> None:
    validate_project_skill_runtime_boundary(boundary, required_connection="preview")


def _print_json(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preview fixed-root project Skill observations")
    parser.add_argument("--preview", action="store_true", help="print the safe in-memory JSON preview")
    arguments = parser.parse_args(argv)
    if not arguments.preview:
        parser.error("--preview is required")
    try:
        boundary = load_project_skill_runtime_boundary()
        _validate_preview_boundary(boundary)
        payload = build_project_skill_snapshot()
        validate_project_skill_payload(payload)
    except ScanRejected as exc:
        print(f"project Skill preview rejected: {exc.code}", file=sys.stderr)
        return 2
    except (ObservationFailure, PayloadValidationError, OSError, ValueError):
        print("project Skill preview rejected: validation_failed", file=sys.stderr)
        return 2
    except Exception:
        print("project Skill preview rejected: internal_failure", file=sys.stderr)
        return 2
    _print_json(payload)
    return 0 if payload["scan_status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(_main())
