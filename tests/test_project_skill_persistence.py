from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]

from src import project_skill_report as report  # noqa: E402
from src import project_skill_scan as scan  # noqa: E402


FIXED_TIME = "2000-01-01T12:00:00Z"


def _registries(*, association: bool = True) -> tuple[dict, dict]:
    projects = {
        "schema_version": 1,
        "registry_id": "fixture-projects-v1",
        "observation_boundary_ref": scan.BOUNDARY_ID,
        "confirmed_on": "2000-01-01",
        "projects": [
            {
                "project_id": "project-fixture",
                "relative_path": "fixture-project",
                "classification": "project",
                "display_name": "Fixture Project",
            },
            {
                "project_id": "container-fixture",
                "relative_path": "fixture-container",
                "classification": "container",
                "display_name": "Fixture Container",
            },
        ],
    }
    associations = {
        "schema_version": 1,
        "registry_id": "fixture-associations-v1",
        "observation_boundary_ref": scan.BOUNDARY_ID,
        "confirmed_on": "2000-01-01",
        "associations": [],
    }
    if association:
        associations["associations"].append(
            {
                "association_id": "human-association:fixture:design",
                "project_id": "project-fixture",
                "relationship": "human_association",
                "asset_id": "skill:design-fixture",
                "reason": "Human memory only.",
                "source": "gate_fixture",
            }
        )
    return projects, associations


def _reidentify(payload: dict) -> dict:
    payload = deepcopy(payload)
    payload = _identity_only(payload)
    scan.validate_project_skill_payload(payload)
    return payload


def _identity_only(payload: dict) -> dict:
    payload = deepcopy(payload)
    identity = dict(payload)
    identity.pop("generation_id", None)
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload["generation_id"] = hashlib.sha256(canonical).hexdigest()[:16]
    return payload


class ProjectSkillPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / "fixture-project").mkdir()
        (self.root / "fixture-container").mkdir()
        self.skill_dir = self.root / "fixture-project" / ".agents" / "skills" / "base"
        self.skill_dir.mkdir(parents=True)
        self.write_base_skill("Baseline description")
        self.projects, self.associations = _registries()
        self.complete = scan.build_project_skill_snapshot(
            self.root,
            self.projects,
            self.associations,
            generated_at=FIXED_TIME,
        )
        self.assertEqual(self.complete["scan_status"], "complete")
        self.store = report.ProjectSkillStore(self.root)

    def write_base_skill(self, description: str) -> None:
        (self.skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: Base Skill\n"
            f"description: {description}\n"
            "version: 1.0.0\n"
            "author: Fixture\n"
            "license: MIT\n"
            "agent_created: false\n"
            "---\n"
            "BODY_SENTINEL_MUST_NEVER_ENTER_REPORT\n",
            encoding="utf-8",
        )

    def scan_at(self, generated_at: str) -> dict:
        return scan.build_project_skill_snapshot(
            self.root,
            self.projects,
            self.associations,
            generated_at=generated_at,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def incomplete(self, status: str) -> dict:
        payload = deepcopy(self.complete)
        payload["scan_status"] = status
        payload.pop("changes")
        payload["issues"] = [
            {
                "issue_id": f"issue:{status}",
                "status": status,
                "code": f"fixture_{status}",
                "message": f"Fixture {status}",
            }
        ]
        return _reidentify(payload)

    def output_entries(self) -> set[str]:
        output = self.root / report.OUTPUT_ROOT
        return {path.name for path in output.iterdir()} if output.exists() else set()

    def test_complete_promotes_only_three_exact_outputs_at_0600(self) -> None:
        self.assertTrue(self.store.persist_scan_result(self.complete))
        self.assertEqual(
            self.output_entries(),
            {"snapshot.json", "last-attempt.json", "项目Skill总览.md"},
        )
        for path in (
            self.store.snapshot_path,
            self.store.last_attempt_path,
            self.store.markdown_path,
        ):
            self.assertTrue(path.is_file())
            self.assertFalse(path.is_symlink())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        persisted = self.store.load_snapshot()
        self.assertEqual(persisted, self.complete)
        self.assertEqual(persisted["changes"]["status"], "not_available")
        attempt = self.store.load_last_attempt()
        self.assertEqual(attempt["attempt_generation_id"], self.complete["generation_id"])
        self.assertEqual(
            attempt["complete_snapshot_generation_id"], self.complete["generation_id"]
        )
        self.assertTrue(attempt["promoted"])
        self.assertTrue(attempt["matches_complete_snapshot"])
        self.assertEqual(list((self.root / report.OUTPUT_ROOT).glob("*.tmp")), [])

    def test_markdown_separates_machine_observation_and_human_association(self) -> None:
        markdown = report.render_project_skill_markdown(self.complete)
        self.assertIn("## 机器观察", markdown)
        self.assertIn("## 人工关联（不升级机器证据）", markdown)
        self.assertIn(self.complete["generation_id"], markdown)
        self.assertIn("human_association", markdown)
        self.assertIn("文件发现不代表宿主可用", markdown)
        self.assertNotIn(str(self.root), markdown)
        self.assertNotIn("<script", markdown.lower())
        self.assertNotIn("BODY_SENTINEL_MUST_NEVER_ENTER_REPORT", markdown)
        self.assertIn("## 相对上一份完整快照的变化", markdown)
        self.assertIn("## 问题", markdown)

    def test_markdown_neutralizes_titles_newlines_links_images_pipes_and_html(self) -> None:
        payload = deepcopy(self.complete)
        injected = "# Injected\n[link](relative) ![image](relative) | `<b>tag</b>"
        payload["projects"][0]["display_name"] = injected
        payload["projects"][0]["logical_skills"][0]["display_name"] = injected
        payload["human_associations"]["items"][0]["reason"] = injected
        payload["issues"] = [
            {
                "issue_id": "issue:markdown",
                "status": "partial",
                "code": "markdown_fixture",
                "message": "ISSUE_MESSAGE_SENTINEL_MUST_NOT_RENDER",
                "project_id": "project-fixture",
                "relative_path": "safe/pipe|html<b>",
            }
        ]
        payload = _reidentify(payload)

        markdown = report.render_project_skill_markdown(payload)
        self.assertEqual(
            [line for line in markdown.splitlines() if line.startswith("# ")],
            ["# 项目 Skill 总览"],
        )
        self.assertNotIn("# Injected\n", markdown)
        self.assertNotIn("[link](relative)", markdown)
        self.assertNotIn("![image](relative)", markdown)
        self.assertNotIn("<b>", markdown)
        self.assertNotIn("| `<b>", markdown)
        self.assertNotIn("ISSUE_MESSAGE_SENTINEL_MUST_NOT_RENDER", markdown)
        self.assertIn(r"\[link\](relative)", markdown)
        self.assertIn(r"!\[image\](relative)", markdown)
        self.assertIn(r"\|", markdown)
        self.assertIn("&#96;&lt;b&gt;tag&lt;/b&gt;", markdown)
        self.assertIn("`partial` / `markdown_fixture`", markdown)
        self.assertIn("safe/pipe\\|html&lt;b&gt;", markdown)

    def test_markdown_rejects_absolute_path_payload_before_render(self) -> None:
        payload = deepcopy(self.complete)
        payload["projects"][0]["display_name"] = "https://private.invalid/project"
        payload = _identity_only(payload)
        with self.assertRaises(scan.PayloadValidationError):
            report.render_project_skill_markdown(payload)

    def test_markdown_loader_requires_the_committed_snapshot_generation(self) -> None:
        self.store.persist_scan_result(self.complete)
        self.assertEqual(
            self.store.load_markdown(), self.store.markdown_path.read_text(encoding="utf-8")
        )
        report.write_project_skill_bytes_atomically(
            self.root,
            report.MARKDOWN_TARGET,
            b"# stale or forged report\n",
        )
        with self.assertRaisesRegex(report.ProjectSkillPersistenceError, "generation mismatch"):
            self.store.load_markdown()

    def test_markdown_loader_rejects_same_generation_forged_content(self) -> None:
        self.store.persist_scan_result(self.complete)
        snapshot = self.store.load_snapshot()
        self.assertIsNotNone(snapshot)
        report.write_project_skill_bytes_atomically(
            self.root,
            report.MARKDOWN_TARGET,
            ("# forged\n\n" + f"- generation ID：`{snapshot['generation_id']}`\n").encode(),
        )
        with self.assertRaisesRegex(report.ProjectSkillPersistenceError, "generation mismatch"):
            self.store.load_markdown()

    def test_incomplete_results_only_replace_attempt_and_preserve_lkg_pair(self) -> None:
        self.store.persist_scan_result(self.complete)
        old_snapshot = self.store.snapshot_path.read_bytes()
        old_markdown = self.store.markdown_path.read_bytes()
        for status in ("partial", "error", "security_reject"):
            with self.subTest(status=status):
                incomplete = self.incomplete(status)
                self.assertFalse(self.store.persist_scan_result(incomplete))
                self.assertEqual(self.store.snapshot_path.read_bytes(), old_snapshot)
                self.assertEqual(self.store.markdown_path.read_bytes(), old_markdown)
                attempt = self.store.load_last_attempt()
                self.assertEqual(attempt["scan_status"], status)
                self.assertEqual(attempt["attempt_generation_id"], incomplete["generation_id"])
                self.assertEqual(
                    attempt["complete_snapshot_generation_id"], self.complete["generation_id"]
                )
                self.assertFalse(attempt["promoted"])
                self.assertFalse(attempt["matches_complete_snapshot"])
                self.assertEqual(
                    attempt["issues"],
                    [{"status": status, "code": f"fixture_{status}"}],
                )

    def test_second_complete_is_finalized_against_the_persisted_lkg(self) -> None:
        self.store.persist_scan_result(self.complete)
        newer = deepcopy(self.complete)
        newer["generated_at"] = "2000-01-01T12:01:00Z"
        newer = _reidentify(newer)

        self.assertTrue(self.store.persist_scan_result(newer))
        persisted = self.store.load_snapshot()
        self.assertEqual(persisted["changes"]["status"], "compared")
        self.assertEqual(
            persisted["changes"]["compared_to_generation_id"],
            self.complete["generation_id"],
        )
        self.assertEqual(persisted["changes"]["added"], [])
        self.assertEqual(persisted["changes"]["changed"], [])
        self.assertEqual(persisted["changes"]["removed"], [])
        self.assertNotEqual(persisted["generation_id"], newer["generation_id"])
        markdown = self.store.markdown_path.read_text(encoding="utf-8")
        self.assertIn(persisted["generation_id"], markdown)
        self.assertIn(self.complete["generation_id"], markdown)
        self.assertEqual(
            self.store.load_last_attempt()["attempt_generation_id"],
            persisted["generation_id"],
        )

    def test_added_changed_and_removed_are_computed_only_from_complete_lkg(self) -> None:
        for expected_key in ("added", "changed", "removed"):
            with self.subTest(expected_key=expected_key):
                # Each mutation needs an independent filesystem/store baseline.
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory).resolve()
                    project = root / "fixture-project"
                    container = root / "fixture-container"
                    skill = project / ".agents" / "skills" / "base"
                    skill.mkdir(parents=True)
                    container.mkdir()
                    (skill / "SKILL.md").write_text(
                        "---\nname: Base Skill\ndescription: Baseline description\n---\nBODY\n",
                        encoding="utf-8",
                    )
                    before = scan.build_project_skill_snapshot(
                        root, self.projects, self.associations, generated_at=FIXED_TIME
                    )
                    store = report.ProjectSkillStore(root)
                    store.persist_scan_result(before)
                    before_id = before["projects"][0]["logical_skills"][0]["logical_skill_id"]
                    if expected_key == "added":
                        added = skill.parent / "added"
                        added.mkdir()
                        (added / "SKILL.md").write_text(
                            "---\nname: Added Skill\ndescription: Added\n---\nBODY\n",
                            encoding="utf-8",
                        )
                    elif expected_key == "changed":
                        (skill / "SKILL.md").write_text(
                            "---\nname: Base Skill\ndescription: Changed\n---\nBODY\n",
                            encoding="utf-8",
                        )
                    else:
                        (skill / "SKILL.md").unlink()
                        skill.rmdir()
                    current = scan.build_project_skill_snapshot(
                        root,
                        self.projects,
                        self.associations,
                        generated_at="2000-01-01T12:01:00Z",
                    )
                    store.persist_scan_result(current)
                    changes = store.load_snapshot()["changes"]
                    self.assertEqual(changes["status"], "compared")
                    self.assertEqual(len(changes[expected_key]), 1)
                    for other in {"added", "changed", "removed"} - {expected_key}:
                        self.assertEqual(changes[other], [])
                    if expected_key in {"changed", "removed"}:
                        self.assertEqual(changes[expected_key], [before_id])
                    markdown = store.load_markdown()
                    label = {"added": "新增", "changed": "变化", "removed": "移除"}[
                        expected_key
                    ]
                    self.assertIn(f"{label}（1）", markdown)

    def test_partial_attempt_never_recomputes_or_replaces_complete_changes(self) -> None:
        self.store.persist_scan_result(self.complete)
        lkg = self.store.load_snapshot()
        partial = self.incomplete("partial")
        self.store.persist_scan_result(partial)
        self.assertEqual(self.store.load_snapshot(), lkg)
        self.assertNotIn("changes", partial)

    def test_forged_or_oversized_change_sets_fail_and_preserve_lkg(self) -> None:
        self.store.persist_scan_result(self.complete)
        snapshot_before = self.store.snapshot_path.read_bytes()
        markdown_before = self.store.markdown_path.read_bytes()
        current_id = self.complete["projects"][0]["logical_skills"][0]["logical_skill_id"]
        forged_overlap = deepcopy(self.complete)
        forged_overlap["changes"] = {
            "status": "compared",
            "compared_to_generation_id": "1" * 16,
            "fingerprint_basis": scan.PROJECTION_VERSION,
            "added": [current_id],
            "changed": [current_id],
            "removed": [],
        }
        forged_unknown = deepcopy(self.complete)
        forged_unknown["changes"] = {
            "status": "compared",
            "compared_to_generation_id": "1" * 16,
            "fingerprint_basis": scan.PROJECTION_VERSION,
            "added": ["skill:unknown"],
            "changed": [],
            "removed": [],
        }
        forged_removed = deepcopy(self.complete)
        forged_removed["changes"] = {
            "status": "compared",
            "compared_to_generation_id": "1" * 16,
            "fingerprint_basis": scan.PROJECTION_VERSION,
            "added": [],
            "changed": [],
            "removed": [current_id],
        }
        oversized = deepcopy(self.complete)
        oversized["changes"] = {
            "status": "compared",
            "compared_to_generation_id": "1" * 16,
            "fingerprint_basis": scan.PROJECTION_VERSION,
            "added": [f"skill:fake{index:04d}" for index in range(1001)],
            "changed": [],
            "removed": [],
        }
        for payload in (forged_overlap, forged_unknown, forged_removed, oversized):
            payload = _identity_only(payload)
            with self.assertRaises(scan.PayloadValidationError):
                self.store.persist_scan_result(payload)
            self.assertEqual(self.store.snapshot_path.read_bytes(), snapshot_before)
            self.assertEqual(self.store.markdown_path.read_bytes(), markdown_before)

    def test_incomplete_without_lkg_writes_only_attempt(self) -> None:
        incomplete = self.incomplete("partial")
        self.assertFalse(self.store.persist_scan_result(incomplete))
        self.assertEqual(self.output_entries(), {"last-attempt.json"})
        self.assertIsNone(self.store.load_snapshot())
        self.assertIsNone(self.store.load_last_attempt()["complete_snapshot_generation_id"])

    def test_forged_generation_id_is_rejected_before_any_write(self) -> None:
        forged = deepcopy(self.complete)
        forged["generation_id"] = "f" * 16
        with self.assertRaisesRegex(scan.PayloadValidationError, "generation id"):
            self.store.persist_scan_result(forged)
        self.assertFalse((self.root / report.OUTPUT_ROOT).exists())

    def test_incomplete_snapshot_cannot_render_markdown(self) -> None:
        with self.assertRaisesRegex(report.ProjectSkillPersistenceError, "complete"):
            report.render_project_skill_markdown(self.incomplete("error"))

    def test_writer_rejects_every_non_allowlisted_target(self) -> None:
        for target in (
            Path("generated/project-skills/extra.json"),
            Path("generated/other.json"),
            Path("snapshot.json"),
            Path("generated/project-skills/../escape.json"),
            self.root / report.SNAPSHOT_TARGET,
        ):
            with self.subTest(target=str(target)), self.assertRaisesRegex(
                report.ProjectSkillPersistenceError, "allowlist"
            ):
                report.write_project_skill_bytes_atomically(self.root, target, b"{}\n")

    def test_root_and_each_output_segment_reject_symlinks(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        linked_root = self.root / "linked-root"
        os.symlink(outside, linked_root, target_is_directory=True)
        with self.assertRaises(report.ProjectSkillPersistenceError):
            report.ProjectSkillStore(linked_root).persist_scan_result(self.complete)

        generated = self.root / "generated"
        os.symlink(outside, generated, target_is_directory=True)
        with self.assertRaises(report.ProjectSkillPersistenceError):
            self.store.persist_scan_result(self.complete)
        generated.unlink()
        generated.mkdir()
        os.symlink(outside, generated / "project-skills", target_is_directory=True)
        with self.assertRaises(report.ProjectSkillPersistenceError):
            self.store.persist_scan_result(self.complete)

    def test_each_existing_target_rejects_symlinks_without_touching_target(self) -> None:
        output = self.root / report.OUTPUT_ROOT
        output.mkdir(parents=True)
        outside = self.root / "outside.txt"
        outside.write_text("sentinel", encoding="utf-8")
        for target in report.ALLOWED_TARGETS:
            with self.subTest(target=target.name):
                path = self.root / target
                os.symlink(outside, path)
                with self.assertRaises(report.ProjectSkillPersistenceError):
                    report.write_project_skill_bytes_atomically(self.root, target, b"replacement")
                self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel")
                path.unlink()

    def test_second_file_failure_rolls_back_the_complete_pair(self) -> None:
        self.store.persist_scan_result(self.complete)
        old_snapshot = self.store.snapshot_path.read_bytes()
        old_markdown = self.store.markdown_path.read_bytes()
        newer = deepcopy(self.complete)
        newer["generated_at"] = "2000-01-01T12:01:00Z"
        newer = _reidentify(newer)
        real_write = report.write_project_skill_bytes_atomically
        failed = False

        def fail_snapshot_once(root, target, data):
            nonlocal failed
            if Path(target) == report.SNAPSHOT_TARGET and not failed:
                failed = True
                raise OSError("fixture snapshot write failure")
            return real_write(root, target, data)

        with mock.patch.object(
            report, "write_project_skill_bytes_atomically", side_effect=fail_snapshot_once
        ):
            with self.assertRaisesRegex(OSError, "fixture"):
                self.store.persist_scan_result(newer)
        self.assertEqual(self.store.snapshot_path.read_bytes(), old_snapshot)
        self.assertEqual(self.store.markdown_path.read_bytes(), old_markdown)
        self.assertEqual(list((self.root / report.OUTPUT_ROOT).glob("*.tmp")), [])

    def test_first_promotion_failure_removes_new_markdown_and_leaves_no_snapshot(self) -> None:
        real_write = report.write_project_skill_bytes_atomically

        def fail_snapshot(root, target, data):
            if Path(target) == report.SNAPSHOT_TARGET:
                raise OSError("fixture first snapshot failure")
            return real_write(root, target, data)

        with mock.patch.object(report, "write_project_skill_bytes_atomically", side_effect=fail_snapshot):
            with self.assertRaisesRegex(OSError, "fixture"):
                self.store.persist_scan_result(self.complete)
        self.assertFalse(self.store.snapshot_path.exists())
        self.assertFalse(self.store.markdown_path.exists())
        self.assertEqual(self.output_entries(), set())

    def test_receipt_failure_rolls_back_prepared_markdown_before_snapshot_commit(self) -> None:
        real_write = report.write_project_skill_bytes_atomically

        def fail_receipt(root, target, data):
            if Path(target) == report.LAST_ATTEMPT_TARGET:
                raise OSError("fixture receipt failure")
            return real_write(root, target, data)

        with mock.patch.object(report, "write_project_skill_bytes_atomically", side_effect=fail_receipt):
            with self.assertRaisesRegex(OSError, "receipt failure"):
                self.store.persist_scan_result(self.complete)
        self.assertIsNone(self.store.load_snapshot())
        self.assertFalse(self.store.markdown_path.exists())
        self.assertIsNone(self.store.load_last_attempt())
        self.assertIsNone(self.store.last_receipt_error)

    def test_atomic_writer_cleans_temp_file_after_write_failure(self) -> None:
        with mock.patch.object(report.os, "write", side_effect=OSError("fixture write failure")):
            with self.assertRaisesRegex(OSError, "fixture"):
                report.write_project_skill_bytes_atomically(
                    self.root, report.LAST_ATTEMPT_TARGET, b"receipt"
                )
        output = self.root / report.OUTPUT_ROOT
        self.assertEqual(list(output.iterdir()), [])

    def test_loaders_reject_malformed_or_incomplete_persisted_data(self) -> None:
        report.write_project_skill_bytes_atomically(
            self.root, report.SNAPSHOT_TARGET, b'{"schema_version":1}\n'
        )
        with self.assertRaises(report.ProjectSkillPersistenceError):
            self.store.load_snapshot()
        report.write_project_skill_bytes_atomically(
            self.root, report.LAST_ATTEMPT_TARGET, b'{"schema_version":1}\n'
        )
        with self.assertRaises(report.ProjectSkillPersistenceError):
            self.store.load_last_attempt()

    def test_attempt_validator_rejects_claimed_match_or_promotion_drift(self) -> None:
        self.store.persist_scan_result(self.complete)
        receipt = self.store.load_last_attempt()
        for key, value in (
            ("matches_complete_snapshot", False),
            ("promoted", False),
            ("complete_snapshot_generation_id", None),
        ):
            forged = deepcopy(receipt)
            forged[key] = value
            with self.subTest(key=key), self.assertRaises(report.ProjectSkillPersistenceError):
                report.validate_last_attempt(forged)

    def test_attempt_time_is_bounded_narrow_rfc3339(self) -> None:
        self.store.persist_scan_result(self.complete)
        receipt = self.store.load_last_attempt()
        for valid in (
            "2000-01-01T12:00:00Z",
            "2000-01-01T12:00:00.123456+08:00",
            "2000-01-01T12:00:00-07:30",
        ):
            candidate = deepcopy(receipt)
            candidate["attempted_at"] = valid
            report.validate_last_attempt(candidate)
        for invalid in (
            "2000-01-01 12:00:00+00:00",
            "2000-01-01T12:00:00",
            "2000-01-01T12:00:00z",
            "2000-01-01T12:00:00.1234567Z",
            "2026-02-30T12:00:00Z",
            "2000-01-01T12:00:00Z" + "0" * 64,
        ):
            candidate = deepcopy(receipt)
            candidate["attempted_at"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(
                report.ProjectSkillPersistenceError
            ):
                report.validate_last_attempt(candidate)

    def test_nonnarrow_complete_time_is_rejected_before_promotion(self) -> None:
        payload = deepcopy(self.complete)
        payload["generated_at"] = "2000-01-01 12:00:00+00:00"
        payload = _reidentify(payload)
        with self.assertRaisesRegex(report.ProjectSkillPersistenceError, "attempt time"):
            self.store.persist_scan_result(payload)
        self.assertFalse((self.root / report.OUTPUT_ROOT).exists())

    def test_attempt_issue_relative_path_is_bounded_to_4000(self) -> None:
        self.store.persist_scan_result(self.complete)
        receipt = self.store.load_last_attempt()
        issue = {"status": "partial", "code": "bounded_path"}
        accepted = deepcopy(receipt)
        accepted["issues"] = [{**issue, "relative_path": "a" * 4000}]
        report.validate_last_attempt(accepted)
        rejected = deepcopy(receipt)
        rejected["issues"] = [{**issue, "relative_path": "a" * 4001}]
        with self.assertRaisesRegex(report.ProjectSkillPersistenceError, "issue path"):
            report.validate_last_attempt(rejected)


if __name__ == "__main__":
    unittest.main()
