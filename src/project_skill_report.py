"""Project Skill Markdown derivation and narrowly-scoped persistence.

This module never reads an observed project.  It accepts an already-built
project Skill snapshot, validates it with the scanner contract, and may write
only the three files frozen by the project observation boundary.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

try:  # Package import in tests; flat import when app.py adds src/ to sys.path.
    from .project_skill_scan import (
        BOUNDARY_ID,
        PayloadValidationError,
        finalize_project_skill_changes,
        validate_project_skill_payload,
    )
except ImportError:  # pragma: no cover - exercised by the server integration.
    from project_skill_scan import (  # type: ignore[no-redef]
        BOUNDARY_ID,
        PayloadValidationError,
        finalize_project_skill_changes,
        validate_project_skill_payload,
    )


OUTPUT_ROOT = Path("generated/project-skills")
SNAPSHOT_TARGET = OUTPUT_ROOT / "snapshot.json"
LAST_ATTEMPT_TARGET = OUTPUT_ROOT / "last-attempt.json"
MARKDOWN_TARGET = OUTPUT_ROOT / "项目Skill总览.md"
ALLOWED_TARGETS = frozenset((SNAPSHOT_TARGET, LAST_ATTEMPT_TARGET, MARKDOWN_TARGET))
MAX_PERSISTED_BYTES = 16 * 1024 * 1024
_NARROW_RFC3339 = re.compile(
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])"
    r"T(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$"
)


class ProjectSkillPersistenceError(RuntimeError):
    """A project Skill output could not be handled without weakening safety."""


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def validate_persistable_snapshot(snapshot: Any) -> Mapping[str, Any]:
    """Apply the scanner's strict contract and verify its content-derived ID."""

    validate_project_skill_payload(snapshot)
    if not isinstance(snapshot, Mapping):  # Kept explicit for type narrowing.
        raise PayloadValidationError("invalid project Skill snapshot")
    return snapshot


def _markdown_text(value: Any) -> str:
    """Keep validated data inert when embedded in Markdown."""

    text = " ".join(str(value).split())
    for source, replacement in (
        ("\\", "\\\\"),
        # Backslash does not escape a backtick inside a CommonMark code span.
        # An entity keeps attacker-controlled text from closing the span.
        ("`", "&#96;"),
        ("*", "\\*"),
        ("_", "\\_"),
        ("[", "\\["),
        ("]", "\\]"),
        ("<", "&lt;"),
        (">", "&gt;"),
        ("|", "\\|"),
    ):
        text = text.replace(source, replacement)
    return text


