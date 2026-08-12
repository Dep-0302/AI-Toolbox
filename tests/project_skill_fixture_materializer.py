"""Safe materialization helpers for the project-Skill synthetic fixtures.

The helpers in this module deliberately stop at filesystem construction.  They
do not execute runtime-event recipes, inject I/O faults, invoke a scanner, or
perform network/process/model operations.  Callers own the temporary directory
and therefore also own cleanup (normally via :class:`tempfile.TemporaryDirectory`).
"""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import stat
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "project-skills"
INDEX_PATH = FIXTURE_ROOT / "index.json"
EXPECTED_PATH = FIXTURE_ROOT / "expected.json"

_CONTENT_KEYS = (
    "source",
    "content_utf8",
    "content_base64",
    "content_template",
    "content_recipe",
)
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_TEMPLATE_VARIABLE = re.compile(r"{{([A-Za-z_][A-Za-z0-9_]*)}}")


class FixtureMaterializationError(ValueError):
    """Raised when inert fixture data violates the materialization boundary."""


@dataclass(frozen=True)
class MaterializationResult(Mapping[str, Any]):
    """Deterministic description of one materialized scenario or scenario case.

    Attribute access is convenient in tests, while the ``Mapping`` interface
    keeps the result compatible with helpers that expect dictionary-style
    access.  Event and fault declarations are deep-copied from fixture data and
    remain inert until a test explicitly passes them to its own hook.
    """

    scenario_id: str
    case_id: str | None
    root: Path
    project_roots: tuple[Path, ...]
    scan_targets: tuple[Mapping[str, Path], ...]
    events: tuple[Mapping[str, Any], ...]
    faults: tuple[Mapping[str, Any], ...]
    expected: Mapping[str, Any]
    created_paths: tuple[Path, ...]

    _KEYS = (
        "scenario_id",
        "case_id",
        "root",
        "project_roots",
        "scan_targets",
        "events",
        "faults",
        "expected",
        "created_paths",
        "runtime_events",
        "io_faults",
    )

    @property
    def runtime_events(self) -> tuple[Mapping[str, Any], ...]:
        return self.events

    @property
    def io_faults(self) -> tuple[Mapping[str, Any], ...]:
        return self.faults

    def __getitem__(self, key: str) -> Any:
        if key not in self._KEYS:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._KEYS)

    def __len__(self) -> int:
        return len(self._KEYS)

    def as_dict(self) -> dict[str, Any]:
        """Return a shallow dictionary view with deterministic key ordering."""

        return {key: self[key] for key in self._KEYS}


def _load_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise FixtureMaterializationError(f"fixture must be a JSON object: {path}")
    return value


def load_fixture_index() -> dict[str, Any]:
    """Load a fresh copy of the checked-in fixture index."""

    return _load_json_object(INDEX_PATH)


def load_expected_matrix() -> dict[str, Any]:
    """Load a fresh copy of the compact cross-scenario expectation matrix."""

    return _load_json_object(EXPECTED_PATH)


def available_scenarios() -> tuple[str, ...]:
    """Return scenario identifiers in their frozen index order."""

    index = load_fixture_index()
    scenarios = index.get("scenarios")
    if not isinstance(scenarios, list):
        raise FixtureMaterializationError("fixture index scenarios must be a list")
    identifiers: list[str] = []
    for item in scenarios:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise FixtureMaterializationError("invalid fixture index scenario entry")
        identifiers.append(item["id"])
    return tuple(identifiers)


def load_scenario(scenario_id: str) -> dict[str, Any]:
    """Load a scenario by its exact identifier without accepting arbitrary paths."""

    if not isinstance(scenario_id, str) or not scenario_id:
        raise FixtureMaterializationError("scenario_id must be a non-empty string")
    index = load_fixture_index()
    entries = index.get("scenarios")
    if not isinstance(entries, list):
        raise FixtureMaterializationError("fixture index scenarios must be a list")
    matches = [item for item in entries if item.get("id") == scenario_id]
    if len(matches) != 1:
        raise FixtureMaterializationError(f"unknown or duplicate scenario: {scenario_id}")
    spec = matches[0].get("spec")
    spec_path = _checked_source_path(spec)
    scenario = _load_json_object(spec_path)
    if scenario.get("id") != scenario_id:
        raise FixtureMaterializationError(
            f"scenario id mismatch: expected {scenario_id!r}, got {scenario.get('id')!r}"
        )
    return scenario


