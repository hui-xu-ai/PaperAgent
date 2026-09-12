#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/rule_library.py
功能: 规则库（P-ENHANCE R03）：PDF 解析识别错误的可学习修复规则——存储/schema/增删改查/
      导出导入 bundle/merge 冲突裁决/统计。R04 S6.5 规则引擎直接消费本库规则。

规则三级：builtin（随代码只读）/ learned（自动学习，可禁用）/ user（人工，覆盖优先）。
存储：rules/<level>/<category>.json（每类一文件）+ rules/manifest.json。
对外接口: RULE_CATEGORIES / validate_rule / load_rules / load_by_level / add_rule /
          remove_rule / set_enabled / export_bundle / import_bundle / merge / stats
版本: v0.1.0 (2026-08-22)
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["RULE_CATEGORIES", "LEVELS", "SCHEMA_VERSION",
           "validate_rule", "load_rules", "load_by_level", "add_rule",
           "remove_rule", "set_enabled", "export_bundle", "import_bundle",
           "merge", "stats", "default_rules_dir", "rules_manifest"]

RULE_CATEGORIES = ("latex", "formula", "variable", "spacing", "metadata", "layout")
LEVELS = ("builtin", "learned", "user")
SCHEMA_VERSION = 1
BUNDLE_VERSION = "0.1.0"
PATTERN_TYPES = ("regex", "latex_token", "dict", "layout_rule")
SOURCES = ("builtin", "ai_review", "user_correction", "manual", "mining")
REVIEWS = ("ai", "user", "manual")

