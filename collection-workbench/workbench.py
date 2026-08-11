#!/usr/bin/env python3
"""技能能力工作台 —— 数据生成侧。纯只读，不解压。

用法：  python3 workbench.py [--open]
输出：  data.js    前端用（<script> 加载，file:// 下双击 index.html 即可）
        data.json  同一份数据的标准 JSON，给工具 / AI / diff 用

本脚本不再生成 HTML。前端是手写的 index.html，重跑本脚本不会覆盖它。
"""
from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("inv", HERE / "check-inventory.py")
inv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(inv)

OUT_JS = HERE / "data.js"      # 前端用：<script> 加载，file:// 下不受 CORS 限制
OUT_JSON = HERE / "data.json"  # 同一份数据的标准 JSON，给工具 / AI / diff 用
DEC_JSON = HERE / "decisions.json"   # 用户从页面导出、手动放回来的决策
DEC_JS   = HERE / "decisions.js"     # 同一份决策的 <script> 版，页面双击即开时自动读回  # 同一份数据的标准 JSON，给工具 / AI / diff 用
PAGE = HERE / "index.html"     # 手写前端，本脚本永不写入



PLATFORM = [
    ("Seedance 2.5", [r"seedance\s*2\.5", r"即梦\s*2\.5"]),
    ("Seedance 2.0", [r"seedance\s*2\.0", r"即梦\s*2\.0"]),
    ("即梦 / 剪映", [r"即梦", r"剪映"]),
    ("Midjourney", [r"midjourney", r"\bmj\b"]),
    ("Nano Banana", [r"nano\s*banana"]),
    ("通用大模型", [r"claude", r"gpt", r"gemini"]),
]
INPUT_KINDS = [
    ("剧本 / 分镜", [r"剧本", r"分镜", r"shot list", r"screenplay", r"storyboard"]),
    ("参考图", [r"参考图", r"reference image", r"图片", r"原图"]),
    ("参考视频", [r"参考视频", r"reference video", r"ai 视频", r"视频反推"]),
    ("白模 / 3D", [r"白模", r"灰模", r"3d", r"whitebox"]),
    ("一句话想法", [r"一句话", r"一个想法", r"brief scene idea", r"模糊想法"]),
    ("小说 / 长文", [r"小说", r"网文", r"webnovel", r"文章"]),
]
OUTPUT_KINDS = [
    ("视频提示词", [r"视频提示词", r"video prompt"]),
    ("生图提示词", [r"生图提示词", r"image prompt", r"生成或编辑提示词"]),
    ("分镜方案", [r"分镜", r"storyboard", r"镜头表"]),
    ("完整剧本", [r"剧本", r"screenplay", r"章节"]),
    ("分析报告", [r"报告", r"report", r"诊断", r"拆解"]),
]


def hits(text: str, table) -> list[str]:
    low = text.lower()
    return [label for label, pats in table if any(re.search(p, low) for p in pats)]


BUCKETS = [
    ("训练与测试", ["training_files", "training", "tests", "test", "evals", "eval",
                     "evidence", "checklists", "regression", "golden"]),
    ("知识库",   ["knowledge_base", "knowledge", "kb", "protocols", "rules", "core", "知识库", "词库"]),
    ("范例库",   ["examples", "example", "case_library", "cases", "recipes", "samples", "案例"]),
    ("模板",     ["templates", "template", "模板"]),
    ("参考资料", ["references", "reference", "docs", "doc", "资料"]),
    ("素材",     ["assets", "asset", "cards", "media", "images", "素材"]),
    ("脚本",     ["scripts", "script", "tools", "bin"]),
]
IGNORE_TOP = {"crates", ".github", "node_modules", "__pycache__", "target"}


