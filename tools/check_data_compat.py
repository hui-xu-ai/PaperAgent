#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""升级前数据兼容性检查（见 `docs/VERSIONING.md` §7）。**只读**，不改任何数据。

三件事：
  1) **清单契约**：`data/manifest.json` 是否存在、`data_format` 与代码是否一致
     （数据更新 → 必须升级程序；数据更旧 → 需要迁移）；
  2) **结构自检**：五库文件、必需表/列是否齐（缺表/缺列会在这里暴露，而不是等到运行时崩）；
  3) **黄金夹具升级演练**：对 `tests/fixtures/data-v*/` 里的历史数据夹具跑当前代码的
     迁移 + 读取断言（有夹具时必须通过；无夹具时明确提示"尚未冻结夹具"）。

用法：
  python tools/check_data_compat.py                 # 检查真实 data/
  python tools/check_data_compat.py --fixtures-only # 只跑夹具
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "paperkb"))
sys.path.insert(0, str(ROOT / "backend"))

# 每个库"必须有"的表（当前格式的契约）
REQUIRED = {
    "system": {"tasks", "llm_usage", "settings", "plugins"},
    "chat": {"sessions", "messages", "answer_cache"},
    "biblio": {"papers", "papers_meta", "identifiers", "doi_md5_map", "compile_jobs",
               "meta_fts", "notes_fts", "fulltext_fts", "kb_edited"},
    "reference": {"jcr", "cas"},
}
FIXTURE_GLOB = "tests/fixtures/data-v*"


def _tables(db: Path) -> set[str]:
    if not db.exists():
        return set()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    finally:
        con.close()


def check_live() -> int:
    from app.config import DATA_FORMAT
    from app.services.container import _build_roots
    from paperkb.manifest import read_manifest

    roots = _build_roots()
    man = read_manifest(roots)
    bad = 0
    print(f"[1] 清单契约  data/manifest.json")
    if man is None:
        print("    ! 缺清单：先启动一次应用（会自动按当前格式认领）")
        bad += 1
    else:
        df = int(man.get("data_format") or 0)
        flag = "OK" if df == DATA_FORMAT else ("数据更旧→需迁移" if df < DATA_FORMAT
                                              else "数据更新→必须升级程序")
        print(f"    {'OK ' if df == DATA_FORMAT else '!! '}data_format={df} "
              f"代码={DATA_FORMAT} layout={man.get('layout')} → {flag}")
        bad += 0 if df == DATA_FORMAT else 1
    print(f"[2] 结构自检")
    for name, tables in REQUIRED.items():
        db = {"system": roots.system_db, "chat": roots.chat_db, "diary": roots.diary_db,
              "biblio": roots.biblio_db, "reference": roots.reference_db}[name]
        have = _tables(db)
        miss = tables - have
        if not db.exists():
            if name in ("diary", "reference"):
                print(f"    -  {name:9s} 未创建（可选）")
                continue
            print(f"    !! {name:9s} 缺文件：{db}")
            bad += 1
            continue
        print(f"    {'OK ' if not miss else '!! '}{name:9s} 表 {len(have):3d} 个"
              + (f"，缺 {sorted(miss)}" if miss else ""))
        bad += 1 if miss else 0
    return bad


