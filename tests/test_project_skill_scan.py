from __future__ import annotations

import base64
import builtins
import errno
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Iterator, Mapping
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = ROOT / "tests"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

from src import project_skill_scan as scan  # noqa: E402
import project_skill_fixture_materializer as materializer  # noqa: E402
from project_skill_fixture_materializer import (  # noqa: E402
    FixtureMaterializationError,
    load_expected_matrix,
    load_scenario,
    materialize_scenario,
)


FIXED_TIME = "2000-01-01T12:00:00Z"
BOUNDARY_REF = "project-skill-observation-boundary-v1"
PROJECTS_PATH = ROOT / "registry" / "project_skill_projects.json"
ASSOCIATIONS_PATH = ROOT / "registry" / "project_skill_associations.json"
SNAPSHOT_SCHEMA_PATH = ROOT / "schemas" / "project_skill_snapshot.schema.json"
BOUNDARY_PATH = ROOT / "registry" / "project_skill_observation_boundary.json"

_REAL_OS_OPEN = os.open
_REAL_OS_READ = os.read
_REAL_OS_FSTAT = os.fstat
_REAL_OS_STAT = os.stat
_REAL_OS_UNLINK = os.unlink
_REAL_OS_SYMLINK = os.symlink
_REAL_BUILTIN_OPEN = builtins.open


def _project_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = {
        "project_id": row["project_id"],
        "relative_path": row["relative_path"],
        "classification": row["classification"],
        "display_name": row.get("display_name", row["project_id"]),
    }
    if "parent_container_id" in row:
        result["parent_container_id"] = row["parent_container_id"]
    return result


