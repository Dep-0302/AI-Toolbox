from __future__ import annotations

import json
import re
import unittest
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_DIR = ROOT / "registry"
SCHEMA_DIR = ROOT / "schemas"
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "project-skills"

BOUNDARY_PATH = REGISTRY_DIR / "project_skill_observation_boundary.json"
PROJECTS_PATH = REGISTRY_DIR / "project_skill_projects.json"
ASSOCIATIONS_PATH = REGISTRY_DIR / "project_skill_associations.json"
PROJECTS_SCHEMA_PATH = SCHEMA_DIR / "project_skill_projects.schema.json"
ASSOCIATIONS_SCHEMA_PATH = SCHEMA_DIR / "project_skill_associations.schema.json"
SNAPSHOT_SCHEMA_PATH = SCHEMA_DIR / "project_skill_snapshot.schema.json"
ROOT_SCHEMA_PATH = SCHEMA_DIR / "project_skill_root.schema.json"
SAVED_PROJECTS_SCHEMA_PATH = SCHEMA_DIR / "project_skill_saved_projects.schema.json"
CHINESE_METADATA_PATH = REGISTRY_DIR / "chinese_metadata.json"
CHINESE_METADATA_SCHEMA_PATH = SCHEMA_DIR / "chinese_metadata.schema.json"
TOOL_ASSET_SCHEMA_PATH = SCHEMA_DIR / "tool_asset.schema.json"

CLASSIFICATIONS = {"project", "container", "archive", "excluded", "unclassified"}
APPROVED_PROJECTION = {
    "name",
    "description",
    "version",
    "author",
    "license",
    "agent_created",
}


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"{path} must contain a JSON object")
    return value


def is_safe_relative_path(value: str) -> bool:
    """Domain check used by the Registry, not a JSON Schema implementation."""

    if not value or value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
        return False
    if "\\" in value or "\x00" in value or "//" in value:
        return False
    parts = value.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def project_registry_domain_errors(projects: list[dict[str, Any]]) -> list[str]:
    """Freeze cross-record rules that JSON Schema alone cannot express."""

    errors: list[str] = []
    ids: dict[str, dict[str, Any]] = {}
    paths: dict[str, dict[str, Any]] = {}

    for project in projects:
        project_id = project.get("project_id")
        relative_path = project.get("relative_path")
        if not isinstance(project_id, str):
            errors.append("missing project_id")
            continue
        if project_id in ids:
            errors.append(f"duplicate project_id:{project_id}")
        ids[project_id] = project

        if not isinstance(relative_path, str) or not is_safe_relative_path(relative_path):
            errors.append(f"unsafe relative_path:{relative_path}")
            continue
        if relative_path in paths:
            errors.append(f"duplicate relative_path:{relative_path}")
        paths[relative_path] = project

    for project in projects:
        parent_id = project.get("parent_container_id")
        if parent_id is None:
            continue
        parent = ids.get(parent_id)
        if parent is None:
            errors.append(f"missing parent:{parent_id}")
            continue
        if parent.get("classification") != "container":
            errors.append(f"parent is not container:{parent_id}")
        if project.get("classification") != "project":
            errors.append(f"child is not project:{project.get('project_id')}")
        parent_path = parent.get("relative_path")
        child_path = project.get("relative_path")
        if isinstance(parent_path, str) and isinstance(child_path, str):
            if not child_path.startswith(parent_path + "/"):
                errors.append(f"child outside parent:{project.get('project_id')}")

    safe_projects = [
        project
        for project in projects
        if isinstance(project.get("relative_path"), str)
        and is_safe_relative_path(project["relative_path"])
    ]
    for ancestor in safe_projects:
        ancestor_path = ancestor["relative_path"]
        for child in safe_projects:
            child_path = child["relative_path"]
            if ancestor is child or not child_path.startswith(ancestor_path + "/"):
                continue
            explicitly_parented = (
                ancestor.get("classification") == "container"
                and child.get("classification") == "project"
                and child.get("parent_container_id") == ancestor.get("project_id")
            )
            if not explicitly_parented:
                errors.append(
                    "ambiguous ancestor:"
                    f"{ancestor.get('project_id')}->{child.get('project_id')}"
                )
    return errors


