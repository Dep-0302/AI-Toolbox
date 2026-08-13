from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest

from src.project_skill_root import (
    ProjectSkillRootError,
    ROOT_CONFIG_TARGET,
    build_project_skill_root_config,
    load_project_skill_root_config,
    validate_project_skill_root_config,
    write_project_skill_root_config,
)


FIXED_TIME = "2026-08-13T08:00:00Z"


class ProjectSkillRootStorageTests(unittest.TestCase):
    def test_missing_setting_is_not_an_implicit_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self.assertIsNone(load_project_skill_root_config(root))
            self.assertFalse((root / ROOT_CONFIG_TARGET).exists())

    def test_round_trip_is_exact_0600_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            toolbox = Path(temporary).resolve()
            observed = toolbox.parent / "中文 project"
            observed.mkdir(exist_ok=True)
            info = observed.stat()
            row = build_project_skill_root_config(
                observed,
                device=info.st_dev,
                inode=info.st_ino,
                configured_at=FIXED_TIME,
                root_id="root-0123456789abcdef",
            )
            write_project_skill_root_config(row, toolbox)
            target = toolbox / ROOT_CONFIG_TARGET
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(load_project_skill_root_config(toolbox), row)
            self.assertEqual([path.name for path in target.parent.iterdir()], [target.name])

    def test_unknown_fields_invalid_values_and_oversize_fail_closed(self) -> None:
        base = {
            "schema_version": 1,
            "root_id": "root-0123456789abcdef",
            "path": "/tmp/project",
            "configured_at": FIXED_TIME,
            "device": 1,
            "inode": 2,
        }
        for mutation in (
            {**base, "extra": True},
            {**base, "root_id": "guess"},
            {**base, "path": "relative"},
            {**base, "path": "/tmp/../secret"},
            {**base, "configured_at": "today"},
            {**base, "device": True},
        ):
            with self.subTest(mutation=mutation):
                with self.assertRaises(ProjectSkillRootError):
                    validate_project_skill_root_config(mutation)

        with tempfile.TemporaryDirectory() as temporary:
            toolbox = Path(temporary).resolve()
            target = toolbox / ROOT_CONFIG_TARGET
            target.parent.mkdir(parents=True)
            target.write_bytes(b"{" + b"x" * 9000)
            os.chmod(target, 0o600)
            with self.assertRaises(ProjectSkillRootError):
                load_project_skill_root_config(toolbox)

    def test_symlinked_setting_and_ancestor_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            toolbox = base / "toolbox"
            toolbox.mkdir()
            outside = base / "outside.json"
            outside.write_text(json.dumps({"secret": "do-not-read"}), encoding="utf-8")
            target = toolbox / ROOT_CONFIG_TARGET
            target.parent.mkdir(parents=True)
            target.symlink_to(outside)
            with self.assertRaises(ProjectSkillRootError):
                load_project_skill_root_config(toolbox)

            target.unlink()
            (toolbox / "generated").rename(toolbox / "generated-real")
            (toolbox / "generated").symlink_to(toolbox / "generated-real", target_is_directory=True)
            with self.assertRaises(ProjectSkillRootError):
                load_project_skill_root_config(toolbox)


if __name__ == "__main__":
    unittest.main()
