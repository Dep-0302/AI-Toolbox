from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import unittest

import app


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "打开AI-Toolbox工作台.command"
IDENTITY = ROOT / "registry" / "workbench_identity.json"


class WorkbenchLauncherTests(unittest.TestCase):
    def test_launcher_and_server_share_one_exact_identity_source(self) -> None:
        row = json.loads(IDENTITY.read_text(encoding="utf-8"))
        expected_source = hashlib.sha256(os.fsencode(str(ROOT.resolve()))).hexdigest()[:16]
        self.assertEqual(
            app.WORKBENCH_IDENTITY,
            {**row, "source_id": expected_source},
        )
        source = LAUNCHER.read_text(encoding="utf-8")
        for field in ("app", "api_version", "build_id", "source_id"):
            self.assertIn(field, source)

    def test_launcher_is_executable_and_never_reuses_name_only(self) -> None:
        mode = stat.S_IMODE(LAUNCHER.stat().st_mode)
        self.assertTrue(mode & stat.S_IXUSR)
        source = LAUNCHER.read_text(encoding="utf-8")
        self.assertIn('payload.get("build_id") == build_id', source)
        self.assertIn('payload.get("source_id") == source_id', source)
        self.assertIn('[[ "$cwd" == "$ROOT_DIR" && "$listener" == "$pid" ]]', source)
        self.assertNotIn('[[ "$body" == *"ai-toolbox-workbench"* ]]', source)


if __name__ == "__main__":
    unittest.main()