def check_fixtures() -> int:
    """对历史数据夹具跑「当前代码能否打开 + 迁移」。夹具缺失只提示，不算失败。"""
    dirs = sorted(ROOT.glob(FIXTURE_GLOB))
    if not dirs:
        print(f"[3] 黄金夹具  未冻结（约定：每次发布把上一版本极小数据放到 {FIXTURE_GLOB}/）")
        print("    → 建议：从真实 data/ 导出极小夹具（几 KB）冻结，升级测试才可复现")
        return 0
    bad = 0
    for d in dirs:
        print(f"[3] 黄金夹具  {d.relative_to(ROOT)}")
        with tempfile.TemporaryDirectory(prefix="compat-") as tmp:
            work = Path(tmp) / "data"
            shutil.copytree(d / "data", work)
            from paperkb.config import Roots
            from paperkb.manifest import ensure_manifest, read_manifest

            roots = Roots(data_dir=work, library_dir=Path(tmp) / "library",
                          kb_dir=Path(tmp) / "kb").ensure()
            man = read_manifest(roots) or {}
            from app.config import APP_VERSION, DATA_FORMAT, LAYOUT_VERSION
            try:
                res = ensure_manifest(roots, app_version=APP_VERSION,
                                      data_format=DATA_FORMAT, layout=LAYOUT_VERSION)
            except Exception as e:  # noqa: BLE001
                print(f"    !! 清单校验失败：{e}")
                bad += 1
                continue
            need = res.get("needs_migration")
            # 2026-09-12 修复（发布闸门 G2）：这里**真的跑一遍迁移**——旧版只查表，
            # 迁移链坏掉也照样"通过"，与 docstring/VERSIONING §7 的声明不符。
            from paperkb.migrations import run_migrations

            dry = run_migrations(roots, target_format=DATA_FORMAT, dry_run=True)
            rep = run_migrations(roots, target_format=DATA_FORMAT)
            applied = rep.get("applied") or []
            healed = rep.get("self_healed") or []
            print(f"    夹具 data_format={man.get('data_format')} → "
                  f"{'需要迁移' if need else '与当前格式一致'}；实跑："
                  f"applied={applied or '无'} self_healed={healed or '无'} "
                  f"（dry-run 计划={dry.get('planned') or '无'}）")
            if need and not applied:
                print("    !! 夹具落后于当前格式，却没有任何迁移被执行（迁移链断裂）")
                bad += 1
            if applied and not rep.get("backup"):
                print("    !! 迁移执行了但没有备份产物（违反 VERSIONING §2.6）")
                bad += 1
            for name, tables in REQUIRED.items():
                db = {"system": roots.system_db, "chat": roots.chat_db,
                      "diary": roots.diary_db, "biblio": roots.biblio_db,
                      "reference": roots.reference_db}[name]
                have = _tables(db)
                if have and (tables - have):
                    print(f"    !! {name} 缺表 {sorted(tables - have)}")
                    bad += 1
    return bad


def check_migration_chain() -> int:
    """迁移链自检：每条迁移必须**可自证**（NAME + verify）且 VERSION ≤ 当前 DATA_FORMAT。

    自证能力是"同格式漏跑自愈"的前提（见 `packages/paperkb/paperkb/migrations/__init__.py`）。
    """
    from app.config import DATA_FORMAT
    from paperkb.migrations import available

    print("[4] 迁移链自检")
    bad = 0
    mods = available()
    if not mods:
        print("    !! 没有任何迁移模块（数据格式变更将无法升级）")
        return 1
    for m in mods:
        name = getattr(m, "NAME", "")
        ver = int(getattr(m, "VERSION", 0))
        ok = bool(name) and callable(getattr(m, "verify", None)) and ver <= DATA_FORMAT
        print(f"    {'OK ' if ok else '!! '}{getattr(m, '__name__', '?')} "
              f"VERSION={ver} NAME={name or '<缺>'} verify="
              f"{'有' if callable(getattr(m, 'verify', None)) else '缺'}")
        bad += 0 if ok else 1
    return bad


def main() -> int:
    # 2026-09-12（发布闸门 G1）：Windows 控制台默认 GBK，打印 ✅/❌ 会 UnicodeEncodeError
    # → 退出码恒为 1（与是否通过无关），CI/人工信号全部失效。双保险：重配 UTF-8 + ASCII 结论。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 老解释器/重定向场景
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures-only", action="store_true")
    ap.add_argument("--json", default="")
    args = ap.parse_args()
    bad = 0
    if not args.fixtures_only:
        bad += check_live()
    bad += check_fixtures()
    bad += check_migration_chain()
    print(f"\n结论: {'[PASS] 兼容性检查通过' if bad == 0 else f'[FAIL] {bad} 项需处理'}")
    if args.json:
        Path(args.json).write_text(json.dumps({"issues": bad, "pass": bad == 0},
                                              ensure_ascii=False), encoding="utf-8")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
