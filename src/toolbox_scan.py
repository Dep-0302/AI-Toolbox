"""AI-Toolbox read-only inventory scanner.

This module deliberately uses only Python's standard library.  Importing it has
no side effects.  It never executes discovered files, starts processes, opens a
network connection, or writes to a host directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import stat
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit


SCHEMA_VERSION = 1
ASSET_TYPES = frozenset({"skill", "plugin", "mcp", "cli", "sdk"})
ASSET_TYPE_ORDER = ("skill", "plugin", "mcp", "cli", "sdk")
HOST_ALLOWLIST = {
    "codex": "Codex",
    "claude": "Claude",
    "hermes": "Hermes",
    "workbuddy": "WorkBuddy",
    "antigravity": "Antigravity",
}
SCANNABLE_HOSTS = frozenset({"codex", "claude", "hermes", "workbuddy"})
# Codex materializes this directory only while some runtime bundles are active.
# Counting it as a persistent user asset makes a just-generated snapshot drift
# when the runtime unmounts it seconds later.  System skills remain available
# through the host, but are not user-manageable Toolbox assets.
TRANSIENT_SYSTEM_ROOTS = {("codex", "skill"): frozenset({".system"})}
IGNORED_NAMES = frozenset(
    {
        ".git",
        ".svn",
        ".hg",
        "__pycache__",
        "node_modules",
        ".DS_Store",
    }
)
# Whole-package identity must cover every host-exposed file.  Scan traversal
# may skip dependency or VCS directories, but the package digest must not:
# otherwise an executable under node_modules or a hook under .git could change
# without invalidating the vetted digest.  Only Finder's inert metadata is
# excluded from identity.
PACKAGE_METADATA_IGNORE = frozenset({".DS_Store"})
SAFE_MANIFESTS = {"skill": "SKILL.md", "plugin": "plugin.json"}
SAFE_PLUGIN_FIELDS = frozenset(
    {"name", "description", "version", "author", "license", "homepage"}
)
SAFE_STATIC_FIELDS = frozenset(
    {"type", "name", "description", "version", "author", "license", "source_ref"}
)
SKILL_ORIGIN_KINDS = frozenset(
    {"github", "website", "chat", "local", "system", "unknown"}
)
SKILL_ORIGIN_BASES = frozenset(
    {
        "download_metadata",
        "frontmatter",
        "hermes_bundled_manifest",
        "hermes_skill_registry",
        "workbuddy_skill_metadata",
        "conflicting_evidence",
        "unavailable",
    }
)
WHERE_FROM_XATTR = "com.apple.metadata:kMDItemWhereFroms"
MAX_ORIGIN_METADATA_BYTES = 64 * 1024
PROVENANCE_METADATA_BYTES = 128 * 1024
MAX_METADATA_JSON_DEPTH = 64
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
SLUG_CHARS = re.compile(r"[^a-z0-9]+")
CODEX_METADATA_ADAPTERS = {
    "mcp": "codex_plugin_mcp_v1",
    "cli": "codex_cli_metadata_v1",
    "sdk": "codex_plugin_sdk_v1",
}
DIRECTORY_ADAPTER_ID = "manifest_directory_v1"
EXPECTED_CODEX_METADATA_CONFIG = {
    "plugin_cache_root": "{home}/.codex/plugins/cache",
    "mcp_manifest_name": ".mcp.json",
    "cli_link": "{home}/.local/bin/codex",
    "cli_target": "{home}/.codex/plugins/.plugin-appserver/codex",
}
EXPECTED_HOST_ROOTS = {
    "codex": {
        "skill_roots": ("{home}/.codex/skills",),
        "plugin_roots": ("{home}/.codex/plugins/cache",),
    },
    "claude": {
        "skill_roots": ("{home}/.claude/skills",),
        "plugin_roots": ("{home}/.claude/plugins/cache",),
    },
    "hermes": {
        "skill_roots": ("{home}/.hermes/skills",),
        "plugin_roots": (),
    },
    "workbuddy": {
        "skill_roots": ("{home}/.workbuddy/skills",),
        "plugin_roots": (),
    },
    "antigravity": {"skill_roots": (), "plugin_roots": ()},
}
SDK_PACKAGE_ALLOWLIST = frozenset({"@modelcontextprotocol/sdk"})
EXPECTED_SCAN_LIMITS = {
    "max_file_bytes": 262144,
    "max_overlay_bytes": 2097152,
    "max_package_file_bytes": 4194304,
    "max_package_bytes": 33554432,
    "max_package_files": 2000,
    "max_depth": 5,
    "max_entries_per_root": 5000,
    "scan_timeout_seconds": 8.0,
    "max_text_length": 4000,
}
EXCLUDED_BOUNDARIES = (
    "/Applications",
    "browser_sessions",
    "credentials",
    "cookies",
    "ssh_aws",
    "other_projects",
    ".git",
    "unapproved_config_bodies",
    "codex_config_auth_and_global_state",
    "codex_logs_sessions_and_databases",
    "codex_dynamic_runtime_caches",
    "binaries",
    "path_wide_cli_discovery",
    "recursive_dependency_scans",
    "out_of_allowlist_symlinks",
    "ephemeral_system_managed_skills",
)
SENSITIVE_CONFIG_KEYS = frozenset(
    {
        "apikey",
        "accesstoken",
        "auth",
        "authorization",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "clientsecret",
        "env",
        "envvars",
        "header",
        "headers",
        "password",
        "privatekey",
        "secret",
        "secrets",
        "token",
        "bearertoken",
    }
)


class ScanBudgetExceeded(RuntimeError):
    """Raised internally when the configured read-only scan budget is spent."""


class PackageFingerprintIncomplete(RuntimeError):
    """Raised when a package cannot be fully hashed inside the frozen budget."""


def _bundle_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _iso_now(now: Any) -> str:
    value = now() if callable(now) else now
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise TypeError("now must be None, a datetime, an ISO string, or a callable")


def _safe_text(value: Any, max_length: int) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    text = CONTROL_CHARS.sub("", str(value)).strip()
    return text[:max_length]


def _display_path(path: Path, home: Path, *, resolve: bool = False) -> str:
    try:
        display_path = path.resolve(strict=False) if resolve else path.absolute()
        display_home = home.resolve(strict=False) if resolve else home.absolute()
        relative = display_path.relative_to(display_home)
        return "~/" + relative.as_posix()
    except (ValueError, OSError):
        if not resolve:
            try:
                relative = path.absolute().relative_to(home.resolve(strict=False))
                return "~/" + relative.as_posix()
            except (ValueError, OSError):
                pass
        return (path.resolve(strict=False) if resolve else path).as_posix()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolved_without_following_leaf(path: Path) -> Path:
    """Resolve a path for containment checks without requiring it to exist."""

    return path.resolve(strict=False)


def _within_any(path: Path, roots: Iterable[Path]) -> bool:
    resolved = _resolved_without_following_leaf(path)
    return any(_is_relative_to(resolved, root) for root in roots)


def _path_chain_contains_symlink(path: Path, stop: Path) -> bool:
    current = path.absolute()
    boundary = stop.absolute()
    while True:
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except FileNotFoundError:
            pass
        if current == boundary:
            return False
        parent = current.parent
        if parent == current or not _is_relative_to(current, boundary):
            return True
        current = parent


def _read_regular_file_limited(path: Path, max_bytes: int) -> bytes:
    """Read one regular file without following a symlink or exceeding max_bytes."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        if info.st_size > max_bytes:
            raise ValueError(f"file exceeds {max_bytes} bytes")
        data = os.read(descriptor, max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"file exceeds {max_bytes} bytes")
        return data
    finally:
        os.close(descriptor)


def _relative_parts_beneath(root: Path, path: Path) -> tuple[str, ...]:
    absolute_root = root.absolute()
    absolute_path = path.absolute()
    try:
        relative = absolute_path.relative_to(absolute_root)
    except ValueError as exc:
        canonical_root = root.resolve(strict=False)
        try:
            relative = absolute_path.relative_to(canonical_root)
        except ValueError:
            raise ValueError("metadata source is outside its anchored root") from exc
    parts = relative.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("metadata source has an unsafe relative path")
    return parts


def _open_directory_beneath(root: Path, path: Path) -> int:
    """Open a directory by walking from an anchored root without symlinks."""

    parts = _relative_parts_beneath(root, path)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    directory_fd = os.open(root.absolute(), directory_flags)
    try:
        for part in parts:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except Exception:
        os.close(directory_fd)
        raise


def _read_regular_descriptor_limited(descriptor: int, max_bytes: int) -> bytes:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("metadata source is not a regular file")
    if info.st_size > max_bytes:
        raise ValueError(f"metadata source exceeds {max_bytes} bytes")
    data = os.read(descriptor, max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"metadata source exceeds {max_bytes} bytes")
    return data


def _read_regular_file_limited_at(
    root_fd: int, relative_parts: tuple[str, ...], max_bytes: int
) -> bytes:
    """Read one relative file while staying bound to an open package directory."""

    if not relative_parts or any(
        not part or part in {".", ".."} or "/" in part for part in relative_parts
    ):
        raise ValueError("metadata source has an unsafe relative path")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    file_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
        file_flags |= os.O_NOFOLLOW
    directory_fd = os.dup(root_fd)
    try:
        for part in relative_parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(relative_parts[-1], file_flags, dir_fd=directory_fd)
        try:
            return _read_regular_descriptor_limited(file_fd, max_bytes)
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


def _read_regular_file_limited_beneath(root: Path, path: Path, max_bytes: int) -> bytes:
    """Read a regular file through an anchored, no-symlink directory walk.

    Opening every component relative to an already-open directory prevents a
    cache ancestor from being swapped to a symlink between a lexical boundary
    check and the read.  Only the approved leaf body is opened.
    """

    parts = _relative_parts_beneath(root, path)
    if not parts:
        raise ValueError("metadata source must name a file beneath its root")

    file_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= os.O_NOFOLLOW

    directory_fd = _open_directory_beneath(root, path.parent)
    try:
        file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
        try:
            return _read_regular_descriptor_limited(file_fd, max_bytes)
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


def _load_trusted_json(path: Path, max_bytes: int) -> Any:
    data = _read_regular_file_limited(path, max_bytes)
    return json.loads(data.decode("utf-8"))


def _load_metadata_json(path: Path, max_bytes: int) -> Any:
    """Load one bounded cache manifest and reject ambiguous duplicate keys."""

    data = _read_regular_file_limited(path, max_bytes)

    return _decode_metadata_json(data)


def _load_metadata_json_beneath(root: Path, path: Path, max_bytes: int) -> Any:
    """Load one bounded manifest through an anchored no-symlink path."""

    data = _read_regular_file_limited_beneath(root, path, max_bytes)
    return _decode_metadata_json(data)


def _load_metadata_json_at(
    root_fd: int, relative_parts: tuple[str, ...], max_bytes: int
) -> Any:
    """Load bounded metadata JSON from one already-bound package directory."""

    return _decode_metadata_json(
        _read_regular_file_limited_at(root_fd, relative_parts, max_bytes)
    )


