from __future__ import annotations

import contextlib
import copy
import datetime
import importlib.util
import io
import json
import os
import stat
import struct
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKBENCH_DIR = PROJECT_ROOT / "collection-workbench"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


workbench = load_module("candidate_workbench_tests", WORKBENCH_DIR / "workbench.py")
inventory = workbench.inv
check_data = load_module("candidate_check_data_tests", WORKBENCH_DIR / "check_data.py")


class CandidateWorkbenchTest(unittest.TestCase):
    def setUp(self) -> None:
        # macOS /var and /tmp are symlink aliases. Put fixtures under the repo so
        # ancestor-symlink tests exercise only the path deliberately created.
        self.temp = tempfile.TemporaryDirectory(prefix=".candidate-fixture-", dir=PROJECT_ROOT / "tests")
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_skill(self, relative: str, *, name: str, body: str | None = None) -> Path:
        directory = self.source / relative
        directory.mkdir(parents=True, exist_ok=True)
        text = (
            "---\n"
            f"name: {name}\n"
            f"description: {name} has a sufficiently long description for the candidate contract.\n"
            "---\n"
            f"{body or name + ' unique body'}\n"
        )
        (directory / "SKILL.md").write_text(text, encoding="utf-8")
        return directory

    def write_zip(self, members: dict[str, str | bytes], *, compression: int = zipfile.ZIP_STORED) -> Path:
        archive = self.source / "fixture.zip"
        with zipfile.ZipFile(archive, "w", compression=compression) as bundle:
            for name, content in members.items():
                bundle.writestr(name, content)
        return archive

    def test_explicit_source_root_flows_through_payload_without_mutating_dump(self) -> None:
        self.write_skill("right-skill", name="right-skill")
        (self.source / "notes.md").write_text("# Notes\n" + "candidate note " * 600, encoding="utf-8")
        repo = self.source / "reference-repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "nested" / "SKILL.md").parent.mkdir(parents=True)
        (repo / "nested" / "SKILL.md").write_text("# nested\n", encoding="utf-8")

        wrong_root = self.base / "wrong-default"
        wrong_root.mkdir()
        with mock.patch.object(inventory, "DUMP", wrong_root):
            with mock.patch.object(
                inventory,
                "count_repo_skills",
                side_effect=AssertionError("repository path was reopened"),
            ):
                payload = workbench.build_payload(self.source, host_roots={})
                self.assertEqual(inventory.DUMP, wrong_root)
                self.assertEqual(
                    {item["name"] for item in workbench.build(self.source, host_roots={})},
                    {"right-skill"},
                )
                _docs, repos = workbench.loose_and_repos(self.source)

        self.assertEqual(payload["source_dir"], str(self.source.absolute()))
        self.assertEqual({item["name"] for item in payload["items"]}, {"right-skill"})
        self.assertEqual(payload["stats"]["loose_docs"], 1)
        self.assertEqual(repos, [{"name": "reference-repo", "src": "reference-repo", "skills_inside": 1}])
        self.assertFalse(any(key.startswith("_") for item in payload["items"] for key in item))
        result = check_data.main(payload=payload, require_data_js=False, quiet=True)
        self.assertEqual(result, 0)

    def test_default_source_root_remains_available_without_ignored_data_files(self) -> None:
        self.write_skill("default-skill", name="default-skill")
        with mock.patch.object(inventory, "DUMP", self.source):
            payload = workbench.build_payload(host_roots={})
        self.assertEqual(payload["source_dir"], str(self.source.absolute()))
        self.assertEqual([item["name"] for item in payload["items"]], ["default-skill"])

    def test_large_loose_document_uses_bounded_prefix_and_never_calls_path_isfile(self) -> None:
        skill = self.write_skill("safe-skill", name="safe-skill")
        (self.source / "large.md").write_text("# Large\n" + "long candidate note " * 800, encoding="utf-8")
        with mock.patch.object(os.path, "isfile", side_effect=AssertionError("path TOCTOU")):
            text = inventory.read_bounded_text(skill / "SKILL.md")
            _items, loose, _repos = inventory.collect(self.source)
        self.assertIn("safe-skill", text)
        self.assertEqual(len(loose), 1)
        self.assertEqual(loose[0]["name"], "Large")
        self.assertEqual(loose[0]["src"], "large.md")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are required")
    def test_root_chain_rejects_symlinks_but_descendants_are_skipped_with_warning(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        outside_manifest = outside / "SKILL.md"
        outside_manifest.write_text(
            "---\nname: leaked-target\ndescription: leaked target must never enter payload.\n---\n"
            "OUTSIDE_SECRET_MARKER\n",
            encoding="utf-8",
        )
        self.write_skill("safe-local", name="safe-local")

        real_parent = self.base / "real-parent"
        (real_parent / "collection").mkdir(parents=True)
        alias = self.base / "ancestor-alias"
        alias.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaises(inventory.ScanSafetyError):
            inventory.collect(alias / "collection")

        root_link = self.base / "root-link"
        root_link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(inventory.ScanSafetyError):
            inventory.collect(root_link)

        cases = (
            ("manifest-file", self.source / "linked-skill" / "SKILL.md", outside_manifest, False),
            ("child-dir", self.source / "child-dir", outside, True),
            ("git-dir", self.source / ".git", outside, True),
        )
        for name, link, target, is_directory in cases:
            with self.subTest(name=name):
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target, target_is_directory=is_directory)
                try:
                    with warnings.catch_warnings(record=True) as caught:
                        warnings.simplefilter("always")
                        scanned = inventory.collect(self.source)
                    serialized = json.dumps(scanned, ensure_ascii=False, default=str)
                    self.assertIn("safe-local", serialized)
                    self.assertNotIn("leaked-target", serialized)
                    self.assertNotIn("OUTSIDE_SECRET_MARKER", serialized)
                    self.assertTrue(any("跳过软链接" in str(warning.message) for warning in caught))
                finally:
                    link.unlink()
                    if link.parent.name == "linked-skill":
                        link.parent.rmdir()

    def test_sparse_groups_are_renumbered_to_the_payload_contract(self) -> None:
        self.write_skill("portrait", name="midjourney-portrait")
        payload = workbench.build_payload(self.source, host_roots={})
        self.assertEqual([group["name"] for group in payload["groups"]], ["人像与社媒生图"])
        self.assertEqual([group["order"] for group in payload["groups"]], [0])
        result = check_data.main(payload=payload, require_data_js=False, quiet=True)
        self.assertEqual(result, 0)

    def test_payload_emits_complete_time_host_and_installed_contract(self) -> None:
        skill = self.write_skill("candidate", name="candidate")
        host_root = self.base / "host"
        installed = host_root / "renamed-candidate"
        installed.mkdir(parents=True)
        (installed / "SKILL.md").write_bytes((skill / "SKILL.md").read_bytes())

        payload = workbench.build_payload(self.source, host_roots={"codex": host_root})
        generated = datetime.datetime.fromisoformat(payload["generated_at"])
        self.assertIsNotNone(generated.utcoffset())
        self.assertEqual(payload["hosts"], ["codex", "claude", "hermes", "workbuddy"])
        self.assertEqual(payload["stats"]["installed"], 1)
        self.assertEqual(payload["items"][0]["installed"]["hosts"], ["codex"])
        self.assertEqual(payload["items"][0]["installed"]["as_name"], "renamed-candidate")

    def test_checker_quiet_mode_and_negative_contracts(self) -> None:
        self.write_skill("candidate", name="candidate")
        payload = workbench.build_payload(self.source, host_roots={})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                check_data.main(payload=payload, require_data_js=False, quiet=True),
                0,
            )
        self.assertEqual(output.getvalue(), "")

        mutations = {
            "naive_generated_at": lambda data: data.update(generated_at="2026-08-11T12:00:00"),
            "source_dir_parent_segment": lambda data: data.update(source_dir="/tmp/../etc"),
            "source_dir_dot_segment": lambda data: data.update(source_dir="/tmp/./source"),
            "source_dir_double_separator": lambda data: data.update(source_dir="/tmp//source"),
            "source_dir_control": lambda data: data.update(source_dir="/tmp/source\nleak"),
            "hosts_order": lambda data: data.update(hosts=list(reversed(data["hosts"]))),
            "installed_count": lambda data: data["stats"].update(installed=7),
            "loose_count": lambda data: data["stats"].update(loose_docs=7),
            "source_escape": lambda data: data["items"][0].update(src="../escape"),
            "inner_absolute": lambda data: data["items"][0].update(inner="/absolute/SKILL.md"),
            "copy_escape": lambda data: data["items"][0].update(copy_srcs=["../../escape"]),
            "loose_docs_type": lambda data: data.update(loose_docs="not-an-array"),
            "repos_type": lambda data: data.update(repos={"not": "an-array"}),
            "loose_doc_negative_bytes": lambda data: data["loose_docs"].append({
                "name": "note", "heading": "", "src": "note.md", "bytes": -1,
                "desc": "note", "zh": False,
            }),
            "repo_negative_skills": lambda data: data["repos"].append({
                "name": "repo", "src": "repo", "skills_inside": -1,
            }),
            "missing_top_level": lambda data: data.pop("hosts"),
            "extra_top_level": lambda data: data.update(unexpected=True),
        }
        for label, mutate in mutations.items():
            with self.subTest(contract=label):
                invalid = copy.deepcopy(payload)
                mutate(invalid)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = check_data.main(
                        payload=invalid,
                        require_data_js=False,
                        quiet=True,
                    )
                self.assertEqual(result, 1)
                self.assertEqual(output.getvalue(), "")

    def test_zip_rejects_absolute_parent_and_windows_escape_paths(self) -> None:
        for member in ("../escape/SKILL.md", "/absolute/SKILL.md", "C:\\escape\\SKILL.md"):
            with self.subTest(member=member):
                self.write_zip({member: "# unsafe\n"})
                with self.assertRaises(inventory.ScanSafetyError):
                    inventory.collect(self.source)

    def test_zip_resource_limits_fail_closed(self) -> None:
        cases = (
            (
                "archive_bytes",
                {"skill/SKILL.md": "# skill\n"},
                {"MAX_ARCHIVE_BYTES": 1},
                zipfile.ZIP_STORED,
            ),
            (
                "entries",
                {"skill/SKILL.md": "# skill\n", "second.txt": "x"},
                {"MAX_ARCHIVE_ENTRIES": 1},
                zipfile.ZIP_STORED,
            ),
            (
                "member_bytes",
                {"skill/SKILL.md": "x" * 32},
                {"MAX_ARCHIVE_MEMBER_BYTES": 16},
                zipfile.ZIP_STORED,
            ),
            (
                "total_uncompressed",
                {"one.txt": "x" * 8, "two.txt": "y" * 8},
                {"MAX_ARCHIVE_TOTAL_UNCOMPRESSED_BYTES": 10},
                zipfile.ZIP_STORED,
            ),
            (
                "compression_ratio",
                {"skill/SKILL.md": "A" * 4_000},
                {"MAX_ARCHIVE_COMPRESSION_RATIO": 2.0},
                zipfile.ZIP_DEFLATED,
            ),
            (
                "manifest_reads",
                {"one/SKILL.md": "# one\n", "two/SKILL.md": "# two\n"},
                {"MAX_ARCHIVE_MANIFEST_READS": 1},
                zipfile.ZIP_STORED,
            ),
        )
        for label, members, patches, compression in cases:
            with self.subTest(limit=label):
                self.write_zip(members, compression=compression)
                patchers = [mock.patch.object(inventory, key, value) for key, value in patches.items()]
                for patcher in patchers:
                    patcher.start()
                try:
                    with self.assertRaises(inventory.ScanLimitError):
                        inventory.collect(self.source)
                finally:
                    for patcher in reversed(patchers):
                        patcher.stop()

    def test_zip_symlink_member_is_skipped_without_reading_its_target(self) -> None:
        archive = self.source / "links.zip"
        linked = zipfile.ZipInfo("linked/SKILL.md")
        linked.create_system = 3
        linked.external_attr = (stat.S_IFLNK | 0o777) << 16
        safe = (
            "---\nname: safe-archive\n"
            "description: safe archive has a sufficiently long description for the contract.\n"
            "---\nsafe\n"
        )
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(linked, "../../outside/SKILL.md")
            bundle.writestr("safe/SKILL.md", safe)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            items, _loose, _repos = inventory.collect(self.source)
        self.assertEqual([item["name"] for item in items], ["safe-archive"])
        self.assertFalse(any(item.get("inner") == "linked/SKILL.md" for item in items))
        self.assertTrue(
            any("跳过压缩包软链接" in str(warning.message) for warning in caught)
        )

    def test_zip_central_directory_is_bounded_before_zipfile_construction(self) -> None:
        archive = self.source / "many.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            for index in range(inventory.MAX_ARCHIVE_ENTRIES + 1):
                bundle.writestr(f"empty-{index}", b"")
        with mock.patch.object(
            inventory.zipfile,
            "ZipFile",
            side_effect=AssertionError("ZipFile parsed an oversized central directory"),
        ):
            with self.assertRaises(inventory.ScanLimitError):
                inventory.collect(self.source)

        for label, offset, value, error in (
            (
                "forged_entry_count",
                8,
                inventory.MAX_ARCHIVE_ENTRIES + 1,
                inventory.ScanLimitError,
            ),
            (
                "forged_central_size",
                12,
                inventory.MAX_ARCHIVE_CENTRAL_DIRECTORY_BYTES + 1,
                inventory.ScanLimitError,
            ),
            ("forged_zip64_sentinel", 10, 0xFFFF, inventory.ScanSafetyError),
        ):
            with self.subTest(metadata=label):
                archive = self.write_zip({"safe/SKILL.md": "# safe\n"})
                raw = bytearray(archive.read_bytes())
                eocd = raw.rfind(b"PK\x05\x06")
                self.assertGreaterEqual(eocd, 0)
                if offset in {8, 10}:
                    struct.pack_into("<H", raw, eocd + offset, value)
                    if offset == 8:
                        struct.pack_into("<H", raw, eocd + 10, value)
                else:
                    struct.pack_into("<I", raw, eocd + offset, value)
                archive.write_bytes(raw)
                with mock.patch.object(
                    inventory.zipfile,
                    "ZipFile",
                    side_effect=AssertionError("ZipFile parsed forged EOCD metadata"),
                ):
                    with self.assertRaises(error):
                        inventory.collect(self.source)

    def test_global_entry_read_and_manifest_budgets_fail_closed(self) -> None:
        self.write_skill("budget-skill", name="budget-skill", body="body " * 100)
        (self.source / "extra.txt").write_text("x", encoding="utf-8")
        budgets = (
            inventory.ScanBudget(max_entries=1, max_read_bytes=1_000_000, max_manifest_reads=10),
            inventory.ScanBudget(max_entries=100, max_read_bytes=10, max_manifest_reads=10),
            inventory.ScanBudget(max_entries=100, max_read_bytes=1_000_000, max_manifest_reads=0),
        )
        for budget in budgets:
            with self.subTest(budget=budget.__dict__):
                with self.assertRaises(inventory.ScanLimitError):
                    inventory.collect(self.source, budget=budget)

    def test_one_global_budget_covers_collection_and_installed_hosts(self) -> None:
        self.write_skill("candidate", name="candidate")
        host_root = self.base / "host"
        host_skill = host_root / "installed"
        host_skill.mkdir(parents=True)
        (host_skill / "SKILL.md").write_text(
            "---\nname: installed\ndescription: installed skill description is long enough.\n---\nbody\n",
            encoding="utf-8",
        )
        budget = inventory.ScanBudget(
            max_entries=1_000,
            max_read_bytes=1_000_000,
            max_manifest_reads=1,
        )
        with mock.patch.object(inventory, "ScanBudget", return_value=budget):
            with self.assertRaises(inventory.ScanLimitError):
                workbench.build_payload(self.source, host_roots={"codex": host_root})

    def test_directory_membership_change_during_manifest_read_fails_closed(self) -> None:
        skill = self.write_skill("moving-skill", name="moving-skill")
        original = inventory._read_regular_at
        mutated = False

        def read_then_mutate(*args, **kwargs):
            nonlocal mutated
            text = original(*args, **kwargs)
            if kwargs.get("manifest") and not mutated:
                mutated = True
                (skill / "late.py").write_text("print('late')\n", encoding="utf-8")
            return text

        with mock.patch.object(inventory, "_read_regular_at", side_effect=read_then_mutate):
            with self.assertRaisesRegex(inventory.ScanSafetyError, "扫描期间发生变化"):
                inventory.collect(self.source)

    def test_archive_change_during_scan_fails_closed(self) -> None:
        archive = self.write_zip({"skill/SKILL.md": "# stable\n"})
        original = inventory._validate_archive_entries
        mutated = False

        def validate_then_mutate(*args, **kwargs):
            nonlocal mutated
            entries = original(*args, **kwargs)
            if not mutated:
                mutated = True
                with archive.open("ab") as stream:
                    stream.write(b"changed")
            return entries

        with mock.patch.object(inventory, "_validate_archive_entries", side_effect=validate_then_mutate):
            with self.assertRaisesRegex(inventory.ScanSafetyError, "压缩包在读取期间发生变化"):
                inventory.collect(self.source)


if __name__ == "__main__":
    unittest.main()
