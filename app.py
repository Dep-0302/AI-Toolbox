"""Local observe-only API and static server for the AI-Toolbox workbench."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse, urlsplit


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
GENERATED_DIR = ROOT / "generated"
SNAPSHOT_PATH = GENERATED_DIR / "snapshot.json"
LAST_ATTEMPT_PATH = GENERATED_DIR / "last-attempt.json"
LOCK_PATH = GENERATED_DIR / ".refresh.lock"
COLLECTION_SNAPSHOT_PATH = GENERATED_DIR / "collection-snapshot.json"
COLLECTION_LOCK_PATH = GENERATED_DIR / ".collection-refresh.lock"
COLLECTION_SIGNATURE_PATH = GENERATED_DIR / "collection-signature.json"
CANDIDATE_SNAPSHOT_PATH = GENERATED_DIR / "candidate-catalog.json"
PROJECT_SKILL_OUTPUT_ROOT = GENERATED_DIR / "project-skills"
COLLECTION_SIGNATURE_SCHEMA_VERSION = 1
DIST_DIR = ROOT / "dist"
# 候选 builder/checker 只作为固定项目内模块载入；不通过 HTTP 静态暴露。
CANDIDATES_DIR = ROOT / "collection-workbench"
CANDIDATE_BUILDER_PATH = CANDIDATES_DIR / "workbench.py"
CANDIDATE_CHECKER_PATH = CANDIDATES_DIR / "check_data.py"
NATIVE_FOLDER_PICKER_PATH = SRC / "native_folder_picker.py"
MACOS_OSASCRIPT_PATH = Path("/usr/bin/osascript")
API_VERSION = "v1"
RELEASE_ID = "0.2.1-public"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MAX_REQUEST_BYTES = 1024
FOLDER_SELECTION_TTL_SECONDS = 120
FOLDER_PICKER_TIMEOUT_SECONDS = 300
FOLDER_SELECTION_TARGET = "collection-source"
PROJECT_SKILL_FOLDER_SELECTION_TARGET = "project-skill-source"
_CANDIDATE_MODULE_LOCK = threading.RLock()
HOST_IDS = ("codex", "claude", "hermes", "workbuddy", "antigravity")
HOST_LABELS = {
    "codex": "Codex",
    "claude": "Claude",
    "hermes": "Hermes",
    "workbuddy": "WorkBuddy",
    "antigravity": "Antigravity",
}
COVERAGE_STATUSES = frozenset(
    {"observed", "partial", "missing", "not_connected", "placeholder", "error"}
)
INCOMPLETE_SCOPE_STATES = frozenset(
    {"error", "partial", "blocked_symlink_root", "not_directory"}
)
INCOMPLETE_COVERAGE_STATES = frozenset({"error", "partial"})
ROOT_SCOPE_STATUSES = frozenset(
    {"missing", "observed", "partial", "error", "blocked_symlink_root", "not_directory"}
)
ROOT_SCOPE_PAIRS = frozenset(
    {
        ("codex", "skill"),
        ("codex", "plugin"),
        ("claude", "skill"),
        ("claude", "plugin"),
        ("hermes", "skill"),
        ("workbuddy", "skill"),
    }
)
EXPECTED_ROOT_PATHS = {
    ("codex", "skill"): "~/.codex/skills",
    ("codex", "plugin"): "~/.codex/plugins/cache",
    ("claude", "skill"): "~/.claude/skills",
    ("claude", "plugin"): "~/.claude/plugins/cache",
    ("hermes", "skill"): "~/.hermes/skills",
    ("workbuddy", "skill"): "~/.workbuddy/skills",
}
EXPECTED_ADAPTERS = {
    **{pair: "manifest_directory_v1" for pair in ROOT_SCOPE_PAIRS},
    ("codex", "mcp"): "codex_plugin_mcp_v1",
    ("codex", "cli"): "codex_cli_metadata_v1",
    ("codex", "sdk"): "codex_plugin_sdk_v1",
}
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
GENERATION_ID_RE = re.compile(r"^[a-f0-9]{16}$")
RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
CODEX_CACHE_PREFIX = ("~", ".codex", "plugins", "cache")

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from toolbox_scan import (  # noqa: E402
    ASSET_TYPES,
    EXCLUDED_BOUNDARIES,
    EXPECTED_SCAN_LIMITS,
    SKILL_ORIGIN_BASES,
    SKILL_ORIGIN_KINDS,
    build_toolbox_payload,
)
from collection_scan import (  # noqa: E402
    DEFAULT_SOURCE_ROOT,
    build_collection_input_state,
    build_collection_payload,
    validate_collection_payload,
)
from project_skill_scan import (  # noqa: E402
    BOUNDARY_ID as PROJECT_SKILL_BOUNDARY_ID,
    BOUNDARY_PATH as PROJECT_SKILL_BOUNDARY_PATH,
    EXCLUDED_NAMES as PROJECT_SKILL_EXCLUDED_NAMES,
    PRODUCTION_ROOT as PROJECT_SKILL_PRODUCTION_ROOT,
    ScanRejected as ProjectSkillScanRejected,
    build_project_skill_snapshot,
    load_project_skill_runtime_boundary,
    validate_project_skill_runtime_boundary,
)
from project_skill_report import (  # noqa: E402
    ProjectSkillPersistenceError,
    ProjectSkillStore,
)


class ProjectSkillReceiptUnavailable(RuntimeError):
    """The complete snapshot committed, but its auxiliary attempt receipt did not."""


class SnapshotValidationError(ValueError):
    """Raised when a generated snapshot violates the observe-only contract."""


class RefreshBusy(RuntimeError):
    """Raised when another thread or process already owns the refresh lock."""


class CollectionScanIncomplete(RuntimeError):
    """Raised when a collection result is unsafe to promote as current truth."""

    def __init__(self, payload: dict[str, Any], reason: str) -> None:
        super().__init__(reason)
        self.payload = payload
        self.reason = reason


class FolderSelectionError(ValueError):
    """Raised when a native folder selection crosses the local read boundary."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _assert_exact_object(
    value: Any,
    *,
    allowed: set[str] | frozenset[str],
    required: set[str] | frozenset[str] | None = None,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SnapshotValidationError(f"{label} must be an object")
    keys = set(value)
    required_keys = set(allowed if required is None else required)
    if keys - set(allowed) or required_keys - keys:
        raise SnapshotValidationError(f"{label} fields do not match the data contract")
    return value


