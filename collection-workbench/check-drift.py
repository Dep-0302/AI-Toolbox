#!/usr/bin/env python3
"""AI 技能漂移看板——有界、只读、fail-closed 扫描。

扫描四个宿主的 skills 目录，找出同名技能，报告内容分叉。扫描不跟随
任何软链接，所有文件都通过 dirfd/openat + O_NOFOLLOW 流式读取。
授权根及其祖先若是软链接则 fail closed；根内后代软链接仅记录路径警告并
跳过，绝不读取链接目标。文件竞态或任一预算耗尽时，仅写「扫描未完成」
状态页并非零退出；不会把部分扫描或越界文件正文混入报告。

用法：  python3 check-drift.py          生成 report.html
       python3 check-drift.py --open   生成并直接打开
"""
from __future__ import annotations

import argparse
import datetime
import difflib
import hashlib
import html
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

HOME = Path.home()
HOSTS = {
    "codex": HOME / ".codex" / "skills",
    "claude": HOME / ".claude" / "skills",
    "hermes": HOME / ".hermes" / "skills",
    "workbuddy": HOME / ".workbuddy" / "skills",
}
HOST_LABEL = {"codex": "Codex", "claude": "Claude", "hermes": "Hermes", "workbuddy": "WorkBuddy"}
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", ".pytest_cache"}
SKIP_FILES = {".DS_Store"}
OUT = Path(__file__).resolve().parent / "report.html"

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_SCAN_ENTRIES = 100_000
MAX_SCAN_DEPTH = 32
MAX_SCAN_SECONDS = 30.0
READ_CHUNK_BYTES = 64 * 1024


class ScanIncomplete(RuntimeError):
    """不允许降级为「跳过」的扫描异常。"""


@dataclass(frozen=True)
class ScanWarning:
    code: str
    path: str

    @property
    def message(self) -> str:
        if self.code == "symlink_skipped":
            return f"已跳过后代软链接（未跟随、未读取目标）：{self.path}"
        return f"{self.code}：{self.path}"


@dataclass
class ScanWarningLog:
    items: list[ScanWarning] = field(default_factory=list)
    _seen: set[tuple[str, str]] = field(default_factory=set)

    def add(self, code: str, path: str) -> None:
        identity = (code, path)
        if identity in self._seen:
            return
        self._seen.add(identity)
        self.items.append(ScanWarning(code, path))


@dataclass
class ScanBudget:
    max_file_bytes: int = MAX_FILE_BYTES
    max_total_bytes: int = MAX_TOTAL_BYTES
    max_entries: int = MAX_SCAN_ENTRIES
    max_depth: int = MAX_SCAN_DEPTH
    max_seconds: float = MAX_SCAN_SECONDS
    started_at: float = field(default_factory=time.monotonic)
    entries: int = 0
    total_bytes: int = 0

    def check_time(self) -> None:
        if time.monotonic() - self.started_at > self.max_seconds:
            raise ScanIncomplete(f"时间预算耗尽（>{self.max_seconds:g} 秒）")

    def check_depth(self, depth: int, label: str) -> None:
        self.check_time()
        if depth > self.max_depth:
            raise ScanIncomplete(f"目录深度超限（>{self.max_depth}）：{label}")

    def add_entry(self, label: str) -> None:
        self.check_time()
        self.entries += 1
        if self.entries > self.max_entries:
            raise ScanIncomplete(f"条目预算耗尽（>{self.max_entries}），最后位置：{label}")

    def add_bytes(self, amount: int, file_bytes: int, label: str) -> None:
        self.check_time()
        if file_bytes > self.max_file_bytes:
            raise ScanIncomplete(f"单文件预算超限（>{self.max_file_bytes} 字节）：{label}")
        self.total_bytes += amount
        if self.total_bytes > self.max_total_bytes:
            raise ScanIncomplete(f"总读取预算耗尽（>{self.max_total_bytes} 字节），最后位置：{label}")


def _dir_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _file_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _require_secure_open_features() -> None:
    required_constants = ["O_DIRECTORY", "O_NOFOLLOW"]
    missing = [name for name in required_constants if not hasattr(os, name)]
    if missing:
        raise ScanIncomplete("当前平台缺少安全打开能力：" + "、".join(missing))
    if os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd:
        raise ScanIncomplete("当前平台不支持 dirfd/openat 安全扫描")
    if os.stat not in os.supports_follow_symlinks:
        raise ScanIncomplete("当前平台不支持 follow_symlinks=False")