def render_project_skill_markdown(snapshot: Any) -> str:
    """Render a complete validated snapshot without reading project content."""

    row = validate_persistable_snapshot(snapshot)
    if row["scan_status"] != "complete":
        raise ProjectSkillPersistenceError("only a complete snapshot can render Markdown")

    logical_count = sum(len(project["logical_skills"]) for project in row["projects"])
    observation_count = sum(
        len(skill["observations"])
        for project in row["projects"]
        for skill in project["logical_skills"]
    )
    lines = [
        "# 项目 Skill 总览",
        "",
        "> 本文由已校验快照与人工关联派生，仅用于只读浏览；文件发现不代表宿主可用、可调用或实际使用。",
        "",
        "## 快照",
        "",
        f"- generation ID：`{row['generation_id']}`",
        f"- 生成时间：`{_markdown_text(row['generated_at'])}`",
        f"- 扫描状态：`{row['scan_status']}`",
        f"- 机器观察：{logical_count} 个逻辑 Skill，{observation_count} 条文件观察",
        f"- 人工关联：{len(row['human_associations']['items'])} 条",
        "",
        "## 机器观察",
        "",
    ]

    for project in row["projects"]:
        lines.extend(
            [
                f"### {_markdown_text(project['display_name'])}",
                "",
                f"- 项目 ID：`{project['project_id']}`",
                f"- 相对路径：`{_markdown_text(project['relative_path'])}`",
                f"- 分类：`{project['classification']}`",
                f"- 扫描状态：`{project['scan_status']}`",
            ]
        )
        if not project["logical_skills"]:
            lines.extend(["- 逻辑 Skill：0", ""])
            continue
        lines.append("")
        for skill in project["logical_skills"]:
            lines.extend(
                [
                    f"#### {_markdown_text(skill['display_name'])}",
                    "",
                    f"- 逻辑 Skill ID：`{skill['logical_skill_id']}`",
                ]
            )
            for observation in skill["observations"]:
                evidence = observation["evidence"]
                lines.append(
                    "- 文件 `{path}`；来源 `{source}`；项目绑定 `{binding}`；"
                    "宿主可用 `{host}`；调用资格 `{invocation}`；实际使用 `{actual}`".format(
                        path=_markdown_text(observation["manifest_relative_path"]),
                        source=observation["source_kind"],
                        binding=evidence["project_binding"]["status"],
                        host=evidence["host_availability"]["status"],
                        invocation=evidence["invocation_eligibility"]["status"],
                        actual=evidence["actual_use"]["status"],
                    )
                )
            lines.append("")

    associations = row["human_associations"]
    lines.extend(
        [
            "## 人工关联（不升级机器证据）",
            "",
            f"- Registry：`{associations['registry_id']}`",
            f"- 人工确认日期：`{associations['confirmed_on']}`",
        ]
    )
    if not associations["items"]:
        lines.append("- 关联：0")
    else:
        lines.append("")
        for association in associations["items"]:
            target = association.get("asset_id", association.get("unresolved_name"))
            lines.append(
                "- 项目 `{project}` ↔ `{target}`；关系 `human_association`；说明：{reason}".format(
                    project=association["project_id"],
                    target=_markdown_text(target),
                    reason=_markdown_text(association["reason"]),
                )
            )
    changes = row["changes"]
    lines.extend(["", "## 相对上一份完整快照的变化", ""])
    if changes["status"] == "not_available":
        lines.append("- 暂不可用：没有上一份完整快照。")
    elif changes["status"] == "not_comparable":
        lines.append("- 不可比较：投影指纹版本不一致。")
    else:
        lines.extend(
            [
                f"- 比较基准：`{changes['compared_to_generation_id']}`",
                f"- 新增（{len(changes['added'])}）："
                + (", ".join(f"`{item}`" for item in changes["added"]) or "无"),
                f"- 变化（{len(changes['changed'])}）："
                + (", ".join(f"`{item}`" for item in changes["changed"]) or "无"),
                f"- 移除（{len(changes['removed'])}）："
                + (", ".join(f"`{item}`" for item in changes["removed"]) or "无"),
            ]
        )
    lines.extend(["", "## 问题", ""])
    if not row["issues"]:
        lines.append("- 无")
    else:
        for issue in row["issues"]:
            project = f"；项目 `{issue['project_id']}`" if "project_id" in issue else ""
            relative = (
                f"；相对路径 `{_markdown_text(issue['relative_path'])}`"
                if "relative_path" in issue
                else ""
            )
            lines.append(f"- `{issue['status']}` / `{issue['code']}`{project}{relative}")
    lines.extend(["", "---", "", "此文件不是机器真源，不能反向修改项目或 Registry。", ""])
    return "\n".join(lines)


