from __future__ import annotations

import json
import unittest
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
BOUNDARY_PATH = ROOT / "registry" / "project_skill_observation_boundary.json"


class ProjectSkillObservationBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.boundary = json.loads(BOUNDARY_PATH.read_text(encoding="utf-8"))

    def test_readonly_workbench_phase_has_only_five_current_writes(self) -> None:
        self.assertEqual(self.boundary["schema_version"], 1)
        self.assertEqual(self.boundary["phase"], "readonly_workbench")
        self.assertEqual(
            self.boundary["persistence"]["current_phase_writes"],
            [
                "generated/project-skills/snapshot.json",
                "generated/project-skills/last-attempt.json",
                "generated/project-skills/项目Skill总览.md",
                "generated/project-skills/local-root.json",
                "generated/project-skills/local-projects.json",
            ],
        )

        data_contract = self.boundary["data_contract"]
        self.assertEqual(
            data_contract["registries"],
            [
                "registry/project_skill_projects.json",
                "registry/project_skill_associations.json",
                "registry/chinese_metadata.json",
            ],
        )
        self.assertEqual(
            data_contract["schemas"],
            [
                "schemas/project_skill_projects.schema.json",
                "schemas/project_skill_associations.schema.json",
                "schemas/project_skill_snapshot.schema.json",
                "schemas/project_skill_root.schema.json",
                "schemas/project_skill_saved_projects.schema.json",
                "schemas/chinese_metadata.schema.json",
            ],
        )
        self.assertEqual(
            data_contract["fixture_root"], "tests/fixtures/project-skills"
        )
        self.assertEqual(
            data_contract["contract_test"], "tests/test_project_skill_contract.py"
        )
        self.assertEqual(
            data_contract["connections"],
            {
                "preview": True,
                "scanner": True,
                "api": True,
                "ui": True,
                "persistence": True,
            },
        )

        for relative_path in (
            *data_contract["registries"],
            *data_contract["schemas"],
            data_contract["fixture_root"],
            data_contract["contract_test"],
        ):
            self.assertTrue((ROOT / relative_path).exists(), relative_path)

    def test_workbench_phase_has_dedicated_store_routes_and_ui(self) -> None:
        self.assertTrue((ROOT / "src" / "project_skill_scan.py").is_file())
        self.assertTrue((ROOT / "tests" / "test_project_skill_scan.py").is_file())
        self.assertTrue(
            (ROOT / "tests" / "project_skill_fixture_materializer.py").is_file()
        )
        self.assertTrue((ROOT / "src" / "project_skill_report.py").is_file())
        self.assertTrue((ROOT / "tests" / "test_project_skill_persistence.py").is_file())
        self.assertTrue((ROOT / "web" / "ProjectSkillsView.jsx").is_file())
        self.assertTrue((ROOT / "web" / "lib" / "projectSkills.js").is_file())

        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn('"/api/project-skills"', app_source)
        self.assertIn('"/api/project-skills/refresh"', app_source)

        web_source = (ROOT / "web" / "App.jsx").read_text(encoding="utf-8")
        self.assertIn("ProjectSkillsView", web_source)
        self.assertIn("/api/project-skills", web_source)

    def test_configured_root_scope_requires_human_project_classification(self) -> None:
        scope = self.boundary["source_scope"]
        registry = self.boundary["project_registry"]

        self.assertEqual(scope["root_mode"], "single_local_config")
        self.assertEqual(
            scope["local_config"], "generated/project-skills/local-root.json"
        )
        self.assertEqual(
            scope["configured_root_selection"],
            {
                "enabled": True,
                "selection": "native_picker_token_only",
                "persistence": "local_only_0600",
                "public_default": "not_configured",
            },
        )
        self.assertEqual(scope["candidate_discovery"], "top_level_directories_only")
        self.assertEqual(scope["nested_projects"], "explicit_registry_only")
        self.assertEqual(scope["new_candidate_status"], "unclassified")
        self.assertEqual(scope["unclassified_policy"], "list_only_no_skill_scan")
        self.assertEqual(
            scope["saved_selected_project"],
            {
                "enabled": True,
                "selection": "native_picker_token_only",
                "must_be_within_root": False,
                "exact_project_only": True,
                "classification": "project",
                "candidate_discovery": False,
                "persistence": "local_only_0600",
                "local_state": "generated/project-skills/local-projects.json",
                "remove_by": "stable_project_id_only",
                "remove_observed_project": False,
                "human_associations": False,
            },
        )
        self.assertEqual(registry["scannable_classifications"], ["project"])
        self.assertFalse(registry["write_back_to_projects"])

    def test_entry_points_are_relative_unique_and_semantically_separated(self) -> None:
        entries = self.boundary["entry_points"]
        by_path = {entry["relative_path"]: entry for entry in entries}

        self.assertEqual(len(by_path), len(entries))
        self.assertEqual(
            set(by_path),
            {
                ".agents/skills",
                ".claude/skills",
                ".codex/skills",
                ".hermes/skills",
                ".workbuddy/skills",
                "skills",
                ".agents/plugins",
            },
        )
        for relative_path in by_path:
            parsed = PurePosixPath(relative_path)
            self.assertFalse(parsed.is_absolute())
            self.assertNotIn("..", parsed.parts)

        self.assertEqual(
            by_path[".codex/skills"]["binding_semantics"],
            "observed_path_only",
        )
        self.assertEqual(by_path["skills"]["binding_semantics"], "source_only")
        self.assertEqual(
            by_path[".agents/plugins"]["binding_semantics"],
            "plugin_bundled_source",
        )

    def test_evidence_never_upgrades_static_observation_to_runtime_truth(self) -> None:
        evidence = self.boundary["evidence_contract"]

        self.assertEqual(evidence["host_availability"], "unverified")
        self.assertEqual(evidence["invocation_eligibility"], "declaration_only")
        self.assertEqual(evidence["actual_use"], "not_connected")
        self.assertEqual(evidence["lifecycle_action"], "locked")
        self.assertTrue(evidence["human_association_is_separate"])

    def test_metadata_projection_excludes_bodies_scripts_and_credentials(self) -> None:
        projection = self.boundary["metadata_projection"]

        self.assertFalse(projection["read_manifest_body"])
        self.assertFalse(projection["read_scripts"])
        self.assertFalse(projection["read_references"])
        self.assertFalse(projection["read_credentials"])
        self.assertFalse(projection["store_absolute_project_paths"])
        self.assertEqual(projection["render_free_text_as"], "escaped_plain_text")
        self.assertEqual(
            projection["optional_skill_metadata"]["field_allowlist"],
            ["policy.allow_implicit_invocation"],
        )
        overlay = projection["chinese_metadata_overlay"]
        self.assertEqual(overlay["relative_path"], "registry/chinese_metadata.json")
        self.assertEqual(overlay["freshness_basis"], "projection_sha256_v1")
        self.assertEqual(overlay["evidence_semantics"], "display_only_no_evidence_upgrade")
        self.assertNotIn("translated_from_hash", overlay["field_allowlist"])

    def test_symlinks_fail_closed_outside_the_same_project(self) -> None:
        policy = self.boundary["symlink_policy"]

        self.assertEqual(policy["source_root_or_ancestor"], "reject")
        self.assertEqual(policy["project_root_or_ancestor"], "reject")
        self.assertEqual(policy["skill_entry"], "follow_same_project_only")
        self.assertEqual(policy["outside_project"], "record_without_reading_target")
        self.assertEqual(policy["descendant_symlinks"], "skip")
        self.assertEqual(policy["path_race"], "fail_closed")
        self.assertTrue(policy["descriptor_anchoring_required"])

    def test_runtime_has_no_execution_network_logs_or_mutation(self) -> None:
        safety = self.boundary["runtime_safety"]

        self.assertTrue(safety["manual_refresh_only"])
        self.assertTrue(safety["foreground_only"])
        for key in (
            "network",
            "process_execution",
            "model_calls",
            "reads_host_config_bodies",
            "reads_sessions_or_logs",
            "background_watchers",
            "host_mutations",
            "project_mutations",
        ):
            self.assertFalse(safety[key], key)

    def test_current_outputs_are_bounded_to_generated_project_skills(self) -> None:
        persistence = self.boundary["persistence"]
        root = PurePosixPath(persistence["future_write_root"])

        self.assertEqual(root, PurePosixPath("generated/project-skills"))
        self.assertEqual(persistence["file_mode"], "0600")
        self.assertTrue(persistence["atomic_writes"])
        self.assertFalse(persistence["write_observed_projects"])
        self.assertFalse(persistence["external_sync"])
        self.assertEqual(
            persistence["current_phase_writes"], persistence["future_allowed_outputs"]
        )
        for output in persistence["future_allowed_outputs"]:
            path = PurePosixPath(output)
            self.assertFalse(path.is_absolute())
            self.assertNotIn("..", path.parts)
            self.assertEqual(path.parts[:2], root.parts)

    def test_partial_scan_cannot_infer_removal(self) -> None:
        failure = self.boundary["failure_policy"]

        self.assertEqual(failure["incomplete_scan_status"], "partial")
        self.assertTrue(failure["retain_last_complete_snapshot"])
        self.assertFalse(failure["absence_after_partial_means_removed"])
        self.assertEqual(failure["unsafe_input"], "fail_closed")

    def test_public_boundary_is_self_contained_and_machine_independent(self) -> None:
        serialized = json.dumps(self.boundary, ensure_ascii=False)

        self.assertEqual(
            self.boundary["contract_id"],
            "project-skill-observation-boundary-v1",
        )
        self.assertEqual(
            self.boundary["source_scope"]["root_mode"], "single_local_config"
        )
        self.assertEqual(
            self.boundary["source_scope"]["configured_root_selection"]["public_default"],
            "not_configured",
        )
        self.assertNotIn("/" + "Users/", serialized)
        self.assertFalse(
            self.boundary["metadata_projection"]["store_absolute_project_paths"]
        )


if __name__ == "__main__":
    unittest.main()