def _display(parts: tuple[str, ...]) -> str:
    return "/".join(parts) or "."


def _open_root_dir(root: Path) -> tuple[int | None, Path]:
    """Walk every absolute path component with openat/O_NOFOLLOW.

    ``None`` means the configured host root does not exist.  A symlink or any
    other ambiguous component is a hard failure, including ancestors.
    """
    _require_secure_open_features()
    absolute = Path(os.path.abspath(os.path.expanduser(str(root))))
    parts = absolute.parts
    fd = os.open(parts[0] if parts else os.sep, _dir_flags())
    try:
        for index, name in enumerate(parts[1:], start=1):
            label = str(Path(*parts[: index + 1]))
            try:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                os.close(fd)
                return None, absolute
            except OSError as exc:
                raise ScanIncomplete(f"根目录祖先无法检查：{label}（{exc}）") from exc
            if stat.S_ISLNK(info.st_mode):
                raise ScanIncomplete(f"根目录或祖先是软链接：{label}")
            if not stat.S_ISDIR(info.st_mode):
                raise ScanIncomplete(f"根目录祖先不是目录：{label}")
            try:
                child_fd = os.open(name, _dir_flags(), dir_fd=fd)
            except OSError as exc:
                raise ScanIncomplete(f"根目录祖先打开失败：{label}（{exc}）") from exc
            child_info = os.fstat(child_fd)
            if not stat.S_ISDIR(child_info.st_mode):
                os.close(child_fd)
                raise ScanIncomplete(f"根目录祖先类型竞态：{label}")
            os.close(fd)
            fd = child_fd
        return fd, absolute
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _list_entries(
    dir_fd: int,
    budget: ScanBudget,
    warnings: ScanWarningLog,
    parts: tuple[str, ...],
):
    label = _display(parts)
    budget.check_time()
    try:
        names = sorted(os.listdir(dir_fd))
    except OSError as exc:
        raise ScanIncomplete(f"目录无法只读列举：{label}（{exc}）") from exc
    for name in names:
        item_parts = parts + (name,)
        item_label = _display(item_parts)
        budget.add_entry(item_label)
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as exc:
            raise ScanIncomplete(f"条目无法安全检查：{item_label}（{exc}）") from exc
        if stat.S_ISLNK(info.st_mode):
            # Do not call readlink(), open(), stat-following, hash, or recurse.
            # The link's own path is sufficient audit evidence; its target is
            # deliberately outside the observation contract.
            warnings.add("symlink_skipped", item_label)
            continue
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise ScanIncomplete(f"发现非常规文件/目录：{item_label}")
        yield name, info


def _open_child_dir(parent_fd: int, name: str, label: str) -> int:
    try:
        fd = os.open(name, _dir_flags(), dir_fd=parent_fd)
    except OSError as exc:
        raise ScanIncomplete(f"子目录打开失败（可能发生软链接竞态）：{label}（{exc}）") from exc
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        os.close(fd)
        raise ScanIncomplete(f"子目录类型竞态：{label}")
    return fd


def _read_file_at(
    dir_fd: int,
    name: str,
    expected: os.stat_result,
    budget: ScanBudget,
    label: str,
    *,
    capture_text: bool = False,
) -> tuple[str, list[str]]:
    """Hash one regular file from its already-open parent directory."""
    try:
        fd = os.open(name, _file_flags(), dir_fd=dir_fd)
    except OSError as exc:
        raise ScanIncomplete(f"文件打开失败（可能发生软链接竞态）：{label}（{exc}）") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ScanIncomplete(f"叶子不是常规文件：{label}")
        if (before.st_dev, before.st_ino) != (expected.st_dev, expected.st_ino):
            raise ScanIncomplete(f"文件在检查与打开之间被替换：{label}")
        if before.st_size > budget.max_file_bytes:
            raise ScanIncomplete(f"单文件预算超限（>{budget.max_file_bytes} 字节）：{label}")

        digest = hashlib.sha256()
        captured = bytearray() if capture_text else None
        file_bytes = 0
        while True:
            budget.check_time()
            try:
                chunk = os.read(fd, READ_CHUNK_BYTES)
            except OSError as exc:
                raise ScanIncomplete(f"文件流式读取失败：{label}（{exc}）") from exc
            if not chunk:
                break
            file_bytes += len(chunk)
            budget.add_bytes(len(chunk), file_bytes, label)
            digest.update(chunk)
            if captured is not None:
                captured.extend(chunk)

        after = os.fstat(fd)
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after or file_bytes != after.st_size:
            raise ScanIncomplete(f"文件在读取期间发生变化：{label}")

        lines: list[str] = []
        if captured is not None:
            lines = captured.decode("utf-8", errors="replace").splitlines(keepends=True)
        return digest.hexdigest(), lines
    finally:
        os.close(fd)


