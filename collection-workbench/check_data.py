#!/usr/bin/env python3
"""契约自检 —— 校验 data.json / data.js 是否符合 docs/数据契约。

    python3 check_data.py

不导入 workbench.py，纯粹按契约独立校验，所以生成侧写错了这里能抓到。
任何一项不过就 exit 1。改完 workbench.py 或动过数据，跑一次这个。
"""
from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA = 3
EXPECTED_HOSTS = ["codex", "claude", "hermes", "workbuddy"]
HOSTS = set(EXPECTED_HOSTS)
ZH_STATES = {"missing", "stale", "ai_draft", "reviewed"}
GROUP_BY = {"override", "name_any", "desc_any", "fallback"}
DESC_FLAGS = {"desc_broken", "frontmatter_bleed", "truncated"}
SHAPE_KEYS = {"训练与测试", "知识库", "范例库", "模板", "参考资料", "素材", "脚本", "total"}
STATS_FIELDS = {"skills", "groups", "ungrouped", "installed", "competing", "loose_docs", "repos"}
TOP_LEVEL_FIELDS = (
    "schema_version", "generated_at", "source_dir", "stats", "hosts",
    "guessed_fields", "groups", "facets", "items", "loose_docs", "repos",
)
TOP_LEVEL_FIELD_SET = set(TOP_LEVEL_FIELDS)
LOOSE_DOC_FIELDS = {"name", "heading", "src", "bytes", "desc", "zh"}
REPO_FIELDS = {"name", "src", "skills_inside"}
ITEM_FIELDS = {
    "uid": str, "name": str, "src": str, "inner": str, "version": str,
    "zh_name": str, "zh_sum": str, "zh_state": str, "desc": str, "desc_full": str,
    "desc_flags": list, "zh": bool, "group": str, "group_by": str,
    "platform": list, "inputs": list, "outputs": list, "chars": int,
    "shape": dict, "copies": int, "copy_srcs": list, "multi": bool,
    "installed": dict, "tags": list,
}


def _is_timezone_aware_iso8601(value: object) -> bool:
    if not isinstance(value, str) or "T" not in value:
        return False
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _is_safe_relative_path(
    value: object,
    *,
    allow_empty: bool = False,
    allow_root: bool = False,
) -> bool:
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        return False
    if allow_empty and value == "":
        return True
    if allow_root and value == ".":
        return True
    if not value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        return False
    parts = value.split("/")
    return all(part not in {"", ".", ".."} for part in parts)


def _is_canonical_absolute_path(value: object) -> bool:
    if not isinstance(value, str) or not value or not os.path.isabs(value):
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return False
    if os.path.normpath(value) != value:
        return False
    return all(part not in {".", ".."} for part in value.split("/")[1:])


def _is_object_array(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, dict) for item in value)