def _assert_string(value: Any, *, label: str, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise SnapshotValidationError(f"{label} must be a string")
    return value


def _assert_nullable_string(value: Any, *, label: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise SnapshotValidationError(f"{label} must be a string or null")
    return value


def _assert_boolean(value: Any, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise SnapshotValidationError(f"{label} must be a boolean")
    return value


def _assert_enum(value: Any, choices: set[Any] | frozenset[Any], *, label: str) -> Any:
    try:
        accepted = value in choices
    except TypeError as exc:
        raise SnapshotValidationError(f"{label} is outside the data contract") from exc
    if not accepted:
        raise SnapshotValidationError(f"{label} is outside the data contract")
    return value


def _assert_sha256(value: Any, *, label: str, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise SnapshotValidationError(f"{label} must be a SHA-256 digest")
    return value


def _assert_string_list(
    value: Any,
    *,
    label: str,
    nonempty_items: bool = False,
    minimum: int = 0,
    maximum: int | None = None,
    unique: bool = False,
) -> list[str]:
    if not isinstance(value, list) or len(value) < minimum:
        raise SnapshotValidationError(f"{label} must be a string array")
    if maximum is not None and len(value) > maximum:
        raise SnapshotValidationError(f"{label} exceeds its item limit")
    if any(
        not isinstance(item, str) or (nonempty_items and not item)
        for item in value
    ):
        raise SnapshotValidationError(f"{label} contains an invalid string")
    if unique and len(value) != len(set(value)):
        raise SnapshotValidationError(f"{label} must contain unique values")
    return value


def _parse_datetime(value: Any, *, label: str, nullable: bool = False) -> datetime | None:
    if nullable and value is None:
        return None
    if (
        not isinstance(value, str)
        or not value
        or RFC3339_RE.fullmatch(value) is None
    ):
        raise SnapshotValidationError(f"{label} must be an RFC 3339 date-time")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise SnapshotValidationError(f"{label} must be an RFC 3339 date-time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SnapshotValidationError(f"{label} must include a timezone")
    return parsed


def _safe_projected_path_parts(value: Any) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or value != value.strip() or "\\" in value:
        raise SnapshotValidationError("snapshot projected path is invalid")
    parts = tuple(value.split("/"))
    if any(not part or part in {".", ".."} for part in parts):
        raise SnapshotValidationError("snapshot projected path contains traversal")
    return parts


def _is_codex_mcp_source_ref(value: str, *, allow_root: bool = True) -> bool:
    if value == "~/.codex/plugins/cache":
        return allow_root
    try:
        parts = _safe_projected_path_parts(value)
    except SnapshotValidationError:
        return False
    return (
        len(parts) == 8
        and parts[:4] == CODEX_CACHE_PREFIX
        and parts[-1] == ".mcp.json"
    )


def _is_codex_sdk_source_ref(
    value: str,
    *,
    allow_root: bool = True,
    allow_declaring_manifest: bool = True,
) -> bool:
    if value == "~/.codex/plugins/cache":
        return allow_root
    try:
        parts = _safe_projected_path_parts(value)
    except SnapshotValidationError:
        return False
    declaring_manifest = (
        allow_declaring_manifest
        and len(parts) == 8
        and parts[:4] == CODEX_CACHE_PREFIX
        and parts[-1] == "package.json"
    )
    sdk_manifest = (
        len(parts) == 11
        and parts[:4] == CODEX_CACHE_PREFIX
        and parts[7:] == (
            "node_modules",
            "@modelcontextprotocol",
            "sdk",
            "package.json",
        )
    )
    return declaring_manifest or sdk_manifest


def _validate_package_variant(value: Any, *, label: str) -> dict[str, Any]:
    variant = _assert_exact_object(
        value,
        allowed={
            "source_ref",
            "host_id",
            "observed_path",
            "package_fingerprint",
            "package_fingerprint_status",
        },
        label=label,
    )
    _assert_string(variant.get("source_ref"), label=f"{label} source_ref", nonempty=True)
    _assert_enum(
        variant.get("host_id"), set(HOST_IDS) | {None}, label=f"{label} host_id"
    )
    _assert_nullable_string(variant.get("observed_path"), label=f"{label} observed_path")
    _assert_sha256(
        variant.get("package_fingerprint"),
        label=f"{label} package_fingerprint",
        nullable=True,
    )
    status = _assert_enum(
        variant.get("package_fingerprint_status"),
        {"complete", "incomplete"},
        label=f"{label} package_fingerprint_status",
    )
    if (status == "complete") is (variant.get("package_fingerprint") is None):
        raise SnapshotValidationError(f"{label} package fingerprint facts are inconsistent")
    return variant


def _root_status_from_rows(rows: list[dict[str, Any]]) -> str:
    statuses = {row["status"] for row in rows}
    if statuses & {"error", "blocked_symlink_root", "not_directory"}:
        return "error"
    if "partial" in statuses:
        return "partial"
    if "observed" in statuses:
        return "observed"
    return "missing"


def _generation_id_for(payload: dict[str, Any]) -> str:
    canonical_payload = dict(payload)
    canonical_payload.pop("generation_id", None)
    canonical = json.dumps(
        canonical_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()[:16]


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


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _assert_plain_directory(path: Path, *, create: bool = False) -> None:
    """Require a real directory whose project-local parent chain has no symlink."""

    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.exists() or not path.is_dir() or path.is_symlink():
        raise RuntimeError(f"unsafe or missing workbench directory: {path.name}")
    resolved_root = ROOT.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    if not _is_relative_to(resolved_path, resolved_root):
        raise RuntimeError(f"workbench directory escaped project root: {path.name}")
    current = path
    while current != ROOT:
        if current.is_symlink():
            raise RuntimeError(f"symlink is not allowed in workbench path: {current.name}")
        current = current.parent


def _assert_dist_safe() -> None:
    _assert_plain_directory(DIST_DIR)
    index = DIST_DIR / "index.html"
    if not index.is_file() or index.is_symlink():
        raise RuntimeError("dist/index.html is missing; run npm run build first")
    for candidate in DIST_DIR.rglob("*"):
        if candidate.is_symlink():
            raise RuntimeError("symlinks are not allowed inside dist")


def _validate_payload(payload: Any, *, require_generation: bool) -> dict[str, Any]:
    """Validate the strict persisted-data projection the UI is allowed to trust."""

    if not isinstance(payload, dict):
        raise SnapshotValidationError("snapshot must be an object")
    required = {
        "schema_version",
        "generated_at",
        "mode",
        "summary",
        "hosts",
        "items",
        "health_findings",
        "scan_scope",
        "scan_errors",
    }
    if require_generation:
        required.add("generation_id")
    _assert_exact_object(
        payload,
        allowed=required | {"generation_id"},
        required=required,
        label="snapshot",
    )
    if payload.get("schema_version") != 1 or payload.get("mode") != "observe":
        raise SnapshotValidationError("snapshot is not observe-mode schema v1")
    _parse_datetime(payload.get("generated_at"), label="snapshot generated_at")
    if not isinstance(payload.get("hosts"), list) or not isinstance(payload.get("items"), list):
        raise SnapshotValidationError("snapshot collections are invalid")
    if not isinstance(payload.get("health_findings"), list) or not isinstance(payload.get("scan_errors"), list):
        raise SnapshotValidationError("snapshot issue collections are invalid")
    summary_fields = {
        "asset_count",
        "counts_by_type",
        "host_binding_count",
        "localized_skill_count",
        "stale_localization_count",
        "locked_asset_count",
        "health_finding_count",
        "scan_error_count",
    }
    summary = _assert_exact_object(
        payload.get("summary"), allowed=summary_fields, label="snapshot summary"
    )
    counts_by_type = _assert_exact_object(
        summary.get("counts_by_type"),
        allowed=set(ASSET_TYPES),
        label="snapshot type counts",
    )
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 0
        for value in counts_by_type.values()
    ):
        raise SnapshotValidationError("snapshot type counts are invalid")
    for key in summary_fields - {"counts_by_type"}:
        value = summary.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise SnapshotValidationError("snapshot summary counts are invalid")
    scope = payload.get("scan_scope")
    scope_fields = {"mode", "home", "roots", "coverage", "limits", "excluded", "safety"}
    scope = _assert_exact_object(scope, allowed=scope_fields, label="snapshot scan_scope")
    if scope.get("mode") != "read_only" or not isinstance(scope.get("home"), str) or not scope["home"]:
        raise SnapshotValidationError("snapshot scan scope identity is invalid")
    if scope["home"] != "~/.":
        raise SnapshotValidationError("snapshot scan scope must not expose an absolute home")
    safety_fields = {
        "writes",
        "network",
        "process_execution",
        "follows_outside_symlinks",
        "reads_host_config_bodies",
    }
    safety = _assert_exact_object(
        scope.get("safety"), allowed=safety_fields, label="snapshot safety declaration"
    )
    for flag in safety_fields:
        if safety.get(flag) is not False:
            raise SnapshotValidationError("snapshot exceeds the observe-only safety boundary")
    limits = _assert_exact_object(
        scope.get("limits"),
        allowed=set(EXPECTED_SCAN_LIMITS),
        label="snapshot scan limits",
    )
    if limits != EXPECTED_SCAN_LIMITS:
        raise SnapshotValidationError("snapshot scan limits exceed the frozen read budget")
    excluded = scope.get("excluded")
    if not isinstance(excluded, list) or tuple(excluded) != EXCLUDED_BOUNDARIES:
        raise SnapshotValidationError("snapshot exclusions do not match the frozen boundary")

    root_fields = {
        "host_id",
        "asset_type",
        "path",
        "status",
        "entries_examined",
        "manifests_read",
        "transient_entries_excluded",
        "depth_limited_entries",
    }
    roots = scope.get("roots")
    if not isinstance(roots, list) or len(roots) != len(ROOT_SCOPE_PAIRS):
        raise SnapshotValidationError("snapshot roots must contain the frozen six sources")
    roots_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for root in roots:
        root = _assert_exact_object(root, allowed=root_fields, label="snapshot root row")
        pair = (root.get("host_id"), root.get("asset_type"))
        if pair not in ROOT_SCOPE_PAIRS or pair in roots_by_pair:
            raise SnapshotValidationError("snapshot root rows are invalid or duplicated")
        if root.get("path") != EXPECTED_ROOT_PATHS[pair]:
            raise SnapshotValidationError("snapshot root path is invalid")
        if root.get("status") not in ROOT_SCOPE_STATUSES:
            raise SnapshotValidationError("snapshot root status is invalid")
        counters = (
            root.get("entries_examined"),
            root.get("manifests_read"),
            root.get("transient_entries_excluded"),
            root.get("depth_limited_entries"),
        )
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in counters):
            raise SnapshotValidationError("snapshot root counters are invalid")
        if any(value > root["entries_examined"] for value in counters[1:]):
            raise SnapshotValidationError("snapshot root counters exceed examined entries")
        if root["status"] == "missing" and any(counters):
            raise SnapshotValidationError("snapshot missing root contains observed facts")
        roots_by_pair[pair] = [root]
    if set(roots_by_pair) != set(ROOT_SCOPE_PAIRS):
        raise SnapshotValidationError("snapshot root rows are incomplete")
    expected_coverage_counts = {
        (host_id, asset_type): 0
        for host_id in HOST_IDS
        for asset_type in ASSET_TYPES
    }
    item_fields = {
        "asset_id", "type", "source_name", "description", "source", "fingerprint",
        "manifest_fingerprint", "host_bindings", "localization", "trust", "curation",
        "composition", "duplicate",
    }
    source_fields = {
        "ref", "version", "author", "license", "origin_kind", "origin_basis",
        "display_name", "source_refs",
        "manifest_fingerprint", "package_fingerprint_status", "package_fingerprints",
        "package_variants", "observation",
    }
    observation_fields = {
        "adapter_id", "basis", "plugin_channel", "plugin_name", "plugin_version",
        "server_id", "transport_kind", "sensitive_config_present", "cli_name",
        "sdk_package_name",
    }
    binding_fields = {
        "host_id", "presence", "provisioning", "link_health", "activation", "effective",
        "action", "locked_reason", "observed_path", "link_target", "package_fingerprint",
        "package_fingerprint_status",
    }
    variant_fields = {
        "source_ref", "host_id", "observed_path", "package_fingerprint",
        "package_fingerprint_status",
    }
    localization_missing_fields = {"status", "coverage_complete", "translated_from_hash"}
    localization_complete_fields = {
        "asset_id", "zh_name", "source_name", "summary", "use_cases", "not_for", "examples",
        "synonyms", "source_ref", "translated_from_hash", "status", "generated_at",
        "reviewed_at", "evidence", "coverage_complete",
    }
    for item in payload["items"]:
        item = _assert_exact_object(item, allowed=item_fields, label="snapshot asset")
        if item.get("type") not in ASSET_TYPES:
            raise SnapshotValidationError("snapshot contains an unsupported asset type")
        _assert_string(item.get("asset_id"), label="snapshot asset_id", nonempty=True)
        _assert_string(item.get("source_name"), label="snapshot source_name", nonempty=True)
        _assert_string(item.get("description"), label="snapshot description")
        _assert_sha256(item.get("fingerprint"), label="snapshot asset fingerprint")
        _assert_sha256(
            item.get("manifest_fingerprint"), label="snapshot asset manifest fingerprint"
        )
        source = _assert_exact_object(
            item.get("source"), allowed=source_fields, label="snapshot asset source"
        )
        _assert_string(source.get("ref"), label="snapshot source ref", nonempty=True)
        for field in ("version", "author", "license", "display_name"):
            _assert_nullable_string(
                source.get(field), label=f"snapshot source {field}"
            )
        _assert_enum(
            source.get("origin_kind"),
            SKILL_ORIGIN_KINDS,
            label="snapshot source origin kind",
        )
        _assert_enum(
            source.get("origin_basis"),
            SKILL_ORIGIN_BASES,
            label="snapshot source origin basis",
        )
        source_refs = _assert_string_list(
            source.get("source_refs"),
            label="snapshot source refs",
            nonempty_items=True,
            minimum=1,
            unique=True,
        )
        _assert_sha256(
            source.get("manifest_fingerprint"), label="snapshot source manifest fingerprint"
        )
        if source["manifest_fingerprint"] != item["manifest_fingerprint"]:
            raise SnapshotValidationError("snapshot manifest fingerprints are inconsistent")
        package_status = _assert_enum(
            source.get("package_fingerprint_status"),
            {"complete", "divergent", "incomplete"},
            label="snapshot package fingerprint status",
        )
        package_fingerprints = source.get("package_fingerprints")
        if not isinstance(package_fingerprints, list):
            raise SnapshotValidationError("snapshot package fingerprints are invalid")
        for fingerprint in package_fingerprints:
            _assert_sha256(fingerprint, label="snapshot package fingerprint")
        if len(package_fingerprints) != len(set(package_fingerprints)):
            raise SnapshotValidationError("snapshot package fingerprints are duplicated")
        if package_status == "complete" and not package_fingerprints:
            raise SnapshotValidationError("snapshot complete package has no package fingerprint")
        observation = source.get("observation")
        if observation is not None:
            observation = _assert_exact_object(
                observation,
                allowed=observation_fields,
                label="snapshot observation projection",
            )
            _assert_enum(
                observation.get("adapter_id"),
                {
                    "codex_plugin_mcp_v1",
                    "codex_cli_metadata_v1",
                    "codex_plugin_sdk_v1",
                },
                label="snapshot observation adapter",
            )
            _assert_enum(
                observation.get("basis"),
                {
                    "cache_manifest_projection",
                    "filesystem_metadata_projection",
                    "dependency_manifest_projection",
                },
                label="snapshot observation basis",
            )
            for field in (
                "plugin_channel",
                "plugin_name",
                "plugin_version",
                "server_id",
                "cli_name",
                "sdk_package_name",
            ):
                _assert_nullable_string(
                    observation.get(field), label=f"snapshot observation {field}"
                )
            _assert_enum(
                observation.get("transport_kind"),
                {"stdio", "http", "ambiguous", "unknown", None},
                label="snapshot observation transport",
            )
            if observation.get("sensitive_config_present") is not None:
                _assert_boolean(
                    observation.get("sensitive_config_present"),
                    label="snapshot observation sensitive_config_present",
                )
        expected_observation_adapter = {
            "mcp": "codex_plugin_mcp_v1",
            "cli": "codex_cli_metadata_v1",
            "sdk": "codex_plugin_sdk_v1",
        }.get(item["type"])
        expected_observation_basis = {
            "mcp": "cache_manifest_projection",
            "cli": "filesystem_metadata_projection",
            "sdk": "dependency_manifest_projection",
        }.get(item["type"])
        if expected_observation_adapter is None:
            if observation is not None:
                raise SnapshotValidationError("snapshot manifest asset has an unexpected observation")
        elif (
            not isinstance(observation, dict)
            or observation.get("adapter_id") != expected_observation_adapter
            or observation.get("basis") != expected_observation_basis
        ):
            raise SnapshotValidationError("snapshot extension observation adapter is invalid")
        if item["type"] == "mcp":
            if (
                not observation.get("server_id")
                or observation.get("transport_kind") is None
                or not isinstance(observation.get("sensitive_config_present"), bool)
                or any(
                    not observation.get(field)
                    for field in ("plugin_channel", "plugin_name", "plugin_version")
                )
                or observation.get("cli_name") is not None
                or observation.get("sdk_package_name") is not None
                or any(
                    not _is_codex_mcp_source_ref(ref, allow_root=False)
                    for ref in source_refs
                )
            ):
                raise SnapshotValidationError("snapshot MCP projection is invalid")
        elif item["type"] == "cli":
            if (
                observation.get("cli_name") != "codex"
                or observation.get("sensitive_config_present") is not False
                or any(
                    observation.get(field) is not None
                    for field in (
                        "plugin_channel", "plugin_name", "plugin_version", "server_id",
                        "transport_kind", "sdk_package_name",
                    )
                )
                or source.get("ref") != "~/.local/bin/codex"
                or source_refs != ["~/.local/bin/codex"]
            ):
                raise SnapshotValidationError("snapshot CLI projection is invalid")
        elif item["type"] == "sdk":
            if (
                observation.get("sdk_package_name") != "@modelcontextprotocol/sdk"
                or observation.get("sensitive_config_present") is not False
                or any(
                    not observation.get(field)
                    for field in ("plugin_channel", "plugin_name", "plugin_version")
                )
                or any(
                    observation.get(field) is not None
                    for field in ("server_id", "transport_kind", "cli_name")
                )
                or any(
                    not _is_codex_sdk_source_ref(
                        ref, allow_root=False, allow_declaring_manifest=False
                    )
                    for ref in source_refs
                )
            ):
                raise SnapshotValidationError("snapshot SDK projection is invalid")
        variants = source.get("package_variants")
        if not isinstance(variants, list) or not variants:
            raise SnapshotValidationError("snapshot package variants are invalid")
        for variant in variants:
            _validate_package_variant(variant, label="snapshot package variant")
        if item["type"] in {"mcp", "sdk"}:
            if source.get("ref") not in source_refs:
                raise SnapshotValidationError("snapshot extension source ref is inconsistent")
            for variant in variants:
                if (
                    variant.get("source_ref") not in source_refs
                    or variant.get("observed_path") not in source_refs
                ):
                    raise SnapshotValidationError(
                        "snapshot extension package variant escaped its source refs"
                    )
        complete_variant_fingerprints = sorted(
            {
                variant["package_fingerprint"]
                for variant in variants
                if variant["package_fingerprint_status"] == "complete"
            }
        )
        if sorted(package_fingerprints) != complete_variant_fingerprints:
            raise SnapshotValidationError("snapshot package fingerprints do not match variants")
        incomplete_variants = any(
            variant["package_fingerprint_status"] == "incomplete" for variant in variants
        )
        if (
            (package_status == "complete" and (incomplete_variants or len(package_fingerprints) != 1))
            or (package_status == "divergent" and (incomplete_variants or len(package_fingerprints) < 2))
            or (package_status == "incomplete" and not incomplete_variants)
        ):
            raise SnapshotValidationError("snapshot package status does not match variants")
        trust = _assert_exact_object(
            item.get("trust"),
            allowed={"review_state", "license_state", "risk_level", "action", "locked_reason"},
            label="snapshot asset trust",
        )
        if (
            trust.get("action") != "locked"
            or trust.get("review_state") not in {"unreviewed", "vetted", "blocked"}
            or trust.get("license_state")
            not in {"declared", "unknown", "restricted", "incompatible"}
            or trust.get("risk_level")
            not in {"unknown", "low", "medium", "high", "critical"}
            or trust.get("locked_reason")
            not in {
                "read_only_inventory",
                "package_divergence",
                "package_fingerprint_incomplete",
                "safe_metadata_only",
            }
        ):
            raise SnapshotValidationError("snapshot contains an unlocked asset")
        curation = _assert_exact_object(
            item.get("curation"),
            allowed={"category", "subcategory", "common", "archived"},
            required={"category", "common", "archived"},
            label="snapshot asset curation",
        )
        _assert_string(curation.get("category"), label="snapshot curation category", nonempty=True)
        subcategory = _assert_nullable_string(
            curation.get("subcategory"), label="snapshot curation subcategory"
        )
        if subcategory == "":
            raise SnapshotValidationError("snapshot curation subcategory must not be empty")
        _assert_boolean(curation.get("common"), label="snapshot curation common")
        _assert_boolean(curation.get("archived"), label="snapshot curation archived")
        duplicate = _assert_exact_object(
            item.get("duplicate"),
            allowed={"exact_duplicate", "occurrence_count", "name_collision", "package_divergence"},
            label="snapshot duplicate facts",
        )
        for field in ("exact_duplicate", "name_collision", "package_divergence"):
            _assert_boolean(duplicate.get(field), label=f"snapshot duplicate {field}")
        if (
            not isinstance(duplicate.get("occurrence_count"), int)
            or isinstance(duplicate.get("occurrence_count"), bool)
            or duplicate["occurrence_count"] < 1
        ):
            raise SnapshotValidationError("snapshot duplicate occurrence_count is invalid")
        localization = item.get("localization")
        localization_fields = (
            localization_missing_fields
            if isinstance(localization, dict) and localization.get("status") == "missing"
            else localization_complete_fields
        )
        localization = _assert_exact_object(
            localization, allowed=localization_fields, label="snapshot localization"
        )
        if localization.get("status") == "missing":
            if (
                localization.get("coverage_complete") is not False
                or localization.get("translated_from_hash") is not None
            ):
                raise SnapshotValidationError("snapshot missing localization is invalid")
        else:
            for field in ("asset_id", "zh_name", "source_name", "summary", "source_ref"):
                _assert_string(
                    localization.get(field),
                    label=f"snapshot localization {field}",
                    nonempty=True,
                )
            for field in ("use_cases", "not_for"):
                _assert_string_list(
                    localization.get(field),
                    label=f"snapshot localization {field}",
                    nonempty_items=True,
                    minimum=1,
                )
            _assert_string_list(
                localization.get("examples"),
                label="snapshot localization examples",
                nonempty_items=True,
                minimum=1,
                maximum=3,
            )
            _assert_string_list(
                localization.get("synonyms"),
                label="snapshot localization synonyms",
                nonempty_items=True,
            )
            _assert_sha256(
                localization.get("translated_from_hash"),
                label="snapshot localization translated_from_hash",
            )
            _assert_enum(
                localization.get("status"),
                {"ai_draft", "reviewed", "stale"},
                label="snapshot localization status",
            )
            _parse_datetime(
                localization.get("generated_at"), label="snapshot localization generated_at"
            )
            _parse_datetime(
                localization.get("reviewed_at"),
                label="snapshot localization reviewed_at",
                nullable=True,
            )
            _assert_string_list(
                localization.get("evidence"), label="snapshot localization evidence"
            )
            _assert_boolean(
                localization.get("coverage_complete"),
                label="snapshot localization coverage_complete",
            )
        composition = item.get("composition")
        if not isinstance(composition, list):
            raise SnapshotValidationError("snapshot composition is invalid")
        for component in composition:
            component = _assert_exact_object(
                component, allowed={"type", "name"}, label="snapshot composition entry"
            )
            _assert_enum(
                component.get("type"), {"skill", "mcp", "cli"}, label="snapshot composition type"
            )
            _assert_string(
                component.get("name"), label="snapshot composition name", nonempty=True
            )
        bindings = item.get("host_bindings")
        if not isinstance(bindings, list):
            raise SnapshotValidationError("snapshot host bindings are invalid")
        bound_hosts: set[str] = set()
        for binding in bindings:
            binding = _assert_exact_object(
                binding, allowed=binding_fields, label="snapshot host binding"
            )
            if (
                binding.get("host_id") not in HOST_IDS
                or binding.get("action") != "locked"
                or binding.get("activation") != "unknown"
                or binding.get("effective") != "unverified"
                or binding.get("presence") not in {"present", "cached", "unknown"}
                or binding.get("provisioning")
                not in {"copy", "unmanaged_symlink", "cache", "system_managed", "configured", "none"}
                or binding.get("link_health") not in {"ok", "na"}
                or binding.get("locked_reason")
                not in {
                    "read_only_inventory", "read_only_cache_inventory", "system_managed",
                    "safe_metadata_only",
                }
                or binding.get("package_fingerprint_status")
                not in {"complete", "incomplete"}
            ):
                raise SnapshotValidationError("snapshot contains an actionable host binding")
            _assert_nullable_string(
                binding.get("observed_path"), label="snapshot binding observed_path"
            )
            _assert_nullable_string(
                binding.get("link_target"), label="snapshot binding link_target"
            )
            _assert_sha256(
                binding.get("package_fingerprint"),
                label="snapshot binding package_fingerprint",
                nullable=True,
            )
            if (
                (binding["package_fingerprint_status"] == "complete")
                is (binding.get("package_fingerprint") is None)
            ):
                raise SnapshotValidationError("snapshot binding package fingerprint is inconsistent")
            if item["type"] == "cli" and (
                binding.get("host_id") != "codex"
                or binding.get("observed_path") != "~/.local/bin/codex"
                or binding.get("link_target")
                != "~/.codex/plugins/.plugin-appserver/codex"
            ):
                raise SnapshotValidationError("snapshot CLI binding is invalid")
            if item["type"] == "mcp" and (
                binding.get("host_id") != "codex"
                or not _is_codex_mcp_source_ref(
                    binding.get("observed_path"), allow_root=False
                )
                or binding.get("observed_path") not in source_refs
                or binding.get("link_target") is not None
            ):
                raise SnapshotValidationError("snapshot MCP binding is invalid")
            if item["type"] == "sdk" and (
                binding.get("host_id") != "codex"
                or not _is_codex_sdk_source_ref(
                    binding.get("observed_path"),
                    allow_root=False,
                    allow_declaring_manifest=False,
                )
                or binding.get("observed_path") not in source_refs
                or binding.get("link_target") is not None
            ):
                raise SnapshotValidationError("snapshot SDK binding is invalid")
            bound_hosts.add(binding["host_id"])
        for host_id in bound_hosts:
            expected_coverage_counts[(host_id, item["type"])] += 1

    finding_fields = {
        "code", "severity", "title", "detail", "asset_id", "host_id", "path", "target",
        "reason", "package_variants",
    }
    for finding in payload["health_findings"]:
        finding = _assert_exact_object(
            finding,
            allowed=finding_fields,
            required={"code", "severity", "title", "detail"},
            label="snapshot health finding",
        )
        _assert_string(finding.get("code"), label="snapshot finding code", nonempty=True)
        _assert_enum(
            finding.get("severity"),
            {"error", "warning", "info"},
            label="snapshot finding severity",
        )
        _assert_string(finding.get("title"), label="snapshot finding title", nonempty=True)
        _assert_string(finding.get("detail"), label="snapshot finding detail", nonempty=True)
        for field in ("asset_id", "host_id", "path", "target", "reason"):
            if field in finding:
                _assert_string(finding[field], label=f"snapshot finding {field}")
        if "package_variants" in finding:
            if not isinstance(finding["package_variants"], list):
                raise SnapshotValidationError("snapshot finding variants are invalid")
            for variant in finding["package_variants"]:
                _validate_package_variant(
                    variant, label="snapshot finding package variant"
                )
    for error in payload["scan_errors"]:
        error = _assert_exact_object(
            error,
            allowed={"code", "host_id", "path", "message"},
            required={"code", "message"},
            label="snapshot scan error",
        )
        _assert_string(error.get("code"), label="snapshot scan error code", nonempty=True)
        _assert_string(error.get("message"), label="snapshot scan error message")
        for field in ("host_id", "path"):
            if field in error:
                _assert_string(error[field], label=f"snapshot scan error {field}")

    coverage = scope.get("coverage")
    if not isinstance(coverage, list) or len(coverage) != len(expected_coverage_counts):
        raise SnapshotValidationError("snapshot coverage must contain all 25 host/type pairs")
    seen_coverage: set[tuple[str, str]] = set()
    for row in coverage:
        row = _assert_exact_object(
            row,
            allowed={
                "host_id", "asset_type", "adapter_id", "status", "source_refs",
                "configured_source_count", "observed_source_count", "item_count",
            },
            label="snapshot coverage row",
        )
        host_id = row.get("host_id")
        asset_type = row.get("asset_type")
        pair = (host_id, asset_type)
        if pair not in expected_coverage_counts or pair in seen_coverage:
            raise SnapshotValidationError("snapshot coverage pairs are invalid or duplicated")
        seen_coverage.add(pair)
        status = row.get("status")
        if status not in COVERAGE_STATUSES:
            raise SnapshotValidationError("snapshot coverage status is invalid")
        adapter_id = row.get("adapter_id")
        expected_adapter = EXPECTED_ADAPTERS.get(pair)
        if adapter_id != expected_adapter:
            raise SnapshotValidationError("snapshot coverage adapter does not match the frozen source")
        if status in {"not_connected", "placeholder"}:
            if adapter_id is not None:
                raise SnapshotValidationError("snapshot coverage adapter_id is invalid")
        elif not isinstance(adapter_id, str) or not adapter_id.strip():
            raise SnapshotValidationError("snapshot coverage adapter_id is invalid")
        source_refs = row.get("source_refs")
        if (
            not isinstance(source_refs, list)
            or any(not isinstance(value, str) or not value.strip() for value in source_refs)
            or len(source_refs) != len(set(source_refs))
        ):
            raise SnapshotValidationError("snapshot coverage source_refs are invalid")
        for key in ("configured_source_count", "observed_source_count", "item_count"):
            value = row.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise SnapshotValidationError("snapshot coverage counts are invalid")
        if row["item_count"] != expected_coverage_counts[pair]:
            raise SnapshotValidationError("snapshot coverage item_count does not match bindings")
        configured = row["configured_source_count"]
        observed = row["observed_source_count"]
        item_count = row["item_count"]
        if host_id == "antigravity":
            if status != "placeholder":
                raise SnapshotValidationError("snapshot placeholder coverage is invalid")
        elif expected_adapter is None:
            if status != "not_connected":
                raise SnapshotValidationError("snapshot unconfigured coverage claims observation")
        elif status in {"not_connected", "placeholder"}:
            raise SnapshotValidationError("snapshot configured coverage lost its source")
        if status in {"not_connected", "placeholder"}:
            if source_refs or configured != 0 or observed != 0 or item_count != 0:
                raise SnapshotValidationError("snapshot unconnected coverage contains observed facts")
        else:
            if configured < 1 or not source_refs:
                raise SnapshotValidationError("snapshot connected coverage has no declared source")
            if status == "missing" and (observed != 0 or item_count != 0):
                raise SnapshotValidationError("snapshot missing coverage contains observed facts")
            if status == "observed" and observed < 1 and (
                pair in ROOT_SCOPE_PAIRS or pair == ("codex", "cli")
            ):
                raise SnapshotValidationError("snapshot observed coverage has no observed source")
        if pair in ROOT_SCOPE_PAIRS:
            root_rows = roots_by_pair[pair]
            if (
                status != _root_status_from_rows(root_rows)
                or source_refs != sorted(root["path"] for root in root_rows)
                or configured != len(root_rows)
                or observed
                != sum(1 for root in root_rows if root["status"] in {"observed", "partial"})
            ):
                raise SnapshotValidationError("snapshot directory coverage does not match root facts")
        elif pair in {
            ("codex", "mcp"), ("codex", "cli"), ("codex", "sdk")
        } and configured != 1:
            raise SnapshotValidationError("snapshot Codex metadata coverage count is invalid")
        if pair == ("codex", "cli") and observed not in {0, 1}:
            raise SnapshotValidationError("snapshot Codex CLI observation count is invalid")
        if pair == ("codex", "cli") and set(source_refs) != {
            "~/.local/bin/codex", "~/.codex/plugins/.plugin-appserver/codex"
        }:
            raise SnapshotValidationError("snapshot Codex CLI sources are invalid")
        if pair == ("codex", "mcp") and any(
            not _is_codex_mcp_source_ref(ref)
            for ref in source_refs
        ):
            raise SnapshotValidationError("snapshot Codex MCP sources are invalid")
        if pair == ("codex", "mcp") and (
            "~/.codex/plugins/cache" not in source_refs
            or observed > sum(ref.endswith("/.mcp.json") for ref in source_refs)
        ):
            raise SnapshotValidationError("snapshot Codex MCP source facts are incomplete")
        if pair == ("codex", "sdk") and any(
            not _is_codex_sdk_source_ref(ref)
            for ref in source_refs
        ):
            raise SnapshotValidationError("snapshot Codex SDK sources are invalid")
        if pair == ("codex", "sdk") and (
            "~/.codex/plugins/cache" not in source_refs
            or observed
            > sum(
                ref.endswith(
                    "/node_modules/@modelcontextprotocol/sdk/package.json"
                )
                for ref in source_refs
            )
        ):
            raise SnapshotValidationError("snapshot Codex SDK source facts are incomplete")
        if pair in {("codex", "mcp"), ("codex", "sdk")}:
            projected_item_refs = {
                ref
                for item in payload["items"]
                if item["type"] == asset_type
                for ref in item["source"]["source_refs"]
            }
            if not projected_item_refs.issubset(set(source_refs)):
                raise SnapshotValidationError(
                    "snapshot extension items escaped their coverage sources"
                )
    if seen_coverage != set(expected_coverage_counts):
        raise SnapshotValidationError("snapshot coverage pairs are incomplete")

    hosts = payload["hosts"]
    if len(hosts) != len(HOST_IDS):
        raise SnapshotValidationError("snapshot hosts must contain all five hosts")
    seen_hosts: set[str] = set()
    for host in hosts:
        host = _assert_exact_object(
            host,
            allowed={"id", "label", "kind", "status", "safe_metadata_only"},
            label="snapshot host row",
        )
        host_id = host.get("id")
        if host_id not in HOST_IDS or host_id in seen_hosts:
            raise SnapshotValidationError("snapshot hosts are invalid or duplicated")
        seen_hosts.add(host_id)
        related = [row for row in coverage if row["host_id"] == host_id]
        if (
            host.get("label") != HOST_LABELS[host_id]
            or host.get("kind") != "host"
            or host.get("status") != _host_status_from_coverage(related)
            or host.get("safe_metadata_only") is not (host_id == "antigravity")
        ):
            raise SnapshotValidationError("snapshot host row does not match coverage")
    if seen_hosts != set(HOST_IDS):
        raise SnapshotValidationError("snapshot hosts are incomplete")
    expected_type_counts = {asset_type: 0 for asset_type in sorted(ASSET_TYPES)}
    for item in payload["items"]:
        expected_type_counts[item["type"]] += 1
    expected_summary = {
        "asset_count": len(payload["items"]),
        "counts_by_type": expected_type_counts,
        "host_binding_count": sum(len(item["host_bindings"]) for item in payload["items"]),
        "localized_skill_count": sum(
            1
            for item in payload["items"]
            if item["type"] == "skill" and item.get("localization", {}).get("coverage_complete")
        ),
        "stale_localization_count": sum(
            1
            for item in payload["items"]
            if item.get("localization", {}).get("status") == "stale"
        ),
        "locked_asset_count": sum(
            1 for item in payload["items"] if item["trust"].get("action") == "locked"
        ),
        "health_finding_count": len(payload["health_findings"]),
        "scan_error_count": len(payload["scan_errors"]),
    }
    for key, expected in expected_summary.items():
        if summary.get(key) != expected:
            raise SnapshotValidationError(f"snapshot summary field does not match payload: {key}")
    if "generation_id" in payload:
        generation_id = payload.get("generation_id")
        if (
            not isinstance(generation_id, str)
            or GENERATION_ID_RE.fullmatch(generation_id) is None
            or generation_id != _generation_id_for(payload)
        ):
            raise SnapshotValidationError("snapshot generation_id does not match payload")
    return payload


def validate_scan_payload(payload: Any) -> dict[str, Any]:
    """Validate the scanner projection before adding storage identity."""

    try:
        return _validate_payload(payload, require_generation=False)
    except SnapshotValidationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotValidationError("scanner projection value shape is invalid") from exc


def validate_snapshot(payload: Any) -> dict[str, Any]:
    """Validate a persisted snapshot, including its canonical generation ID."""

    try:
        return _validate_payload(payload, require_generation=True)
    except SnapshotValidationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotValidationError("persisted snapshot value shape is invalid") from exc


def _with_generation_id(payload: dict[str, Any]) -> dict[str, Any]:
    decorated = dict(payload)
    decorated["generation_id"] = _generation_id_for(decorated)
    return decorated


def snapshot_is_complete(payload: dict[str, Any]) -> bool:
    try:
        validate_snapshot(payload)
    except (SnapshotValidationError, KeyError, TypeError):
        return False
    if payload["summary"].get("scan_error_count", len(payload["scan_errors"])):
        return False
    roots = payload["scan_scope"]["roots"]
    if any(
        isinstance(row, dict) and row.get("status") in INCOMPLETE_SCOPE_STATES
        for row in roots
    ):
        return False
    coverage = payload["scan_scope"]["coverage"]
    return not any(
        isinstance(row, dict) and row.get("status") in INCOMPLETE_COVERAGE_STATES
        for row in coverage
    )


def _assert_generated_target(path: Path) -> None:
    _assert_plain_directory(GENERATED_DIR, create=True)
    if path.parent != GENERATED_DIR or path.name not in {
        SNAPSHOT_PATH.name,
        LAST_ATTEMPT_PATH.name,
        COLLECTION_SNAPSHOT_PATH.name,
        COLLECTION_SIGNATURE_PATH.name,
        CANDIDATE_SNAPSHOT_PATH.name,
    }:
        raise RuntimeError("output target is outside the generated boundary")
    if path.exists() and path.is_symlink():
        raise RuntimeError("refusing to replace a symlink output")


def write_json_atomically(payload: dict[str, Any], destination: Path) -> None:
    """Write validated JSON using a same-directory fsync + replace transaction."""

    _assert_generated_target(destination)
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}.", suffix=".tmp", dir=str(GENERATED_DIR)
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        round_trip = json.loads(temporary.read_text(encoding="utf-8"))
        if round_trip != payload:
            raise RuntimeError("atomic snapshot verification failed")
        os.replace(str(temporary), str(destination))
        directory_fd = os.open(str(GENERATED_DIR), os.O_RDONLY)
        try:
            try:
                os.fsync(directory_fd)
            except OSError:
                # The atomic replace has already committed in the live
                # filesystem. Do not falsely report that the previous file
                # remains; the pre-replace file and payload were both verified.
                pass
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_snapshot(path: Path | None = None) -> dict[str, Any] | None:
    path = SNAPSHOT_PATH if path is None else path
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise SnapshotValidationError("snapshot storage is unsafe")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "generation_id" not in payload:
        raise SnapshotValidationError("stored snapshot generation_id is missing")
    return validate_snapshot(payload)


def load_collection_snapshot(path: Path | None = None) -> dict[str, Any] | None:
    path = COLLECTION_SNAPSHOT_PATH if path is None else path
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise SnapshotValidationError("collection snapshot storage is unsafe")
    payload = json.loads(path.read_text(encoding="utf-8"))
    try:
        return validate_collection_payload(payload)
    except ValueError as exc:
        raise SnapshotValidationError("collection snapshot is invalid") from exc


def _load_project_module(path: Path, module_name: str) -> Any:
    """Load one fixed project-local module without adding its directory to sys.path."""

    if not path.is_file() or path.is_symlink():
        raise SnapshotValidationError("candidate catalog helper is unavailable")
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise SnapshotValidationError("candidate catalog helper cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    previous_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous_dont_write_bytecode
    return module


def validate_candidate_payload(payload: Any) -> dict[str, Any]:
    """Run the independent candidate data contract against an in-memory payload."""

    try:
        with _CANDIDATE_MODULE_LOCK:
            checker = _load_project_module(
                CANDIDATE_CHECKER_PATH,
                "ai_toolbox_candidate_checker",
            )
            result = checker.main(
                payload=payload,
                require_data_js=False,
                quiet=True,
            )
    except Exception as exc:
        raise SnapshotValidationError("candidate catalog validation failed") from exc
    if result != 0:
        raise SnapshotValidationError("candidate catalog violates its data contract")
    return payload


def build_candidate_payload(source_root: Path | None = None) -> dict[str, Any]:
    """Build the legacy candidate catalog in process without writing legacy files."""

    try:
        selected_root = (source_root or DEFAULT_SOURCE_ROOT).expanduser().absolute()
        with _CANDIDATE_MODULE_LOCK:
            builder = _load_project_module(
                CANDIDATE_BUILDER_PATH,
                "ai_toolbox_candidate_builder",
            )
            payload = builder.build_payload(source_root=selected_root)
            payload = validate_candidate_payload(payload)
    except Exception as exc:
        raise SnapshotValidationError("candidate catalog scan failed") from exc
    return payload


def load_candidate_snapshot(path: Path | None = None) -> dict[str, Any] | None:
    path = CANDIDATE_SNAPSHOT_PATH if path is None else path
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise SnapshotValidationError("candidate catalog storage is unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotValidationError("candidate catalog storage is invalid") from exc
    return validate_candidate_payload(payload)


def load_candidate_catalog() -> dict[str, Any] | None:
    """Return the newest valid web or legacy candidate catalog."""

    legacy_path = CANDIDATES_DIR / "data.json"
    candidates: list[tuple[int, dict[str, Any]]] = []
    generated = load_candidate_snapshot()
    if generated is not None:
        try:
            candidates.append((CANDIDATE_SNAPSHOT_PATH.stat().st_mtime_ns, generated))
        except OSError as exc:
            raise SnapshotValidationError("candidate catalog storage is invalid") from exc
    legacy_invalid = False
    if legacy_path.exists():
        try:
            if legacy_path.is_symlink() or not legacy_path.is_file():
                raise SnapshotValidationError("candidate catalog storage is unsafe")
            payload = json.loads(legacy_path.read_text(encoding="utf-8"))
            candidates.append(
                (legacy_path.stat().st_mtime_ns, validate_candidate_payload(payload))
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, SnapshotValidationError):
            legacy_invalid = True
    if candidates:
        return max(candidates, key=lambda row: row[0])[1]
    if legacy_invalid:
        raise SnapshotValidationError("no valid candidate catalog is available")
    return None


def _display_path(path: Path) -> str:
    """Return a user-facing path without baking the account name into UI state."""

    home = Path.home().absolute()
    try:
        relative = path.relative_to(home)
    except ValueError:
        return path.as_posix()
    if not relative.parts:
        return "~"
    return f"~/{relative.as_posix()}"


def _directory_identity_without_symlinks(path: Path) -> tuple[int, int]:
    """Open every path component with O_NOFOLLOW and return the root identity."""

    if not path.is_absolute():
        raise FolderSelectionError("folder_path_not_absolute")
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC

    descriptor = os.open(os.sep, flags)
    try:
        for part in path.parts[1:]:
            if part in {"", ".", ".."}:
                raise FolderSelectionError("folder_path_not_canonical")
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as exc:
                raise FolderSelectionError("folder_path_unsafe_or_unreadable") from exc
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise FolderSelectionError("folder_path_not_directory")
        return info.st_dev, info.st_ino
    finally:
        os.close(descriptor)


def validate_selected_source_root(value: Any) -> dict[str, Any]:
    """Validate one picker result against broad and sensitive local boundaries."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise FolderSelectionError("folder_path_invalid")
    raw = Path(value).expanduser()
    if not raw.is_absolute() or any(part in {".", ".."} for part in raw.parts):
        raise FolderSelectionError("folder_path_not_canonical")
    path = Path(os.path.normpath(str(raw)))

    home = Path.home().absolute()
    broad_roots = {
        Path(os.sep),
        home,
        home / "Desktop",
        home / "Documents",
        home / "Downloads",
        home / "Library",
    }
    if path in broad_roots:
        raise FolderSelectionError("folder_root_too_broad")

    project_and_host_ranges = {
        ROOT.absolute(),
        GENERATED_DIR.absolute(),
        home / ".codex",
        home / ".claude",
        home / ".hermes",
        home / ".workbuddy",
        home / ".antigravity",
        home / ".agents",
    }
    for protected in project_and_host_ranges:
        if _is_relative_to(path, protected) or _is_relative_to(protected, path):
            raise FolderSelectionError("folder_root_protected")

    sensitive_ranges = {
        home / ".ssh",
        home / ".gnupg",
        home / ".aws",
        home / ".kube",
        home / ".docker",
        home / ".config",
        home / ".local",
        home / "Library" / "Keychains",
        home / "Library" / "Application Support",
        home / "Library" / "Containers",
        home / "Library" / "Group Containers",
    }
    if any(_is_relative_to(path, protected) for protected in sensitive_ranges):
        raise FolderSelectionError("folder_root_sensitive")

    device, inode = _directory_identity_without_symlinks(path)
    return {
        "path": path,
        "display_path": _display_path(path),
        "device": device,
        "inode": inode,
    }


def validate_selected_project_root(value: Any) -> dict[str, Any]:
    """Validate a picker-selected project inside the frozen Documents root."""

    selected = validate_selected_source_root(value)
    root = PROJECT_SKILL_PRODUCTION_ROOT.absolute()
    path = selected["path"]
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise FolderSelectionError("project_folder_outside_observation_root") from None
    if not relative.parts or any(part in PROJECT_SKILL_EXCLUDED_NAMES for part in relative.parts):
        raise FolderSelectionError("project_folder_not_scannable")
    return {**selected, "relative_path": relative.as_posix()}


def build_in_memory_project_skill_preview(
    selected_path: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Observe one picker-selected project in memory without Registry or disk writes."""

    boundary = load_project_skill_runtime_boundary(PROJECT_SKILL_BOUNDARY_PATH)
    validate_project_skill_runtime_boundary(boundary, required_connection="api")
    selected = validate_selected_project_root(str(selected_path))
    identity = (selected["device"], selected["inode"])
    if expected_identity is not None and identity != expected_identity:
        raise FolderSelectionError("folder_identity_changed")
    relative_path = selected["relative_path"]
    project_id = "session-project:" + hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:16]
    confirmed_on = datetime.now(timezone.utc).date().isoformat()
    payload = build_project_skill_snapshot(
        root=PROJECT_SKILL_PRODUCTION_ROOT,
        projects_registry={
            "schema_version": 1,
            "registry_id": "project-skill-session-projects-v1",
            "observation_boundary_ref": PROJECT_SKILL_BOUNDARY_ID,
            "confirmed_on": confirmed_on,
            "projects": [
                {
                    "project_id": project_id,
                    "relative_path": relative_path,
                    "classification": "project",
                    "display_name": selected_path.name,
                }
            ],
        },
        associations_registry={
            "schema_version": 1,
            "registry_id": "project-skill-session-associations-v1",
            "observation_boundary_ref": PROJECT_SKILL_BOUNDARY_ID,
            "confirmed_on": confirmed_on,
            "associations": [],
        },
        discover_candidates=False,
    )
    selected_after = validate_selected_project_root(str(selected_path))
    if (selected_after["device"], selected_after["inode"]) != identity:
        raise FolderSelectionError("folder_identity_changed")
    if payload.get("candidates") != [] or len(payload.get("projects", [])) != 1:
        raise ProjectSkillScanRejected("session_project_scope_mismatch")
    return payload


def native_folder_picker_status() -> dict[str, Any]:
    """Report whether the fixed same-Python native picker helper can be invoked."""

    if (
        not NATIVE_FOLDER_PICKER_PATH.is_file()
        or NATIVE_FOLDER_PICKER_PATH.is_symlink()
        or not Path(sys.executable).is_file()
    ):
        return {
            "available": False,
            "provider": "unavailable",
            "reason": "picker_helper_unavailable",
        }
    if sys.platform == "darwin":
        if (
            not MACOS_OSASCRIPT_PATH.is_file()
            or MACOS_OSASCRIPT_PATH.is_symlink()
            or not os.access(MACOS_OSASCRIPT_PATH, os.X_OK)
        ):
            return {
                "available": False,
                "provider": "macos-standard-additions",
                "reason": "macos_picker_unavailable",
            }
        return {
            "available": True,
            "provider": "macos-standard-additions",
            "reason": None,
        }
    try:
        tkinter_spec = importlib.util.find_spec("tkinter")
    except (ImportError, AttributeError, ValueError):
        tkinter_spec = None
    if tkinter_spec is None:
        return {
            "available": False,
            "provider": "tkinter",
            "reason": "tkinter_unavailable",
        }
    return {"available": True, "provider": "tkinter", "reason": None}


def run_native_folder_picker() -> dict[str, Any]:
    """Invoke only the fixed project helper with this exact Python executable."""

    status = native_folder_picker_status()
    if not status["available"]:
        raise FolderSelectionError(status["reason"] or "picker_unavailable")
    try:
        completed = subprocess.run(
            [sys.executable, str(NATIVE_FOLDER_PICKER_PATH)],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=FOLDER_PICKER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FolderSelectionError("picker_failed") from exc
    if completed.returncode != 0 or len(completed.stdout) > 4096:
        raise FolderSelectionError("picker_failed")
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise FolderSelectionError("picker_response_invalid") from exc
    if payload == {"selected": False, "cancelled": True}:
        return payload
    if set(payload) != {"selected", "cancelled", "path"}:
        raise FolderSelectionError("picker_response_invalid")
    if payload.get("selected") is not True or payload.get("cancelled") is not False:
        raise FolderSelectionError("picker_response_invalid")
    if not isinstance(payload.get("path"), str):
        raise FolderSelectionError("picker_response_invalid")
    return payload


def _collection_payload_is_promotable(
    payload: dict[str, Any],
    *,
    stable: bool,
    input_state: dict[str, Any],
) -> bool:
    summary = payload.get("summary", {})
    scan_errors = payload.get("scan_errors", [])
    warnings_are_nonfatal = isinstance(scan_errors, list) and all(
        isinstance(error, dict)
        and set(error) in ({"code", "path"}, {"code", "path", "detail"})
        and error.get("code") == "symlink_skipped"
        and isinstance(error.get("path"), str)
        and bool(error["path"])
        and (
            "detail" not in error
            or (isinstance(error["detail"], str) and bool(error["detail"]))
        )
        for error in scan_errors
    )
    return (
        stable
        and input_state.get("status") == "observed"
        and isinstance(input_state.get("signature"), str)
        and SHA256_RE.fullmatch(input_state["signature"]) is not None
        and warnings_are_nonfatal
        and summary.get("scan_error_count") == len(scan_errors)
    )


def build_in_memory_source_pair(
    source_root: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Build collection and candidate views atomically for one selected root."""

    selected = validate_selected_source_root(str(source_root))
    identity = (selected["device"], selected["inode"])
    if expected_identity is not None and identity != expected_identity:
        raise FolderSelectionError("folder_identity_changed")

    before = build_collection_input_state(source_root=selected["path"])
    collection = validate_collection_payload(
        build_collection_payload(source_root=selected["path"])
    )
    candidate = build_candidate_payload(source_root=selected["path"])
    after = build_collection_input_state(source_root=selected["path"])
    selected_after = validate_selected_source_root(str(selected["path"]))
    after_identity = (selected_after["device"], selected_after["inode"])
    stable = (
        before.get("status") == "observed"
        and after.get("status") == "observed"
        and before.get("signature") == after.get("signature")
        and identity == after_identity
    )
    if not _collection_payload_is_promotable(
        collection,
        stable=stable,
        input_state=after,
    ):
        raise CollectionScanIncomplete(collection, "collection_source_unstable_or_incomplete")
    expected_path = selected["path"].as_posix()
    if collection.get("source_root") != expected_path or candidate.get("source_dir") != expected_path:
        raise FolderSelectionError("folder_source_mismatch")
    return {
        "path": selected["path"],
        "display_path": selected["display_path"],
        "device": identity[0],
        "inode": identity[1],
        "input_signature": after["signature"],
        "collection_snapshot": collection,
        "candidate_catalog": candidate,
    }


def _collection_snapshot_fingerprint(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_collection_signature(path: Path | None = None) -> dict[str, Any] | None:
    """Load the optional change-check sidecar; invalid data means "check again"."""

    path = COLLECTION_SIGNATURE_PATH if path is None else path
    if not path.exists() or path.is_symlink() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        receipt = _assert_exact_object(
            payload,
            allowed={
                "schema_version",
                "input_signature",
                "snapshot_fingerprint",
                "generated_at",
            },
            label="collection signature",
        )
        if receipt.get("schema_version") != COLLECTION_SIGNATURE_SCHEMA_VERSION:
            return None
        _assert_sha256(receipt.get("input_signature"), label="collection input signature")
        _assert_sha256(
            receipt.get("snapshot_fingerprint"),
            label="collection snapshot fingerprint",
        )
        _parse_datetime(receipt.get("generated_at"), label="collection signature generated_at")
        return receipt
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SnapshotValidationError):
        return None


def load_health_attempt(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return only a receipt that is current relative to the stored snapshot."""

    if not LAST_ATTEMPT_PATH.exists() and not LAST_ATTEMPT_PATH.is_symlink():
        return None
    if LAST_ATTEMPT_PATH.is_symlink() or not LAST_ATTEMPT_PATH.is_file():
        return {"status": "unavailable", "reason": "attempt_receipt_invalid"}
    try:
        raw = json.loads(LAST_ATTEMPT_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"status": "unavailable", "reason": "attempt_receipt_unreadable"}
    if not isinstance(raw, dict):
        return {"status": "unavailable", "reason": "attempt_receipt_invalid"}
    if snapshot is None:
        return raw
    if raw.get("generation_id") == snapshot.get("generation_id"):
        return {**raw, "matches_snapshot": True}
    attempt_time = raw.get("generated_at")
    snapshot_time = snapshot.get("generated_at")
    if raw.get("status") in {"failed", "incomplete"}:
        try:
            attempt_datetime = _parse_datetime(
                attempt_time, label="attempt receipt generated_at"
            )
            snapshot_datetime = _parse_datetime(
                snapshot_time, label="snapshot generated_at"
            )
        except SnapshotValidationError:
            attempt_datetime = None
            snapshot_datetime = None
        if (
            attempt_datetime is not None
            and snapshot_datetime is not None
            and attempt_datetime > snapshot_datetime
        ):
            return {**raw, "matches_snapshot": False}
    return {
        "status": "unavailable",
        "reason": "receipt_not_persisted_for_current_snapshot",
        "snapshot_generation_id": snapshot.get("generation_id"),
    }


@contextmanager
def refresh_guard() -> Iterator[None]:
    _assert_plain_directory(GENERATED_DIR, create=True)
    if LOCK_PATH.exists() and LOCK_PATH.is_symlink():
        raise RuntimeError("refresh lock must not be a symlink")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(LOCK_PATH), flags, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RefreshBusy("another process is refreshing") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def collection_refresh_guard() -> Iterator[None]:
    _assert_plain_directory(GENERATED_DIR, create=True)
    if COLLECTION_LOCK_PATH.exists() and COLLECTION_LOCK_PATH.is_symlink():
        raise RuntimeError("collection refresh lock must not be a symlink")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(COLLECTION_LOCK_PATH), flags, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RefreshBusy("another collection scan is refreshing") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def scan_and_persist(home: Path | None = None) -> tuple[bool, dict[str, Any]]:
    """Run one in-process read-only scan and persist only inside generated/."""

    with refresh_guard():
        payload = validate_scan_payload(build_toolbox_payload(home=home))
        payload = _with_generation_id(payload)
        payload = validate_snapshot(payload)
        complete = snapshot_is_complete(payload)
        attempt = {
            "status": "success" if complete else "incomplete",
            "generated_at": payload["generated_at"],
            "generation_id": payload["generation_id"],
            "summary": payload["summary"],
            "scan_errors": payload["scan_errors"],
            "incomplete_coverage": [
                row
                for row in payload["scan_scope"].get("coverage", [])
                if row.get("status") in INCOMPLETE_COVERAGE_STATES
            ],
            "incomplete_roots": [
                row
                for row in payload["scan_scope"].get("roots", [])
                if row.get("status") in INCOMPLETE_SCOPE_STATES
            ],
        }
        if complete:
            write_json_atomically(payload, SNAPSHOT_PATH)
            try:
                write_json_atomically(attempt, LAST_ATTEMPT_PATH)
            except Exception:
                # The committed snapshot is the success truth. A secondary
                # receipt failure must not turn a completed refresh into a
                # false HTTP failure claiming the snapshot was not saved.
                pass
        else:
            write_json_atomically(attempt, LAST_ATTEMPT_PATH)
        return complete, payload


def _scan_collection_under_guard(
    *,
    source_root: Path | None = None,
    host_roots: dict[str, Path] | None = None,
) -> tuple[dict[str, Any], bool, dict[str, Any]]:
    """Build a collection snapshot and bind a stable input signature to it."""

    _assert_generated_target(COLLECTION_SNAPSHOT_PATH)
    _assert_generated_target(COLLECTION_SIGNATURE_PATH)
    effective_root = source_root or DEFAULT_SOURCE_ROOT
    before = build_collection_input_state(
        source_root=effective_root,
        host_roots=host_roots,
    )
    payload = validate_collection_payload(
        build_collection_payload(source_root=source_root, host_roots=host_roots)
    )
    after = build_collection_input_state(
        source_root=effective_root,
        host_roots=host_roots,
    )
    stable = (
        before.get("status") == "observed"
        and after.get("status") == "observed"
        and before.get("signature") == after.get("signature")
    )
    if not stable:
        retry_before = after
        payload = validate_collection_payload(
            build_collection_payload(source_root=source_root, host_roots=host_roots)
        )
        after = build_collection_input_state(
            source_root=effective_root,
            host_roots=host_roots,
        )
        stable = (
            retry_before.get("status") == "observed"
            and after.get("status") == "observed"
            and retry_before.get("signature") == after.get("signature")
        )

    promotable = _collection_payload_is_promotable(
        payload,
        stable=stable,
        input_state=after,
    )
    if not promotable:
        return payload, False, after

    write_json_atomically(payload, COLLECTION_SNAPSHOT_PATH)
    try:
        write_json_atomically(
            {
                "schema_version": COLLECTION_SIGNATURE_SCHEMA_VERSION,
                "input_signature": after["signature"],
                "snapshot_fingerprint": _collection_snapshot_fingerprint(payload),
                "generated_at": payload["generated_at"],
            },
            COLLECTION_SIGNATURE_PATH,
        )
    except Exception:
        # The stable, error-free snapshot has already committed. Without a
        # matching sidecar the next page entry safely checks/scans again.
        pass
    return payload, stable, after


def scan_collection_and_persist(
    *,
    source_root: Path | None = None,
    host_roots: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Force a read-only collection scan and persist only generated artifacts."""

    with collection_refresh_guard():
        payload, stable, after = _scan_collection_under_guard(
            source_root=source_root,
            host_roots=host_roots,
        )
        if not _collection_payload_is_promotable(
            payload,
            stable=stable,
            input_state=after,
        ):
            raise CollectionScanIncomplete(payload, "collection_source_unstable_or_incomplete")
        return payload


def check_collection_and_persist() -> dict[str, Any]:
    """Return the current snapshot, scanning only when bounded inputs changed."""

    with collection_refresh_guard():
        try:
            snapshot = load_collection_snapshot()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, SnapshotValidationError):
            snapshot = None
        receipt = load_collection_signature()
        current = build_collection_input_state(source_root=DEFAULT_SOURCE_ROOT)
        snapshot_fingerprint = (
            _collection_snapshot_fingerprint(snapshot) if snapshot is not None else None
        )
        unchanged = (
            snapshot is not None
            and current.get("status") == "observed"
            and receipt is not None
            and receipt.get("input_signature") == current.get("signature")
            and receipt.get("snapshot_fingerprint") == snapshot_fingerprint
        )
        if unchanged:
            return {
                "status": "unchanged",
                "reason": "input_signature_match",
                "stable": True,
                "snapshot": snapshot,
            }

        payload, stable, after = _scan_collection_under_guard()
        if not _collection_payload_is_promotable(
            payload,
            stable=stable,
            input_state=after,
        ):
            raise CollectionScanIncomplete(payload, "collection_source_unstable_or_incomplete")
        return {
            "status": "refreshed",
            "reason": (
                "collection_inputs_changed"
                if current.get("status") == "observed"
                else "collection_input_state_indeterminate"
            ),
            "stable": stable,
            "snapshot": payload,
        }


def scan_candidate_and_persist() -> dict[str, Any]:
    """Refresh the web candidate catalog while preserving the legacy workbench files."""

    with collection_refresh_guard():
        payload = build_candidate_payload()
        write_json_atomically(payload, CANDIDATE_SNAPSHOT_PATH)
        return payload


def record_failed_attempt() -> None:
    """Best-effort failure receipt; never replace the last valid snapshot."""

    attempt = {
        "status": "failed",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "error": "scan_failed",
    }
    write_json_atomically(attempt, LAST_ATTEMPT_PATH)


def scan_project_skills_and_persist() -> tuple[bool, dict[str, Any]]:
    """Run the fixed-root project Skill scan and persist only its dedicated outputs."""

    boundary = load_project_skill_runtime_boundary(PROJECT_SKILL_BOUNDARY_PATH)
    validate_project_skill_runtime_boundary(boundary, required_connection="api")
    payload = build_project_skill_snapshot()
    store = ProjectSkillStore(PROJECT_SKILL_OUTPUT_ROOT.parents[1])
    complete = store.persist_scan_result(payload)
    if complete:
        persisted = store.load_snapshot()
        if persisted is None:
            raise ProjectSkillPersistenceError("project Skill snapshot missing after promotion")
        if store.last_receipt_error is not None:
            raise ProjectSkillReceiptUnavailable(
                "project Skill snapshot committed but attempt receipt was unavailable"
            )
        return True, persisted
    return False, payload


def load_project_skill_api_view() -> dict[str, Any] | None:
    """Load only validated persisted project-Skill data; never trigger a scan."""

    boundary = load_project_skill_runtime_boundary(PROJECT_SKILL_BOUNDARY_PATH)
    validate_project_skill_runtime_boundary(boundary, required_connection="api")
    store = ProjectSkillStore(PROJECT_SKILL_OUTPUT_ROOT.parents[1])
    snapshot = store.load_snapshot()
    auxiliary_errors: list[str] = []
    try:
        last_attempt = store.load_last_attempt()
    except ProjectSkillPersistenceError:
        if snapshot is None:
            raise
        last_attempt = None
        auxiliary_errors.append("last_attempt_invalid")
    if snapshot is None and last_attempt is None:
        return None
    if snapshot is None:
        return {
            "snapshot": None,
            "last_attempt": last_attempt,
            "report": {"available": False, "generation_id": None, "content": None},
            "integrity": {"degraded": False, "errors": []},
        }
    if (
        last_attempt is not None
        and last_attempt["complete_snapshot_generation_id"] != snapshot["generation_id"]
    ):
        last_attempt = None
        auxiliary_errors.append("last_attempt_stale")
    elif last_attempt is None:
        auxiliary_errors.append("last_attempt_missing")
    try:
        markdown = store.load_markdown(expected_snapshot=snapshot)
    except ProjectSkillPersistenceError:
        markdown = None
        auxiliary_errors.append("report_invalid")
    if markdown is None and "report_invalid" not in auxiliary_errors:
        auxiliary_errors.append("report_missing")
    return {
        "snapshot": snapshot,
        "last_attempt": last_attempt,
        "report": {
            "available": markdown is not None,
            "generation_id": snapshot["generation_id"] if markdown is not None else None,
            "content": markdown,
        },
        "integrity": {"degraded": bool(auxiliary_errors), "errors": auxiliary_errors},
    }


def _loopback_name(value: str) -> str:
    host = value.strip().lower()
    if host.startswith("[") and "]" in host:
        return host[1 : host.index("]")]
    return host.split(":", 1)[0]


def _is_loopback_host(value: str) -> bool:
    return _loopback_name(value) in LOOPBACK_HOSTS


class ToolboxServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], handler: Any) -> None:
        super().__init__(server_address, handler)
        self.csrf_token = secrets.token_urlsafe(24)
        self.refresh_lock = threading.Lock()
        self.project_skill_refresh_lock = threading.Lock()
        self.source_state_lock = threading.RLock()
        self.folder_tokens: dict[str, dict[str, Any]] = {}
        self.folder_picker = native_folder_picker_status()
        self.source_session: dict[str, Any] = {
            "mode": "default",
            "display_path": _display_path(DEFAULT_SOURCE_ROOT.absolute()),
            "source_key": "default",
            "path": DEFAULT_SOURCE_ROOT.absolute(),
            "device": None,
            "inode": None,
            "input_signature": None,
            "collection_snapshot": None,
            "candidate_catalog": None,
        }

    def source_session_payload(self) -> dict[str, Any]:
        with self.source_state_lock:
            return {
                "mode": self.source_session["mode"],
                "display_path": self.source_session["display_path"],
                "source_key": self.source_session["source_key"],
            }

    def temporary_source(self) -> dict[str, Any] | None:
        with self.source_state_lock:
            if self.source_session["mode"] != "temporary":
                return None
            return dict(self.source_session)

    def issue_folder_token(
        self,
        selection: dict[str, Any],
        *,
        target: str = FOLDER_SELECTION_TARGET,
    ) -> tuple[str, int]:
        now = time.monotonic()
        with self.source_state_lock:
            self.folder_tokens = {
                token: record
                for token, record in self.folder_tokens.items()
                if record["expires_at"] > now
            }
            if len(self.folder_tokens) >= 16:
                oldest = min(
                    self.folder_tokens,
                    key=lambda candidate: self.folder_tokens[candidate]["expires_at"],
                )
                self.folder_tokens.pop(oldest, None)
            token = secrets.token_urlsafe(32)
            self.folder_tokens[token] = {
                "target": target,
                "path": selection["path"],
                "display_path": selection["display_path"],
                "device": selection["device"],
                "inode": selection["inode"],
                "expires_at": now + FOLDER_SELECTION_TTL_SECONDS,
            }
        return token, FOLDER_SELECTION_TTL_SECONDS

    def consume_folder_token(
        self,
        token: str,
        *,
        expected_target: str = FOLDER_SELECTION_TARGET,
    ) -> dict[str, Any]:
        now = time.monotonic()
        with self.source_state_lock:
            record = self.folder_tokens.pop(token, None)
        if record is None:
            raise FolderSelectionError("selection_token_invalid_or_replayed")
        if record.get("target") != expected_target:
            raise FolderSelectionError("selection_token_target_mismatch")
        if record.get("expires_at", 0) <= now:
            raise FolderSelectionError("selection_token_expired")
        return record

    def activate_temporary_source(self, pair: dict[str, Any]) -> None:
        with self.source_state_lock:
            source_key = secrets.token_urlsafe(18)
            if (
                self.source_session["mode"] == "temporary"
                and self.source_session["path"] == pair["path"]
                and self.source_session["device"] == pair["device"]
                and self.source_session["inode"] == pair["inode"]
            ):
                source_key = self.source_session["source_key"]
            self.source_session = {
                "mode": "temporary",
                "display_path": pair["display_path"],
                "source_key": source_key,
                "path": pair["path"],
                "device": pair["device"],
                "inode": pair["inode"],
                "input_signature": pair["input_signature"],
                "collection_snapshot": pair["collection_snapshot"],
                "candidate_catalog": pair["candidate_catalog"],
            }

    def restore_default_source(self) -> None:
        with self.source_state_lock:
            self.source_session = {
                "mode": "default",
                "display_path": _display_path(DEFAULT_SOURCE_ROOT.absolute()),
                "source_key": "default",
                "path": DEFAULT_SOURCE_ROOT.absolute(),
                "device": None,
                "inode": None,
                "input_signature": None,
                "collection_snapshot": None,
                "candidate_catalog": None,
            }
            self.folder_tokens.clear()


def _load_default_source_pair() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Load the two persisted default views without scanning or mutating state."""

    return load_collection_snapshot(), load_candidate_catalog()


def _temporary_source_check(server: ToolboxServer) -> dict[str, Any]:
    """Check one in-memory source and rebuild both views only when it changed."""

    session = server.temporary_source()
    if session is None:
        raise RuntimeError("temporary source is not active")
    selected = validate_selected_source_root(str(session["path"]))
    if (selected["device"], selected["inode"]) != (
        session["device"],
        session["inode"],
    ):
        raise FolderSelectionError("folder_identity_changed")
    current = build_collection_input_state(source_root=session["path"])
    if (
        current.get("status") == "observed"
        and current.get("signature") == session.get("input_signature")
    ):
        return {
            "status": "unchanged",
            "reason": "input_signature_match",
            "stable": True,
            "snapshot": session["collection_snapshot"],
        }
    pair = build_in_memory_source_pair(
        session["path"],
        expected_identity=(session["device"], session["inode"]),
    )
    server.activate_temporary_source(pair)
    return {
        "status": "refreshed",
        "reason": (
            "collection_inputs_changed"
            if current.get("status") == "observed"
            else "collection_input_state_indeterminate"
        ),
        "stable": True,
        "snapshot": pair["collection_snapshot"],
    }


def _refresh_temporary_source(server: ToolboxServer) -> dict[str, Any]:
    """Atomically rebuild both in-memory views for the active selected root."""

    session = server.temporary_source()
    if session is None:
        raise RuntimeError("temporary source is not active")
    pair = build_in_memory_source_pair(
        session["path"],
        expected_identity=(session["device"], session["inode"]),
    )
    server.activate_temporary_source(pair)
    return pair


class ToolboxHandler(SimpleHTTPRequestHandler):
    server_version = "AI-Toolbox/0.1"

    POST_ROUTES = frozenset(
        {
            "/api/refresh",
            "/api/collections/refresh",
            "/api/collections/check",
            "/api/candidates/refresh",
            "/api/project-skills/refresh",
            "/api/project-skills/folder-selection/confirm",
            "/api/folder-picker",
            "/api/folder-selection/confirm",
            "/api/folder-source/restore",
        }
    )

    def log_message(self, format_string: str, *args: Any) -> None:
        sys.stdout.write("[workbench] " + (format_string % args) + "\n")

    def end_headers(self) -> None:
        path = self._request_path() or ""
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if path.startswith("/api/") or path.startswith("/candidates/"):
            self.send_header("Cache-Control", "no-store")
        elif path.startswith("/assets/"):
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        super().end_headers()

    def translate_path(self, path: str) -> str:
        translated = Path(super().translate_path(path))
        base = Path(self.directory).absolute()
        try:
            relative = translated.relative_to(base)
        except ValueError:
            return str(base / ".ai-toolbox-blocked-static")
        current = base
        if current.is_symlink():
            return str(base / ".ai-toolbox-blocked-static")
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return str(base / ".ai-toolbox-blocked-static")
        return str(translated)

    def list_directory(self, path: str) -> None:
        self.send_error(HTTPStatus.NOT_FOUND)
        return None

    def _request_path(self) -> str | None:
        try:
            return urlsplit(self.path).path
        except ValueError:
            return None

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _request_host_is_safe(self) -> bool:
        return _is_loopback_host(self.headers.get("Host", ""))

    def _origin_is_safe(self) -> bool:
        origin = self.headers.get("Origin", "")
        if not origin:
            return False
        try:
            parsed = urlparse(origin)
            parsed_port = parsed.port or 80
        except ValueError:
            return False
        if parsed.scheme != "http" or parsed.hostname not in LOOPBACK_HOSTS:
            return False
        expected_port = self.server.server_address[1]
        return parsed_port == expected_port

    def _api_preflight(self) -> bool:
        if self._request_host_is_safe():
            return True
        self._json(HTTPStatus.FORBIDDEN, {"error": "unsafe_host"})
        return False

    def _storage_error(self, resource: str) -> None:
        self._json(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            {"ok": False, "error": "snapshot_invalid", "resource": resource},
        )

    def _health(self) -> None:
        errors: list[str] = []
        try:
            snapshot = load_snapshot()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, SnapshotValidationError):
            snapshot = None
            errors.append("snapshot_invalid")
        temporary = self.server.temporary_source()
        if temporary is None:
            try:
                collection_snapshot = load_collection_snapshot()
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, SnapshotValidationError):
                collection_snapshot = None
                errors.append("collection_snapshot_invalid")
            try:
                candidate_snapshot = load_candidate_snapshot()
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, SnapshotValidationError):
                candidate_snapshot = None
                errors.append("candidate_snapshot_invalid")
        else:
            collection_snapshot = temporary["collection_snapshot"]
            candidate_snapshot = temporary["candidate_catalog"]
        attempt = load_health_attempt(snapshot)
        if isinstance(attempt, dict) and attempt.get("reason") in {
            "attempt_receipt_unreadable",
            "attempt_receipt_invalid",
        }:
            errors.append("last_attempt_invalid")
            attempt = {**attempt, "error": "snapshot_invalid"}
        snapshot_health = {
            "available": snapshot is not None,
            "generated_at": snapshot.get("generated_at") if snapshot else None,
            "generation_id": snapshot.get("generation_id") if snapshot else None,
        }
        if "snapshot_invalid" in errors:
            snapshot_health["error"] = "snapshot_invalid"
        try:
            project_skill_view = load_project_skill_api_view()
        except (ProjectSkillScanRejected, ProjectSkillPersistenceError, OSError):
            project_skill_view = None
            errors.append("project_skill_snapshot_invalid")
        project_skill_snapshot = (
            project_skill_view.get("snapshot") if project_skill_view is not None else None
        )
        project_skill_health = {
            "available": project_skill_snapshot is not None,
            "generated_at": (
                project_skill_snapshot.get("generated_at") if project_skill_snapshot else None
            ),
            "generation_id": (
                project_skill_snapshot.get("generation_id") if project_skill_snapshot else None
            ),
            "refresh_state": (
                "running" if self.server.project_skill_refresh_lock.locked() else "idle"
            ),
            "integrity": (
                project_skill_view.get("integrity")
                if project_skill_view is not None
                else {"degraded": False, "errors": []}
            ),
        }
        if project_skill_health["integrity"]["degraded"]:
            errors.append("project_skill_auxiliary_invalid")
        if "project_skill_snapshot_invalid" in errors:
            project_skill_health["error"] = "snapshot_invalid"
        collection_health = {
            "available": collection_snapshot is not None,
            "generated_at": (
                collection_snapshot.get("generated_at")
                if collection_snapshot
                else None
            ),
            "source_count": (
                collection_snapshot.get("summary", {}).get("source_count")
                if collection_snapshot
                else 0
            ),
        }
        if "collection_snapshot_invalid" in errors:
            collection_health["error"] = "snapshot_invalid"
        candidate_health = {
            "available": candidate_snapshot is not None,
            "generated_at": (
                candidate_snapshot.get("generated_at")
                if candidate_snapshot
                else None
            ),
            "skill_count": (
                candidate_snapshot.get("stats", {}).get("skills")
                if candidate_snapshot
                else 0
            ),
        }
        if "candidate_snapshot_invalid" in errors:
            candidate_health["error"] = "snapshot_invalid"
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "degraded": bool(errors),
                "errors": errors,
                "app": "ai-toolbox-workbench",
                "api_version": API_VERSION,
                "release_id": RELEASE_ID,
                "mode": "observe-only",
                "refresh_state": (
                    "running" if self.server.refresh_lock.locked() else "idle"
                ),
                "snapshot": snapshot_health,
                "project_skill_snapshot": project_skill_health,
                "collection_snapshot": collection_health,
                "candidate_snapshot": candidate_health,
                "last_attempt": attempt,
                "folder_picker": dict(self.server.folder_picker),
                "source_session": self.server.source_session_payload(),
                "capabilities": {
                    "scan": True,
                    "collection_scan": True,
                    "candidate_scan": True,
                    "project_skill_scan": True,
                    "host_mutation": False,
                    "model_calls": False,
                    "external_network": False,
                    "background_watch": False,
                },
                "csrf_token": self.server.csrf_token,
            },
        )

    def do_GET(self) -> None:
        if not self._api_preflight():
            return
        path = self._request_path()
        if path is None:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_path"})
            return
        if path == "/candidates/data.json":
            temporary = self.server.temporary_source()
            try:
                candidate_catalog = (
                    temporary["candidate_catalog"]
                    if temporary is not None
                    else load_candidate_catalog()
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, SnapshotValidationError):
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "candidate_catalog_invalid", "message": "候选数据不可用"},
                )
                return
            if candidate_catalog is None:
                self._json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "candidate_catalog_missing", "message": "尚未建立候选数据"},
                )
            else:
                self._json(HTTPStatus.OK, candidate_catalog)
            return
        if path == "/api/health":
            self._health()
            return
        if path == "/api/snapshot":
            try:
                snapshot = load_snapshot()
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, SnapshotValidationError):
                self._storage_error("snapshot")
                return
            if snapshot is None:
                self._json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "snapshot_missing", "message": "尚未执行只读观察"},
                )
            else:
                self._json(HTTPStatus.OK, snapshot)
            return
        if path == "/api/project-skills":
            try:
                view = load_project_skill_api_view()
            except ProjectSkillScanRejected:
                self._json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "project_skill_contract_unavailable"},
                )
                return
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ProjectSkillPersistenceError):
                self._storage_error("project_skill_snapshot")
                return
            except Exception:
                self._storage_error("project_skill_view")
                return
            if view is None:
                self._json(
                    HTTPStatus.NOT_FOUND,
                    {
                        "error": "project_skill_snapshot_missing",
                        "message": "尚未建立项目 Skill 观察快照",
                    },
                )
            else:
                self._json(HTTPStatus.OK, view)
            return
        if path == "/api/collections":
            temporary = self.server.temporary_source()
            try:
                collection_snapshot = (
                    temporary["collection_snapshot"]
                    if temporary is not None
                    else load_collection_snapshot()
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, SnapshotValidationError):
                self._storage_error("collection_snapshot")
                return
            if collection_snapshot is None:
                self._json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "collection_snapshot_missing", "message": "尚未建立全部收藏索引"},
                )
            else:
                self._json(HTTPStatus.OK, collection_snapshot)
            return
        if path in self.POST_ROUTES:
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})
            return
        if path.startswith("/api/") or path.startswith("/candidates/"):
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        super().do_GET()

    def do_HEAD(self) -> None:
        if not self._api_preflight():
            return
        path = self._request_path()
        if path is None:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_path"})
            return
        if path.startswith("/api/") or path == "/candidates/data.json":
            self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})
            return
        if path.startswith("/candidates/"):
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        super().do_HEAD()

    def do_POST(self) -> None:
        if not self._api_preflight():
            return
        path = self._request_path()
        if path is None:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_path"})
            return
        if path not in self.POST_ROUTES:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if not self._origin_is_safe():
            if self._request_host_is_safe():
                self._json(HTTPStatus.FORBIDDEN, {"error": "unsafe_origin"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "content_type_required"})
            return
        if self.headers.get("X-AI-Toolbox-CSRF") != self.server.csrf_token:
            self._json(HTTPStatus.FORBIDDEN, {"error": "invalid_csrf"})
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "unsupported_transfer_encoding"})
            return
        content_lengths = self.headers.get_all("Content-Length", failobj=[])
        if not content_lengths:
            self._json(HTTPStatus.LENGTH_REQUIRED, {"error": "content_length_required"})
            return
        if len(content_lengths) != 1 or "," in content_lengths[0]:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_content_length"})
            return
        try:
            length = int(content_lengths[0])
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_content_length"})
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "request_too_large"})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
            return
        if path == "/api/folder-picker":
            valid_body = body in (
                {"target": FOLDER_SELECTION_TARGET},
                {"target": PROJECT_SKILL_FOLDER_SELECTION_TARGET},
            )
        elif path in {
            "/api/folder-selection/confirm",
            "/api/project-skills/folder-selection/confirm",
        }:
            valid_body = (
                isinstance(body, dict)
                and set(body) == {"selection_token"}
                and isinstance(body.get("selection_token"), str)
                and 1 <= len(body["selection_token"]) <= 256
            )
        else:
            valid_body = body == {}
        if not valid_body:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "unexpected_fields"})
            return
        refresh_lock = (
            self.server.project_skill_refresh_lock
            if path in {
                "/api/project-skills/refresh",
                "/api/project-skills/folder-selection/confirm",
            }
            else self.server.refresh_lock
        )
        if not refresh_lock.acquire(blocking=False):
            self._json(
                HTTPStatus.CONFLICT,
                {
                    "error": (
                        "project_skill_refresh_in_progress"
                        if path.startswith("/api/project-skills/")
                        else "refresh_in_progress"
                    )
                },
            )
            return
        try:
            if path == "/api/project-skills/refresh":
                try:
                    complete, payload = scan_project_skills_and_persist()
                except ProjectSkillScanRejected:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "project_skill_contract_unavailable"},
                    )
                    return
                except ProjectSkillReceiptUnavailable:
                    self._json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {
                            "error": "project_skill_receipt_unavailable",
                            "message": "完整快照已提交，但本次刷新回执不可用；可通过只读 GET 核对最新快照",
                        },
                    )
                    return
                except Exception:
                    self._json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {
                            "error": "project_skill_scan_failed",
                            "message": "本次项目 Skill 刷新失败；请通过只读 GET 核对最后可用快照",
                        },
                    )
                    return
                if not complete:
                    self._json(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        {
                            "error": "project_skill_scan_incomplete",
                            "message": "本次项目 Skill 观察不完整，上一份有效快照未被覆盖",
                            "scan_status": payload["scan_status"],
                            "generation_id": payload["generation_id"],
                        },
                    )
                    return
                self._json(HTTPStatus.OK, payload)
                return
            try:
                temporary = self.server.temporary_source()
                if path == "/api/folder-picker":
                    picked = run_native_folder_picker()
                    if picked == {"selected": False, "cancelled": True}:
                        self._json(HTTPStatus.OK, picked)
                        return
                    target = body["target"]
                    selection = (
                        validate_selected_project_root(picked["path"])
                        if target == PROJECT_SKILL_FOLDER_SELECTION_TARGET
                        else validate_selected_source_root(picked["path"])
                    )
                    token, expires_in = self.server.issue_folder_token(
                        selection,
                        target=target,
                    )
                    self._json(
                        HTTPStatus.OK,
                        {
                            "selected": True,
                            "selection_token": token,
                            "display_path": selection["display_path"],
                            "expires_in": expires_in,
                        },
                    )
                    return
                if path == "/api/project-skills/folder-selection/confirm":
                    record = self.server.consume_folder_token(
                        body["selection_token"],
                        expected_target=PROJECT_SKILL_FOLDER_SELECTION_TARGET,
                    )
                    payload = build_in_memory_project_skill_preview(
                        record["path"],
                        expected_identity=(record["device"], record["inode"]),
                    )
                    self._json(
                        HTTPStatus.OK,
                        {
                            "snapshot": payload,
                            "selection": {
                                "mode": "temporary",
                                "display_path": record["display_path"],
                            },
                        },
                    )
                    return
                if path == "/api/folder-selection/confirm":
                    record = self.server.consume_folder_token(body["selection_token"])
                    pair = build_in_memory_source_pair(
                        record["path"],
                        expected_identity=(record["device"], record["inode"]),
                    )
                    self.server.activate_temporary_source(pair)
                    self._json(
                        HTTPStatus.OK,
                        {
                            "source_session": self.server.source_session_payload(),
                            "collection_snapshot": pair["collection_snapshot"],
                            "candidate_catalog": pair["candidate_catalog"],
                        },
                    )
                    return
                if path == "/api/folder-source/restore":
                    collection_snapshot, candidate_catalog = _load_default_source_pair()
                    self.server.restore_default_source()
                    self._json(
                        HTTPStatus.OK,
                        {
                            "source_session": self.server.source_session_payload(),
                            "collection_snapshot": collection_snapshot,
                            "candidate_catalog": candidate_catalog,
                        },
                    )
                    return
                if path == "/api/collections/check":
                    payload = (
                        _temporary_source_check(self.server)
                        if temporary is not None
                        else check_collection_and_persist()
                    )
                    complete = True
                elif path == "/api/collections/refresh":
                    if temporary is None:
                        payload = scan_collection_and_persist()
                    else:
                        payload = _refresh_temporary_source(self.server)[
                            "collection_snapshot"
                        ]
                    complete = True
                elif path == "/api/candidates/refresh":
                    if temporary is None:
                        payload = scan_candidate_and_persist()
                    else:
                        payload = _refresh_temporary_source(self.server)[
                            "candidate_catalog"
                        ]
                    complete = True
                else:
                    complete, payload = scan_and_persist()
            except RefreshBusy:
                self._json(HTTPStatus.CONFLICT, {"error": "refresh_in_progress"})
                return
            except CollectionScanIncomplete as exc:
                self._json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {
                        "error": "collection_scan_incomplete",
                        "reason": exc.reason,
                        "message": "本次收藏索引不完整，上一份有效数据未被覆盖",
                        "attempt": {
                            "generated_at": exc.payload.get("generated_at"),
                            "summary": exc.payload.get("summary"),
                            "scan_errors": exc.payload.get("scan_errors"),
                        },
                    },
                )
                return
            except FolderSelectionError as exc:
                if exc.reason == "selection_token_expired":
                    status = HTTPStatus.GONE
                elif exc.reason in {
                    "selection_token_invalid_or_replayed",
                    "selection_token_target_mismatch",
                    "folder_identity_changed",
                }:
                    status = HTTPStatus.CONFLICT
                elif exc.reason in {
                    "picker_helper_unavailable",
                    "macos_picker_unavailable",
                    "tkinter_unavailable",
                    "picker_unavailable",
                    "picker_failed",
                    "picker_response_invalid",
                }:
                    status = HTTPStatus.SERVICE_UNAVAILABLE
                else:
                    status = HTTPStatus.BAD_REQUEST
                self._json(status, {"error": exc.reason})
                return
            except Exception:
                if path == "/api/refresh":
                    try:
                        record_failed_attempt()
                    except Exception:
                        pass
                if path == "/api/folder-source/restore":
                    self._storage_error("default_source")
                    return
                self._json(
                    (
                        HTTPStatus.UNPROCESSABLE_ENTITY
                        if temporary is not None
                        or path in {
                            "/api/folder-selection/confirm",
                            "/api/project-skills/folder-selection/confirm",
                        }
                        else HTTPStatus.INTERNAL_SERVER_ERROR
                    ),
                    {
                        "error": (
                            "selected_project_skill_scan_failed"
                            if path == "/api/project-skills/folder-selection/confirm"
                            else
                            "selected_source_scan_failed"
                            if temporary is not None
                            or path == "/api/folder-selection/confirm"
                            else "candidate_scan_failed"
                            if path == "/api/candidates/refresh"
                            else "collection_scan_failed"
                            if path
                            in {
                                "/api/collections/refresh",
                                "/api/collections/check",
                            }
                            else "scan_failed"
                        ),
                        "message": (
                            "所选项目的 Skill 只读观察未完成；登记项目快照与所选文件夹均未改变"
                            if path == "/api/project-skills/folder-selection/confirm"
                            else
                            "本次临时来源扫描失败，当前会话数据未被覆盖"
                            if temporary is not None
                            or path == "/api/folder-selection/confirm"
                            else "本次候选数据刷新失败，上一份有效候选索引未被覆盖"
                            if path == "/api/candidates/refresh"
                            else "本次收藏索引失败，上一份有效索引未被覆盖"
                            if path
                            in {
                                "/api/collections/refresh",
                                "/api/collections/check",
                            }
                            else "本次观察失败，上一份有效快照未被覆盖"
                        ),
                    },
                )
                return
            if not complete:
                self._json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {
                        "error": "scan_incomplete",
                        "message": "本次观察不完整，上一份有效快照未被覆盖",
                        "attempt": {
                            "generated_at": payload["generated_at"],
                            "summary": payload["summary"],
                            "scan_errors": payload["scan_errors"],
                        },
                    },
                )
                return
            self._json(HTTPStatus.OK, payload)
        finally:
            refresh_lock.release()

    def do_OPTIONS(self) -> None:
        if not self._api_preflight():
            return
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})