def package_shape(
    src: Path,
    inner: str,
    *,
    names: list[str] | None = None,
    budget: object | None = None,
) -> dict:
    """看包里带了什么料。按 SKILL.md 所在层的一级子目录归类。"""
    shape = {label: 0 for label, _ in BUCKETS}
    shape["total"] = 0
    if names is None:
        try:
            names = inv.package_file_names(src, budget=budget)
        except inv.ScanSafetyError:
            raise
        except OSError:
            return shape
    base = (inner or "").rsplit("SKILL.md", 1)[0]
    for n in names:
        if base and not n.startswith(base):
            continue
        rel = n[len(base):] if base else n
        parts = [x for x in rel.split("/") if x]
        shape["total"] += 1
        if len(parts) < 2:
            continue
        top = parts[0].lower()
        if top in IGNORE_TOP:
            continue
        for label, keys in BUCKETS:
            if any(k == top or k in top for k in keys):
                shape[label] += 1
                break
        else:
            if re.search(r"\.(py|sh|js|mjs|ts|command)$", parts[-1].lower()):
                shape["脚本"] += 1
    # 根目录下的散装脚本也算
    for n in names:
        rel = n[len(base):] if base and n.startswith(base) else n
        if "/" not in rel and re.search(r"\.(py|sh|js|mjs|ts|command)$", rel.lower()):
            shape["脚本"] += 1
    return shape


def full_text(
    src: Path,
    inner: str,
    *,
    text: str | None = None,
    budget: object | None = None,
) -> str:
    if text is not None:
        return text
    try:
        return inv.read_skill_text_from_source(src, inner, budget=budget)
    except inv.ScanSafetyError:
        raise
    except OSError:
        return ""


RULES = json.loads((HERE / "rules.json").read_text("utf-8"))

# 组的展示顺序 / 说明 / 是否直接竞争，全部来自 rules.json 的 groups 段。
# rules 与 groups 是两个不同的顺序，不可合并：
#   rules  = 匹配优先级（网文最先；特化场景必须早于通用型，否则 whitebox 会被 seedance 抢走）
#   groups = 展示顺序
GROUPS = [(g["name"], g["desc"], g["competing"]) for g in RULES["groups"]]

_ZH_FILE = HERE / "zh_metadata.json"
ZH_META = {}
if _ZH_FILE.exists():
    try:
        ZH_META = {x["source_name"]: x for x in json.loads(_ZH_FILE.read_text("utf-8"))["items"]}
    except Exception:
        ZH_META = {}


def group_of(name: str, desc: str = "") -> tuple[str, str]:
    """按 rules.json 归组：先看技能名（强信号），名字没中再看简介。

    返回 (组名, 归组依据)。依据取值：override / name_any / desc_any / fallback
    """
    ov = RULES.get("overrides", {})
    if name in ov:
        return ov[name], "override"
    n, d = name.lower(), (desc or "").lower()
    for r in RULES["rules"]:
        if any(re.search(p, n) for p in r.get("exclude", [])):
            continue
        if any(re.search(p, n) for p in r.get("name_any", [])):
            return r["group"], "name_any"
        if any(re.search(p, d) for p in r.get("desc_any", [])):
            return r["group"], "desc_any"
    return "未归类", "fallback"


# 关键词推测出来的字段，前端必须视觉降级并允许人工覆盖
GUESSED_FIELDS = ["platform", "inputs", "outputs"]

# frontmatter 里会被误吞进 description 的键（parse_front 修好后应为 0，留作回归探针）
FRONT_KEYS = ("allowed-tools:", "argument-hint:", "disable-model-invocation:",
              "license:", "metadata:", "model:", "version:")


def desc_flags(desc: str, full: str, name: str) -> list[str]:
    """简介质量标记。desc_broken / frontmatter_bleed / truncated"""
    f, s = [], (full or "").strip()
    if s.startswith(("name:", "description:")) or s == (name or "").strip() or len(s) < 25:
        f.append("desc_broken")
    if any(k in s for k in FRONT_KEYS):
        f.append("frontmatter_bleed")
    if len(s) > 220:
        f.append("truncated")
    return f


