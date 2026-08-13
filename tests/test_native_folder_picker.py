from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "src" / "native_folder_picker.py"
SPEC = importlib.util.spec_from_file_location("ai_toolbox_native_folder_picker", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
picker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = picker
SPEC.loader.exec_module(picker)


class NativeFolderPickerTests(unittest.TestCase):
    def run_macos(
        self,
        completed: subprocess.CompletedProcess[str],
        *,
        target: str = picker.COLLECTION_TARGET,
    ) -> tuple[int, dict]:
        output = io.StringIO()
        with mock.patch.object(picker.subprocess, "run", return_value=completed) as run:
            with redirect_stdout(output):
                result = picker._run_macos_picker(target)
        self.run_call = run.call_args
        return result, json.loads(output.getvalue())

    def test_macos_picker_uses_fixed_standard_additions_command_without_shell(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {"selected": True, "cancelled": False, "path": "/tmp/收藏 目录"}
            ),
            stderr="ignored framework diagnostic",
        )

        result, payload = self.run_macos(completed)

        self.assertEqual(result, 0)
        self.assertEqual(payload["path"], "/tmp/收藏 目录")
        self.assertEqual(
            self.run_call.args[0],
            [
                str(picker.MACOS_OSASCRIPT_PATH),
                "-l",
                "JavaScript",
                "-e",
                picker.MACOS_PICKER_SCRIPT,
            ],
        )
        self.assertIs(self.run_call.kwargs["shell"], False)
        self.assertIn("app.activate()", picker.MACOS_PICKER_SCRIPT)
        self.assertIn("app.chooseFolder", picker.MACOS_PICKER_SCRIPT)

    def test_project_picker_uses_project_prompt_and_documents_start_location(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {"selected": True, "cancelled": False, "path": "/Users/example/Documents/project"}
            ),
            stderr="",
        )

        result, payload = self.run_macos(completed, target=picker.PROJECT_SKILL_TARGET)

        self.assertEqual(result, 0)
        self.assertTrue(payload["selected"])
        script = self.run_call.args[0][-1]
        self.assertEqual(script, picker.MACOS_PROJECT_SKILL_PICKER_SCRIPT)
        self.assertIn("documents folder", script)
        self.assertIn("项目文件夹", script)

    def test_macos_picker_maps_user_cancel_to_exact_cancel_payload(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"selected":false,"cancelled":true}\n',
            stderr="",
        )

        result, payload = self.run_macos(completed)

        self.assertEqual(result, 0)
        self.assertEqual(payload, {"selected": False, "cancelled": True})
        self.assertIn("Number(error.errorNumber) === -128", picker.MACOS_PICKER_SCRIPT)

    def test_macos_picker_rejects_invalid_or_failed_helper_output(self) -> None:
        for completed in (
            subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="failure"),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="not-json", stderr=""),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='{"selected":true,"cancelled":false,"path":"bad\\u0000path"}',
                stderr="",
            ),
        ):
            with self.subTest(completed=completed):
                result, payload = self.run_macos(completed)
                self.assertEqual(result, 2)
                self.assertEqual(payload, {"selected": False, "cancelled": False})

    def test_macos_picker_rejects_relative_paths(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"selected":true,"cancelled":false,"path":"relative/path"}',
            stderr="",
        )

        result, payload = self.run_macos(completed)

        self.assertEqual(result, 2)
        self.assertEqual(payload, {"selected": False, "cancelled": False})

    def test_main_uses_macos_provider_and_keeps_tk_as_non_macos_fallback(self) -> None:
        with self.subTest(platform="darwin"):
            with mock.patch.object(picker.sys, "platform", "darwin"), mock.patch.object(
                picker, "MACOS_OSASCRIPT_PATH", MODULE_PATH
            ), mock.patch.object(
                picker.os, "access", return_value=True
            ), mock.patch.object(
                picker, "_run_macos_picker", return_value=7
            ) as macos, mock.patch.object(picker, "_run_tk_picker", return_value=8) as tk:
                self.assertEqual(picker.main([]), 7)
                macos.assert_called_once_with(picker.COLLECTION_TARGET)
                tk.assert_not_called()

        with mock.patch.object(picker.sys, "platform", "linux"), mock.patch.object(
            picker, "_run_macos_picker", return_value=7
        ) as macos, mock.patch.object(picker, "_run_tk_picker", return_value=8) as tk:
            self.assertEqual(picker.main([]), 8)
            tk.assert_called_once_with(picker.COLLECTION_TARGET)
            macos.assert_not_called()


if __name__ == "__main__":
    unittest.main()
