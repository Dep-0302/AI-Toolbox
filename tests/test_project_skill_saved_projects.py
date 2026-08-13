from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

import app
from project_skill_saved_projects import (
    SAVED_PROJECTS_TARGET,
    SavedProjectError,
    load_saved_projects,
    remove_saved_project,
    saved_projects_api_view,
    upsert_saved_project,
    validate_saved_projects,
    write_saved_projects,
)


class SavedProjectObservationTests(unittest.TestCase):
    def _project(self, root: Path, name: str = "external-project") -> Path:
        project = root / name
        skill = project / ".agents" / "skills" / "safe-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: safe-skill\ndescription: safe saved project skill\n---\nBODY_SECRET\n",
            encoding="utf-8",
        )
        return project

    def test_saved_project_round_trip_survives_reload_and_api_hides_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as toolbox_temp, tempfile.TemporaryDirectory() as project_temp:
            toolbox = Path(toolbox_temp).resolve()
            project = self._project(Path(project_temp).resolve())
            identity = app._directory_identity_without_symlinks(project)
            snapshot = app.build_in_memory_project_skill_preview(
                project, expected_identity=identity
            )
            project_id = snapshot["projects"][0]["project_id"]
            row = upsert_saved_project(
                project_id=project_id,
                path=project,
                device=identity[0],
                inode=identity[1],
                saved_at="2026-08-13T12:00:00Z",
                snapshot=snapshot,
                toolbox_root=toolbox,
            )

            target = toolbox / SAVED_PROJECTS_TARGET
            loaded = load_saved_projects(toolbox)
            public = saved_projects_api_view(loaded)

            self.assertEqual(loaded["projects"], [row])
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(public[0]["project_id"], project_id)
            self.assertEqual(public[0]["snapshot"]["projects"][0]["display_name"], project.name)
            self.assertNotIn("path", public[0])
            self.assertNotIn(str(project.parent), json.dumps(public, ensure_ascii=False))
            self.assertNotIn("BODY_SECRET", json.dumps(public, ensure_ascii=False))

            self.assertTrue(remove_saved_project(project_id, toolbox))
            self.assertEqual(load_saved_projects(toolbox)["projects"], [])
            self.assertFalse(remove_saved_project(project_id, toolbox))

    def test_manual_refresh_updates_saved_snapshot_without_writing_observed_project(self) -> None:
        with tempfile.TemporaryDirectory() as toolbox_temp, tempfile.TemporaryDirectory() as project_temp:
            toolbox = Path(toolbox_temp).resolve()
            project = self._project(Path(project_temp).resolve())
            manifest = project / ".agents" / "skills" / "safe-skill" / "SKILL.md"
            identity = app._directory_identity_without_symlinks(project)
            first = app.build_in_memory_project_skill_preview(project, expected_identity=identity)
            project_id = first["projects"][0]["project_id"]
            upsert_saved_project(
                project_id=project_id,
                path=project,
                device=identity[0],
                inode=identity[1],
                saved_at="2026-08-13T12:00:00Z",
                snapshot=first,
                toolbox_root=toolbox,
            )
            manifest.write_text(
                "---\nname: safe-skill\ndescription: updated safe projection\n---\nBODY_SECRET\n",
                encoding="utf-8",
            )
            before_tree = sorted(
                (path.relative_to(project).as_posix(), path.lstat().st_size)
                for path in project.rglob("*")
            )

            with mock.patch.object(app, "ROOT", toolbox):
                app.refresh_saved_project_observations()

            after_tree = sorted(
                (path.relative_to(project).as_posix(), path.lstat().st_size)
                for path in project.rglob("*")
            )
            refreshed = load_saved_projects(toolbox)["projects"][0]["snapshot"]
            projection = refreshed["projects"][0]["logical_skills"][0]["observations"][0][
                "frontmatter_projection"
            ]
            self.assertEqual(projection["description"], "updated safe projection")
            self.assertEqual(before_tree, after_tree)
            self.assertNotIn("BODY_SECRET", json.dumps(refreshed, ensure_ascii=False))

    def test_selecting_registered_project_reuses_registry_id_and_prunes_saved_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as toolbox_temp, tempfile.TemporaryDirectory() as root_temp:
            toolbox = Path(toolbox_temp).resolve()
            observation_root = Path(root_temp).resolve()
            project = self._project(observation_root, "example-project-alpha")
            identity = app._directory_identity_without_symlinks(project)
            duplicate_snapshot = app.build_in_memory_project_skill_preview(
                project, expected_identity=identity
            )
            duplicate_id = duplicate_snapshot["projects"][0]["project_id"]
            upsert_saved_project(
                project_id=duplicate_id,
                path=project,
                device=identity[0],
                inode=identity[1],
                saved_at="2026-08-13T12:00:00Z",
                snapshot=duplicate_snapshot,
                toolbox_root=toolbox,
            )
            root_state = {
                "configured": True,
                "root_id": "root-1234567890abcdef",
                "path": observation_root,
            }
            registered_snapshot = {
                "scan_scope": {"root_config_id": root_state["root_id"]},
                "projects": [{
                    "project_id": "project-alpha",
                    "relative_path": project.name,
                    "classification": "project",
                    "display_name": project.name,
                }],
            }
            store = mock.Mock()
            store.load_snapshot.return_value = registered_snapshot

            with mock.patch.object(app, "ROOT", toolbox), mock.patch.object(
                app, "load_project_skill_root_state", return_value=root_state
            ), mock.patch.object(app, "ProjectSkillStore", return_value=store):
                result = app.save_project_skill_observation({
                    "path": project,
                    "device": identity[0],
                    "inode": identity[1],
                })

            self.assertEqual(
                result,
                {"mode": "registered", "project_id": "project-alpha", "saved_projects": []},
            )
            self.assertEqual(load_saved_projects(toolbox)["projects"], [])
            store.load_snapshot.assert_called_once_with()

    def test_invalid_state_and_symlink_target_fail_closed(self) -> None:
        with self.assertRaises(SavedProjectError):
            validate_saved_projects({"schema_version": 1, "projects": [{"path": "relative"}]})

        with tempfile.TemporaryDirectory() as toolbox_temp, tempfile.TemporaryDirectory() as outside_temp:
            toolbox = Path(toolbox_temp).resolve()
            directory = toolbox / SAVED_PROJECTS_TARGET.parent
            directory.mkdir(parents=True)
            outside = Path(outside_temp).resolve() / "outside.json"
            outside.write_text("UNCHANGED", encoding="utf-8")
            os.symlink(outside, directory / SAVED_PROJECTS_TARGET.name)
            with self.assertRaises(SavedProjectError):
                write_saved_projects({"schema_version": 1, "projects": []}, toolbox)
            self.assertEqual(outside.read_text(encoding="utf-8"), "UNCHANGED")

    def test_duplicate_json_key_and_wrong_file_mode_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as toolbox_temp:
            toolbox = Path(toolbox_temp).resolve()
            target = toolbox / SAVED_PROJECTS_TARGET
            target.parent.mkdir(parents=True)
            target.write_text(
                '{"schema_version":1,"schema_version":1,"projects":[]}\n',
                encoding="utf-8",
            )
            target.chmod(0o600)
            with self.assertRaises(SavedProjectError):
                load_saved_projects(toolbox)

            target.write_text('{"schema_version":1,"projects":[]}\n', encoding="utf-8")
            target.chmod(0o644)
            with self.assertRaises(SavedProjectError):
                load_saved_projects(toolbox)


if __name__ == "__main__":
    unittest.main()