# 布尔型筛选标签。判定只写在这里一处，前端直接用 item.tags.includes(值)，
# 不要自己重算 —— 两边逻辑分叉会让筛选结果和 facets 计数对不上且极难发现。
TAG_RULES = [
    ("含脚本",     lambda x: x["shape"]["脚本"] > 0),
    ("带知识库",   lambda x: x["shape"]["知识库"] > 0),
    ("带范例库",   lambda x: x["shape"]["范例库"] > 0),
    ("带模板",     lambda x: x["shape"]["模板"] > 0),
    ("带参考资料", lambda x: x["shape"]["参考资料"] > 0),
    ("带素材",     lambda x: x["shape"]["素材"] > 0),
    ("带训练与测试", lambda x: x["shape"]["训练与测试"] > 0),
    ("同名多版本", lambda x: x["multi"]),
    ("有重复副本", lambda x: x["copies"] > 1),
    ("已安装",     lambda x: bool(x["installed"]["hosts"])),
    ("简介有问题", lambda x: bool(x["desc_flags"])),
    ("中文包",     lambda x: x["zh"]),
]


def tags_of(x: dict) -> list[str]:
    return [label for label, ok in TAG_RULES if ok(x)]


def build(
    source_root: Path | str | None = None,
    *,
    budget: object | None = None,
    inventory: tuple[list[dict], list[dict], list[object]] | None = None,
    installed: dict[str, dict] | None = None,
    host_roots: dict[str, Path] | None = None,
) -> list[dict]:
    root = inv.normalize_source_root(source_root)
    effective_budget = budget or inv.ScanBudget()
    items = (inventory or inv.collect(root, budget=effective_budget))[0]
    installed_items = (
        installed
        if installed is not None
        else inv.scan_installed(budget=effective_budget, host_roots=host_roots)
    )
    seen, out = {}, []
    for e in items:
        key = e["fp"] or e["src"]
        seen.setdefault(key, []).append(e)
    for key, group in seen.items():
        e = group[0]
        src = root / e["src"]
        text = full_text(
            src,
            e.get("inner", ""),
            text=e.get("_text") if "_text" in e else None,
            budget=effective_budget,
        )
        shape = package_shape(
            src,
            e.get("inner", ""),
            names=e.get("_file_names"),
            budget=effective_budget,
        )
        # 只认「定位段」：frontmatter + 正文开头，避免大部头把所有关键词都扫中
        head = text[:2500]
        ver = re.search(r"v?(\d+\.\d+(?:\.\d+)?)", e["src"])
        grp, gby = group_of(e["name"], e["desc"])
        import hashlib as _h
        cur = _h.sha256(text.encode("utf-8")).hexdigest()
        z = ZH_META.get(e["name"])
        if not z:
            zh_name, zh_sum, zh_state = "", "", "missing"
        elif z["translated_from_hash"] != cur:
            zh_name, zh_sum, zh_state = z["zh_name"], z["summary"], "stale"
        else:
            zh_name, zh_sum, zh_state = z["zh_name"], z["summary"], z.get("status", "ai_draft")
        out.append({
            "uid": key if e["fp"] else "src:" + key,
            "inner": e.get("inner", ""),
            "copy_srcs": [g["src"] for g in group],
            "zh_name": zh_name,
            "zh_sum": zh_sum,
            "zh_state": zh_state,
            "name": e["name"],
            "group": grp,
            "group_by": gby,
            "desc": e["desc"],
            "desc_full": e.get("desc_full", e["desc"]),
            "desc_flags": desc_flags(e["desc"], e.get("desc_full", e["desc"]), e["name"]),
            "src": e["src"],
            "copies": len(group),
            "platform": hits(head, PLATFORM),
            "inputs": hits(head, INPUT_KINDS),
            "outputs": hits(head, OUTPUT_KINDS),
            "chars": len(text),
            "shape": shape,
            "version": ver.group(1) if ver else "",
            "installed": ({"hosts": sorted(installed_items[key]["hosts"]),
                           "as_name": installed_items[key]["name"]}
                          if key in installed_items else {"hosts": [], "as_name": ""}),
            "zh": bool(re.search(r"[一-鿿]", text[:3000])),
        })
    # 同名多版本标记
    from collections import Counter
    c = Counter(x["name"] for x in out)
    for x in out:
        x["multi"] = c[x["name"]] > 1
        x["tags"] = tags_of(x)
    return sorted(out, key=lambda x: (-x["chars"],))



