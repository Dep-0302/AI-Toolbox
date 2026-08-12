from __future__ import annotations

from copy import deepcopy
import hashlib
import http.client
import io
import json
import os
import shutil
import stat
import threading
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from functools import partial
from pathlib import Path
from unittest import mock

import app
import collection_scan
from toolbox_scan import build_toolbox_payload


FIXED_NOW = "2026-08-06T12:00:00Z"


def candidate_payload(
    generated_at: str = "2026-08-10T12:00:00+00:00",
    source_dir: str = "/fixture/collection",
) -> dict:
    return {
        "schema_version": 3,
        "generated_at": generated_at,
        "source_dir": source_dir,
        "stats": {
            "skills": 0,
            "groups": 0,
            "ungrouped": 0,
            "installed": 0,
            "competing": 0,
            "loose_docs": 0,
            "repos": 0,
        },
        "hosts": ["codex", "claude", "hermes", "workbuddy"],
        "guessed_fields": ["platform", "inputs", "outputs"],
        "groups": [],
        "facets": {
            "platform": [],
            "inputs": [],
            "outputs": [],
            "zh_state": [],
            "has": [],
        },
        "items": [],
        "loose_docs": [],
        "repos": [],
    }


def project_skill_payload(*, scan_status: str = "complete") -> dict:
    payload = {
        "schema_version": 1,
        "observation_boundary_ref": "project-skill-observation-boundary-v1",
        "generation_id": "a" * 16,
        "generated_at": FIXED_NOW,
        "scan_status": scan_status,
        "scan_scope": {
            "trigger": "manual",
            "execution": "foreground",
            "network": "disabled",
            "limits": {
                "max_project_candidates": 256,
                "max_entries_per_project": 1000,
                "max_depth": 5,
                "max_manifest_bytes": 262144,
                "max_frontmatter_bytes": 32768,
                "max_text_length": 4000,
                "scan_timeout_seconds": 12,
            },
        },
        "candidates": [],
        "projects": [],
        "human_associations": {
            "registry_id": "fixture-associations-v1",
            "confirmed_on": "2026-08-11",
            "items": [],
        },
        "issues": [],
    }
    if scan_status == "complete":
        payload["changes"] = {
            "status": "not_available",
            "reason": "no_prior_complete_snapshot",
            "fingerprint_basis": "projection_sha256_v1",
        }
    else:
        payload["issues"] = [
            {
                "issue_id": f"fixture-{scan_status}",
                "status": scan_status,
                "code": "fixture_incomplete",
                "message": "fixture incomplete",
            }
        ]
    return payload


def temporary_pair(path: Path, *, marker: str = "current") -> dict:
    collection = {
        "schema_version": 3,
        "generated_at": FIXED_NOW,
        "mode": "collection-observe",
        "source_root": path.as_posix(),
        "summary": {"source_count": 0, "scan_error_count": 0, "marker": marker},
        "items": [],
        "scan_errors": [],
    }
    return {
        "path": path,
        "display_path": path.as_posix(),
        "device": 41,
        "inode": 73,
        "input_signature": "a" * 64,
        "collection_snapshot": collection,
        "candidate_catalog": candidate_payload(source_dir=path.as_posix()),
    }


class WorkbenchStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        # macOS exposes /var as a symlink to /private/var.  Security tests use
        # no-follow traversal, so fixtures must use their canonical temp root.
        self.root = Path(self.temporary.name).resolve()
        self.generated = self.root / "generated"
        self.generated.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.patchers = [
            mock.patch.object(app, "ROOT", self.root),
            mock.patch.object(app, "GENERATED_DIR", self.generated),
            mock.patch.object(app, "SNAPSHOT_PATH", self.generated / "snapshot.json"),
            mock.patch.object(app, "LAST_ATTEMPT_PATH", self.generated / "last-attempt.json"),
            mock.patch.object(app, "LOCK_PATH", self.generated / ".refresh.lock"),
            mock.patch.object(
                app, "COLLECTION_SNAPSHOT_PATH", self.generated / "collection-snapshot.json"
            ),
            mock.patch.object(
                app, "COLLECTION_LOCK_PATH", self.generated / ".collection-refresh.lock"
            ),
            mock.patch.object(
                app, "COLLECTION_SIGNATURE_PATH", self.generated / "collection-signature.json"
            ),
            mock.patch.object(
                app, "CANDIDATE_SNAPSHOT_PATH", self.generated / "candidate-catalog.json"
            ),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary.cleanup()

    def payload(self):
        payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        return app._with_generation_id(app.validate_scan_payload(payload))

    def test_validate_snapshot_rejects_actionable_binding(self) -> None:
        payload = self.payload()
        payload["items"] = [
            {
                "type": "skill",
                "trust": {"action": "locked"},
                "host_bindings": [{"action": "enabled", "effective": "unverified"}],
            }
        ]
        payload["summary"]["asset_count"] = 1
        with self.assertRaises(app.SnapshotValidationError):
            app.validate_snapshot(payload)

    def test_validate_snapshot_rejects_inconsistent_summary(self) -> None:
        payload = self.payload()
        payload["summary"]["host_binding_count"] += 1
        with self.assertRaises(app.SnapshotValidationError):
            app.validate_snapshot(payload)

    def test_validate_scan_payload_accepts_legacy_curation_without_subcategory(self) -> None:
        skill = self.home / ".codex" / "skills" / "sample-skill"
        skill.parent.mkdir(parents=True)
        shutil.copytree(Path(__file__).parent / "fixtures" / "sample_skill", skill)
        payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        self.assertEqual(len(payload["items"]), 1)
        payload["items"][0]["curation"].pop("subcategory")

        self.assertIs(app.validate_scan_payload(payload), payload)

    def test_validate_snapshot_requires_all_unique_host_type_coverage_pairs(self) -> None:
        payload = self.payload()
        coverage = payload["scan_scope"]["coverage"]
        self.assertEqual(len(coverage), 25)
        self.assertEqual(
            {(row["host_id"], row["asset_type"]) for row in coverage},
            {(host_id, asset_type) for host_id in app.HOST_IDS for asset_type in app.ASSET_TYPES},
        )

        missing = deepcopy(payload)
        missing["scan_scope"]["coverage"].pop()
        with self.assertRaises(app.SnapshotValidationError):
            app.validate_snapshot(missing)

        duplicated = deepcopy(payload)
        duplicated["scan_scope"]["coverage"][-1] = deepcopy(
            duplicated["scan_scope"]["coverage"][0]
        )
        with self.assertRaises(app.SnapshotValidationError):
            app.validate_snapshot(duplicated)

    def test_validate_snapshot_rejects_invalid_coverage_fields(self) -> None:
        payload = self.payload()
        mutations = (
            ("status", "running"),
            ("adapter_id", ""),
            ("source_refs", "not-a-list"),
            ("source_refs", ["valid", "valid"]),
            ("configured_source_count", -1),
            ("observed_source_count", True),
            ("item_count", "0"),
        )
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                invalid = deepcopy(payload)
                invalid["scan_scope"]["coverage"][0][key] = value
                with self.assertRaises(app.SnapshotValidationError):
                    app.validate_snapshot(invalid)

    def test_runtime_contract_rejects_invalid_value_types_and_dates(self) -> None:
        source = Path(__file__).parent / "fixtures" / "sample_skill"
        skill = self.home / ".codex" / "skills" / "sample-skill"
        skill.parent.mkdir(parents=True)
        shutil.copytree(source, skill)
        payload = self.payload()
        payload.pop("generation_id")
        invalid_dates = (
            "not-a-date",
            "2026-08-06 12:00:00+00:00",
            "2026-08-06T12:00:00",
        )
        for value in invalid_dates:
            with self.subTest(generated_at=value):
                candidate = deepcopy(payload)
                candidate["generated_at"] = value
                with self.assertRaises(app.SnapshotValidationError):
                    app.validate_scan_payload(candidate)

        for field, value in (("severity", 123), ("detail", {"token": "sentinel"})):
            with self.subTest(finding_field=field):
                candidate = deepcopy(payload)
                finding = {
                    "code": "shape",
                    "severity": "warning",
                    "title": "shape",
                    "detail": "shape",
                }
                finding[field] = value
                candidate["health_findings"].append(finding)
                candidate["summary"]["health_finding_count"] += 1
                with self.assertRaises(app.SnapshotValidationError):
                    app.validate_scan_payload(candidate)

        for field, value in (("review_state", {}), ("risk_level", [])):
            with self.subTest(trust_field=field):
                candidate = deepcopy(payload)
                candidate["items"][0]["trust"][field] = value
                with self.assertRaises(app.SnapshotValidationError):
                    app.validate_scan_payload(candidate)
        for field, value in (("origin_kind", "social"), ("origin_basis", "guess")):
            with self.subTest(source_field=field):
                candidate = deepcopy(payload)
                candidate["items"][0]["source"][field] = value
                with self.assertRaises(app.SnapshotValidationError):
                    app.validate_scan_payload(candidate)
        candidate = deepcopy(payload)
        candidate["items"][0]["host_bindings"][0]["presence"] = {}
        with self.assertRaises(app.SnapshotValidationError):
            app.validate_scan_payload(candidate)

    def test_runtime_contract_rejects_codex_metadata_source_traversal(self) -> None:
        payload = self.payload()
        payload.pop("generation_id")
        for asset_type, forged in (
            ("mcp", "~/.codex/plugins/cache/../../auth/.mcp.json"),
            ("sdk", "~/.codex/plugins/cache/../../auth/package.json"),
        ):
            with self.subTest(asset_type=asset_type):
                candidate = deepcopy(payload)
                row = next(
                    entry for entry in candidate["scan_scope"]["coverage"]
                    if entry["host_id"] == "codex" and entry["asset_type"] == asset_type
                )
                row["source_refs"].append(forged)
                with self.assertRaises(app.SnapshotValidationError):
                    app.validate_scan_payload(candidate)

    def test_runtime_contract_rejects_extension_item_source_escape(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "codex_observation_plugin"
        plugin = (
            self.home / ".codex" / "plugins" / "cache" / "fixture-channel"
            / "codex-observation-plugin" / "1.2.3"
        )
        shutil.copytree(fixture, plugin)
        sdk_manifest = (
            plugin / "node_modules" / "@modelcontextprotocol" / "sdk" / "package.json"
        )
        sdk_manifest.parent.mkdir(parents=True)
        shutil.copy2(plugin / "sdk-package.json", sdk_manifest)
        payload = app.validate_scan_payload(
            build_toolbox_payload(home=self.home, now=FIXED_NOW)
        )

        for asset_type in ("mcp", "sdk"):
            with self.subTest(asset_type=asset_type, field="source.ref"):
                candidate = deepcopy(payload)
                item = next(entry for entry in candidate["items"] if entry["type"] == asset_type)
                item["source"]["ref"] = "~/.codex/auth.json"
                with self.assertRaises(app.SnapshotValidationError):
                    app.validate_scan_payload(candidate)
            with self.subTest(asset_type=asset_type, field="variant.source_ref"):
                candidate = deepcopy(payload)
                item = next(entry for entry in candidate["items"] if entry["type"] == asset_type)
                item["source"]["package_variants"][0]["source_ref"] = "~/.codex/auth.json"
                with self.assertRaises(app.SnapshotValidationError):
                    app.validate_scan_payload(candidate)

    def test_empty_codex_plugin_cache_is_a_complete_zero_result(self) -> None:
        self.home.joinpath(".codex", "plugins", "cache").mkdir(parents=True)
        raw = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        validated = app.validate_scan_payload(raw)
        for asset_type in ("mcp", "sdk"):
            row = next(
                entry for entry in validated["scan_scope"]["coverage"]
                if entry["host_id"] == "codex" and entry["asset_type"] == asset_type
            )
            self.assertEqual(row["status"], "observed")
            self.assertEqual(row["observed_source_count"], 0)
            self.assertEqual(row["item_count"], 0)

        complete, persisted = app.scan_and_persist(home=self.home)
        self.assertTrue(complete)
        self.assertEqual(app.load_snapshot()["generation_id"], persisted["generation_id"])

    def test_validate_snapshot_rejects_coverage_item_count_drift(self) -> None:
        payload = self.payload()
        payload["scan_scope"]["coverage"][0]["item_count"] += 1
        with self.assertRaises(app.SnapshotValidationError):
            app.validate_snapshot(payload)

    def test_validate_snapshot_rejects_coverage_status_semantic_drift(self) -> None:
        source = Path(__file__).parent / "fixtures" / "sample_skill"
        skill = self.home / ".codex" / "skills" / "sample-skill"
        skill.parent.mkdir(parents=True)
        shutil.copytree(source, skill)
        payload = self.payload()

        forged_missing = deepcopy(payload)
        codex_skill = next(
            row
            for row in forged_missing["scan_scope"]["coverage"]
            if row["host_id"] == "codex" and row["asset_type"] == "skill"
        )
        self.assertGreater(codex_skill["item_count"], 0)
        codex_skill["status"] = "missing"
        with self.assertRaises(app.SnapshotValidationError):
            app.validate_snapshot(forged_missing)

        forged_unconnected = deepcopy(payload)
        claude_mcp = next(
            row
            for row in forged_unconnected["scan_scope"]["coverage"]
            if row["host_id"] == "claude" and row["asset_type"] == "mcp"
        )
        claude_mcp["source_refs"] = ["~/.claude/forged"]
        claude_mcp["configured_source_count"] = 1
        with self.assertRaises(app.SnapshotValidationError):
            app.validate_snapshot(forged_unconnected)

    def test_validate_snapshot_rejects_host_and_generation_identity_drift(self) -> None:
        payload = self.payload()

        forged_host = deepcopy(payload)
        forged_host["hosts"][0]["status"] = "placeholder"
        with self.assertRaises(app.SnapshotValidationError):
            app.validate_snapshot(forged_host)

        duplicated_host = deepcopy(payload)
        duplicated_host["hosts"][-1]["id"] = duplicated_host["hosts"][0]["id"]
        with self.assertRaises(app.SnapshotValidationError):
            app.validate_snapshot(duplicated_host)

        forged_generation = deepcopy(payload)
        forged_generation["generation_id"] = "0" * 16
        with self.assertRaises(app.SnapshotValidationError):
            app.validate_snapshot(forged_generation)

    def test_validate_snapshot_rejects_unknown_fields_and_missing_root_contract(self) -> None:
        unknown = self.payload()
        unknown["unexpected_raw_config"] = {"token": "must-not-persist"}
        unknown.pop("generation_id")
        unknown = app._with_generation_id(unknown)
        with self.assertRaises(app.SnapshotValidationError):
            app.validate_snapshot(unknown)

        missing_roots = self.payload()
        missing_roots["scan_scope"]["roots"] = []
        missing_roots.pop("generation_id")
        missing_roots = app._with_generation_id(missing_roots)
        with self.assertRaises(app.SnapshotValidationError):
            app.validate_snapshot(missing_roots)
        self.assertFalse(app.snapshot_is_complete(missing_roots))

    def test_validate_snapshot_rejects_forged_observed_counts_and_adapter(self) -> None:
        forged = self.payload()
        root = next(
            row for row in forged["scan_scope"]["roots"]
            if row["host_id"] == "codex" and row["asset_type"] == "skill"
        )
        root["status"] = "observed"
        coverage = next(
            row for row in forged["scan_scope"]["coverage"]
            if row["host_id"] == "codex" and row["asset_type"] == "skill"
        )
        coverage["status"] = "observed"
        coverage["observed_source_count"] = 0
        next(host for host in forged["hosts"] if host["id"] == "codex")["status"] = "observed"
        forged.pop("generation_id")
        forged = app._with_generation_id(forged)
        with self.assertRaises(app.SnapshotValidationError):
            app.validate_snapshot(forged)

        forged_adapter = self.payload()
        forged_adapter["scan_scope"]["coverage"][0]["adapter_id"] = "arbitrary-adapter"
        forged_adapter.pop("generation_id")
        forged_adapter = app._with_generation_id(forged_adapter)
        with self.assertRaises(app.SnapshotValidationError):
            app.validate_snapshot(forged_adapter)

    def test_validate_snapshot_rejects_config_body_reads_and_activation_claims(self) -> None:
        unsafe = self.payload()
        unsafe["scan_scope"]["safety"]["reads_host_config_bodies"] = True
        with self.assertRaises(app.SnapshotValidationError):
            app.validate_snapshot(unsafe)

        actionable = self.payload()
        actionable["items"] = [
            {
                "type": "cli",
                "trust": {"action": "locked"},
                "host_bindings": [
                    {
                        "host_id": "codex",
                        "action": "locked",
                        "activation": "active",
                        "effective": "unverified",
                    }
                ],
            }
        ]
        with self.assertRaises(app.SnapshotValidationError):
            app.validate_snapshot(actionable)

    def test_atomic_snapshot_round_trip(self) -> None:
        payload = self.payload()
        app.write_json_atomically(payload, app.SNAPSHOT_PATH)
        self.assertEqual(app.load_snapshot(), payload)
        self.assertFalse(app.SNAPSHOT_PATH.is_symlink())
        self.assertEqual(list(self.generated.glob("*.tmp")), [])

    def test_scan_writes_only_generated_and_preserves_fake_home(self) -> None:
        source = Path(__file__).parent / "fixtures" / "sample_skill"
        skill = self.home / ".codex" / "skills" / "sample-skill"
        skill.parent.mkdir(parents=True)
        shutil.copytree(source, skill)
        linked = self.home / ".claude" / "skills" / "sample-skill"
        linked.parent.mkdir(parents=True)
        os.symlink(skill, linked, target_is_directory=True)

        def snapshot_tree() -> list[tuple[str, str, str | None]]:
            rows: list[tuple[str, str, str | None]] = []
            for candidate in sorted(self.home.rglob("*")):
                relative = candidate.relative_to(self.home).as_posix()
                if candidate.is_symlink():
                    rows.append((relative, "symlink", os.readlink(candidate)))
                elif candidate.is_dir():
                    rows.append((relative, "dir", None))
                else:
                    rows.append(
                        (relative, "file", hashlib.sha256(candidate.read_bytes()).hexdigest())
                    )
            return rows

        before = snapshot_tree()
        complete, payload = app.scan_and_persist(home=self.home)
        after = snapshot_tree()
        self.assertTrue(complete)
        self.assertEqual(before, after)
        self.assertEqual(app.load_snapshot()["generation_id"], payload["generation_id"])
        self.assertEqual(
            {path.name for path in self.generated.iterdir()},
            {".refresh.lock", "snapshot.json", "last-attempt.json"},
        )

    def test_candidate_scan_writes_only_generated_and_preserves_legacy_files(self) -> None:
        legacy_dir = self.root / "collection-workbench"
        legacy_dir.mkdir()
        legacy_json = legacy_dir / "data.json"
        legacy_js = legacy_dir / "data.js"
        legacy_json.write_text('{"legacy":true}\n', encoding="utf-8")
        legacy_js.write_text("window.__WORKBENCH_DATA__ = {};\n", encoding="utf-8")
        before = (legacy_json.read_bytes(), legacy_js.read_bytes())
        payload = candidate_payload()

        with mock.patch.object(app, "build_candidate_payload", return_value=payload):
            result = app.scan_candidate_and_persist()

        self.assertEqual(result, payload)
        self.assertEqual(app.load_candidate_snapshot(), payload)
        self.assertEqual((legacy_json.read_bytes(), legacy_js.read_bytes()), before)
        self.assertTrue((self.generated / "candidate-catalog.json").is_file())

    def test_candidate_scan_failure_preserves_previous_snapshot(self) -> None:
        previous = candidate_payload("2026-08-10T11:59:00+00:00")
        app.write_json_atomically(previous, app.CANDIDATE_SNAPSHOT_PATH)
        before = app.CANDIDATE_SNAPSHOT_PATH.read_bytes()

        with mock.patch.object(
            app,
            "build_candidate_payload",
            side_effect=app.SnapshotValidationError("invalid candidate payload"),
        ):
            with self.assertRaises(app.SnapshotValidationError):
                app.scan_candidate_and_persist()

        self.assertEqual(app.CANDIDATE_SNAPSHOT_PATH.read_bytes(), before)

    def test_candidate_catalog_uses_newest_valid_source_and_falls_back(self) -> None:
        legacy_dir = self.root / "collection-workbench"
        legacy_dir.mkdir()
        legacy_path = legacy_dir / "data.json"
        generated = candidate_payload("2026-08-10T12:00:00+00:00")
        legacy = candidate_payload("2026-08-10T12:01:00+00:00")
        app.write_json_atomically(generated, app.CANDIDATE_SNAPSHOT_PATH)
        legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
        os.utime(app.CANDIDATE_SNAPSHOT_PATH, ns=(1_000_000_000, 1_000_000_000))
        os.utime(legacy_path, ns=(2_000_000_000, 2_000_000_000))

        with mock.patch.object(app, "CANDIDATES_DIR", legacy_dir):
            self.assertEqual(app.load_candidate_catalog(), legacy)
            legacy_path.write_text("not-json", encoding="utf-8")
            self.assertEqual(app.load_candidate_catalog(), generated)

    def test_collection_scan_has_separate_snapshot_and_preserves_source(self) -> None:
        collection = self.root / "collection"
        skill = collection / "sample" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "---\nname: sample\ndescription: fixture collection skill\n---\n",
            encoding="utf-8",
        )
        before = hashlib.sha256(skill.read_bytes()).hexdigest()
        host_roots = {
            host: self.home / f".{host}" / "skills"
            for host in ("codex", "claude", "hermes", "workbuddy")
        }

        payload = app.scan_collection_and_persist(
            source_root=collection,
            host_roots=host_roots,
        )

        self.assertEqual(payload["summary"]["source_count"], 1)
        self.assertEqual(app.load_collection_snapshot(), payload)
        self.assertEqual(hashlib.sha256(skill.read_bytes()).hexdigest(), before)
        self.assertTrue((self.generated / "collection-snapshot.json").is_file())
        self.assertTrue((self.generated / "collection-signature.json").is_file())
        self.assertFalse(app.SNAPSHOT_PATH.exists())

    def test_collection_check_reuses_matching_snapshot_without_scanning(self) -> None:
        collection = self.root / "collection-check"
        skill = collection / "sample" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "---\nname: sample\ndescription: fixture collection skill\n---\n",
            encoding="utf-8",
        )
        host_roots = {
            host: self.home / f".{host}" / "skills"
            for host in ("codex", "claude", "hermes", "workbuddy")
        }
        payload = app.validate_collection_payload(
            app.build_collection_payload(source_root=collection, host_roots=host_roots, now=FIXED_NOW)
        )
        signature = "a" * 64
        app.write_json_atomically(payload, app.COLLECTION_SNAPSHOT_PATH)
        app.write_json_atomically(
            {
                "schema_version": app.COLLECTION_SIGNATURE_SCHEMA_VERSION,
                "input_signature": signature,
                "snapshot_fingerprint": app._collection_snapshot_fingerprint(payload),
                "generated_at": payload["generated_at"],
            },
            app.COLLECTION_SIGNATURE_PATH,
        )
        before_mtime = app.COLLECTION_SNAPSHOT_PATH.stat().st_mtime_ns

        with mock.patch.object(app, "DEFAULT_SOURCE_ROOT", collection), mock.patch.object(
            app,
            "build_collection_input_state",
            return_value={"status": "observed", "signature": signature},
        ), mock.patch.object(app, "build_collection_payload") as build:
            result = app.check_collection_and_persist()

        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["snapshot"], payload)
        self.assertEqual(app.COLLECTION_SNAPSHOT_PATH.stat().st_mtime_ns, before_mtime)
        build.assert_not_called()

    def test_collection_check_refreshes_changed_inputs_and_updates_sidecar(self) -> None:
        collection = self.root / "collection-changed"
        skill = collection / "sample" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "---\nname: sample\ndescription: changed fixture\n---\n",
            encoding="utf-8",
        )
        host_roots = {
            host: self.home / f".{host}" / "skills"
            for host in ("codex", "claude", "hermes", "workbuddy")
        }
        payload = app.validate_collection_payload(
            app.build_collection_payload(source_root=collection, host_roots=host_roots, now=FIXED_NOW)
        )
        state = {"status": "observed", "signature": "b" * 64}

        with mock.patch.object(app, "DEFAULT_SOURCE_ROOT", collection), mock.patch.object(
            app, "build_collection_input_state", return_value=state
        ), mock.patch.object(app, "build_collection_payload", return_value=payload) as build:
            result = app.check_collection_and_persist()

        self.assertEqual(result["status"], "refreshed")
        self.assertTrue(result["stable"])
        build.assert_called_once_with(source_root=None, host_roots=None)
        receipt = app.load_collection_signature()
        self.assertEqual(receipt["input_signature"], state["signature"])
        self.assertEqual(
            receipt["snapshot_fingerprint"],
            app._collection_snapshot_fingerprint(app.load_collection_snapshot()),
        )

    def test_collection_check_corrupt_sidecar_fails_safe_to_refresh(self) -> None:
        collection = self.root / "collection-corrupt"
        skill = collection / "sample" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "---\nname: sample\ndescription: corrupt fixture\n---\n",
            encoding="utf-8",
        )
        host_roots = {
            host: self.home / f".{host}" / "skills"
            for host in ("codex", "claude", "hermes", "workbuddy")
        }
        payload = app.validate_collection_payload(
            app.build_collection_payload(source_root=collection, host_roots=host_roots, now=FIXED_NOW)
        )
        app.write_json_atomically(payload, app.COLLECTION_SNAPSHOT_PATH)
        app.COLLECTION_SIGNATURE_PATH.write_text("not-json", encoding="utf-8")
        state = {"status": "observed", "signature": "c" * 64}

        with mock.patch.object(app, "DEFAULT_SOURCE_ROOT", collection), mock.patch.object(
            app, "build_collection_input_state", return_value=state
        ), mock.patch.object(app, "build_collection_payload", return_value=payload) as build:
            result = app.check_collection_and_persist()

        self.assertEqual(result["status"], "refreshed")
        build.assert_called_once()

    def test_collection_scan_errors_preserve_last_known_good_snapshot(self) -> None:
        source = self.root / "collection-last-known-good"
        skill = source / "sample" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "---\nname: sample\ndescription: stable fixture\n---\n",
            encoding="utf-8",
        )
        host_roots = {
            host: self.home / f".{host}" / "skills"
            for host in ("codex", "claude", "hermes", "workbuddy")
        }
        previous = app.validate_collection_payload(
            app.build_collection_payload(
                source_root=source,
                host_roots=host_roots,
                now=FIXED_NOW,
            )
        )
        app.write_json_atomically(previous, app.COLLECTION_SNAPSHOT_PATH)
        before = app.COLLECTION_SNAPSHOT_PATH.read_bytes()
        incomplete = app.validate_collection_payload(
            app.build_collection_payload(
                source_root=self.root / "missing-collection",
                host_roots=host_roots,
                now=FIXED_NOW,
            )
        )
        observed = {"status": "observed", "signature": "d" * 64}

        with mock.patch.object(
            app, "build_collection_input_state", return_value=observed
        ), mock.patch.object(
            app, "build_collection_payload", return_value=incomplete
        ):
            with self.assertRaises(app.CollectionScanIncomplete):
                app.scan_collection_and_persist(
                    source_root=source,
                    host_roots=host_roots,
                )

        self.assertEqual(app.COLLECTION_SNAPSHOT_PATH.read_bytes(), before)

    def test_unstable_collection_scan_preserves_last_known_good_snapshot(self) -> None:
        source = self.root / "collection-unstable"
        skill = source / "sample" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "---\nname: sample\ndescription: unstable fixture\n---\n",
            encoding="utf-8",
        )
        host_roots = {
            host: self.home / f".{host}" / "skills"
            for host in ("codex", "claude", "hermes", "workbuddy")
        }
        previous = app.validate_collection_payload(
            app.build_collection_payload(
                source_root=source,
                host_roots=host_roots,
                now=FIXED_NOW,
            )
        )
        app.write_json_atomically(previous, app.COLLECTION_SNAPSHOT_PATH)
        before = app.COLLECTION_SNAPSHOT_PATH.read_bytes()
        states = [
            {"status": "observed", "signature": "a" * 64},
            {"status": "observed", "signature": "b" * 64},
            {"status": "observed", "signature": "c" * 64},
        ]

        with mock.patch.object(
            app, "build_collection_input_state", side_effect=states
        ), mock.patch.object(
            app, "build_collection_payload", return_value=previous
        ):
            with self.assertRaises(app.CollectionScanIncomplete):
                app.scan_collection_and_persist(
                    source_root=source,
                    host_roots=host_roots,
                )

        self.assertEqual(app.COLLECTION_SNAPSHOT_PATH.read_bytes(), before)

    def test_default_source_promotes_only_symlink_warnings_and_blocks_mixed_errors(self) -> None:
        source = self.root / "default-with-symlink-warning"
        source.mkdir()
        outside = self.root / "outside-symlink-target"
        outside.mkdir()
        (outside / "DO_NOT_INDEX.md").write_text(
            "external target contents must never enter the payload",
            encoding="utf-8",
        )
        (source / "linked-outside").symlink_to(outside, target_is_directory=True)
        host_roots = {
            host: self.home / f".{host}" / "skills"
            for host in ("codex", "claude", "hermes", "workbuddy")
        }

        with mock.patch.object(app, "DEFAULT_SOURCE_ROOT", source), mock.patch.object(
            collection_scan, "DEFAULT_SOURCE_ROOT", source
        ):
            promoted = app.scan_collection_and_persist(host_roots=host_roots)

        self.assertEqual(
            promoted["scan_errors"],
            [{"code": "symlink_skipped", "path": "linked-outside"}],
        )
        self.assertEqual(promoted["summary"]["scan_error_count"], 1)
        serialized = json.dumps(promoted, ensure_ascii=False)
        self.assertNotIn("DO_NOT_INDEX.md", serialized)
        self.assertNotIn("external target contents", serialized)
        before = app.COLLECTION_SNAPSHOT_PATH.read_bytes()

        oversized = source / "broken-skill" / "SKILL.md"
        oversized.parent.mkdir()
        oversized.write_text("x" * (collection_scan.MAX_MANIFEST_BYTES + 1), encoding="utf-8")
        with mock.patch.object(app, "DEFAULT_SOURCE_ROOT", source), mock.patch.object(
            collection_scan, "DEFAULT_SOURCE_ROOT", source
        ):
            with self.assertRaises(app.CollectionScanIncomplete) as raised:
                app.scan_collection_and_persist(host_roots=host_roots)

        codes = {error["code"] for error in raised.exception.payload["scan_errors"]}
        self.assertIn("symlink_skipped", codes)
        self.assertTrue(codes - {"symlink_skipped"})
        self.assertEqual(app.COLLECTION_SNAPSHOT_PATH.read_bytes(), before)

    def test_selected_source_pair_passes_exact_same_root_to_both_builders(self) -> None:
        with tempfile.TemporaryDirectory() as selected_temp:
            source = Path(selected_temp).resolve()
            collection = {
                "source_root": source.as_posix(),
                "summary": {"scan_error_count": 0},
                "scan_errors": [],
            }
            candidate = candidate_payload(source_dir=source.as_posix())
            state = {"status": "observed", "signature": "e" * 64}
            with mock.patch.object(
                app, "build_collection_input_state", return_value=state
            ) as input_state, mock.patch.object(
                app, "build_collection_payload", return_value=collection
            ) as collection_build, mock.patch.object(
                app, "validate_collection_payload", side_effect=lambda payload: payload
            ), mock.patch.object(
                app, "build_candidate_payload", return_value=candidate
            ) as candidate_build:
                pair = app.build_in_memory_source_pair(source)

        self.assertEqual(pair["path"], source)
        collection_build.assert_called_once_with(source_root=source)
        candidate_build.assert_called_once_with(source_root=source)
        self.assertEqual(input_state.call_count, 2)
        for call in input_state.call_args_list:
            self.assertEqual(call.kwargs["source_root"], source)

    def test_selected_source_rejects_broad_protected_and_symlink_ancestor_roots(self) -> None:
        for unsafe in (
            Path("/"),
            Path.home(),
            Path.home() / "Desktop",
            self.root,
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(app.FolderSelectionError):
                app.validate_selected_source_root(str(unsafe))

        with tempfile.TemporaryDirectory() as selected_temp:
            parent = Path(selected_temp).resolve()
            real = parent / "real"
            child = real / "child"
            child.mkdir(parents=True)
            linked = parent / "linked"
            linked.symlink_to(real, target_is_directory=True)
            with self.assertRaises(app.FolderSelectionError) as raised:
                app.validate_selected_source_root(str(linked / "child"))
        self.assertEqual(raised.exception.reason, "folder_path_unsafe_or_unreadable")

    def test_selected_source_rejects_sensitive_descendants_but_allows_dedicated_common_subfolders(self) -> None:
        home = Path.home()
        sensitive = (
            home / ".ssh",
            home / ".gnupg" / "private-keys-v1.d",
            home / ".aws",
            home / ".kube" / "cache",
            home / ".docker",
            home / ".config",
            home / ".local" / "share",
            home / "Library" / "Keychains",
            home / "Library" / "Application Support" / "Example",
            home / "Library" / "Containers" / "Example",
            home / "Library" / "Group Containers" / "Example",
        )
        for unsafe in sensitive:
            with self.subTest(unsafe=unsafe), self.assertRaises(
                app.FolderSelectionError
            ) as raised:
                app.validate_selected_source_root(str(unsafe))
            self.assertEqual(raised.exception.reason, "folder_root_sensitive")

        with mock.patch.object(
            app,
            "_directory_identity_without_symlinks",
            return_value=(12, 34),
        ):
            for allowed in (
                home / "Desktop" / "Dedicated AI Collection",
                home / "Documents" / "Dedicated AI Collection",
            ):
                with self.subTest(allowed=allowed):
                    selected = app.validate_selected_source_root(str(allowed))
                    self.assertEqual(selected["path"], allowed)

    def test_selected_project_preview_is_one_in_memory_project_without_candidate_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as observation_temp:
            observation_root = Path(observation_temp).resolve()
            selected = observation_root / "009-selected-project"
            skill = selected / ".agents" / "skills" / "sample-skill"
            skill.mkdir(parents=True)
            manifest = skill / "SKILL.md"
            manifest.write_text(
                "---\nname: sample-skill\ndescription: safe selected project skill\n---\nBODY_SECRET\n",
                encoding="utf-8",
            )
            before = sorted(path.relative_to(selected).as_posix() for path in selected.rglob("*"))
            with mock.patch.object(app, "PROJECT_SKILL_PRODUCTION_ROOT", observation_root):
                identity = app._directory_identity_without_symlinks(selected)
                payload = app.build_in_memory_project_skill_preview(
                    selected,
                    expected_identity=identity,
                )
            after = sorted(path.relative_to(selected).as_posix() for path in selected.rglob("*"))

        self.assertEqual(payload["scan_status"], "complete")
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(len(payload["projects"]), 1)
        self.assertEqual(payload["projects"][0]["relative_path"], "009-selected-project")
        self.assertEqual(len(payload["projects"][0]["logical_skills"]), 1)
        self.assertEqual(payload["human_associations"]["items"], [])
        self.assertNotIn(str(observation_root), json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("BODY_SECRET", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(before, after)

    def test_selected_project_root_must_stay_inside_frozen_observation_root(self) -> None:
        with tempfile.TemporaryDirectory() as observation_temp, tempfile.TemporaryDirectory() as outside_temp:
            observation_root = Path(observation_temp).resolve()
            inside = observation_root / "project"
            outside = Path(outside_temp).resolve() / "outside"
            inside.mkdir()
            outside.mkdir()
            with mock.patch.object(app, "PROJECT_SKILL_PRODUCTION_ROOT", observation_root):
                selected = app.validate_selected_project_root(str(inside))
                self.assertEqual(selected["relative_path"], "project")
                with self.assertRaises(app.FolderSelectionError) as raised:
                    app.validate_selected_project_root(str(outside))
        self.assertEqual(raised.exception.reason, "project_folder_outside_observation_root")

    def test_concurrent_candidate_builds_are_serialized_and_keep_roots_isolated(self) -> None:
        class Builder:
            def __init__(self) -> None:
                self.active = 0
                self.maximum_active = 0
                self.seen: list[Path] = []
                self.guard = threading.Lock()

            def build_payload(self, *, source_root: Path) -> dict:
                with self.guard:
                    self.active += 1
                    self.maximum_active = max(self.maximum_active, self.active)
                    self.seen.append(source_root)
                app.time.sleep(0.02)
                try:
                    return candidate_payload(source_dir=source_root.as_posix())
                finally:
                    with self.guard:
                        self.active -= 1

        builder = Builder()
        roots = {
            "left": Path("/safe/left-root"),
            "right": Path("/safe/right-root"),
        }
        barrier = threading.Barrier(2)
        results: dict[str, dict] = {}
        failures: list[BaseException] = []

        def run(name: str) -> None:
            try:
                barrier.wait(timeout=2)
                results[name] = app.build_candidate_payload(roots[name])
            except BaseException as exc:  # pragma: no cover - assertion reports details
                failures.append(exc)

        with mock.patch.object(app, "_load_project_module", return_value=builder), mock.patch.object(
            app, "validate_candidate_payload", side_effect=lambda payload: payload
        ):
            threads = [threading.Thread(target=run, args=(name,)) for name in roots]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)

        self.assertFalse(failures)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(builder.maximum_active, 1)
        self.assertEqual(set(builder.seen), set(roots.values()))
        self.assertEqual(
            {name: payload["source_dir"] for name, payload in results.items()},
            {name: root.as_posix() for name, root in roots.items()},
        )

    def test_native_picker_uses_fixed_same_python_command_without_shell(self) -> None:
        completed = mock.Mock(
            returncode=0,
            stdout=json.dumps(
                {"selected": True, "cancelled": False, "path": "/safe/source"}
            ),
        )
        with mock.patch.object(
            app,
            "native_folder_picker_status",
            return_value={"available": True, "provider": "tkinter", "reason": None},
        ), mock.patch.object(app.subprocess, "run", return_value=completed) as run:
            result = app.run_native_folder_picker()

        self.assertTrue(result["selected"])
        self.assertEqual(
            run.call_args.args[0],
            [app.sys.executable, str(app.NATIVE_FOLDER_PICKER_PATH)],
        )
        self.assertIs(run.call_args.kwargs["shell"], False)

    def test_native_picker_status_prefers_macos_standard_additions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            osascript = Path(temp_dir) / "osascript"
            osascript.write_text("#!/bin/sh\n", encoding="utf-8")
            osascript.chmod(0o700)
            with mock.patch.object(app.sys, "platform", "darwin"), mock.patch.object(
                app, "MACOS_OSASCRIPT_PATH", osascript
            ):
                status = app.native_folder_picker_status()

        self.assertEqual(
            status,
            {
                "available": True,
                "provider": "macos-standard-additions",
                "reason": None,
            },
        )

    def test_collections_cli_returns_non_success_for_incomplete_scan(self) -> None:
        incomplete = {
            "generated_at": FIXED_NOW,
            "summary": {"scan_error_count": 1},
            "scan_errors": [{"code": "source_root_missing"}],
        }
        output = io.StringIO()
        with mock.patch.object(app.sys, "argv", ["app.py", "collections-scan"]), mock.patch.object(
            app,
            "scan_collection_and_persist",
            side_effect=app.CollectionScanIncomplete(incomplete, "collection_incomplete"),
        ), mock.patch("sys.stdout", output):
            result = app._main()

        self.assertEqual(result, 2)
        self.assertFalse(json.loads(output.getvalue())["complete"])

    def test_cross_process_refresh_guard_fails_closed(self) -> None:
        with app.refresh_guard():
            with self.assertRaises(app.RefreshBusy):
                with app.refresh_guard():
                    pass

    def test_incomplete_snapshot_is_not_promoted(self) -> None:
        previous = self.payload()
        app.write_json_atomically(previous, app.SNAPSHOT_PATH)
        raw = app.SNAPSHOT_PATH.read_bytes()
        incomplete = self.payload()
        incomplete.pop("generation_id")
        incomplete["scan_errors"] = [{"code": "budget", "message": "fixture budget reached"}]
        incomplete["summary"]["scan_error_count"] = 1
        with mock.patch.object(app, "build_toolbox_payload", return_value=incomplete):
            complete, _ = app.scan_and_persist(home=self.home)
        self.assertFalse(complete)
        self.assertEqual(app.SNAPSHOT_PATH.read_bytes(), raw)
        attempt = json.loads(app.LAST_ATTEMPT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(attempt["status"], "incomplete")

    def test_partial_coverage_is_not_promoted(self) -> None:
        previous = self.payload()
        app.write_json_atomically(previous, app.SNAPSHOT_PATH)
        raw = app.SNAPSHOT_PATH.read_bytes()
        incomplete = self.payload()
        incomplete.pop("generation_id")
        coverage_row = incomplete["scan_scope"]["coverage"][0]
        coverage_row["status"] = "partial"
        coverage_row["observed_source_count"] = 1
        root_row = next(
            row for row in incomplete["scan_scope"]["roots"]
            if row["host_id"] == coverage_row["host_id"]
            and row["asset_type"] == coverage_row["asset_type"]
        )
        root_row["status"] = "partial"
        codex_rows = [
            row for row in incomplete["scan_scope"]["coverage"]
            if row["host_id"] == "codex"
        ]
        next(host for host in incomplete["hosts"] if host["id"] == "codex")["status"] = (
            app._host_status_from_coverage(codex_rows)
        )
        with mock.patch.object(app, "build_toolbox_payload", return_value=incomplete):
            complete, _ = app.scan_and_persist(home=self.home)
        self.assertFalse(complete)
        self.assertEqual(app.SNAPSHOT_PATH.read_bytes(), raw)
        attempt = json.loads(app.LAST_ATTEMPT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(attempt["incomplete_coverage"][0]["status"], "partial")

    def test_successful_snapshot_is_not_reported_failed_when_receipt_write_fails(self) -> None:
        app.write_json_atomically(
            {
                "status": "failed",
                "generated_at": "2026-08-05T00:00:00Z",
                "error": "old_failure",
            },
            app.LAST_ATTEMPT_PATH,
        )
        real_write = app.write_json_atomically

        def fail_receipt(payload, destination):
            if destination == app.LAST_ATTEMPT_PATH:
                raise RuntimeError("receipt unavailable")
            return real_write(payload, destination)

        with mock.patch.object(app, "write_json_atomically", side_effect=fail_receipt):
            complete, payload = app.scan_and_persist(home=self.home)

        self.assertTrue(complete)
        self.assertEqual(app.load_snapshot()["generation_id"], payload["generation_id"])
        attempt = app.load_health_attempt(payload)
        self.assertEqual(attempt["status"], "unavailable")
        self.assertEqual(attempt["reason"], "receipt_not_persisted_for_current_snapshot")

    def test_directory_fsync_failure_after_replace_does_not_claim_old_snapshot(self) -> None:
        payload = self.payload()
        calls = 0
        real_fsync = app.os.fsync

        def fail_directory_fsync(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("directory durability unavailable")
            return real_fsync(descriptor)

        with mock.patch.object(app.os, "fsync", side_effect=fail_directory_fsync):
            app.write_json_atomically(payload, app.SNAPSHOT_PATH)

        self.assertEqual(app.load_snapshot()["generation_id"], payload["generation_id"])

    def test_coverage_completion_states_are_fail_closed_only_when_incomplete(self) -> None:
        payload = self.payload()
        for status in ("partial", "error"):
            with self.subTest(status=status):
                candidate = deepcopy(payload)
                candidate["scan_scope"]["coverage"][0]["status"] = status
                candidate["hosts"][0]["status"] = status
                candidate.pop("generation_id")
                candidate = app._with_generation_id(candidate)
                self.assertFalse(app.snapshot_is_complete(candidate))
        self.assertTrue(app.snapshot_is_complete(payload))


class WorkbenchHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.generated = self.root / "generated"
        self.generated.mkdir()
        self.project_skills_generated = self.generated / "project-skills"
        self.web = self.root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<!doctype html><title>test</title>", encoding="utf-8")
        self.home = self.root / "home"
        self.home.mkdir()
        self.patchers = [
            mock.patch.object(app, "ROOT", self.root),
            mock.patch.object(app, "GENERATED_DIR", self.generated),
            mock.patch.object(app, "SNAPSHOT_PATH", self.generated / "snapshot.json"),
            mock.patch.object(app, "LAST_ATTEMPT_PATH", self.generated / "last-attempt.json"),
            mock.patch.object(app, "LOCK_PATH", self.generated / ".refresh.lock"),
            mock.patch.object(
                app, "COLLECTION_SNAPSHOT_PATH", self.generated / "collection-snapshot.json"
            ),
            mock.patch.object(
                app, "COLLECTION_LOCK_PATH", self.generated / ".collection-refresh.lock"
            ),
            mock.patch.object(
                app, "COLLECTION_SIGNATURE_PATH", self.generated / "collection-signature.json"
            ),
            mock.patch.object(
                app, "CANDIDATE_SNAPSHOT_PATH", self.generated / "candidate-catalog.json"
            ),
            mock.patch.object(
                app,
                "PROJECT_SKILL_OUTPUT_ROOT",
                self.project_skills_generated,
                create=True,
            ),
        ]
        for patcher in self.patchers:
            patcher.start()
        handler = partial(app.ToolboxHandler, directory=str(self.web))
        self.server = app.ToolboxServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary.cleanup()

    def request_raw(self, path, *, method="GET", data=None, headers=None):
        request_headers = dict(headers or {})
        host_header = request_headers.pop("Host", None)
        if host_header is not None:
            connection = http.client.HTTPConnection(
                "127.0.0.1", self.server.server_address[1], timeout=3
            )
            try:
                connection.putrequest(method, path, skip_host=True)
                connection.putheader("Host", host_header)
                for name, value in request_headers.items():
                    connection.putheader(name, value)
                connection.endheaders(data)
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()
        request = urllib.request.Request(
            self.base + path,
            method=method,
            data=data,
            headers=request_headers,
        )
        try:
            response = urllib.request.urlopen(request, timeout=3)
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()
        return response.status, dict(response.headers), response.read()

    def request_json(self, path, *, method="GET", data=None, headers=None):
        status, response_headers, raw = self.request_raw(
            path,
            method=method,
            data=data,
            headers=headers,
        )
        return status, response_headers, json.loads(raw.decode("utf-8"))

    def post_headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Origin": self.base,
            "X-AI-Toolbox-CSRF": self.server.csrf_token,
        }

    def request_wire(
        self,
        path: str,
        *,
        headers: list[tuple[str, str]],
        body: bytes = b"",
    ) -> tuple[int, dict[str, str], dict]:
        """Send exact HTTP/1.1 headers, including duplicates or no Content-Length."""

        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=3
        )
        try:
            connection.putrequest(
                "POST",
                path,
                skip_host=True,
                skip_accept_encoding=True,
            )
            connection.putheader("Host", f"127.0.0.1:{self.server.server_address[1]}")
            connection.putheader("Connection", "close")
            for name, value in headers:
                connection.putheader(name, value)
            connection.endheaders(body)
            response = connection.getresponse()
            raw = response.read()
            return (
                response.status,
                dict(response.getheaders()),
                json.loads(raw.decode("utf-8")),
            )
        finally:
            connection.close()

    def persistent_project_skill_boundary(self) -> Path:
        boundary = json.loads(app.PROJECT_SKILL_BOUNDARY_PATH.read_text(encoding="utf-8"))
        boundary["phase"] = "persistent_readonly_api"
        boundary["data_contract"]["connections"] = {
            "preview": True,
            "scanner": True,
            "api": True,
            "ui": False,
            "persistence": True,
        }
        boundary["persistence"]["current_phase_writes"] = list(
            boundary["persistence"]["future_allowed_outputs"]
        )
        path = self.root / "project-skill-boundary.json"
        path.write_text(json.dumps(boundary, ensure_ascii=False), encoding="utf-8")
        return path

    def payload(self):
        payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        return app._with_generation_id(app.validate_scan_payload(payload))

    def test_health_is_local_observe_only_and_has_security_headers(self) -> None:
        status, headers, payload = self.request_json("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["mode"], "observe-only")
        self.assertEqual(payload["release_id"], "0.2.1-public")
        self.assertFalse(payload["degraded"])
        self.assertEqual(payload["errors"], [])
        self.assertFalse(payload["capabilities"]["host_mutation"])
        self.assertFalse(payload["capabilities"]["model_calls"])
        self.assertTrue(payload["csrf_token"])
        self.assertEqual(
            set(payload["folder_picker"]),
            {"available", "provider", "reason"},
        )
        self.assertEqual(
            payload["source_session"],
            {
                "mode": "default",
                "display_path": app._display_path(app.DEFAULT_SOURCE_ROOT.absolute()),
                "source_key": "default",
            },
        )
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_health_reports_refresh_in_progress(self) -> None:
        self.server.refresh_lock.acquire()
        try:
            status, _, payload = self.request_json("/api/health")
        finally:
            self.server.refresh_lock.release()
        self.assertEqual(status, 200)
        self.assertEqual(payload["refresh_state"], "running")

    def test_health_surfaces_project_skill_auxiliary_degradation(self) -> None:
        view = {
            "snapshot": {"generated_at": FIXED_NOW, "generation_id": "truth123456789ab"},
            "last_attempt": None,
            "report": {"available": False, "generation_id": None},
            "integrity": {"degraded": True, "errors": ["report_invalid"]},
        }
        with mock.patch.object(app, "load_project_skill_api_view", return_value=view):
            status, _, payload = self.request_json("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["degraded"])
        self.assertIn("project_skill_auxiliary_invalid", payload["errors"])
        self.assertEqual(payload["project_skill_snapshot"]["integrity"], view["integrity"])

    def test_snapshot_get_does_not_trigger_scan(self) -> None:
        with mock.patch.object(app, "scan_and_persist") as scan:
            status, _, payload = self.request_json("/api/snapshot")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "snapshot_missing")
        scan.assert_not_called()

    def test_collection_get_does_not_trigger_scan(self) -> None:
        with mock.patch.object(app, "scan_collection_and_persist") as scan:
            status, _, payload = self.request_json("/api/collections")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "collection_snapshot_missing")
        scan.assert_not_called()

    def test_project_skill_get_is_storage_only_and_missing_is_404(self) -> None:
        self.assertFalse(self.project_skills_generated.exists())
        with mock.patch.object(
            app,
            "scan_project_skills_and_persist",
            side_effect=AssertionError("GET must not scan"),
            create=True,
        ), mock.patch.object(
            app,
            "build_project_skill_snapshot",
            side_effect=AssertionError("GET must not build a scan"),
            create=True,
        ):
            status, headers, payload = self.request_json("/api/project-skills")

        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "project_skill_snapshot_missing")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertFalse(self.project_skills_generated.exists())

    def test_project_skill_api_contract_mismatch_returns_503_before_build_or_store(self) -> None:
        boundary = json.loads(app.PROJECT_SKILL_BOUNDARY_PATH.read_text(encoding="utf-8"))
        boundary["phase"] = "non_persistent_preview"
        boundary["data_contract"]["connections"] = {
            "preview": True,
            "scanner": True,
            "api": False,
            "ui": False,
            "persistence": False,
        }
        boundary["persistence"]["current_phase_writes"] = []
        boundary_path = self.root / "preview-only-boundary.json"
        boundary_path.write_text(json.dumps(boundary, ensure_ascii=False), encoding="utf-8")

        self.assertFalse(self.project_skills_generated.exists())
        with mock.patch.object(
            app, "PROJECT_SKILL_BOUNDARY_PATH", boundary_path
        ), mock.patch.object(app, "build_project_skill_snapshot") as build, mock.patch.object(
            app, "ProjectSkillStore"
        ) as store:
            status, _, payload = self.request_json(
                "/api/project-skills/refresh",
                method="POST",
                data=b"{}",
                headers=self.post_headers(),
            )

        self.assertEqual(status, 503)
        self.assertEqual(payload, {"error": "project_skill_contract_unavailable"})
        build.assert_not_called()
        store.assert_not_called()
        self.assertFalse(self.project_skills_generated.exists())

    def test_project_skill_corrupt_snapshot_returns_stable_path_free_500(self) -> None:
        self.project_skills_generated.mkdir()
        snapshot = self.project_skills_generated / "snapshot.json"
        snapshot.write_text("not-json", encoding="utf-8")

        status, _, payload = self.request_json("/api/project-skills")

        self.assertEqual(status, 500)
        self.assertEqual(
            payload,
            {
                "ok": False,
                "error": "snapshot_invalid",
                "resource": "project_skill_snapshot",
            },
        )
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("snapshot.json", serialized)

    def test_project_skill_get_returns_validated_snapshot_attempt_and_report_view(self) -> None:
        snapshot = {"generation_id": "finalized1234567"}
        attempt = {
            "scan_status": "partial",
            "promoted": False,
            "complete_snapshot_generation_id": "finalized1234567",
        }
        store = mock.Mock()
        store.load_snapshot.return_value = snapshot
        store.load_last_attempt.return_value = attempt
        store.load_markdown.return_value = "# safe derived report\n"
        with mock.patch.object(app, "ProjectSkillStore", return_value=store), mock.patch.object(
            app,
            "build_project_skill_snapshot",
            side_effect=AssertionError("GET must not scan"),
        ):
            status, _, payload = self.request_json("/api/project-skills")

        self.assertEqual(status, 200)
        self.assertEqual(
            payload,
            {
                "snapshot": snapshot,
                "last_attempt": attempt,
                "report": {
                    "available": True,
                    "generation_id": "finalized1234567",
                    "content": "# safe derived report\n",
                },
                "integrity": {"degraded": False, "errors": []},
            },
        )
        store.load_snapshot.assert_called_once_with()
        store.load_last_attempt.assert_called_once_with()
        store.load_markdown.assert_called_once_with(expected_snapshot=snapshot)

    def test_project_skill_get_internal_error_is_stable_json(self) -> None:
        with mock.patch.object(
            app, "load_project_skill_api_view", side_effect=RuntimeError("/private/fixture-project")
        ):
            status, _, payload = self.request_json("/api/project-skills")
        self.assertEqual(status, 500)
        self.assertEqual(payload["error"], "snapshot_invalid")
        self.assertNotIn("/private/fixture-project", json.dumps(payload))

    def test_project_skill_get_exposes_first_incomplete_attempt_without_snapshot(self) -> None:
        attempt = {"scan_status": "partial", "promoted": False}
        store = mock.Mock()
        store.load_snapshot.return_value = None
        store.load_last_attempt.return_value = attempt
        with mock.patch.object(app, "ProjectSkillStore", return_value=store):
            status, _, payload = self.request_json("/api/project-skills")

        self.assertEqual(status, 200)
        self.assertEqual(
            payload,
            {
                "snapshot": None,
                "last_attempt": attempt,
                "report": {"available": False, "generation_id": None, "content": None},
                "integrity": {"degraded": False, "errors": []},
            },
        )
        store.load_markdown.assert_not_called()

    def test_project_skill_get_rejects_stale_attempt_from_older_snapshot(self) -> None:
        store = mock.Mock()
        store.load_snapshot.return_value = {"generation_id": "newgeneration123"}
        store.load_last_attempt.return_value = {
            "complete_snapshot_generation_id": "oldgeneration123"
        }
        store.load_markdown.return_value = "# valid current report\n"
        with mock.patch.object(app, "ProjectSkillStore", return_value=store):
            status, _, payload = self.request_json("/api/project-skills")
        self.assertEqual(status, 200)
        self.assertEqual(payload["snapshot"]["generation_id"], "newgeneration123")
        self.assertIsNone(payload["last_attempt"])
        self.assertEqual(
            payload["integrity"],
            {"degraded": True, "errors": ["last_attempt_stale"]},
        )
        store.load_markdown.assert_called_once_with(
            expected_snapshot={"generation_id": "newgeneration123"}
        )

    def test_project_skill_get_keeps_valid_lkg_when_derived_report_is_invalid(self) -> None:
        generation = "currenttruth1234"
        store = mock.Mock()
        store.load_snapshot.return_value = {"generation_id": generation}
        store.load_last_attempt.return_value = {
            "complete_snapshot_generation_id": generation
        }
        store.load_markdown.side_effect = app.ProjectSkillPersistenceError("stale")
        with mock.patch.object(app, "ProjectSkillStore", return_value=store):
            status, _, payload = self.request_json("/api/project-skills")
        self.assertEqual(status, 200)
        self.assertEqual(payload["snapshot"]["generation_id"], generation)
        self.assertEqual(
            payload["report"],
            {"available": False, "generation_id": None, "content": None},
        )
        self.assertEqual(
            payload["integrity"], {"degraded": True, "errors": ["report_invalid"]}
        )

    def test_project_skill_get_marks_missing_attempt_beside_valid_snapshot(self) -> None:
        generation = "currenttruth1234"
        store = mock.Mock()
        store.load_snapshot.return_value = {"generation_id": generation}
        store.load_last_attempt.return_value = None
        store.load_markdown.return_value = "# current\n"
        with mock.patch.object(app, "ProjectSkillStore", return_value=store):
            status, _, payload = self.request_json("/api/project-skills")
        self.assertEqual(status, 200)
        self.assertEqual(payload["snapshot"]["generation_id"], generation)
        self.assertEqual(
            payload["integrity"],
            {"degraded": True, "errors": ["last_attempt_missing"]},
        )

    def test_project_skill_refresh_enforces_local_same_origin_json_csrf_and_size(self) -> None:
        cases = (
            (
                "unsafe-host",
                {"Host": "attacker.example", **self.post_headers()},
                b"{}",
                403,
                "unsafe_host",
            ),
            (
                "missing-origin",
                {
                    "Content-Type": "application/json",
                    "X-AI-Toolbox-CSRF": self.server.csrf_token,
                },
                b"{}",
                403,
                "unsafe_origin",
            ),
            (
                "cross-origin",
                {**self.post_headers(), "Origin": "http://attacker.example"},
                b"{}",
                403,
                "unsafe_origin",
            ),
            (
                "wrong-content-type",
                {**self.post_headers(), "Content-Type": "text/plain"},
                b"{}",
                415,
                "content_type_required",
            ),
            (
                "missing-csrf",
                {"Content-Type": "application/json", "Origin": self.base},
                b"{}",
                403,
                "invalid_csrf",
            ),
            (
                "non-empty-object",
                self.post_headers(),
                b'{"path":"/private/fixture-project"}',
                400,
                "unexpected_fields",
            ),
            (
                "non-object-json",
                self.post_headers(),
                b"[]",
                400,
                "unexpected_fields",
            ),
            (
                "too-large",
                self.post_headers(),
                b"x" * (app.MAX_REQUEST_BYTES + 1),
                413,
                "request_too_large",
            ),
        )
        with mock.patch.object(
            app,
            "scan_project_skills_and_persist",
            side_effect=AssertionError("rejected request must not scan"),
            create=True,
        ):
            for label, headers, body, expected_status, expected_error in cases:
                with self.subTest(label=label):
                    status, _, payload = self.request_json(
                        "/api/project-skills/refresh",
                        method="POST",
                        data=body,
                        headers=headers,
                    )
                    self.assertEqual(status, expected_status)
                    self.assertEqual(payload["error"], expected_error)

    def test_project_skill_refresh_method_and_registry_routes_are_closed(self) -> None:
        status, _, payload = self.request_json("/api/project-skills/refresh")
        self.assertEqual(status, 405)
        self.assertEqual(payload["error"], "method_not_allowed")

        for method, path, body, headers in (
            ("GET", "/api/project-skills/registry", None, None),
            (
                "POST",
                "/api/project-skills/registry",
                b"{}",
                self.post_headers(),
            ),
            (
                "POST",
                "/api/project-skills/refresh/001",
                b"{}",
                self.post_headers(),
            ),
        ):
            with self.subTest(method=method, path=path):
                status, _, payload = self.request_json(
                    path,
                    method=method,
                    data=body,
                    headers=headers,
                )
                self.assertEqual(status, 404)
                self.assertEqual(payload["error"], "not_found")

        for path in ("/api/project-skills", "/api/project-skills/refresh"):
            with self.subTest(method="HEAD", path=path):
                status, _, raw = self.request_raw(path, method="HEAD")
                self.assertEqual(status, 405)
                self.assertEqual(raw, b"")

    def test_project_skill_refresh_rejects_ambiguous_http_framing_without_scan(self) -> None:
        base_headers = [
            ("Content-Type", "application/json"),
            ("Origin", self.base),
            ("X-AI-Toolbox-CSRF", self.server.csrf_token),
        ]
        cases = (
            (
                "transfer-encoding",
                [*base_headers, ("Transfer-Encoding", "chunked"), ("Content-Length", "2")],
                b"{}",
                400,
                "unsupported_transfer_encoding",
            ),
            (
                "missing-content-length",
                base_headers,
                b"",
                411,
                "content_length_required",
            ),
            (
                "duplicate-content-length",
                [*base_headers, ("Content-Length", "2"), ("Content-Length", "2")],
                b"{}",
                400,
                "invalid_content_length",
            ),
        )
        with mock.patch.object(app, "scan_project_skills_and_persist") as scan:
            for label, headers, body, expected_status, expected_error in cases:
                with self.subTest(label=label):
                    status, _, payload = self.request_wire(
                        "/api/project-skills/refresh",
                        headers=headers,
                        body=body,
                    )
                    self.assertEqual(status, expected_status)
                    self.assertEqual(payload["error"], expected_error)
        scan.assert_not_called()

    def test_project_skill_refresh_uses_dedicated_process_lock(self) -> None:
        lock = self.server.project_skill_refresh_lock
        lock.acquire()
        try:
            with mock.patch.object(
                app,
                "scan_project_skills_and_persist",
                side_effect=AssertionError("conflict must not scan"),
                create=True,
            ):
                status, _, payload = self.request_json(
                    "/api/project-skills/refresh",
                    method="POST",
                    data=b"{}",
                    headers=self.post_headers(),
                )
        finally:
            lock.release()

        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "project_skill_refresh_in_progress")

    def test_project_skill_refresh_does_not_wait_for_collection_source_lock(self) -> None:
        completed = project_skill_payload()
        self.server.source_state_lock.acquire()
        try:
            with mock.patch.object(
                app, "scan_project_skills_and_persist", return_value=(True, completed)
            ) as scan:
                status, _, payload = self.request_json(
                    "/api/project-skills/refresh",
                    method="POST",
                    data=b"{}",
                    headers=self.post_headers(),
                )
        finally:
            self.server.source_state_lock.release()
        self.assertEqual(status, 200)
        self.assertEqual(payload["generation_id"], completed["generation_id"])
        scan.assert_called_once_with()

    def test_project_skill_generated_outputs_are_never_static_routes(self) -> None:
        from urllib.parse import quote

        for name in ("snapshot.json", "last-attempt.json", "项目Skill总览.md"):
            with self.subTest(name=name):
                status, _, raw = self.request_raw(
                    f"/generated/project-skills/{quote(name)}"
                )
                self.assertEqual(status, 404)
                self.assertNotIn(b"generation_id", raw)

    def test_project_skill_complete_refresh_is_isolated_from_existing_scans(self) -> None:
        completed = project_skill_payload()
        with mock.patch.object(
            app,
            "scan_project_skills_and_persist",
            return_value=(True, completed),
            create=True,
        ) as project_scan, mock.patch.object(app, "scan_and_persist") as host_scan, mock.patch.object(
            app, "scan_collection_and_persist"
        ) as collection_scan, mock.patch.object(
            app, "scan_candidate_and_persist"
        ) as candidate_scan:
            status, _, payload = self.request_json(
                "/api/project-skills/refresh",
                method="POST",
                data=b"{}",
                headers=self.post_headers(),
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload["generation_id"], completed["generation_id"])
        project_scan.assert_called_once_with()
        host_scan.assert_not_called()
        collection_scan.assert_not_called()
        candidate_scan.assert_not_called()

    def test_project_skill_success_returns_store_finalized_snapshot(self) -> None:
        boundary_path = self.persistent_project_skill_boundary()
        scanner_payload = project_skill_payload()
        scanner_payload["generation_id"] = "1" * 16
        scanner_payload["changes"] = {
            "status": "not_available",
            "reason": "no_prior_complete_snapshot",
            "fingerprint_basis": "projection_sha256_v1",
        }
        persisted = deepcopy(scanner_payload)
        persisted["generation_id"] = "2" * 16
        persisted["changes"] = {
            "status": "compared",
            "compared_to_generation_id": "3" * 16,
            "fingerprint_basis": "projection_sha256_v1",
            "added": [],
            "changed": [],
            "removed": [],
        }
        store = mock.Mock()
        store.last_receipt_error = None
        store.persist_scan_result.return_value = True
        store.load_snapshot.return_value = persisted

        with mock.patch.object(
            app, "PROJECT_SKILL_BOUNDARY_PATH", boundary_path
        ), mock.patch.object(
            app, "build_project_skill_snapshot", return_value=scanner_payload
        ) as build, mock.patch.object(
            app, "ProjectSkillStore", return_value=store
        ) as store_type:
            status, _, payload = self.request_json(
                "/api/project-skills/refresh",
                method="POST",
                data=b"{}",
                headers=self.post_headers(),
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload, persisted)
        self.assertNotEqual(payload["generation_id"], scanner_payload["generation_id"])
        build.assert_called_once_with()
        store_type.assert_called_once_with(app.PROJECT_SKILL_OUTPUT_ROOT.parents[1])
        store.persist_scan_result.assert_called_once_with(scanner_payload)
        store.load_snapshot.assert_called_once_with()

    def test_project_skill_partial_and_error_refresh_return_422_and_preserve_lkg(self) -> None:
        self.project_skills_generated.mkdir()
        snapshot_path = self.project_skills_generated / "snapshot.json"
        lkg = b'{"generation_id":"preserved-lkg"}\n'
        snapshot_path.write_bytes(lkg)

        for scan_status in ("partial", "error"):
            with self.subTest(scan_status=scan_status):
                incomplete = project_skill_payload(scan_status=scan_status)
                with mock.patch.object(
                    app,
                    "scan_project_skills_and_persist",
                    return_value=(False, incomplete),
                    create=True,
                ) as project_scan:
                    status, _, payload = self.request_json(
                        "/api/project-skills/refresh",
                        method="POST",
                        data=b"{}",
                        headers=self.post_headers(),
                    )

                self.assertEqual(status, 422)
                self.assertEqual(payload["error"], "project_skill_scan_incomplete")
                self.assertEqual(payload["scan_status"], scan_status)
                self.assertNotIn(str(self.root), json.dumps(payload, ensure_ascii=False))
                self.assertEqual(snapshot_path.read_bytes(), lkg)
                project_scan.assert_called_once_with()

    def test_refresh_rejects_missing_origin_and_csrf(self) -> None:
        status, _, payload = self.request_json(
            "/api/refresh",
            method="POST",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "unsafe_origin")

    def test_refresh_accepts_only_empty_same_origin_request(self) -> None:
        payload = self.payload()
        headers = {
            "Content-Type": "application/json",
            "Origin": self.base,
            "X-AI-Toolbox-CSRF": self.server.csrf_token,
        }
        with mock.patch.object(app, "scan_and_persist", return_value=(True, payload)) as scan:
            status, _, response = self.request_json(
                "/api/refresh", method="POST", data=b"{}", headers=headers
            )
        self.assertEqual(status, 200)
        self.assertEqual(response["generation_id"], payload["generation_id"])
        scan.assert_called_once_with()

    def test_collection_refresh_uses_separate_read_only_scan(self) -> None:
        payload = {
            "schema_version": 2,
            "generated_at": FIXED_NOW,
            "mode": "collection-observe",
            "source_root": "/fixture/collection",
            "summary": {"source_count": 0},
            "items": [],
            "scan_errors": [],
        }
        headers = {
            "Content-Type": "application/json",
            "Origin": self.base,
            "X-AI-Toolbox-CSRF": self.server.csrf_token,
        }
        with mock.patch.object(
            app, "scan_collection_and_persist", return_value=payload
        ) as scan:
            status, _, response = self.request_json(
                "/api/collections/refresh", method="POST", data=b"{}", headers=headers
            )
        self.assertEqual(status, 200)
        self.assertEqual(response["mode"], "collection-observe")
        scan.assert_called_once_with()

    def test_collection_check_uses_csrf_protected_change_probe(self) -> None:
        snapshot = {
            "schema_version": 3,
            "generated_at": FIXED_NOW,
            "mode": "collection-observe",
            "source_root": "/fixture/collection",
            "summary": {"source_count": 0},
            "items": [],
            "scan_errors": [],
        }
        envelope = {
            "status": "unchanged",
            "reason": "input_signature_match",
            "stable": True,
            "snapshot": snapshot,
        }
        headers = {
            "Content-Type": "application/json",
            "Origin": self.base,
            "X-AI-Toolbox-CSRF": self.server.csrf_token,
        }
        with mock.patch.object(
            app, "check_collection_and_persist", return_value=envelope
        ) as check:
            status, _, response = self.request_json(
                "/api/collections/check", method="POST", data=b"{}", headers=headers
            )
        self.assertEqual(status, 200)
        self.assertEqual(response["status"], "unchanged")
        check.assert_called_once_with()

    def test_candidate_refresh_uses_csrf_protected_generated_scan(self) -> None:
        payload = candidate_payload()
        headers = {
            "Content-Type": "application/json",
            "Origin": self.base,
            "X-AI-Toolbox-CSRF": self.server.csrf_token,
        }
        with mock.patch.object(
            app, "scan_candidate_and_persist", return_value=payload
        ) as scan:
            status, _, response = self.request_json(
                "/api/candidates/refresh", method="POST", data=b"{}", headers=headers
            )
        self.assertEqual(status, 200)
        self.assertEqual(response, payload)
        scan.assert_called_once_with()

    def test_candidate_catalog_get_returns_current_valid_payload(self) -> None:
        payload = candidate_payload()
        with mock.patch.object(app, "load_candidate_catalog", return_value=payload) as load:
            status, headers, response = self.request_json("/candidates/data.json")
        self.assertEqual(status, 200)
        self.assertEqual(response, payload)
        self.assertEqual(headers["Cache-Control"], "no-store")
        load.assert_called_once_with()

    def test_get_head_query_and_static_symlink_security_boundaries(self) -> None:
        payload = candidate_payload()
        with mock.patch.object(app, "load_candidate_catalog", return_value=payload):
            status, _, response = self.request_json("/candidates/data.json?view=current")
        self.assertEqual(status, 200)
        self.assertEqual(response, payload)

        status, _, health = self.request_json("/api/health?probe=1")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])

        for path in ("/api/health", "/candidates/data.json", "/index.html"):
            with self.subTest(unsafe_host_path=path):
                status, _, error = self.request_json(
                    path,
                    headers={"Host": "attacker.example"},
                )
                self.assertEqual(status, 403)
                self.assertEqual(error["error"], "unsafe_host")

        status, _, raw = self.request_raw(
            "/index.html",
            method="HEAD",
            headers={"Host": "attacker.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(raw, b"")

        secret = self.root / "outside-static-secret.txt"
        secret.write_text("must-not-be-served", encoding="utf-8")
        (self.web / "leak.txt").symlink_to(secret)
        status, _, raw = self.request_raw("/leak.txt")
        self.assertEqual(status, 404)
        self.assertNotIn(b"must-not-be-served", raw)

        status, _, error = self.request_json("/candidates/index.html")
        self.assertEqual(status, 404)
        self.assertEqual(error["error"], "not_found")
        status, _, raw = self.request_raw("/index.html?cache=bust")
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html>", raw)

    def test_corrupt_snapshots_return_stable_json_errors(self) -> None:
        app.SNAPSHOT_PATH.write_text("not-json", encoding="utf-8")
        status, _, response = self.request_json("/api/snapshot")
        self.assertEqual(status, 500)
        self.assertEqual(
            response,
            {"ok": False, "error": "snapshot_invalid", "resource": "snapshot"},
        )
        status, _, response = self.request_json("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertTrue(response["degraded"])
        self.assertEqual(response["errors"], ["snapshot_invalid"])
        self.assertEqual(response["snapshot"]["error"], "snapshot_invalid")
        self.assertFalse(response["snapshot"]["available"])
        self.assertTrue(response["csrf_token"])

        repaired = self.payload()

        def repair_snapshot():
            app.write_json_atomically(repaired, app.SNAPSHOT_PATH)
            app.write_json_atomically(
                {
                    "status": "success",
                    "generated_at": repaired["generated_at"],
                    "generation_id": repaired["generation_id"],
                },
                app.LAST_ATTEMPT_PATH,
            )
            return True, repaired

        with mock.patch.object(app, "scan_and_persist", side_effect=repair_snapshot):
            status, _, _ = self.request_json(
                "/api/refresh",
                method="POST",
                data=b"{}",
                headers=self.post_headers(),
            )
        self.assertEqual(status, 200)
        status, _, health = self.request_json("/api/health")
        self.assertEqual(status, 200)
        self.assertFalse(health["degraded"])
        self.assertEqual(health["errors"], [])
        self.assertTrue(health["snapshot"]["available"])

    def test_corrupt_collection_and_candidate_health_never_tear_connection(self) -> None:
        app.COLLECTION_SNAPSHOT_PATH.write_text("not-json", encoding="utf-8")
        status, _, response = self.request_json("/api/collections")
        self.assertEqual(status, 500)
        self.assertEqual(response["resource"], "collection_snapshot")
        status, _, response = self.request_json("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(response["errors"], ["collection_snapshot_invalid"])
        self.assertEqual(response["collection_snapshot"]["error"], "snapshot_invalid")

        source = self.root / "collection-health-repair"
        skill = source / "sample" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "---\nname: sample\ndescription: health repair fixture\n---\n",
            encoding="utf-8",
        )
        host_roots = {
            host: self.home / f".{host}" / "skills"
            for host in ("codex", "claude", "hermes", "workbuddy")
        }
        repaired_collection = app.validate_collection_payload(
            app.build_collection_payload(
                source_root=source,
                host_roots=host_roots,
                now=FIXED_NOW,
            )
        )

        def repair_collection():
            app.write_json_atomically(repaired_collection, app.COLLECTION_SNAPSHOT_PATH)
            return repaired_collection

        with mock.patch.object(
            app, "scan_collection_and_persist", side_effect=repair_collection
        ):
            status, _, _ = self.request_json(
                "/api/collections/refresh",
                method="POST",
                data=b"{}",
                headers=self.post_headers(),
            )
        self.assertEqual(status, 200)
        status, _, health = self.request_json("/api/health")
        self.assertFalse(health["degraded"])
        self.assertTrue(health["collection_snapshot"]["available"])

        app.COLLECTION_SNAPSHOT_PATH.unlink()
        app.CANDIDATE_SNAPSHOT_PATH.write_text("not-json", encoding="utf-8")
        status, _, response = self.request_json("/candidates/data.json")
        self.assertEqual(status, 500)
        self.assertEqual(response["error"], "candidate_catalog_invalid")
        status, _, response = self.request_json("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(response["errors"], ["candidate_snapshot_invalid"])
        self.assertEqual(response["candidate_snapshot"]["error"], "snapshot_invalid")

        repaired = candidate_payload()

        def repair_candidate():
            app.write_json_atomically(repaired, app.CANDIDATE_SNAPSHOT_PATH)
            return repaired

        with mock.patch.object(app, "scan_candidate_and_persist", side_effect=repair_candidate):
            status, _, _ = self.request_json(
                "/api/candidates/refresh",
                method="POST",
                data=b"{}",
                headers=self.post_headers(),
            )
        self.assertEqual(status, 200)
        status, _, health = self.request_json("/api/health")
        self.assertFalse(health["degraded"])
        self.assertTrue(health["candidate_snapshot"]["available"])

    def test_corrupt_last_attempt_degrades_health_but_refresh_can_repair_it(self) -> None:
        app.LAST_ATTEMPT_PATH.write_text("not-json", encoding="utf-8")
        status, _, health = self.request_json("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["errors"], ["last_attempt_invalid"])
        self.assertEqual(health["last_attempt"]["reason"], "attempt_receipt_unreadable")
        self.assertEqual(health["last_attempt"]["error"], "snapshot_invalid")
        self.assertTrue(health["csrf_token"])

        repaired = self.payload()

        def repair_attempt():
            app.write_json_atomically(repaired, app.SNAPSHOT_PATH)
            app.write_json_atomically(
                {
                    "status": "success",
                    "generated_at": repaired["generated_at"],
                    "generation_id": repaired["generation_id"],
                },
                app.LAST_ATTEMPT_PATH,
            )
            return True, repaired

        with mock.patch.object(app, "scan_and_persist", side_effect=repair_attempt):
            status, _, _ = self.request_json(
                "/api/refresh",
                method="POST",
                data=b"{}",
                headers=self.post_headers(),
            )
        self.assertEqual(status, 200)
        status, _, health = self.request_json("/api/health")
        self.assertFalse(health["degraded"])
        self.assertEqual(health["errors"], [])

    def test_folder_picker_requires_exact_target_and_supports_cancel(self) -> None:
        with mock.patch.object(
            app,
            "run_native_folder_picker",
            return_value={"selected": False, "cancelled": True},
        ) as picker:
            status, _, response = self.request_json(
                "/api/folder-picker",
                method="POST",
                data=json.dumps({"target": "collection-source"}).encode(),
                headers=self.post_headers(),
            )
        self.assertEqual(status, 200)
        self.assertEqual(response, {"selected": False, "cancelled": True})
        picker.assert_called_once_with()

        with mock.patch.object(
            app,
            "run_native_folder_picker",
            side_effect=app.FolderSelectionError("macos_picker_unavailable"),
        ):
            status, _, response = self.request_json(
                "/api/folder-picker",
                method="POST",
                data=json.dumps({"target": "collection-source"}).encode(),
                headers=self.post_headers(),
            )
        self.assertEqual(status, 503)
        self.assertEqual(response, {"error": "macos_picker_unavailable"})

        with mock.patch.object(
            app,
            "run_native_folder_picker",
            return_value={"selected": False, "cancelled": True},
        ):
            status, _, _ = self.request_json(
                "/api/folder-picker",
                method="POST",
                data=json.dumps({"target": "collection-source"}).encode(),
                headers=self.post_headers(),
            )
        self.assertEqual(status, 200)

        for body in ({}, {"target": "candidates"}, {"target": "collection-source", "path": "/tmp"}):
            with self.subTest(body=body), mock.patch.object(
                app, "run_native_folder_picker"
            ) as picker:
                status, _, response = self.request_json(
                    "/api/folder-picker",
                    method="POST",
                    data=json.dumps(body).encode(),
                    headers=self.post_headers(),
                )
                self.assertEqual(status, 400)
                self.assertEqual(response["error"], "unexpected_fields")
                picker.assert_not_called()

    def test_project_skill_folder_picker_uses_target_bound_token_and_in_memory_preview(self) -> None:
        selected = {
            "path": Path("/safe/project"),
            "display_path": "~/Documents/safe-project",
            "relative_path": "safe-project",
            "device": 12,
            "inode": 34,
        }
        preview = {"generation_id": "temporary1234567", "projects": [{}]}
        with mock.patch.object(
            app,
            "run_native_folder_picker",
            return_value={"selected": True, "cancelled": False, "path": "/safe/project"},
        ), mock.patch.object(
            app,
            "validate_selected_project_root",
            return_value=selected,
        ):
            status, _, picked = self.request_json(
                "/api/folder-picker",
                method="POST",
                data=json.dumps({"target": "project-skill-source"}).encode(),
                headers=self.post_headers(),
            )

        self.assertEqual(status, 200)
        self.assertNotIn("path", picked)
        with mock.patch.object(
            app,
            "build_in_memory_project_skill_preview",
            return_value=preview,
        ) as build:
            status, _, payload = self.request_json(
                "/api/project-skills/folder-selection/confirm",
                method="POST",
                data=json.dumps({"selection_token": picked["selection_token"]}).encode(),
                headers=self.post_headers(),
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["snapshot"], preview)
        self.assertEqual(payload["selection"], {
            "mode": "temporary",
            "display_path": "~/Documents/safe-project",
        })
        build.assert_called_once_with(Path("/safe/project"), expected_identity=(12, 34))

        collection_token, _ = self.server.issue_folder_token(selected)
        status, _, rejected = self.request_json(
            "/api/project-skills/folder-selection/confirm",
            method="POST",
            data=json.dumps({"selection_token": collection_token}).encode(),
            headers=self.post_headers(),
        )
        self.assertEqual(status, 409)
        self.assertEqual(rejected["error"], "selection_token_target_mismatch")

    def test_folder_picker_returns_only_opaque_token_and_display_path(self) -> None:
        selection = {
            "path": Path("/safe/source"),
            "display_path": "/safe/source",
            "device": 41,
            "inode": 73,
        }
        with mock.patch.object(
            app,
            "run_native_folder_picker",
            return_value={
                "selected": True,
                "cancelled": False,
                "path": "/safe/source",
            },
        ), mock.patch.object(
            app, "validate_selected_source_root", return_value=selection
        ):
            status, _, response = self.request_json(
                "/api/folder-picker",
                method="POST",
                data=json.dumps({"target": "collection-source"}).encode(),
                headers=self.post_headers(),
            )

        self.assertEqual(status, 200)
        self.assertEqual(
            set(response),
            {"selected", "selection_token", "display_path", "expires_in"},
        )
        self.assertTrue(response["selected"])
        self.assertNotIn("device", response)
        self.assertNotIn("inode", response)
        self.assertIn(response["selection_token"], self.server.folder_tokens)

    def test_folder_picker_rejects_sensitive_roots_with_400_without_path_disclosure(self) -> None:
        home = Path.home()
        unsafe_paths = (
            home / ".ssh",
            home / ".gnupg" / "private-keys-v1.d",
            home / ".aws",
            home / ".kube",
            home / ".docker",
            home / ".config",
            home / ".local",
            home / "Library" / "Keychains",
            home / "Library" / "Application Support" / "Provider",
            home / "Library" / "Containers" / "Provider",
            home / "Library" / "Group Containers" / "Provider",
            app.ROOT,
        )
        for unsafe in unsafe_paths:
            with self.subTest(unsafe=unsafe), mock.patch.object(
                app,
                "run_native_folder_picker",
                return_value={
                    "selected": True,
                    "cancelled": False,
                    "path": str(unsafe),
                },
            ):
                status, _, response = self.request_json(
                    "/api/folder-picker",
                    method="POST",
                    data=json.dumps({"target": "collection-source"}).encode(),
                    headers=self.post_headers(),
                )
                self.assertEqual(status, 400)
                self.assertIn(
                    response["error"],
                    {"folder_root_sensitive", "folder_root_protected"},
                )
                serialized = json.dumps(response, ensure_ascii=False)
                self.assertNotIn(str(unsafe), serialized)
                self.assertNotIn(str(home), serialized)

    def test_folder_confirmation_is_single_use_and_never_writes_generated(self) -> None:
        selection = {
            "path": Path("/safe/source"),
            "display_path": "/safe/source",
            "device": 41,
            "inode": 73,
        }
        token, _ = self.server.issue_folder_token(selection)
        pair = temporary_pair(selection["path"])
        before = sorted(path.name for path in self.generated.iterdir())
        with mock.patch.object(app, "build_in_memory_source_pair", return_value=pair) as build:
            status, _, response = self.request_json(
                "/api/folder-selection/confirm",
                method="POST",
                data=json.dumps({"selection_token": token}).encode(),
                headers=self.post_headers(),
            )
        self.assertEqual(status, 200)
        self.assertEqual(
            set(response),
            {"source_session", "collection_snapshot", "candidate_catalog"},
        )
        self.assertEqual(response["source_session"]["mode"], "temporary")
        self.assertNotIn("path", response["source_session"])
        build.assert_called_once_with(
            selection["path"],
            expected_identity=(selection["device"], selection["inode"]),
        )
        self.assertEqual(
            sorted(path.name for path in self.generated.iterdir()),
            before,
        )

        status, _, response = self.request_json(
            "/api/folder-selection/confirm",
            method="POST",
            data=json.dumps({"selection_token": token}).encode(),
            headers=self.post_headers(),
        )
        self.assertEqual(status, 409)
        self.assertEqual(response["error"], "selection_token_invalid_or_replayed")

    def test_folder_confirmation_accepts_audited_zip_symlink_warning_without_reading_target(self) -> None:
        marker = "SECRET_ZIP_SYMLINK_TARGET/../../outside"
        with tempfile.TemporaryDirectory() as selected_temp:
            source = Path(selected_temp).resolve()
            archive = source / "symlink-member.zip"
            link = zipfile.ZipInfo("links/SKILL.md")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr(link, marker)

            selection = app.validate_selected_source_root(str(source))
            token, _ = self.server.issue_folder_token(selection)
            state = {"status": "observed", "signature": "f" * 64}
            candidate = candidate_payload(source_dir=source.as_posix())
            with mock.patch.object(
                app, "build_collection_input_state", return_value=state
            ), mock.patch.object(
                app, "build_candidate_payload", return_value=candidate
            ):
                status, _, response = self.request_json(
                    "/api/folder-selection/confirm",
                    method="POST",
                    data=json.dumps({"selection_token": token}).encode(),
                    headers=self.post_headers(),
                )

        self.assertEqual(status, 200)
        errors = response["collection_snapshot"]["scan_errors"]
        self.assertEqual(
            errors,
            [
                {
                    "code": "symlink_skipped",
                    "path": "symlink-member.zip/links/SKILL.md",
                }
            ],
        )
        serialized = json.dumps(response, ensure_ascii=False)
        self.assertNotIn(marker, serialized)
        self.assertNotIn("../../outside", serialized)

    def test_folder_tokens_reject_expiry_and_cross_target(self) -> None:
        selection = {
            "path": Path("/safe/source"),
            "display_path": "/safe/source",
            "device": 41,
            "inode": 73,
        }
        expired, _ = self.server.issue_folder_token(selection)
        with self.server.source_state_lock:
            self.server.folder_tokens[expired]["expires_at"] = app.time.monotonic() - 1
        status, _, response = self.request_json(
            "/api/folder-selection/confirm",
            method="POST",
            data=json.dumps({"selection_token": expired}).encode(),
            headers=self.post_headers(),
        )
        self.assertEqual(status, 410)
        self.assertEqual(response["error"], "selection_token_expired")

        wrong_target, _ = self.server.issue_folder_token(selection)
        with self.server.source_state_lock:
            self.server.folder_tokens[wrong_target]["target"] = "candidate-only"
        status, _, response = self.request_json(
            "/api/folder-selection/confirm",
            method="POST",
            data=json.dumps({"selection_token": wrong_target}).encode(),
            headers=self.post_headers(),
        )
        self.assertEqual(status, 409)
        self.assertEqual(response["error"], "selection_token_target_mismatch")

    def test_failed_folder_confirmation_preserves_current_session_and_consumes_token(self) -> None:
        current = temporary_pair(Path("/safe/current"), marker="before")
        self.server.activate_temporary_source(current)
        before = self.server.temporary_source()
        selection = {
            "path": Path("/safe/replacement"),
            "display_path": "/safe/replacement",
            "device": 99,
            "inode": 100,
        }
        token, _ = self.server.issue_folder_token(selection)
        with mock.patch.object(
            app,
            "build_in_memory_source_pair",
            side_effect=app.SnapshotValidationError("candidate rejected"),
        ):
            status, _, response = self.request_json(
                "/api/folder-selection/confirm",
                method="POST",
                data=json.dumps({"selection_token": token}).encode(),
                headers=self.post_headers(),
            )
        self.assertEqual(status, 422)
        self.assertEqual(response["error"], "selected_source_scan_failed")
        after = self.server.temporary_source()
        self.assertEqual(after["source_key"], before["source_key"])
        self.assertEqual(after["collection_snapshot"], before["collection_snapshot"])

        status, _, response = self.request_json(
            "/api/folder-selection/confirm",
            method="POST",
            data=json.dumps({"selection_token": token}).encode(),
            headers=self.post_headers(),
        )
        self.assertEqual(status, 409)
        self.assertEqual(response["error"], "selection_token_invalid_or_replayed")

    def test_temporary_get_and_refresh_share_one_atomic_in_memory_pair(self) -> None:
        initial = temporary_pair(Path("/safe/source"), marker="initial")
        self.server.activate_temporary_source(initial)
        with mock.patch.object(
            app, "load_collection_snapshot", side_effect=AssertionError("disk read")
        ), mock.patch.object(
            app, "load_candidate_catalog", side_effect=AssertionError("disk read")
        ):
            status, _, collection = self.request_json("/api/collections?session=1")
            self.assertEqual(status, 200)
            self.assertEqual(collection, initial["collection_snapshot"])
            status, _, candidate = self.request_json("/candidates/data.json?session=1")
            self.assertEqual(status, 200)
            self.assertEqual(candidate, initial["candidate_catalog"])

        updated = temporary_pair(Path("/safe/source"), marker="updated")
        updated["candidate_catalog"] = candidate_payload(
            generated_at="2026-08-10T12:02:00+00:00",
            source_dir="/safe/source",
        )
        with mock.patch.object(app, "build_in_memory_source_pair", return_value=updated) as build:
            status, _, collection = self.request_json(
                "/api/collections/refresh",
                method="POST",
                data=b"{}",
                headers=self.post_headers(),
            )
        self.assertEqual(status, 200)
        self.assertEqual(collection["summary"]["marker"], "updated")
        build.assert_called_once_with(
            initial["path"],
            expected_identity=(initial["device"], initial["inode"]),
        )
        status, _, candidate = self.request_json("/candidates/data.json")
        self.assertEqual(candidate["generated_at"], "2026-08-10T12:02:00+00:00")

    def test_temporary_collection_check_uses_active_root_without_rebuild_when_unchanged(self) -> None:
        pair = temporary_pair(Path("/safe/source"))
        self.server.activate_temporary_source(pair)
        selection = {
            "path": pair["path"],
            "display_path": pair["display_path"],
            "device": pair["device"],
            "inode": pair["inode"],
        }
        with mock.patch.object(
            app, "validate_selected_source_root", return_value=selection
        ), mock.patch.object(
            app,
            "build_collection_input_state",
            return_value={
                "status": "observed",
                "signature": pair["input_signature"],
            },
        ) as state, mock.patch.object(app, "build_in_memory_source_pair") as build:
            status, _, response = self.request_json(
                "/api/collections/check",
                method="POST",
                data=b"{}",
                headers=self.post_headers(),
            )
        self.assertEqual(status, 200)
        self.assertEqual(response["status"], "unchanged")
        self.assertEqual(response["snapshot"], pair["collection_snapshot"])
        state.assert_called_once_with(source_root=pair["path"])
        build.assert_not_called()

    def test_restore_returns_default_pair_and_clears_temporary_session(self) -> None:
        self.server.activate_temporary_source(temporary_pair(Path("/safe/source")))
        default_collection = {"source_root": "/default", "items": []}
        default_candidate = candidate_payload(source_dir="/default")
        with mock.patch.object(
            app,
            "_load_default_source_pair",
            return_value=(default_collection, default_candidate),
        ):
            status, _, response = self.request_json(
                "/api/folder-source/restore",
                method="POST",
                data=b"{}",
                headers=self.post_headers(),
            )
        self.assertEqual(status, 200)
        self.assertEqual(response["source_session"]["mode"], "default")
        self.assertEqual(response["collection_snapshot"], default_collection)
        self.assertEqual(response["candidate_catalog"], default_candidate)
        self.assertIsNone(self.server.temporary_source())

    def test_restore_validates_default_pair_before_replacing_temporary_session(self) -> None:
        current = temporary_pair(Path("/safe/current"), marker="preserved")
        self.server.activate_temporary_source(current)
        before = self.server.temporary_source()
        app.COLLECTION_SNAPSHOT_PATH.write_text("not-json", encoding="utf-8")

        status, _, response = self.request_json(
            "/api/folder-source/restore",
            method="POST",
            data=b"{}",
            headers=self.post_headers(),
        )

        self.assertEqual(status, 500)
        self.assertEqual(response["error"], "snapshot_invalid")
        self.assertEqual(response["resource"], "default_source")
        after = self.server.temporary_source()
        self.assertEqual(after["source_key"], before["source_key"])
        self.assertEqual(after["collection_snapshot"], before["collection_snapshot"])
        self.assertEqual(after["candidate_catalog"], before["candidate_catalog"])

    def test_picker_confirm_and_refresh_return_conflict_during_active_operation(self) -> None:
        requests = (
            ("/api/folder-picker", {"target": "collection-source"}),
            ("/api/folder-selection/confirm", {"selection_token": "opaque"}),
            ("/api/collections/refresh", {}),
        )
        self.server.refresh_lock.acquire()
        try:
            for path, body in requests:
                with self.subTest(path=path):
                    status, _, response = self.request_json(
                        path,
                        method="POST",
                        data=json.dumps(body).encode(),
                        headers=self.post_headers(),
                    )
                    self.assertEqual(status, 409)
                    self.assertEqual(response["error"], "refresh_in_progress")
        finally:
            self.server.refresh_lock.release()

    def test_incomplete_collection_refresh_returns_422(self) -> None:
        incomplete = {
            "generated_at": FIXED_NOW,
            "summary": {"scan_error_count": 1},
            "scan_errors": [{"code": "source_root_missing"}],
        }
        with mock.patch.object(
            app,
            "scan_collection_and_persist",
            side_effect=app.CollectionScanIncomplete(
                incomplete,
                "collection_source_unstable_or_incomplete",
            ),
        ):
            status, _, response = self.request_json(
                "/api/collections/refresh",
                method="POST",
                data=b"{}",
                headers=self.post_headers(),
            )
        self.assertEqual(status, 422)
        self.assertEqual(response["error"], "collection_scan_incomplete")
        self.assertEqual(response["attempt"]["scan_errors"], incomplete["scan_errors"])

    def test_zh_sync_http_routes_are_not_exposed(self) -> None:
        status, _, response = self.request_json("/api/zh-sync/check")
        self.assertEqual(status, 404)
        self.assertEqual(response["error"], "not_found")
        status, _, response = self.request_json(
            "/api/zh-sync/apply",
            method="POST",
            data=b"{}",
            headers=self.post_headers(),
        )
        self.assertEqual(status, 404)
        self.assertEqual(response["error"], "not_found")

    def test_refresh_failure_records_attempt_without_replacing_snapshot(self) -> None:
        previous = self.payload()
        app.write_json_atomically(previous, app.SNAPSHOT_PATH)
        original = app.SNAPSHOT_PATH.read_bytes()
        headers = {
            "Content-Type": "application/json",
            "Origin": self.base,
            "X-AI-Toolbox-CSRF": self.server.csrf_token,
        }
        with mock.patch.object(app, "scan_and_persist", side_effect=RuntimeError("private detail")):
            status, _, response = self.request_json(
                "/api/refresh", method="POST", data=b"{}", headers=headers
            )
        self.assertEqual(status, 500)
        self.assertEqual(response["error"], "scan_failed")
        self.assertEqual(app.SNAPSHOT_PATH.read_bytes(), original)
        attempt = json.loads(app.LAST_ATTEMPT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(attempt["status"], "failed")
        self.assertNotIn("private detail", json.dumps(attempt))

    def test_refresh_get_and_options_are_not_allowed(self) -> None:
        status, _, _ = self.request_json("/api/refresh")
        self.assertEqual(status, 405)
        status, _, _ = self.request_json("/api/collections/check")
        self.assertEqual(status, 405)
        status, _, _ = self.request_json("/api/candidates/refresh")
        self.assertEqual(status, 405)
        status, _, _ = self.request_json("/api/health", method="OPTIONS")
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()