def main(
    payload: dict | None = None,
    *,
    require_data_js: bool = True,
    quiet: bool = False,
) -> int:
    """Validate a candidate payload independently from the generator.

    The CLI keeps validating the sibling data.json/data.js pair.  The parent
    AI-Toolbox server passes an in-memory payload and skips only the file://
    compatibility data.js check before committing its runtime snapshot.
    ``quiet=True`` is process-local and safe for concurrent library callers.
    """
    fails: list[str] = []
    lines: list[str] = []

    def emit(message: str) -> None:
        lines.append(message)

    def check(name: str, ok: object, detail: str = "") -> None:
        passed = bool(ok)
        emit(f"  {'✅' if passed else '❌'} {name}{'' if passed else '  → ' + detail}")
        if not passed:
            fails.append(name)

    def finish() -> int:
        if not quiet and lines:
            print("\n".join(lines))
        return 1 if fails else 0

    if payload is None:
        jp = HERE / "data.json"
        if not jp.exists():
            emit("找不到 data.json，先跑：python3 workbench.py")
            fails.append("data.json 存在")
            return finish()
        try:
            P = json.loads(jp.read_text("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            emit("data.json 不是可读的 JSON")
            fails.append("data.json 可读")
            return finish()
    else:
        P = payload
    if not isinstance(P, dict):
        emit("候选数据必须是 JSON 对象")
        fails.append("候选数据是对象")
        return finish()
    raw_items = P.get("items", [])
    items_ok = _is_object_array(raw_items)
    items = raw_items if items_ok else []
    raw_groups = P.get("groups", [])
    groups_ok = _is_object_array(raw_groups)
    groups = raw_groups if groups_ok else []
    raw_facets = P.get("facets", {})
    facets_ok = isinstance(raw_facets, dict)
    facets = raw_facets if facets_ok else {}

    emit("\n【外层信封】")
    for k in TOP_LEVEL_FIELDS:
        check(f"存在 {k}", k in P)
    check("顶层字段集精确", set(P) == TOP_LEVEL_FIELD_SET,
          str(set(P) ^ TOP_LEVEL_FIELD_SET))
    check(f"schema_version == {SCHEMA}", P.get("schema_version") == SCHEMA,
          f"实为 {P.get('schema_version')}")
    check("generated_at 是带时区 ISO 8601", _is_timezone_aware_iso8601(P.get("generated_at")),
          str(P.get("generated_at")))
    check("source_dir 是规范绝对路径",
          _is_canonical_absolute_path(P.get("source_dir")),
          str(P.get("source_dir")))
    check("hosts 固定且有序", P.get("hosts") == EXPECTED_HOSTS, str(P.get("hosts")))
    check("guessed_fields 正确", P.get("guessed_fields") == ["platform", "inputs", "outputs"],
          str(P.get("guessed_fields")))
    check("items 是对象数组", items_ok, type(raw_items).__name__)
    check("groups 是对象数组", groups_ok, type(raw_groups).__name__)
    check("facets 是对象", facets_ok, type(raw_facets).__name__)
    loose_docs_ok = _is_object_array(P.get("loose_docs"))
    repos_ok = _is_object_array(P.get("repos"))
    check("loose_docs 是对象数组", loose_docs_ok,
          type(P.get("loose_docs")).__name__)
    check("repos 是对象数组", repos_ok,
          type(P.get("repos")).__name__)
    loose_docs = P["loose_docs"] if loose_docs_ok else []
    repos = P["repos"] if repos_ok else []
    loose_shape_ok = all(
        set(row) == LOOSE_DOC_FIELDS
        and all(isinstance(row[field], str) for field in ("name", "heading", "src", "desc"))
        and isinstance(row["bytes"], int)
        and not isinstance(row["bytes"], bool)
        and row["bytes"] >= 0
        and isinstance(row["zh"], bool)
        for row in loose_docs
    )
    repo_shape_ok = all(
        set(row) == REPO_FIELDS
        and all(isinstance(row[field], str) for field in ("name", "src"))
        and isinstance(row["skills_inside"], int)
        and not isinstance(row["skills_inside"], bool)
        and row["skills_inside"] >= 0
        for row in repos
    )
    check("loose_docs 字段结构正确", loose_shape_ok)
    check("repos 字段结构正确", repo_shape_ok)

    emit("\n【items 字段完整性】")
    missing = {f: [x.get("name") for x in items if f not in x] for f in ITEM_FIELDS}
    missing = {f: v for f, v in missing.items() if v}
    check("所有必需字段齐全", not missing, str(list(missing))[:120])
    badtype = [(x.get("name"), f) for x in items for f, ty in ITEM_FIELDS.items()
               if f in x and not isinstance(x[f], ty)]
    check("字段类型正确", not badtype, str(badtype[:4]))
    extra = {k for x in items for k in x} - set(ITEM_FIELDS)
    check("没有契约外的字段", not extra, f"多出 {extra}")
    if not items_ok or missing or badtype:
        return finish()
    nested_types_ok = all(
        all(isinstance(value, str) for field in (
            "desc_flags", "platform", "inputs", "outputs", "copy_srcs", "tags"
        ) for value in item[field])
        and all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in item["shape"].values()
        )
        for item in items
    )
    check("列表与 shape 子字段类型正确", nested_types_ok)
    if not nested_types_ok:
        return finish()

    unsafe_paths = [
        (item["name"], field, value)
        for item in items
        for field, values, allow_empty, allow_root in (
            ("src", [item["src"]], False, True),
            ("inner", [item["inner"]], True, False),
            ("copy_srcs", item["copy_srcs"], False, True),
        )
        for value in values
        if not _is_safe_relative_path(value, allow_empty=allow_empty, allow_root=allow_root)
    ]
    check("src/inner/copy_srcs 是安全相对路径", not unsafe_paths,
          str(unsafe_paths[:3]))
    check("src 存在于 copy_srcs", all(item["src"] in item["copy_srcs"] for item in items))

    optional_paths = []
    for field, rows in (("loose_docs", loose_docs), ("repos", repos)):
        optional_paths.extend(
            (field, row.get("src"))
            for row in rows
            if not _is_safe_relative_path(row.get("src"), allow_root=False)
        )
    check("loose_docs/repos src 是安全相对路径", not optional_paths,
          str(optional_paths[:3]))

    emit("\n【uid 稳定标识】")
    uids = [x["uid"] for x in items]
    check(f"唯一（{len(set(uids))}/{len(uids)}）", len(set(uids)) == len(uids))
    check("全部非空", all(uids))
    bad = [u for u in uids if not (re.fullmatch(r"[0-9a-f]{32}", u) or u.startswith("src:"))]
    check("格式为 md5 或 src: 回退", not bad, str(bad[:3]))

    emit("\n【枚举合法性】")
    if not groups_ok or not facets_ok:
        return finish()
    group_shape_ok = all(
        set(group) == {"name", "desc", "competing", "count", "order"}
        and isinstance(group["name"], str)
        and isinstance(group["desc"], str)
        and isinstance(group["competing"], bool)
        and isinstance(group["count"], int)
        and not isinstance(group["count"], bool)
        and isinstance(group["order"], int)
        and not isinstance(group["order"], bool)
        for group in groups
    )
    check("groups 字段结构正确", group_shape_ok)
    if not group_shape_ok:
        return finish()
    gnames = {g["name"] for g in groups} | {"未归类"}
    check("group 都在 groups 内", all(x["group"] in gnames for x in items),
          str({x["group"] for x in items} - gnames))
    check("group_by 合法", all(x["group_by"] in GROUP_BY for x in items))
    check("zh_state 合法", all(x["zh_state"] in ZH_STATES for x in items))
    check("desc_flags 合法", all(set(x["desc_flags"]) <= DESC_FLAGS for x in items))
    check("shape 键齐全", all(set(x["shape"]) == SHAPE_KEYS for x in items))

    emit("\n【字段间一致性】")
    check("desc == desc_full[:220]", all(x["desc"] == x["desc_full"][:220] for x in items),
          str([x["name"] for x in items if x["desc"] != x["desc_full"][:220]][:3]))
    check("truncated 标记与实际相符",
          all(("truncated" in x["desc_flags"]) == (len(x["desc_full"].strip()) > 220)
              for x in items))
    check("copy_srcs 长度 == copies",
          all(len(x["copy_srcs"]) == x["copies"] for x in items))
    check("multi 与同名条数相符",
          all(x["multi"] == (sum(1 for y in items if y["name"] == x["name"]) > 1)
              for x in items))
    installed_ok = all(
        set(x["installed"]) == {"hosts", "as_name"}
        and isinstance(x["installed"].get("hosts"), list)
        and all(isinstance(host, str) for host in x["installed"]["hosts"])
        and isinstance(x["installed"].get("as_name"), str)
        and set(x["installed"]["hosts"]) <= HOSTS
        for x in items
    )
    check("installed 结构正确", installed_ok)
    if not installed_ok:
        return finish()
    check("已安装的必有 as_name",
          all(x["installed"]["as_name"] or not x["installed"]["hosts"] for x in items))

    emit("\n【facets 与 items 对得上】")
    F = facets
    facets_shape_ok = set(F) == {"platform", "inputs", "outputs", "zh_state", "has"} and all(
        _is_object_array(F[field])
        and all(
            set(entry) == {"value", "count"}
            and isinstance(entry["value"], str)
            and isinstance(entry["count"], int)
            and not isinstance(entry["count"], bool)
            and entry["count"] >= 0
            for entry in F[field]
        )
        for field in ("platform", "inputs", "outputs", "zh_state", "has")
    )
    check("facets 字段结构正确", facets_shape_ok)
    if not facets_shape_ok:
        return finish()
    for field in ("platform", "inputs", "outputs"):
        bad = [f'{e["value"]}: 标 {e["count"]} 实 {sum(1 for x in items if e["value"] in x[field])}'
               for e in F.get(field, [])
               if e["count"] != sum(1 for x in items if e["value"] in x[field])]
        check(f"facets.{field} 计数准确", not bad, "; ".join(bad[:3]))
    bad = [f'{e["value"]}: 标 {e["count"]} 实 {sum(1 for x in items if x["zh_state"] == e["value"])}'
           for e in F.get("zh_state", [])
           if e["count"] != sum(1 for x in items if x["zh_state"] == e["value"])]
    check("facets.zh_state 计数准确", not bad, "; ".join(bad[:3]))
    bad = [f'{e["value"]}: 标 {e["count"]} 实 {sum(1 for x in items if e["value"] in x["tags"])}'
           for e in F.get("has", [])
           if e["count"] != sum(1 for x in items if e["value"] in x["tags"])]
    check("facets.has 计数准确", not bad, "; ".join(bad[:3]))
    known = {e["value"] for e in F.get("has", [])}
    check("tags 都在 facets.has 内", all(set(x["tags"]) <= known for x in items),
          str({t for x in items for t in x["tags"]} - known))

    emit("\n【groups 与 stats】")
    check("groups.count 准确",
          all(g["count"] == sum(1 for x in items if x["group"] == g["name"])
              for g in groups))
    check("groups.order 连续", [g["order"] for g in groups]
          == list(range(len(groups))))
    check("未归类不在 groups 内", "未归类" not in {g["name"] for g in groups})
    S = P.get("stats", {})
    check("stats 字段完整", isinstance(S, dict) and set(S) == STATS_FIELDS,
          str(set(S) if isinstance(S, dict) else type(S).__name__))
    S = S if isinstance(S, dict) else {}
    check("stats.skills", S.get("skills") == len(items))
    check("stats.groups", S.get("groups") == len(groups))
    check("stats.ungrouped", S.get("ungrouped") == sum(1 for x in items if x["group"] == "未归类"))
    check("stats.installed",
          S.get("installed") == sum(1 for x in items if x["installed"]["hosts"]))
    vs = {g["name"] for g in groups if g["competing"]}
    check("stats.competing", S.get("competing") == sum(1 for x in items if x["group"] in vs))
    check("stats.loose_docs", S.get("loose_docs") == len(loose_docs))
    check("stats.repos", S.get("repos") == len(repos))

    if require_data_js:
        emit("\n【data.js】")
        jsp = HERE / "data.js"
        if not jsp.exists():
            check("data.js 存在", False, "缺失")
        else:
            m = re.search(r"window\.__WORKBENCH_DATA__ = (.*);\n\Z", jsp.read_text("utf-8"), re.S)
            check("data.js 格式正确", bool(m))
            if m:
                check("载荷与 data.json 一致", json.loads(m.group(1)) == P)

    emit("\n【回归探针】")
    check("没有 frontmatter 泄漏进简介",
          not any("frontmatter_bleed" in x["desc_flags"] for x in items),
          str([x["name"] for x in items if "frontmatter_bleed" in x["desc_flags"]][:3]))

    n = len(fails)
    emit(f"\n{'─'*54}\n{'❌ 有 %d 项没过：%s' % (n, fails) if n else '✅ 全部通过'}")
    emit(f"{len(items)} 个技能 · {len(groups)} 组 · "
         f"简介有问题 {sum(1 for x in items if x['desc_flags'])} 个 · "
         f"已安装 {sum(1 for x in items if x['installed']['hosts'])} 个")
    return finish()


if __name__ == "__main__":
    raise SystemExit(main())