def _scan_skill_dir(
    dir_fd: int,
    budget: ScanBudget,
    warnings: ScanWarningLog,
    parts: tuple[str, ...],
    depth: int,
    relative: tuple[str, ...] = (),
) -> tuple[dict[str, str], str, list[str]]:
    budget.check_depth(depth, _display(parts))
    files: dict[str, str] = {}
    skill_digest: str | None = None
    skill_lines: list[str] = []
    for name, info in _list_entries(dir_fd, budget, warnings, parts):
        child_parts = parts + (name,)
        child_rel = relative + (name,)
        if stat.S_ISDIR(info.st_mode):
            if name in SKIP_DIRS:
                continue
            child_fd = _open_child_dir(dir_fd, name, _display(child_parts))
            try:
                nested_files, nested_digest, nested_lines = _scan_skill_dir(
                    child_fd, budget, warnings, child_parts, depth + 1, child_rel
                )
            finally:
                os.close(child_fd)
            files.update(nested_files)
            if nested_digest:
                # A nested SKILL.md is still an attachment of the outer package;
                # only the root manifest drives the drift diff.
                pass
            continue
        if name in SKIP_FILES:
            continue
        rel = "/".join(child_rel)
        is_manifest = not relative and name == "SKILL.md"
        digest, lines = _read_file_at(
            dir_fd, name, info, budget, _display(child_parts), capture_text=is_manifest
        )
        files[rel] = digest
        if is_manifest:
            skill_digest = digest
            skill_lines = lines
    if not relative and skill_digest is None:
        raise ScanIncomplete(f"技能目录的 SKILL.md 在扫描期间消失：{_display(parts)}")
    return files, skill_digest or "", skill_lines


def _discover_skills(
    dir_fd: int,
    budget: ScanBudget,
    warnings: ScanWarningLog,
    root: Path,
    parts: tuple[str, ...] = (),
    depth: int = 0,
):
    budget.check_depth(depth, str(root / Path(*parts)))
    entries = list(_list_entries(dir_fd, budget, warnings, (str(root),) + parts))
    manifest = next((pair for pair in entries if pair[0] == "SKILL.md"), None)
    if manifest:
        # _list_entries already proved the manifest is a non-symlink regular file.
        if not stat.S_ISREG(manifest[1].st_mode):
            raise ScanIncomplete(f"SKILL.md 不是常规文件：{root / Path(*parts) / 'SKILL.md'}")
        files, digest, lines = _scan_skill_dir(
            dir_fd, budget, warnings, (str(root),) + parts, depth
        )
        name = parts[-1] if parts else root.name
        yield name, {
            "dir": root / Path(*parts),
            "files": files,
            "digest": digest,
            "lines": lines,
        }
        return

    for name, info in entries:
        if not stat.S_ISDIR(info.st_mode) or name in SKIP_DIRS:
            continue
        child_parts = parts + (name,)
        child_fd = _open_child_dir(dir_fd, name, str(root / Path(*child_parts)))
        try:
            yield from _discover_skills(child_fd, budget, warnings, root, child_parts, depth + 1)
        finally:
            os.close(child_fd)