def _relative_parts(value: Any, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        raise FixtureMaterializationError(f"{label} must be a non-empty string")
    if "\x00" in value or "\\" in value:
        raise FixtureMaterializationError(f"unsafe {label}: {value!r}")
    if value.startswith(("/", "\\")) or _WINDOWS_DRIVE.match(value):
        raise FixtureMaterializationError(f"absolute {label} is forbidden: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise FixtureMaterializationError(f"unsafe {label}: {value!r}")
    return tuple(path.parts)


@dataclass(frozen=True)
class _RootAnchor:
    path: Path
    fd: int
    device: int
    inode: int


def _directory_open_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise FixtureMaterializationError(
            "safe fixture materialization requires O_NOFOLLOW and O_DIRECTORY"
        )
    return os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)


def _open_root(root: str | os.PathLike[str]) -> _RootAnchor:
    raw_path = os.fspath(root)
    if not isinstance(raw_path, str):
        raise FixtureMaterializationError("temporary root path must be text")
    path = Path(os.path.abspath(raw_path))
    flags = _directory_open_flags()
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise FixtureMaterializationError(
            "temporary root must exist as a regular, non-symlink directory"
        ) from exc
    try:
        descriptor_stat = os.fstat(fd)
        path_stat = os.stat(path, follow_symlinks=False)
        if not stat.S_ISDIR(descriptor_stat.st_mode) or not stat.S_ISDIR(
            path_stat.st_mode
        ):
            raise FixtureMaterializationError(
                "temporary root must be a regular directory"
            )
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
            path_stat.st_dev,
            path_stat.st_ino,
        ):
            raise FixtureMaterializationError(
                "temporary root changed while its descriptor was opened"
            )
        return _RootAnchor(
            path=path,
            fd=fd,
            device=descriptor_stat.st_dev,
            inode=descriptor_stat.st_ino,
        )
    except Exception:
        os.close(fd)
        raise


def _verify_root(anchor: _RootAnchor) -> None:
    descriptor_stat = os.fstat(anchor.fd)
    try:
        path_stat = os.stat(anchor.path, follow_symlinks=False)
    except OSError as exc:
        raise FixtureMaterializationError(
            "temporary root disappeared during materialization"
        ) from exc
    identity = (anchor.device, anchor.inode)
    if (
        not stat.S_ISDIR(descriptor_stat.st_mode)
        or not stat.S_ISDIR(path_stat.st_mode)
        or (descriptor_stat.st_dev, descriptor_stat.st_ino) != identity
        or (path_stat.st_dev, path_stat.st_ino) != identity
    ):
        raise FixtureMaterializationError(
            "temporary root identity changed during materialization"
        )


