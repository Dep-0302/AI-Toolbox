from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in os.sys.path:
    os.sys.path.insert(0, str(SRC))

from toolbox_scan import (  # noqa: E402
    ASSET_TYPES,
    EXPECTED_SCAN_LIMITS,
    MAX_METADATA_JSON_DEPTH,
    _classify_subcategory,
    _load_overlay,
    _parse_plugin_manifest,
    _reject_excessive_json_depth,
    build_toolbox_payload,
)


FIXTURES = Path(__file__).parent / "fixtures"
FIXED_NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


class ToolboxScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _copy_skill(self, host: str, fixture: str = "sample_skill", name: str = "sample-skill") -> Path:
        destination = self.home / f".{host}" / "skills" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(FIXTURES / fixture, destination)
        return destination

    def _copy_plugin(self) -> Path:
        destination = self.home / ".codex" / "plugins" / "cache" / "vendor" / "sample" / "2.0.0"
        destination.mkdir(parents=True)
        shutil.copy2(FIXTURES / "sample_plugin" / "plugin.json", destination / "plugin.json")
        return destination

    def _copy_codex_observation_plugin(self) -> Path:
        destination = (
            self.home
            / ".codex"
            / "plugins"
            / "cache"
            / "fixture-channel"
            / "codex-observation-plugin"
            / "1.2.3"
        )
        shutil.copytree(FIXTURES / "codex_observation_plugin", destination)
        sdk_manifest = (
            destination
            / "node_modules"
            / "@modelcontextprotocol"
            / "sdk"
            / "package.json"
        )
        sdk_manifest.parent.mkdir(parents=True)
        shutil.copy2(destination / "sdk-package.json", sdk_manifest)
        return destination

    def test_scene_subcategory_keeps_specific_rules_ahead_of_general_rules(self) -> None:
        self.assertEqual(
            _classify_subcategory(
                "skill",
                "seedance-whitebox-director",
                "Seedance director",
                "创意生产",
            ),
            "Seedance 提示词 · 特化场景",
        )
        self.assertEqual(
            _classify_subcategory(
                "skill",
                "seedance-video-prompt",
                "Seedance prompt generator",
                "创意生产",
            ),
            "Seedance 提示词 · 通用型",
        )
        self.assertEqual(
            _classify_subcategory(
                "skill",
                "motionprint-reverse",
                "Reverse an AI video into a prompt",
                "创意生产",
            ),
            "反推与提示词提取",
        )
        self.assertEqual(
            _classify_subcategory("skill", "forget", "Forget selected memory", "数据与分析"),
            "AgentMemory 与长期记忆",
        )
        self.assertNotEqual(
            _classify_subcategory("skill", "forget", "Forget selected memory", "数据与分析"),
            "造技能的元技能",
        )

    def _install_fake_codex_cli(self) -> Path:
        target = self.home / ".codex" / "plugins" / ".plugin-appserver" / "codex"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"must-not-leak-binary-body")
        target.chmod(0o100)
        entry = self.home / ".local" / "bin" / "codex"
        entry.parent.mkdir(parents=True)
        os.symlink(target, entry)
        return target

    def _snapshot_tree(self) -> list[tuple[object, ...]]:
        snapshot: list[tuple[object, ...]] = []
        for path in sorted(self.home.rglob("*")):
            relative = path.relative_to(self.home).as_posix()
            info = path.lstat()
            mode = info.st_mode & 0o777
            if path.is_symlink():
                snapshot.append((relative, "symlink", mode, os.readlink(path)))
            elif path.is_dir():
                snapshot.append((relative, "dir", mode, info.st_mtime_ns))
            else:
                try:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                except PermissionError:
                    digest = "unreadable"
                snapshot.append(
                    (relative, "file", mode, info.st_size, info.st_mtime_ns, digest)
                )
        return snapshot

    def test_empty_home_has_fixed_hosts_and_no_side_effects(self) -> None:
        before = self._snapshot_tree()
        payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        after = self._snapshot_tree()

        self.assertEqual(before, after)
        self.assertEqual(
            [host["id"] for host in payload["hosts"]],
            ["codex", "claude", "hermes", "workbuddy", "antigravity"],
        )
        self.assertEqual(payload["hosts"][-1]["status"], "placeholder")
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["summary"]["asset_count"], 0)
        self.assertEqual(
            set(payload),
            {"schema_version", "generated_at", "mode", "summary", "hosts", "items", "health_findings", "scan_scope", "scan_errors"},
        )
        self.assertEqual(payload["mode"], "observe")
        self.assertEqual(payload["generated_at"], "2026-07-20T12:00:00Z")
        self.assertFalse(payload["scan_scope"]["safety"]["writes"])
        self.assertFalse(payload["scan_scope"]["safety"]["network"])
        self.assertFalse(payload["scan_scope"]["safety"]["process_execution"])
        self.assertFalse(payload["scan_scope"]["safety"]["reads_host_config_bodies"])
        coverage = payload["scan_scope"]["coverage"]
        self.assertEqual(len(coverage), 25)
        self.assertEqual(
            len({(row["host_id"], row["asset_type"]) for row in coverage}),
            25,
        )
        by_pair = {(row["host_id"], row["asset_type"]): row for row in coverage}
        for asset_type in ("mcp", "cli", "sdk"):
            self.assertEqual(by_pair[("codex", asset_type)]["status"], "missing")
            for host_id in ("claude", "hermes", "workbuddy"):
                self.assertEqual(by_pair[(host_id, asset_type)]["status"], "not_connected")
            self.assertEqual(by_pair[("antigravity", asset_type)]["status"], "placeholder")

    def test_skill_sources_use_only_bounded_provenance_evidence(self) -> None:
        def write_skill(host: str, relative: str, name: str, extra: str = "") -> Path:
            directory = self.home / f".{host}" / "skills" / relative
            directory.mkdir(parents=True, exist_ok=True)
            directory.joinpath("SKILL.md").write_text(
                f"---\nname: {name}\ndescription: Fixture source skill.\n{extra}---\n\n# {name}\n",
                encoding="utf-8",
            )
            return directory

        hermes_root = self.home / ".hermes" / "skills"
        system_skill = write_skill("hermes", "system-example", "system-example")
        website_skill = write_skill("hermes", "community-example", "community-example")
        hermes_root.joinpath(".bundled_manifest").write_text(
            "system-example:" + "a" * 32 + "\n",
            encoding="utf-8",
        )
        lock = hermes_root / ".hub" / "lock.json"
        lock.parent.mkdir(parents=True)
        lock.write_text(
            json.dumps(
                {
                    "version": 1,
                    "installed": {
                        "community-example": {
                            "source": "skills.sh",
                            "install_path": "community-example",
                            "trust_level": "community",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        workbuddy_website = write_skill("workbuddy", "market-example", "market-example")
        workbuddy_website.joinpath("_skillhub_meta.json").write_text(
            json.dumps({"source": "marketplace", "version": "1.0.0"}),
            encoding="utf-8",
        )
        write_skill("workbuddy", "local-example", "local-example", "agent_created: true\n")
        github_skill = write_skill("codex", "github-example", "github-example")
        write_skill("claude", "unknown-example", "unknown-example")

        where_from = plistlib.dumps(["https://github.com/example/source"])

        def fake_getxattr(path, _name, **_kwargs):
            if Path(path) == github_skill / "SKILL.md":
                return where_from
            raise OSError("no provenance metadata")

        with mock.patch("toolbox_scan.os.getxattr", side_effect=fake_getxattr, create=True):
            payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)

        origins = {
            item["source_name"]: (
                item["source"]["origin_kind"],
                item["source"]["origin_basis"],
            )
            for item in payload["items"]
            if item["type"] == "skill"
        }
        self.assertEqual(origins[system_skill.name], ("system", "hermes_bundled_manifest"))
        self.assertEqual(origins[website_skill.name], ("website", "hermes_skill_registry"))
        self.assertEqual(origins[workbuddy_website.name], ("website", "workbuddy_skill_metadata"))
        self.assertEqual(origins["local-example"], ("local", "frontmatter"))
        self.assertEqual(origins[github_skill.name], ("github", "download_metadata"))
        self.assertEqual(origins["unknown-example"], ("unknown", "unavailable"))

    def test_codex_metadata_sources_observe_three_types_without_leaks_or_writes(self) -> None:
        self._copy_codex_observation_plugin()
        cli_target = self._install_fake_codex_cli()
        codex_root = self.home / ".codex"
        codex_root.joinpath("config.toml").write_text(
            'api_key = "must-not-leak-config-secret"\n', encoding="utf-8"
        )
        codex_root.joinpath("auth.json").write_text(
            '{"token":"must-not-leak-auth-secret"}\n', encoding="utf-8"
        )
        before = self._snapshot_tree()
        real_open = os.open

        def deny_binary_read(path, flags, *args, **kwargs):
            if Path(path) == cli_target:
                raise AssertionError("Codex CLI binary body must not be opened")
            return real_open(path, flags, *args, **kwargs)

        with mock.patch("toolbox_scan.os.open", side_effect=deny_binary_read):
            payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        after = self._snapshot_tree()

        self.assertEqual(before, after)
        observed = {
            asset_type: [item for item in payload["items"] if item["type"] == asset_type]
            for asset_type in ("mcp", "cli", "sdk")
        }
        self.assertEqual({key: len(value) for key, value in observed.items()}, {"mcp": 1, "cli": 1, "sdk": 1})
        for item in (observed["mcp"][0], observed["cli"][0], observed["sdk"][0]):
            self.assertEqual(item["trust"]["action"], "locked")
            self.assertTrue(item["host_bindings"])
            for binding in item["host_bindings"]:
                self.assertEqual(binding["host_id"], "codex")
                self.assertEqual(binding["action"], "locked")
                self.assertEqual(binding["activation"], "unknown")
                self.assertEqual(binding["effective"], "unverified")

        mcp_observation = observed["mcp"][0]["source"]["observation"]
        self.assertEqual(mcp_observation["server_id"], "fixture-stdio")
        self.assertEqual(mcp_observation["transport_kind"], "stdio")
        self.assertTrue(mcp_observation["sensitive_config_present"])
        self.assertEqual(
            observed["cli"][0]["source"]["observation"]["basis"],
            "filesystem_metadata_projection",
        )
        self.assertEqual(observed["sdk"][0]["source_name"], "@modelcontextprotocol/sdk")

        serialized = json.dumps(payload, ensure_ascii=False)
        for secret in (
            "must-not-leak-command",
            "must-not-leak-argument",
            "/must/not/leak",
            "must-not-leak-token",
            "must-not-leak-header",
            "must-not-leak-sensitive-title",
            "must-not-leak-sensitive-description",
            "must-not-execute-postinstall",
            "must-not-leak-package-token",
            "must-not-execute-sdk-postinstall",
            "must-not-leak-sdk-token",
            "must-not-leak-binary-body",
            "must-not-leak-config-secret",
            "must-not-leak-auth-secret",
        ):
            self.assertNotIn(secret, serialized)
        self.assertNotIn("config.toml", serialized)
        self.assertNotIn("auth.json", serialized)

        def nested_keys(value) -> set[str]:
            if isinstance(value, dict):
                return set(value).union(*(nested_keys(child) for child in value.values()))
            if isinstance(value, list):
                return set().union(*(nested_keys(child) for child in value))
            return set()

        self.assertTrue(
            {
                "command",
                "args",
                "cwd",
                "url",
                "env",
                "headers",
                "credentials",
                "token",
                "scripts",
                "postinstall",
            }.isdisjoint(nested_keys(payload))
        )

        coverage = {
            (row["host_id"], row["asset_type"]): row
            for row in payload["scan_scope"]["coverage"]
        }
        for asset_type in ("mcp", "cli", "sdk"):
            self.assertEqual(coverage[("codex", asset_type)]["status"], "observed")
            self.assertEqual(coverage[("codex", asset_type)]["item_count"], 1)

    def test_codex_metadata_stays_bound_to_the_observed_plugin_directory(self) -> None:
        plugin = self._copy_codex_observation_plugin()
        replacement = plugin.with_name("1.2.3-replacement")
        import toolbox_scan

        real_scan_mcp = toolbox_scan._scan_codex_mcp_metadata
        swapped = False

        def replace_package_before_metadata(*args, **kwargs):
            nonlocal swapped
            if not swapped:
                plugin.rename(replacement)
                shutil.copytree(FIXTURES / "codex_observation_plugin", plugin)
                plugin.joinpath(".mcp.json").write_text(
                    '{"mcpServers":{"replacement-server":{"command":"REPLACEMENT_SENTINEL"}}}',
                    encoding="utf-8",
                )
                swapped = True
            return real_scan_mcp(*args, **kwargs)

        with mock.patch(
            "toolbox_scan._scan_codex_mcp_metadata",
            side_effect=replace_package_before_metadata,
        ):
            payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)

        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertTrue(swapped)
        self.assertIn("fixture-stdio", serialized)
        self.assertNotIn("replacement-server", serialized)
        self.assertNotIn("REPLACEMENT_SENTINEL", serialized)
        self.assertEqual(len([item for item in payload["items"] if item["type"] == "sdk"]), 1)

    def test_deep_plugin_mcp_and_sdk_json_fail_closed(self) -> None:
        cases = (
            (
                "plugin",
                ".codex-plugin/plugin.json",
                '{"name":"deep-plugin","nested":',
                "}",
                "manifest_rejected",
            ),
            (
                "mcp",
                ".mcp.json",
                '{"mcpServers":{"deep":{"nested":',
                "}}}",
                "metadata_source_rejected",
            ),
            (
                "sdk",
                "node_modules/@modelcontextprotocol/sdk/package.json",
                '{"name":"@modelcontextprotocol/sdk","nested":',
                "}",
                "metadata_source_rejected",
            ),
        )
        depth_cases = (
            ("policy_limit", MAX_METADATA_JSON_DEPTH + 1, True),
            (
                "parser_recursion",
                max(MAX_METADATA_JSON_DEPTH + 1, os.sys.getrecursionlimit() + 100),
                False,
            ),
        )
        package_ref = (
            "~/.codex/plugins/cache/fixture-channel/"
            "codex-observation-plugin/1.2.3"
        )

        for depth_label, depth, expect_depth_message in depth_cases:
            deep_array = "[" * depth + "null" + "]" * depth
            for asset_type, relative_path, prefix, suffix, expected_error in cases:
                with self.subTest(asset_type=asset_type, depth=depth_label):
                    shutil.rmtree(self.home)
                    self.home.mkdir()
                    plugin = self._copy_codex_observation_plugin()
                    plugin.joinpath(relative_path).write_text(
                        prefix + deep_array + suffix, encoding="utf-8"
                    )
                    payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)

                    self.assertEqual(
                        [
                            (row["code"], row["path"])
                            for row in payload["scan_errors"]
                        ],
                        [(expected_error, f"{package_ref}/{relative_path}")],
                    )
                    if expect_depth_message:
                        self.assertIn(
                            "maximum JSON nesting depth",
                            payload["scan_errors"][0]["message"],
                        )
                    self.assertEqual(
                        [item for item in payload["items"] if item["type"] == asset_type],
                        [],
                    )
                    if asset_type in {"mcp", "sdk"}:
                        coverage = next(
                            row
                            for row in payload["scan_scope"]["coverage"]
                            if row["host_id"] == "codex"
                            and row["asset_type"] == asset_type
                        )
                        self.assertEqual(coverage["status"], "partial")

    def test_metadata_json_depth_guard_has_an_explicit_boundary(self) -> None:
        at_limit: object = None
        for _ in range(MAX_METADATA_JSON_DEPTH):
            at_limit = [at_limit]

        _reject_excessive_json_depth(at_limit)

        with self.assertRaisesRegex(ValueError, "maximum JSON nesting depth"):
            _reject_excessive_json_depth([at_limit])

    def test_deep_plugin_rejection_closes_package_directory_handle(self) -> None:
        plugin = self._copy_codex_observation_plugin()
        manifest = plugin / ".codex-plugin" / "plugin.json"
        depth = MAX_METADATA_JSON_DEPTH + 1
        deep_array = "[" * depth + "null" + "]" * depth
        manifest.write_text(
            '{"name":"deep-plugin","nested":' + deep_array + "}",
            encoding="utf-8",
        )
        import toolbox_scan

        real_open_directory = toolbox_scan._open_directory_beneath
        captured_fds: list[int] = []

        def capture_package_handle(root, path):
            descriptor = real_open_directory(root, path)
            if Path(path) == plugin:
                captured_fds.append(descriptor)
            return descriptor

        with mock.patch(
            "toolbox_scan._open_directory_beneath",
            side_effect=capture_package_handle,
        ):
            with self.assertRaisesRegex(ValueError, "maximum JSON nesting depth"):
                _parse_plugin_manifest(
                    manifest,
                    self.home,
                    EXPECTED_SCAN_LIMITS,
                )

        self.assertEqual(len(captured_fds), 1)
        with self.assertRaises(OSError):
            os.fstat(captured_fds[0])

    def test_plugin_directory_handles_close_when_metadata_adapter_raises(self) -> None:
        self._copy_codex_observation_plugin()
        import toolbox_scan

        real_plugin_packages = toolbox_scan._plugin_packages
        captured_fds: list[int] = []

        def capture_package_handles(*args, **kwargs):
            packages = real_plugin_packages(*args, **kwargs)
            captured_fds.extend(package["directory_fd"] for package in packages)
            return packages

        with mock.patch(
            "toolbox_scan._plugin_packages",
            side_effect=capture_package_handles,
        ), mock.patch(
            "toolbox_scan._scan_codex_mcp_metadata",
            side_effect=RuntimeError("fixture adapter failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "fixture adapter failure"):
                build_toolbox_payload(home=self.home, now=FIXED_NOW)

        self.assertTrue(captured_fds)
        for package_fd in captured_fds:
            with self.assertRaises(OSError):
                os.fstat(package_fd)

    def test_sdk_requires_a_direct_allowlisted_dependency(self) -> None:
        plugin = self._copy_codex_observation_plugin()
        package_path = plugin / "package.json"
        package = json.loads(package_path.read_text(encoding="utf-8"))
        package["dependencies"] = {"not-an-allowed-sdk": "1.0.0"}
        package_path.write_text(json.dumps(package), encoding="utf-8")
        skill = self.home / ".codex" / "skills" / "agents-sdk"
        skill.mkdir(parents=True)
        shutil.copy2(FIXTURES / "sample_skill" / "SKILL.md", skill / "SKILL.md")

        payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)

        self.assertEqual([item for item in payload["items"] if item["type"] == "sdk"], [])
        sdk_coverage = next(
            row
            for row in payload["scan_scope"]["coverage"]
            if row["host_id"] == "codex" and row["asset_type"] == "sdk"
        )
        self.assertEqual(sdk_coverage["status"], "observed")
        self.assertEqual(sdk_coverage["item_count"], 0)

    def test_schema_freezes_type_host_binding_and_trust_axes(self) -> None:
        schema = json.loads((ROOT / "schemas" / "tool_asset.schema.json").read_text(encoding="utf-8"))
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("generation_id", schema["required"])
        self.assertIn("mode", schema["required"])
        asset = schema["$defs"]["asset"]
        binding = schema["$defs"]["binding"]
        trust = schema["$defs"]["trust"]
        source = schema["$defs"]["source"]
        scan_scope = schema["$defs"]["scan_scope"]
        curation = schema["$defs"]["curation"]
        self.assertFalse(asset["additionalProperties"])
        self.assertEqual(asset["properties"]["type"]["enum"], ["skill", "plugin", "mcp", "cli", "sdk"])
        self.assertEqual(binding["properties"]["action"]["const"], "locked")
        self.assertEqual(binding["properties"]["effective"]["const"], "unverified")
        self.assertIn("package_fingerprint", binding["required"])
        self.assertIn("package_variants", source["required"])
        self.assertIn("origin_kind", source["required"])
        self.assertIn("origin_basis", source["required"])
        self.assertEqual(
            source["properties"]["origin_kind"]["enum"],
            ["github", "website", "chat", "local", "system", "unknown"],
        )
        self.assertEqual(trust["properties"]["action"]["const"], "locked")
        self.assertIn("subcategory", curation["properties"])
        self.assertNotIn("subcategory", curation["required"])
        self.assertEqual(scan_scope["properties"]["roots"]["minItems"], 6)
        self.assertEqual(scan_scope["properties"]["roots"]["maxItems"], 6)
        self.assertEqual(
            scan_scope["properties"]["limits"]["properties"]["max_depth"]["const"],
            5,
        )

    def test_schema_freezes_exact_codex_metadata_paths(self) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "tool_asset.schema.json").read_text(encoding="utf-8")
        )
        root_scope = schema["$defs"]["root_scope"]
        frozen_paths = {
            rule["then"]["properties"]["path"]["const"]
            for rule in root_scope["allOf"]
        }
        self.assertEqual(
            frozen_paths,
            {
                "~/.codex/skills",
                "~/.codex/plugins/cache",
                "~/.claude/skills",
                "~/.claude/plugins/cache",
                "~/.hermes/skills",
                "~/.workbuddy/skills",
            },
        )

        coverage_rules = schema["$defs"]["coverage"]["items"]["allOf"]
        metadata_rules = {
            rule["if"]["properties"]["asset_type"]["const"]: rule["then"]["properties"]
            for rule in coverage_rules
            if isinstance(rule.get("if"), dict)
            and isinstance(rule["if"].get("properties"), dict)
            and rule["if"]["properties"].get("host_id") == {"const": "codex"}
            and rule["if"]["properties"].get("asset_type", {}).get("const")
            in {"mcp", "cli", "sdk"}
        }
        self.assertEqual(set(metadata_rules), {"mcp", "cli", "sdk"})
        self.assertEqual(
            metadata_rules["cli"]["source_refs"]["minItems"], 2
        )
        for asset_type in ("mcp", "sdk"):
            source_schema = metadata_rules[asset_type]["source_refs"]
            self.assertEqual(
                source_schema["contains"]["const"], "~/.codex/plugins/cache"
            )
            patterns = [
                option["pattern"]
                for option in source_schema["items"]["oneOf"]
                if "pattern" in option
            ]
            self.assertTrue(patterns)
            self.assertFalse(
                any(
                    re.fullmatch(pattern, "~/.codex/plugins/cache/../../auth/package.json")
                    for pattern in patterns
                )
            )

        observed_rule = next(
            rule for rule in coverage_rules
            if isinstance(rule.get("if"), dict) and "allOf" in rule["if"]
        )
        constrained_types = {
            branch["properties"]["asset_type"].get("const")
            for branch in observed_rule["if"]["allOf"][1]["anyOf"]
        }
        self.assertNotIn("mcp", constrained_types)
        self.assertNotIn("sdk", constrained_types)

    def test_production_overlay_is_lossless_unique_and_complete(self) -> None:
        overlay_path = ROOT / "registry" / "chinese_metadata.json"
        raw = json.loads(overlay_path.read_text(encoding="utf-8"))
        schema = json.loads(
            (ROOT / "schemas" / "chinese_metadata.schema.json").read_text(encoding="utf-8")
        )
        item_schema = schema["properties"]["items"]["items"]
        required = set(item_schema["required"])
        allowed = set(item_schema["properties"])
        raw_items = raw["items"]
        raw_ids = [item["asset_id"] for item in raw_items]
        host_overlay_items = [item for item in raw_items if "translated_from_hash" in item]

        self.assertEqual(len(raw_ids), len(set(raw_ids)))
        for item in raw_items:
            self.assertTrue(required.issubset(item))
            self.assertTrue(set(item).issubset(allowed))

        config = json.loads((ROOT / "registry" / "scan_config.json").read_text(encoding="utf-8"))
        errors: list[dict[str, object]] = []
        loaded = _load_overlay(overlay_path, config["limits"], errors)

        self.assertEqual(errors, [])
        self.assertEqual(len(loaded), len(host_overlay_items))
        self.assertEqual(set(loaded), {item["asset_id"] for item in host_overlay_items})
        for item in loaded.values():
            self.assertTrue(item["coverage_complete"])
            self.assertRegex(item["translated_from_hash"], r"^[a-f0-9]{64}$")
            self.assertIn(item["status"], {"ai_draft", "reviewed", "stale"})

    def test_ephemeral_codex_system_skills_are_not_persistent_assets(self) -> None:
        destination = self.home / ".codex" / "skills" / ".system" / "runtime-only"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(FIXTURES / "sample_skill", destination)

        payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)

        self.assertEqual(payload["items"], [])
        scope = next(
            row for row in payload["scan_scope"]["roots"]
            if row["host_id"] == "codex" and row["asset_type"] == "skill"
        )
        self.assertEqual(scope["transient_entries_excluded"], 1)
        self.assertIn("ephemeral_system_managed_skills", payload["scan_scope"]["excluded"])

    def test_bounded_depth_is_reported_in_root_scope(self) -> None:
        config = json.loads((ROOT / "registry" / "scan_config.json").read_text(encoding="utf-8"))
        max_depth = int(config["limits"]["max_depth"])
        directory = self.home / ".codex" / "skills"
        for index in range(max_depth + 1):
            directory = directory / f"level-{index}"
        directory.mkdir(parents=True)
        shutil.copy2(FIXTURES / "sample_skill" / "SKILL.md", directory / "SKILL.md")

        payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)

        scope = next(
            row for row in payload["scan_scope"]["roots"]
            if row["host_id"] == "codex" and row["asset_type"] == "skill"
        )
        self.assertEqual(scope["status"], "observed")
        self.assertEqual(scope["depth_limited_entries"], 1)
        self.assertEqual(payload["items"], [])

    def test_exact_skill_duplicate_merges_into_host_bindings(self) -> None:
        source = self._copy_skill("codex")
        claude_root = self.home / ".claude" / "skills"
        claude_root.mkdir(parents=True)
        os.symlink(source, claude_root / "sample-skill", target_is_directory=True)

        payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        skills = [item for item in payload["items"] if item["type"] == "skill"]
        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0]["asset_id"], "skill:sample-skill")
        self.assertEqual(skills[0]["duplicate"]["occurrence_count"], 2)
        self.assertEqual({row["host_id"] for row in skills[0]["host_bindings"]}, {"codex", "claude"})
        self.assertEqual(payload["summary"]["asset_count"], 1)
        self.assertEqual(payload["summary"]["host_binding_count"], 2)

    def test_unapproved_package_files_make_identity_incomplete_without_body_reads(self) -> None:
        first = self._copy_skill("codex")
        second = self._copy_skill("hermes")
        (first / "worker.py").write_text("print('first')\n", encoding="utf-8")
        (second / "worker.py").write_text("print('second')\n", encoding="utf-8")

        blocked = {first / "worker.py", second / "worker.py"}
        real_open = os.open

        def deny_worker_body(path, flags, *args, **kwargs):
            if Path(path) in blocked:
                raise AssertionError("unapproved worker body was opened")
            return real_open(path, flags, *args, **kwargs)

        with mock.patch("toolbox_scan.os.open", side_effect=deny_worker_body):
            payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        skills = [item for item in payload["items"] if item["type"] == "skill"]

        self.assertEqual(len(skills), 1)
        self.assertEqual(skills[0]["duplicate"]["occurrence_count"], 2)
        self.assertFalse(skills[0]["duplicate"]["exact_duplicate"])
        self.assertFalse(skills[0]["duplicate"]["package_divergence"])
        self.assertEqual(skills[0]["source"]["package_fingerprint_status"], "incomplete")
        self.assertEqual(skills[0]["source"]["package_fingerprints"], [])
        variants = skills[0]["source"]["package_variants"]
        self.assertEqual({row["host_id"] for row in variants}, {"codex", "hermes"})
        self.assertTrue(all(row["package_fingerprint"] is None for row in variants))
        self.assertNotIn("package_divergence", {row["code"] for row in payload["health_findings"]})

    def test_dependency_directory_is_not_read_and_locks_exact_identity(self) -> None:
        first = self._copy_skill("codex")
        second = self._copy_skill("hermes")
        (first / "node_modules" / "runner").mkdir(parents=True)
        (second / "node_modules" / "runner").mkdir(parents=True)
        (first / "node_modules" / "runner" / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")
        (second / "node_modules" / "runner" / "index.js").write_text("module.exports = 2;\n", encoding="utf-8")

        blocked = {
            first / "node_modules" / "runner" / "index.js",
            second / "node_modules" / "runner" / "index.js",
        }
        real_open = os.open

        def deny_dependency_body(path, flags, *args, **kwargs):
            if Path(path) in blocked:
                raise AssertionError("dependency body was opened")
            return real_open(path, flags, *args, **kwargs)

        with mock.patch("toolbox_scan.os.open", side_effect=deny_dependency_body):
            payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        item = next(row for row in payload["items"] if row["type"] == "skill")

        self.assertFalse(item["duplicate"]["package_divergence"])
        self.assertEqual(item["source"]["package_fingerprint_status"], "incomplete")
        self.assertEqual(item["source"]["package_fingerprints"], [])

    def test_sensitive_package_path_is_never_read_and_locks_exact_deduplication(self) -> None:
        skill = self._copy_skill("codex")
        sensitive_name = "token-sk_live_EXPOSED.txt"
        (skill / sensitive_name).write_text("API_TOKEN=must-not-leak\n", encoding="utf-8")

        payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        item = payload["items"][0]
        serialized = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(item["source"]["package_fingerprint_status"], "incomplete")
        self.assertEqual(item["trust"]["locked_reason"], "package_fingerprint_incomplete")
        self.assertIn("package_fingerprint_incomplete", {row["code"] for row in payload["health_findings"]})
        self.assertNotIn("must-not-leak", serialized)
        self.assertNotIn(sensitive_name, serialized)

    def test_special_package_entry_reason_does_not_leak_its_name(self) -> None:
        skill = self._copy_skill("codex")
        sensitive_name = "token-sk_live_FIFO"
        os.mkfifo(skill / sensitive_name)

        payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        serialized = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn(sensitive_name, serialized)
        self.assertIn("unsupported_special_package_entry", serialized)
        self.assertEqual(
            payload["items"][0]["source"]["package_fingerprint_status"], "incomplete"
        )

    def test_versioned_codex_plugin_identity_covers_the_outer_package(self) -> None:
        version_root = (
            self.home / ".codex" / "plugins" / "cache" / "fixture" / "outer-plugin" / "1.0.0"
        )
        manifest = version_root / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            '{"name":"outer-plugin","version":"1.0.0"}\n', encoding="utf-8"
        )

        clean = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        clean_plugin = next(item for item in clean["items"] if item["type"] == "plugin")
        self.assertEqual(clean_plugin["source"]["package_fingerprint_status"], "complete")

        version_root.joinpath("settings.yaml").write_text(
            "token: must-not-leak-outer-setting\n", encoding="utf-8"
        )
        guarded = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        guarded_plugin = next(item for item in guarded["items"] if item["type"] == "plugin")
        serialized = json.dumps(guarded, ensure_ascii=False)
        self.assertEqual(guarded_plugin["source"]["package_fingerprint_status"], "incomplete")
        self.assertNotIn("settings.yaml", serialized)
        self.assertNotIn("must-not-leak-outer-setting", serialized)

    def test_mcp_free_text_is_never_projected_even_without_sensitive_keys(self) -> None:
        plugin = self._copy_codex_observation_plugin()
        sensitive_title = "sk-" + "fixture-title-must-not-leak"
        plugin.joinpath(".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "safe-server-id": {
                            "title": sensitive_title,
                            "description": "free-text-secret-must-not-leak",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        serialized = json.dumps(payload, ensure_ascii=False)

        self.assertIn("safe-server-id", serialized)
        self.assertNotIn(sensitive_title, serialized)
        self.assertNotIn("free-text-secret-must-not-leak", serialized)

    def test_git_metadata_and_executable_binary_lock_package_without_body_output(self) -> None:
        git_skill = self._copy_skill("codex", name="git-bearing")
        git_skill.joinpath("SKILL.md").write_text("---\nname: git-bearing\n---\nBody\n", encoding="utf-8")
        git_skill.joinpath(".git").mkdir()
        git_skill.joinpath(".git", "config").write_text("token=git-secret-must-not-leak\n", encoding="utf-8")
        binary_skill = self._copy_skill("hermes", name="binary-bearing")
        binary_skill.joinpath("SKILL.md").write_text("---\nname: binary-bearing\n---\nBody\n", encoding="utf-8")
        binary_skill.joinpath("helper.dll").write_bytes(b"binary-secret-must-not-leak")

        payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        serialized = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(len(payload["items"]), 2)
        self.assertTrue(all(item["source"]["package_fingerprint_status"] == "incomplete" for item in payload["items"]))
        self.assertNotIn("git-secret-must-not-leak", serialized)
        self.assertNotIn("binary-secret-must-not-leak", serialized)
        reasons = " ".join(row.get("reason", "") for row in payload["health_findings"])
        self.assertIn("unapproved directory", reasons)
        self.assertIn("unapproved file", reasons)

    def test_sensitive_config_binary_and_extensionless_executable_bodies_are_not_opened(self) -> None:
        cases = (
            ("auth.json", b'{"token":"must-not-read-auth"}', False),
            ("config.toml", b'token = "must-not-read-config"\n', False),
            ("preview.pdf", b"%PDF-must-not-read", False),
            ("runner", b"must-not-read-extensionless-binary", True),
            ("settings.yaml", b"token: must-not-read-generic-config", False),
        )
        real_open = os.open
        for name, body, executable in cases:
            with self.subTest(name=name):
                shutil.rmtree(self.home)
                self.home.mkdir()
                skill = self._copy_skill("codex")
                blocked = skill / name
                blocked.write_bytes(body)
                if executable:
                    blocked.chmod(0o700)

                def deny_blocked_read(path, flags, *args, **kwargs):
                    if Path(path) == blocked:
                        raise AssertionError(f"blocked package body was opened: {name}")
                    return real_open(path, flags, *args, **kwargs)

                with mock.patch("toolbox_scan.os.open", side_effect=deny_blocked_read):
                    payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)
                item = next(row for row in payload["items"] if row["type"] == "skill")
                self.assertEqual(item["source"]["package_fingerprint_status"], "incomplete")
                self.assertNotIn(body.decode("utf-8", errors="ignore"), json.dumps(payload))

    def test_outside_and_broken_symlinks_are_not_followed(self) -> None:
        codex_root = self.home / ".codex" / "skills"
        codex_root.mkdir(parents=True)
        outside = Path(self.temp.name) / "outside"
        shutil.copytree(FIXTURES / "sample_skill", outside)
        os.symlink(outside, codex_root / "outside", target_is_directory=True)
        os.symlink(codex_root / "missing", codex_root / "broken", target_is_directory=True)

        payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        codes = {finding["code"] for finding in payload["health_findings"]}
        self.assertIn("symlink_outside_allowlist", codes)
        self.assertIn("symlink_broken", codes)
        self.assertEqual(payload["items"], [])

    def test_host_ancestor_symlink_is_blocked_before_scanning(self) -> None:
        outside_codex = Path(self.temp.name) / "outside-codex"
        outside_skill = outside_codex / "skills" / "sample-skill"
        outside_skill.parent.mkdir(parents=True)
        shutil.copytree(FIXTURES / "sample_skill", outside_skill)
        os.symlink(outside_codex, self.home / ".codex", target_is_directory=True)

        payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)

        codex_skill_scope = next(
            row
            for row in payload["scan_scope"]["roots"]
            if row["host_id"] == "codex" and row["asset_type"] == "skill"
        )
        self.assertEqual(codex_skill_scope["status"], "blocked_symlink_root")
        self.assertEqual(payload["items"], [])
        self.assertIn(
            "symlink_root_blocked",
            {row["code"] for row in payload["health_findings"]},
        )

    def test_manifest_read_rejects_package_directory_replaced_by_outside_symlink(self) -> None:
        victim = self._copy_skill("codex", name="victim")
        outside = Path(self.temp.name) / "outside-victim"
        shutil.copytree(FIXTURES / "sample_skill", outside)
        outside.joinpath("SKILL.md").write_text(
            "---\nname: OUTSIDE_SENTINEL\ndescription: OUTSIDE_SENTINEL\n---\n",
            encoding="utf-8",
        )
        backup = victim.with_name("victim-original")
        real_read = __import__("toolbox_scan")._read_regular_file_limited_beneath
        swapped = False

        def swap_before_read(root, path, max_bytes):
            nonlocal swapped
            if not swapped and Path(path) == victim / "SKILL.md":
                victim.rename(backup)
                os.symlink(outside, victim, target_is_directory=True)
                swapped = True
            return real_read(root, path, max_bytes)

        with mock.patch(
            "toolbox_scan._read_regular_file_limited_beneath",
            side_effect=swap_before_read,
        ):
            payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)

        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertTrue(swapped)
        self.assertNotIn("OUTSIDE_SENTINEL", serialized)
        self.assertEqual([item for item in payload["items"] if item["type"] == "skill"], [])
        self.assertIn("manifest_rejected", {row["code"] for row in payload["scan_errors"]})

    def test_directory_enumeration_rejects_outside_symlink_replacement(self) -> None:
        victim = self._copy_skill("codex", name="enumeration-victim")
        outside = Path(self.temp.name) / "outside-enumeration"
        outside.mkdir()
        outside.joinpath("OUTSIDE_ENUMERATION_SENTINEL").write_text(
            "must-not-enumerate", encoding="utf-8"
        )
        backup = victim.with_name("enumeration-victim-original")
        import toolbox_scan

        real_open_directory = toolbox_scan._open_directory_beneath
        real_scandir = os.scandir
        outside_info = outside.stat()
        swapped = False

        def swap_before_directory_open(root, path):
            nonlocal swapped
            if not swapped and Path(path) == victim:
                victim.rename(backup)
                os.symlink(outside, victim, target_is_directory=True)
                swapped = True
            return real_open_directory(root, path)

        def reject_outside_scandir(path):
            if isinstance(path, int):
                info = os.fstat(path)
                if (info.st_dev, info.st_ino) == (outside_info.st_dev, outside_info.st_ino):
                    raise AssertionError("outside directory was enumerated")
            return real_scandir(path)

        with mock.patch(
            "toolbox_scan._open_directory_beneath",
            side_effect=swap_before_directory_open,
        ), mock.patch("toolbox_scan.os.scandir", side_effect=reject_outside_scandir):
            payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)

        self.assertTrue(swapped)
        self.assertEqual([item for item in payload["items"] if item["type"] == "skill"], [])
        self.assertIn("directory_unreadable", {row["code"] for row in payload["scan_errors"]})

    def test_package_fingerprint_rejects_directory_replaced_after_manifest_parse(self) -> None:
        victim = self._copy_skill("codex", name="fingerprint-victim")
        outside = Path(self.temp.name) / "outside-fingerprint"
        shutil.copytree(FIXTURES / "sample_skill", outside)
        outside.joinpath("SKILL.md").write_text(
            "---\nname: OUTSIDE_FINGERPRINT_SENTINEL\n---\n", encoding="utf-8"
        )
        backup = victim.with_name("fingerprint-victim-original")
        import toolbox_scan

        real_compute = toolbox_scan.compute_package_fingerprint
        swapped = False

        def swap_before_fingerprint(root, *args, **kwargs):
            nonlocal swapped
            if not swapped and Path(root) == victim:
                victim.rename(backup)
                os.symlink(outside, victim, target_is_directory=True)
                swapped = True
            return real_compute(root, *args, **kwargs)

        with mock.patch(
            "toolbox_scan.compute_package_fingerprint",
            side_effect=swap_before_fingerprint,
        ):
            payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)

        serialized = json.dumps(payload, ensure_ascii=False)
        item = next(entry for entry in payload["items"] if entry["type"] == "skill")
        self.assertTrue(swapped)
        self.assertEqual(item["source"]["package_fingerprint_status"], "incomplete")
        self.assertNotIn("OUTSIDE_FINGERPRINT_SENTINEL", serialized)

    def test_cli_launcher_ancestor_symlink_is_rejected(self) -> None:
        target = self.home / ".codex" / "plugins" / ".plugin-appserver" / "codex"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"must-not-read-cli")
        target.chmod(0o700)
        outside_local = Path(self.temp.name) / "outside-local"
        outside_local.joinpath("bin").mkdir(parents=True)
        os.symlink(target, outside_local / "bin" / "codex")
        os.symlink(outside_local, self.home / ".local", target_is_directory=True)

        payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)

        cli = next(
            row for row in payload["scan_scope"]["coverage"]
            if row["host_id"] == "codex" and row["asset_type"] == "cli"
        )
        self.assertEqual(cli["status"], "error")
        self.assertEqual([item for item in payload["items"] if item["type"] == "cli"], [])
        self.assertIn("metadata_source_rejected", {row["code"] for row in payload["scan_errors"]})

    def test_cli_target_ancestor_symlink_is_rejected_before_target_lstat(self) -> None:
        outside = Path(self.temp.name) / "outside-appserver"
        outside.mkdir()
        outside.joinpath("codex").write_bytes(b"must-not-inspect-outside-target")
        outside.joinpath("codex").chmod(0o700)
        target_parent = self.home / ".codex" / "plugins" / ".plugin-appserver"
        target_parent.parent.mkdir(parents=True)
        os.symlink(outside, target_parent, target_is_directory=True)
        target = target_parent / "codex"
        launcher = self.home / ".local" / "bin" / "codex"
        launcher.parent.mkdir(parents=True)
        os.symlink(target, launcher)
        real_lstat = os.lstat
        calls: list[Path] = []

        def track_lstat(path, *args, **kwargs):
            calls.append(Path(path))
            return real_lstat(path, *args, **kwargs)

        with mock.patch("toolbox_scan.os.lstat", side_effect=track_lstat):
            payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)

        self.assertNotIn(target, calls)
        self.assertEqual([item for item in payload["items"] if item["type"] == "cli"], [])
        cli = next(
            row for row in payload["scan_scope"]["coverage"]
            if row["host_id"] == "codex" and row["asset_type"] == "cli"
        )
        self.assertEqual(cli["status"], "partial")

    def test_cli_target_parent_replacement_is_rejected_by_anchored_metadata_read(self) -> None:
        target = self._install_fake_codex_cli()
        target_parent = target.parent
        backup = target_parent.with_name(".plugin-appserver-original")
        outside = Path(self.temp.name) / "outside-cli-parent"
        outside.mkdir()
        outside.joinpath("codex").write_bytes(b"OUTSIDE_CLI_SENTINEL")
        outside.joinpath("codex").chmod(0o700)
        import toolbox_scan

        real_open_directory = toolbox_scan._open_directory_beneath
        swapped = False

        def swap_before_target_parent(root, path):
            nonlocal swapped
            if not swapped and Path(path) == target_parent:
                target_parent.rename(backup)
                os.symlink(outside, target_parent, target_is_directory=True)
                swapped = True
            return real_open_directory(root, path)

        with mock.patch(
            "toolbox_scan._open_directory_beneath",
            side_effect=swap_before_target_parent,
        ):
            payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)

        cli = next(
            row for row in payload["scan_scope"]["coverage"]
            if row["host_id"] == "codex" and row["asset_type"] == "cli"
        )
        self.assertTrue(swapped)
        self.assertEqual(cli["status"], "partial")
        self.assertEqual([item for item in payload["items"] if item["type"] == "cli"], [])
        self.assertNotIn("OUTSIDE_CLI_SENTINEL", json.dumps(payload))

    def test_sdk_symlink_component_is_rejected_without_outside_read(self) -> None:
        plugin = self._copy_codex_observation_plugin()
        shutil.rmtree(plugin / "node_modules")
        outside = Path(self.temp.name) / "outside-sdk"
        outside_sdk = outside / "@modelcontextprotocol" / "sdk"
        outside_sdk.mkdir(parents=True)
        outside_sdk.joinpath("package.json").write_text(
            '{"name":"@modelcontextprotocol/sdk","description":"must-not-read-outside-sdk"}',
            encoding="utf-8",
        )
        os.symlink(outside, plugin / "node_modules", target_is_directory=True)

        payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        serialized = json.dumps(payload, ensure_ascii=False)
        sdk = next(
            row for row in payload["scan_scope"]["coverage"]
            if row["host_id"] == "codex" and row["asset_type"] == "sdk"
        )

        self.assertEqual(sdk["status"], "partial")
        self.assertEqual([item for item in payload["items"] if item["type"] == "sdk"], [])
        self.assertNotIn("must-not-read-outside-sdk", serialized)

    def test_scan_config_roots_and_budgets_are_frozen(self) -> None:
        bundle = Path(self.temp.name) / "bundle"
        registry = bundle / "registry"
        registry.mkdir(parents=True)
        config = json.loads((ROOT / "registry" / "scan_config.json").read_text(encoding="utf-8"))
        config["hosts"][0]["skill_roots"] = ["{home}/../outside"]
        registry.joinpath("scan_config.json").write_text(json.dumps(config), encoding="utf-8")
        shutil.copy2(ROOT / "registry" / "chinese_metadata.json", registry / "chinese_metadata.json")

        with mock.patch("toolbox_scan._bundle_root", return_value=bundle):
            with self.assertRaisesRegex(ValueError, "unsafe host root configuration"):
                build_toolbox_payload(home=self.home, now=FIXED_NOW)

    def test_plugin_manifest_is_allowlisted_and_sensitive_fields_do_not_leak(self) -> None:
        self._copy_plugin()
        payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        plugins = [item for item in payload["items"] if item["type"] == "plugin"]
        self.assertEqual(len(plugins), 1)
        plugin = plugins[0]
        self.assertEqual(plugin["source_name"], "sample-plugin")
        self.assertEqual(plugin["source"]["display_name"], "Sample Plugin Display")
        self.assertEqual(plugin["host_bindings"][0]["presence"], "cached")
        self.assertEqual(plugin["host_bindings"][0]["provisioning"], "cache")
        self.assertEqual({part["type"] for part in plugin["composition"]}, {"skill", "mcp", "cli"})
        serialized = json.dumps(plugin, ensure_ascii=False)
        self.assertNotIn("postinstall", serialized)
        self.assertNotIn("credentials", serialized)
        self.assertNotIn("must-not-be-read", serialized)
        self.assertNotIn("must-not-enter-payload", serialized)
        self.assertTrue(set(payload["summary"]["counts_by_type"]).issubset(ASSET_TYPES))

    def test_chinese_overlay_becomes_stale_when_source_hash_changes(self) -> None:
        skill = self._copy_skill("codex")
        overlay = Path(self.temp.name) / "overlay.json"
        overlay.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "items": [
                        {
                            "asset_id": "skill:sample-skill",
                            "zh_name": "示例技能",
                            "source_name": "sample-skill",
                            "summary": "总结用户提供的文档。",
                            "use_cases": ["需要整理文档时"],
                            "not_for": ["没有来源的事实判断"],
                            "examples": ["请总结这份文档并保留来源。"],
                            "synonyms": ["文档总结"],
                            "source_ref": "fixture",
                            "translated_from_hash": "0" * 64,
                            "status": "reviewed",
                            "generated_at": "2026-07-20T00:00:00Z",
                            "reviewed_at": "2026-07-20T00:00:00Z",
                            "evidence": ["fixture review"]
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        before = skill.joinpath("SKILL.md").read_bytes()
        payload = build_toolbox_payload(home=self.home, overlay_path=overlay, now=FIXED_NOW)
        after = skill.joinpath("SKILL.md").read_bytes()
        item = payload["items"][0]
        self.assertEqual(before, after)
        self.assertEqual(item["localization"]["status"], "stale")
        self.assertIn("localization_stale", {row["code"] for row in payload["health_findings"]})

    def test_current_overlay_can_be_complete_without_modifying_source(self) -> None:
        skill = self._copy_skill("codex")
        fingerprint = hashlib.sha256((skill / "SKILL.md").read_bytes()).hexdigest()
        overlay = Path(self.temp.name) / "overlay.json"
        overlay.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "items": [
                        {
                            "asset_id": "skill:sample-skill",
                            "zh_name": "示例技能",
                            "source_name": "sample-skill",
                            "summary": "总结用户提供的文档。",
                            "use_cases": ["需要整理文档时"],
                            "not_for": ["不适合没有来源的事实判断"],
                            "examples": ["请总结这份文档并保留来源。"],
                            "synonyms": [],
                            "source_ref": "fixture",
                            "translated_from_hash": fingerprint,
                            "status": "reviewed",
                            "generated_at": "2026-07-20T00:00:00Z",
                            "reviewed_at": "2026-07-20T00:00:00Z",
                            "evidence": []
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        payload = build_toolbox_payload(home=self.home, overlay_path=overlay, now=FIXED_NOW)
        item = payload["items"][0]
        self.assertEqual(item["localization"]["status"], "reviewed")
        self.assertTrue(item["localization"]["coverage_complete"])
        self.assertEqual(payload["summary"]["localized_skill_count"], 1)

    def test_malformed_overlay_lists_fail_closed_to_missing_localization(self) -> None:
        skill = self._copy_skill("codex")
        fingerprint = hashlib.sha256((skill / "SKILL.md").read_bytes()).hexdigest()
        overlay = Path(self.temp.name) / "malformed-overlay.json"
        overlay.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "items": [
                        {
                            "asset_id": "skill:sample-skill",
                            "zh_name": "示例技能",
                            "source_name": "sample-skill",
                            "summary": "不得把字符串拆成伪数组。",
                            "use_cases": "not-a-list",
                            "not_for": None,
                            "examples": 7,
                            "synonyms": {},
                            "source_ref": "fixture",
                            "translated_from_hash": fingerprint,
                            "status": "reviewed",
                            "generated_at": "2026-07-20T00:00:00Z",
                            "reviewed_at": None,
                            "evidence": "not-a-list",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        payload = build_toolbox_payload(home=self.home, overlay_path=overlay, now=FIXED_NOW)
        item = next(row for row in payload["items"] if row["type"] == "skill")
        self.assertEqual(item["localization"]["status"], "missing")
        self.assertFalse(item["localization"]["coverage_complete"])

    def test_refreshing_localization_hash_keeps_unread_packages_locked_and_sources_untouched(self) -> None:
        first = self._copy_skill("codex")
        second = self._copy_skill("hermes")
        (first / "worker.py").write_text("print('codex')\n", encoding="utf-8")
        (second / "worker.py").write_text("print('hermes')\n", encoding="utf-8")
        manifest_hash = hashlib.sha256((first / "SKILL.md").read_bytes()).hexdigest()

        def write_overlay(path: Path, translated_hash: str) -> bytes:
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "items": [
                            {
                                "asset_id": "skill:sample-skill",
                                "zh_name": "示例技能",
                                "source_name": "sample-skill",
                                "summary": "总结用户明确提供的文档并保留来源。",
                                "use_cases": ["需要整理用户提供的文档时"],
                                "not_for": ["没有用户提供文档的任务"],
                                "examples": ["请总结这份文档并保留来源。"],
                                "synonyms": ["文档总结"],
                                "source_ref": "fixture",
                                "translated_from_hash": translated_hash,
                                "status": "reviewed",
                                "generated_at": "2026-08-06T00:00:00Z",
                                "reviewed_at": "2026-08-06T00:00:00Z",
                                "evidence": ["sample fixture"],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return path.read_bytes()

        before_sources = self._snapshot_tree()
        stale_overlay = Path(self.temp.name) / "stale-overlay.json"
        stale_overlay_bytes = write_overlay(stale_overlay, "0" * 64)
        stale = build_toolbox_payload(home=self.home, overlay_path=stale_overlay, now=FIXED_NOW)
        self.assertEqual(stale_overlay.read_bytes(), stale_overlay_bytes)
        self.assertEqual(self._snapshot_tree(), before_sources)
        self.assertEqual(stale["summary"]["stale_localization_count"], 1)
        self.assertIn("localization_stale", {row["code"] for row in stale["health_findings"]})

        current_overlay = Path(self.temp.name) / "current-overlay.json"
        current_overlay_bytes = write_overlay(current_overlay, manifest_hash)
        current = build_toolbox_payload(home=self.home, overlay_path=current_overlay, now=FIXED_NOW)
        self.assertEqual(current_overlay.read_bytes(), current_overlay_bytes)
        self.assertEqual(self._snapshot_tree(), before_sources)

        item = current["items"][0]
        self.assertEqual(item["asset_id"], stale["items"][0]["asset_id"])
        self.assertEqual(current["summary"]["stale_localization_count"], 0)
        self.assertNotIn("localization_stale", {row["code"] for row in current["health_findings"]})
        self.assertNotIn("package_divergence", {row["code"] for row in current["health_findings"]})
        self.assertFalse(item["duplicate"]["package_divergence"])
        self.assertEqual(item["source"]["package_fingerprint_status"], "incomplete")
        self.assertEqual(item["trust"]["action"], "locked")
        self.assertEqual(item["trust"]["locked_reason"], "package_fingerprint_incomplete")
        self.assertTrue(
            all(
                binding["action"] == "locked" and binding["effective"] == "unverified"
                for binding in item["host_bindings"]
            )
        )
        self.assertEqual(current["mode"], "observe")
        self.assertTrue(all(value is False for value in current["scan_scope"]["safety"].values()))

    def test_chinese_overlay_also_applies_to_plugin_cards(self) -> None:
        plugin = self._copy_plugin()
        fingerprint = hashlib.sha256((plugin / "plugin.json").read_bytes()).hexdigest()
        overlay = Path(self.temp.name) / "plugin-overlay.json"
        overlay.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "items": [
                        {
                            "asset_id": "plugin:sample-plugin",
                            "zh_name": "示例插件",
                            "source_name": "sample-plugin",
                            "summary": "展示安全插件清单的中文说明。",
                            "use_cases": ["核对插件 manifest 和组成时"],
                            "not_for": ["把缓存可见误报成已经安装或启用"],
                            "examples": ["请只读检查这个插件清单并说明它包含哪些能力。"],
                            "synonyms": ["插件清单"],
                            "source_ref": "fixture",
                            "translated_from_hash": fingerprint,
                            "status": "ai_draft",
                            "generated_at": "2026-07-20T00:00:00Z",
                            "reviewed_at": None,
                            "evidence": ["sample plugin fixture"],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        payload = build_toolbox_payload(home=self.home, overlay_path=overlay, now=FIXED_NOW)
        item = next(row for row in payload["items"] if row["type"] == "plugin")
        self.assertEqual(item["localization"]["zh_name"], "示例插件")
        self.assertEqual(item["localization"]["status"], "ai_draft")
        self.assertTrue(item["localization"]["coverage_complete"])
        self.assertNotIn("localization_missing", {row["code"] for row in payload["health_findings"]})

    def test_invalid_utf8_and_oversized_manifests_fail_closed(self) -> None:
        invalid = self.home / ".codex" / "skills" / "invalid"
        invalid.mkdir(parents=True)
        (invalid / "SKILL.md").write_bytes(b"\xff\xfe\xfd")
        oversized = self.home / ".codex" / "skills" / "oversized"
        oversized.mkdir()
        (oversized / "SKILL.md").write_bytes(b"x" * 262145)

        payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        self.assertEqual(payload["items"], [])
        self.assertEqual([row["code"] for row in payload["scan_errors"]].count("manifest_rejected"), 2)

    def test_malicious_metadata_remains_inert_data_and_no_new_types_appear(self) -> None:
        self._copy_skill("codex", fixture="malicious_skill", name="malicious")
        payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        self.assertEqual(len(payload["items"]), 1)
        item = payload["items"][0]
        self.assertEqual(item["source_name"], "<img src=x onerror=alert(1)>")
        self.assertIn("</script>", item["description"])
        self.assertIn(item["type"], ASSET_TYPES)
        self.assertNotIn("app", {entry["type"] for entry in payload["items"]})

    def test_name_collision_is_not_silently_deduplicated(self) -> None:
        first = self._copy_skill("codex")
        second = self._copy_skill("hermes")
        (second / "SKILL.md").write_text(
            (first / "SKILL.md").read_text(encoding="utf-8") + "\nDifferent content.\n",
            encoding="utf-8",
        )
        payload = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        skills = [item for item in payload["items"] if item["type"] == "skill"]
        self.assertEqual(len(skills), 2)
        self.assertTrue(all(item["duplicate"]["name_collision"] for item in skills))
        self.assertTrue(all(":variant-" in item["asset_id"] for item in skills))
        self.assertEqual(len({item["asset_id"] for item in skills}), 2)

    def test_variant_asset_id_is_stable_when_a_host_binding_is_added(self) -> None:
        first = self._copy_skill("codex")
        second = self._copy_skill("hermes")
        (second / "SKILL.md").write_text(
            (first / "SKILL.md").read_text(encoding="utf-8") + "\nDifferent content.\n",
            encoding="utf-8",
        )
        before = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        before_ids = {item["fingerprint"]: item["asset_id"] for item in before["items"]}

        claude_root = self.home / ".claude" / "skills"
        claude_root.mkdir(parents=True)
        os.symlink(first, claude_root / "sample-skill", target_is_directory=True)
        after = build_toolbox_payload(home=self.home, now=FIXED_NOW)
        after_ids = {item["fingerprint"]: item["asset_id"] for item in after["items"]}

        self.assertEqual(before_ids, after_ids)

    def test_localization_source_anchor_keeps_variant_id_and_marks_content_stale(self) -> None:
        first = self._copy_skill("codex")
        second = self._copy_skill("hermes")
        (second / "SKILL.md").write_text(
            (first / "SKILL.md").read_text(encoding="utf-8") + "\nDifferent content.\n",
            encoding="utf-8",
        )
        empty_overlay = Path(self.temp.name) / "empty-overlay.json"
        empty_overlay.write_text('{"schema_version": 1, "items": []}\n', encoding="utf-8")
        before = build_toolbox_payload(home=self.home, overlay_path=empty_overlay, now=FIXED_NOW)
        before_items = [item for item in before["items"] if item["type"] == "skill"]

        overlay_items = []
        for index, item in enumerate(before_items, 1):
            overlay_items.append(
                {
                    "asset_id": item["asset_id"],
                    "zh_name": f"示例技能变体 {index}",
                    "source_name": item["source_name"],
                    "summary": "用于验证稳定身份。",
                    "use_cases": ["内容更新后需要保留整理身份时"],
                    "not_for": ["来源已经完全更换且无法确认继承关系时"],
                    "examples": ["请核对这个技能更新前后的差异。"],
                    "synonyms": [],
                    "source_ref": item["source"]["ref"],
                    "translated_from_hash": item["manifest_fingerprint"],
                    "status": "ai_draft",
                    "generated_at": "2026-07-20T00:00:00Z",
                    "reviewed_at": None,
                    "evidence": ["fixture identity anchor"],
                }
            )
        overlay = Path(self.temp.name) / "anchored-overlay.json"
        overlay.write_text(
            json.dumps({"schema_version": 1, "items": overlay_items}, ensure_ascii=False),
            encoding="utf-8",
        )
        codex_before = next(
            item for item in before_items
            if any(binding["host_id"] == "codex" for binding in item["host_bindings"])
        )
        first.joinpath("SKILL.md").write_text(
            first.joinpath("SKILL.md").read_text(encoding="utf-8") + "\nUpdated in place.\n",
            encoding="utf-8",
        )

        after = build_toolbox_payload(home=self.home, overlay_path=overlay, now=FIXED_NOW)
        after_items = [item for item in after["items"] if item["type"] == "skill"]

        self.assertEqual({item["asset_id"] for item in after_items}, {item["asset_id"] for item in before_items})
        changed = next(item for item in after_items if item["asset_id"] == codex_before["asset_id"])
        self.assertEqual(changed["localization"]["status"], "stale")

        shutil.rmtree(second)
        without_collision = build_toolbox_payload(home=self.home, overlay_path=overlay, now=FIXED_NOW)
        remaining = [item for item in without_collision["items"] if item["type"] == "skill"]
        self.assertEqual([item["asset_id"] for item in remaining], [codex_before["asset_id"]])


if __name__ == "__main__":
    unittest.main()