def collect(
    hosts: Mapping[str, Path] = HOSTS,
    budget: ScanBudget | None = None,
) -> tuple[dict[str, dict[str, dict]], list[str], list[ScanWarning]]:
    """Return ``(skills, missing_hosts, warnings)`` under one global budget."""
    active_budget = budget or ScanBudget()
    skills: dict[str, dict[str, dict]] = {}
    missing: list[str] = []
    warning_log = ScanWarningLog()
    for host, configured_root in hosts.items():
        root_fd, root = _open_root_dir(configured_root)
        if root_fd is None:
            missing.append(host)
            continue
        try:
            for name, entry in _discover_skills(root_fd, active_budget, warning_log, root):
                host_entries = skills.setdefault(name, {})
                if host not in host_entries:
                    host_entries[host] = entry
        finally:
            os.close(root_fd)
    return skills, missing, warning_log.items


def classify(entry: dict[str, dict]) -> str:
    """一致 / SKILL.md 分叉 / 附件不同。"""
    digests = {v["digest"] for v in entry.values()}
    filemaps = [v["files"] for v in entry.values()]
    if len(digests) > 1:
        return "分叉"
    if any(filemap != filemaps[0] for filemap in filemaps[1:]):
        return "附件不同"
    return "一致"


def diff_html(a_host: str, b_host: str, a: dict, b: dict) -> str:
    diff = difflib.unified_diff(
        a["lines"], b["lines"],
        fromfile=f"{HOST_LABEL.get(a_host, a_host)}/SKILL.md",
        tofile=f"{HOST_LABEL.get(b_host, b_host)}/SKILL.md",
        n=2, lineterm="",
    )
    rows = []
    for line in diff:
        line = line.rstrip("\n")
        cls = ""
        if line.startswith("+++") or line.startswith("---"):
            cls = "meta"
        elif line.startswith("@@"):
            cls = "hunk"
        elif line.startswith("+"):
            cls = "add"
        elif line.startswith("-"):
            cls = "del"
        rows.append(f'<div class="dl {cls}">{html.escape(line) or "&nbsp;"}</div>')
    return "".join(rows)


CSS = """
:root{--bg:#fbfaf8;--card:#fff;--line:#e6e2dc;--ink:#1f1d1b;--dim:#78716c;
--amber:#b45309;--amber-bg:#fef6e7;--green:#15803d;--green-bg:#eef7f0;--blue:#1d4ed8;--blue-bg:#eef2ff;}
*{box-sizing:border-box}
body{margin:0;padding:40px 24px 80px;background:var(--bg);color:var(--ink);
font:15px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif;}
.wrap{max-width:900px;margin:0 auto}
h1{font-size:26px;margin:0 0 6px;letter-spacing:-.01em}
.sub{color:var(--dim);font-size:13px;margin-bottom:32px}
h2{font-size:15px;margin:40px 0 14px;padding-bottom:8px;border-bottom:1px solid var(--line);
letter-spacing:.02em;color:var(--dim);font-weight:600}
.verdict{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:22px 24px;margin-bottom:8px}
.verdict.incomplete{border-color:#efc58e;background:var(--amber-bg)}
.verdict .big{font-size:19px;font-weight:600;margin-bottom:6px}
.verdict p{margin:0;color:var(--dim);font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:18px 20px;margin-bottom:12px}
.card.warn{border-color:#f0d9a8;background:var(--amber-bg)}
.head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.name{font-weight:600;font-size:16px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.tag{font-size:11px;padding:2px 8px;border-radius:20px;font-weight:600;letter-spacing:.02em}
.tag.warn{background:#fbe9c8;color:var(--amber)}
.tag.ok{background:var(--green-bg);color:var(--green)}
.tag.info{background:var(--blue-bg);color:var(--blue)}
.hosts{color:var(--dim);font-size:12.5px;margin-top:8px}
.hosts code{background:#f2efea;padding:1px 6px;border-radius:4px;font-size:12px}
details{margin-top:14px}
summary{cursor:pointer;font-size:13px;color:var(--dim);user-select:none;outline:none}
summary:hover{color:var(--ink)}
.diff{margin-top:10px;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#fff}
.dl{font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;padding:1px 12px;white-space:pre-wrap;word-break:break-word}
.dl.meta{background:#f7f5f2;color:var(--dim)}
.dl.hunk{background:#f0f4fb;color:#4b5fa8}
.dl.add{background:#eaf6ee;color:#16653a}
.dl.del{background:#fdeceb;color:#9b1c1c}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.mini{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;font-size:13px}
.mini .n{font-family:ui-monospace,Menlo,monospace;font-weight:600}
.mini .h{color:var(--dim);font-size:11.5px;margin-top:3px}
.note{color:var(--dim);font-size:13px;margin:0 0 16px}
footer{margin-top:56px;color:var(--dim);font-size:12px;border-top:1px solid var(--line);padding-top:16px}
"""