def _open_directory_chain(
    anchor: _RootAnchor,
    parts: tuple[str, ...],
    *,
    create: bool,
) -> int:
    """Open a descendant directory without ever re-resolving the root path."""

    current_fd = os.dup(anchor.fd)
    flags = _directory_open_flags()
    try:
        for part in parts:
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    # A racing creator still has to survive O_NOFOLLOW below.
                    pass
                next_fd = os.open(part, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except OSError as exc:
        os.close(current_fd)
        raise FixtureMaterializationError(
            "destination directory is missing, replaced, or a symlink"
        ) from exc


def _create_directory_at(anchor: _RootAnchor, parts: tuple[str, ...]) -> bool:
    parent_fd = _open_directory_chain(anchor, parts[:-1], create=True)
    try:
        try:
            os.mkdir(parts[-1], mode=0o700, dir_fd=parent_fd)
            return True
        except FileExistsError:
            child_fd = os.open(
                parts[-1], _directory_open_flags(), dir_fd=parent_fd
            )
            os.close(child_fd)
            return False
    except OSError as exc:
        raise FixtureMaterializationError(
            f"directory destination collision: {'/'.join(parts)}"
        ) from exc
    finally:
        os.close(parent_fd)


def _write_file_at(anchor: _RootAnchor, parts: tuple[str, ...], data: bytes) -> None:
    parent_fd = _open_directory_chain(anchor, parts[:-1], create=True)
    file_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_fd: int | None = None
    try:
        file_fd = os.open(parts[-1], file_flags, 0o600, dir_fd=parent_fd)
        view = memoryview(data)
        while view:
            written = os.write(file_fd, view)
            if written <= 0:
                raise OSError("short fixture write")
            view = view[written:]
    except OSError as exc:
        raise FixtureMaterializationError(
            f"file destination collision or write failure: {'/'.join(parts)}"
        ) from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(parent_fd)


def _create_symlink_at(
    anchor: _RootAnchor, parts: tuple[str, ...], target: str
) -> None:
    parent_fd = _open_directory_chain(anchor, parts[:-1], create=True)
    try:
        os.symlink(target, parts[-1], dir_fd=parent_fd)
    except OSError as exc:
        raise FixtureMaterializationError(
            f"symlink destination collision: {'/'.join(parts)}"
        ) from exc
    finally:
        os.close(parent_fd)


def _declared_path(root: Path, value: Any, *, label: str) -> Path:
    """Build a lexical in-root path without resolving intentional test links."""

    return root.joinpath(*_relative_parts(value, label=label))


def _checked_source_path(value: Any) -> Path:
    parts = _relative_parts(value, label="source")
    path = FIXTURE_ROOT.joinpath(*parts)
    current = FIXTURE_ROOT
    for part in parts:
        current = current / part
        try:
            current.lstat()
        except FileNotFoundError as exc:
            raise FixtureMaterializationError(f"fixture source does not exist: {value}") from exc
        if current.is_symlink():
            raise FixtureMaterializationError(f"fixture source may not be a symlink: {value}")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(FIXTURE_ROOT.resolve(strict=True))
    except ValueError as exc:
        raise FixtureMaterializationError(f"fixture source escapes fixture root: {value}") from exc
    if not resolved.is_file():
        raise FixtureMaterializationError(f"fixture source must be a file: {value}")
    return resolved


def _render_template(node: Mapping[str, Any], scenario: Mapping[str, Any]) -> bytes:
    declaration = node.get("content_template")
    if not isinstance(declaration, dict):
        raise FixtureMaterializationError("content_template must be an object")
    name = declaration.get("name")
    if name != "minimal_manifest":
        raise FixtureMaterializationError(f"unsupported content template: {name!r}")
    templates = scenario.get("templates")
    if not isinstance(templates, dict) or not isinstance(templates.get(name), str):
        raise FixtureMaterializationError("minimal_manifest template is not declared")
    variables = declaration.get("variables", {})
    if not isinstance(variables, dict):
        raise FixtureMaterializationError("template variables must be an object")
    rendered = templates[name]
    placeholders = set(_TEMPLATE_VARIABLE.findall(rendered))
    if placeholders != set(variables):
        raise FixtureMaterializationError(
            "template variables must exactly match declared placeholders"
        )
    for variable in sorted(placeholders):
        value = variables[variable]
        if not isinstance(value, (str, int, float, bool)):
            raise FixtureMaterializationError(
                f"template variable {variable!r} must be scalar"
            )
        rendered = rendered.replace("{{" + variable + "}}", str(value))
    if _TEMPLATE_VARIABLE.search(rendered):
        raise FixtureMaterializationError("unresolved template placeholder")
    return rendered.encode("utf-8")


def _render_recipe(node: Mapping[str, Any]) -> bytes:
    recipe = node.get("content_recipe")
    if not isinstance(recipe, dict):
        raise FixtureMaterializationError("content_recipe must be an object")
    prefix = recipe.get("prefix")
    suffix = recipe.get("suffix")
    repeat = recipe.get("repeat")
    if not isinstance(prefix, str) or not isinstance(suffix, str):
        raise FixtureMaterializationError("recipe prefix and suffix must be strings")
    if not isinstance(repeat, dict):
        raise FixtureMaterializationError("recipe repeat must be an object")
    text = repeat.get("text")
    count = repeat.get("count")
    if not isinstance(text, str) or not isinstance(count, int) or isinstance(count, bool):
        raise FixtureMaterializationError("recipe repeat requires string text and integer count")
    if count < 0:
        raise FixtureMaterializationError("recipe repeat count may not be negative")
    return (prefix + text * count + suffix).encode("utf-8")


def _file_bytes(node: Mapping[str, Any], scenario: Mapping[str, Any]) -> bytes:
    providers = [key for key in _CONTENT_KEYS if key in node]
    if len(providers) != 1:
        raise FixtureMaterializationError(
            "file node must declare exactly one content provider"
        )
    provider = providers[0]
    if provider == "source":
        return _checked_source_path(node[provider]).read_bytes()
    if provider == "content_utf8":
        content = node[provider]
        if not isinstance(content, str):
            raise FixtureMaterializationError("content_utf8 must be a string")
        return content.encode("utf-8")
    if provider == "content_base64":
        content = node[provider]
        if not isinstance(content, str):
            raise FixtureMaterializationError("content_base64 must be a string")
        try:
            return base64.b64decode(content, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise FixtureMaterializationError("invalid base64 file content") from exc
    if provider == "content_template":
        return _render_template(node, scenario)
    return _render_recipe(node)


def _create_node(
    anchor: _RootAnchor,
    node: Mapping[str, Any],
    scenario: Mapping[str, Any],
    created: list[Path],
) -> None:
    node_type = node.get("type")
    parts = _relative_parts(node.get("path"), label="destination")
    destination = anchor.path.joinpath(*parts)
    if node_type == "directory":
        forbidden = [key for key in _CONTENT_KEYS if key in node]
        if forbidden:
            raise FixtureMaterializationError("directory node may not declare content")
        if _create_directory_at(anchor, parts):
            created.append(destination)
        return
    if node_type != "file":
        raise FixtureMaterializationError(f"unsupported node type: {node_type!r}")
    _write_file_at(anchor, parts, _file_bytes(node, scenario))
    created.append(destination)


def _expand_node_matrix(matrix: Any) -> tuple[dict[str, Any], ...]:
    if matrix is None:
        return ()
    if not isinstance(matrix, dict):
        raise FixtureMaterializationError("node_matrix must be an object")
    base_path = matrix.get("base_path")
    suffix = matrix.get("suffix")
    segments = matrix.get("excluded_segments")
    content = matrix.get("content_utf8")
    if not isinstance(segments, list) or not isinstance(content, str):
        raise FixtureMaterializationError("invalid node_matrix declaration")
    _relative_parts(base_path, label="node_matrix base_path")
    _relative_parts(suffix, label="node_matrix suffix")
    nodes: list[dict[str, Any]] = []
    for segment in segments:
        _relative_parts(segment, label="node_matrix excluded segment")
        nodes.append(
            {
                "type": "file",
                "path": str(PurePosixPath(base_path) / segment / suffix),
                "content_utf8": content,
            }
        )
    return tuple(nodes)


def _validate_link_target(root: Path, link_path: Path, target: Any) -> str:
    if not isinstance(target, str) or not target or "\x00" in target or "\\" in target:
        raise FixtureMaterializationError("symlink target must be a non-empty POSIX path")
    if target.startswith(("/", "\\")) or _WINDOWS_DRIVE.match(target):
        raise FixtureMaterializationError("absolute symlink targets are forbidden")
    target_path = PurePosixPath(target)
    if target_path.is_absolute():
        raise FixtureMaterializationError("absolute symlink targets are forbidden")

    link_parent = list(link_path.parent.relative_to(root).parts)
    normalized = link_parent
    for part in target_path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not normalized:
                raise FixtureMaterializationError("symlink target escapes temporary root")
            normalized.pop()
        else:
            normalized.append(part)
    # This is a lexical check only.  The target is intentionally not resolved/read.
    root.joinpath(*normalized).relative_to(root)
    return target


def _create_symlink(
    anchor: _RootAnchor, declaration: Mapping[str, Any], created: list[Path]
) -> None:
    parts = _relative_parts(declaration.get("path"), label="symlink path")
    destination = anchor.path.joinpath(*parts)
    target = _validate_link_target(anchor.path, destination, declaration.get("target"))
    _create_symlink_at(anchor, parts, target)
    created.append(destination)


def _validate_declarations(
    root: Path,
    declarations: Any,
    *,
    kind: str,
) -> tuple[Mapping[str, Any], ...]:
    if declarations is None:
        return ()
    if not isinstance(declarations, list):
        raise FixtureMaterializationError(f"{kind} must be a list")
    checked: list[Mapping[str, Any]] = []
    for declaration in declarations:
        if not isinstance(declaration, dict):
            raise FixtureMaterializationError(f"{kind} entries must be objects")
        path = _declared_path(root, declaration.get("path"), label=f"{kind} path")
        if kind == "runtime_events" and "target" in declaration:
            _validate_link_target(root, path, declaration["target"])
        checked.append(copy.deepcopy(declaration))
    return tuple(checked)


def _project_roots(root: Path, scenario: Mapping[str, Any]) -> tuple[Path, ...]:
    records: list[Mapping[str, Any]] = []
    project = scenario.get("project")
    projects = scenario.get("projects")
    if project is not None:
        if not isinstance(project, dict):
            raise FixtureMaterializationError("project must be an object")
        records.append(project)
    if projects is not None:
        if not isinstance(projects, list) or not all(isinstance(item, dict) for item in projects):
            raise FixtureMaterializationError("projects must be a list of objects")
        records.extend(projects)
    roots: list[Path] = []
    for record in records:
        roots.append(_declared_path(root, record.get("relative_path"), label="project path"))
    scan_target = scenario.get("scan_target")
    if isinstance(scan_target, dict) and "project_root" in scan_target:
        declared = _declared_path(
            root, scan_target.get("project_root"), label="project root"
        )
        if declared not in roots:
            roots.append(declared)
    return tuple(roots)


def _scan_targets(root: Path, scenario: Mapping[str, Any]) -> tuple[Mapping[str, Path], ...]:
    scan_target = scenario.get("scan_target")
    if scan_target is None:
        return ()
    if not isinstance(scan_target, dict):
        raise FixtureMaterializationError("scan_target must be an object")
    observation = scan_target.get("observation_root")
    if observation == ".":
        observation_root = root
    else:
        observation_root = _declared_path(root, observation, label="observation root")
    project_root = _declared_path(
        root, scan_target.get("project_root"), label="project root"
    )
    return ({"observation_root": observation_root, "project_root": project_root},)


def _materialize_definition(
    scenario_id: str,
    definition: Mapping[str, Any],
    anchor: _RootAnchor,
    *,
    case_id: str | None,
    templates: Mapping[str, Any] | None = None,
) -> MaterializationResult:
    effective: dict[str, Any] = dict(definition)
    if templates is not None and "templates" not in effective:
        effective["templates"] = templates
    nodes = effective.get("nodes", [])
    if not isinstance(nodes, list):
        raise FixtureMaterializationError("nodes must be a list")
    created: list[Path] = []
    for node in (*nodes, *_expand_node_matrix(effective.get("node_matrix"))):
        if not isinstance(node, dict):
            raise FixtureMaterializationError("node entries must be objects")
        _create_node(anchor, node, effective, created)

    links = effective.get("dynamic_symlinks", [])
    if not isinstance(links, list):
        raise FixtureMaterializationError("dynamic_symlinks must be a list")
    for declaration in links:
        if not isinstance(declaration, dict):
            raise FixtureMaterializationError("dynamic_symlink entries must be objects")
        _create_symlink(anchor, declaration, created)

    events = _validate_declarations(
        anchor.path, effective.get("runtime_events"), kind="runtime_events"
    )
    faults = _validate_declarations(
        anchor.path, effective.get("io_faults"), kind="io_faults"
    )
    expected = effective.get("expected", {})
    if not isinstance(expected, dict):
        raise FixtureMaterializationError("expected must be an object")
    return MaterializationResult(
        scenario_id=scenario_id,
        case_id=case_id,
        root=anchor.path,
        project_roots=_project_roots(anchor.path, effective),
        scan_targets=_scan_targets(anchor.path, effective),
        events=events,
        faults=faults,
        expected=copy.deepcopy(expected),
        created_paths=tuple(created),
    )


def materialize_scenario(
    scenario: str | Mapping[str, Any],
    root: str | os.PathLike[str],
    *,
    case_id: str | None = None,
) -> MaterializationResult:
    """Materialize one indexed scenario into an existing caller-owned temp root.

    ``scenario`` may be an indexed scenario id or an already-loaded mapping.
    Case-based scenarios such as ``path-safety`` require ``case_id`` so every
    case can receive a distinct ``TemporaryDirectory`` from its caller.
    """

    if isinstance(scenario, str):
        specification = load_scenario(scenario)
    elif isinstance(scenario, Mapping):
        specification = copy.deepcopy(dict(scenario))
    else:
        raise FixtureMaterializationError("scenario must be an id or mapping")
    scenario_id = specification.get("id")
    if not isinstance(scenario_id, str) or not scenario_id:
        raise FixtureMaterializationError("scenario id must be a non-empty string")

    cases = specification.get("cases")
    if cases is None and case_id is not None:
        raise FixtureMaterializationError(
            f"scenario {scenario_id!r} has no cases; case_id is invalid"
        )
    if cases is not None:
        if not isinstance(cases, list):
            raise FixtureMaterializationError("scenario cases must be a list")
        if not isinstance(case_id, str) or not case_id:
            raise FixtureMaterializationError(
                f"scenario {scenario_id!r} requires an explicit case_id"
            )
        matching = [
            case
            for case in cases
            if isinstance(case, dict) and case.get("case_id") == case_id
        ]
        if len(matching) != 1:
            raise FixtureMaterializationError(
                f"unknown or duplicate case {case_id!r} in scenario {scenario_id!r}"
            )
        definition = matching[0]
        templates = specification.get("templates")
    else:
        definition = specification
        templates = None

    anchor = _open_root(root)
    try:
        result = _materialize_definition(
            scenario_id,
            definition,
            anchor,
            case_id=case_id,
            templates=templates,
        )
        _verify_root(anchor)
        return result
    finally:
        os.close(anchor.fd)


__all__ = [
    "EXPECTED_PATH",
    "FIXTURE_ROOT",
    "INDEX_PATH",
    "FixtureMaterializationError",
    "MaterializationResult",
    "available_scenarios",
    "load_expected_matrix",
    "load_fixture_index",
    "load_scenario",
    "materialize_scenario",
]
