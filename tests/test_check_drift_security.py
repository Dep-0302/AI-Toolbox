from __future__ import annotations

import importlib.util
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "collection-workbench" / "check-drift.py"
SPEC = importlib.util.spec_from_file_location("check_drift_security_target", SCRIPT)
assert SPEC and SPEC.loader
drift = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = drift
SPEC.loader.exec_module(drift)


class CheckDriftSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "tests")
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def make_skill(root: Path, name: str = "example", body: str = "# Example\n") -> Path:
        skill = root / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(body, encoding="utf-8")
        return skill

    def budget(self, **overrides):
        values = {
            "max_file_bytes": 1024 * 1024,
            "max_total_bytes": 4 * 1024 * 1024,
            "max_entries": 1000,
            "max_depth": 16,
            "max_seconds": 5.0,
        }
        values.update(overrides)
        return drift.ScanBudget(**values)

    def test_safe_regular_files_are_streamed_and_classified(self):
        codex = self.base / "codex"
        claude = self.base / "claude"
        one = self.make_skill(codex, body="# Same\n")
        two = self.make_skill(claude, body="# Same\n")
        (one / "notes.txt").write_text("attachment", encoding="utf-8")
        (two / "notes.txt").write_text("attachment", encoding="utf-8")

        skills, missing, warnings = drift.collect(
            {"codex": codex, "claude": claude}, self.budget()
        )

        self.assertEqual(missing, [])
        self.assertEqual(warnings, [])
        self.assertEqual(drift.classify(skills["example"]), "一致")
        digest = skills["example"]["codex"]["files"]["SKILL.md"]
        self.assertEqual(len(digest), 64)  # SHA-256, produced by streaming reads.

    def test_root_or_ancestor_symlink_fails_closed(self):
        real = self.base / "real"
        self.make_skill(real)
        linked = self.base / "linked-root"
        linked.symlink_to(real, target_is_directory=True)

        with self.assertRaisesRegex(drift.ScanIncomplete, "软链接"):
            drift.collect({"codex": linked}, self.budget())

        real_parent = self.base / "real-parent"
        self.make_skill(real_parent / "host")
        linked_parent = self.base / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaisesRegex(drift.ScanIncomplete, "软链接"):
            drift.collect({"codex": linked_parent / "host"}, self.budget())

    def test_child_directory_symlink_is_skipped_with_audit_warning(self):
        root = self.base / "host"
        root.mkdir()
        target = self.base / "outside"
        target.mkdir()
        (root / ".git").symlink_to(target, target_is_directory=True)

        skills, missing, warnings = drift.collect({"codex": root}, self.budget())

        self.assertEqual(skills, {})
        self.assertEqual(missing, [])
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].code, "symlink_skipped")
        self.assertTrue(warnings[0].path.endswith("/.git"))

    def test_manifest_and_attachment_symlinks_are_never_followed(self):
        for leaf in ("SKILL.md", "attachment.txt"):
            with self.subTest(leaf=leaf):
                root = self.base / ("host-" + leaf.replace(".", "-"))
                skill = root / "example"
                skill.mkdir(parents=True)
                target = self.base / ("target-" + leaf.replace(".", "-"))
                target.write_text("secret", encoding="utf-8")
                if leaf == "SKILL.md":
                    (skill / leaf).symlink_to(target)
                else:
                    (skill / "SKILL.md").write_text("# Safe\n", encoding="utf-8")
                    (skill / leaf).symlink_to(target)
                skills, _, warnings = drift.collect({"codex": root}, self.budget())
                self.assertEqual(len(warnings), 1)
                self.assertTrue(warnings[0].path.endswith("/" + leaf))
                if leaf == "SKILL.md":
                    self.assertEqual(skills, {})
                else:
                    self.assertIn("example", skills)
                    self.assertNotIn(
                        leaf, skills["example"]["codex"]["files"]
                    )

    def test_descendant_symlink_targets_never_enter_report_or_hashes(self):
        root = self.base / "host"
        real_skill = self.make_skill(root, name="regular", body="# Regular\n")
        outside_skill = self.make_skill(
            self.base / "outside-root", name="linked-skill", body="LINK_TARGET_SECRET"
        )
        secret_file = self.base / "outside-secret.txt"
        secret_file.write_text("LEAF_TARGET_SECRET", encoding="utf-8")
        secret_digest = hashlib.sha256(secret_file.read_bytes()).hexdigest()
        skill_digest = hashlib.sha256(
            (outside_skill / "SKILL.md").read_bytes()
        ).hexdigest()
        (root / "linked-skill").symlink_to(outside_skill, target_is_directory=True)
        (real_skill / "linked-attachment.txt").symlink_to(secret_file)
        git_target = self.base / "outside-git"
        git_target.mkdir()
        (root / ".git").symlink_to(git_target, target_is_directory=True)
        out = self.base / "report.html"

        code = drift.run_scan({"codex": root}, out, self.budget())

        report = out.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertIn("symlink_skipped", report)
        self.assertIn("linked-skill", report)
        self.assertIn("linked-attachment.txt", report)
        self.assertIn("/.git", report)
        self.assertNotIn("LINK_TARGET_SECRET", report)
        self.assertNotIn("LEAF_TARGET_SECRET", report)
        self.assertNotIn(secret_digest, report)
        self.assertNotIn(skill_digest, report)
        self.assertNotIn(str(outside_skill), report)
        self.assertNotIn(str(secret_file), report)

    def test_each_scan_budget_fails_closed(self):
        root = self.base / "host"
        skill = self.make_skill(root, body="# Manifest body\n")
        (skill / "payload.bin").write_bytes(b"x" * 64)

        cases = [
            (self.budget(max_file_bytes=8), "单文件预算"),
            (self.budget(max_total_bytes=8), "总读取预算"),
            (self.budget(max_entries=1), "条目预算"),
            (self.budget(max_depth=0), "目录深度"),
            (self.budget(max_seconds=-1), "时间预算"),
        ]
        for budget, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(drift.ScanIncomplete, message):
                    drift.collect({"codex": root}, budget)

    def test_incomplete_report_is_nonzero_and_contains_no_file_body(self):
        root = self.base / "host"
        secret = "TOP_SECRET_MUST_NOT_REACH_REPORT"
        self.make_skill(root, body=secret * 20)
        out = self.base / "report.html"

        code = drift.run_scan(
            {"codex": root}, out, self.budget(max_file_bytes=32)
        )

        report = out.read_text(encoding="utf-8")
        self.assertEqual(code, 2)
        self.assertIn("扫描未完成", report)
        self.assertNotIn(secret, report)

    def test_report_escapes_filesystem_controlled_skill_names(self):
        codex = self.base / "codex"
        claude = self.base / "claude"
        bad_name = 'bad<img src=x onerror="alert(1)">'
        self.make_skill(codex, name=bad_name, body="# One\n")
        self.make_skill(claude, name=bad_name, body="# Two\n")
        out = self.base / "report.html"

        code = drift.run_scan(
            {"codex": codex, "claude": claude}, out, self.budget()
        )

        report = out.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertNotIn('<img src=x onerror="alert(1)">', report)
        self.assertIn("&lt;img", report)


if __name__ == "__main__":
    unittest.main()