def render(
    skills: dict[str, dict[str, dict]],
    warnings: tuple[ScanWarning, ...] | list[ScanWarning] = (),
) -> str:
    shared = {key: value for key, value in sorted(skills.items()) if len(value) > 1}
    drifted = {key: value for key, value in shared.items() if classify(value) == "分叉"}
    attach = {key: value for key, value in shared.items() if classify(value) == "附件不同"}
    same = {key: value for key, value in shared.items() if classify(value) == "一致"}
    exclusive = {host: sorted(key for key, value in skills.items() if list(value) == [host]) for host in HOSTS}

    page = []
    page.append('<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">')
    page.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    page.append("<title>AI 技能漂移看板</title><style>" + CSS + "</style></head><body><div class='wrap'>")
    page.append("<h1>AI 技能漂移看板</h1>")
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    page.append(f"<div class='sub'>{stamp} · 只读扫描 Codex / Claude / Hermes / WorkBuddy</div>")

    if drifted:
        line = f"有 {len(drifted)} 个技能在不同宿主之间已经分叉"
        tail = "先看清楚分叉是「故意按宿主适配」还是「忘了同步」，再决定要不要收编。"
    else:
        line = "没有技能在宿主之间分叉"
        tail = "所有同名技能内容一致，暂时不需要中心库。"
    page.append(
        f"<div class='verdict'><div class='big'>{html.escape(line)}</div>"
        f"<p>共 {len(skills)} 个技能，其中 {len(shared)} 个存在于多个宿主。{tail}"
        f" 本次跳过 {len(warnings)} 个后代软链接。</p></div>"
    )

    if warnings:
        page.append("<h2>只读扫描警告 · 已跳过</h2>")
        page.append("<p class='note'>下列仅是软链接自身的路径；本次未解析链接目标，也未读取或哈希目标正文。</p>")
        for warning in warnings:
            page.append(
                "<div class='card warn'><div class='hosts'>"
                f"<code>{html.escape(warning.code)}</code> {html.escape(warning.path)}"
                "</div></div>"
            )

    page.append("<h2>已分叉 · 需要你判断</h2>")
    if not drifted:
        page.append("<p class='note'>没有。</p>")
    for name, entry in drifted.items():
        hosts = list(entry)
        page.append("<div class='card warn'><div class='head'>")
        page.append(f"<span class='name'>{html.escape(name)}</span>")
        page.append("<span class='tag warn'>SKILL.md 内容不同</span></div>")
        page.append("<div class='hosts'>" + " · ".join(
            f"<code>{html.escape(HOST_LABEL.get(host, host))}</code> {len(entry[host]['files'])} 个文件"
            for host in hosts) + "</div>")
        base = hosts[0]
        for other in hosts[1:]:
            page.append(
                f"<details open><summary>{html.escape(HOST_LABEL.get(base, base))} → "
                f"{html.escape(HOST_LABEL.get(other, other))} 的差异</summary>"
            )
            page.append("<div class='diff'>" + diff_html(base, other, entry[base], entry[other]) + "</div></details>")
        page.append("</div>")

    if attach:
        page.append("<h2>SKILL.md 一致，但附带文件不同</h2>")
        for name, entry in attach.items():
            page.append("<div class='card'><div class='head'>")
            page.append(f"<span class='name'>{html.escape(name)}</span>")
            page.append("<span class='tag info'>附件差异</span></div><div class='hosts'>")
            page.append(" · ".join(
                f"<code>{html.escape(HOST_LABEL.get(host, host))}</code> {len(entry[host]['files'])} 个文件"
                for host in entry
            ))
            all_files = sorted({filename for value in entry.values() for filename in value["files"]})
            rows = []
            for filename in all_files:
                where = [HOST_LABEL.get(host, host) for host in entry if filename in entry[host]["files"]]
                if len(where) < len(entry):
                    rows.append(f"{html.escape(filename)} —— 只在 {html.escape('、'.join(where))}")
            if rows:
                page.append("<br>" + "<br>".join(rows))
            page.append("</div></div>")

    page.append("<h2>多宿主共有、内容一致 · 收编中心库的天然候选</h2>")
    if not same:
        page.append("<p class='note'>没有。</p>")
    else:
        page.append(f"<p class='note'>这 {len(same)} 个是唯一真正被重复维护的部分，也是中心库唯一有实际收益的范围。</p>")
        page.append("<div class='grid'>")
        for name, entry in same.items():
            page.append(f"<div class='mini'><div class='n'>{html.escape(name)}</div>")
            host_names = '、'.join(HOST_LABEL.get(host, host) for host in entry)
            page.append(f"<div class='h'>{html.escape(host_names)}</div></div>")
        page.append("</div>")

    page.append("<h2>各宿主独有 · 建议留在原地</h2>")
    page.append("<p class='note'>只存在于一个宿主，收进中心库只会多一层间接。</p>")
    page.append("<div class='grid'>")
    for host, names in exclusive.items():
        page.append(f"<div class='mini'><div class='n'>{html.escape(HOST_LABEL.get(host, host))}</div>")
        page.append(f"<div class='h'>{len(names)} 个独有技能</div></div>")
    page.append("</div>")

    page.append("<footer>本页由 check-drift.py 只读扫描生成，脚本不会修改任何宿主目录。"
                "重新生成：<code>python3 check-drift.py</code></footer>")
    page.append("</div></body></html>")
    return "".join(page)