def _registries(
    projects: list[Mapping[str, Any]],
    associations: list[Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {
            "schema_version": 1,
            "registry_id": "fixture-projects-v1",
            "observation_boundary_ref": BOUNDARY_REF,
            "confirmed_on": "2000-01-01",
            "projects": [_project_row(row) for row in projects],
        },
        {
            "schema_version": 1,
            "registry_id": "fixture-associations-v1",
            "observation_boundary_ref": BOUNDARY_REF,
            "confirmed_on": "2000-01-01",
            "associations": list(associations or []),
        },
    )


def _scenario_projects(specification: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if "projects" in specification:
        return list(specification["projects"])
    if "project" in specification:
        return [specification["project"]]
    raise AssertionError("scenario has no project declaration")


def _flatten_observations(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        observation
        for project in payload["projects"]
        for logical_skill in project["logical_skills"]
        for observation in logical_skill["observations"]
    ]


def _project(payload: Mapping[str, Any], project_id: str) -> dict[str, Any]:
    return next(row for row in payload["projects"] if row["project_id"] == project_id)


def _entry(project: Mapping[str, Any], relative_path: str) -> dict[str, Any]:
    return next(row for row in project["entries"] if row["entry"]["relative_path"] == relative_path)


def _approved_preview_boundary() -> dict[str, Any]:
    boundary = json.loads(BOUNDARY_PATH.read_text(encoding="utf-8"))
    boundary["phase"] = "non_persistent_preview"
    boundary["data_contract"]["connections"] = {
        "preview": True,
        "scanner": True,
        "api": False,
        "ui": False,
        "persistence": False,
    }
    boundary["persistence"]["current_phase_writes"] = []
    return boundary


class ProjectSkillScanTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.expected = load_expected_matrix()
        cls.snapshot_schema = json.loads(SNAPSHOT_SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_production_root_is_portable_home_documents(self) -> None:
        self.assertEqual(scan.PRODUCTION_ROOT, Path.home() / "Documents")
        self.assertEqual(scan.PRODUCTION_ROOT_DECLARATION, "~/Documents")
        boundary = json.loads(BOUNDARY_PATH.read_text(encoding="utf-8"))
        self.assertEqual(
            boundary["source_scope"]["root"], scan.PRODUCTION_ROOT_DECLARATION
        )

    def _assert_safe_cli_failure(
        self,
        stdout: io.StringIO,
        stderr: io.StringIO,
        *,
        expected_code: str,
        forbidden: str | None = None,
    ) -> None:
        """A failure may be silent on stdout or emit one complete safe payload."""

        output = stdout.getvalue()
        diagnostics = stderr.getvalue()
        if output:
            emitted = json.loads(output)
            scan.validate_project_skill_payload(emitted)
            self.assertNotEqual(emitted["scan_status"], "complete")
            self.assertIn(expected_code, {row["code"] for row in emitted["issues"]})
        else:
            self.assertIn(expected_code, diagnostics)
        if forbidden is not None:
            self.assertNotIn(forbidden, output + diagnostics)

    @contextmanager
    def _scan_guards(self) -> Iterator[dict[str, list[Any]]]:
        """Prove that the scanner performs reads only during its scan phase."""

        calls: dict[str, list[Any]] = {"os_open": [], "builtin_open": [], "writes": []}
        write_flags = (
            os.O_WRONLY
            | os.O_RDWR
            | os.O_CREAT
            | os.O_TRUNC
            | os.O_APPEND
            | getattr(os, "O_EXCL", 0)
        )

        def guarded_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            calls["os_open"].append((str(path), flags))
            if flags & write_flags:
                calls["writes"].append(("os.open", str(path), flags))
                raise AssertionError(f"write-capable os.open during scan: {path!r}")
            return _REAL_OS_OPEN(path, flags, *args, **kwargs)

        def guarded_builtin_open(
            file: Any, mode: str = "r", *args: Any, **kwargs: Any
        ) -> Any:
            calls["builtin_open"].append((str(file), mode))
            if any(marker in mode for marker in ("w", "a", "x", "+")):
                calls["writes"].append(("open", str(file), mode))
                raise AssertionError(f"write-capable builtins.open during scan: {file!r}")
            return _REAL_BUILTIN_OPEN(file, mode, *args, **kwargs)

        def forbidden_write(name: str):
            def fail(*args: Any, **kwargs: Any) -> None:
                calls["writes"].append((name, args, kwargs))
                raise AssertionError(f"filesystem mutation during scan: {name}")

            return fail

        with ExitStack() as stack:
            # The platform capability check compares the current callable by
            # identity with os.supports_dir_fd.  The read spy below is a mock,
            # so exercise the real capability check first and then keep that
            # already-proved precondition stable for the guarded scan.
            scan._require_descriptor_safety()
            stack.enter_context(mock.patch.object(scan, "_require_descriptor_safety", return_value=None))
            stack.enter_context(mock.patch.object(scan.os, "open", side_effect=guarded_os_open))
            stack.enter_context(mock.patch("builtins.open", side_effect=guarded_builtin_open))
            for name in ("write", "rename", "replace", "mkdir", "unlink", "chmod", "symlink"):
                stack.enter_context(mock.patch.object(scan.os, name, side_effect=forbidden_write(name)))
            stack.enter_context(mock.patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))
            stack.enter_context(
                mock.patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden"))
            )
            for name in ("Popen", "run", "call", "check_call", "check_output"):
                stack.enter_context(
                    mock.patch.object(subprocess, name, side_effect=AssertionError("process forbidden"))
                )
            yield calls
        self.assertEqual(calls["writes"], [])

    def _scan_materialized(
        self,
        scenario_id: str,
        *,
        case_id: str | None = None,
        associations: list[Mapping[str, Any]] | None = None,
        guarded: bool = True,
        monotonic: Any = None,
    ) -> tuple[dict[str, Any], dict[str, list[Any]]]:
        specification = load_scenario(scenario_id)
        with tempfile.TemporaryDirectory(prefix="project-skill-scan-") as temporary:
            result = materialize_scenario(specification, temporary, case_id=case_id)
            if case_id is not None:
                target = result.scan_targets[0]
                relative = target["project_root"].relative_to(target["observation_root"]).as_posix()
                projects = [
                    {
                        "project_id": f"fixture-{case_id}",
                        "relative_path": relative,
                        "classification": "project",
                    }
                ]
                root = target["observation_root"]
                if case_id != "observation-root-is-link":
                    root = root.resolve(strict=True)
            else:
                projects = _scenario_projects(specification)
                root = result.root.resolve(strict=True)
            project_registry, association_registry = _registries(projects, associations)
            calls: dict[str, list[Any]] = {"os_open": [], "builtin_open": [], "writes": []}
            context = self._scan_guards() if guarded else nullcontext(calls)
            with context as observed_calls:
                payload = scan.build_project_skill_snapshot(
                    root,
                    project_registry,
                    association_registry,
                    generated_at=FIXED_TIME,
                    **({"monotonic": monotonic} if monotonic is not None else {}),
                )
            return payload, observed_calls

    def _scan_definition(
        self,
        definition: Mapping[str, Any],
        *,
        associations: list[Mapping[str, Any]] | None = None,
        guarded: bool = True,
    ) -> tuple[dict[str, Any], dict[str, list[Any]]]:
        with tempfile.TemporaryDirectory(prefix="project-skill-scan-") as temporary:
            result = materialize_scenario(definition, temporary)
            projects, human = _registries(_scenario_projects(definition), associations)
            calls: dict[str, list[Any]] = {"os_open": [], "builtin_open": [], "writes": []}
            context = self._scan_guards() if guarded else nullcontext(calls)
            with context as observed_calls:
                payload = scan.build_project_skill_snapshot(
                    result.root.resolve(strict=True),
                    projects,
                    human,
                    generated_at=FIXED_TIME,
                )
            return payload, observed_calls

    def test_001_empty_entry_is_complete_zero_not_failure(self) -> None:
        payload, _ = self._scan_materialized("empty-entry")
        project = _project(payload, "fixture-project-empty")
        self.assertEqual(payload["scan_status"], "complete")
        self.assertEqual(project["scan_status"], "complete")
        self.assertEqual(_entry(project, ".agents/skills")["status"], "empty")
        self.assertEqual(project["logical_skills"], [])
        self.assertEqual(payload["issues"], [])

    def test_missing_entries_are_complete_zero_not_failure(self) -> None:
        payload, _ = self._scan_materialized("missing-entry")
        project = _project(payload, "fixture-project-missing")
        self.assertEqual(payload["scan_status"], "complete")
        self.assertEqual(project["scan_status"], "complete")
        self.assertEqual(len(project["entries"]), 7)
        self.assertEqual({row["status"] for row in project["entries"]}, {"missing"})
        self.assertEqual(project["logical_skills"], [])
        self.assertEqual(payload["issues"], [])

    def test_missing_project_root_is_error_zero_and_cannot_claim_changes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="project-skill-scan-") as temporary:
            root = Path(temporary).resolve()
            projects, associations = _registries(
                [
                    {
                        "project_id": "missing-project",
                        "relative_path": "missing-project",
                        "classification": "project",
                    }
                ]
            )
            payload = scan.build_project_skill_snapshot(
                root, projects, associations, generated_at=FIXED_TIME
            )
        project = _project(payload, "missing-project")
        self.assertEqual(payload["scan_status"], "error")
        self.assertEqual(project["scan_status"], "error")
        self.assertEqual(project["logical_skills"], [])
        self.assertEqual(project["entries"], [])
        self.assertIn("project_root_missing", {row["code"] for row in payload["issues"]})
        self.assertNotIn("changes", payload)

    def test_005_has_five_entities_observations_and_bindings(self) -> None:
        payload, _ = self._scan_materialized("five-entities")
        project = _project(payload, "fixture-project-entities")
        observations = _flatten_observations(payload)
        self.assertEqual(project["scan_status"], "complete")
        self.assertEqual(len(project["logical_skills"]), 5)
        self.assertEqual(len(observations), 5)
        self.assertEqual(
            sum(row["evidence"]["project_binding"]["status"] == "observed_binding" for row in observations),
            5,
        )
        self.assertEqual({row["source_kind"] for row in observations}, {"entity"})

    def test_chinese_overlay_hit_missing_and_stale_do_not_change_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            skill_dir = root / "fixture-project" / ".agents" / "skills" / "sample-skill"
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text(
                "---\nname: sample-skill\ndescription: English source summary.\n---\n"
                "BODY_SENTINEL_LOCALIZATION_MUST_NOT_BE_READ\n",
                encoding="utf-8",
            )
            projects, associations = _registries(
                [
                    {
                        "project_id": "fixture-localization",
                        "relative_path": "fixture-project",
                        "classification": "project",
                    }
                ]
            )
            missing = scan.build_project_skill_snapshot(
                root,
                projects,
                associations,
                {"schema_version": 1, "items": []},
                generated_at=FIXED_TIME,
            )
            missing_skill = missing["projects"][0]["logical_skills"][0]
            projection_hash = missing_skill["observations"][0]["projection_sha256_v1"]
            self.assertEqual(
                missing_skill["localization"],
                {"status": "missing", "coverage_complete": False},
            )

            def overlay(translated_hash: str) -> dict[str, Any]:
                return {
                    "schema_version": 1,
                    "items": [
                        {
                            "asset_id": "skill:sample-skill",
                            "source_name": "sample-skill",
                            "zh_name": "示例技能",
                            "summary": "只读展示中文简介。",
                            "use_cases": ["需要核对中文覆盖时。"],
                            "not_for": ["升级宿主可用性。"],
                            "examples": ["请展示项目 Skill 中文简介。"],
                            "status": "reviewed",
                            "translated_from_projection_sha256_v1": translated_hash,
                        }
                    ],
                }

            current = scan.build_project_skill_snapshot(
                root,
                projects,
                associations,
                overlay(projection_hash),
                generated_at=FIXED_TIME,
            )
            stale = scan.build_project_skill_snapshot(
                root,
                projects,
                associations,
                overlay("0" * 64),
                generated_at=FIXED_TIME,
            )
            current_skill = current["projects"][0]["logical_skills"][0]
            stale_skill = stale["projects"][0]["logical_skills"][0]
            self.assertEqual(current_skill["localization"]["status"], "reviewed")
            self.assertTrue(current_skill["localization"]["coverage_complete"])
            self.assertEqual(current_skill["localization"]["zh_name"], "示例技能")
            self.assertEqual(
                stale_skill["localization"],
                {
                    "status": "stale",
                    "coverage_complete": False,
                    "asset_id": "skill:sample-skill",
                    "translated_from_projection_sha256_v1": "0" * 64,
                },
            )
            evidence = missing_skill["observations"][0]["evidence"]
            self.assertEqual(current_skill["observations"][0]["evidence"], evidence)
            self.assertEqual(stale_skill["observations"][0]["evidence"], evidence)
            serialized = json.dumps(current, ensure_ascii=False)
            self.assertNotIn("BODY_SENTINEL_LOCALIZATION_MUST_NOT_BE_READ", serialized)

    def test_006_never_reads_marketplace_or_unapproved_plugins_tree(self) -> None:
        payload, calls = self._scan_materialized("marketplace-bait")
        project = _project(payload, "fixture-project-marketplace")
        opened_names = [path for path, _flags in calls["os_open"]]
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["scan_status"], "complete")
        self.assertEqual(project["logical_skills"], [])
        self.assertNotIn("marketplace.json", opened_names)
        self.assertNotIn("bait-one", opened_names)
        self.assertNotIn("bait-two", opened_names)
        self.assertNotIn("SKILL.md", opened_names)
        self.assertNotIn("BODY_SENTINEL_BAIT_PATH_DO_NOT_READ_OR_EMIT", serialized)

    def test_007_groups_same_physical_manifest_but_keeps_six_plugin_sources(self) -> None:
        payload, _ = self._scan_materialized("multi-evidence")
        project = _project(payload, "fixture-project-multi")
        observations = _flatten_observations(payload)
        self.assertEqual(project["scan_status"], "complete")
        self.assertEqual(len(project["logical_skills"]), 10)
        self.assertEqual(len(observations), 16)
        self.assertEqual(
            sum(row["evidence"]["project_binding"]["status"] == "observed_binding" for row in observations),
            10,
        )
        self.assertEqual(
            sum(row["source_kind"] == "plugin_bundled_source" for row in observations),
            6,
        )
        linked = [skill for skill in project["logical_skills"] if skill["display_name"].startswith("plugin-")]
        self.assertEqual(len(linked), 6)
        self.assertTrue(all(len(skill["observations"]) == 2 for skill in linked))
        self.assertTrue(
            all(
                {row["source_kind"] for row in skill["observations"]}
                == {"same_project_symlink", "plugin_bundled_source"}
                for skill in linked
            )
        )

    def test_identical_projection_hashes_do_not_merge_across_projects(self) -> None:
        definition = {
            "id": "cross-project-identity",
            "projects": [
                {"project_id": "project-a", "relative_path": "project-a", "classification": "project"},
                {"project_id": "project-b", "relative_path": "project-b", "classification": "project"},
            ],
            "nodes": [
                {
                    "type": "file",
                    "path": f"project-{letter}/.agents/skills/shared/SKILL.md",
                    "content_utf8": "---\nname: shared\ndescription: Same projection.\n---\n",
                }
                for letter in ("a", "b")
            ],
        }
        payload, _ = self._scan_definition(definition)
        skills = [skill for project in payload["projects"] for skill in project["logical_skills"]]
        self.assertEqual(len(skills), 2)
        self.assertEqual(len({skill["logical_skill_id"] for skill in skills}), 2)
        self.assertEqual(
            len({skill["observations"][0]["projection_sha256_v1"] for skill in skills}),
            1,
        )

    def test_008_human_association_stays_separate_from_zero_machine_observation(self) -> None:
        association = {
            "association_id": "fixture-association-human-design",
            "project_id": "fixture-project-human",
            "relationship": "human_association",
            "asset_id": "skill:design-guidance",
            "reason": "Synthetic human memory only.",
            "source": "synthetic-confirmation",
        }
        payload, _ = self._scan_materialized("human-only", associations=[association])
        project = _project(payload, "fixture-project-human")
        self.assertEqual(project["logical_skills"], [])
        self.assertEqual(project["scan_status"], "complete")
        self.assertEqual(payload["human_associations"]["items"], [association])
        self.assertNotIn("human_associations", project)

    def test_010b_has_seven_path_only_observations_without_availability_or_use_upgrade(self) -> None:
        payload, _ = self._scan_materialized("codex-path-only")
        project = _project(payload, "fixture-project-codex-path")
        observations = _flatten_observations(payload)
        self.assertEqual(len(project["logical_skills"]), 7)
        self.assertEqual(len(observations), 7)
        for row in observations:
            self.assertEqual(row["entry"]["binding_semantics"], "observed_path_only")
            self.assertEqual(row["source_kind"], "observed_path_only")
            self.assertEqual(row["evidence"]["project_binding"]["status"], "observed_path_only")
            self.assertEqual(row["evidence"]["host_availability"]["status"], "unverified")
            self.assertEqual(row["evidence"]["actual_use"]["status"], "not_connected")

    def test_non_project_classifications_and_codex_container_never_descend(self) -> None:
        payload, calls = self._scan_materialized("classification-matrix")
        rows = {row["classification"]: row for row in payload["projects"]}
        self.assertEqual(len(rows["project"]["logical_skills"]), 1)
        for classification in ("container", "archive", "excluded", "unclassified"):
            self.assertEqual(rows[classification]["scan_status"], "not_scanned")
            self.assertEqual(rows[classification]["entries"], [])
            self.assertEqual(rows[classification]["logical_skills"], [])
        opened = [path for path, _flags in calls["os_open"]]
        self.assertNotIn("container-bait", opened)
        self.assertNotIn("archive-bait", opened)
        self.assertNotIn("excluded-bait", opened)
        self.assertNotIn("unclassified-bait", opened)

        codex, codex_calls = self._scan_materialized("container-no-descent")
        row = _project(codex, "fixture-container-generic")
        self.assertEqual(row["scan_status"], "not_scanned")
        self.assertEqual(row["logical_skills"], [])
        self.assertNotIn("SKILL.md", [path for path, _flags in codex_calls["os_open"]])
        self.assertNotIn("BODY_SENTINEL_CONTAINER_DO_NOT_READ_OR_EMIT", json.dumps(codex))

    def test_seven_entry_semantics_are_exact_and_behaviorally_distinct(self) -> None:
        expected = {
            ".agents/skills": ("host_entry", "codex_compatible", "project_binding_candidate"),
            ".claude/skills": ("host_specific_entry", "claude", "project_binding_candidate"),
            ".codex/skills": ("host_specific_or_legacy_entry", "codex", "observed_path_only"),
            ".hermes/skills": ("host_specific_entry", "hermes", "project_binding_candidate"),
            ".workbuddy/skills": ("host_specific_entry", "workbuddy", "project_binding_candidate"),
            "skills": ("generic_source", None, "source_only"),
            ".agents/plugins": ("plugin_container", None, "plugin_bundled_source"),
        }
        actual = {
            row["relative_path"]: (row["kind"], row["host_hint"], row["binding_semantics"])
            for row in scan.ENTRY_POINTS
        }
        self.assertEqual(actual, expected)

        definition = {
            "id": "all-entry-semantics",
            "project": {"project_id": "all-entries", "relative_path": "all-entries", "classification": "project"},
            "nodes": [
                {
                    "type": "file",
                    "path": f"all-entries/{entry}/skill-{index}/SKILL.md",
                    "content_utf8": f"---\nname: skill-{index}\ndescription: Entry fixture.\n---\n",
                }
                for index, entry in enumerate(
                    (".agents/skills", ".claude/skills", ".codex/skills", ".hermes/skills", ".workbuddy/skills", "skills"),
                    start=1,
                )
            ]
            + [
                {
                    "type": "file",
                    "path": "all-entries/.agents/plugins/bundle/skills/skill-7/SKILL.md",
                    "content_utf8": "---\nname: skill-7\ndescription: Plugin entry fixture.\n---\n",
                }
            ],
        }
        payload, _ = self._scan_definition(definition)
        observations = _flatten_observations(payload)
        self.assertEqual(len(observations), 7)
        self.assertEqual(
            {row["entry"]["relative_path"] for row in observations}, set(expected)
        )

    def test_frontmatter_reads_exactly_through_closing_delimiter_not_body(self) -> None:
        sentinel = "BODY_SENTINEL_EXACT_BYTE_CUTOFF"
        manifest = f"---\nname: exact\ndescription: Exact cutoff.\n---\n{sentinel}\n"
        definition = {
            "id": "exact-frontmatter-cutoff",
            "project": {"project_id": "exact", "relative_path": "exact", "classification": "project"},
            "nodes": [
                {
                    "type": "file",
                    "path": "exact/.agents/skills/exact/SKILL.md",
                    "content_utf8": manifest,
                }
            ],
        }
        closing_offset = manifest.index("---\n", 4) + 4
        with tempfile.TemporaryDirectory(prefix="project-skill-scan-") as temporary:
            materialized = materialize_scenario(definition, temporary)
            manifest_path = materialized.root / "exact/.agents/skills/exact/SKILL.md"
            target_inode = manifest_path.stat().st_ino
            bytes_read = 0

            def counted_read(fd: int, amount: int) -> bytes:
                nonlocal bytes_read
                piece = _REAL_OS_READ(fd, amount)
                if _REAL_OS_FSTAT(fd).st_ino == target_inode:
                    bytes_read += len(piece)
                return piece

            projects, associations = _registries(_scenario_projects(definition))
            with mock.patch.object(scan.os, "read", side_effect=counted_read):
                payload = scan.build_project_skill_snapshot(
                    materialized.root.resolve(strict=True),
                    projects,
                    associations,
                    generated_at=FIXED_TIME,
                )
        observation = _flatten_observations(payload)[0]
        self.assertEqual(bytes_read, closing_offset)
        self.assertEqual(observation["frontmatter_bytes_read"], closing_offset)
        self.assertNotIn(sentinel, json.dumps(payload, ensure_ascii=False))

    def test_six_field_projection_escapes_markup_and_drops_unknown_sensitive_values(self) -> None:
        secret = "https://sensitive.invalid/token"
        definition = {
            "id": "projection-safety",
            "project": {"project_id": "projection-safe", "relative_path": "projection-safe", "classification": "project"},
            "nodes": [
                {
                    "type": "file",
                    "path": "projection-safe/.agents/skills/safe/SKILL.md",
                    "content_utf8": (
                        "---\nname: safe\ndescription: \"<b>plain text</b>\"\nversion: 1\n"
                        "author: Fixture\nlicense: Test\nagent_created: false\n"
                        f"unknown_secret: {secret}\n---\nBODY_SECRET_NOT_READ\n"
                    ),
                }
            ],
        }
        payload, _ = self._scan_definition(definition)
        projection = _flatten_observations(payload)[0]["frontmatter_projection"]
        self.assertEqual(
            set(projection),
            {"name", "description", "version", "author", "license", "agent_created"},
        )
        self.assertEqual(projection["description"], "&lt;b&gt;plain text&lt;/b&gt;")
        self.assertFalse(projection["agent_created"])
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("unknown_secret", serialized)
        self.assertNotIn("BODY_SECRET_NOT_READ", serialized)

    def test_approved_description_literal_and_folded_blocks_project_as_safe_text(self) -> None:
        definition = {
            "id": "approved-description-blocks",
            "project": {
                "project_id": "approved-description-blocks",
                "relative_path": "approved-description-blocks",
                "classification": "project",
            },
            "nodes": [
                {
                    "type": "file",
                    "path": "approved-description-blocks/.agents/skills/literal/SKILL.md",
                    "content_utf8": (
                        "---\nname: literal\ndescription: |\n"
                        "  First <literal> line.\n"
                        "  Second & literal line.\n"
                        "---\nLITERAL_BODY_SENTINEL\n"
                    ),
                },
                {
                    "type": "file",
                    "path": "approved-description-blocks/.agents/skills/folded/SKILL.md",
                    "content_utf8": (
                        "---\nname: folded\ndescription: >\n"
                        "  First <folded> line.\n"
                        "  Second & folded line.\n"
                        "---\nFOLDED_BODY_SENTINEL\n"
                    ),
                },
            ],
        }
        payload, _ = self._scan_definition(definition)
        self.assertEqual(payload["scan_status"], "complete")
        observations = {
            row["frontmatter_projection"]["name"]: row
            for row in _flatten_observations(payload)
        }
        self.assertEqual(
            observations["literal"]["frontmatter_projection"]["description"],
            "First &lt;literal&gt; line.\nSecond &amp; literal line.",
        )
        self.assertEqual(
            observations["folded"]["frontmatter_projection"]["description"],
            "First &lt;folded&gt; line. Second &amp; folded line.",
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("LITERAL_BODY_SENTINEL", serialized)
        self.assertNotIn("FOLDED_BODY_SENTINEL", serialized)

    def test_plain_inline_comment_is_dropped_but_quoted_hash_is_preserved(self) -> None:
        sentinel = "COMMENT_SENTINEL_MUST_NOT_LEAK"
        definition = {
            "id": "frontmatter-inline-comment",
            "project": {
                "project_id": "frontmatter-inline-comment",
                "relative_path": "frontmatter-inline-comment",
                "classification": "project",
            },
            "nodes": [
                {
                    "type": "file",
                    "path": "frontmatter-inline-comment/.agents/skills/plain/SKILL.md",
                    "content_utf8": (
                        "---\nname: plain\n"
                        f"description: safe # {sentinel}\n"
                        "---\nPLAIN_COMMENT_BODY_SENTINEL\n"
                    ),
                },
                {
                    "type": "file",
                    "path": "frontmatter-inline-comment/.agents/skills/quoted/SKILL.md",
                    "content_utf8": (
                        "---\nname: quoted\n"
                        'description: "safe # literal"\n'
                        "---\nQUOTED_HASH_BODY_SENTINEL\n"
                    ),
                },
            ],
        }
        payload, _ = self._scan_definition(definition)
        self.assertEqual(payload["scan_status"], "complete")
        observations = {
            row["frontmatter_projection"]["name"]: row
            for row in _flatten_observations(payload)
        }
        self.assertEqual(
            observations["plain"]["frontmatter_projection"]["description"],
            "safe",
        )
        self.assertEqual(
            observations["quoted"]["frontmatter_projection"]["description"],
            "safe # literal",
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(sentinel, serialized)
        self.assertNotIn("PLAIN_COMMENT_BODY_SENTINEL", serialized)
        self.assertNotIn("QUOTED_HASH_BODY_SENTINEL", serialized)

    def test_unknown_nested_frontmatter_is_ignored_without_forging_approved_projection(self) -> None:
        secret = "UNKNOWN_NESTED_TOKEN_MUST_NOT_LEAK"
        definition = {
            "id": "unknown-nested-frontmatter",
            "project": {
                "project_id": "unknown-nested-frontmatter",
                "relative_path": "unknown-nested-frontmatter",
                "classification": "project",
            },
            "nodes": [
                {
                    "type": "file",
                    "path": "unknown-nested-frontmatter/.agents/skills/safe/SKILL.md",
                    "content_utf8": (
                        "---\n"
                        "name: approved-name\n"
                        "description: Approved description.\n"
                        "metadata:\n"
                        "  private:\n"
                        f"    token: {secret}\n"
                        "    name: forged-name\n"
                        "    description: Forged description.\n"
                        "    agent_created: true\n"
                        "  version: forged-version\n"
                        "---\nUNKNOWN_METADATA_BODY_SENTINEL\n"
                    ),
                }
            ],
        }
        payload, _ = self._scan_definition(definition)
        self.assertEqual(payload["scan_status"], "complete")
        projection = _flatten_observations(payload)[0]["frontmatter_projection"]
        self.assertEqual(projection["name"], "approved-name")
        self.assertEqual(projection["description"], "Approved description.")
        self.assertIsNone(projection["version"])
        self.assertIsNone(projection["agent_created"])
        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in (
            secret,
            "forged-name",
            "Forged description.",
            "forged-version",
            "UNKNOWN_METADATA_BODY_SENTINEL",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_frontmatter_tags_anchors_aliases_merges_and_approved_flow_are_rejected(self) -> None:
        cases = {
            "tag": "description: !!str tagged",
            "anchor": "description: &shared anchored",
            "alias": "description: *shared",
            "merge": "<<: forged",
            "flow-sequence": "description: [approved, flow]",
            "flow-mapping": "description: {approved: flow}",
        }
        for case_id, crafted_line in cases.items():
            with self.subTest(case=case_id):
                definition = {
                    "id": f"unsafe-frontmatter-{case_id}",
                    "project": {
                        "project_id": f"unsafe-frontmatter-{case_id}",
                        "relative_path": f"unsafe-frontmatter-{case_id}",
                        "classification": "project",
                    },
                    "nodes": [
                        {
                            "type": "file",
                            "path": (
                                f"unsafe-frontmatter-{case_id}/.agents/skills/unsafe/SKILL.md"
                            ),
                            "content_utf8": (
                                f"---\nname: safe-name\n{crafted_line}\n---\n"
                                f"UNSAFE_{case_id.upper()}_BODY_SENTINEL\n"
                            ),
                        }
                    ],
                }
                payload, _ = self._scan_definition(definition)
                self.assertEqual(payload["scan_status"], "partial")
                self.assertEqual(_flatten_observations(payload), [])
                self.assertIn("unsafe_frontmatter", {row["code"] for row in payload["issues"]})
                serialized = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn(crafted_line, serialized)
                self.assertNotIn(f"UNSAFE_{case_id.upper()}_BODY_SENTINEL", serialized)

    def test_yaml_tag_handle_variants_fail_closed_in_frontmatter_and_openai(self) -> None:
        cases = {
            "non-specific": "! value",
            "dot": "!.foo value",
            "slash": "!/foo value",
            "question": "!?foo value",
            "colon": "!:foo value",
            "at": "!@foo value",
        }
        for case_id, tagged_scalar in cases.items():
            with self.subTest(case=case_id):
                project_id = f"yaml-tag-{case_id}"
                openai_name = f"openai-{case_id}"
                definition = {
                    "id": project_id,
                    "project": {
                        "project_id": project_id,
                        "relative_path": project_id,
                        "classification": "project",
                    },
                    "nodes": [
                        {
                            "type": "file",
                            "path": f"{project_id}/.agents/skills/frontmatter/SKILL.md",
                            "content_utf8": (
                                "---\nname: frontmatter-tag\n"
                                f"description: {tagged_scalar}\n"
                                "---\nTAGGED_FRONTMATTER_BODY_SENTINEL\n"
                            ),
                        },
                        {
                            "type": "file",
                            "path": f"{project_id}/.agents/skills/{openai_name}/SKILL.md",
                            "content_utf8": (
                                f"---\nname: {openai_name}\n"
                                "description: OpenAI tag fixture.\n---\n"
                            ),
                        },
                        {
                            "type": "file",
                            "path": (
                                f"{project_id}/.agents/skills/{openai_name}/agents/openai.yaml"
                            ),
                            "content_utf8": (
                                "metadata:\n"
                                f"  tagged: {tagged_scalar}\n"
                                "policy:\n"
                                "  allow_implicit_invocation: true\n"
                            ),
                        },
                    ],
                }
                payload, _ = self._scan_definition(definition)
                self.assertEqual(payload["scan_status"], "partial")
                observations = _flatten_observations(payload)
                self.assertEqual(len(observations), 1)
                self.assertEqual(
                    observations[0]["frontmatter_projection"]["name"],
                    openai_name,
                )
                self.assertEqual(
                    observations[0]["openai_declaration"],
                    {"status": "unverified", "reason": "invalid_yaml"},
                )
                self.assertNotIn(
                    "allow_implicit_invocation",
                    observations[0]["openai_declaration"],
                )
                codes = {row["code"] for row in payload["issues"]}
                self.assertTrue(
                    {"unsafe_frontmatter", "openai_invalid_yaml"}.issubset(codes)
                )
                serialized = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn(tagged_scalar, serialized)
                self.assertNotIn("TAGGED_FRONTMATTER_BODY_SENTINEL", serialized)

    def test_invalid_unknown_yaml_subtrees_fail_closed_in_frontmatter_and_openai(self) -> None:
        cases = {
            "scalar-with-child": "metadata: scalar\n  child: value\n",
            "orphan-scalar": "metadata:\n  orphan scalar\n",
            "sequence-with-nested-sequence": (
                "metadata:\n  - key: value\n    - nested\n"
            ),
            "otherwise-valid-sequence": (
                "metadata:\n  - key: value\n  - key: other\n"
            ),
        }
        for case_id, invalid_subtree in cases.items():
            with self.subTest(case=case_id):
                project_id = f"invalid-unknown-{case_id}"
                openai_name = f"openai-{case_id}"
                definition = {
                    "id": project_id,
                    "project": {
                        "project_id": project_id,
                        "relative_path": project_id,
                        "classification": "project",
                    },
                    "nodes": [
                        {
                            "type": "file",
                            "path": f"{project_id}/.agents/skills/frontmatter/SKILL.md",
                            "content_utf8": (
                                "---\nname: invalid-frontmatter\n"
                                "description: Approved description.\n"
                                f"{invalid_subtree}"
                                "---\nINVALID_UNKNOWN_BODY_SENTINEL\n"
                            ),
                        },
                        {
                            "type": "file",
                            "path": f"{project_id}/.agents/skills/{openai_name}/SKILL.md",
                            "content_utf8": (
                                f"---\nname: {openai_name}\n"
                                "description: Invalid unknown subtree fixture.\n---\n"
                            ),
                        },
                        {
                            "type": "file",
                            "path": (
                                f"{project_id}/.agents/skills/{openai_name}/agents/openai.yaml"
                            ),
                            "content_utf8": (
                                f"{invalid_subtree}"
                                "policy:\n"
                                "  allow_implicit_invocation: true\n"
                            ),
                        },
                    ],
                }
                payload, _ = self._scan_definition(definition)
                self.assertEqual(payload["scan_status"], "partial")
                observations = _flatten_observations(payload)
                self.assertEqual(len(observations), 1)
                self.assertEqual(
                    observations[0]["frontmatter_projection"]["name"],
                    openai_name,
                )
                self.assertEqual(
                    observations[0]["openai_declaration"],
                    {"status": "unverified", "reason": "invalid_yaml"},
                )
                self.assertNotIn(
                    "allow_implicit_invocation",
                    observations[0]["openai_declaration"],
                )
                codes = {row["code"] for row in payload["issues"]}
                self.assertTrue(
                    {"unsafe_frontmatter", "openai_invalid_yaml"}.issubset(codes)
                )
                serialized = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn(invalid_subtree.strip(), serialized)
                self.assertNotIn("INVALID_UNKNOWN_BODY_SENTINEL", serialized)

    def test_invalid_unknown_yaml_scalars_fail_closed_in_frontmatter_and_openai(self) -> None:
        noncharacters = list(range(0xFDD0, 0xFDF0)) + [
            plane * 0x10000 + suffix
            for plane in range(17)
            for suffix in (0xFFFE, 0xFFFF)
        ]
        cases = {
            "invalid-double-escape": 'metadata: "bad\\q"\n',
            "plain-colon-space": "metadata: safe: broken\n",
            "mid-single-quote-colon-space": "metadata: safe 'x: y'\n",
            "mid-double-quote-colon-space": 'metadata: safe "x: y"\n',
            "reserved-percent": "metadata: %value\n",
            "reserved-at": "metadata: @value\n",
            "reserved-backtick": "metadata: `value\n",
            "reserved-close-bracket": "metadata: ]value\n",
            "reserved-close-brace": "metadata: }value\n",
            "reserved-comma": "metadata: ,value\n",
            "c0-nul": "metadata: safe\x00value\n",
            "c0-soh": "metadata: safe\x01value\n",
            **{
                f"c1-{codepoint:02x}": f"metadata: safe{chr(codepoint)}value\n"
                for codepoint in range(0x80, 0xA0)
            },
            **{
                f"noncharacter-{codepoint:06x}": (
                    f"metadata: safe{chr(codepoint)}value\n"
                )
                for codepoint in noncharacters
            },
        }
        for case_id, invalid_scalar in cases.items():
            with self.subTest(case=case_id):
                project_id = f"invalid-unknown-scalar-{case_id}"
                openai_name = f"openai-{case_id}"
                definition = {
                    "id": project_id,
                    "project": {
                        "project_id": project_id,
                        "relative_path": project_id,
                        "classification": "project",
                    },
                    "nodes": [
                        {
                            "type": "file",
                            "path": f"{project_id}/.agents/skills/frontmatter/SKILL.md",
                            "content_utf8": (
                                "---\nname: invalid-frontmatter\n"
                                "description: Approved description.\n"
                                f"{invalid_scalar}"
                                "---\nINVALID_UNKNOWN_SCALAR_BODY_SENTINEL\n"
                            ),
                        },
                        {
                            "type": "file",
                            "path": f"{project_id}/.agents/skills/{openai_name}/SKILL.md",
                            "content_utf8": (
                                f"---\nname: {openai_name}\n"
                                "description: Invalid unknown scalar fixture.\n---\n"
                            ),
                        },
                        {
                            "type": "file",
                            "path": (
                                f"{project_id}/.agents/skills/{openai_name}/agents/openai.yaml"
                            ),
                            "content_utf8": (
                                f"{invalid_scalar}"
                                "policy:\n"
                                "  allow_implicit_invocation: true\n"
                            ),
                        },
                    ],
                }
                payload, _ = self._scan_definition(definition)
                self.assertEqual(payload["scan_status"], "partial")
                observations = _flatten_observations(payload)
                self.assertEqual(len(observations), 1)
                self.assertEqual(
                    observations[0]["frontmatter_projection"]["name"],
                    openai_name,
                )
                self.assertEqual(
                    observations[0]["openai_declaration"],
                    {"status": "unverified", "reason": "invalid_yaml"},
                )
                self.assertNotIn(
                    "allow_implicit_invocation",
                    observations[0]["openai_declaration"],
                )
                codes = {row["code"] for row in payload["issues"]}
                self.assertTrue(
                    {"unsafe_frontmatter", "openai_invalid_yaml"}.issubset(codes)
                )
                serialized = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn(invalid_scalar.strip(), serialized)
                self.assertNotIn("INVALID_UNKNOWN_SCALAR_BODY_SENTINEL", serialized)

    def test_surrogate_utf8_encodings_fail_closed_in_frontmatter_and_openai(self) -> None:
        cases = {
            "high-start": b"\xed\xa0\x80",
            "high-end": b"\xed\xaf\xbf",
            "low-start": b"\xed\xb0\x80",
            "low-end": b"\xed\xbf\xbf",
        }
        for case_id, surrogate_bytes in cases.items():
            with self.subTest(case=case_id):
                project_id = f"surrogate-{case_id}"
                openai_name = f"openai-{case_id}"
                frontmatter = (
                    b"---\nname: surrogate-frontmatter\ndescription: safe\nmetadata: bad"
                    + surrogate_bytes
                    + b"value\n---\nSURROGATE_BODY_SENTINEL\n"
                )
                openai = (
                    b"metadata: bad"
                    + surrogate_bytes
                    + b"value\npolicy:\n  allow_implicit_invocation: true\n"
                )
                definition = {
                    "id": project_id,
                    "project": {
                        "project_id": project_id,
                        "relative_path": project_id,
                        "classification": "project",
                    },
                    "nodes": [
                        {
                            "type": "file",
                            "path": f"{project_id}/.agents/skills/frontmatter/SKILL.md",
                            "content_base64": base64.b64encode(frontmatter).decode("ascii"),
                        },
                        {
                            "type": "file",
                            "path": f"{project_id}/.agents/skills/{openai_name}/SKILL.md",
                            "content_utf8": (
                                f"---\nname: {openai_name}\n"
                                "description: Surrogate encoding fixture.\n---\n"
                            ),
                        },
                        {
                            "type": "file",
                            "path": (
                                f"{project_id}/.agents/skills/{openai_name}/agents/openai.yaml"
                            ),
                            "content_base64": base64.b64encode(openai).decode("ascii"),
                        },
                    ],
                }
                payload, _ = self._scan_definition(definition)
                self.assertEqual(payload["scan_status"], "partial")
                observations = _flatten_observations(payload)
                self.assertEqual(len(observations), 1)
                self.assertEqual(
                    observations[0]["openai_declaration"],
                    {"status": "unverified", "reason": "invalid_yaml"},
                )
                codes = {row["code"] for row in payload["issues"]}
                self.assertTrue(
                    {"invalid_encoding", "openai_invalid_yaml"}.issubset(codes)
                )
                self.assertNotIn(
                    "SURROGATE_BODY_SENTINEL",
                    json.dumps(payload, ensure_ascii=False),
                )

    def test_mid_scalar_quotes_do_not_hide_comments_but_initial_quotes_remain_literal(self) -> None:
        for case_id, quote, escaped_quote in (
            ("single", "'", "&#x27;"),
            ("double", '"', "&quot;"),
        ):
            with self.subTest(kind="mid-scalar-comment", quote=case_id):
                sentinel = f"MID_{case_id.upper()}_COMMENT_SENTINEL"
                project_id = f"mid-quote-comment-{case_id}"
                definition = {
                    "id": project_id,
                    "project": {
                        "project_id": project_id,
                        "relative_path": project_id,
                        "classification": "project",
                    },
                    "nodes": [
                        {
                            "type": "file",
                            "path": f"{project_id}/.agents/skills/safe/SKILL.md",
                            "content_utf8": (
                                f"---\nname: safe-{case_id}\n"
                                f"description: safe {quote} # {sentinel}{quote}\n"
                                f"metadata: safe {quote} # UNKNOWN_{sentinel}{quote}\n"
                                "---\nMID_QUOTE_BODY_SENTINEL\n"
                            ),
                        },
                        {
                            "type": "file",
                            "path": f"{project_id}/.agents/skills/safe/agents/openai.yaml",
                            "content_utf8": (
                                f"metadata: safe {quote} # OPENAI_{sentinel}{quote}\n"
                                "policy:\n"
                                "  allow_implicit_invocation: true\n"
                            ),
                        },
                    ],
                }
                payload, _ = self._scan_definition(definition)
                self.assertEqual(payload["scan_status"], "complete")
                observation = _flatten_observations(payload)[0]
                self.assertEqual(
                    observation["frontmatter_projection"]["description"],
                    f"safe {escaped_quote}",
                )
                self.assertEqual(
                    observation["openai_declaration"],
                    {"status": "declared", "allow_implicit_invocation": True},
                )
                serialized = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn(sentinel, serialized)
                self.assertNotIn(f"UNKNOWN_{sentinel}", serialized)
                self.assertNotIn(f"OPENAI_{sentinel}", serialized)
                self.assertNotIn("MID_QUOTE_BODY_SENTINEL", serialized)

            with self.subTest(kind="initial-quoted-literal", quote=case_id):
                literal = "safe: value # literal"
                quoted = f"{quote}{literal}{quote}"
                project_id = f"initial-quoted-{case_id}"
                definition = {
                    "id": project_id,
                    "project": {
                        "project_id": project_id,
                        "relative_path": project_id,
                        "classification": "project",
                    },
                    "nodes": [
                        {
                            "type": "file",
                            "path": f"{project_id}/.agents/skills/safe/SKILL.md",
                            "content_utf8": (
                                f"---\nname: quoted-{case_id}\n"
                                f"description: {quoted}\nmetadata: {quoted}\n---\n"
                            ),
                        },
                        {
                            "type": "file",
                            "path": f"{project_id}/.agents/skills/safe/agents/openai.yaml",
                            "content_utf8": (
                                f"metadata: {quoted}\npolicy:\n"
                                "  allow_implicit_invocation: true\n"
                            ),
                        },
                    ],
                }
                payload, _ = self._scan_definition(definition)
                self.assertEqual(payload["scan_status"], "complete")
                observation = _flatten_observations(payload)[0]
                self.assertEqual(
                    observation["frontmatter_projection"]["description"],
                    literal,
                )
                self.assertEqual(
                    observation["openai_declaration"],
                    {"status": "declared", "allow_implicit_invocation": True},
                )

    def test_implicit_multiline_description_lexes_each_continuation_before_folding(self) -> None:
        inline_sentinel = "MULTILINE_INLINE_COMMENT_SENTINEL"
        comment_only_sentinel = "MULTILINE_COMMENT_ONLY_SENTINEL"
        definition = {
            "id": "implicit-multiline-positive",
            "project": {
                "project_id": "implicit-multiline-positive",
                "relative_path": "implicit-multiline-positive",
                "classification": "project",
            },
            "nodes": [
                {
                    "type": "file",
                    "path": "implicit-multiline-positive/.agents/skills/legal/SKILL.md",
                    "content_utf8": (
                        "---\nname: legal\ndescription:\n"
                        "  合法第一行\n"
                        "  middle@marker 50%complete\n"
                        "---\n"
                    ),
                },
                {
                    "type": "file",
                    "path": "implicit-multiline-positive/.agents/skills/comment/SKILL.md",
                    "content_utf8": (
                        "---\nname: comment\ndescription:\n"
                        "  safe\n"
                        f"  # {comment_only_sentinel}\n"
                        f"  next # {inline_sentinel}\n"
                        "---\n"
                    ),
                },
            ],
        }
        payload, _ = self._scan_definition(definition)
        self.assertEqual(payload["scan_status"], "complete")
        observations = {
            row["frontmatter_projection"]["name"]: row
            for row in _flatten_observations(payload)
        }
        self.assertEqual(
            observations["legal"]["frontmatter_projection"]["description"],
            "合法第一行 middle@marker 50%complete",
        )
        self.assertEqual(
            observations["comment"]["frontmatter_projection"]["description"],
            "safe next",
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(inline_sentinel, serialized)
        self.assertNotIn(comment_only_sentinel, serialized)

        invalid_blocks = {
            "colon-space": "  safe\n  next value: broken\n",
            "tag": "  safe\n  ! value\n",
            "control": "  safe\n  next\x01value\n",
            "key": "  safe\n  nested: value\n",
            "list": "  safe\n  - item\n",
            "nested-indent": "  safe\n    nested\n",
            "double-quoted-then-plain": '  "first"\n  second\n',
            "two-double-quoted": '  "first"\n  "second"\n',
            "plain-then-double-quoted": '  first\n  "second"\n',
            "single-quoted-then-plain": "  'first'\n  second\n",
            "two-single-quoted": "  'first'\n  'second'\n",
            "plain-then-single-quoted": "  first\n  'second'\n",
        }
        for case_id, continuation in invalid_blocks.items():
            with self.subTest(kind="invalid-continuation", case=case_id):
                project_id = f"implicit-multiline-{case_id}"
                definition = {
                    "id": project_id,
                    "project": {
                        "project_id": project_id,
                        "relative_path": project_id,
                        "classification": "project",
                    },
                    "nodes": [
                        {
                            "type": "file",
                            "path": f"{project_id}/.agents/skills/invalid/SKILL.md",
                            "content_utf8": (
                                "---\nname: invalid\ndescription:\n"
                                f"{continuation}"
                                "---\nINVALID_MULTILINE_BODY_SENTINEL\n"
                            ),
                        }
                    ],
                }
                payload, _ = self._scan_definition(definition)
                self.assertEqual(payload["scan_status"], "partial")
                self.assertEqual(_flatten_observations(payload), [])
                self.assertIn(
                    "unsafe_frontmatter",
                    {row["code"] for row in payload["issues"]},
                )
                self.assertNotIn(
                    "INVALID_MULTILINE_BODY_SENTINEL",
                    json.dumps(payload, ensure_ascii=False),
                )

    def test_unicode_punctuation_and_mid_scalar_at_percent_remain_allowed(self) -> None:
        approved = "中文，正常。\ufffd\U00010000 middle@marker 50%complete - ok"
        definition = {
            "id": "allowed-plain-scalars",
            "project": {
                "project_id": "allowed-plain-scalars",
                "relative_path": "allowed-plain-scalars",
                "classification": "project",
            },
            "nodes": [
                {
                    "type": "file",
                    "path": "allowed-plain-scalars/.agents/skills/allowed/SKILL.md",
                    "content_utf8": (
                        "---\nname: allowed\n"
                        f"description: {approved}\n"
                        f"metadata: {approved}\n"
                        "---\nALLOWED_PLAIN_BODY_SENTINEL\n"
                    ),
                },
                {
                    "type": "file",
                    "path": "allowed-plain-scalars/.agents/skills/allowed/agents/openai.yaml",
                    "content_utf8": (
                        f"metadata: {approved}\n"
                        "policy:\n"
                        "  allow_implicit_invocation: true\n"
                    ),
                },
            ],
        }
        payload, _ = self._scan_definition(definition)
        self.assertEqual(payload["scan_status"], "complete")
        observation = _flatten_observations(payload)[0]
        self.assertEqual(
            observation["frontmatter_projection"]["description"],
            approved,
        )
        self.assertEqual(
            observation["openai_declaration"],
            {"status": "declared", "allow_implicit_invocation": True},
        )
        self.assertNotIn(
            "ALLOWED_PLAIN_BODY_SENTINEL",
            json.dumps(payload, ensure_ascii=False),
        )

    def test_malicious_yaml_invalid_utf8_and_unclosed_frontmatter_fail_closed(self) -> None:
        payload, _ = self._scan_materialized("malicious-frontmatter")
        codes = {row["code"] for row in payload["issues"]}
        self.assertEqual(payload["scan_status"], "partial")
        self.assertTrue({"unsafe_frontmatter", "invalid_frontmatter", "invalid_encoding"}.issubset(codes))
        self.assertEqual(len(_flatten_observations(payload)), 1)
        observation = _flatten_observations(payload)[0]
        self.assertEqual(observation["frontmatter_projection"]["name"], "untrusted-markup")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("!!python", serialized)
        self.assertNotIn("&shared", serialized)
        self.assertNotIn("*shared", serialized)
        self.assertNotIn("<script>", serialized)
        self.assertIn("&lt;script&gt;", serialized)

    def test_manifest_frontmatter_and_text_limits_fail_closed(self) -> None:
        payload, _ = self._scan_materialized("metadata-limits")
        self.assertEqual(payload["scan_status"], "partial")
        self.assertEqual(_project(payload, "fixture-metadata-limits")["logical_skills"], [])
        self.assertEqual(
            {row["code"] for row in payload["issues"]},
            {"manifest_budget_exceeded", "frontmatter_budget_exceeded", "text_length_exceeded"},
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("z" * 100, serialized)
        self.assertNotIn("y" * 100, serialized)

    def test_openai_declaration_true_false_not_observed_and_unverified_exact_file_only(self) -> None:
        definition = {
            "id": "openai-declarations",
            "project": {"project_id": "openai-cases", "relative_path": "openai-cases", "classification": "project"},
            "nodes": [
                {
                    "type": "file",
                    "path": f"openai-cases/.agents/skills/{name}/SKILL.md",
                    "content_utf8": f"---\nname: {name}\ndescription: Declaration fixture.\n---\n",
                }
                for name in ("true-case", "false-case", "missing-case", "invalid-case", "wrong-file-case")
            ]
            + [
                {"type": "file", "path": "openai-cases/.agents/skills/true-case/agents/openai.yaml", "content_utf8": "policy:\n  allow_implicit_invocation: true\n"},
                {"type": "file", "path": "openai-cases/.agents/skills/false-case/agents/openai.yaml", "content_utf8": "policy:\n  allow_implicit_invocation: false\n"},
                {"type": "file", "path": "openai-cases/.agents/skills/invalid-case/agents/openai.yaml", "content_utf8": "policy: !!unsafe\n"},
                {"type": "file", "path": "openai-cases/.agents/skills/wrong-file-case/agents/not-openai.yaml", "content_utf8": "policy:\n  allow_implicit_invocation: true\n"},
            ],
        }
        payload, calls = self._scan_definition(definition)
        by_name = {
            skill["display_name"]: skill["observations"][0]
            for skill in _project(payload, "openai-cases")["logical_skills"]
        }
        self.assertEqual(by_name["true-case"]["openai_declaration"], {"status": "declared", "allow_implicit_invocation": True})
        self.assertEqual(by_name["false-case"]["openai_declaration"], {"status": "declared", "allow_implicit_invocation": False})
        self.assertEqual(by_name["missing-case"]["openai_declaration"], {"status": "not_observed"})
        self.assertEqual(by_name["wrong-file-case"]["openai_declaration"], {"status": "not_observed"})
        self.assertEqual(by_name["invalid-case"]["openai_declaration"], {"status": "unverified", "reason": "invalid_yaml"})
        self.assertNotIn("not-openai.yaml", [path for path, _flags in calls["os_open"]])
        for observation in by_name.values():
            status = observation["openai_declaration"]["status"]
            expected = {"declared": "declaration_only", "not_observed": "not_observed", "unverified": "unverified"}[status]
            self.assertEqual(observation["evidence"]["invocation_eligibility"]["status"], expected)

    def test_openai_ignores_other_top_level_sections_and_recognizes_only_direct_policy_bool(self) -> None:
        secret = "OPENAI_UNKNOWN_SECTION_TOKEN_MUST_NOT_LEAK"
        definition = {
            "id": "openai-structural-allowlist",
            "project": {
                "project_id": "openai-structural-allowlist",
                "relative_path": "openai-structural-allowlist",
                "classification": "project",
            },
            "nodes": [
                {
                    "type": "file",
                    "path": f"openai-structural-allowlist/.agents/skills/{name}/SKILL.md",
                    "content_utf8": f"---\nname: {name}\ndescription: OpenAI structure fixture.\n---\n",
                }
                for name in ("direct-true", "nested-metadata", "nested-policy")
            ]
            + [
                {
                    "type": "file",
                    "path": "openai-structural-allowlist/.agents/skills/direct-true/agents/openai.yaml",
                    "content_utf8": (
                        "metadata:\n"
                        "  owner: fixture\n"
                        f"  token: {secret}\n"
                        "policy:\n"
                        "  allow_implicit_invocation: true\n"
                        "runtime:\n"
                        "  mode: local\n"
                    ),
                },
                {
                    "type": "file",
                    "path": "openai-structural-allowlist/.agents/skills/nested-metadata/agents/openai.yaml",
                    "content_utf8": (
                        "metadata:\n"
                        "  policy:\n"
                        "    allow_implicit_invocation: true\n"
                    ),
                },
                {
                    "type": "file",
                    "path": "openai-structural-allowlist/.agents/skills/nested-policy/agents/openai.yaml",
                    "content_utf8": (
                        "policy:\n"
                        "  nested:\n"
                        "    allow_implicit_invocation: true\n"
                    ),
                },
            ],
        }
        payload, _ = self._scan_definition(definition)
        self.assertEqual(payload["scan_status"], "complete")
        observations = {
            row["frontmatter_projection"]["name"]: row
            for row in _flatten_observations(payload)
        }
        self.assertEqual(
            observations["direct-true"]["openai_declaration"],
            {"status": "declared", "allow_implicit_invocation": True},
        )
        self.assertEqual(
            observations["nested-metadata"]["openai_declaration"],
            {"status": "not_observed"},
        )
        self.assertEqual(
            observations["nested-policy"]["openai_declaration"],
            {"status": "not_observed"},
        )
        self.assertNotIn(secret, json.dumps(payload, ensure_ascii=False))

    def test_openai_dangerous_yaml_is_unverified_without_content_leakage(self) -> None:
        cases = {
            "tag": "policy:\n  allow_implicit_invocation: !!bool true\n",
            "anchor": "defaults: &defaults\n  allow_implicit_invocation: true\n",
            "alias": "policy:\n  allow_implicit_invocation: *flag\n",
            "merge": "policy:\n  <<: *defaults\n",
            "flow": "policy: {allow_implicit_invocation: true}\n",
        }
        definition = {
            "id": "openai-dangerous-yaml",
            "project": {
                "project_id": "openai-dangerous-yaml",
                "relative_path": "openai-dangerous-yaml",
                "classification": "project",
            },
            "nodes": [
                {
                    "type": "file",
                    "path": f"openai-dangerous-yaml/.agents/skills/{name}/SKILL.md",
                    "content_utf8": f"---\nname: {name}\ndescription: Dangerous YAML fixture.\n---\n",
                }
                for name in cases
            ]
            + [
                {
                    "type": "file",
                    "path": f"openai-dangerous-yaml/.agents/skills/{name}/agents/openai.yaml",
                    "content_utf8": content,
                }
                for name, content in cases.items()
            ],
        }
        payload, _ = self._scan_definition(definition)
        self.assertEqual(payload["scan_status"], "partial")
        observations = {
            row["frontmatter_projection"]["name"]: row
            for row in _flatten_observations(payload)
        }
        self.assertEqual(set(observations), set(cases))
        for name in cases:
            with self.subTest(case=name):
                self.assertEqual(
                    observations[name]["openai_declaration"],
                    {"status": "unverified", "reason": "invalid_yaml"},
                )
                self.assertEqual(
                    observations[name]["evidence"]["invocation_eligibility"]["status"],
                    "unverified",
                )
        self.assertIn("openai_invalid_yaml", {row["code"] for row in payload["issues"]})
        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in ("!!bool", "&defaults", "*flag", "<<:", "{allow_implicit_invocation"):
            self.assertNotIn(forbidden, serialized)

    def test_broken_outside_loop_and_descendant_links_fail_or_skip_without_target_content(self) -> None:
        cases = {
            "broken-entry-link": "broken_link",
            "outside-entry-link": "outside_project_link",
            "loop-entry-links": "loop_link",
        }
        for case_id, expected_code in cases.items():
            with self.subTest(case=case_id):
                payload, _ = self._scan_materialized("path-safety", case_id=case_id)
                self.assertEqual(payload["scan_status"], "partial")
                self.assertIn(expected_code, {row["code"] for row in payload["issues"]})
                self.assertEqual(_flatten_observations(payload), [])
                for sentinel in load_scenario("path-safety")["cases"]:
                    if sentinel["case_id"] == case_id:
                        for value in sentinel["expected"].get("must_not_emit", []):
                            self.assertNotIn(value, json.dumps(payload, ensure_ascii=False))

        descendant, calls = self._scan_materialized("path-safety", case_id="descendant-link-skip")
        self.assertEqual(descendant["scan_status"], "complete")
        self.assertEqual(len(_flatten_observations(descendant)), 1)
        self.assertNotIn("external.txt", [path for path, _flags in calls["os_open"]])
        self.assertNotIn("DESCENDANT_TARGET_SENTINEL_DO_NOT_READ_OR_EMIT", json.dumps(descendant))

    def test_root_project_and_project_ancestor_symlinks_reject_before_manifest_read(self) -> None:
        cases = {
            "observation-root-is-link": "observation_root_symlink",
            "project-root-is-link": "project_root_symlink",
            "project-ancestor-is-link": "project_ancestor_symlink",
        }
        for case_id, expected_code in cases.items():
            with self.subTest(case=case_id):
                payload, calls = self._scan_materialized("path-safety", case_id=case_id)
                self.assertEqual(payload["scan_status"], "security_reject")
                self.assertIn(expected_code, {row["code"] for row in payload["issues"]})
                self.assertEqual(_flatten_observations(payload), [])
                self.assertNotIn("SKILL.md", [path for path, _flags in calls["os_open"]])

    def test_manifest_toctou_swap_is_rejected_and_replacement_body_never_opens(self) -> None:
        specification = load_scenario("path-safety")
        with tempfile.TemporaryDirectory(prefix="project-skill-scan-") as temporary:
            result = materialize_scenario(specification, temporary, case_id="manifest-toctou-swap")
            target = result.scan_targets[0]
            relative = target["project_root"].relative_to(target["observation_root"]).as_posix()
            projects, associations = _registries(
                [{"project_id": "fixture-race", "relative_path": relative, "classification": "project"}]
            )
            swapped = False

            def racing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
                nonlocal swapped
                if path == "SKILL.md" and not swapped:
                    swapped = True
                    parent_fd = kwargs["dir_fd"]
                    _REAL_OS_UNLINK(path, dir_fd=parent_fd)
                    _REAL_OS_SYMLINK(
                        "../../../../outside/raced/SKILL.md",
                        path,
                        dir_fd=parent_fd,
                    )
                return _REAL_OS_OPEN(path, flags, *args, **kwargs)

            scan._require_descriptor_safety()
            with mock.patch.object(scan, "_require_descriptor_safety", return_value=None), mock.patch.object(
                scan.os, "open", side_effect=racing_open
            ):
                payload = scan.build_project_skill_snapshot(
                    target["observation_root"].resolve(strict=True),
                    projects,
                    associations,
                    generated_at=FIXED_TIME,
                )
        self.assertTrue(swapped)
        self.assertEqual(payload["scan_status"], "partial")
        self.assertIn("path_race", {row["code"] for row in payload["issues"]})
        self.assertEqual(_flatten_observations(payload), [])
        self.assertNotIn("TOCTOU_TARGET_SENTINEL_DO_NOT_READ_OR_EMIT", json.dumps(payload))

    def test_permission_oserror_is_generic_relative_and_reports_zero(self) -> None:
        specification = load_scenario("path-safety")
        with tempfile.TemporaryDirectory(prefix="project-skill-scan-") as temporary:
            result = materialize_scenario(specification, temporary, case_id="manifest-permission-denied")
            target = result.scan_targets[0]
            relative = target["project_root"].relative_to(target["observation_root"]).as_posix()
            projects, associations = _registries(
                [{"project_id": "fixture-denied", "relative_path": relative, "classification": "project"}]
            )

            def denied_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
                if path == "SKILL.md":
                    raise PermissionError(errno.EACCES, "synthetic secret absolute path")
                return _REAL_OS_OPEN(path, flags, *args, **kwargs)

            scan._require_descriptor_safety()
            with mock.patch.object(scan, "_require_descriptor_safety", return_value=None), mock.patch.object(
                scan.os, "open", side_effect=denied_open
            ):
                payload = scan.build_project_skill_snapshot(
                    target["observation_root"].resolve(strict=True),
                    projects,
                    associations,
                    generated_at=FIXED_TIME,
                )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["scan_status"], "partial")
        self.assertIn("permission_denied", {row["code"] for row in payload["issues"]})
        self.assertEqual(_flatten_observations(payload), [])
        self.assertNotIn(temporary, serialized)
        self.assertNotIn("synthetic secret absolute path", serialized)
        self.assertNotIn("/" + "Users/", serialized)

    def test_budget_constants_and_depth_are_frozen(self) -> None:
        self.assertEqual(
            scan.LIMITS,
            {
                "max_project_candidates": 256,
                "max_entries_per_project": 1000,
                "max_depth": 5,
                "max_manifest_bytes": 262144,
                "max_frontmatter_bytes": 32768,
                "max_text_length": 4000,
                "scan_timeout_seconds": 12.0,
            },
        )
        payload, _ = self._scan_materialized("multi-evidence")
        self.assertLessEqual(max(row["observation_depth"] for row in _flatten_observations(payload)), 5)

    def test_candidate_budget_256_fails_partial_without_overflow(self) -> None:
        with tempfile.TemporaryDirectory(prefix="project-skill-scan-") as temporary:
            root = Path(temporary).resolve()
            (root / "registered").mkdir()
            for index in range(256):
                (root / f"candidate-{index:03d}").mkdir()
            projects, associations = _registries(
                [{"project_id": "registered", "relative_path": "registered", "classification": "project"}]
            )
            payload = scan.build_project_skill_snapshot(
                root, projects, associations, generated_at=FIXED_TIME
            )
        self.assertEqual(payload["scan_status"], "partial")
        self.assertLessEqual(len(payload["candidates"]), 256)
        self.assertIn("candidate_budget_exceeded", {row["code"] for row in payload["issues"]})
        self.assertNotIn("changes", payload)

    def test_entry_budget_1000_fails_partial_and_reports_no_illegal_zero_as_complete(self) -> None:
        with tempfile.TemporaryDirectory(prefix="project-skill-scan-") as temporary:
            root = Path(temporary).resolve()
            entry = root / "budget-project/.agents/skills"
            entry.mkdir(parents=True)
            for index in range(1001):
                (entry / f"entry-{index:04d}").mkdir()
            projects, associations = _registries(
                [{"project_id": "budget-project", "relative_path": "budget-project", "classification": "project"}]
            )
            payload = scan.build_project_skill_snapshot(
                root, projects, associations, generated_at=FIXED_TIME
            )
        project = _project(payload, "budget-project")
        self.assertEqual(payload["scan_status"], "partial")
        self.assertEqual(project["scan_status"], "partial")
        self.assertEqual(project["logical_skills"], [])
        self.assertIn("entry_budget_exceeded", {row["code"] for row in payload["issues"]})
        self.assertNotIn("changes", payload)

    def test_twelve_second_budget_expires_fail_closed(self) -> None:
        values = iter([0.0] + [13.0] * 1000)

        def clock() -> float:
            return next(values)

        payload, _ = self._scan_materialized("empty-entry", monotonic=clock)
        self.assertEqual(payload["scan_status"], "partial")
        self.assertIn("scan_timeout", {row["code"] for row in payload["issues"]})
        self.assertNotIn("changes", payload)
        self.assertEqual(_flatten_observations(payload), [])

    def test_all_scans_have_zero_filesystem_process_and_network_side_effects(self) -> None:
        before = {
            PROJECTS_PATH: PROJECTS_PATH.read_bytes(),
            ASSOCIATIONS_PATH: ASSOCIATIONS_PATH.read_bytes(),
        }
        for scenario_id in (
            "empty-entry",
            "five-entities",
            "marketplace-bait",
            "multi-evidence",
            "human-only",
            "codex-path-only",
            "entry-semantics",
            "excluded-boundaries",
        ):
            with self.subTest(scenario=scenario_id):
                _payload, calls = self._scan_materialized(scenario_id)
                self.assertEqual(calls["writes"], [])
        self.assertEqual(PROJECTS_PATH.read_bytes(), before[PROJECTS_PATH])
        self.assertEqual(ASSOCIATIONS_PATH.read_bytes(), before[ASSOCIATIONS_PATH])

    def test_payload_matches_snapshot_schema_key_shapes_and_changes_rule(self) -> None:
        payload, _ = self._scan_materialized("safe-six-frontmatter")
        schema = self.snapshot_schema

        def exact_shape(value: Mapping[str, Any], definition: Mapping[str, Any]) -> None:
            self.assertTrue(set(definition["required"]).issubset(value))
            self.assertTrue(set(value).issubset(definition["properties"]))
            self.assertFalse(definition["additionalProperties"])

        exact_shape(payload, schema)
        self.assertEqual(payload["scan_status"], "complete")
        self.assertIn("changes", payload)
        self.assertEqual(
            payload["changes"],
            {
                "status": "not_available",
                "reason": "no_prior_complete_snapshot",
                "fingerprint_basis": "projection_sha256_v1",
            },
        )
        for project in payload["projects"]:
            exact_shape(project, schema["$defs"]["project_observation"])
            for entry in project["entries"]:
                exact_shape(entry, schema["$defs"]["entry_observation"])
            for skill in project["logical_skills"]:
                exact_shape(skill, schema["$defs"]["logical_skill"])
                for observation in skill["observations"]:
                    exact_shape(observation, schema["$defs"]["skill_observation"])
                    exact_shape(
                        observation["frontmatter_projection"],
                        schema["$defs"]["frontmatter_projection"],
                    )
                    exact_shape(observation["evidence"], schema["$defs"]["five_layer_evidence"])
        exact_shape(payload["human_associations"], schema["$defs"]["human_association_section"])
        self.assertEqual(payload["scan_scope"]["limits"], scan.LIMITS)

        partial, _ = self._scan_materialized("malicious-frontmatter")
        self.assertNotIn("changes", partial)

    def test_fixed_time_preview_is_deterministic_and_contains_only_safe_relative_paths(self) -> None:
        specification = load_scenario("multi-evidence")
        with tempfile.TemporaryDirectory(prefix="project-skill-scan-") as temporary:
            materialized = materialize_scenario(specification, temporary)
            projects, associations = _registries(_scenario_projects(specification))
            first = scan.build_project_skill_snapshot(
                materialized.root.resolve(strict=True),
                projects,
                associations,
                generated_at=FIXED_TIME,
            )
            second = scan.build_project_skill_snapshot(
                materialized.root.resolve(strict=True),
                projects,
                associations,
                generated_at=FIXED_TIME,
            )
        self.assertEqual(first, second)
        serialized = json.dumps(first, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("/" + "Users/", serialized)
        self.assertNotIn("file://", serialized)
        self.assertNotIn("BODY_SENTINEL", serialized)

    def test_cli_rejects_data_contract_only_before_calling_scan_builder(self) -> None:
        boundary = json.loads(BOUNDARY_PATH.read_text(encoding="utf-8"))
        boundary["phase"] = "data_contract_only"
        boundary["data_contract"]["connections"]["preview"] = False
        boundary["data_contract"]["connections"]["scanner"] = False
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(scan, "_load_fixed_json", return_value=boundary), mock.patch.object(
            scan,
            "build_project_skill_snapshot",
            return_value={"schema_version": 1, "scan_status": "complete"},
        ) as builder, redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(scan._main(["--preview"]), 2)
        builder.assert_not_called()
        self._assert_safe_cli_failure(
            stdout,
            stderr,
            expected_code="boundary_contract_mismatch",
            forbidden=str(scan.PRODUCTION_ROOT),
        )

    def test_runtime_boundary_rejects_any_unreviewed_contract_drift(self) -> None:
        mutations = (
            lambda row: row.update({"unexpected": True}),
            lambda row: row["metadata_projection"].update({"read_scripts": True}),
            lambda row: row["symlink_policy"].update({"outside_project": "follow"}),
            lambda row: row["failure_policy"].update({"unsafe_input": "continue"}),
            lambda row: row["persistence"].update({"external_sync": True}),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate.__code__.co_firstlineno):
                boundary = json.loads(BOUNDARY_PATH.read_text(encoding="utf-8"))
                mutate(boundary)
                with self.assertRaises(scan.ScanRejected):
                    scan.validate_project_skill_runtime_boundary(
                        boundary, required_connection="api"
                    )

    def test_cli_runs_only_for_exact_approved_nonpersistent_preview_contract(self) -> None:
        payload, _ = self._scan_materialized("empty-entry")
        stdout = io.StringIO()
        with mock.patch.object(
            scan, "_load_fixed_json", return_value=_approved_preview_boundary()
        ), mock.patch.object(
            scan, "build_project_skill_snapshot", return_value=payload
        ) as builder, redirect_stdout(stdout):
            self.assertEqual(scan._main(["--preview"]), 0)
        builder.assert_called_once_with()
        self.assertEqual(json.loads(stdout.getvalue()), payload)

    def test_cli_runtime_validation_rejects_incomplete_payload_and_oserror_emits_one_safe_json(self) -> None:
        invalid = {
            "schema_version": 1,
            "scan_status": "complete",
            "unexpected": "/private/synthetic/SHOULD_NOT_EMIT",
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            scan, "_load_fixed_json", return_value=_approved_preview_boundary()
        ), mock.patch.object(
            scan, "build_project_skill_snapshot", return_value=invalid
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(scan._main(["--preview"]), 2)
        self._assert_safe_cli_failure(
            stdout,
            stderr,
            expected_code="validation_failed",
            forbidden="SHOULD_NOT_EMIT",
        )

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            scan, "_load_fixed_json", return_value=_approved_preview_boundary()
        ), mock.patch.object(
            scan,
            "build_project_skill_snapshot",
            side_effect=OSError(errno.EIO, "/private/synthetic/OS_ERROR_MUST_NOT_EMIT"),
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(scan._main(["--preview"]), 2)
        self._assert_safe_cli_failure(
            stdout,
            stderr,
            expected_code="validation_failed",
            forbidden="OS_ERROR_MUST_NOT_EMIT",
        )

    def test_plugin_bundle_skills_and_skill_open_failures_are_not_silently_swallowed(self) -> None:
        definition = {
            "id": "plugin-layer-faults",
            "project": {
                "project_id": "plugin-layer-faults",
                "relative_path": "plugin-layer-faults",
                "classification": "project",
            },
            "nodes": [
                {
                    "type": "file",
                    "path": "plugin-layer-faults/.agents/plugins/bundle/skills/plugin-skill/SKILL.md",
                    "content_utf8": "---\nname: plugin-skill\ndescription: Plugin fault fixture.\n---\n",
                }
            ],
        }
        layers = {
            "bundle": ("plugin-layer-faults/.agents/plugins", "bundle"),
            "skills": ("plugin-layer-faults/.agents/plugins/bundle", "skills"),
            "skill": (
                "plugin-layer-faults/.agents/plugins/bundle/skills",
                "plugin-skill",
            ),
        }
        for layer, (parent_relative, target_name) in layers.items():
            for code in ("permission_denied", "path_race"):
                with self.subTest(layer=layer, code=code), tempfile.TemporaryDirectory(
                    prefix="project-skill-scan-"
                ) as temporary:
                    result = materialize_scenario(definition, temporary)
                    projects, associations = _registries(_scenario_projects(definition))
                    parent_stat = (result.root / parent_relative).stat()
                    parent_identity = (parent_stat.st_dev, parent_stat.st_ino)

                    def is_target(path: Any, kwargs: Mapping[str, Any]) -> bool:
                        parent_fd = kwargs.get("dir_fd")
                        if path != target_name or not isinstance(parent_fd, int):
                            return False
                        info = _REAL_OS_FSTAT(parent_fd)
                        return (info.st_dev, info.st_ino) == parent_identity

                    def faulted_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
                        if code == "permission_denied" and is_target(path, kwargs):
                            raise PermissionError(errno.EACCES, "plugin permission fixture")
                        return _REAL_OS_STAT(path, *args, **kwargs)

                    def faulted_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
                        if code == "path_race" and is_target(path, kwargs):
                            raise FileNotFoundError(errno.ENOENT, "plugin race fixture")
                        return _REAL_OS_OPEN(path, flags, *args, **kwargs)

                    scan._require_descriptor_safety()
                    with mock.patch.object(
                        scan, "_require_descriptor_safety", return_value=None
                    ), mock.patch.object(
                        scan.os, "stat", side_effect=faulted_stat
                    ), mock.patch.object(
                        scan.os, "open", side_effect=faulted_open
                    ):
                        payload = scan.build_project_skill_snapshot(
                            result.root.resolve(strict=True),
                            projects,
                            associations,
                            generated_at=FIXED_TIME,
                        )
                    self.assertEqual(payload["scan_status"], "partial")
                    self.assertIn(code, {row["code"] for row in payload["issues"]})
                    self.assertEqual(_flatten_observations(payload), [])

    def test_backslash_candidate_is_rejected_without_schema_invalid_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="project-skill-scan-") as temporary:
            root = Path(temporary).resolve()
            (root / "registered").mkdir()
            (root / "safe-candidate").mkdir()
            (root / "bad\\candidate").mkdir()
            projects, associations = _registries(
                [
                    {
                        "project_id": "registered",
                        "relative_path": "registered",
                        "classification": "project",
                    }
                ]
            )
            payload = scan.build_project_skill_snapshot(
                root, projects, associations, generated_at=FIXED_TIME
            )
        self.assertEqual([row["relative_path"] for row in payload["candidates"]], ["safe-candidate"])
        self.assertNotIn("bad\\candidate", json.dumps(payload, ensure_ascii=False))

    def test_registry_domain_failures_reject_before_observation_root_open(self) -> None:
        base_projects, base_associations = _registries(
            [{"project_id": "project-a", "relative_path": "project-a", "classification": "project"}]
        )
        valid_association = {
            "association_id": "association-a",
            "project_id": "project-a",
            "relationship": "human_association",
            "asset_id": "skill:a",
            "reason": "Fixture association.",
            "source": "synthetic-confirmation",
        }
        cases: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

        projects = json.loads(json.dumps(base_projects))
        projects["unexpected"] = "https://private.invalid/TOP_EXTRA"
        cases["project-top-extra"] = (projects, base_associations)
        associations = json.loads(json.dumps(base_associations))
        associations["unexpected"] = "https://private.invalid/ASSOCIATION_EXTRA"
        cases["association-top-extra"] = (base_projects, associations)
        projects = json.loads(json.dumps(base_projects))
        projects["confirmed_on"] = "2026-99-99"
        cases["project-invalid-date"] = (projects, base_associations)
        associations = json.loads(json.dumps(base_associations))
        associations["confirmed_on"] = "not-a-date"
        cases["association-invalid-date"] = (base_projects, associations)
        associations = json.loads(json.dumps(base_associations))
        associations["associations"] = [valid_association, dict(valid_association)]
        cases["duplicate-association-id"] = (base_projects, associations)
        projects, associations = _registries(
            [
                {
                    "project_id": "orphan",
                    "relative_path": "container/orphan",
                    "classification": "project",
                    "parent_container_id": "missing-container",
                }
            ]
        )
        cases["missing-parent"] = (projects, associations)
        projects, associations = _registries(
            [
                {"project_id": "parent", "relative_path": "parent", "classification": "project"},
                {
                    "project_id": "child",
                    "relative_path": "parent/child",
                    "classification": "project",
                    "parent_container_id": "parent",
                },
            ]
        )
        cases["parent-not-container"] = (projects, associations)
        projects, associations = _registries(
            [
                {"project_id": "container", "relative_path": "container", "classification": "container"},
                {
                    "project_id": "child",
                    "relative_path": "outside/child",
                    "classification": "project",
                    "parent_container_id": "container",
                },
            ]
        )
        cases["child-outside-container"] = (projects, associations)
        projects, associations = _registries(
            [
                {"project_id": "ancestor", "relative_path": "ancestor", "classification": "project"},
                {"project_id": "nested", "relative_path": "ancestor/nested", "classification": "project"},
            ]
        )
        cases["ambiguous-ancestor"] = (projects, associations)
        projects, associations = _registries(
            [
                {
                    "project_id": "excluded-segment",
                    "relative_path": "project/node_modules/nested",
                    "classification": "project",
                }
            ]
        )
        cases["excluded-path-segment"] = (projects, associations)
        associations = json.loads(json.dumps(base_associations))
        associations["associations"] = [{**valid_association, "unresolved_name": "also-present"}]
        cases["association-xor-both"] = (base_projects, associations)
        associations = json.loads(json.dumps(base_associations))
        missing_xor = dict(valid_association)
        missing_xor.pop("asset_id")
        associations["associations"] = [missing_xor]
        cases["association-xor-neither"] = (base_projects, associations)

        for label, (projects, associations) in cases.items():
            with self.subTest(case=label), mock.patch.object(
                scan, "_open_absolute_directory"
            ) as root_open:
                with self.assertRaises(scan.ScanRejected) as rejected:
                    scan.build_project_skill_snapshot(
                        Path("/definitely/not/opened"),
                        projects,
                        associations,
                        generated_at=FIXED_TIME,
                    )
                root_open.assert_not_called()
                self.assertNotIn("/" + "Users/", str(rejected.exception))
                self.assertNotIn("TOP_EXTRA", str(rejected.exception))
                self.assertNotIn("ASSOCIATION_EXTRA", str(rejected.exception))

    def test_linked_entry_manifest_swap_after_read_fails_path_race_without_observation(self) -> None:
        definition = {
            "id": "linked-post-read-race",
            "project": {
                "project_id": "linked-race",
                "relative_path": "linked-race",
                "classification": "project",
            },
            "nodes": [
                {
                    "type": "file",
                    "path": "linked-race/.agents/shared/canonical/SKILL.md",
                    "content_utf8": "---\nname: linked\ndescription: Before race.\n---\n",
                },
                {
                    "type": "file",
                    "path": "outside/escape/SKILL.md",
                    "content_utf8": "---\nname: outside\ndescription: Outside.\n---\nOUTSIDE_POST_READ_SENTINEL",
                },
            ],
            "dynamic_symlinks": [
                {
                    "path": "linked-race/.agents/skills/linked",
                    "target": "../shared/canonical",
                }
            ],
        }
        with tempfile.TemporaryDirectory(prefix="project-skill-scan-") as temporary:
            result = materialize_scenario(definition, temporary)
            projects, associations = _registries(_scenario_projects(definition))
            open_count = 0

            def racing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
                nonlocal open_count
                if path == "SKILL.md":
                    open_count += 1
                    if open_count == 2:
                        parent_fd = kwargs["dir_fd"]
                        _REAL_OS_UNLINK(path, dir_fd=parent_fd)
                        _REAL_OS_SYMLINK(
                            "../../../../outside/escape/SKILL.md",
                            path,
                            dir_fd=parent_fd,
                        )
                return _REAL_OS_OPEN(path, flags, *args, **kwargs)

            scan._require_descriptor_safety()
            with mock.patch.object(scan, "_require_descriptor_safety", return_value=None), mock.patch.object(
                scan.os, "open", side_effect=racing_open
            ):
                payload = scan.build_project_skill_snapshot(
                    result.root.resolve(strict=True),
                    projects,
                    associations,
                    generated_at=FIXED_TIME,
                )
        self.assertGreaterEqual(open_count, 2)
        self.assertEqual(payload["scan_status"], "partial")
        self.assertIn("path_race", {row["code"] for row in payload["issues"]})
        self.assertEqual(_flatten_observations(payload), [])
        self.assertNotIn("OUTSIDE_POST_READ_SENTINEL", json.dumps(payload))

    def test_manifest_read_eio_is_sanitized_and_fails_closed(self) -> None:
        definition = {
            "id": "manifest-eio",
            "project": {"project_id": "manifest-eio", "relative_path": "manifest-eio", "classification": "project"},
            "nodes": [
                {
                    "type": "file",
                    "path": "manifest-eio/.agents/skills/eio/SKILL.md",
                    "content_utf8": "---\nname: eio\ndescription: EIO fixture.\n---\n",
                }
            ],
        }
        with tempfile.TemporaryDirectory(prefix="project-skill-scan-") as temporary:
            result = materialize_scenario(definition, temporary)
            inode = (result.root / "manifest-eio/.agents/skills/eio/SKILL.md").stat().st_ino
            projects, associations = _registries(_scenario_projects(definition))

            def failing_read(fd: int, amount: int) -> bytes:
                if _REAL_OS_FSTAT(fd).st_ino == inode:
                    raise OSError(errno.EIO, "/private/synthetic/EIO_READ_SECRET")
                return _REAL_OS_READ(fd, amount)

            with mock.patch.object(scan.os, "read", side_effect=failing_read):
                payload = scan.build_project_skill_snapshot(
                    result.root.resolve(strict=True),
                    projects,
                    associations,
                    generated_at=FIXED_TIME,
                )
        self.assertEqual(payload["scan_status"], "partial")
        self.assertEqual(_flatten_observations(payload), [])
        self.assertTrue(payload["issues"])
        self.assertNotIn("EIO_READ_SECRET", json.dumps(payload))

    def test_openai_nested_allow_field_is_not_misread_as_top_level_policy_declaration(self) -> None:
        definition = {
            "id": "nested-openai",
            "project": {"project_id": "nested-openai", "relative_path": "nested-openai", "classification": "project"},
            "nodes": [
                {
                    "type": "file",
                    "path": "nested-openai/.agents/skills/nested/SKILL.md",
                    "content_utf8": "---\nname: nested\ndescription: Nested declaration fixture.\n---\n",
                },
                {
                    "type": "file",
                    "path": "nested-openai/.agents/skills/nested/agents/openai.yaml",
                    "content_utf8": "policy:\n  nested:\n    allow_implicit_invocation: true\n",
                },
            ],
        }
        payload, _ = self._scan_definition(definition)
        observation = _flatten_observations(payload)[0]
        self.assertIn(observation["openai_declaration"]["status"], {"not_observed", "unverified"})
        self.assertNotIn("allow_implicit_invocation", observation["openai_declaration"])
        self.assertIn(
            observation["evidence"]["invocation_eligibility"]["status"],
            {"not_observed", "unverified"},
        )

    def test_manifest_fstat_eio_closes_descriptor_and_does_not_leak_error_text(self) -> None:
        definition = {
            "id": "manifest-fstat-eio",
            "project": {
                "project_id": "manifest-fstat-eio",
                "relative_path": "manifest-fstat-eio",
                "classification": "project",
            },
            "nodes": [
                {
                    "type": "file",
                    "path": "manifest-fstat-eio/.agents/skills/eio/SKILL.md",
                    "content_utf8": "---\nname: fstat-eio\ndescription: Fstat fixture.\n---\n",
                }
            ],
        }
        manifest_fd: int | None = None
        injected = False
        with tempfile.TemporaryDirectory(prefix="project-skill-scan-") as temporary:
            result = materialize_scenario(definition, temporary)
            projects, associations = _registries(_scenario_projects(definition))

            def tracking_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
                nonlocal manifest_fd
                descriptor = _REAL_OS_OPEN(path, flags, *args, **kwargs)
                if path == "SKILL.md" and manifest_fd is None:
                    manifest_fd = descriptor
                return descriptor

            def failing_fstat(fd: int) -> os.stat_result:
                nonlocal injected
                if manifest_fd is not None and fd == manifest_fd and not injected:
                    injected = True
                    raise OSError(errno.EIO, "/private/synthetic/FSTAT_EIO_SECRET")
                return _REAL_OS_FSTAT(fd)

            scan._require_descriptor_safety()
            with mock.patch.object(scan, "_require_descriptor_safety", return_value=None), mock.patch.object(
                scan.os, "open", side_effect=tracking_open
            ), mock.patch.object(scan.os, "fstat", side_effect=failing_fstat):
                payload = scan.build_project_skill_snapshot(
                    result.root.resolve(strict=True),
                    projects,
                    associations,
                    generated_at=FIXED_TIME,
                )
            self.assertTrue(injected)
            self.assertIsNotNone(manifest_fd)
            with self.assertRaises(OSError) as closed:
                _REAL_OS_FSTAT(manifest_fd)  # type: ignore[arg-type]
            self.assertEqual(closed.exception.errno, errno.EBADF)
        self.assertEqual(payload["scan_status"], "partial")
        self.assertEqual(_flatten_observations(payload), [])
        self.assertNotIn("FSTAT_EIO_SECRET", json.dumps(payload))

    def test_frontmatter_exact_maximum_boundary_does_not_read_one_body_byte(self) -> None:
        maximum = int(scan.LIMITS["max_frontmatter_bytes"])
        inside = "#" + ("x" * (maximum - 10)) + "\n"
        manifest = "---\n" + inside + "---\nBODY_AT_LIMIT_MUST_NOT_BE_READ"
        self.assertEqual(len(("---\n" + inside + "---\n").encode("utf-8")), maximum)
        definition = {
            "id": "frontmatter-max-boundary",
            "project": {
                "project_id": "frontmatter-max",
                "relative_path": "frontmatter-max",
                "classification": "project",
            },
            "nodes": [
                {
                    "type": "file",
                    "path": "frontmatter-max/.agents/skills/boundary/SKILL.md",
                    "content_utf8": manifest,
                }
            ],
        }
        with tempfile.TemporaryDirectory(prefix="project-skill-scan-") as temporary:
            result = materialize_scenario(definition, temporary)
            manifest_path = result.root / "frontmatter-max/.agents/skills/boundary/SKILL.md"
            manifest_inode = manifest_path.stat().st_ino
            bytes_read = 0

            def counted_read(fd: int, amount: int) -> bytes:
                nonlocal bytes_read
                piece = _REAL_OS_READ(fd, amount)
                if _REAL_OS_FSTAT(fd).st_ino == manifest_inode:
                    bytes_read += len(piece)
                return piece

            projects, associations = _registries(_scenario_projects(definition))
            with mock.patch.object(scan.os, "read", side_effect=counted_read):
                payload = scan.build_project_skill_snapshot(
                    result.root.resolve(strict=True),
                    projects,
                    associations,
                    generated_at=FIXED_TIME,
                )
        observation = _flatten_observations(payload)[0]
        self.assertEqual(bytes_read, maximum)
        self.assertEqual(observation["frontmatter_bytes_read"], maximum)
        self.assertNotIn("BODY_AT_LIMIT_MUST_NOT_BE_READ", json.dumps(payload))

    def test_issue_cap_preserves_partial_after_additional_failures(self) -> None:
        with tempfile.TemporaryDirectory(prefix="project-skill-scan-") as temporary:
            root = Path(temporary).resolve()
            entry = root / "issue-cap/.agents/skills"
            entry.mkdir(parents=True)
            for index in range(1000):
                os.symlink("missing-target", entry / f"broken-{index:04d}")
            projects, associations = _registries(
                [{"project_id": "issue-cap", "relative_path": "issue-cap", "classification": "project"}]
            )
            payload = scan.build_project_skill_snapshot(
                root, projects, associations, generated_at=FIXED_TIME
            )
        self.assertEqual(payload["scan_status"], "partial")
        self.assertEqual(_project(payload, "issue-cap")["scan_status"], "partial")
        self.assertLessEqual(len(payload["issues"]), 1000)
        self.assertIn("issue_budget_exceeded", {row["code"] for row in payload["issues"]})
        self.assertNotIn("changes", payload)

    def test_fixture_materializer_rejects_initial_and_mid_materialization_root_symlink_without_outside_write(self) -> None:
        definition = {
            "id": "materializer-root-swap",
            "project": {
                "project_id": "materializer-root-swap",
                "relative_path": "project",
                "classification": "project",
            },
            "nodes": [
                {
                    "type": "file",
                    "path": "project/.agents/skills/safe/SKILL.md",
                    "content_utf8": "---\nname: safe\ndescription: Materializer root fixture.\n---\n",
                }
            ],
        }
        with tempfile.TemporaryDirectory(prefix="project-skill-materializer-") as temporary:
            base = Path(temporary).resolve()
            outside = base / "outside"
            outside.mkdir()
            linked_root = base / "linked-root"
            os.symlink("outside", linked_root)
            with self.assertRaises(FixtureMaterializationError):
                materialize_scenario(definition, linked_root)
            self.assertEqual(list(outside.iterdir()), [])

            root = base / "root"
            root.mkdir()
            parked = base / "parked-root"
            original_create = materializer._create_node
            swapped = False

            def swap_then_create(*args: Any, **kwargs: Any) -> None:
                nonlocal swapped
                if not swapped:
                    swapped = True
                    os.rename(root, parked)
                    os.symlink("outside", root)
                return original_create(*args, **kwargs)

            try:
                with mock.patch.object(materializer, "_create_node", side_effect=swap_then_create):
                    with self.assertRaises(FixtureMaterializationError):
                        materialize_scenario(definition, root)
                self.assertEqual(list(outside.iterdir()), [])
            finally:
                if root.is_symlink():
                    root.unlink()
                if parked.exists():
                    parked.rename(root)

    def test_cli_requires_only_preview_and_rejects_root_or_output_before_scan(self) -> None:
        for argv in ([], ["--root", "/tmp"], ["--output", "/tmp/out.json"], ["--preview", "--root", "/tmp"]):
            with self.subTest(argv=argv):
                stderr = io.StringIO()
                with mock.patch.object(scan, "build_project_skill_snapshot") as build, redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        scan._main(argv)
                self.assertEqual(raised.exception.code, 2)
                build.assert_not_called()

    def test_cli_complete_returns_zero_noncomplete_returns_two_and_stdout_is_deterministic(self) -> None:
        boundary = _approved_preview_boundary()
        complete, _ = self._scan_materialized("empty-entry")
        outputs: list[str] = []
        for _ in range(2):
            stdout = io.StringIO()
            with mock.patch.object(scan, "_load_fixed_json", return_value=boundary), mock.patch.object(
                scan, "build_project_skill_snapshot", return_value=complete
            ), redirect_stdout(stdout):
                self.assertEqual(scan._main(["--preview"]), 0)
            outputs.append(stdout.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(json.loads(outputs[0]), complete)
        self.assertNotIn("/" + "Users/", outputs[0])

        noncomplete, _ = self._scan_materialized("malicious-frontmatter")
        stdout = io.StringIO()
        with mock.patch.object(scan, "_load_fixed_json", return_value=boundary), mock.patch.object(
            scan, "build_project_skill_snapshot", return_value=noncomplete
        ), redirect_stdout(stdout):
            self.assertEqual(scan._main(["--preview"]), 2)


if __name__ == "__main__":
    unittest.main()
