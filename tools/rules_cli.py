#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则库迁移汇总 CLI（P-ENHANCE R08）：导出/导入/合并/校验/统计。

规则库（rules/）支持跨机迁移与多源汇总：
- 导出单文件 bundle（规则+证据+版本）→ 拷贝到其他机器/仓库
- 导入 bundle → 写入 learned/user 级（冲突不覆盖，返回冲突清单）
- 合并多来源 → 冲突裁决（user>builtin>learned → confidence → version）→ 输出合并 bundle
- 校验规则文件/bundle 合法性

用法:
    .venv\\Scripts\\python.exe tools/rules_cli.py list [--dir rules]
    .venv\\Scripts\\python.exe tools/rules_cli.py export <out.json> [--dir rules] [--level learned,user]
    .venv\\Scripts\\python.exe tools/rules_cli.py import <bundle.json> [--dir rules] [--level learned]
    .venv\\Scripts\\python.exe tools/rules_cli.py merge <b1.json> <b2.json> ... -o <merged.json>
    .venv\\Scripts\\python.exe tools/rules_cli.py validate <file.json>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paperparse.core import rule_library as rl  # noqa: E402


def cmd_list(args) -> int:
    rd = Path(args.dir)
    st = rl.stats(rd)
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0


def cmd_export(args) -> int:
    rd = Path(args.dir)
    levels = tuple(lv for lv in (args.level or "builtin,learned,user").split(",") if lv)
    p = rl.export_bundle(rd, args.out, levels=levels, note=args.note or "")
    print("已导出 %d 条规则 -> %s" % (len(rl.load_rules(rd)), p))
    return 0


def cmd_import(args) -> int:
    rd = Path(args.dir)
    res = rl.import_bundle(rd, args.bundle, level=args.level)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res["errors"]:
        print("警告：%d 条规则校验失败（未导入）" % len(res["errors"]), file=sys.stderr)
    return 0 if not res["errors"] else 1


def cmd_merge(args) -> int:
    rule_sets = []
    for b in args.bundles:
        data = json.loads(Path(b).read_text(encoding="utf-8"))
        rules = data.get("rules") or []
        rule_sets.append(rules)
        print("  %s: %d 条规则" % (b, len(rules)))
    res = rl.merge(rule_sets, source_order=("user", "builtin", "learned"))
    if args.out:
        bundle = {"bundle_version": rl.BUNDLE_VERSION,
                  "schema_version": rl.SCHEMA_VERSION,
                  "exported_at": rl._now(),
                  "meta": {"merged_from": args.bundles,
                           "rule_count": len(res["rules"]),
                           "conflict_count": len(res["conflicts"])},
                  "rules": res["rules"]}
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    print("合并后 %d 条规则，%d 个冲突" % (len(res["rules"]), len(res["conflicts"])))
    for c in res["conflicts"][:20]:
        print("  冲突: %s | kept=%s dropped=%s (%s)" % (
            c.get("pattern", "")[:40], c.get("kept"), c.get("dropped"), c.get("reason")))
    return 0


def cmd_validate(args) -> int:
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    rules = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(rules, list):
        print("错误：不是规则列表或 bundle", file=sys.stderr)
        return 2
    bad = 0
    for r in rules:
        errs = rl.validate_rule(r)
        if errs:
            bad += 1
            print("  非法规则 %s: %s" % (r.get("rule_id"), "; ".join(errs)))
    print("校验完成：%d 条合法 / %d 条非法" % (len(rules) - bad, bad))
    return 1 if bad else 0


def cmd_promote(args) -> int:
    """固化：外部 learned/user 规则 → 引擎 skill/rules/builtin（随版本分发）。

    过滤：--min-confidence（默认 0.9）；冲突裁决 user>builtin>learned。
    固化后提示：引擎版本 bump → sync_engine_assets → 重新打包。
    """
    from paperparse.core.rule_library import (RULE_CATEGORIES, builtin_rules_dir,
                                                load_rules, merge, save_rules)
    ext_rules = load_rules(args.dir, level="learned") + load_rules(args.dir, level="user")
    if not args.all and args.min_confidence:
        ext_rules = [r for r in ext_rules
                     if float(r.get("confidence", 0)) >= args.min_confidence]
    if not ext_rules:
        print("无可固化规则（learned/user 为空或置信度 < %.2f）"
              % (args.min_confidence or 0))
        return 0
    bdir = builtin_rules_dir(args.builtin_base)
    builtin = load_rules(bdir, level="builtin")
    res = merge([builtin, ext_rules], source_order=("user", "builtin", "learned"))
    by_cat: dict[str, list] = {}
    for r in res["rules"]:
        r["level"] = "builtin"
        by_cat.setdefault(r.get("category", "other"), []).append(r)
    for cat, rules in by_cat.items():
        if cat not in RULE_CATEGORIES:
            continue
        save_rules(bdir, "builtin", cat, rules)
    print("已固化 %d 条规则 -> %s/builtin（冲突 %d）" % (
        len(res["rules"]), bdir, len(res["conflicts"])))
    for c in res["conflicts"][:10]:
        print("  冲突: %s | kept=%s dropped=%s" % (
            c.get("pattern", "")[:40], c.get("kept"), c.get("dropped")))
    print("下一步：1) 引擎 pyproject.toml 版本 bump  2) tools/sync_engine_assets.ps1 同步快照 "
          "3) PaperAgent.spec 重新打包")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="规则库迁移汇总 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="规则统计")
    p_list.add_argument("--dir", default=None, help="外部规则根（默认自动探测）")

    p_export = sub.add_parser("export", help="导出 bundle")
    p_export.add_argument("out", help="输出 bundle 路径")
    p_export.add_argument("--dir", default=None)
    p_export.add_argument("--level", default="builtin,learned,user")
    p_export.add_argument("--note", default="")

    p_import = sub.add_parser("import", help="导入 bundle")
    p_import.add_argument("bundle", help="bundle 路径")
    p_import.add_argument("--dir", default=None)
    p_import.add_argument("--level", default="learned")

    p_merge = sub.add_parser("merge", help="合并多来源 bundle")
    p_merge.add_argument("bundles", nargs="+", help="≥2 个 bundle")
    p_merge.add_argument("-o", "--out", help="输出合并 bundle（可选）")

    p_val = sub.add_parser("validate", help="校验规则/bundle")
    p_val.add_argument("file")

    p_promote = sub.add_parser("promote", help="固化 learned/user → 引擎内嵌 builtin")
    p_promote.add_argument("--dir", default=None, help="外部规则根")
    p_promote.add_argument("--builtin-base", default=None,
                           help="引擎 skill 根（默认 asset_root()）")
    p_promote.add_argument("--min-confidence", type=float, default=0.9,
                           help="固化置信度下限（默认 0.9）")
    p_promote.add_argument("--all", action="store_true", help="忽略置信度全部固化")

    args = ap.parse_args()
    if getattr(args, "dir", None) is None:
        args.dir = str(rl.default_rules_dir())
    return {"list": cmd_list, "export": cmd_export, "import": cmd_import,
            "merge": cmd_merge, "validate": cmd_validate,
            "promote": cmd_promote}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