def _decode_metadata_json(data: bytes) -> Any:
    """Decode bounded metadata JSON while rejecting duplicate object keys."""

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in pairs:
            if key in output:
                raise ValueError("metadata manifest contains a duplicate key")
            output[key] = value
        return output

    try:
        parsed = json.loads(data.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("metadata manifest is not valid UTF-8 JSON") from exc
    _reject_excessive_json_depth(parsed)
    return parsed


def _reject_excessive_json_depth(
    value: Any, max_depth: int = MAX_METADATA_JSON_DEPTH
) -> None:
    """Reject deeply nested JSON without relying on interpreter recursion."""

    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if not isinstance(current, (dict, list)):
            continue
        if depth > max_depth:
            raise ValueError(
                f"metadata manifest exceeds maximum JSON nesting depth ({max_depth})"
            )
        children = current.values() if isinstance(current, dict) else current
        stack.extend((child, depth + 1) for child in children)


def _origin_kind_from_reference(reference: Any) -> str:
    value = _safe_text(reference, 2048)
    lowered = value.casefold()
    if not lowered:
        return ""
    if lowered in {"official", "bundled", "builtin", "system"}:
        return "system"
    if lowered in {"skills.sh", "skillhub", "marketplace"}:
        return "website"
    if lowered in {"github", "github.com"}:
        return "github"
    if lowered in {"chat", "wechat", "weixin", "微信", "聊天"}:
        return "chat"
    if lowered in {"local", "local-created", "local_created", "本地创建"}:
        return "local"
    if lowered.startswith(("weixin://", "wechat://")):
        return "chat"
    try:
        host = (urlsplit(value).hostname or "").casefold().rstrip(".")
    except ValueError:
        host = ""
    if host == "github.com" or host.endswith(".github.com"):
        return "github"
    if host in {"weixin.qq.com", "mp.weixin.qq.com", "wx.qq.com"} or host.endswith(
        ".weixin.qq.com"
    ):
        return "chat"
    return "website" if host else ""


@lru_cache(maxsize=1)
def _darwin_getxattr_function() -> Any:
    if sys.platform != "darwin":
        return None
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        native_getxattr = libc.getxattr
        native_getxattr.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
            ctypes.c_int,
        ]
        native_getxattr.restype = ctypes.c_ssize_t
        return native_getxattr
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _download_metadata_origin(path: Path) -> str:
    """Project one Finder download source into the six display categories."""

    raw = b""
    getxattr = getattr(os, "getxattr", None)
    if callable(getxattr):
        try:
            try:
                raw = getxattr(path, WHERE_FROM_XATTR, follow_symlinks=False)
            except TypeError:
                raw = getxattr(path, WHERE_FROM_XATTR)
        except (OSError, ValueError):
            raw = b""
    elif sys.platform == "darwin":
        try:
            import ctypes

            native_getxattr = _darwin_getxattr_function()
            if native_getxattr is None:
                return ""
            encoded_path = os.fsencode(path)
            encoded_name = WHERE_FROM_XATTR.encode("utf-8")
            size = native_getxattr(encoded_path, encoded_name, None, 0, 0, 0x0001)
            if 0 < size <= MAX_ORIGIN_METADATA_BYTES:
                buffer = ctypes.create_string_buffer(size)
                read_size = native_getxattr(
                    encoded_path,
                    encoded_name,
                    buffer,
                    size,
                    0,
                    0x0001,
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
        if isinstance(reference, str):
            kind = _origin_kind_from_reference(reference)
            if kind:
                return kind
    return ""


def _safe_relative_package_path(value: Any) -> str:
    path = Path(_safe_text(value, 1000))
    if not str(path) or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return ""
    return path.as_posix()


def _load_skill_provenance(home: Path, limits: dict[str, Any]) -> dict[str, Any]:
    """Read only exact, bounded Skill provenance manifests inside host roots."""

    bundled_names: set[str] = set()
    hermes_registry: dict[str, tuple[str, str]] = {}
    hermes_root = home / ".hermes" / "skills"
    bundled_path = hermes_root / ".bundled_manifest"
    try:
        raw = _read_regular_file_limited_beneath(
            home, bundled_path, min(PROVENANCE_METADATA_BYTES, int(limits["max_file_bytes"]))
        ).decode("utf-8")
        for line in raw.splitlines()[:5000]:
            name, separator, digest = line.partition(":")
            safe_name = _safe_text(name, 200)
            if separator and safe_name and re.fullmatch(r"[0-9a-fA-F]{32,64}", digest.strip()):
                bundled_names.add(safe_name)
    except (OSError, UnicodeDecodeError, ValueError):
        pass

    lock_path = hermes_root / ".hub" / "lock.json"
    try:
        lock = _load_metadata_json_beneath(
            home, lock_path, min(PROVENANCE_METADATA_BYTES, int(limits["max_file_bytes"]))
        )
    except (OSError, ValueError):
        lock = None
    installed = lock.get("installed") if isinstance(lock, dict) else None
    if isinstance(installed, dict):
        for record in list(installed.values())[:5000]:
            if not isinstance(record, dict):
                continue
            install_path = _safe_relative_package_path(record.get("install_path"))
            if not install_path:
                continue
            source_kind = _origin_kind_from_reference(record.get("source"))
            if record.get("trust_level") == "builtin" or source_kind == "system":
                source_kind = "system"
            if source_kind in SKILL_ORIGIN_KINDS - {"unknown"}:
                existing = hermes_registry.get(install_path)
                evidence = (source_kind, "hermes_skill_registry")
                hermes_registry[install_path] = (
                    evidence
                    if existing in {None, evidence}
                    else ("unknown", "conflicting_evidence")
                )

    return {
        "hermes_bundled_names": frozenset(bundled_names),
        "hermes_registry": hermes_registry,
    }


def _skill_origin(
    host_id: str,
    manifest: Path,
    parsed: dict[str, Any],
    home: Path,
    limits: dict[str, Any],
    provenance: dict[str, Any],
) -> tuple[str, str]:
    if host_id == "hermes":
        hermes_root = home / ".hermes" / "skills"
        try:
            package_path = manifest.parent.absolute().relative_to(hermes_root.absolute()).as_posix()
        except ValueError:
            package_path = ""
        registry_evidence = provenance.get("hermes_registry", {}).get(package_path)
        if registry_evidence:
            return registry_evidence
        if parsed.get("name") in provenance.get("hermes_bundled_names", frozenset()):
            return "system", "hermes_bundled_manifest"

    if host_id == "workbuddy":
        metadata_path = manifest.parent / "_skillhub_meta.json"
        try:
            metadata = _load_metadata_json_beneath(
                home,
                metadata_path,
                min(PROVENANCE_METADATA_BYTES, int(limits["max_file_bytes"])),
            )
        except (OSError, ValueError):
            metadata = None
        if isinstance(metadata, dict):
            source_kind = _origin_kind_from_reference(metadata.get("source"))
            if source_kind in SKILL_ORIGIN_KINDS - {"unknown"}:
                return source_kind, "workbuddy_skill_metadata"

    if str(parsed.get("agent_created", "")).casefold() in {"true", "yes", "1"}:
        return "local", "frontmatter"

    for candidate in (manifest, manifest.parent):
        source_kind = _download_metadata_origin(candidate)
        if source_kind:
            return source_kind, "download_metadata"
    return "unknown", "unavailable"


def _metadata_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).casefold())


def _contains_sensitive_config(value: Any) -> bool:
    stack = [value]
    examined = 0
    while stack:
        current = stack.pop()
        examined += 1
        if examined > 10000:
            return True
        if isinstance(current, dict):
            for key, nested in current.items():
                if _normalized_key(key) in SENSITIVE_CONFIG_KEYS:
                    return True
                stack.append(nested)
        elif isinstance(current, list):
            stack.extend(current)
        elif isinstance(current, str):
            normalized = _normalized_key(current)
            if any(
                marker in normalized
                for marker in SENSITIVE_CONFIG_KEYS
                if len(marker) >= 5
            ):
                return True
    return False


