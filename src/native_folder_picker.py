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


def _run_macos_picker() -> int:
    """Use macOS Standard Additions instead of the obsolete system Tk 8.5."""

    try:
        completed = subprocess.run(
            [
                str(MACOS_OSASCRIPT_PATH),
                "-l",
                "JavaScript",
                "-e",
                MACOS_PICKER_SCRIPT,
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


def _run_tk_picker() -> int:
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
            title="选择 AI-Toolbox 本次会话要读取的收藏文件夹",
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


def main() -> int:
    if sys.platform == "darwin":
        if (
            not MACOS_OSASCRIPT_PATH.is_file()
            or MACOS_OSASCRIPT_PATH.is_symlink()
            or not os.access(MACOS_OSASCRIPT_PATH, os.X_OK)
        ):
            _emit(FAILED)
            return 2
        return _run_macos_picker()
    return _run_tk_picker()


if __name__ == "__main__":
    raise SystemExit(main())