# source 优先级（merge 冲突裁决：数值越大越优先）
_SOURCE_PRIORITY = {"user": 3, "builtin": 2, "learned": 1}
_REQUIRED = ("rule_id", "category", "pattern_type", "pattern", "replacement")
_ID_RE = re.compile(r"^R-\d{8}-\d{3,}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------- 路径 ----------------

def default_rules_dir() -> Path:
    """[全局] 规则根目录：环境变量 RULES_DIR > cwd/rules > 向上探测（AGENTS.md+rules）> 引擎回退"""
    env = os.getenv("RULES_DIR", "").strip()
    if env:
        return Path(env)
    # cwd 优先（parse_offline 等从应用仓库根运行）
    cwd_rules = Path.cwd() / "rules"
    if cwd_rules.is_dir():
        return cwd_rules
    # 向上探测：AGENTS.md 所在项目根 + rules/
    for parent in Path.cwd().parents:
        if (parent / "AGENTS.md").exists() and (parent / "rules").is_dir():
            return parent / "rules"
    return Path.cwd() / "rules"                        # 兜底：cwd/rules


def builtin_rules_dir(base: str | Path | None = None) -> Path:
    """[全局] 内嵌内置规则根：asset_root()/rules（含 builtin/ 子目录；wheel 自包含，
    随 skill 版本分发——R09 训练结果固化目标位置）"""
    from paperparse.config import asset_root
    root = Path(base) if base else asset_root()
    return root / "rules"


def external_rules_dir() -> Path:
    """[全局] 外部学习规则根（同 default_rules_dir：learned/user 可写）"""
    return default_rules_dir()


def load_all_rules(external: str | Path | None = None,
                   builtin_base: str | Path | None = None) -> list[dict]:
    """[全局] 双源加载：内嵌 builtin（随 skill 版本）+ 外部 learned/user（学习产生）

    参数:
        external: 外部规则根（None → default_rules_dir()）
        builtin_base: 内嵌根（测试可覆盖；None → asset_root()）
    返回:
        规则列表（builtin 在前，level 字段已标注）
    """
    out = load_rules(builtin_rules_dir(builtin_base), level="builtin")
    ext = external or external_rules_dir()
    out += load_rules(ext, level="learned")
    out += load_rules(ext, level="user")
    return out


def _rules_file(rules_dir: str | Path, level: str, category: str) -> Path:
    return Path(rules_dir) / level / ("%s.json" % category)


def rules_manifest(rules_dir: str | Path) -> dict:
    """[全局] manifest：schema 版本与各级规则数（不存在则生成）"""
    d = Path(rules_dir)
    p = d / "manifest.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    manifest = {"schema_version": SCHEMA_VERSION, "levels": {}}
    for level in LEVELS:
        manifest["levels"][level] = len(load_rules(d, level=level))
    p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


# ---------------- 校验 ----------------

def validate_rule(rule: dict, require_id: bool = True) -> list[str]:
    """[全局] 规则 schema 校验，返回错误列表（空=合法）

    必需：rule_id/category/pattern_type/pattern/replacement；
    confidence ∈ [0,1]；scope 合法前缀；evidence 条目含 paper/before/after。
    """
    errs: list[str] = []
    for key in _REQUIRED:
        if key not in rule or rule.get(key) in (None, ""):
            errs.append("缺少必需字段: %s" % key)
    if require_id and rule.get("rule_id") and not _ID_RE.match(str(rule["rule_id"])):
        errs.append("rule_id 格式: R-YYYYMMDD-NNN")
    cat = rule.get("category")
    if cat and cat not in RULE_CATEGORIES:
        errs.append("category 非法: %s（可用 %s）" % (cat, RULE_CATEGORIES))
    pt = rule.get("pattern_type")
    if pt and pt not in PATTERN_TYPES:
        errs.append("pattern_type 非法: %s" % pt)
    conf = rule.get("confidence")
    if conf is not None and not (isinstance(conf, (int, float)) and 0 <= conf <= 1):
        errs.append("confidence 必须在 [0,1]")
    scope = rule.get("scope", "global")
    if scope and scope != "global" and not (
            scope.startswith("journal:") or scope.startswith("paper:")):
        errs.append("scope 非法: %s（global|journal:<x>|paper:<doi>）" % scope)
    src = rule.get("source")
    if src and src not in SOURCES:
        errs.append("source 非法: %s" % src)
    if rule.get("pattern_type") == "regex":
        try:
            re.compile(rule["pattern"])
        except re.error as e:
            errs.append("pattern 正则非法: %s" % e)
    for ev in rule.get("evidence", []) or []:
        for k in ("paper", "before", "after"):
            if k not in ev:
                errs.append("evidence 条目缺 %s" % k)
                break
    return errs


# ---------------- 加载 ----------------

def _load_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def load_rules(rules_dir: str | Path, level: str | None = None,
               category: str | None = None) -> list[dict]:
    """[全局] 加载规则（默认全部级别；可限定 level/category；按 level 优先级排序）"""
    d = Path(rules_dir)
    levels = [level] if level else list(LEVELS)
    cats = [category] if category else list(RULE_CATEGORIES)
    out: list[dict] = []
    for lv in levels:
        for cat in cats:
            for rule in _load_file(_rules_file(d, lv, cat)):
                rule = dict(rule)
                rule.setdefault("level", lv)
                out.append(rule)
    return out


def load_by_level(rules_dir: str | Path) -> dict[str, list[dict]]:
    """[全局] 按级别分组加载"""
    return {lv: load_rules(rules_dir, level=lv) for lv in LEVELS}


# ---------------- 写 ----------------

def save_rules(rules_dir: str | Path, level: str, category: str,
               rules: list[dict]) -> None:
    """[全局] 整类落盘（先校验；写入后更新 manifest）"""
    if level not in LEVELS:
        raise ValueError("level 非法: %s" % level)
    if category not in RULE_CATEGORIES:
        raise ValueError("category 非法: %s" % category)
    for r in rules:
        errs = validate_rule(r)
        if errs:
            raise ValueError("规则非法（%s）: %s" % (r.get("rule_id"), "; ".join(errs)))
    p = _rules_file(rules_dir, level, category)
    p.parent.mkdir(parents=True, exist_ok=True)
    # level 不入文件（由目录决定；load_rules 内存 setdefault）
    clean = [{k: v for k, v in r.items() if k != "level"} for r in rules]
    p.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    # 更新 manifest（整体重算）
    rules_manifest(rules_dir)


def add_rule(rules_dir: str | Path, rule: dict, level: str = "learned") -> dict:
    """[全局] 追加规则（rule_id 自动生成；重复 id/重复 pattern 拒绝）"""
    rule = dict(rule)
    if not rule.get("rule_id"):
        rule["rule_id"] = "R-%s-%03d" % (datetime.now(timezone.utc).strftime("%Y%m%d"),
                                         len(load_rules(rules_dir)) + 1)
    rule.setdefault("level", level)
    rule.setdefault("confidence", 0.7)
    rule.setdefault("auto_apply", rule["confidence"] >= 0.9)
    rule.setdefault("enabled", True)
    rule.setdefault("source", "mining")
    rule.setdefault("hits", 0)
    rule.setdefault("version", 1)
    rule.setdefault("evidence", [])
    rule.setdefault("scope", "global")
    rule.setdefault("created", _now())
    rule.setdefault("updated", rule["created"])
    errs = validate_rule(rule)
    if errs:
        raise ValueError("规则非法: %s" % "; ".join(errs))
    # 只操作本类别文件（防跨类别污染）
    existing = load_rules(rules_dir, level=level, category=rule["category"])
    existing = [dict(r, level=level) for r in existing]   # 补 level（load 时 setdefault）
    ids = {r["rule_id"] for r in existing}
    if rule["rule_id"] in ids:
        raise ValueError("rule_id 已存在: %s" % rule["rule_id"])
    key = (rule["category"], rule["pattern_type"], rule["pattern"])
    for r in existing:
        if (r["category"], r["pattern_type"], r["pattern"]) == key:
            raise ValueError("同 pattern 规则已存在（%s）" % r["rule_id"])
    existing.append(rule)
    save_rules(rules_dir, level, rule["category"], existing)
    return rule


def remove_rule(rules_dir: str | Path, rule_id: str, level: str | None = None) -> bool:
    """[全局] 删除规则（builtin 只读：仅允许 learned/user）"""
    for lv in ([level] if level else LEVELS):
        if lv == "builtin":
            continue
        for cat in RULE_CATEGORIES:
            rules = _load_file(_rules_file(rules_dir, lv, cat))
            before = len(rules)
            rules = [r for r in rules if r.get("rule_id") != rule_id]
            if len(rules) != before:
                save_rules(rules_dir, lv, cat, rules)
                return True
    return False


def set_enabled(rules_dir: str | Path, rule_id: str, enabled: bool,
                level: str | None = None) -> bool:
    """[全局] 启用/禁用规则（builtin 也可禁用）"""
    for lv in ([level] if level else LEVELS):
        for cat in RULE_CATEGORIES:
            rules = _load_file(_rules_file(rules_dir, lv, cat))
            for r in rules:
                if r.get("rule_id") == rule_id:
                    r["enabled"] = bool(enabled)
                    r["updated"] = _now()
                    save_rules(rules_dir, lv, cat, rules)
                    return True
    return False


# ---------------- 导出 / 导入 ----------------

def export_bundle(rules_dir: str | Path, out_path: str | Path,
                  levels: tuple[str, ...] = LEVELS,
                  note: str = "") -> Path:
    """[全局] 导出规则 bundle（单文件：规则 + 版本 + meta）"""
    rules = [r for r in load_rules(rules_dir) if r.get("level") in levels]
    bundle = {
        "bundle_version": BUNDLE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "exported_at": _now(),
        "meta": {"note": note, "rule_count": len(rules)},
        "rules": rules,
    }
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def import_bundle(rules_dir: str | Path, bundle_path: str | Path,
                  level: str = "learned") -> dict:
    """[全局] 导入 bundle（校验 schema；冲突=同 rule_id 已存在——不覆盖，返回冲突列表）"""
    data = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("bundle schema_version 不匹配: %s" % data.get("schema_version"))
    rules = data.get("rules") or []
    imported, conflicts, errors = 0, [], []
    for r in rules:
        errs = validate_rule(r)
        if errs:
            errors.append({"rule_id": r.get("rule_id"), "errors": errs})
            continue
        try:
            add_rule(rules_dir, r, level=level)
            imported += 1
        except ValueError as e:
            conflicts.append({"rule_id": r.get("rule_id"), "reason": str(e)})
    return {"imported": imported, "conflicts": conflicts, "errors": errors}


# ---------------- 合并（聚合） ----------------

def merge(rule_sets: list[list[dict]],
          source_order: tuple[str, ...] = ("user", "builtin", "learned")) -> dict:
    """[全局] 合并多来源规则：去重 + 冲突裁决

    冲突裁决（同 category+pattern_type+pattern 不同 replacement）：
    source 优先级（user>builtin>learned）→ confidence 高 → version 新。
    返回 {"rules": [...], "conflicts": [{pattern, kept, dropped, reason}]}
    """
    by_key: dict[tuple, dict] = {}
    conflicts: list[dict] = []
    for rules in rule_sets:
        for r in rules:
            key = (r.get("category"), r.get("pattern_type"), r.get("pattern"))
            repl = r.get("replacement")
            if key not in by_key:
                by_key[key] = dict(r)
                continue
            cur = by_key[key]
            if cur.get("replacement") == repl:
                # 去重：合并证据/命中数
                cur["hits"] = int(cur.get("hits", 0)) + int(r.get("hits", 0))
                cur["evidence"] = list(cur.get("evidence", [])) + \
                    [e for e in r.get("evidence", []) if e not in cur.get("evidence", [])]
                cur["updated"] = _now()
                continue
            # 冲突裁决（source_order 下标越小越优先：user=0 > builtin=1 > learned=2）
            def _pri(x: dict) -> tuple:
                lv = x.get("level", "learned")
                return (source_order.index(lv) if lv in source_order else 99,
                        -float(x.get("confidence", 0.0)), -int(x.get("version", 1)))
            if _pri(r) < _pri(cur):
                conflicts.append({"pattern": key[2], "kept": r.get("rule_id"),
                                  "dropped": cur.get("rule_id"),
                                  "reason": "来源/置信度/版本更高"})
                by_key[key] = dict(r)
            else:
                conflicts.append({"pattern": key[2], "kept": cur.get("rule_id"),
                                  "dropped": r.get("rule_id"),
                                  "reason": "来源/置信度/版本不占优"})
    return {"rules": sorted(by_key.values(), key=lambda x: x.get("rule_id", "")),
            "conflicts": conflicts}


# ---------------- 统计 ----------------

def stats(rules_dir: str | Path) -> dict:
    """[全局] 规则统计（按级别/类别/启用）"""
    by_level = load_by_level(rules_dir)
    out = {"levels": {lv: len(rules) for lv, rules in by_level.items()},
           "total": sum(len(r) for r in by_level.values()),
           "by_category": {cat: 0 for cat in RULE_CATEGORIES},
           "enabled": 0, "auto_apply": 0}
    for lv, rules in by_level.items():
        for r in rules:
            cat = r.get("category")
            if cat in out["by_category"]:
                out["by_category"][cat] += 1
            if r.get("enabled", True):
                out["enabled"] += 1
            if r.get("auto_apply", False):
                out["auto_apply"] += 1
    return out
