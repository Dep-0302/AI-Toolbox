from __future__ import annotations

import copy
import hashlib
import json
import os
import plistlib
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import collection_scan
from collection_scan import (
    build_collection_input_state,
    build_collection_payload,
    build_collection_source_state,
    validate_collection_payload,
)


FIXED_NOW = "2026-08-09T20:00:00Z"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_TAXONOMY = PROJECT_ROOT / "registry" / "collection_taxonomy.json"
ORIGINAL_CANDIDATE_RULES = PROJECT_ROOT / "collection-workbench" / "rules.json"


def write_skill(
    path: Path,
    name: str,
    description: str = "fixture",
    version: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    version_row = f"version: {version}\n" if version else ""
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n{version_row}---\n\n# {name}\n",
        encoding="utf-8",
    )


def tree_fingerprints(root: Path) -> list[tuple[str, str, str | None]]:
    rows: list[tuple[str, str, str | None]] = []
    for candidate in sorted(root.rglob("*")):
        relative = candidate.relative_to(root).as_posix()
        if candidate.is_symlink():
            rows.append((relative, "symlink", os.readlink(candidate)))
        elif candidate.is_dir():
            rows.append((relative, "dir", None))
        else:
            rows.append((relative, "file", hashlib.sha256(candidate.read_bytes()).hexdigest()))
    return rows


class CollectionScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        # macOS exposes TemporaryDirectory through /var, an intentional system
        # symlink. Resolve the fixture base so scanner tests exercise a real
        # no-follow root; dedicated tests below cover ancestor aliases.
        self.root = Path(self.temporary.name).resolve(strict=True)
        self.collection = self.root / "collection"
        self.collection.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.host_roots = {
            "codex": self.home / ".codex" / "skills",
            "claude": self.home / ".claude" / "skills",
            "hermes": self.home / ".hermes" / "skills",
            "workbuddy": self.home / ".workbuddy" / "skills",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def payload(self, taxonomy_path: Path | None = None):
        return build_collection_payload(
            source_root=self.collection,
            host_roots=self.host_roots,
            taxonomy_path=taxonomy_path or self.root / "missing-taxonomy.json",
            now=FIXED_NOW,
        )

    def write_taxonomy(self, overrides: dict[str, dict]) -> Path:
        path = self.root / "collection-taxonomy.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "scenario": {"overrides": {}, "rules": []},
                    "assets": {"overrides": overrides},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return path

    def test_indexes_repository_as_parent_with_child_capabilities(self) -> None:
        repository = self.collection / "整库" / "sample-repo"
        (repository / ".git").mkdir(parents=True)
        write_skill(repository / "skills" / "review" / "SKILL.md", "repo-review")
        (repository / ".codex-plugin").mkdir()
        (repository / ".codex-plugin" / "plugin.json").write_text(
            json.dumps({"name": "repo-plugin", "description": "fixture plugin"}),
            encoding="utf-8",
        )
        (repository / "README.md").write_text("reference", encoding="utf-8")

        payload = validate_collection_payload(self.payload())

        self.assertEqual(payload["summary"]["repository_count"], 1)
        self.assertEqual(payload["summary"]["source_count"], 1)
        source = payload["items"][0]
        self.assertEqual(source["kind"], "repository")
        self.assertEqual(source["classification"]["primary_type"], "project")
        self.assertEqual(source["classification"]["functional_type"], "plugin")
        self.assertEqual({row["type"] for row in source["capabilities"]}, {"skill", "plugin"})
        self.assertEqual(source["shape"]["documents"], 2)
        self.assertEqual(source["proposed_bucket"], "20_整库与项目")

    def test_origin_categories_keep_evidence_private_and_versions_explicit(self) -> None:
        repository = self.collection / "archives" / "github-project"
        (repository / ".git").mkdir(parents=True)
        (repository / ".git" / "config").write_text(
            "[remote \"origin\"]\n"
            "  url = git@github.com:example/private-looking.git?token=do-not-store\n",
            encoding="utf-8",
        )
        write_skill(repository / "SKILL.md", "github-project", version="2.4.1")

        payload = validate_collection_payload(self.payload())
        source = payload["items"][0]

        self.assertEqual(source["origin_kind"], "github")
        self.assertEqual(source["origin_basis"], "git_remote")
        self.assertEqual(source["version"], "2.4.1")
        self.assertEqual(source["capabilities"][0]["version"], "2.4.1")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("do-not-store", serialized)
        self.assertNotIn("github.com:example", serialized)

    def test_download_chat_local_and_unknown_origins_stay_in_fixed_categories(self) -> None:
        website = self.collection / "download-v1.3.zip"
        with zipfile.ZipFile(website, "w") as bundle:
            bundle.writestr("notes.txt", "fixture")
        chat = self.collection / "inbox" / "chat-notes.md"
        chat.parent.mkdir()
        chat.write_text("fixture", encoding="utf-8")
        local = self.collection / "local-notes.md"
        local.write_text("fixture", encoding="utf-8")
        unknown = self.collection / "unknown-notes.md"
        unknown.write_text("fixture", encoding="utf-8")
        taxonomy = self.write_taxonomy({"local-notes.md": {"source_kind": "local"}})
        where_from = plistlib.dumps(["https://example.org/file.zip?secret=do-not-store"])

        def fake_getxattr(path, _name, **_kwargs):
            if isinstance(path, int):
                observed = os.fstat(path)
                expected = website.stat()
                is_website = (observed.st_dev, observed.st_ino) == (
                    expected.st_dev,
                    expected.st_ino,
                )
            else:
                is_website = Path(path) == website
            if is_website:
                return where_from
            raise OSError("attribute missing")

        with mock.patch("collection_scan.os.getxattr", side_effect=fake_getxattr, create=True):
            payload = validate_collection_payload(self.payload(taxonomy))
        by_path = {item["relative_path"]: item for item in payload["items"]}

        self.assertEqual(by_path["download-v1.3.zip"]["origin_kind"], "website")
        self.assertEqual(by_path["download-v1.3.zip"]["origin_basis"], "download_metadata")
        self.assertEqual(by_path["download-v1.3.zip"]["version"], "1.3")
        self.assertEqual(by_path["inbox/chat-notes.md"]["origin_kind"], "chat")
        self.assertEqual(by_path["local-notes.md"]["origin_kind"], "local")
        self.assertEqual(by_path["local-notes.md"]["origin_basis"], "manual")
        self.assertEqual(by_path["unknown-notes.md"]["origin_kind"], "unknown")
        self.assertNotIn("do-not-store", json.dumps(payload, ensure_ascii=False))

    def test_deep_documents_and_nonstandard_archive_never_disappear(self) -> None:
        card = self.collection / "archives" / "notes" / "project-card.md"
        card.parent.mkdir(parents=True)
        card.write_text("短说明也必须被索引", encoding="utf-8")
        archive = self.collection / "1.剧本skill.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("剧本/提示词一.txt", "prompt")
            bundle.writestr("剧本/提示词二.txt", "prompt")

        payload = validate_collection_payload(self.payload())
        by_path = {item["relative_path"]: item for item in payload["items"]}

        self.assertIn("archives/notes/project-card.md", by_path)
        self.assertEqual(by_path["1.剧本skill.zip"]["kind"], "archive")
        self.assertEqual(by_path["1.剧本skill.zip"]["shape"]["documents"], 2)
        self.assertEqual(by_path["1.剧本skill.zip"]["proposed_bucket"], "99_待识别")
        self.assertEqual(
            by_path["1.剧本skill.zip"]["classification"]["status"], "needs_review"
        )

    def test_host_symlink_marks_source_as_anchor_without_following_it(self) -> None:
        repository = self.collection / "workflows" / "coordinator"
        (repository / ".git").mkdir(parents=True)
        target = repository / "脚手架" / "integrations" / "qunce" / "SKILL.md"
        write_skill(target, "qunce")
        link = self.host_roots["codex"] / "qunce" / "SKILL.md"
        link.parent.mkdir(parents=True)
        os.symlink(target, link)

        payload = validate_collection_payload(self.payload())
        source = payload["items"][0]

        self.assertEqual(source["installation_status"], "linked")
        self.assertEqual(source["move_safety"], "keep_anchor")
        self.assertEqual(source["proposed_bucket"], "原位保留_活动锚点")
        self.assertEqual([row["host_id"] for row in source["host_links"]], ["codex"])

    def test_duplicate_capabilities_keep_both_physical_sources(self) -> None:
        write_skill(self.collection / "first" / "SKILL.md", "same")
        write_skill(self.collection / "second" / "SKILL.md", "same")

        payload = validate_collection_payload(self.payload())

        self.assertEqual(payload["summary"]["source_count"], 2)
        self.assertEqual(payload["summary"]["capability_count"], 2)
        self.assertEqual(payload["summary"]["unique_capability_count"], 1)
        self.assertEqual(payload["summary"]["duplicate_group_count"], 1)
        self.assertTrue(all(item["duplicate_capability_count"] == 1 for item in payload["items"]))

    def test_backslash_skill_path_in_zip_is_recognized(self) -> None:
        archive = self.collection / "cinema.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(
                "skill\\cinema\\SKILL.md",
                "---\nname: cinema\ndescription: cinematic image editing skill\n---\n",
            )

        payload = validate_collection_payload(self.payload())
        source = payload["items"][0]

        self.assertEqual(source["kind"], "skill_archive")
        self.assertEqual(source["classification"]["primary_type"], "skill_bundle")
        self.assertEqual(source["classification"]["functional_type"], "skill")
        self.assertEqual(source["capabilities"][0]["relative_path"], "skill/cinema/SKILL.md")

    def test_archive_plugin_manifest_and_candidate_skill_are_components(self) -> None:
        archive = self.collection / "mixed.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(
                "plugin.json",
                json.dumps({"name": "mixed-plugin", "description": "plugin fixture"}),
            )
            bundle.writestr(
                "research-hub.md",
                "---\nname: research-hub\ndescription: routes a research workflow\n---\n",
            )

        payload = validate_collection_payload(self.payload())
        source = payload["items"][0]

        self.assertEqual(
            {row["type"] for row in source["capabilities"]}, {"plugin", "skill_candidate"}
        )
        self.assertTrue(source["classification"]["is_composite"])
        self.assertEqual(source["classification"]["functional_type"], "plugin")
        self.assertIn("plugin", source["classification"]["component_types"])
        self.assertIn("workflow", source["classification"]["component_types"])

    def test_agent_collection_frontmatter_is_not_counted_as_skill_candidate(self) -> None:
        collection = self.collection / "agents" / "agency-agents"
        collection.mkdir(parents=True)
        (collection / ".git").mkdir()
        (collection / "research-agent.md").write_text(
            "---\nname: research-agent\ndescription: research specialist agent\n---\n",
            encoding="utf-8",
        )

        payload = validate_collection_payload(self.payload())
        source = payload["items"][0]

        self.assertEqual(source["capabilities"][0]["type"], "agent_profile")
        self.assertEqual(source["classification"]["functional_type"], "workflow")
        self.assertNotIn("skill_bundle", source["classification"]["component_types"])

    def test_manual_asset_root_groups_loose_files_under_one_parent(self) -> None:
        project = self.collection / "research-suite"
        project.mkdir()
        (project / "research-hub.md").write_text(
            "---\nname: research-hub\ndescription: 调研编排路由工作流\n---\n",
            encoding="utf-8",
        )
        (project / "notes.txt").write_text("reference", encoding="utf-8")
        taxonomy = self.write_taxonomy(
            {
                "research-suite": {
                    "group_as_source": True,
                    "primary_type": "composite_asset",
                    "display_label": "复合 Skill 项目",
                    "component_types": ["skill_bundle", "workflow"],
                    "status": "reviewed",
                    "confidence": "high",
                    "readiness": "needs_adaptation",
                    "summary": "fixture",
                    "basis": ["manual fixture"],
                    "proposed_bucket": "20_整库与项目",
                }
            }
        )

        payload = validate_collection_payload(self.payload(taxonomy))
        source = payload["items"][0]

        self.assertEqual(payload["summary"]["source_count"], 1)
        self.assertEqual(source["kind"], "collection_directory")
        self.assertEqual(source["relative_path"], "research-suite")
        self.assertEqual(source["classification"]["status"], "reviewed")
        self.assertEqual(source["shape"]["files"], 2)

    def test_unsupported_standalone_file_remains_visible(self) -> None:
        image = self.collection / "reference.png"
        image.write_bytes(b"fixture")

        payload = validate_collection_payload(self.payload())
        source = payload["items"][0]

        self.assertEqual(source["kind"], "resource_file")
        self.assertEqual(source["classification"]["primary_type"], "resource")
        self.assertEqual(source["classification"]["functional_type"], "other_material")
        self.assertIsNone(source["classification"]["primary_scenario"])
        self.assertEqual(source["shape"]["images"], 1)

    def test_multiple_real_skill_entries_make_one_composite_skill_bundle(self) -> None:
        archive = self.collection / "screenplay-suite.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(
                "orchestrator/SKILL.md",
                "---\nname: screenplay-orchestrator\ndescription: screenplay workflow\n---\n",
            )
            bundle.writestr(
                "doctor/SKILL.md",
                "---\nname: screenplay-doctor\ndescription: screenplay review\n---\n",
            )

        payload = validate_collection_payload(self.payload())
        source = payload["items"][0]

        self.assertEqual(source["classification"]["functional_type"], "composite_skill_bundle")
        self.assertEqual(
            {row["name"] for row in source["capabilities"]},
            {"screenplay-orchestrator", "screenplay-doctor"},
        )

    def test_supporting_skill_examples_do_not_upgrade_a_single_skill(self) -> None:
        bundle = self.collection / "skill-forge"
        write_skill(bundle / "SKILL.md", "skill-forge")
        write_skill(bundle / "templates" / "starter" / "SKILL.md", "example-skill")

        payload = validate_collection_payload(self.payload())
        source = payload["items"][0]

        self.assertEqual(source["classification"]["functional_type"], "skill")
        self.assertEqual(
            [row["name"] for row in source["capabilities"] if row["type"] == "skill"],
            ["skill-forge"],
        )

    def test_public_taxonomy_excludes_personal_collection_overrides(self) -> None:
        taxonomy = json.loads(REAL_TAXONOMY.read_text(encoding="utf-8"))
        candidate_rules = json.loads(ORIGINAL_CANDIDATE_RULES.read_text(encoding="utf-8"))

        self.assertEqual(taxonomy["scenario"]["groups"], [])
        self.assertEqual(taxonomy["scenario"]["overrides"], {})
        self.assertEqual(taxonomy["assets"]["overrides"], {})
        self.assertEqual(len(candidate_rules["groups"]), 13)
        self.assertEqual(candidate_rules["overrides"], {})

    def test_plugin_manifest_takes_precedence_without_personal_taxonomy_override(self) -> None:
        archive = self.collection / "inbox" / "webnovel-writer-master.zip"
        archive.parent.mkdir()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(
                "webnovel-writer/.claude-plugin/plugin.json",
                json.dumps({"name": "webnovel-writer"}),
            )
            bundle.writestr(
                "webnovel-writer/skills/webnovel-write/SKILL.md",
                "---\nname: webnovel-write\ndescription: write webnovel chapters\n---\n",
            )
            bundle.writestr(
                "webnovel-writer/skills/webnovel-review/SKILL.md",
                "---\nname: webnovel-review\ndescription: review webnovel chapters\n---\n",
            )

        payload = validate_collection_payload(self.payload(REAL_TAXONOMY))
        source = payload["items"][0]

        self.assertEqual(source["classification"]["functional_type"], "plugin")
        self.assertEqual(source["classification"]["primary_scenario"], "未归类")

    def test_functional_types_are_exclusive_and_other_material_has_no_scenario(self) -> None:
        write_skill(self.collection / "skill" / "SKILL.md", "cinema")
        (self.collection / "reference.pdf").write_bytes(b"fixture")

        payload = validate_collection_payload(self.payload(REAL_TAXONOMY))
        functional_types = [row["classification"]["functional_type"] for row in payload["items"]]

        self.assertEqual(len(functional_types), payload["summary"]["source_count"])
        self.assertTrue(
            set(functional_types).issubset(
                {"skill", "project", "composite_skill_bundle", "plugin", "workflow", "other_material"}
            )
        )
        other = next(row for row in payload["items"] if row["classification"]["functional_type"] == "other_material")
        self.assertIsNone(other["classification"]["primary_scenario"])
        skill = next(row for row in payload["items"] if row["classification"]["functional_type"] == "skill")
        self.assertEqual(skill["classification"]["primary_scenario"], "未归类")

    def test_scan_is_read_only_for_collection_and_hosts(self) -> None:
        write_skill(self.collection / "safe" / "SKILL.md", "safe")
        self.host_roots["codex"].mkdir(parents=True)
        before_collection = tree_fingerprints(self.collection)
        before_hosts = tree_fingerprints(self.home)

        validate_collection_payload(self.payload())

        self.assertEqual(tree_fingerprints(self.collection), before_collection)
        self.assertEqual(tree_fingerprints(self.home), before_hosts)

    def test_source_state_detects_nested_add_modify_rename_and_delete(self) -> None:
        document = self.collection / "nested" / "notes.txt"
        document.parent.mkdir()
        document.write_text("alpha", encoding="utf-8")
        initial = build_collection_source_state(self.collection)
        self.assertEqual(initial["status"], "observed")

        document.write_text("bravo", encoding="utf-8")
        info = document.stat()
        os.utime(document, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
        modified = build_collection_source_state(self.collection)
        self.assertNotEqual(modified["signature"], initial["signature"])

        renamed = document.with_name("renamed.txt")
        document.rename(renamed)
        renamed_state = build_collection_source_state(self.collection)
        self.assertNotEqual(renamed_state["signature"], modified["signature"])

        renamed.unlink()
        deleted = build_collection_source_state(self.collection)
        self.assertNotEqual(deleted["signature"], renamed_state["signature"])

    def test_source_state_ignores_skip_dirs_and_ds_store_but_audits_symlinks(self) -> None:
        document = self.collection / "nested" / "notes.txt"
        document.parent.mkdir()
        document.write_text("fixture", encoding="utf-8")
        initial = build_collection_source_state(self.collection)

        (self.collection / ".DS_Store").write_bytes(b"ignored")
        git = self.collection / ".git"
        git.mkdir()
        (git / "index").write_bytes(b"ignored")
        os.symlink(document, self.collection / "linked-notes")
        ignored = build_collection_source_state(self.collection)

        self.assertEqual(ignored["status"], "observed")
        self.assertNotEqual(ignored["signature"], initial["signature"])
        self.assertEqual(ignored["error_count"], 1)

    def test_source_state_tracks_git_config_metadata_without_opening_the_body(self) -> None:
        repository = self.collection / "repository"
        config = repository / ".git" / "config"
        config.parent.mkdir(parents=True)
        config.write_text("url = https://github.com/example/one.git\n", encoding="utf-8")

        with mock.patch("collection_scan._read_regular_file", side_effect=AssertionError):
            initial = build_collection_source_state(self.collection)
        config.write_text("url = https://github.com/example/a-longer-name.git\n", encoding="utf-8")
        changed = build_collection_source_state(self.collection)

        self.assertEqual(initial["status"], "observed")
        self.assertNotEqual(changed["signature"], initial["signature"])

    def test_input_state_tracks_taxonomy_and_collection_host_anchors(self) -> None:
        source = self.collection / "sample"
        write_skill(source / "SKILL.md", "sample")
        taxonomy = self.root / "taxonomy.json"
        taxonomy.write_text('{"schema_version":1}', encoding="utf-8")
        initial = build_collection_input_state(
            source_root=self.collection,
            host_roots=self.host_roots,
            taxonomy_path=taxonomy,
        )

        taxonomy.write_text('{"schema_version":2}', encoding="utf-8")
        taxonomy_changed = build_collection_input_state(
            source_root=self.collection,
            host_roots=self.host_roots,
            taxonomy_path=taxonomy,
        )
        self.assertNotEqual(taxonomy_changed["signature"], initial["signature"])

        taxonomy.write_text('{"schema_version":1}', encoding="utf-8")
        self.host_roots["codex"].mkdir(parents=True)
        os.symlink(source, self.host_roots["codex"] / "sample", target_is_directory=True)
        anchored = build_collection_input_state(
            source_root=self.collection,
            host_roots=self.host_roots,
            taxonomy_path=taxonomy,
        )
        self.assertNotEqual(anchored["signature"], initial["signature"])
        self.assertEqual(anchored["host_link_count"], 1)

    def test_source_state_errors_and_budget_are_fail_safe(self) -> None:
        (self.collection / "one.txt").write_text("one", encoding="utf-8")
        (self.collection / "two.txt").write_text("two", encoding="utf-8")
        with mock.patch("collection_scan.MAX_SCAN_ENTRIES", 1):
            limited = build_collection_source_state(self.collection)
        self.assertEqual(limited["status"], "partial")
        self.assertIsNone(limited["signature"])

        with mock.patch("collection_scan.os.scandir", side_effect=OSError("fixture")):
            failed = build_collection_source_state(self.collection)
        self.assertEqual(failed["status"], "partial")
        self.assertIsNone(failed["signature"])

    def test_rejects_root_aliases_and_audits_descendant_symlinks_without_following(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        write_skill(outside / "SKILL.md", "outside")

        real_parent = self.root / "real-parent"
        real_collection = real_parent / "collection"
        real_collection.mkdir(parents=True)
        write_skill(real_collection / "safe" / "SKILL.md", "safe")
        ancestor_alias = self.root / "ancestor-alias"
        os.symlink(real_parent, ancestor_alias, target_is_directory=True)
        ancestor_payload = build_collection_payload(
            source_root=ancestor_alias / "collection",
            host_roots={},
            taxonomy_path=self.root / "missing.json",
            now=FIXED_NOW,
        )
        self.assertEqual(ancestor_payload["scan_errors"][0]["code"], "source_root_unsafe")
        self.assertEqual(ancestor_payload["items"], [])

        root_alias = self.root / "root-alias"
        os.symlink(real_collection, root_alias, target_is_directory=True)
        root_payload = build_collection_payload(
            source_root=root_alias,
            host_roots={},
            taxonomy_path=self.root / "missing.json",
            now=FIXED_NOW,
        )
        self.assertEqual(root_payload["scan_errors"][0]["code"], "source_root_unsafe")

        child_alias = self.collection / "child-alias"
        os.symlink(outside, child_alias, target_is_directory=True)
        file_alias = self.collection / "file-alias.md"
        os.symlink(outside / "SKILL.md", file_alias)
        child_payload = validate_collection_payload(self.payload())
        self.assertEqual(
            child_payload["scan_errors"],
            [
                {"code": "symlink_skipped", "path": "child-alias"},
                {"code": "symlink_skipped", "path": "file-alias.md"},
            ],
        )
        self.assertEqual(child_payload["items"], [])
        self.assertNotIn("outside", json.dumps(child_payload))
        child_alias.unlink()
        file_alias.unlink()

        git_alias = self.collection / "repository" / ".git"
        git_alias.parent.mkdir()
        os.symlink(outside, git_alias, target_is_directory=True)
        git_payload = validate_collection_payload(self.payload())
        self.assertEqual(
            git_payload["scan_errors"],
            [{"code": "symlink_skipped", "path": "repository/.git"}],
        )
        self.assertEqual(git_payload["items"], [])

    def test_root_replacement_during_scan_fails_closed(self) -> None:
        write_skill(self.collection / "safe" / "SKILL.md", "safe")
        original_verify = collection_scan._AnchoredTree.verify_snapshot
        replaced = False

        def replace_before_verify(tree):
            nonlocal replaced
            if not replaced:
                replaced = True
                moved = self.root / "collection-before-replacement"
                self.collection.rename(moved)
                self.collection.mkdir()
                write_skill(self.collection / "outside" / "SKILL.md", "replacement")
            return original_verify(tree)

        with mock.patch.object(
            collection_scan._AnchoredTree,
            "verify_snapshot",
            autospec=True,
            side_effect=replace_before_verify,
        ):
            with self.assertRaisesRegex(ValueError, "changed|replaced"):
                self.payload()

    def test_subdirectory_replacement_during_scan_fails_closed(self) -> None:
        source = self.collection / "safe"
        write_skill(source / "SKILL.md", "safe")
        original_scan = collection_scan._scan_directory_capabilities_anchored
        replaced = False

        def replace_before_directory_read(tree, directory_relative, *, rules):
            nonlocal replaced
            if not replaced:
                replaced = True
                moved = self.collection / "safe-before-replacement"
                source.rename(moved)
                source.mkdir()
                write_skill(source / "SKILL.md", "replacement")
            return original_scan(tree, directory_relative, rules=rules)

        with mock.patch(
            "collection_scan._scan_directory_capabilities_anchored",
            autospec=True,
            side_effect=replace_before_directory_read,
        ):
            with self.assertRaisesRegex(ValueError, "replaced"):
                self.payload()

    def test_zip_bomb_budgets_fail_closed_before_manifest_read(self) -> None:
        member_archive = self.collection / "member.zip"
        with zipfile.ZipFile(member_archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("SKILL.md", "x" * 64)
        with mock.patch("collection_scan.MAX_ARCHIVE_MEMBER_BYTES", 32):
            payload = validate_collection_payload(self.payload())
        member = next(item for item in payload["items"] if item["name"] == "member.zip")
        self.assertEqual(member["scan_errors"][0]["code"], "archive_member_too_large")
        member_archive.unlink()

        total_archive = self.collection / "total.zip"
        with zipfile.ZipFile(total_archive, "w") as bundle:
            bundle.writestr("one.txt", "12345678")
            bundle.writestr("two.txt", "12345678")
        with mock.patch("collection_scan.MAX_ARCHIVE_UNCOMPRESSED_BYTES", 12):
            payload = validate_collection_payload(self.payload())
        total = next(item for item in payload["items"] if item["name"] == "total.zip")
        self.assertEqual(total["scan_errors"][0]["code"], "archive_total_size_limit")
        total_archive.unlink()

        ratio_archive = self.collection / "ratio.zip"
        with zipfile.ZipFile(ratio_archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr("payload.txt", b"0" * (1024 * 1024))
        payload = validate_collection_payload(self.payload())
        ratio = next(item for item in payload["items"] if item["name"] == "ratio.zip")
        self.assertEqual(ratio["scan_errors"][0]["code"], "archive_compression_ratio")
        ratio_archive.unlink()

        traversal_archive = self.collection / "traversal.zip"
        with zipfile.ZipFile(traversal_archive, "w") as bundle:
            bundle.writestr("../SKILL.md", "fixture")
        payload = validate_collection_payload(self.payload())
        traversal = next(item for item in payload["items"] if item["name"] == "traversal.zip")
        self.assertEqual(traversal["scan_errors"][0]["code"], "archive_path_invalid")

    def test_zip_symlink_member_is_skipped_without_opening_or_leaking_target(self) -> None:
        archive = self.collection / "archive-symlink.zip"
        marker = "SECRET_TARGET_MARKER/../../outside"
        link_info = zipfile.ZipInfo("links/SKILL.md")
        link_info.create_system = 3
        link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(link_info, marker)

        with mock.patch.object(
            zipfile.ZipFile,
            "open",
            autospec=True,
            side_effect=AssertionError("symlink member body must not be opened"),
        ):
            payload = validate_collection_payload(self.payload())

        item = next(row for row in payload["items"] if row["name"] == archive.name)
        self.assertEqual(
            item["scan_errors"],
            [
                {
                    "code": "symlink_skipped",
                    "path": "archive-symlink.zip/links/SKILL.md",
                }
            ],
        )
        self.assertEqual(item["capabilities"], [])
        self.assertEqual(item["shape"]["files"], 0)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(marker, serialized)
        self.assertNotIn("../../outside", serialized)

    def test_zip_duplicate_normalized_path_and_encryption_remain_fatal(self) -> None:
        duplicate = self.collection / "duplicate.zip"
        with zipfile.ZipFile(duplicate, "w") as bundle:
            bundle.writestr("nested//item.txt", "one")
            bundle.writestr("nested/item.txt", "two")
        payload = validate_collection_payload(self.payload())
        duplicate_item = next(row for row in payload["items"] if row["name"] == duplicate.name)
        self.assertEqual(
            duplicate_item["scan_errors"][0]["code"], "archive_duplicate_path"
        )
        duplicate.unlink()

        encrypted = self.collection / "encrypted.zip"
        with zipfile.ZipFile(encrypted, "w") as bundle:
            bundle.writestr("item.txt", "fixture")
        archive_bytes = bytearray(encrypted.read_bytes())
        for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            index = archive_bytes.find(signature)
            self.assertGreaterEqual(index, 0)
            flags = int.from_bytes(
                archive_bytes[index + flag_offset : index + flag_offset + 2], "little"
            ) | 0x1
            archive_bytes[index + flag_offset : index + flag_offset + 2] = flags.to_bytes(
                2, "little"
            )
        encrypted.write_bytes(archive_bytes)
        payload = validate_collection_payload(self.payload())
        encrypted_item = next(row for row in payload["items"] if row["name"] == encrypted.name)
        self.assertEqual(
            encrypted_item["scan_errors"][0]["code"], "archive_encrypted_entry"
        )

    def test_validate_collection_payload_rejects_malicious_nested_contracts(self) -> None:
        source = self.collection / "safe"
        write_skill(source / "SKILL.md", "safe")
        link = self.host_roots["codex"] / "safe" / "SKILL.md"
        link.parent.mkdir(parents=True)
        os.symlink(source / "SKILL.md", link)
        baseline = validate_collection_payload(self.payload())

        mutations = []

        def mutate(label, callback):
            candidate = copy.deepcopy(baseline)
            callback(candidate)
            mutations.append((label, candidate))

        mutate("generated_at", lambda row: row.__setitem__("generated_at", "not-a-time"))
        mutate(
            "modified_at timezone",
            lambda row: row["items"][0].__setitem__("modified_at", "2026-08-11T12:00:00"),
        )
        mutate("negative count", lambda row: row["summary"].__setitem__("source_count", -1))
        mutate("capability count", lambda row: row["summary"].__setitem__("capability_count", 99))
        mutate("absolute source path", lambda row: row["items"][0].__setitem__("relative_path", "/tmp/x"))
        mutate(
            "parent capability path",
            lambda row: row["items"][0]["capabilities"][0].__setitem__(
                "relative_path", "../SKILL.md"
            ),
        )
        mutate(
            "capability type",
            lambda row: row["items"][0]["capabilities"][0].__setitem__("type", "executable"),
        )
        mutate(
            "capability extra field",
            lambda row: row["items"][0]["capabilities"][0].__setitem__("secret", "x"),
        )
        mutate(
            "host target escape",
            lambda row: row["items"][0]["host_links"][0].__setitem__(
                "target_path", "/tmp/outside"
            ),
        )
        mutate(
            "shape mismatch",
            lambda row: row["items"][0]["shape"].__setitem__("files", 99),
        )
        mutate(
            "classification readiness",
            lambda row: row["items"][0]["classification"].__setitem__("readiness", "ready"),
        )
        mutate(
            "classification unhashable component",
            lambda row: row["items"][0]["classification"].__setitem__(
                "component_types", [{"type": "skill_bundle"}]
            ),
        )
        mutate(
            "relation parent escape",
            lambda row: row["items"][0]["relations"].append(
                {"type": "packaged_as", "target_relative_path": "../outside", "note": "x"}
            ),
        )
        mutate(
            "scan error fields",
            lambda row: (
                row["scan_errors"].append({"code": "unknown", "path": "x", "extra": "y"}),
                row["summary"].__setitem__("scan_error_count", 1),
            ),
        )

        for label, candidate in mutations:
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    validate_collection_payload(candidate)


if __name__ == "__main__":
    unittest.main()