def facets_of(data: list[dict]) -> dict:
    """筛选器选项 + 计数。计数为 0 的项也输出，前端渲染为禁用态。"""
    def multi(field, labels):
        return [{"value": v, "count": sum(1 for x in data if v in x[field])} for v in labels]
    return {
        "platform": multi("platform", [l for l, _ in PLATFORM]),
        "inputs":   multi("inputs",   [l for l, _ in INPUT_KINDS]),
        "outputs":  multi("outputs",  [l for l, _ in OUTPUT_KINDS]),
        "zh_state": [{"value": v, "count": sum(1 for x in data if x["zh_state"] == v)}
                     for v in ("missing", "stale", "ai_draft", "reviewed")],
        "has": [{"value": l, "count": sum(1 for x in data if l in x["tags"])}
                for l, _ in TAG_RULES],
    }


def loose_and_repos(
    source_root: Path | str | None = None,
    *,
    budget: object | None = None,
    inventory: tuple[list[dict], list[dict], list[object]] | None = None,
) -> tuple[list[dict], list[dict]]:
    """收藏夹里除技能之外的两类东西。

    它们没有 SKILL.md，进不了 items，但确实占着收藏夹 ——
    以前只有 inventory.html 看得见，现在一并交给前端。
    """
    root = inv.normalize_source_root(source_root)
    effective_budget = budget or inv.ScanBudget()
    _items, loose, repos = inventory or inv.collect(root, budget=effective_budget)
    docs = []
    for e in sorted(loose, key=lambda x: -x["size"]):
        # 散装文档以「文件名」为准 —— 这是他在 Finder 里找得到的东西。
        # parse_front 抓到的往往只是文里第一个小标题（如《超级编剧大师集成版》被抓成"身份设定"）。
        stem = Path(e["src"]).stem
        docs.append({
            "name": stem,
            "heading": e["name"] if e["name"] and e["name"] != stem else "",
            "src": e["src"],
            "bytes": e["size"],
            "desc": e["desc"],
            "zh": bool(re.search(r"[\u4e00-\u9fff]", (e["name"] or "") + (e["desc"] or ""))),
        })
    repo_list = []
    for repo in repos:
        repo_list.append({
            "name": repo.path.name,
            "src": str(repo.path.relative_to(root)),
            "skills_inside": repo.skills_inside,
        })
    return docs, sorted(repo_list, key=lambda x: -x["skills_inside"])


def sync_decisions_js() -> str:
    """把 decisions.json 转成 decisions.js，让页面双击打开时自动读回决策。

    为什么要这一步：file:// 下 fetch 本地文件必被 CORS 拦死（data.js 就是为此存在）。
    没有它，用户每次重开页面都得手动点「导入」再选一次文件，忘了就等于从零开始。
    只做 json → js 单向转换；没有 json 就什么都不做。
    """
    if not DEC_JSON.exists():
        return "没有 decisions.json，跳过"
    try:
        blob = json.dumps(json.loads(DEC_JSON.read_text("utf-8")), ensure_ascii=False, indent=2)
    except Exception as exc:
        return f"⚠ decisions.json 读不动，未生成 decisions.js：{exc}"
    DEC_JS.write_text(
        "// 由 workbench.py 从 decisions.json 转换而来，请勿手改。\n"
        "// 页面会自动读它；要改决策请在页面上改，再导出 decisions.json 放回来。\n"
        "window.__WORKBENCH_DECISIONS__ = " + blob + ";\n", encoding="utf-8")
    n = len(json.loads(DEC_JSON.read_text("utf-8")).get("decisions", {}))
    return f"decisions.js 已同步（{n} 条决策，页面打开即自动读回）"