def _directory_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ProjectSkillPersistenceError("safe descriptor operations are unavailable")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _file_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise ProjectSkillPersistenceError("safe descriptor operations are unavailable")
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
        raise ProjectSkillPersistenceError("output root must be absolute")
    descriptor = os.open(os.sep, _directory_flags())
    try:
        for component in absolute.parts[1:]:
            if not component or component in {".", ".."} or "/" in component or "\\" in component:
                raise ProjectSkillPersistenceError("unsafe output root component")
            before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise ProjectSkillPersistenceError("output root ancestry is not a real directory")
            child = os.open(component, _directory_flags(), dir_fd=descriptor)
            after = os.fstat(child)
            if not _same_object(before, after):
                os.close(child)
                raise ProjectSkillPersistenceError("output root ancestry changed")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_output_directory(toolbox_root: Path, *, create: bool) -> int:
    root_fd = _open_absolute_root(toolbox_root)
    descriptor = root_fd
    try:
        for component in OUTPUT_ROOT.parts:
            try:
                before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, mode=0o700, dir_fd=descriptor)
                before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise ProjectSkillPersistenceError("project Skill output path is not a real directory")
            child = os.open(component, _directory_flags(), dir_fd=descriptor)
            after = os.fstat(child)
            if not _same_object(before, after):
                os.close(child)
                raise ProjectSkillPersistenceError("project Skill output path changed")
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


def _normalize_target(target: Path | str) -> Path:
    candidate = Path(target)
    if candidate not in ALLOWED_TARGETS:
        raise ProjectSkillPersistenceError("target is outside the project Skill output allowlist")
    return candidate


def _verify_output_directory_identity(toolbox_root: Path, expected_fd: int) -> None:
    """Prove the descriptor is still the directory named by the approved path."""

    verification_fd = _open_output_directory(toolbox_root, create=False)
    try:
        if not _same_object(os.fstat(expected_fd), os.fstat(verification_fd)):
            raise ProjectSkillPersistenceError("project Skill output directory changed")
    finally:
        os.close(verification_fd)


def _read_allowed_bytes(toolbox_root: Path, target: Path | str) -> bytes | None:
    candidate = _normalize_target(target)
    try:
        directory_fd = _open_output_directory(toolbox_root, create=False)
    except FileNotFoundError:
        return None
    try:
        try:
            before = os.stat(candidate.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
        ):
            raise ProjectSkillPersistenceError("project Skill output is not a regular file")
        if before.st_size > MAX_PERSISTED_BYTES:
            raise ProjectSkillPersistenceError("project Skill output exceeds the read budget")
        file_fd = os.open(candidate.name, _file_flags(), dir_fd=directory_fd)
        try:
            after = os.fstat(file_fd)
            if not _same_object(before, after) or after.st_size > MAX_PERSISTED_BYTES:
                raise ProjectSkillPersistenceError("project Skill output changed")
            remaining = after.st_size
            chunks: list[bytes] = []
            while remaining:
                chunk = os.read(file_fd, min(remaining, 65536))
                if not chunk:
                    raise ProjectSkillPersistenceError("project Skill output was truncated")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(file_fd, 1):
                raise ProjectSkillPersistenceError("project Skill output grew during read")
            return b"".join(chunks)
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