def compute_package_fingerprint(
    root: Path,
    limits: dict[str, Any],
    deadline: float,
    approved_relative_paths: frozenset[str] | None = None,
    *,
    anchor_root: Path | None = None,
    expected_manifest_fingerprint: str | None = None,
    package_fd: int | None = None,
) -> tuple[str, int, int]:
    """Hash approved manifest bodies only; fail closed on every other entry.

    Exact package identity is intentionally unavailable when a package has
    scripts, references, dependencies, links, or other files.  Those bodies
    may contain credentials or runtime data, so observe-only mode records an
    incomplete fingerprint rather than opening them.
    """

    max_files = int(limits["max_package_files"])
    max_total_bytes = int(limits["max_package_bytes"])
    max_file_bytes = int(limits["max_package_file_bytes"])
    records: list[tuple[str, str, str]] = []
    approved = approved_relative_paths or frozenset({"SKILL.md", "plugin.json"})
    approved_directories = {
        "/".join(parts[:index])
        for approved_path in approved
        for parts in [approved_path.split("/")]
        for index in range(1, len(parts))
    }
    entry_count = 0
    file_count = 0
    total_bytes = 0
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    file_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
        file_flags |= os.O_NOFOLLOW
    if package_fd is None:
        anchor = root.parent if anchor_root is None else anchor_root
        try:
            initial_fd = _open_directory_beneath(anchor, root)
        except (OSError, ValueError) as exc:
            raise PackageFingerprintIncomplete("package_directory_metadata_unverified") from exc
    else:
        initial_fd = os.dup(package_fd)
    queue: list[tuple[int, str]] = [(initial_fd, "")]
    try:
        while queue:
            if time.monotonic() > deadline:
                raise ScanBudgetExceeded("scan timeout reached while hashing a package")
            directory_fd, prefix = queue.pop(0)
            try:
                try:
                    names: list[str] = []
                    with os.scandir(directory_fd) as iterator:
                        for entry in iterator:
                            if time.monotonic() > deadline:
                                raise ScanBudgetExceeded(
                                    "scan timeout reached while hashing a package"
                                )
                            if entry.name in PACKAGE_METADATA_IGNORE:
                                continue
                            if entry_count + len(names) >= max_files:
                                raise PackageFingerprintIncomplete(
                                    "package entry budget exceeded"
                                )
                            names.append(entry.name)
                    names.sort(key=str.casefold)
                except (OSError, PermissionError) as exc:
                    raise PackageFingerprintIncomplete("package_directory_unreadable") from exc

                for name in names:
                    if time.monotonic() > deadline:
                        raise ScanBudgetExceeded(
                            "scan timeout reached while hashing a package"
                        )
                    relative = f"{prefix}/{name}" if prefix else name
                    entry_count += 1
                    if entry_count > max_files:
                        raise PackageFingerprintIncomplete("package entry budget exceeded")
                    try:
                        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            raise PackageFingerprintIncomplete(
                                "package contains an unapproved link; target and body were not read"
                            )
                        if stat.S_ISDIR(info.st_mode):
                            if relative not in approved_directories:
                                raise PackageFingerprintIncomplete(
                                    "package contains an unapproved directory; contents were not read"
                                )
                            child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                            queue.append((child_fd, relative))
                            continue
                        if not stat.S_ISREG(info.st_mode):
                            raise PackageFingerprintIncomplete(
                                "unsupported_special_package_entry"
                            )
                        if relative not in approved:
                            raise PackageFingerprintIncomplete(
                                "package contains an unapproved file; body and name were not persisted"
                            )
                        file_fd = os.open(name, file_flags, dir_fd=directory_fd)
                        try:
                            data = _read_regular_descriptor_limited(file_fd, max_file_bytes)
                        finally:
                            os.close(file_fd)
                        total_bytes += len(data)
                        file_count += 1
                        if total_bytes > max_total_bytes:
                            raise PackageFingerprintIncomplete(
                                "package hashing budget exceeded"
                            )
                        records.append(
                            ("file", relative, hashlib.sha256(data).hexdigest())
                        )
                    except PackageFingerprintIncomplete:
                        raise
                    except (OSError, ValueError) as exc:
                        raise PackageFingerprintIncomplete("package_entry_unreadable") from exc
            finally:
                os.close(directory_fd)
    finally:
        for descriptor, _ in queue:
            os.close(descriptor)

    if {relative for _, relative, _ in records} != set(approved):
        raise PackageFingerprintIncomplete("approved_manifest_missing_during_observation")
    if expected_manifest_fingerprint is not None and any(
        digest != expected_manifest_fingerprint for _, _, digest in records
    ):
        raise PackageFingerprintIncomplete("approved_manifest_changed_during_observation")

    canonical = json.dumps(records, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest(), file_count, total_bytes


# Kept as an internal compatibility alias for the candidate tests and any
# in-flight review code.  New controlled-action code imports the public name so
# the scanner and writer cannot silently drift to different digest algorithms.
_package_fingerprint = compute_package_fingerprint


def _parse_scalar(value: str, max_length: int) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return _safe_text(value, max_length)


def _parse_frontmatter(text: str, max_length: int) -> dict[str, str]:
    """Parse a deliberately small, non-executing YAML front-matter subset."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    closing = None
    for index, line in enumerate(lines[1:201], start=1):
        if line.strip() == "---":
            closing = index
            break
    if closing is None:
        return {}

    allowed = {"name", "description", "version", "author", "license", "agent_created"}
    result: dict[str, str] = {}
    index = 1
    while index < closing:
        line = lines[index]
        index += 1
        if not line or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, raw = line.split(":", 1)
        key = key.strip().lower()
        if key not in allowed:
            continue
        raw = raw.strip()
        if raw in {"|", ">", "|-", ">-"}:
            block: list[str] = []
            while index < closing:
                next_line = lines[index]
                if next_line and not next_line[0].isspace():
                    break
                block.append(next_line.strip())
                index += 1
            separator = " " if raw.startswith(">") else "\n"
            result[key] = _safe_text(separator.join(block), max_length)
        else:
            result[key] = _parse_scalar(raw, max_length)
    return result


def _parse_skill_manifest(
    path: Path, home: Path, limits: dict[str, Any]
) -> dict[str, Any]:
    data = _read_regular_file_limited_beneath(
        home, path, int(limits["max_file_bytes"])
    )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("SKILL.md is not valid UTF-8") from exc
    metadata = _parse_frontmatter(text, int(limits["max_text_length"]))
    name = metadata.get("name") or path.parent.name
    return {
        "type": "skill",
        "name": _safe_text(name, 200) or "unnamed-skill",
        "description": _safe_text(metadata.get("description"), 4000),
        "version": _safe_text(metadata.get("version"), 200),
        "author": _safe_text(metadata.get("author"), 500),
        "license": _safe_text(metadata.get("license"), 500),
        "agent_created": _safe_text(metadata.get("agent_created"), 20),
        "manifest_fingerprint": hashlib.sha256(data).hexdigest(),
        "composition": [],
    }


def _composition_names(value: Any, component_type: str, max_length: int) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    output: list[dict[str, str]] = []
    for item in value[:200]:
        if isinstance(item, str):
            name = _safe_text(item, max_length)
        elif isinstance(item, dict):
            name = _safe_text(item.get("name") or item.get("id"), max_length)
        else:
            name = ""
        if name:
            output.append({"type": component_type, "name": name})
    return output


def _parse_plugin_manifest(
    path: Path, home: Path, limits: dict[str, Any]
) -> dict[str, Any]:
    package_fd: int | None = None
    if path.name == "plugin.json" and path.parent.name == ".codex-plugin":
        package_fd = _open_directory_beneath(home, path.parent.parent)
        try:
            data = _read_regular_file_limited_at(
                package_fd,
                (".codex-plugin", "plugin.json"),
                int(limits["max_file_bytes"]),
            )
        except Exception:
            os.close(package_fd)
            raise
    else:
        data = _read_regular_file_limited_beneath(
            home, path, int(limits["max_file_bytes"])
        )
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        if package_fd is not None:
            os.close(package_fd)
        raise ValueError("plugin.json is not valid UTF-8 JSON") from exc
    try:
        _reject_excessive_json_depth(parsed)
    except ValueError:
        if package_fd is not None:
            os.close(package_fd)
        raise
    if not isinstance(parsed, dict):
        if package_fd is not None:
            os.close(package_fd)
        raise ValueError("plugin.json root must be an object")
    max_text = int(limits["max_text_length"])
    safe = {key: _safe_text(parsed.get(key), max_text) for key in SAFE_PLUGIN_FIELDS}
    interface = parsed.get("interface") if isinstance(parsed.get("interface"), dict) else {}
    display_name = _safe_text(interface.get("displayName"), 200)
    name = safe.get("name") or path.parent.name
    composition: list[dict[str, str]] = []
    composition.extend(_composition_names(parsed.get("skills"), "skill", 200))
    composition.extend(_composition_names(parsed.get("mcp"), "mcp", 200))
    composition.extend(_composition_names(parsed.get("mcp_servers"), "mcp", 200))
    composition.extend(_composition_names(parsed.get("commands"), "cli", 200))
    result = {
        "type": "plugin",
        "name": _safe_text(name, 200) or "unnamed-plugin",
        "description": safe.get("description", ""),
        "version": safe.get("version", ""),
        "author": safe.get("author", ""),
        "license": safe.get("license", ""),
        "display_name": display_name,
        "homepage": safe.get("homepage", ""),
        "manifest_fingerprint": hashlib.sha256(data).hexdigest(),
        "composition": composition,
    }
    if package_fd is not None:
        result["metadata_package_fd"] = package_fd
    return result


def _attach_package_identity(
    parsed: dict[str, Any],
    manifest: Path,
    home: Path,
    limits: dict[str, Any],
    deadline: float,
) -> str | None:
    """Attach the bounded package digest; return a human-readable lock reason."""

    package_root = manifest.parent
    approved_paths = frozenset({manifest.name})
    if (
        parsed.get("type") == "plugin"
        and manifest.name == "plugin.json"
        and manifest.parent.name == ".codex-plugin"
    ):
        package_root = manifest.parent.parent
        approved_paths = frozenset({".codex-plugin/plugin.json"})

    try:
        digest, file_count, total_bytes = compute_package_fingerprint(
            package_root,
            limits,
            deadline,
            approved_paths,
            anchor_root=home,
            expected_manifest_fingerprint=parsed["manifest_fingerprint"],
            package_fd=parsed.get("metadata_package_fd"),
        )
    except PackageFingerprintIncomplete as exc:
        # Keep the manifest visible but never merge it as an exact duplicate.
        parsed["fingerprint"] = parsed["manifest_fingerprint"]
        parsed["package_fingerprint_status"] = "incomplete"
        parsed["package_file_count"] = None
        parsed["package_bytes"] = None
        return _safe_text(exc, 500)
    except Exception:
        package_fd = parsed.pop("metadata_package_fd", None)
        if isinstance(package_fd, int):
            os.close(package_fd)
        raise
    parsed["fingerprint"] = digest
    parsed["package_fingerprint_status"] = "complete"
    parsed["package_file_count"] = file_count
    parsed["package_bytes"] = total_bytes
    return None


def _finding(
    code: str,
    severity: str,
    title: str,
    detail: str,
    **context: Any,
) -> dict[str, Any]:
    finding = {"code": code, "severity": severity, "title": title, "detail": detail}
    finding.update({key: value for key, value in context.items() if value not in (None, "")})
    return finding


def _binding(
    host_id: str,
    asset_type: str,
    path: Path,
    home: Path,
    is_link: bool,
    link_target: Path | None,
) -> dict[str, Any]:
    system_managed = ".system" in path.parts
    if asset_type == "plugin":
        presence = "cached"
        provisioning = "cache"
        locked_reason = "read_only_cache_inventory"
    elif system_managed:
        presence = "present"
        provisioning = "system_managed"
        locked_reason = "system_managed"
    else:
        presence = "present"
        provisioning = "unmanaged_symlink" if is_link else "copy"
        locked_reason = "read_only_inventory"
    return {
        "host_id": host_id,
        "presence": presence,
        "provisioning": provisioning,
        "link_health": "ok" if is_link else "na",
        "activation": "unknown",
        "effective": "unverified",
        "action": "locked",
        "locked_reason": locked_reason,
        "observed_path": _display_path(path, home),
        "link_target": _display_path(link_target, home) if link_target else None,
    }


def _metadata_binding(
    host_id: str,
    observed_path: Path,
    home: Path,
    *,
    presence: str,
    provisioning: str,
    link_target: Path | None = None,
) -> dict[str, Any]:
    return {
        "host_id": host_id,
        "presence": presence,
        "provisioning": provisioning,
        "link_health": "ok" if link_target is not None else "na",
        "activation": "unknown",
        "effective": "unverified",
        "action": "locked",
        "locked_reason": "safe_metadata_only",
        "observed_path": _display_path(observed_path, home),
        "link_target": _display_path(link_target, home) if link_target else None,
        "package_fingerprint": None,
        "package_fingerprint_status": "incomplete",
    }


def _metadata_occurrence(
    *,
    asset_type: str,
    name: str,
    description: str,
    version: str,
    author: str,
    license_name: str,
    display_name: str,
    source_ref: str,
    binding: dict[str, Any],
    observation: dict[str, Any],
) -> dict[str, Any]:
    projection = {
        "type": asset_type,
        "name": name,
        "description": description,
        "version": version,
        "author": author,
        "license": license_name,
        "display_name": display_name,
        "source_ref": source_ref,
        "observation": observation,
    }
    fingerprint = _metadata_fingerprint(projection)
    return {
        "type": asset_type,
        "name": _safe_text(name, 200) or f"unnamed-{asset_type}",
        "description": _safe_text(description, 4000),
        "version": _safe_text(version, 200),
        "author": _safe_text(author, 500),
        "license": _safe_text(license_name, 500),
        "display_name": _safe_text(display_name, 200),
        "fingerprint": fingerprint,
        "manifest_fingerprint": fingerprint,
        "package_fingerprint_status": "incomplete",
        "package_file_count": None,
        "package_bytes": None,
        "composition": [],
        "source_ref": source_ref,
        "manifest_path": None,
        "binding": binding,
        "observation": observation,
        "trust_locked_reason": "safe_metadata_only",
    }


def _metadata_problem(
    findings: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    *,
    error_code: str,
    path: Path,
    home: Path,
    message: str,
) -> None:
    display_path = _display_path(path, home)
    errors.append(
        {
            "code": error_code,
            "host_id": "codex",
            "path": display_path,
            "message": _safe_text(message, 500),
        }
    )
    findings.append(
        _finding(
            "metadata_observation_incomplete",
            "warning",
            "Codex 元数据观察不完整",
            "该来源已失败关闭，不会执行、跟随或输出其操作字段。",
            host_id="codex",
            path=display_path,
            reason=error_code,
        )
    )


def _metadata_state(
    asset_type: str,
    status: str,
    source_refs: Iterable[str],
    observed_source_count: int,
) -> dict[str, Any]:
    return {
        "host_id": "codex",
        "asset_type": asset_type,
        "adapter_id": CODEX_METADATA_ADAPTERS[asset_type],
        "status": status,
        "source_refs": sorted({value for value in source_refs if value}),
        "configured_source_count": 1,
        "observed_source_count": observed_source_count,
        "item_count": 0,
    }


def _metadata_status(base_status: str, had_problem: bool) -> str:
    if base_status in {"error", "blocked_symlink_root", "not_directory"}:
        return "error"
    if base_status == "missing":
        return "missing"
    if base_status == "partial" or had_problem:
        return "partial"
    return "observed"


def _plugin_packages(
    occurrences: Iterable[dict[str, Any]],
    plugin_root: Path,
) -> list[dict[str, Any]]:
    packages: dict[str, dict[str, Any]] = {}
    for occurrence in occurrences:
        binding = occurrence.get("binding")
        manifest = occurrence.get("manifest_path")
        package_fd = occurrence.get("metadata_package_fd")
        if (
            occurrence.get("type") != "plugin"
            or not isinstance(binding, dict)
            or binding.get("host_id") != "codex"
            or not isinstance(manifest, Path)
            or manifest.name != "plugin.json"
            or manifest.parent.name != ".codex-plugin"
            or not isinstance(package_fd, int)
        ):
            continue
        package_root = manifest.parent.parent
        try:
            relative_parts = package_root.absolute().relative_to(
                plugin_root.absolute()
            ).parts
        except ValueError:
            continue
        if len(relative_parts) != 3:
            continue
        packages[package_root.absolute().as_posix()] = {
            "root": package_root,
            "directory_fd": package_fd,
            "channel": _safe_text(relative_parts[0], 120),
            "name": _safe_text(occurrence.get("name") or relative_parts[-2], 200),
            "version": _safe_text(occurrence.get("version") or relative_parts[-1], 200),
            "author": _safe_text(occurrence.get("author"), 500),
            "license": _safe_text(occurrence.get("license"), 500),
        }
    return sorted(packages.values(), key=lambda item: item["root"].as_posix())


def _scan_codex_mcp_metadata(
    packages: list[dict[str, Any]],
    plugin_root: Path,
    plugin_scope_status: str,
    home: Path,
    manifest_name: str,
    limits: dict[str, Any],
    deadline: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    occurrences: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    source_refs = {_display_path(plugin_root, home)}
    observed_sources = 0
    had_problem = False

    if plugin_scope_status not in {"missing", "error", "blocked_symlink_root", "not_directory"}:
        for package in packages:
            if time.monotonic() > deadline:
                had_problem = True
                _metadata_problem(
                    findings,
                    errors,
                    error_code="metadata_scan_budget_exceeded",
                    path=plugin_root,
                    home=home,
                    message="Codex MCP metadata observation exceeded its read budget",
                )
                break
            manifest = package["root"] / manifest_name
            try:
                parsed = _load_metadata_json_at(
                    package["directory_fd"],
                    (manifest_name,),
                    int(limits["max_file_bytes"]),
                )
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as exc:
                source_refs.add(_display_path(manifest, home))
                had_problem = True
                _metadata_problem(
                    findings,
                    errors,
                    error_code="metadata_source_rejected",
                    path=manifest,
                    home=home,
                    message=str(exc),
                )
                continue
            source_refs.add(_display_path(manifest, home))
            servers = parsed.get("mcpServers") if isinstance(parsed, dict) else None
            if not isinstance(servers, dict):
                had_problem = True
                _metadata_problem(
                    findings,
                    errors,
                    error_code="metadata_source_rejected",
                    path=manifest,
                    home=home,
                    message="MCP metadata source must contain an mcpServers object",
                )
                continue
            observed_sources += 1
            for raw_server_id, server in servers.items():
                if not isinstance(server, dict):
                    had_problem = True
                    _metadata_problem(
                        findings,
                        errors,
                        error_code="metadata_entry_rejected",
                        path=manifest,
                        home=home,
                        message="MCP server metadata entry must be an object",
                    )
                    continue
                server_id = _safe_text(raw_server_id, 200)
                if not server_id:
                    had_problem = True
                    continue
                keys = {_normalized_key(key) for key in server}
                has_command = "command" in keys
                has_url = bool(keys & {"url", "httpurl", "serverurl"})
                if has_command and has_url:
                    transport_kind = "ambiguous"
                elif has_command:
                    transport_kind = "stdio"
                elif has_url:
                    transport_kind = "http"
                else:
                    transport_kind = "unknown"
                sensitive_present = _contains_sensitive_config(server)
                # Free-text title/description are never projected. They are
                # not required for identity and cannot be proven credential-free.
                title = ""
                description = ""
                source_ref = _display_path(manifest, home)
                observation = {
                    "adapter_id": CODEX_METADATA_ADAPTERS["mcp"],
                    "basis": "cache_manifest_projection",
                    "plugin_channel": package["channel"] or None,
                    "plugin_name": package["name"] or None,
                    "plugin_version": package["version"] or None,
                    "server_id": server_id,
                    "transport_kind": transport_kind,
                    "sensitive_config_present": sensitive_present,
                    "cli_name": None,
                    "sdk_package_name": None,
                }
                occurrences.append(
                    _metadata_occurrence(
                        asset_type="mcp",
                        name=server_id,
                        description=description or f"MCP server declared by Codex plugin {package['name']}.",
                        version=package["version"],
                        author=package["author"],
                        license_name=package["license"],
                        display_name=title,
                        source_ref=source_ref,
                        binding=_metadata_binding(
                            "codex", manifest, home, presence="cached", provisioning="cache"
                        ),
                        observation=observation,
                    )
                )

    status = _metadata_status(plugin_scope_status, had_problem)
    return occurrences, findings, errors, _metadata_state("mcp", status, source_refs, observed_sources)


def _scan_codex_cli_metadata(
    cli_link: Path,
    expected_target: Path,
    home: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    occurrences: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    source_refs = {_display_path(cli_link, home), _display_path(expected_target, home)}
    observed_sources = 0
    status = "missing"

    try:
        launcher_parent_fd = _open_directory_beneath(home, cli_link.parent)
    except FileNotFoundError:
        return occurrences, findings, errors, _metadata_state("cli", status, source_refs, observed_sources)
    except OSError as exc:
        _metadata_problem(
            findings,
            errors,
            error_code="metadata_source_rejected",
            path=cli_link,
            home=home,
            message=str(exc),
        )
        return occurrences, findings, errors, _metadata_state("cli", "error", source_refs, observed_sources)

    try:
        try:
            link_info = os.stat(
                cli_link.name, dir_fd=launcher_parent_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            return occurrences, findings, errors, _metadata_state(
                "cli", status, source_refs, observed_sources
            )
        if not stat.S_ISLNK(link_info.st_mode):
            _metadata_problem(
                findings,
                errors,
                error_code="metadata_source_rejected",
                path=cli_link,
                home=home,
                message="Codex CLI source must be the fixed launcher symlink",
            )
            return occurrences, findings, errors, _metadata_state(
                "cli", "partial", source_refs, observed_sources
            )
        raw_target = os.readlink(cli_link.name, dir_fd=launcher_parent_fd)
    except OSError as exc:
        _metadata_problem(
            findings,
            errors,
            error_code="metadata_source_rejected",
            path=cli_link,
            home=home,
            message=str(exc),
        )
        return occurrences, findings, errors, _metadata_state(
            "cli", "partial", source_refs, observed_sources
        )
    finally:
        os.close(launcher_parent_fd)

    try:
        lexical_target = Path(os.path.abspath(cli_link.parent / raw_target))
        allowed_target = Path(os.path.abspath(expected_target))
    except (OSError, ValueError) as exc:
        _metadata_problem(
            findings,
            errors,
            error_code="metadata_source_rejected",
            path=cli_link,
            home=home,
            message=str(exc),
        )
        return occurrences, findings, errors, _metadata_state("cli", "partial", source_refs, observed_sources)

    if lexical_target != allowed_target:
        _metadata_problem(
            findings,
            errors,
            error_code="metadata_source_outside_allowlist",
            path=cli_link,
            home=home,
            message="Codex CLI launcher target is outside the one allowed target",
        )
        return occurrences, findings, errors, _metadata_state("cli", "partial", source_refs, observed_sources)

    try:
        target_parent_fd = _open_directory_beneath(home, allowed_target.parent)
        try:
            target_info = os.stat(
                allowed_target.name,
                dir_fd=target_parent_fd,
                follow_symlinks=False,
            )
        finally:
            os.close(target_parent_fd)
    except OSError as exc:
        _metadata_problem(
            findings,
            errors,
            error_code="metadata_source_missing",
            path=allowed_target,
            home=home,
            message=str(exc),
        )
        return occurrences, findings, errors, _metadata_state("cli", "partial", source_refs, observed_sources)
    executable_mask = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    if (
        stat.S_ISLNK(target_info.st_mode)
        or not stat.S_ISREG(target_info.st_mode)
        or not target_info.st_mode & executable_mask
    ):
        _metadata_problem(
            findings,
            errors,
            error_code="metadata_source_rejected",
            path=allowed_target,
            home=home,
            message="Codex CLI target must be a regular executable file",
        )
        return occurrences, findings, errors, _metadata_state("cli", "partial", source_refs, observed_sources)

    observed_sources = 1
    status = "observed"
    observation = {
        "adapter_id": CODEX_METADATA_ADAPTERS["cli"],
        "basis": "filesystem_metadata_projection",
        "plugin_channel": None,
        "plugin_name": None,
        "plugin_version": None,
        "server_id": None,
        "transport_kind": None,
        "sensitive_config_present": False,
        "cli_name": "codex",
        "sdk_package_name": None,
    }
    metadata_description = (
        "Codex command-line launcher observed from a fixed symlink and allowlisted target metadata; "
        "the executable body was not read or executed."
    )
    source_ref = _display_path(cli_link, home)
    projection_observation = dict(observation)
    projection_observation["file_size"] = int(target_info.st_size)
    projection_observation["file_mode"] = int(stat.S_IMODE(target_info.st_mode))
    projection_observation["modified_ns"] = int(target_info.st_mtime_ns)
    occurrence = _metadata_occurrence(
        asset_type="cli",
        name="codex",
        description=metadata_description,
        version="",
        author="",
        license_name="",
        display_name="Codex CLI",
        source_ref=source_ref,
        binding=_metadata_binding(
            "codex",
            cli_link,
            home,
            presence="present",
            provisioning="unmanaged_symlink",
            link_target=allowed_target,
        ),
        observation=observation,
    )
    occurrence["fingerprint"] = _metadata_fingerprint(
        {
            "type": "cli",
            "name": "codex",
            "source_ref": source_ref,
            "target": _display_path(allowed_target, home),
            "observation": projection_observation,
        }
    )
    occurrence["manifest_fingerprint"] = occurrence["fingerprint"]
    occurrences.append(occurrence)
    return occurrences, findings, errors, _metadata_state("cli", status, source_refs, observed_sources)


def _sdk_manifest_path(package_root: Path, package_name: str) -> Path:
    return package_root / "node_modules" / Path(*package_name.split("/")) / "package.json"


def _scan_codex_sdk_metadata(
    packages: list[dict[str, Any]],
    plugin_root: Path,
    plugin_scope_status: str,
    home: Path,
    sdk_allowlist: frozenset[str],
    limits: dict[str, Any],
    deadline: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    occurrences: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    source_refs = {_display_path(plugin_root, home)}
    observed_sources = 0
    had_problem = False

    if plugin_scope_status not in {"missing", "error", "blocked_symlink_root", "not_directory"}:
        for package in packages:
            if time.monotonic() > deadline:
                had_problem = True
                _metadata_problem(
                    findings,
                    errors,
                    error_code="metadata_scan_budget_exceeded",
                    path=plugin_root,
                    home=home,
                    message="Codex SDK metadata observation exceeded its read budget",
                )
                break
            package_manifest = package["root"] / "package.json"
            try:
                package_json = _load_metadata_json_at(
                    package["directory_fd"],
                    ("package.json",),
                    int(limits["max_file_bytes"]),
                )
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as exc:
                source_refs.add(_display_path(package_manifest, home))
                had_problem = True
                _metadata_problem(
                    findings,
                    errors,
                    error_code="metadata_source_rejected",
                    path=package_manifest,
                    home=home,
                    message=str(exc),
                )
                continue
            source_refs.add(_display_path(package_manifest, home))
            if not isinstance(package_json, dict):
                had_problem = True
                continue
            declared: set[str] = set()
            for field in ("dependencies", "peerDependencies", "optionalDependencies"):
                values = package_json.get(field)
                if isinstance(values, dict):
                    declared.update(name for name in values if name in sdk_allowlist)
            for package_name in sorted(declared):
                sdk_manifest = _sdk_manifest_path(package["root"], package_name)
                try:
                    sdk_json = _load_metadata_json_at(
                        package["directory_fd"],
                        (
                            "node_modules",
                            "@modelcontextprotocol",
                            "sdk",
                            "package.json",
                        ),
                        int(limits["max_file_bytes"]),
                    )
                except (OSError, ValueError) as exc:
                    source_refs.add(_display_path(sdk_manifest, home))
                    had_problem = True
                    _metadata_problem(
                        findings,
                        errors,
                        error_code="metadata_source_rejected",
                        path=sdk_manifest,
                        home=home,
                        message=str(exc),
                    )
                    continue
                source_refs.add(_display_path(sdk_manifest, home))
                if not isinstance(sdk_json, dict) or sdk_json.get("name") != package_name:
                    had_problem = True
                    _metadata_problem(
                        findings,
                        errors,
                        error_code="metadata_source_rejected",
                        path=sdk_manifest,
                        home=home,
                        message="Cached SDK package name does not match the strict allowlist declaration",
                    )
                    continue
                observed_sources += 1
                version = _safe_text(sdk_json.get("version"), 200)
                description = _safe_text(sdk_json.get("description"), 4000)
                license_name = _safe_text(sdk_json.get("license"), 500)
                source_ref = _display_path(sdk_manifest, home)
                observation = {
                    "adapter_id": CODEX_METADATA_ADAPTERS["sdk"],
                    "basis": "dependency_manifest_projection",
                    "plugin_channel": package["channel"] or None,
                    "plugin_name": package["name"] or None,
                    "plugin_version": package["version"] or None,
                    "server_id": None,
                    "transport_kind": None,
                    "sensitive_config_present": False,
                    "cli_name": None,
                    "sdk_package_name": package_name,
                }
                occurrences.append(
                    _metadata_occurrence(
                        asset_type="sdk",
                        name=package_name,
                        description=description,
                        version=version,
                        author="",
                        license_name=license_name,
                        display_name="",
                        source_ref=source_ref,
                        binding=_metadata_binding(
                            "codex", sdk_manifest, home, presence="cached", provisioning="cache"
                        ),
                        observation=observation,
                    )
                )

    status = _metadata_status(plugin_scope_status, had_problem)
    return occurrences, findings, errors, _metadata_state("sdk", status, source_refs, observed_sources)


def _scan_codex_extension_metadata(
    packages: list[dict[str, Any]],
    source_occurrences: list[dict[str, Any]],
    plugin_root: Path,
    plugin_scope_status: str,
    home: Path,
    metadata_config: dict[str, Any],
    limits: dict[str, Any],
    deadline: float,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
]:
    """Observe all three Codex extensions and always release package handles."""

    found: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    states: dict[str, dict[str, Any]] = {}
    try:
        result = _scan_codex_mcp_metadata(
            packages,
            plugin_root,
            plugin_scope_status,
            home,
            metadata_config["mcp_manifest_name"],
            limits,
            deadline,
        )
        mcp_found, mcp_findings, mcp_errors, states["mcp"] = result
        found.extend(mcp_found)
        findings.extend(mcp_findings)
        errors.extend(mcp_errors)

        cli_link = Path(metadata_config["cli_link"].replace("{home}", str(home), 1))
        cli_target = Path(
            metadata_config["cli_target"].replace("{home}", str(home), 1)
        )
        result = _scan_codex_cli_metadata(cli_link, cli_target, home)
        cli_found, cli_findings, cli_errors, states["cli"] = result
        found.extend(cli_found)
        findings.extend(cli_findings)
        errors.extend(cli_errors)

        result = _scan_codex_sdk_metadata(
            packages,
            plugin_root,
            plugin_scope_status,
            home,
            SDK_PACKAGE_ALLOWLIST,
            limits,
            deadline,
        )
        sdk_found, sdk_findings, sdk_errors, states["sdk"] = result
        found.extend(sdk_found)
        findings.extend(sdk_findings)
        errors.extend(sdk_errors)
        return found, findings, errors, states
    finally:
        closed: set[int] = set()
        for occurrence in source_occurrences:
            package_fd = occurrence.pop("metadata_package_fd", None)
            if isinstance(package_fd, int) and package_fd not in closed:
                os.close(package_fd)
                closed.add(package_fd)


def _scan_root(
    host_id: str,
    asset_type: str,
    root: Path,
    home: Path,
    allowed_roots: tuple[Path, ...],
    limits: dict[str, Any],
    deadline: float,
    skill_provenance: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    occurrences: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    scope = {
        "host_id": host_id,
        "asset_type": asset_type,
        "path": _display_path(root, home),
        "status": "missing",
        "entries_examined": 0,
        "manifests_read": 0,
        "transient_entries_excluded": 0,
        "depth_limited_entries": 0,
    }

    try:
        if _path_chain_contains_symlink(root, home):
            scope["status"] = "blocked_symlink_root"
            findings.append(
                _finding(
                    "symlink_root_blocked",
                    "warning",
                    "扫描根路径含软链接，已阻止",
                    "为避免根目录逃逸，扫描器不会跟随宿主根或其祖先链中的软链接。",
                    host_id=host_id,
                    path=scope["path"],
                )
            )
            return occurrences, findings, errors, scope
        if not root.exists():
            return occurrences, findings, errors, scope
        if not root.is_dir():
            scope["status"] = "not_directory"
            findings.append(
                _finding(
                    "root_not_directory",
                    "warning",
                    "扫描根不是目录",
                    "该路径未作为宿主能力目录读取。",
                    host_id=host_id,
                    path=scope["path"],
                )
            )
            return occurrences, findings, errors, scope

        scope["status"] = "observed"
        queue: list[tuple[Path, int]] = [(root, 0)]
        manifest_name = SAFE_MANIFESTS[asset_type]
        max_depth = int(limits["max_depth"])
        max_entries = int(limits["max_entries_per_root"])

        while queue:
            if time.monotonic() > deadline:
                raise ScanBudgetExceeded("scan timeout reached")
            directory, depth = queue.pop(0)
            if _path_chain_contains_symlink(directory, root):
                errors.append(
                    {
                        "code": "directory_symlink_chain_blocked",
                        "host_id": host_id,
                        "path": _display_path(directory, home),
                        "message": "directory symlink chain changed during observation",
                    }
                )
                scope["status"] = "partial"
                continue
            try:
                entries: list[tuple[str, os.stat_result, str | None]] = []
                remaining = max_entries - scope["entries_examined"]
                directory_fd = _open_directory_beneath(home, directory)
                try:
                    with os.scandir(directory_fd) as iterator:
                        for entry in iterator:
                            if time.monotonic() > deadline:
                                raise ScanBudgetExceeded("scan timeout reached")
                            if len(entries) >= remaining:
                                raise ScanBudgetExceeded("entry budget reached")
                            info = os.stat(
                                entry.name,
                                dir_fd=directory_fd,
                                follow_symlinks=False,
                            )
                            raw_link = (
                                os.readlink(entry.name, dir_fd=directory_fd)
                                if stat.S_ISLNK(info.st_mode)
                                else None
                            )
                            entries.append((entry.name, info, raw_link))
                finally:
                    os.close(directory_fd)
                entries.sort(key=lambda item: item[0].casefold())
            except (OSError, PermissionError, ValueError) as exc:
                errors.append(
                    {
                        "code": "directory_unreadable",
                        "host_id": host_id,
                        "path": _display_path(directory, home),
                        "message": _safe_text(exc, 500),
                    }
                )
                scope["status"] = "partial"
                continue

            for entry_name, entry_info, raw_link in entries:
                if time.monotonic() > deadline:
                    raise ScanBudgetExceeded("scan timeout reached")
                scope["entries_examined"] += 1
                if scope["entries_examined"] > max_entries:
                    raise ScanBudgetExceeded("entry budget reached")
                if entry_name in IGNORED_NAMES:
                    continue
                if depth == 0 and entry_name in TRANSIENT_SYSTEM_ROOTS.get((host_id, asset_type), frozenset()):
                    scope["transient_entries_excluded"] += 1
                    continue

                path = directory / entry_name
                is_link = stat.S_ISLNK(entry_info.st_mode)

                if is_link:
                    try:
                        if raw_link is None:
                            raise ValueError("symlink target metadata is unavailable")
                        target = (
                            Path(raw_link)
                            if Path(raw_link).is_absolute()
                            else path.parent / raw_link
                        ).resolve(strict=False)
                    except (OSError, RuntimeError) as exc:
                        findings.append(
                            _finding(
                                "symlink_unresolvable",
                                "warning",
                                "软链接无法解析",
                                "该链接未被跟随或计入可用资产。",
                                host_id=host_id,
                                path=_display_path(path, home),
                            )
                        )
                        errors.append(
                            {
                                "code": "symlink_unresolvable",
                                "host_id": host_id,
                                "path": _display_path(path, home),
                                "message": _safe_text(exc, 500),
                            }
                        )
                        continue
                    if not _within_any(target, allowed_roots):
                        findings.append(
                            _finding(
                                "symlink_outside_allowlist",
                                "warning",
                                "软链接越出允许范围",
                                "扫描器只记录问题，不跟随该链接。",
                                host_id=host_id,
                                path=_display_path(path, home),
                            )
                        )
                        continue
                    if not target.exists():
                        findings.append(
                            _finding(
                                "symlink_broken",
                                "warning",
                                "发现断开的软链接",
                                "链接目标不存在，未计入可用资产。",
                                host_id=host_id,
                                path=_display_path(path, home),
                            )
                        )
                        continue

                    manifest = target / manifest_name if target.is_dir() else target
                    if manifest.name != manifest_name or not manifest.exists() or manifest.is_symlink():
                        continue
                    try:
                        parsed = (
                            _parse_skill_manifest(manifest, home, limits)
                            if asset_type == "skill"
                            else _parse_plugin_manifest(manifest, home, limits)
                        )
                    except (OSError, ValueError) as exc:
                        errors.append(
                            {
                                "code": "manifest_rejected",
                                "host_id": host_id,
                                "path": _display_path(manifest, home),
                                "message": _safe_text(exc, 500),
                            }
                        )
                        scope["status"] = "partial"
                        continue
                    if asset_type == "skill":
                        parsed["origin_kind"], parsed["origin_basis"] = _skill_origin(
                            host_id,
                            manifest,
                            parsed,
                            home,
                            limits,
                            skill_provenance,
                        )
                    else:
                        parsed["origin_kind"], parsed["origin_basis"] = "unknown", "unavailable"
                    package_issue = _attach_package_identity(
                        parsed, manifest, home, limits, deadline
                    )
                    if package_issue:
                        findings.append(
                            _finding(
                                "package_fingerprint_incomplete",
                                "warning",
                                "能力包指纹不完整",
                                "该资产仍可查看，但不会参与精确去重，也不能进入受控开启。",
                                host_id=host_id,
                                path=_display_path(manifest.parent, home),
                                reason=package_issue,
                            )
                        )
                    scope["manifests_read"] += 1
                    parsed.update(
                        {
                            "manifest_path": manifest,
                            "source_ref": _display_path(manifest, home),
                            "binding": {
                                **_binding(host_id, asset_type, path, home, True, target),
                                "package_fingerprint": (
                                    parsed["fingerprint"]
                                    if parsed["package_fingerprint_status"] == "complete"
                                    else None
                                ),
                                "package_fingerprint_status": parsed["package_fingerprint_status"],
                            },
                        }
                    )
                    occurrences.append(parsed)
                    continue

                if stat.S_ISDIR(entry_info.st_mode):
                    if depth < max_depth:
                        queue.append((path, depth + 1))
                    else:
                        scope["depth_limited_entries"] += 1
                    continue
                if not stat.S_ISREG(entry_info.st_mode) or entry_name != manifest_name:
                    continue

                try:
                    parsed = (
                        _parse_skill_manifest(path, home, limits)
                        if asset_type == "skill"
                        else _parse_plugin_manifest(path, home, limits)
                    )
                except (OSError, ValueError) as exc:
                    errors.append(
                        {
                            "code": "manifest_rejected",
                            "host_id": host_id,
                            "path": _display_path(path, home),
                            "message": _safe_text(exc, 500),
                        }
                    )
                    scope["status"] = "partial"
                    continue
                if asset_type == "skill":
                    parsed["origin_kind"], parsed["origin_basis"] = _skill_origin(
                        host_id,
                        path,
                        parsed,
                        home,
                        limits,
                        skill_provenance,
                    )
                else:
                    parsed["origin_kind"], parsed["origin_basis"] = "unknown", "unavailable"
                package_issue = _attach_package_identity(
                    parsed, path, home, limits, deadline
                )
                if package_issue:
                    findings.append(
                        _finding(
                            "package_fingerprint_incomplete",
                            "warning",
                            "能力包指纹不完整",
                            "该资产仍可查看，但不会参与精确去重，也不能进入受控开启。",
                            host_id=host_id,
                            path=_display_path(path.parent, home),
                            reason=package_issue,
                        )
                    )
                scope["manifests_read"] += 1
                parsed.update(
                    {
                        "manifest_path": path,
                        "source_ref": _display_path(path, home),
                        "binding": {
                            **_binding(host_id, asset_type, path.parent, home, False, None),
                            "package_fingerprint": (
                                parsed["fingerprint"]
                                if parsed["package_fingerprint_status"] == "complete"
                                else None
                            ),
                            "package_fingerprint_status": parsed["package_fingerprint_status"],
                        },
                    }
                )
                occurrences.append(parsed)
    except ScanBudgetExceeded as exc:
        scope["status"] = "partial"
        errors.append(
            {
                "code": "scan_budget_exceeded",
                "host_id": host_id,
                "path": scope["path"],
                "message": str(exc),
            }
        )
        findings.append(
            _finding(
                "scan_incomplete",
                "warning",
                "扫描预算已用完",
                "该宿主结果不完整；旧状态不能据此推断为已移除。",
                host_id=host_id,
                path=scope["path"],
            )
        )
    except (OSError, PermissionError) as exc:
        scope["status"] = "error"
        errors.append(
            {
                "code": "root_scan_failed",
                "host_id": host_id,
                "path": scope["path"],
                "message": _safe_text(exc, 500),
            }
        )

    return occurrences, findings, errors, scope


def _slug(name: str) -> str:
    slug = SLUG_CHARS.sub("-", name.casefold()).strip("-")
    return slug[:100] or "unnamed"


def _classify_asset(asset_type: str, name: str, description: str) -> str:
    if asset_type != "skill":
        return {"plugin": "插件", "mcp": "连接", "cli": "命令行", "sdk": "开发依赖"}.get(asset_type, "其他")
    haystack = f"{name} {description}".casefold()
    rules = (
        ("安全与审查", ("security", "threat", "attack", "vulnerab", "taint", "audit", "review", "vetter")),
        ("产品与视觉", ("design", "figma", "canva", "image", "visual", "canvas", "accessibility", "animation", "storyboard")),
        ("网页与调研", ("browser", "chrome", "web", "research", "crawl", "search", "reach", "wiki", "firecrawl")),
        ("数据与分析", ("data", "analytics", "spreadsheet", "excel", "metric", "kpi", "airtable", "report", "market")),
        ("文档与沟通", ("document", "docx", "pdf", "slide", "presentation", "email", "drive", "recap", "communication")),
        ("开发与代码", ("code", "git", "github", "debug", "refactor", "commit", "circleci", "frontend", "api", "sdk", "supabase")),
        ("创意生产", ("creative", "video", "audio", "story", "hyperframe", "cowart", "libtv")),
        ("协作与管理", ("agent", "handoff", "workflow", "maestro", "project", "session", "memory", "parallel", "dispatch")),
    )
    for label, keywords in rules:
        if any(keyword in haystack for keyword in keywords):
            return label
    return "其他"


def _classify_subcategory(
    asset_type: str,
    name: str,
    description: str,
    _category: str,
) -> str | None:
    """Add a task-shaped second level without changing the broad category.

    The creative labels and their precedence mirror the candidate Skill
    workbench taxonomy: specialised Seedance cases must be tested before the
    generic Seedance rule, and reverse/extraction remains a distinct task.
    The rest of the rules give installed Skills and tools an equally concrete
    scenario label.  Unknown future assets keep a category-specific fallback
    instead of being silently forced into an unrelated product family.
    """

    source_name = name.casefold()
    details = description.casefold()
    haystack = f"{source_name} {details}"

    def name_has(*keywords: str) -> bool:
        return any(keyword in source_name for keyword in keywords)

    def text_has(*keywords: str) -> bool:
        return any(keyword in haystack for keyword in keywords)

    if asset_type == "skill":
        # Candidate-library scene taxonomy.  Keep the specific-before-general
        # order from collection-workbench/rules.json.
        if source_name.startswith("webnovel"):
            return "网文全家桶"
        if name_has(
            "whitebox", "白模", "continuity", "连续性", "motion-camera", "motion camera", "rescue", "救片"
        ):
            return "Seedance 提示词 · 特化场景"
        if name_has("seedance", "即梦"):
            return "Seedance 提示词 · 通用型"
        if name_has(
            "reverse", "反推", "extractor", "extract", "提取", "reconstruction", "distiller", "reference-prompt"
        ) or any(
            keyword in details
            for keyword in (
                "反推", "倒推", "reverse", "参考图复现", "看图提取", "提取提示词", "visual treatment", "把可见结构"
            )
        ):
            return "反推与提示词提取"
        if name_has("score-and-mix", "mixing", "混音", "配乐", "sound-design"):
            return "配乐、音效与混音"
        if name_has("color", "色卡", "upscale", "4k", "material", "材质", "画质"):
            return "色彩 · 材质 · 画质"
        if name_has("portrait", "selfie", "midjourney", "写真", "人像", "zhuangbi"):
            return "人像与社媒生图"
        if name_has("cinema", "cinematic", "fusion", "realism", "multiview", "visual-asset", "视觉资产", "九宫格"):
            return "电影感生图与视觉资产"
        if name_has(
            "skill-forge", "skill-recaster", "writing-great-skills", "distillation", "skill-creator", "skill-factory", "capability-forge", "write-agentmemory-skill", "hermes-agent-skill-authoring"
        ):
            return "造技能的元技能"
        if name_has("tvc", "viral", "广告", "爆款", "impact-director"):
            return "广告与爆款分析"
        if name_has("methodology", "方法论"):
            return "写作方法论"
        if name_has("screenwriting", "screenplay", "script", "drama", "编剧", "剧本", "bianju", "content-pipeline", "短剧"):
            return "编剧与剧本"

        if source_name.startswith("agentmemory-") or source_name in {"recall", "remember", "forget"}:
            return "AgentMemory 与长期记忆"
        if source_name.startswith("gitnexus-") or source_name == "graphify":
            return "GitNexus 代码图谱"
        if source_name.startswith("github-") or source_name in {
            "commit-context",
            "commit-history",
            "receiving-code-review",
            "requesting-code-review",
            "finishing-a-development-branch",
        }:
            return "GitHub 与代码协作"
        if "hermes" in source_name or source_name in {"petdex", "tui-widgets"}:
            return "Hermes 生态"
        if name_has("debug", "diagnos", "refactor", "simplify-code", "codebase-inspection", "spike"):
            return "调试、诊断与重构"
        if source_name in {
            "agent-team-orchestration",
            "dispatching-parallel-agents",
            "compose",
            "amplify",
            "calibrate",
            "executing-plans",
            "multi-perspective-review",
            "model-benchmarking",
            "plan",
            "evaluate",
        }:
            return "Agent 协作与执行方法"
        if source_name in {"skill-vetter", "agent-skill-installation", "find-skills"}:
            return "Skill 发现、安装与治理"
        if name_has("llama", "vllm", "huggingface", "evaluating-llms", "model-benchmark", "weights-and-biases"):
            return "模型运行、训练与评测"
        if name_has("research-paper", "arxiv"):
            return "学术检索与论文"
        if name_has("docx", "document", "pdf", "powerpoint", "presentation", "openxml", "ocr"):
            return "文档、PDF 与演示"
        if name_has("airtable", "spreadsheet", "excel", "xlsx", "jupyter", "notion", "obsidian"):
            return "表格、数据库与知识库"
        if name_has(
            "frontend", "web-animation", "popular-web-design", "design-md", "claude-design", "apple-design", "sketch", "p5js", "ascii-art", "excalidraw", "infographic", "pretext", "architecture-diagram"
        ):
            return "界面设计、图表与动效"
        if name_has("image", "comfyui", "segment-anything", "visual", "cowart", "folder-snapshot"):
            return "图像生成、编辑与视觉资产"
        if name_has("video", "storyboard", "libtv", "manim", "hyperframe", "touchdesigner"):
            return "视频、动画与分镜"
        if name_has("audio", "song", "music", "heartmula", "songsee"):
            return "音频、音乐与歌曲"
        if name_has("browser", "chrome", "web", "youtube", "blogwatcher", "xurl", "gif-search", "wiki"):
            return "网页检索与知识获取"
        if name_has("email", "gmail", "himalaya", "imessage", "teams-meeting", "yuanbao", "google-workspace"):
            return "邮件、消息与会议"
        if name_has("apple-", "findmy", "computer-use", "openhue"):
            return "桌面与 macOS 自动化"
        if source_name in {"codex", "claude-code", "opencode", "antigravity-cli"}:
            return "编码委派与模型 CLI"
        if source_name in {"chronicle", "recap", "session-history", "handoff"}:
            return "会话历史与交接"
        if source_name in {"humanizer"}:
            return "写作润色与表达"
        if source_name in {"media-delivery-and-attachments"}:
            return "文件与媒体交付"
        if source_name in {"chain"}:
            return "工具链与工作流设计"
        if source_name in {"maps", "polymarket"}:
            return "地图、市场与行业数据"
        if source_name in {"dogfood", "test-driven-development"}:
            return "测试与产品验收"
        if source_name == "grounded-citations":
            return "来源核验与引用"
        if source_name == "hatch-pet":
            return "视频、动画与分镜"
        if source_name in {"workbuddy", "workbuddy-troubleshooting"}:
            return "宿主连接与故障排查"

        return None

    # Plugin/MCP/CLI/SDK keep their type as the broad class and use this field
    # for the concrete job the tool family supports.
    if text_has("browser", "chrome"):
        return "浏览器自动化"
    if text_has("computer-use"):
        return "桌面自动化"
    if text_has("codex-security", "security scan"):
        return "安全扫描与审查"
    if text_has("airtable", "data-analytics", "dataanalytics", "spreadsheet", "carta", "skywatch"):
        return "数据、表格与业务分析"
    if text_has("document", "google drive", "google-drive", "pdf", "presentation", "template", "gmail"):
        return "文档、演示与内容协作"
    if text_has("adobe", "canva", "cowart", "creative-production", "creative_production", "figma", "product-design", "visualize"):
        return "视觉设计与创意生产"
    if text_has("storyboard", "hyperframe"):
        return "视频、动画与分镜"
    if text_has("build-web-apps", "sites"):
        return "Web 应用与站点构建"
    if text_has("circleci", "github"):
        return "代码托管与持续集成"
    if text_has("openai-developers", "openai api", "agents sdk", "chatgpt apps", "openai-api-key"):
        return "OpenAI 开发"
    if asset_type == "mcp":
        return "MCP 连接接口"
    if asset_type == "cli":
        return "命令行入口"
    if asset_type == "sdk":
        return "开发依赖与 SDK"
    return None


def _load_overlay(
    overlay_path: Path,
    limits: dict[str, Any],
    errors: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not overlay_path.exists():
        errors.append(
            {
                "code": "overlay_missing",
                "path": overlay_path.as_posix(),
                "message": "Chinese metadata overlay does not exist",
            }
        )
        return {}
    try:
        parsed = _load_trusted_json(
            overlay_path,
            int(limits.get("max_overlay_bytes", limits["max_file_bytes"])),
        )
    except (
        OSError,
        ValueError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
    ) as exc:
        errors.append(
            {
                "code": "overlay_invalid",
                "path": overlay_path.as_posix(),
                "message": _safe_text(exc, 500),
            }
        )
        return {}
    if not isinstance(parsed, dict) or parsed.get("schema_version") != 1 or not isinstance(parsed.get("items"), list):
        errors.append(
            {
                "code": "overlay_invalid",
                "path": overlay_path.as_posix(),
                "message": "Overlay must contain schema_version=1 and an items array",
            }
        )
        return {}
    by_id: dict[str, dict[str, Any]] = {}
    allowed_status = {"ai_draft", "reviewed", "stale"}

    def string_list(value: Any, max_length: int, *, max_items: int | None = None) -> list[str]:
        if not isinstance(value, list):
            return []
        values = value if max_items is None else value[:max_items]
        return [text for item in values if (text := _safe_text(item, max_length))]

    for raw in parsed["items"]:
        if not isinstance(raw, dict):
            continue
        asset_id = _safe_text(raw.get("asset_id"), 240)
        status = _safe_text(raw.get("status"), 40)
        translated_hash = _safe_text(raw.get("translated_from_hash"), 64)
        if not asset_id or status not in allowed_status or not re.fullmatch(r"[a-f0-9]{64}", translated_hash):
            continue
        entry = {
            "asset_id": asset_id,
            "zh_name": _safe_text(raw.get("zh_name"), 120),
            "source_name": _safe_text(raw.get("source_name"), 200),
            "summary": _safe_text(raw.get("summary"), 1000),
            "use_cases": string_list(raw.get("use_cases"), 500),
            "not_for": string_list(raw.get("not_for"), 500),
            "examples": string_list(raw.get("examples"), 500, max_items=3),
            "synonyms": string_list(raw.get("synonyms"), 120),
            "source_ref": _safe_text(raw.get("source_ref"), 1000),
            "translated_from_hash": translated_hash,
            "status": status,
            "generated_at": _safe_text(raw.get("generated_at"), 100),
            "reviewed_at": _safe_text(raw.get("reviewed_at"), 100) or None,
            "evidence": string_list(raw.get("evidence"), 1000),
        }
        entry["coverage_complete"] = bool(
            entry["zh_name"]
            and entry["summary"]
            and entry["use_cases"]
            and entry["not_for"]
            and entry["examples"]
            and entry["source_name"]
            and entry["source_ref"]
            and entry["generated_at"]
        )
        if not entry["coverage_complete"]:
            continue
        by_id[asset_id] = entry
    return by_id


def _static_occurrences(config: dict[str, Any], limits: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    max_text = int(limits["max_text_length"])
    for raw in config.get("static_assets", []):
        if not isinstance(raw, dict) or raw.get("type") not in ASSET_TYPES:
            continue
        safe = {key: _safe_text(raw.get(key), max_text) for key in SAFE_STATIC_FIELDS}
        if not safe.get("name"):
            continue
        canonical = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        static_fingerprint = hashlib.sha256(canonical).hexdigest()
        bindings: list[dict[str, Any]] = []
        for host_id in raw.get("hosts", []):
            if host_id in HOST_ALLOWLIST:
                bindings.append(
                    {
                        "host_id": host_id,
                        "presence": "unknown",
                        "provisioning": "configured" if safe["type"] == "mcp" else "none",
                        "link_health": "na",
                        "activation": "unknown",
                        "effective": "unverified",
                        "action": "locked",
                        "locked_reason": "safe_metadata_only",
                        "observed_path": None,
                        "link_target": None,
                        "package_fingerprint": static_fingerprint,
                        "package_fingerprint_status": "complete",
                    }
                )
        output.append(
            {
                "type": safe["type"],
                "name": safe["name"],
                "description": safe.get("description", ""),
                "version": safe.get("version", ""),
                "author": safe.get("author", ""),
                "license": safe.get("license", ""),
                "fingerprint": static_fingerprint,
                "manifest_fingerprint": static_fingerprint,
                "package_fingerprint_status": "complete",
                "package_file_count": 0,
                "package_bytes": 0,
                "composition": [],
                "source_ref": safe.get("source_ref") or "registry:static_assets",
                "manifest_path": None,
                "bindings": bindings,
            }
        )
    return output


def _group_skill_origin(group: list[dict[str, Any]]) -> tuple[str, str]:
    known = {
        (item.get("origin_kind"), item.get("origin_basis"))
        for item in group
        if item.get("origin_kind") in SKILL_ORIGIN_KINDS - {"unknown"}
        and item.get("origin_basis") in SKILL_ORIGIN_BASES
    }
    kinds = {kind for kind, _basis in known}
    if not kinds:
        return "unknown", "unavailable"
    if len(kinds) > 1:
        return "unknown", "conflicting_evidence"
    basis_order = {
        "hermes_skill_registry": 0,
        "hermes_bundled_manifest": 1,
        "workbuddy_skill_metadata": 2,
        "frontmatter": 3,
        "download_metadata": 4,
    }
    kind = next(iter(kinds))
    basis = min(
        (basis for evidence_kind, basis in known if evidence_kind == kind),
        key=lambda value: (basis_order.get(value, 99), value),
    )
    return kind, basis


def _build_items(
    occurrences: list[dict[str, Any]],
    overlay: dict[str, dict[str, Any]],
    findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    logical_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for occurrence in occurrences:
        # SKILL.md/plugin.json defines the logical capability and localization
        # identity.  Whole-package digests stay on each occurrence/binding so
        # host adapter files can diverge without creating duplicate cards.
        logical_groups[(occurrence["type"], occurrence["manifest_fingerprint"])].append(occurrence)

    grouped: list[dict[str, Any]] = []
    for (asset_type, manifest_fingerprint), group in logical_groups.items():
        primary = sorted(group, key=lambda item: (item["name"].casefold(), item["source_ref"]))[0]
        origin_kind, origin_basis = _group_skill_origin(group)
        bindings: list[dict[str, Any]] = []
        for item in group:
            if "binding" in item:
                bindings.append(item["binding"])
            bindings.extend(item.get("bindings", []))
        unique_bindings: list[dict[str, Any]] = []
        seen_bindings: set[tuple[str, str]] = set()
        for binding in sorted(bindings, key=lambda value: (value["host_id"], value.get("observed_path") or "")):
            key = (binding["host_id"], binding.get("observed_path") or "")
            if key not in seen_bindings:
                seen_bindings.add(key)
                unique_bindings.append(binding)
        package_variants: list[dict[str, Any]] = []
        seen_variants: set[tuple[str, str, str, str, str]] = set()
        for occurrence in group:
            occurrence_bindings = []
            if "binding" in occurrence:
                occurrence_bindings.append(occurrence["binding"])
            occurrence_bindings.extend(occurrence.get("bindings", []))
            if not occurrence_bindings:
                occurrence_bindings.append({"host_id": "", "observed_path": None})
            package_status = occurrence.get("package_fingerprint_status", "incomplete")
            package_digest = occurrence.get("fingerprint") if package_status == "complete" else None
            for binding in occurrence_bindings:
                variant = {
                    "source_ref": occurrence["source_ref"],
                    "host_id": binding.get("host_id") or None,
                    "observed_path": binding.get("observed_path"),
                    "package_fingerprint": package_digest,
                    "package_fingerprint_status": package_status,
                }
                key = (
                    variant["source_ref"],
                    variant["host_id"] or "",
                    variant["observed_path"] or "",
                    variant["package_fingerprint"] or "",
                    variant["package_fingerprint_status"],
                )
                if key not in seen_variants:
                    seen_variants.add(key)
                    package_variants.append(variant)
        package_variants.sort(
            key=lambda value: (
                value.get("host_id") or "",
                value.get("observed_path") or "",
                value["source_ref"],
            )
        )
        package_fingerprints = sorted(
            {
                item["fingerprint"]
                for item in group
                if item.get("package_fingerprint_status") == "complete"
            }
        )
        package_incomplete = any(item.get("package_fingerprint_status") != "complete" for item in group)
        if package_incomplete:
            package_status = "incomplete"
        elif len(package_fingerprints) > 1:
            package_status = "divergent"
        else:
            package_status = "complete"
        grouped.append(
            {
                "type": asset_type,
                "fingerprint": manifest_fingerprint,
                "manifest_fingerprint": manifest_fingerprint,
                "package_fingerprint_status": package_status,
                "package_fingerprints": package_fingerprints,
                "name": primary["name"],
                "description": primary.get("description", ""),
                "version": primary.get("version", ""),
                "author": primary.get("author", ""),
                "license": primary.get("license", ""),
                "origin_kind": origin_kind,
                "origin_basis": origin_basis,
                "display_name": primary.get("display_name", ""),
                "source_ref": primary["source_ref"],
                "composition": primary.get("composition", []),
                "observation": primary.get("observation"),
                "trust_locked_reason": primary.get("trust_locked_reason"),
                "host_bindings": unique_bindings,
                "package_variants": package_variants,
                "occurrence_count": len(group),
                "source_refs": sorted({item["source_ref"] for item in group}),
            }
        )

    name_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    slug_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for group in grouped:
        name_groups[(group["type"], group["name"].casefold())].append(group)
        slug_groups[(group["type"], _slug(group["name"]))].append(group)

    # ChineseMetadata is also the persistence anchor for curated asset IDs.
    # A colliding source can change its manifest while staying at the same
    # source_ref; reusing its prior asset_id lets localization become `stale`
    # instead of silently creating a new card.  If a host binding moves but the
    # content stays byte-identical, translated_from_hash provides the fallback
    # anchor.  New, not-yet-curated collisions use a source-ref suffix and are
    # not publishable by the unified validator until metadata is added.
    resolved_asset_ids: dict[tuple[str, str], str] = {}
    assigned_asset_ids: set[str] = set()
    for group in sorted(grouped, key=lambda value: (value["type"], value["name"].casefold(), value["source_ref"])):
        identity_key = (group["type"], group["manifest_fingerprint"])
        compatible = [
            (asset_id, metadata)
            for asset_id, metadata in overlay.items()
            if asset_id.startswith(f"{group['type']}:")
            and metadata.get("source_name", "").casefold() == group["name"].casefold()
        ]
        source_matches = {
            asset_id
            for asset_id, metadata in compatible
            if metadata.get("source_ref") in group["source_refs"]
        }
        hash_matches = {
            asset_id
            for asset_id, metadata in compatible
            if metadata.get("translated_from_hash") == group["manifest_fingerprint"]
        }
        candidates = source_matches if len(source_matches) == 1 else hash_matches
        if len(candidates) == 1:
            anchored = next(iter(candidates))
            if anchored not in assigned_asset_ids:
                resolved_asset_ids[identity_key] = anchored
                assigned_asset_ids.add(anchored)
        elif source_matches or hash_matches:
            findings.append(
                _finding(
                    "asset_identity_ambiguous",
                    "warning",
                    "资产身份锚点存在冲突",
                    "同一来源匹配到多个历史资产 ID；已锁定并使用安全回退 ID，需先人工合并身份记录。",
                    path=group["source_ref"],
                )
            )

    for group in sorted(grouped, key=lambda value: (value["type"], value["name"].casefold(), value["source_ref"])):
        identity_key = (group["type"], group["manifest_fingerprint"])
        if identity_key in resolved_asset_ids:
            continue
        slug = _slug(group["name"])
        base_id = f"{group['type']}:{slug}"
        if len(slug_groups[(group["type"], slug)]) == 1 and base_id not in assigned_asset_ids:
            asset_id = base_id
        else:
            source_digest = hashlib.sha256(group["source_ref"].encode("utf-8")).hexdigest()
            asset_id = f"{base_id}:variant-{source_digest[:8]}"
            if asset_id in assigned_asset_ids:
                asset_id = f"{base_id}:variant-{source_digest[:16]}"
        resolved_asset_ids[identity_key] = asset_id
        assigned_asset_ids.add(asset_id)

    items: list[dict[str, Any]] = []
    for group in grouped:
        name_variants = name_groups[(group["type"], group["name"].casefold())]
        asset_id = resolved_asset_ids[(group["type"], group["manifest_fingerprint"])]

        localization = overlay.get(asset_id)
        if localization is None:
            localization_payload: dict[str, Any] = {
                "status": "missing",
                "coverage_complete": False,
                "translated_from_hash": None,
            }
            if group["type"] in {"skill", "plugin"}:
                findings.append(
                    _finding(
                        "localization_missing",
                        "info",
                        "能力缺少中文说明",
                        "当前仍会只读显示并保持锁定；补充中文名称、简介、适用场景和使用示意后再进入完整推荐。",
                        asset_id=asset_id,
                    )
                )
        else:
            localization_payload = dict(localization)
            if localization_payload["translated_from_hash"] != group["manifest_fingerprint"]:
                localization_payload["status"] = "stale"
                findings.append(
                    _finding(
                        "localization_stale",
                        "warning",
                        "中文说明已过期",
                        "原文指纹已经变化，需要重新核对中文说明。",
                        asset_id=asset_id,
                    )
                )
            elif not localization_payload.get("coverage_complete"):
                findings.append(
                    _finding(
                        "localization_incomplete",
                        "warning",
                        "中文说明字段不完整",
                        "当前条目不能满足技能库五项中文展示要求。",
                        asset_id=asset_id,
                    )
                )

        exact_duplicate = (
            group["occurrence_count"] > 1
            and group["package_fingerprint_status"] == "complete"
            and len(group["package_fingerprints"]) == 1
        )
        package_divergence = (
            group["occurrence_count"] > 1
            and group["package_fingerprint_status"] == "divergent"
        )
        name_collision = len(name_variants) > 1
        if exact_duplicate:
            findings.append(
                _finding(
                    "exact_duplicate_observed",
                    "info",
                    "同一资产出现在多个位置",
                    "已合并为一个资产，并保留独立宿主绑定。",
                    asset_id=asset_id,
                )
            )
        if name_collision:
            findings.append(
                _finding(
                    "name_collision",
                    "warning",
                    "发现同名不同内容",
                    "同名资产的完整能力包指纹不同，已作为变体保留并锁定。",
                    asset_id=asset_id,
                )
            )
        if package_divergence:
            findings.append(
                _finding(
                    "package_divergence",
                    "warning",
                    "同一能力的宿主包内容不同",
                    "说明文件相同，但脚本、参考资料或宿主适配文件不一致；已保留一个逻辑资产并锁定接管。",
                    asset_id=asset_id,
                    package_variants=group["package_variants"],
                )
            )

        category = _classify_asset(group["type"], group["name"], group["description"])
        subcategory = _classify_subcategory(
            group["type"],
            group["name"],
            group["description"],
            category,
        )

        items.append(
            {
                "asset_id": asset_id,
                "type": group["type"],
                "source_name": group["name"],
                "description": group["description"],
                "source": {
                    "ref": group["source_ref"],
                    "version": group["version"] or None,
                    "author": group["author"] or None,
                    "license": group["license"] or None,
                    "origin_kind": group["origin_kind"],
                    "origin_basis": group["origin_basis"],
                    "display_name": group["display_name"] or None,
                    "source_refs": group["source_refs"],
                    "manifest_fingerprint": group["manifest_fingerprint"],
                    "package_fingerprint_status": group["package_fingerprint_status"],
                    "package_fingerprints": group["package_fingerprints"],
                    "package_variants": group["package_variants"],
                    "observation": group.get("observation"),
                },
                "fingerprint": group["fingerprint"],
                "manifest_fingerprint": group["manifest_fingerprint"],
                "host_bindings": group["host_bindings"],
                "localization": localization_payload,
                "trust": {
                    "review_state": "unreviewed",
                    "license_state": "declared" if group["license"] else "unknown",
                    "risk_level": "unknown",
                    "action": "locked",
                    "locked_reason": (
                        group.get("trust_locked_reason")
                        or (
                            "read_only_inventory"
                            if group["package_fingerprint_status"] == "complete"
                            else (
                                "package_divergence"
                                if group["package_fingerprint_status"] == "divergent"
                                else "package_fingerprint_incomplete"
                            )
                        )
                    ),
                },
                "curation": {
                    "category": category,
                    "subcategory": subcategory,
                    "common": False,
                    "archived": False,
                },
                "composition": group["composition"],
                "duplicate": {
                    "exact_duplicate": exact_duplicate,
                    "occurrence_count": group["occurrence_count"],
                    "name_collision": name_collision,
                    "package_divergence": package_divergence,
                },
            }
        )

    return sorted(items, key=lambda item: (item["type"], item["source_name"].casefold(), item["asset_id"]))


def _scope_coverage_status(rows: list[dict[str, Any]]) -> str:
    statuses = {row.get("status") for row in rows}
    if statuses & {"error", "blocked_symlink_root", "not_directory"}:
        return "error"
    if "partial" in statuses:
        return "partial"
    if "observed" in statuses:
        return "observed"
    return "missing"


def _build_coverage(
    scopes: list[dict[str, Any]],
    metadata_states: dict[str, dict[str, Any]],
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    item_counts: dict[tuple[str, str], int] = defaultdict(int)
    for item in items:
        bound_hosts = {
            binding.get("host_id")
            for binding in item.get("host_bindings", [])
            if isinstance(binding, dict) and binding.get("host_id") in HOST_ALLOWLIST
        }
        for host_id in bound_hosts:
            item_counts[(host_id, item["type"])] += 1

    coverage: list[dict[str, Any]] = []
    for host_id in HOST_ALLOWLIST:
        for asset_type in ASSET_TYPE_ORDER:
            if host_id == "antigravity":
                row = {
                    "host_id": host_id,
                    "asset_type": asset_type,
                    "adapter_id": None,
                    "status": "placeholder",
                    "source_refs": [],
                    "configured_source_count": 0,
                    "observed_source_count": 0,
                    "item_count": 0,
                }
            elif asset_type in {"skill", "plugin"}:
                related = [
                    scope
                    for scope in scopes
                    if scope.get("host_id") == host_id and scope.get("asset_type") == asset_type
                ]
                if related:
                    row = {
                        "host_id": host_id,
                        "asset_type": asset_type,
                        "adapter_id": DIRECTORY_ADAPTER_ID,
                        "status": _scope_coverage_status(related),
                        "source_refs": sorted({scope["path"] for scope in related}),
                        "configured_source_count": len(related),
                        "observed_source_count": sum(
                            1 for scope in related if scope.get("status") in {"observed", "partial"}
                        ),
                        "item_count": item_counts[(host_id, asset_type)],
                    }
                else:
                    row = {
                        "host_id": host_id,
                        "asset_type": asset_type,
                        "adapter_id": None,
                        "status": "not_connected",
                        "source_refs": [],
                        "configured_source_count": 0,
                        "observed_source_count": 0,
                        "item_count": 0,
                    }
            elif host_id == "codex":
                row = dict(metadata_states[asset_type])
                row["item_count"] = item_counts[(host_id, asset_type)]
            else:
                row = {
                    "host_id": host_id,
                    "asset_type": asset_type,
                    "adapter_id": None,
                    "status": "not_connected",
                    "source_refs": [],
                    "configured_source_count": 0,
                    "observed_source_count": 0,
                    "item_count": 0,
                }
            coverage.append(row)
    return coverage


def _host_status_from_coverage(rows: list[dict[str, Any]]) -> str:
    statuses = {row["status"] for row in rows}
    if statuses == {"placeholder"}:
        return "placeholder"
    if "error" in statuses:
        return "error"
    if "partial" in statuses:
        return "partial"
    if "observed" in statuses:
        return "observed"
    return "missing"


def build_toolbox_payload(
    home: Path | None = None,
    overlay_path: Path | None = None,
    now: Any = None,
) -> dict[str, Any]:
    """Build a read-only AI-Toolbox snapshot with no filesystem side effects.

    ``home`` is explicit so callers and tests can point the scanner at a fake
    home.  When omitted it observes the current user's known roots.  It never
    creates a missing root.  ``overlay_path`` may point to a reviewed Chinese
    metadata overlay; it is only read, never modified.
    """

    if overlay_path is not None and home is None:
        raise ValueError("a custom overlay is allowed only with an explicit isolated home")
    observed_home = Path(home).expanduser().absolute() if home is not None else Path.home().absolute()
    bundle_root = _bundle_root()
    config_path = bundle_root / "registry" / "scan_config.json"
    generated_at = _iso_now(now)
    bootstrap_limits = {"max_file_bytes": 262144}
    config = _load_trusted_json(config_path, bootstrap_limits["max_file_bytes"])
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise ValueError("scan_config.json must be a schema_version=1 object")
    limits = config.get("limits")
    if not isinstance(limits, dict):
        raise ValueError("scan_config.json limits must be an object")

    required_limits = {
        "max_file_bytes": int,
        "max_overlay_bytes": int,
        "max_package_file_bytes": int,
        "max_package_bytes": int,
        "max_package_files": int,
        "max_depth": int,
        "max_entries_per_root": int,
        "scan_timeout_seconds": (int, float),
        "max_text_length": int,
    }
    for key, expected_type in required_limits.items():
        value = limits.get(key)
        if not isinstance(value, expected_type) or value <= 0:
            raise ValueError(f"invalid scan limit: {key}")
    if limits != EXPECTED_SCAN_LIMITS:
        raise ValueError("scan_config.json limits must match the frozen read budget")

    metadata_config = config.get("codex_metadata_sources")
    if not isinstance(metadata_config, dict):
        raise ValueError("scan_config.json codex_metadata_sources must be an object")
    for key, expected in EXPECTED_CODEX_METADATA_CONFIG.items():
        if metadata_config.get(key) != expected:
            raise ValueError(f"unsafe Codex metadata source configuration: {key}")
    configured_sdk_allowlist = metadata_config.get("sdk_package_allowlist")
    if (
        not isinstance(configured_sdk_allowlist, list)
        or any(not isinstance(value, str) for value in configured_sdk_allowlist)
        or frozenset(configured_sdk_allowlist) != SDK_PACKAGE_ALLOWLIST
    ):
        raise ValueError("unsafe Codex SDK package allowlist")

    if config.get("allowed_source_roots") != [] or config.get("static_assets") != []:
        raise ValueError("scan_config.json external or static sources are disabled")

    configured_hosts = config.get("hosts")
    if not isinstance(configured_hosts, list) or len(configured_hosts) != len(HOST_ALLOWLIST):
        raise ValueError("scan_config.json hosts must match the frozen host contract")
    seen_host_specs: set[str] = set()
    for raw in configured_hosts:
        if not isinstance(raw, dict):
            raise ValueError("scan_config.json host row must be an object")
        host_id = raw.get("id")
        if host_id not in HOST_ALLOWLIST or host_id in seen_host_specs:
            raise ValueError("scan_config.json host ids must be unique and allowlisted")
        seen_host_specs.add(host_id)
        expected_roots = EXPECTED_HOST_ROOTS[host_id]
        skill_roots = raw.get("skill_roots")
        plugin_roots = raw.get("plugin_roots")
        if (
            raw.get("label") != HOST_ALLOWLIST[host_id]
            or raw.get("kind") != "host"
            or not isinstance(skill_roots, list)
            or not isinstance(plugin_roots, list)
            or tuple(skill_roots) != expected_roots["skill_roots"]
            or tuple(plugin_roots) != expected_roots["plugin_roots"]
        ):
            raise ValueError(f"unsafe host root configuration: {host_id}")
    if seen_host_specs != set(HOST_ALLOWLIST):
        raise ValueError("scan_config.json hosts are incomplete")

    host_specs: list[dict[str, Any]] = []
    configured_ids: set[str] = set()
    for raw in config.get("hosts", []):
        if not isinstance(raw, dict):
            continue
        host_id = raw.get("id")
        if host_id not in HOST_ALLOWLIST or host_id in configured_ids:
            continue
        configured_ids.add(host_id)
        host_specs.append(raw)
    for host_id in HOST_ALLOWLIST:
        if host_id not in configured_ids:
            host_specs.append({"id": host_id, "label": HOST_ALLOWLIST[host_id], "kind": "host", "skill_roots": [], "plugin_roots": []})

    root_specs: list[tuple[str, str, Path]] = []
    for spec in host_specs:
        host_id = spec["id"]
        if host_id not in SCANNABLE_HOSTS:
            continue
        for asset_type, key in (("skill", "skill_roots"), ("plugin", "plugin_roots")):
            for template in spec.get(key, []):
                if not isinstance(template, str) or "{home}" not in template:
                    continue
                root_specs.append((host_id, asset_type, Path(template.replace("{home}", str(observed_home), 1))))

    allowed_roots_list = [_resolved_without_following_leaf(root) for _, _, root in root_specs]
    for template in config.get("allowed_source_roots", []):
        if isinstance(template, str) and "{home}" in template:
            allowed_roots_list.append(
                _resolved_without_following_leaf(Path(template.replace("{home}", str(observed_home), 1)))
            )
    allowed_roots = tuple(dict.fromkeys(allowed_roots_list))

    findings: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    scopes: list[dict[str, Any]] = []
    occurrences: list[dict[str, Any]] = []
    deadline = time.monotonic() + float(limits["scan_timeout_seconds"])
    skill_provenance = _load_skill_provenance(observed_home, limits)

    for host_id, asset_type, root in root_specs:
        found, root_findings, root_errors, scope = _scan_root(
            host_id,
            asset_type,
            root,
            observed_home,
            allowed_roots,
            limits,
            deadline,
            skill_provenance,
        )
        occurrences.extend(found)
        findings.extend(root_findings)
        errors.extend(root_errors)
        scopes.append(scope)

    plugin_root = Path(
        metadata_config["plugin_cache_root"].replace("{home}", str(observed_home), 1)
    )
    plugin_scopes = [
        scope
        for scope in scopes
        if scope.get("host_id") == "codex"
        and scope.get("asset_type") == "plugin"
    ]
    plugin_scope_status = _scope_coverage_status(plugin_scopes) if plugin_scopes else "missing"
    packages = _plugin_packages(occurrences, plugin_root)
    metadata_found, metadata_findings, metadata_errors, metadata_states = (
        _scan_codex_extension_metadata(
            packages,
            occurrences,
            plugin_root,
            plugin_scope_status,
            observed_home,
            metadata_config,
            limits,
            deadline,
        )
    )
    occurrences.extend(metadata_found)
    findings.extend(metadata_findings)
    errors.extend(metadata_errors)

    occurrences.extend(_static_occurrences(config, limits))
    overlay_file = Path(overlay_path) if overlay_path is not None else bundle_root / "registry" / "chinese_metadata.json"
    overlay = _load_overlay(overlay_file, limits, errors)
    items = _build_items(occurrences, overlay, findings)
    coverage = _build_coverage(scopes, metadata_states, items)

    host_rows: list[dict[str, Any]] = []
    for host_id, label in HOST_ALLOWLIST.items():
        related = [row for row in coverage if row["host_id"] == host_id]
        status = _host_status_from_coverage(related)
        host_rows.append(
            {
                "id": host_id,
                "label": label,
                "kind": "host",
                "status": status,
                "safe_metadata_only": host_id == "antigravity",
            }
        )

    counts_by_type = {asset_type: 0 for asset_type in sorted(ASSET_TYPES)}
    for item in items:
        counts_by_type[item["type"]] += 1
    summary = {
        "asset_count": len(items),
        "counts_by_type": counts_by_type,
        "host_binding_count": sum(len(item["host_bindings"]) for item in items),
        "localized_skill_count": sum(
            1
            for item in items
            if item["type"] == "skill" and item["localization"].get("coverage_complete")
        ),
        "stale_localization_count": sum(
            1 for item in items if item["localization"].get("status") == "stale"
        ),
        "locked_asset_count": sum(1 for item in items if item["trust"]["action"] == "locked"),
        "health_finding_count": len(findings),
        "scan_error_count": len(errors),
    }

    findings = sorted(
        findings,
        key=lambda item: (
            {"error": 0, "warning": 1, "info": 2}.get(item["severity"], 3),
            item["code"],
            item.get("asset_id", ""),
            item.get("path", ""),
        ),
    )
    errors = sorted(errors, key=lambda item: (item["code"], item.get("host_id", ""), item.get("path", "")))

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "mode": "observe",
        "summary": summary,
        "hosts": host_rows,
        "items": items,
        "health_findings": findings,
        "scan_scope": {
            "mode": "read_only",
            "home": _display_path(observed_home, observed_home),
            "roots": scopes,
            "coverage": coverage,
            "limits": dict(limits),
            "excluded": list(EXCLUDED_BOUNDARIES),
            "safety": {
                "writes": False,
                "network": False,
                "process_execution": False,
                "follows_outside_symlinks": False,
                "reads_host_config_bodies": False,
            },
        },
        "scan_errors": errors,
    }


def _main() -> int:
    parser = argparse.ArgumentParser(description="Build a read-only AI-Toolbox snapshot")
    parser.add_argument("--home", type=Path, default=None, help="explicit home root; missing roots are not created")
    arguments = parser.parse_args()
    print(json.dumps(build_toolbox_payload(arguments.home), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
