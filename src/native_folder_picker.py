#!/usr/bin/env python3
"""One-shot native directory picker for the local AI-Toolbox server.

The parent server invokes this fixed helper with the same Python executable and
``shell=False``.  It emits one compact JSON object and never scans or writes the
selected directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


MACOS_OSASCRIPT_PATH = Path("/usr/bin/osascript")
MACOS_PICKER_TIMEOUT_SECONDS = 285
CANCELLED = {"selected": False, "cancelled": True}
FAILED = {"selected": False, "cancelled": False}
COLLECTION_TARGET = "collection-source"
PROJECT_SKILL_TARGET = "project-skill-source"
ALLOWED_TARGETS = frozenset({COLLECTION_TARGET, PROJECT_SKILL_TARGET})
MACOS_PICKER_SCRIPT = r"""
(() => {
  const app = Application.currentApplication();
  app.includeStandardAdditions = true;
  app.activate();
  try {
    const folder = app.chooseFolder({
      withPrompt: "选择 AI-Toolbox 本次会话要读取的收藏文件夹",
      multipleSelectionsAllowed: false,
    });
    return JSON.stringify({
      selected: true,
      cancelled: false,
      path: folder.toString(),
    });
  } catch (error) {
    if (Number(error.errorNumber) === -128) {
      return JSON.stringify({selected: false, cancelled: true});
    }
    throw error;
  }
})()
""".strip()
MACOS_PROJECT_SKILL_PICKER_SCRIPT = r"""
(() => {
  const app = Application.currentApplication();
  app.includeStandardAdditions = true;
  app.activate();
  try {
    const folder = app.chooseFolder({
      withPrompt: "选择 Documents 观察根内的具体项目文件夹",
      defaultLocation: app.pathTo("documents folder"),
      multipleSelectionsAllowed: false,
    });
    return JSON.stringify({
      selected: true,
      cancelled: false,
      path: folder.toString(),
    });
  } catch (error) {
    if (Number(error.errorNumber) === -128) {
      return JSON.stringify({selected: false, cancelled: true});
    }
    throw error;
  }
})()
""".strip()


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _validated_payload(raw: str) -> dict[str, object] | None:
    if len(raw) > 4096:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if payload == CANCELLED:
        return payload
    if (
        isinstance(payload, dict)
        and set(payload) == {"selected", "cancelled", "path"}
        and payload.get("selected") is True
        and payload.get("cancelled") is False
        and isinstance(payload.get("path"), str)
        and bool(payload["path"])
        and "\x00" not in payload["path"]
        and Path(payload["path"]).is_absolute()
    ):
        return payload
    return None


def _run_macos_picker(target: str = COLLECTION_TARGET) -> int:
    """Use macOS Standard Additions instead of the obsolete system Tk 8.5."""

    script = (
        MACOS_PROJECT_SKILL_PICKER_SCRIPT
        if target == PROJECT_SKILL_TARGET
        else MACOS_PICKER_SCRIPT
    )

    try:
        completed = subprocess.run(
            [
                str(MACOS_OSASCRIPT_PATH),
                "-l",
                "JavaScript",
                "-e",
                script,
            ],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=MACOS_PICKER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        _emit(FAILED)
        return 2
    if completed.returncode != 0:
        _emit(FAILED)
        return 2
    payload = _validated_payload(completed.stdout.strip())
    if payload is None:
        _emit(FAILED)
        return 2
    _emit(payload)
    return 0


def _run_tk_picker(target: str = COLLECTION_TARGET) -> int:
    """Cross-platform fallback for environments with a supported Tk build."""

    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        _emit(FAILED)
        return 2

    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        selected = filedialog.askdirectory(
            parent=root,
            mustexist=True,
            title=(
                "选择 Documents 观察根内的具体项目文件夹"
                if target == PROJECT_SKILL_TARGET
                else "选择 AI-Toolbox 本次会话要读取的收藏文件夹"
            ),
            **(
                {"initialdir": str(Path.home() / "Documents")}
                if target == PROJECT_SKILL_TARGET
                else {}
            ),
        )
    except Exception:
        _emit(FAILED)
        return 2
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass

    if not selected:
        _emit(CANCELLED)
        return 0
    _emit({"selected": True, "cancelled": False, "path": selected})
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        target = COLLECTION_TARGET
    elif len(args) == 2 and args[0] == "--target" and args[1] in ALLOWED_TARGETS:
        target = args[1]
    else:
        _emit(FAILED)
        return 2
    if sys.platform == "darwin":
        if (
            not MACOS_OSASCRIPT_PATH.is_file()
            or MACOS_OSASCRIPT_PATH.is_symlink()
            or not os.access(MACOS_OSASCRIPT_PATH, os.X_OK)
        ):
            _emit(FAILED)
            return 2
        return _run_macos_picker(target)
    return _run_tk_picker(target)


if __name__ == "__main__":
    raise SystemExit(main())
