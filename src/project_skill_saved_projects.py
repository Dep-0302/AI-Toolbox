"""Strict local-only storage for explicitly saved Project Skill monitoring."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Any, Mapping

try:
    from project_skill_scan import validate_project_skill_payload
except ImportError:  # pragma: no cover - package execution only.
    from .project_skill_scan import validate_project_skill_payload


TOOLBOX_ROOT = Path(__file__).resolve().parents[1]
SAVED_PROJECTS_TARGET = Path("generated/project-skills/local-projects.json")
MAX_SAVED_PROJECTS_BYTES = 8 * 1024 * 1024
MAX_SAVED_PROJECTS = 64
_PROJECT_ID = re.compile(r"^saved-project:[a-f0-9]{16}$")
_RFC3339 = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$"
)


class SavedProjectError(RuntimeError):
    """Saved project state could not be handled without weakening safety."""


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SavedProjectError("duplicate saved project state key")
        value[key] = item
    return value


def _directory_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise SavedProjectError("safe descriptor operations are unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _same_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, stat.S_IFMT(left.st_mode)) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


def _open_settings_directory(toolbox_root: Path, *, create: bool) -> int:
    absolute = toolbox_root.expanduser().absolute()
    descriptor = os.open(os.sep, _directory_flags())
    try:
        for component in absolute.parts[1:]:
            if not component or component in {".", ".."} or "/" in component or "\\" in component:
                raise SavedProjectError("unsafe toolbox root component")
            before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise SavedProjectError("toolbox root ancestry is not a real directory")
            child = os.open(component, _directory_flags(), dir_fd=descriptor)
            after = os.fstat(child)
            if not _same_object(before, after):
                os.close(child)
                raise SavedProjectError("toolbox root ancestry changed")
            os.close(descriptor)
            descriptor = child
        for component in SAVED_PROJECTS_TARGET.parent.parts:
            try:
                before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
                before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise SavedProjectError("saved project directory is not a real directory")
            child = os.open(component, _directory_flags(), dir_fd=descriptor)
            after = os.fstat(child)
            if not _same_object(before, after):
                os.close(child)
                raise SavedProjectError("saved project directory changed")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _validate_timestamp(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 64 or not _RFC3339.fullmatch(value):
        raise SavedProjectError("invalid saved project timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SavedProjectError("invalid saved project timestamp") from exc
    return value


def validate_saved_projects(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "projects"}:
        raise SavedProjectError("invalid saved project state shape")
    if payload["schema_version"] != 1 or not isinstance(payload["projects"], list):
        raise SavedProjectError("invalid saved project state version")
    if len(payload["projects"]) > MAX_SAVED_PROJECTS:
        raise SavedProjectError("too many saved projects")
    rows: list[dict[str, Any]] = []
    ids: set[str] = set()
    paths: set[str] = set()
    for value in payload["projects"]:
        if not isinstance(value, dict) or set(value) != {
            "project_id", "path", "device", "inode", "saved_at", "snapshot"
        }:
            raise SavedProjectError("invalid saved project row shape")
        project_id = value["project_id"]
        path = value["path"]
        if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
            raise SavedProjectError("invalid saved project id")
        if (
            not isinstance(path, str)
            or not path
            or len(path) > 4096
            or "\x00" in path
            or "\\" in path
            or not Path(path).is_absolute()
            or any(part in {".", ".."} for part in Path(path).parts)
            or str(Path(os.path.normpath(path))) != path
        ):
            raise SavedProjectError("invalid saved project path")
        if project_id in ids or path in paths:
            raise SavedProjectError("duplicate saved project")
        ids.add(project_id)
        paths.add(path)
        for field in ("device", "inode"):
            if type(value[field]) is not int or value[field] < 0:
                raise SavedProjectError("invalid saved project identity")
        _validate_timestamp(value["saved_at"])
        snapshot = value["snapshot"]
        validate_project_skill_payload(snapshot)
        if (
            snapshot.get("scan_status") != "complete"
            or snapshot.get("candidates") != []
            or len(snapshot.get("projects", [])) != 1
            or snapshot["projects"][0].get("project_id") != project_id
            or snapshot.get("human_associations", {}).get("items") != []
        ):
            raise SavedProjectError("invalid saved project snapshot")
        rows.append(dict(value))
    rows.sort(key=lambda row: row["project_id"])
    return {"schema_version": 1, "projects": rows}


def load_saved_projects(toolbox_root: Path | str = TOOLBOX_ROOT) -> dict[str, Any]:
    try:
        directory_fd = _open_settings_directory(Path(toolbox_root), create=False)
    except FileNotFoundError:
        return {"schema_version": 1, "projects": []}
    try:
        try:
            before = os.stat(SAVED_PROJECTS_TARGET.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return {"schema_version": 1, "projects": []}
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > MAX_SAVED_PROJECTS_BYTES
        ):
            raise SavedProjectError("unsafe saved project state")
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        file_fd = os.open(SAVED_PROJECTS_TARGET.name, flags, dir_fd=directory_fd)
        try:
            after = os.fstat(file_fd)
            if not _same_object(before, after) or after.st_size > MAX_SAVED_PROJECTS_BYTES:
                raise SavedProjectError("saved project state changed")
            remaining = after.st_size
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(file_fd, min(remaining, 65536))
                if not chunk:
                    raise SavedProjectError("saved project state was truncated")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(file_fd, 1):
                raise SavedProjectError("saved project state grew during read")
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)
    try:
        payload = json.loads(
            b"".join(chunks).decode("utf-8"),
            object_pairs_hook=_strict_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SavedProjectError("invalid saved project state") from exc
    return validate_saved_projects(payload)


def write_saved_projects(payload: Mapping[str, Any], toolbox_root: Path | str = TOOLBOX_ROOT) -> None:
    row = validate_saved_projects(dict(payload))
    data = (json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(data) > MAX_SAVED_PROJECTS_BYTES:
        raise SavedProjectError("saved project state exceeds write budget")
    directory_fd = _open_settings_directory(Path(toolbox_root), create=True)
    temporary_name: str | None = None
    try:
        try:
            existing = os.stat(SAVED_PROJECTS_TARGET.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)):
            raise SavedProjectError("unsafe saved project target")
        for _ in range(32):
            temporary_name = f".{SAVED_PROJECTS_TARGET.name}.{secrets.token_hex(8)}.tmp"
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
            raise SavedProjectError("could not allocate saved project state")
        try:
            os.fchmod(file_fd, 0o600)
            view = memoryview(data)
            while view:
                written = os.write(file_fd, view)
                if written <= 0:
                    raise SavedProjectError("short saved project state write")
                view = view[written:]
            os.fsync(file_fd)
            temporary_stamp = os.fstat(file_fd)
        finally:
            os.close(file_fd)
        try:
            current = os.stat(SAVED_PROJECTS_TARGET.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None and (stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode)):
            raise SavedProjectError("saved project target changed")
        if (existing is None) != (current is None) or (
            existing is not None and current is not None and not _same_object(existing, current)
        ):
            raise SavedProjectError("saved project target raced")
        os.replace(
            temporary_name,
            SAVED_PROJECTS_TARGET.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = None
        os.fsync(directory_fd)
        promoted = os.stat(SAVED_PROJECTS_TARGET.name, dir_fd=directory_fd, follow_symlinks=False)
        if not _same_object(temporary_stamp, promoted) or stat.S_IMODE(promoted.st_mode) != 0o600:
            raise SavedProjectError("saved project state changed after replacement")
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def upsert_saved_project(
    *,
    project_id: str,
    path: Path,
    device: int,
    inode: int,
    saved_at: str,
    snapshot: Mapping[str, Any],
    toolbox_root: Path | str = TOOLBOX_ROOT,
) -> dict[str, Any]:
    state = load_saved_projects(toolbox_root)
    row = {
        "project_id": project_id,
        "path": str(path.absolute()),
        "device": device,
        "inode": inode,
        "saved_at": saved_at,
        "snapshot": dict(snapshot),
    }
    projects = [item for item in state["projects"] if item["project_id"] != project_id and item["path"] != row["path"]]
    projects.append(row)
    next_state = validate_saved_projects({"schema_version": 1, "projects": projects})
    write_saved_projects(next_state, toolbox_root)
    return next(item for item in next_state["projects"] if item["project_id"] == project_id)


def remove_saved_project(project_id: str, toolbox_root: Path | str = TOOLBOX_ROOT) -> bool:
    if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
        raise SavedProjectError("invalid saved project id")
    state = load_saved_projects(toolbox_root)
    projects = [item for item in state["projects"] if item["project_id"] != project_id]
    if len(projects) == len(state["projects"]):
        return False
    write_saved_projects({"schema_version": 1, "projects": projects}, toolbox_root)
    return True


def saved_projects_api_view(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    state = validate_saved_projects(dict(payload))
    return [
        {
            "project_id": row["project_id"],
            "saved_at": row["saved_at"],
            "snapshot": row["snapshot"],
        }
        for row in state["projects"]
    ]


__all__ = [
    "MAX_SAVED_PROJECTS",
    "MAX_SAVED_PROJECTS_BYTES",
    "SAVED_PROJECTS_TARGET",
    "SavedProjectError",
    "load_saved_projects",
    "remove_saved_project",
    "saved_projects_api_view",
    "upsert_saved_project",
    "validate_saved_projects",
    "write_saved_projects",
]
