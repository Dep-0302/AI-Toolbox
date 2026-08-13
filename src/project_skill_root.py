"""Local-only storage for the active Project Skill observation root.

The selected absolute path is deliberately kept out of Git-controlled
registries and generated snapshots.  This module may read or write only the
one ignored ``local-root.json`` file beneath ``generated/project-skills``.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any, Mapping


TOOLBOX_ROOT = Path(__file__).resolve().parents[1]
ROOT_CONFIG_TARGET = Path("generated/project-skills/local-root.json")
MAX_ROOT_CONFIG_BYTES = 8192
_ROOT_ID = re.compile(r"^(?:legacy-documents-root-v1|root-[a-f0-9]{16})$")
_RFC3339 = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$"
)


class ProjectSkillRootError(RuntimeError):
    """The local root setting could not be handled without weakening safety."""


def _directory_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ProjectSkillRootError("safe descriptor operations are unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _file_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise ProjectSkillRootError("safe descriptor operations are unavailable")
    return os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _same_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, stat.S_IFMT(left.st_mode)) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


def _open_absolute_root(root: Path) -> int:
    absolute = root.expanduser().absolute()
    if not absolute.is_absolute() or not absolute.parts or absolute.parts[0] != os.sep:
        raise ProjectSkillRootError("toolbox root must be absolute")
    descriptor = os.open(os.sep, _directory_flags())
    try:
        for component in absolute.parts[1:]:
            if not component or component in {".", ".."} or "/" in component or "\\" in component:
                raise ProjectSkillRootError("unsafe toolbox root component")
            before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise ProjectSkillRootError("toolbox root ancestry is not a real directory")
            child = os.open(component, _directory_flags(), dir_fd=descriptor)
            after = os.fstat(child)
            if not _same_object(before, after):
                os.close(child)
                raise ProjectSkillRootError("toolbox root ancestry changed")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_settings_directory(toolbox_root: Path, *, create: bool) -> int:
    root_fd = _open_absolute_root(toolbox_root)
    descriptor = root_fd
    try:
        for component in ROOT_CONFIG_TARGET.parent.parts:
            try:
                before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
                before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise ProjectSkillRootError("root setting directory is not a real directory")
            child = os.open(component, _directory_flags(), dir_fd=descriptor)
            after = os.fstat(child)
            if not _same_object(before, after):
                os.close(child)
                raise ProjectSkillRootError("root setting directory changed")
            if descriptor != root_fd:
                os.close(descriptor)
            descriptor = child
        os.close(root_fd)
        return descriptor
    except Exception:
        if descriptor != root_fd:
            os.close(descriptor)
        os.close(root_fd)
        raise


def validate_project_skill_root_config(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "root_id",
        "path",
        "configured_at",
        "device",
        "inode",
    }:
        raise ProjectSkillRootError("invalid project Skill root setting shape")
    if payload["schema_version"] != 1:
        raise ProjectSkillRootError("invalid project Skill root setting version")
    if not isinstance(payload["root_id"], str) or not _ROOT_ID.fullmatch(payload["root_id"]):
        raise ProjectSkillRootError("invalid project Skill root id")
    path = payload["path"]
    if (
        not isinstance(path, str)
        or not path
        or len(path) > 4096
        or "\x00" in path
        or not Path(path).is_absolute()
        or any(part in {".", ".."} for part in Path(path).parts)
    ):
        raise ProjectSkillRootError("invalid project Skill root path")
    configured_at = payload["configured_at"]
    if (
        not isinstance(configured_at, str)
        or len(configured_at) > 64
        or not _RFC3339.fullmatch(configured_at)
    ):
        raise ProjectSkillRootError("invalid project Skill root timestamp")
    try:
        datetime.fromisoformat(configured_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectSkillRootError("invalid project Skill root timestamp") from exc
    for field in ("device", "inode"):
        if type(payload[field]) is not int or payload[field] < 0:
            raise ProjectSkillRootError("invalid project Skill root identity")
    return dict(payload)


def build_project_skill_root_config(
    path: Path | str,
    *,
    device: int,
    inode: int,
    configured_at: str,
    root_id: str | None = None,
) -> dict[str, Any]:
    row = {
        "schema_version": 1,
        "root_id": root_id or f"root-{secrets.token_hex(8)}",
        "path": str(Path(path).absolute()),
        "configured_at": configured_at,
        "device": device,
        "inode": inode,
    }
    return validate_project_skill_root_config(row)


def load_project_skill_root_config(
    toolbox_root: Path | str = TOOLBOX_ROOT,
) -> dict[str, Any] | None:
    root = Path(toolbox_root)
    try:
        directory_fd = _open_settings_directory(root, create=False)
    except FileNotFoundError:
        return None
    try:
        try:
            before = os.stat(ROOT_CONFIG_TARGET.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > MAX_ROOT_CONFIG_BYTES
        ):
            raise ProjectSkillRootError("unsafe project Skill root setting")
        file_fd = os.open(ROOT_CONFIG_TARGET.name, _file_flags(), dir_fd=directory_fd)
        try:
            after = os.fstat(file_fd)
            if not _same_object(before, after) or after.st_size > MAX_ROOT_CONFIG_BYTES:
                raise ProjectSkillRootError("project Skill root setting changed")
            remaining = after.st_size
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(file_fd, min(remaining, 4096))
                if not chunk:
                    raise ProjectSkillRootError("project Skill root setting was truncated")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(file_fd, 1):
                raise ProjectSkillRootError("project Skill root setting grew during read")
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProjectSkillRootError("invalid project Skill root setting") from exc
    return validate_project_skill_root_config(payload)


def write_project_skill_root_config(
    payload: Mapping[str, Any],
    toolbox_root: Path | str = TOOLBOX_ROOT,
) -> None:
    row = validate_project_skill_root_config(dict(payload))
    data = (
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(data) > MAX_ROOT_CONFIG_BYTES:
        raise ProjectSkillRootError("project Skill root setting exceeds the write budget")
    root = Path(toolbox_root)
    directory_fd = _open_settings_directory(root, create=True)
    temporary_name: str | None = None
    try:
        try:
            existing = os.stat(ROOT_CONFIG_TARGET.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
        ):
            raise ProjectSkillRootError("unsafe project Skill root setting target")
        for _ in range(32):
            temporary_name = f".{ROOT_CONFIG_TARGET.name}.{secrets.token_hex(8)}.tmp"
            try:
                file_fd = os.open(
                    temporary_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=directory_fd,
                )
                break
            except FileExistsError:
                temporary_name = None
        else:
            raise ProjectSkillRootError("could not allocate project Skill root setting")
        try:
            os.fchmod(file_fd, 0o600)
            view = memoryview(data)
            while view:
                written = os.write(file_fd, view)
                if written <= 0:
                    raise ProjectSkillRootError("short project Skill root setting write")
                view = view[written:]
            os.fsync(file_fd)
            temporary_stamp = os.fstat(file_fd)
        finally:
            os.close(file_fd)
        try:
            current = os.stat(ROOT_CONFIG_TARGET.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None and (
            stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode)
        ):
            raise ProjectSkillRootError("project Skill root setting target changed")
        if (existing is None) != (current is None) or (
            existing is not None and current is not None and not _same_object(existing, current)
        ):
            raise ProjectSkillRootError("project Skill root setting target raced")
        os.replace(
            temporary_name,
            ROOT_CONFIG_TARGET.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = None
        os.fsync(directory_fd)
        promoted = os.stat(ROOT_CONFIG_TARGET.name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not _same_object(temporary_stamp, promoted)
            or not stat.S_ISREG(promoted.st_mode)
            or stat.S_IMODE(promoted.st_mode) != 0o600
        ):
            raise ProjectSkillRootError("project Skill root setting changed after replacement")
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


__all__ = [
    "MAX_ROOT_CONFIG_BYTES",
    "ProjectSkillRootError",
    "ROOT_CONFIG_TARGET",
    "build_project_skill_root_config",
    "load_project_skill_root_config",
    "validate_project_skill_root_config",
    "write_project_skill_root_config",
]