def build_payload(
    source_root: Path | str | None = None,
    *,
    host_roots: dict[str, Path] | None = None,
) -> dict:
    """Build the candidate catalog without writing any files.

    The standalone command still writes data.json/data.js in ``main``.  The
    AI-Toolbox server imports this pure builder and persists its validated
    runtime copy under the parent ``generated/`` boundary instead.
    """
    root = inv.normalize_source_root(source_root)
    budget = inv.ScanBudget()
    inventory = inv.collect(root, budget=budget)
    installed = inv.scan_installed(budget=budget, host_roots=host_roots)
    data = build(
        root,
        budget=budget,
        inventory=inventory,
        installed=installed,
        host_roots=host_roots,
    )
    loose_docs, repos = loose_and_repos(
        root,
        budget=budget,
        inventory=inventory,
    )
    c = Counter(x["group"] for x in data)

    present_groups = [(g, d, vs) for g, d, vs in GROUPS if c[g]]
    groups = [
        {"name": g, "desc": d, "competing": vs, "count": c[g], "order": order}
        for order, (g, d, vs) in enumerate(present_groups)
    ]
    vs_names = {g for g, _, vs in GROUPS if vs}
    return {
        # 迁移第 1 步：只加字段、不改任何已有字段的值或结构。
        # 补齐 docs/数据契约 §3 全部字段后升到 3。
        "schema_version": 3,
        "generated_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_dir": str(root),
        "stats": {
            "skills": len(data),
            "groups": len(groups),
            "ungrouped": c["未归类"],
            "installed": sum(1 for item in data if item["installed"]["hosts"]),
            "competing": sum(1 for x in data if x["group"] in vs_names),
            # 新增：收藏夹里非技能的两类，前端可选读取，缺失当空数组
            "loose_docs": len(loose_docs),
            "repos": len(repos),
        },
        "hosts": list(inv.HOSTS),
        "guessed_fields": GUESSED_FIELDS,
        "groups": groups,
        "facets": facets_of(data),
        "items": data,
        # 纯新增顶层字段，不动 items/facets/groups —— 故意不升 schema_version，
        # 免得把前端现有的版本校验挡在门外。前端按「可选，缺失当 []」处理。
        "loose_docs": loose_docs,
        "repos": repos,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读生成候选 Skill 数据")
    parser.add_argument("--source-root", default=str(inv.DUMP))
    parser.add_argument("--open", action="store_true")
    arguments = parser.parse_args(argv)
    payload = build_payload(arguments.source_root)
    data = payload["items"]
    groups = payload["groups"]
    c = Counter(x["group"] for x in data)
    blob = json.dumps(payload, ensure_ascii=False, indent=2)
    OUT_JSON.write_text(blob + "\n", encoding="utf-8")
    OUT_JS.write_text(
        "// 由 workbench.py 生成，请勿手改。重跑：python3 workbench.py\n"
        "window.__WORKBENCH_DATA__ = " + blob + ";\n", encoding="utf-8")

    print(f"分组完成，共 {len(data)} 个技能：")
    for g, _, vs in GROUPS:
        if c[g]:
            print(f"  {'⚔' if vs else '＋'} {g}: {c[g]}")
    if c["未归类"]:
        print(f"  ? 未归类: {c['未归类']}")

    unknown = sorted({r["group"] for r in RULES["rules"]} - {g for g, _, _ in GROUPS})
    if unknown:
        print(f"\n⚠ rules.json 有组名不在 GROUPS 中，会被前端静默丢掉：{unknown}")

    print(f"\n数据 → data.json / data.js（{len(blob):,} 字符）")
    print(sync_decisions_js())
    if not PAGE.exists():
        print(f"⚠ 尚无 {PAGE.name}，数据已生成但没有前端可打开")
    elif arguments.open:
        subprocess.run(["open", str(PAGE)], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