def iter_json_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from iter_json_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_json_strings(item)


def collect_key_values(value: Any, key: str):
    if isinstance(value, list):
        for item in value:
            yield from collect_key_values(item, key)
    elif isinstance(value, dict):
        for item_key, item_value in value.items():
            if item_key == key:
                yield item_value
            yield from collect_key_values(item_value, key)


class ProjectSkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.boundary = load_json(BOUNDARY_PATH)
        cls.projects = load_json(PROJECTS_PATH)
        cls.associations = load_json(ASSOCIATIONS_PATH)
        cls.projects_schema = load_json(PROJECTS_SCHEMA_PATH)
        cls.associations_schema = load_json(ASSOCIATIONS_SCHEMA_PATH)
        cls.snapshot_schema = load_json(SNAPSHOT_SCHEMA_PATH)
        cls.saved_projects_schema = load_json(SAVED_PROJECTS_SCHEMA_PATH)
        cls.chinese_metadata = load_json(CHINESE_METADATA_PATH)
        cls.chinese_metadata_schema = load_json(CHINESE_METADATA_SCHEMA_PATH)
        cls.tool_asset_schema = load_json(TOOL_ASSET_SCHEMA_PATH)
        cls.fixture_index = load_json(FIXTURE_ROOT / "index.json")
        cls.fixture_expected = load_json(FIXTURE_ROOT / "expected.json")
        cls.scenarios = {
            item["id"]: load_json(FIXTURE_ROOT / item["spec"])
            for item in cls.fixture_index["scenarios"]
        }

    def test_all_contract_json_is_parseable(self) -> None:
        paths = [
            BOUNDARY_PATH,
            PROJECTS_PATH,
            ASSOCIATIONS_PATH,
            PROJECTS_SCHEMA_PATH,
            ASSOCIATIONS_SCHEMA_PATH,
            SNAPSHOT_SCHEMA_PATH,
            ROOT_SCHEMA_PATH,
            SAVED_PROJECTS_SCHEMA_PATH,
            CHINESE_METADATA_PATH,
            CHINESE_METADATA_SCHEMA_PATH,
            *sorted(FIXTURE_ROOT.rglob("*.json")),
        ]
        self.assertEqual(len(paths), len(set(paths)))
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict)

    def test_saved_projects_schema_is_local_strict_and_reuses_snapshot_contract(self) -> None:
        schema = self.saved_projects_schema
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), {"schema_version", "projects"})
        project = schema["properties"]["projects"]["items"]
        self.assertFalse(project["additionalProperties"])
        self.assertEqual(project["properties"]["snapshot"]["$ref"], "project_skill_snapshot.schema.json")
        self.assertEqual(project["properties"]["path"]["maxLength"], 4096)
        self.assertEqual(schema["properties"]["projects"]["maxItems"], 64)

    def test_registry_root_and_record_fields_match_strict_schemas(self) -> None:
        pairs = [
            (self.projects, self.projects_schema, "projects", "project"),
            (
                self.associations,
                self.associations_schema,
                "associations",
                "human_association",
            ),
        ]
        for registry, schema, collection_key, definition_key in pairs:
            with self.subTest(registry=registry["registry_id"]):
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(registry), set(schema["required"]))
                self.assertEqual(set(registry), set(schema["properties"]))
                self.assertEqual(registry["schema_version"], 1)
                self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
                self.assertEqual(
                    registry["observation_boundary_ref"],
                    self.boundary["contract_id"],
                )
                self.assertRegex(registry["confirmed_on"], r"^\d{4}-\d{2}-\d{2}$")
                definition = schema["$defs"][definition_key]
                self.assertFalse(definition["additionalProperties"])
                for record in registry[collection_key]:
                    self.assertTrue(set(definition["required"]).issubset(record))
                    self.assertTrue(set(record).issubset(definition["properties"]))

                stable_id = schema["$defs"]["stable_id"]
                self.assertEqual(stable_id["pattern"], "^[a-z0-9][a-z0-9._:-]*$")
                for key in ("registry_id", "observation_boundary_ref"):
                    self.assertRegex(registry[key], re.compile(stable_id["pattern"]))

        project_definition = self.projects_schema["$defs"]["project"]
        self.assertEqual(
            set(project_definition["properties"]["classification"]["enum"]),
            CLASSIFICATIONS,
        )
        for project in self.projects["projects"]:
            self.assertRegex(
                project["project_id"],
                re.compile(self.projects_schema["$defs"]["stable_id"]["pattern"]),
            )
            if "parent_container_id" in project:
                self.assertRegex(
                    project["parent_container_id"],
                    re.compile(
                        self.projects_schema["$defs"]["stable_id"]["pattern"]
                    ),
                )
            self.assertTrue(is_safe_relative_path(project["relative_path"]))
            self.assertIn(project["classification"], CLASSIFICATIONS)

    def test_public_project_registry_is_an_empty_template(self) -> None:
        self.assertEqual(self.projects["projects"], [])
        self.assertEqual(project_registry_domain_errors(self.projects["projects"]), [])

    def test_project_registry_domain_helper_rejects_unsafe_or_ambiguous_records(self) -> None:
        base = {
            "project_id": "project-safe",
            "relative_path": "safe-project",
            "classification": "project",
            "display_name": "safe-project",
        }
        cases = {
            "absolute": [{**base, "relative_path": "/tmp/project"}],
            "parent_traversal": [{**base, "relative_path": "safe/../project"}],
            "duplicate_id": [base, {**base, "relative_path": "other"}],
            "duplicate_path": [base, {**base, "project_id": "project-other"}],
            "ambiguous_ancestor": [
                base,
                {
                    **base,
                    "project_id": "project-nested",
                    "relative_path": "safe-project/nested",
                },
            ],
        }
        for label, records in cases.items():
            with self.subTest(label=label):
                self.assertTrue(project_registry_domain_errors(records))

    def test_only_project_classification_can_have_scan_results(self) -> None:
        self.assertEqual(
            self.boundary["project_registry"]["scannable_classifications"],
            ["project"],
        )
        project_schema = self.snapshot_schema["$defs"]["project_observation"]
        conditional = project_schema["allOf"][0]
        self.assertEqual(
            conditional["if"]["properties"]["classification"]["const"], "project"
        )
        self.assertEqual(
            set(conditional["then"]["properties"]["scan_status"]["enum"]),
            {"complete", "partial", "error", "security_reject"},
        )
        self.assertEqual(
            conditional["else"]["properties"]["scan_status"]["const"],
            "not_scanned",
        )
        self.assertEqual(conditional["else"]["properties"]["entries"]["maxItems"], 0)
        self.assertEqual(
            conditional["else"]["properties"]["logical_skills"]["maxItems"], 0
        )

    def test_human_association_is_xor_stable_and_machine_independent(self) -> None:
        definition = self.associations_schema["$defs"]["human_association"]
        self.assertEqual(len(definition["oneOf"]), 2)
        alternatives = {
            (
                tuple(branch["required"]),
                tuple(branch["not"]["required"]),
            )
            for branch in definition["oneOf"]
        }
        self.assertEqual(
            alternatives,
            {
                (("asset_id",), ("unresolved_name",)),
                (("unresolved_name",), ("asset_id",)),
            },
        )
        forbidden_machine_fields = {
            "file_discovery",
            "project_binding",
            "host_availability",
            "invocation_eligibility",
            "actual_use",
            "observations",
        }
        self.assertTrue(forbidden_machine_fields.isdisjoint(definition["properties"]))

        self.assertEqual(self.associations["associations"], [])

    def test_snapshot_contract_is_independent_from_tool_asset(self) -> None:
        self.assertNotEqual(self.snapshot_schema["$id"], self.tool_asset_schema["$id"])
        snapshot_refs = list(collect_key_values(self.snapshot_schema, "$ref"))
        self.assertTrue(all(ref.startswith("#/") for ref in snapshot_refs))
        self.assertNotIn("tool_asset", json.dumps(self.snapshot_schema).lower())
        self.assertIn("human_associations", self.snapshot_schema["required"])
        self.assertNotIn(
            "human_associations",
            self.snapshot_schema["$defs"]["project_observation"]["properties"],
        )

    def test_snapshot_freezes_seven_entry_semantics(self) -> None:
        boundary_entries = {
            entry["relative_path"]: entry for entry in self.boundary["entry_points"]
        }
        schema_entries = {}
        for branch in self.snapshot_schema["$defs"]["entry_semantics"]["oneOf"]:
            constants = {
                key: value["const"]
                if "const" in value
                else None
                for key, value in branch["properties"].items()
            }
            schema_entries[constants["relative_path"]] = constants
            self.assertFalse(branch["additionalProperties"])
            self.assertEqual(set(branch["required"]), set(branch["properties"]))
        self.assertEqual(len(schema_entries), 7)
        self.assertEqual(schema_entries, boundary_entries)

    def test_snapshot_freezes_boundary_budgets_and_six_field_projection(self) -> None:
        limit_properties = self.snapshot_schema["$defs"]["scan_scope"]["properties"][
            "limits"
        ]["properties"]
        schema_limits = {key: value["const"] for key, value in limit_properties.items()}
        self.assertEqual(schema_limits, self.boundary["limits"])

        projection = self.snapshot_schema["$defs"]["frontmatter_projection"]
        self.assertFalse(projection["additionalProperties"])
        self.assertEqual(set(projection["required"]), APPROVED_PROJECTION)
        self.assertEqual(set(projection["properties"]), APPROVED_PROJECTION)
        self.assertEqual(
            set(self.boundary["metadata_projection"]["frontmatter_allowlist"]),
            APPROVED_PROJECTION,
        )
        observation = self.snapshot_schema["$defs"]["skill_observation"]
        self.assertIn("projection_sha256_v1", observation["required"])
        self.assertEqual(
            observation["properties"]["projection_sha256_v1"]["$ref"],
            "#/$defs/sha256",
        )

        localization = self.snapshot_schema["$defs"]["project_skill_localization"]
        self.assertEqual(len(localization["oneOf"]), 3)
        statuses = {
            branch["properties"]["status"].get("const")
            or tuple(branch["properties"]["status"]["enum"])
            for branch in localization["oneOf"]
        }
        self.assertEqual(statuses, {"missing", "stale", ("ai_draft", "reviewed")})
        logical = self.snapshot_schema["$defs"]["logical_skill"]
        self.assertNotIn("localization", logical["required"])
        self.assertEqual(
            logical["properties"]["localization"]["$ref"],
            "#/$defs/project_skill_localization",
        )

        overlay = self.boundary["metadata_projection"]["chinese_metadata_overlay"]
        self.assertEqual(overlay["freshness_basis"], "projection_sha256_v1")
        self.assertEqual(overlay["evidence_semantics"], "display_only_no_evidence_upgrade")
        self.assertIn("translated_from_projection_sha256_v1", overlay["field_allowlist"])
        self.assertNotIn("translated_from_hash", overlay["field_allowlist"])

    def test_localization_contract_uses_projection_fingerprint_without_body_hash(self) -> None:
        item_schema = self.chinese_metadata_schema["properties"]["items"]["items"]
        fingerprint = item_schema["properties"][
            "translated_from_projection_sha256_v1"
        ]
        self.assertEqual(fingerprint["pattern"], "^[a-f0-9]{64}$")
        required_variants = {
            tuple(branch["required"]) for branch in item_schema["anyOf"]
        }
        self.assertIn(
            ("translated_from_projection_sha256_v1",), required_variants
        )

    def test_snapshot_evidence_and_openai_declaration_are_not_upgraded(self) -> None:
        evidence = self.snapshot_schema["$defs"]["five_layer_evidence"]
        self.assertEqual(
            evidence["properties"]["host_availability"]["properties"]["status"][
                "const"
            ],
            "unverified",
        )
        self.assertEqual(
            evidence["properties"]["actual_use"]["properties"]["status"]["const"],
            "not_connected",
        )

        declaration = self.snapshot_schema["$defs"]["openai_declaration"]
        statuses = {
            branch["properties"]["status"]["const"]: branch
            for branch in declaration["oneOf"]
        }
        self.assertEqual(set(statuses), {"declared", "not_observed", "unverified"})
        self.assertEqual(
            set(statuses["declared"]["required"]),
            {"status", "allow_implicit_invocation"},
        )
        self.assertEqual(set(statuses["not_observed"]["required"]), {"status"})
        self.assertEqual(set(statuses["unverified"]["required"]), {"status", "reason"})
        self.assertEqual(
            set(statuses["unverified"]["properties"]["reason"]["enum"]),
            {
                "scan_incomplete",
                "invalid_yaml",
                "over_limit",
                "permission_denied",
                "security_reject",
                "path_race",
            },
        )

    def test_snapshot_changes_require_complete_and_freeze_version_mismatch(self) -> None:
        changes_gate = self.snapshot_schema["allOf"][0]
        self.assertEqual(
            changes_gate["if"]["properties"]["scan_status"]["const"], "complete"
        )
        self.assertEqual(changes_gate["then"]["required"], ["changes"])
        self.assertEqual(changes_gate["else"]["not"]["required"], ["changes"])
        self.assertTrue({"partial", "error"}.issubset(self.snapshot_schema["properties"]["scan_status"]["enum"]))

        branches = self.snapshot_schema["$defs"]["changes"]["oneOf"]
        mismatch = next(
            branch
            for branch in branches
            if branch["properties"]["status"].get("const") == "not_comparable"
        )
        self.assertEqual(
            mismatch["properties"]["reason"]["const"],
            "fingerprint_version_mismatch",
        )
        self.assertEqual(
            mismatch["properties"]["fingerprint_basis"]["const"],
            "projection_sha256_v1",
        )
        self.assertIn("prior_fingerprint_basis", mismatch["required"])

    def test_fixture_inventory_is_exact_and_contains_no_static_symlinks(self) -> None:
        scenario_items = self.fixture_index["scenarios"]
        scenario_ids = [item["id"] for item in scenario_items]
        fixture_files = [path for path in FIXTURE_ROOT.rglob("*") if path.is_file()]
        fixture_symlinks = [path for path in FIXTURE_ROOT.rglob("*") if path.is_symlink()]
        self.assertEqual(len(scenario_ids), 15)
        self.assertEqual(len(scenario_ids), len(set(scenario_ids)))
        self.assertEqual(len(fixture_files), 23)
        self.assertEqual(fixture_symlinks, [])
        self.assertFalse(self.fixture_index["checked_in_symlinks_allowed"])
        self.assertTrue(self.fixture_index["dynamic_build_required"])
        self.assertEqual(set(self.fixture_expected["scenario_summary"]), set(scenario_ids))
        for item in scenario_items:
            with self.subTest(scenario=item["id"]):
                self.assertEqual(self.scenarios[item["id"]]["id"], item["id"])

    def test_fixture_calibration_counts_and_boundaries_are_frozen(self) -> None:
        summary = self.fixture_expected["scenario_summary"]
        self.assertEqual(
            summary["empty-entry"],
            {"scan_status": "complete", "logical_skill_count": 0, "error_count": 0},
        )
        self.assertEqual(
            summary["marketplace-bait"],
            {
                "logical_skill_count": 0,
                "manifest_read_count": 0,
                "marketplace_content_read_count": 0,
                "unapproved_plugins_descent_count": 0,
            },
        )
        self.assertEqual(
            summary["multi-evidence"],
            {
                "logical_skill_count": 10,
                "file_observation_count": 16,
                "binding_evidence_count": 10,
                "plugin_source_evidence_count": 6,
            },
        )
        self.assertEqual(summary["human-only"]["machine_logical_skill_count"], 0)
        self.assertEqual(summary["human-only"]["human_association_count"], 1)
        self.assertEqual(summary["human-only"]["machine_state_upgrades"], 0)
        self.assertEqual(
            summary["codex-path-only"]["binding_semantics"],
            "observed_path_only",
        )
        self.assertEqual(summary["codex-path-only"]["binding_evidence_count"], 0)
        self.assertFalse(summary["container-no-descent"]["project_scan_attempted"])
        self.assertEqual(
            summary["container-no-descent"]["nested_project_inference_count"], 0
        )

        global_invariants = self.fixture_expected["global_invariants"]
        self.assertEqual(global_invariants["checked_in_symlink_count"], 0)
        self.assertEqual(global_invariants["absolute_path_count"], 0)
        self.assertEqual(global_invariants["host_availability"], "unverified")
        self.assertEqual(global_invariants["actual_use"], "not_connected")

    def test_fixtures_cover_classification_entries_and_security_failures(self) -> None:
        classification = self.scenarios["classification-matrix"]
        self.assertEqual(
            classification["expected"]["scannable_classifications"], ["project"]
        )
        self.assertEqual(classification["expected"]["non_project_deep_read_count"], 0)

        entry_semantics = self.scenarios["entry-semantics"]["expected"]
        self.assertEqual(entry_semantics["binding_evidence_count"], 3)
        self.assertEqual(entry_semantics["source_only_evidence_count"], 1)
        self.assertEqual(entry_semantics["host_availability"], "unverified")
        self.assertEqual(entry_semantics["actual_use"], "not_connected")

        malicious = self.scenarios["malicious-frontmatter"]
        self.assertEqual(len(malicious["nodes"]), 5)
        self.assertEqual(len(malicious["expected"]["cases"]), 5)
        self.assertEqual(malicious["expected"]["constructors_executed"], 0)
        self.assertFalse(malicious["expected"]["raw_html_rendered"])
        limits = self.scenarios["metadata-limits"]
        self.assertEqual(limits["contract_limits"], {
            "max_manifest_bytes": 262144,
            "max_frontmatter_bytes": 32768,
            "max_text_length": 4000,
        })
        self.assertEqual(len(limits["expected"]["cases"]), 3)
        self.assertEqual(
            self.scenarios["excluded-boundaries"]["expected"]["excluded_segment_count"],
            len(self.boundary["excluded_boundaries"]),
        )

        path_safety = self.scenarios["path-safety"]
        case_ids = {case["case_id"] for case in path_safety["cases"]}
        self.assertEqual(len(case_ids), 9)
        self.assertEqual(
            set(self.fixture_expected["path_safety_outcomes"]), case_ids
        )
        self.assertEqual(
            {
                outcome
                for outcome in self.fixture_expected["path_safety_outcomes"].values()
            },
            {
                "broken",
                "outside_project",
                "loop",
                "skipped",
                "observation_root_symlink",
                "project_root_symlink",
                "project_ancestor_symlink",
                "path_race",
                "permission_denied",
            },
        )

    def test_fixture_paths_are_synthetic_relative_and_contain_no_credentials(self) -> None:
        fixture_json = [load_json(path) for path in sorted(FIXTURE_ROOT.rglob("*.json"))]
        sensitive = re.compile(
            r"(?:/[U]sers/|/home/|[A-Za-z]:\\|AKIA[0-9A-Z]{12,}|"
            r"\bsk-[A-Za-z0-9]{12,}|api[_ -]?key\s*[:=]|authorization\s*[:=]|"
            r"bearer\s+[A-Za-z0-9._-]{8,}|password\s*[:=])",
            re.IGNORECASE,
        )
        for document in [self.projects, self.associations, *fixture_json]:
            for value in iter_json_strings(document):
                self.assertIsNone(sensitive.search(value), value)

        for scenario in self.scenarios.values():
            for node in scenario.get("nodes", []):
                self.assertTrue(is_safe_relative_path(node["path"]), node["path"])
                if "source" in node:
                    self.assertTrue(is_safe_relative_path(node["source"]), node["source"])
            matrix = scenario.get("node_matrix")
            if matrix:
                self.assertTrue(is_safe_relative_path(matrix["base_path"]))
                self.assertTrue(is_safe_relative_path(matrix["suffix"]))

        corpus = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in sorted(FIXTURE_ROOT.rglob("*"))
            if path.is_file()
        )
        self.assertIsNone(sensitive.search(corpus))
        protected_sentinels = set()
        for scenario in self.scenarios.values():
            protected_sentinels.update(scenario.get("expected", {}).get("must_not_emit", []))
            for case in scenario.get("cases", []):
                protected_sentinels.update(case.get("expected", {}).get("must_not_emit", []))
        self.assertEqual(protected_sentinels, set(self.fixture_index["body_sentinels"]))
        for sentinel in self.fixture_index["body_sentinels"]:
            self.assertIn(sentinel, corpus)


if __name__ == "__main__":
    unittest.main()