def render_incomplete(reason: str) -> str:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    safe_reason = html.escape(reason)
    return (
        '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>AI 技能漂移看板 · 扫描未完成</title><style>{CSS}</style></head>'
        '<body><div class="wrap"><h1>AI 技能漂移看板</h1>'
        f'<div class="sub">{stamp} · 本次结果不可用</div>'
        '<div class="verdict incomplete"><div class="big">扫描未完成</div>'
        f'<p>{safe_reason}</p><p>本页未包含任何部分漂移结果或越界文件正文。</p></div>'
        '<footer>请排除软链接、文件竞态或预算超限后重新扫描。</footer>'
        '</div></body></html>'
    )


def run_scan(
    hosts: Mapping[str, Path] = HOSTS,
    out: Path = OUT,
    budget: ScanBudget | None = None,
    *,
    open_report: bool = False,
) -> int:
    try:
        skills, missing, warnings = collect(hosts, budget)
    except ScanIncomplete as exc:
        out.write_text(render_incomplete(str(exc)), encoding="utf-8")
        print(f"扫描未完成：{exc}", file=sys.stderr)
        print(f"状态页已生成 → {out}", file=sys.stderr)
        return 2

    if missing:
        print("提示：这些宿主目录不存在，已跳过 —— " + "、".join(missing))
    if not skills and len(missing) == len(hosts):
        reason = "没扫到任何 SKILL.md，无法生成可用的漂移结论。"
        out.write_text(render_incomplete(reason), encoding="utf-8")
        print(f"扫描未完成：{reason}", file=sys.stderr)
        return 2

    out.write_text(render(skills, warnings), encoding="utf-8")
    shared = {key: value for key, value in skills.items() if len(value) > 1}
    drifted = [key for key, value in shared.items() if classify(value) == "分叉"]
    print(f"扫到 {len(skills)} 个技能，{len(shared)} 个存在于多个宿主。")
    print(f"已分叉：{'、'.join(drifted) if drifted else '无'}")
    for warning in warnings:
        print("警告：" + warning.message, file=sys.stderr)
    if warnings:
        print(f"已审计跳过 {len(warnings)} 个后代软链接。")
    print(f"看板已生成 → {out}")
    if open_report:
        subprocess.run(["open", str(out)], check=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读检查四个宿主之间的 Skill 漂移")
    parser.add_argument("--open", action="store_true", help="生成后打开 report.html")
    args = parser.parse_args(argv)
    return run_scan(open_report=args.open)


if __name__ == "__main__":
    raise SystemExit(main())