def run_server(port: int) -> None:
    _assert_plain_directory(GENERATED_DIR, create=True)
    _assert_dist_safe()
    handler = partial(ToolboxHandler, directory=str(DIST_DIR))
    server = ToolboxServer(("127.0.0.1", port), handler)
    print(f"AI-Toolbox 工作台：http://127.0.0.1:{port}")
    print("边界：只读观察；运行期只写 02_AI-Toolbox/generated；Ctrl+C 停止")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _main() -> int:
    parser = argparse.ArgumentParser(description="AI-Toolbox observe-only workbench")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("scan", help="run one read-only scan")
    subparsers.add_parser("collections-scan", help="index the manual collection read-only")
    serve_parser = subparsers.add_parser("serve", help="serve the built workbench on localhost")
    serve_parser.add_argument("--port", type=int, default=4791)
    arguments = parser.parse_args()
    if arguments.command == "scan":
        complete, payload = scan_and_persist()
        print(
            json.dumps(
                {
                    "complete": complete,
                    "generated_at": payload["generated_at"],
                    "generation_id": payload["generation_id"],
                    "summary": payload["summary"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if complete else 2
    if arguments.command == "collections-scan":
        try:
            payload = scan_collection_and_persist()
        except CollectionScanIncomplete as exc:
            print(
                json.dumps(
                    {
                        "complete": False,
                        "reason": exc.reason,
                        "generated_at": exc.payload.get("generated_at"),
                        "summary": exc.payload.get("summary"),
                        "scan_errors": exc.payload.get("scan_errors"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        print(
            json.dumps(
                {
                    "complete": True,
                    "generated_at": payload["generated_at"],
                    "summary": payload["summary"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not 1 <= arguments.port <= 65535:
        parser.error("port must be between 1 and 65535")
    run_server(arguments.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