def write_project_skill_bytes_atomically(
    toolbox_root: Path | str,
    target: Path | str,
    data: bytes,
) -> None:
    """Write one exact approved target using a same-directory 0600 temp file."""

    candidate = _normalize_target(target)
    if not isinstance(data, bytes) or len(data) > MAX_PERSISTED_BYTES:
        raise ProjectSkillPersistenceError("invalid project Skill output bytes")
    root = Path(toolbox_root)
    directory_fd = _open_output_directory(root, create=True)
    temporary_name: str | None = None
    try:
        try:
            existing = os.stat(candidate.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and (
            stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
        ):
            raise ProjectSkillPersistenceError("project Skill target is not a regular file")
        for _ in range(32):
            temporary_name = f".{candidate.name}.{secrets.token_hex(8)}.tmp"
            try:
                temp_fd = os.open(
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
            raise ProjectSkillPersistenceError("could not allocate an atomic output")
        temp_stamp: os.stat_result
        try:
            os.fchmod(temp_fd, 0o600)
            view = memoryview(data)
            while view:
                written = os.write(temp_fd, view)
                if written <= 0:
                    raise ProjectSkillPersistenceError("short project Skill output write")
                view = view[written:]
            os.fsync(temp_fd)
            temp_stamp = os.fstat(temp_fd)
        finally:
            os.close(temp_fd)

        # Recheck the target immediately before replacement.  os.replace is
        # descriptor-anchored and replaces, rather than follows, a racing link.
        try:
            current = os.stat(candidate.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None and (stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode)):
            raise ProjectSkillPersistenceError("project Skill target changed to an unsafe type")
        if (existing is None) != (current is None) or (
            existing is not None and current is not None and not _same_object(existing, current)
        ):
            raise ProjectSkillPersistenceError("project Skill target changed before replacement")
        _verify_output_directory_identity(root, directory_fd)
        os.replace(
            temporary_name,
            candidate.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = None
        os.fsync(directory_fd)
        promoted = os.stat(candidate.name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not _same_object(temp_stamp, promoted)
            or not stat.S_ISREG(promoted.st_mode)
            or stat.S_IMODE(promoted.st_mode) != 0o600
        ):
            raise ProjectSkillPersistenceError("project Skill target changed after replacement")
        _verify_output_directory_identity(root, directory_fd)
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)


def _remove_allowed_file(toolbox_root: Path, target: Path) -> None:
    try:
        directory_fd = _open_output_directory(toolbox_root, create=False)
    except FileNotFoundError:
        return
    try:
        try:
            os.unlink(target.name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except FileNotFoundError:
            return
    finally:
        os.close(directory_fd)


def _restore_allowed_file(toolbox_root: Path, target: Path, previous: bytes | None) -> None:
    if previous is None:
        _remove_allowed_file(toolbox_root, target)
    else:
        write_project_skill_bytes_atomically(toolbox_root, target, previous)


def _attempt_receipt(
    snapshot: Mapping[str, Any],
    *,
    promoted: bool,
    complete_snapshot_generation_id: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "observation_boundary_ref": BOUNDARY_ID,
        "attempt_generation_id": snapshot["generation_id"],
        "attempted_at": snapshot["generated_at"],
        "scan_status": snapshot["scan_status"],
        "promoted": promoted,
        "complete_snapshot_generation_id": complete_snapshot_generation_id,
        "matches_complete_snapshot": complete_snapshot_generation_id == snapshot["generation_id"],
        "issues": [
            {
                key: issue[key]
                for key in ("status", "code", "project_id", "relative_path")
                if key in issue
            }
            for issue in snapshot["issues"]
        ],
    }


def validate_last_attempt(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "observation_boundary_ref",
        "attempt_generation_id",
        "attempted_at",
        "scan_status",
        "promoted",
        "complete_snapshot_generation_id",
        "matches_complete_snapshot",
        "issues",
    }:
        raise ProjectSkillPersistenceError("invalid project Skill attempt receipt shape")
    if payload["schema_version"] != 1 or payload["observation_boundary_ref"] != BOUNDARY_ID:
        raise ProjectSkillPersistenceError("invalid project Skill attempt receipt contract")
    attempt_id = payload["attempt_generation_id"]
    complete_id = payload["complete_snapshot_generation_id"]
    if not isinstance(attempt_id, str) or len(attempt_id) != 16 or any(
        character not in "0123456789abcdef" for character in attempt_id
    ):
        raise ProjectSkillPersistenceError("invalid project Skill attempt generation id")
    if complete_id is not None and (
        not isinstance(complete_id, str)
        or len(complete_id) != 16
        or any(character not in "0123456789abcdef" for character in complete_id)
    ):
        raise ProjectSkillPersistenceError("invalid complete snapshot generation id")
    if payload["scan_status"] not in {"complete", "partial", "error", "security_reject"}:
        raise ProjectSkillPersistenceError("invalid project Skill attempt status")
    if type(payload["promoted"]) is not bool or type(payload["matches_complete_snapshot"]) is not bool:
        raise ProjectSkillPersistenceError("invalid project Skill attempt flags")
    if payload["matches_complete_snapshot"] != (attempt_id == complete_id):
        raise ProjectSkillPersistenceError("invalid project Skill attempt match flag")
    if payload["promoted"] != (payload["scan_status"] == "complete"):
        raise ProjectSkillPersistenceError("invalid project Skill attempt promotion flag")
    if payload["promoted"] and complete_id != attempt_id:
        raise ProjectSkillPersistenceError("promoted receipt does not match its snapshot")
    if (
        not isinstance(payload["attempted_at"], str)
        or len(payload["attempted_at"]) > 64
        or _NARROW_RFC3339.fullmatch(payload["attempted_at"]) is None
    ):
        raise ProjectSkillPersistenceError("invalid project Skill attempt time")
    try:
        parsed_time = datetime.fromisoformat(payload["attempted_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectSkillPersistenceError("invalid project Skill attempt time") from exc
    if parsed_time.tzinfo is None:
        raise ProjectSkillPersistenceError("invalid project Skill attempt time")
    if not isinstance(payload["issues"], list) or len(payload["issues"]) > 1000:
        raise ProjectSkillPersistenceError("invalid project Skill attempt issues")
    allowed_issue_keys = {"status", "code", "project_id", "relative_path"}
    stable_characters = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._:-")
    for issue in payload["issues"]:
        if (
            not isinstance(issue, dict)
            or not {"status", "code"}.issubset(issue)
            or not set(issue).issubset(allowed_issue_keys)
            or issue["status"] not in {"partial", "error", "security_reject"}
        ):
            raise ProjectSkillPersistenceError("invalid project Skill attempt issue")
        for key in ("code", "project_id"):
            if key not in issue:
                continue
            value = issue[key]
            if (
                not isinstance(value, str)
                or not 1 <= len(value) <= 240
                or value[0] not in "abcdefghijklmnopqrstuvwxyz0123456789"
                or any(character not in stable_characters for character in value)
            ):
                raise ProjectSkillPersistenceError("invalid project Skill attempt issue id")
        if "relative_path" in issue:
            relative = issue["relative_path"]
            if (
                not isinstance(relative, str)
                or not relative
                or len(relative) > 4000
                or relative.startswith("/")
                or "\\" in relative
                or "\x00" in relative
                or any(component in {"", ".", ".."} for component in relative.split("/"))
            ):
                raise ProjectSkillPersistenceError("invalid project Skill attempt issue path")
    return payload


class ProjectSkillStore:
    """Persistence facade rooted at one trusted AI-Toolbox directory."""

    def __init__(self, toolbox_root: Path | str):
        self.toolbox_root = Path(toolbox_root).expanduser().absolute()
        self.snapshot_path = self.toolbox_root / SNAPSHOT_TARGET
        self.last_attempt_path = self.toolbox_root / LAST_ATTEMPT_TARGET
        self.markdown_path = self.toolbox_root / MARKDOWN_TARGET
        self.last_receipt_error: str | None = None

    def load_snapshot(self) -> dict[str, Any] | None:
        raw = _read_allowed_bytes(self.toolbox_root, SNAPSHOT_TARGET)
        if raw is None:
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProjectSkillPersistenceError("invalid persisted project Skill snapshot") from exc
        try:
            validate_persistable_snapshot(payload)
        except PayloadValidationError as exc:
            raise ProjectSkillPersistenceError(
                "invalid persisted project Skill snapshot contract"
            ) from exc
        if payload["scan_status"] != "complete":
            raise ProjectSkillPersistenceError("persisted project Skill snapshot is incomplete")
        return payload

    def load_last_attempt(self) -> dict[str, Any] | None:
        raw = _read_allowed_bytes(self.toolbox_root, LAST_ATTEMPT_TARGET)
        if raw is None:
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProjectSkillPersistenceError("invalid project Skill attempt receipt") from exc
        validate_last_attempt(payload)
        return payload

    def load_markdown(self, expected_snapshot: Mapping[str, Any] | None = None) -> str | None:
        """Load Markdown only when it matches the committed complete snapshot."""

        raw = _read_allowed_bytes(self.toolbox_root, MARKDOWN_TARGET)
        if raw is None:
            return None
        snapshot = dict(expected_snapshot) if expected_snapshot is not None else self.load_snapshot()
        if snapshot is None:
            raise ProjectSkillPersistenceError("project Skill Markdown has no complete snapshot")
        validate_persistable_snapshot(snapshot)
        if snapshot["scan_status"] != "complete":
            raise ProjectSkillPersistenceError("project Skill Markdown snapshot is incomplete")
        try:
            markdown = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProjectSkillPersistenceError("invalid persisted project Skill Markdown") from exc
        if markdown != render_project_skill_markdown(snapshot):
            raise ProjectSkillPersistenceError("project Skill Markdown generation mismatch")
        return markdown

    def persist_scan_result(self, snapshot: Any) -> bool:
        row = validate_persistable_snapshot(snapshot)
        previous_complete = self.load_snapshot()
        if previous_complete is not None and (
            previous_complete.get("scan_scope", {}).get("root_config_id")
            != row.get("scan_scope", {}).get("root_config_id")
        ):
            # A complete snapshot from another configured root is not a legal
            # changes baseline.  It remains readable until this new complete
            # snapshot reaches the final commit point.
            previous_complete = None
        previous_generation = (
            previous_complete["generation_id"] if previous_complete is not None else None
        )
        if row["scan_status"] != "complete":
            receipt = _attempt_receipt(
                row,
                promoted=False,
                complete_snapshot_generation_id=previous_generation,
            )
            validate_last_attempt(receipt)
            write_project_skill_bytes_atomically(
                self.toolbox_root,
                LAST_ATTEMPT_TARGET,
                _canonical_json_bytes(receipt),
            )
            return False

        row = finalize_project_skill_changes(row, previous_complete)
        markdown = render_project_skill_markdown(row).encode("utf-8")
        snapshot_bytes = _canonical_json_bytes(row)
        receipt = _attempt_receipt(
            row,
            promoted=True,
            complete_snapshot_generation_id=row["generation_id"],
        )
        # Validate every derived artifact before the first durable promotion.
        validate_last_attempt(receipt)
        old_markdown = _read_allowed_bytes(self.toolbox_root, MARKDOWN_TARGET)
        old_snapshot = _read_allowed_bytes(self.toolbox_root, SNAPSHOT_TARGET)
        old_receipt = _read_allowed_bytes(self.toolbox_root, LAST_ATTEMPT_TARGET)
        try:
            write_project_skill_bytes_atomically(self.toolbox_root, MARKDOWN_TARGET, markdown)
            write_project_skill_bytes_atomically(
                self.toolbox_root,
                LAST_ATTEMPT_TARGET,
                _canonical_json_bytes(receipt),
            )
            # snapshot.json is the final commit point for the prepared auxiliaries.
            write_project_skill_bytes_atomically(self.toolbox_root, SNAPSHOT_TARGET, snapshot_bytes)
        except Exception as exc:
            rollback_errors: list[str] = []
            for target, previous in (
                (SNAPSHOT_TARGET, old_snapshot),
                (LAST_ATTEMPT_TARGET, old_receipt),
                (MARKDOWN_TARGET, old_markdown),
            ):
                try:
                    _restore_allowed_file(self.toolbox_root, target, previous)
                except Exception as rollback_exc:  # pragma: no cover - catastrophic I/O failure
                    rollback_errors.append(type(rollback_exc).__name__)
            if rollback_errors:
                raise ProjectSkillPersistenceError(
                    "project Skill promotion failed and rollback was incomplete"
                ) from exc
            raise

        self.last_receipt_error = None
        return True


__all__ = [
    "ALLOWED_TARGETS",
    "LAST_ATTEMPT_TARGET",
    "MARKDOWN_TARGET",
    "OUTPUT_ROOT",
    "ProjectSkillPersistenceError",
    "ProjectSkillStore",
    "SNAPSHOT_TARGET",
    "render_project_skill_markdown",
    "validate_last_attempt",
    "validate_persistable_snapshot",
    "write_project_skill_bytes_atomically",
]
