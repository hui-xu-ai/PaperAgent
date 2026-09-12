# -*- coding: utf-8 -*-
"""迁移层（见 `docs/VERSIONING.md` §3）。

**兼容旧格式的代码只许住在这里**：业务层永远只读"当前格式"。
每个迁移模块暴露：
    VERSION = <目标 DATA_FORMAT>
    NAME = "<简短名>"
    def up(conn) -> None       # 幂等；只做"旧格式 → 新格式"
    def verify(conn) -> list[str]   # 返回问题列表（空 = 通过）——**必填**，见下

`run_migrations()` 由启动流程调用：
  1. 读 `data/manifest.json` 的 `data_format`，并读**台账** `main.migrations_applied`；
  2. 选步规则（2026-09-12 修复，见下"为什么需要 verify 自证"）：
     - 台账已登记 → 跳过；
     - `VERSION <= 当前格式` 但台账没有 → **跑 verify() 自证**：能自证已生效就补登记，
       证不实（历史版本只跑了同版本的另一条迁移）就**补跑 up()**；
     - `VERSION > 当前格式` → 必须执行。
  3. 真要执行时：**先备份**（`VACUUM INTO`，失败即中止）→ 顺序执行（各一事务，同事务写台账）
     → 逐条 `verify()` → 写回 manifest。

为什么需要 verify 自证（旧实现的数据风险）：旧选步是 `cur < VERSION <= target`，
而 `data_format` 是**格式**不是**步数**——0002/0003 同为 VERSION=2、都是 "1 → 2" 的步骤。
于是"数据已是格式 2、但当时只跑了 0002"的库，在新版本里 0003 会被**永久跳过**（`cur >= target`
直接 return），缺列/主键未换却无人报错。现在同格式漏跑的步会被 verify() 抓到并补上。
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["available", "apply", "plan_migrations", "run_migrations"]

LEDGER_DDL = ("CREATE TABLE IF NOT EXISTS main.migrations_applied ("
              "name TEXT PRIMARY KEY, target_format INTEGER, applied_at TEXT)")


def available() -> list:
    """按 VERSION 升序（同版本按文件名顺序）返回迁移模块列表。"""
    mods = []
    for info in pkgutil.iter_modules([str(Path(__file__).parent)]):
        if not info.name.startswith(("0", "1", "2", "3", "4", "5", "6", "7", "8", "9")):
            continue
        mod = importlib.import_module(f"{__name__}.{info.name}")
        if hasattr(mod, "VERSION") and hasattr(mod, "up"):
            mods.append(mod)
    mods.sort(key=lambda m: (int(m.VERSION), getattr(m, "__name__", "")))
    return mods


def _name_of(mod) -> str:
    return getattr(mod, "NAME", mod.__name__)


def _ledger_names(conn) -> set[str]:
    """已登记迁移名（表不存在 → 空集，**不建表**，保持探测只读）。"""
    try:
        return {r[0] for r in conn.execute("SELECT name FROM main.migrations_applied")}
    except sqlite3.Error:
        return set()


def _record(conn, name: str, version: int) -> None:
    conn.execute(LEDGER_DDL)
    conn.execute("INSERT OR REPLACE INTO main.migrations_applied"
                 "(name, target_format, applied_at) VALUES(?,?,?)",
                 (name, int(version), datetime.now().isoformat(timespec="seconds")))


def plan_migrations(conn, mods: list, cur: int, target: int) -> tuple[list, list[str]]:
    """选出要执行的迁移；返回 `(待执行模块, 自证已生效的名字)`。**只读**。"""
    recorded = _ledger_names(conn)
    todo: list = []
    healed: list[str] = []
    for m in mods:
        name = _name_of(m)
        ver = int(getattr(m, "VERSION", 0))
        if ver > target or name in recorded:
            continue
        if ver <= cur:
            problems = m.verify(conn) if hasattr(m, "verify") else []
            if not problems:
                healed.append(name)          # 已生效：无需重跑（台账在写入路径补登记）
                continue
            logger.warning("迁移 %s 声称已包含在格式 %s 中，但自证不通过（%s）→ 补跑",
                           name, cur, problems[:3])
        todo.append(m)
    return todo, healed


def apply(conn, migrations: list) -> list[str]:
    """在**同一连接**里顺序执行迁移并写台账；返回执行过的名字列表。"""
    done = []
    for m in migrations:
        name = _name_of(m)
        m.up(conn)
        problems = m.verify(conn) if hasattr(m, "verify") else []
        if problems:
            raise RuntimeError(f"迁移校验失败 {name}: {problems}")
        _record(conn, name, int(getattr(m, "VERSION", 0)))
        done.append(name)
        logger.info("迁移已应用: %s (→ data_format %s)", name, getattr(m, "VERSION", "?"))
    return done


def _connect(roots) -> sqlite3.Connection:
    """系统库为主连接，其余库 ATTACH（与 backend Store 同一形状，跨库 DDL 同事务）。"""
    sys_db = Path(roots.system_db)
    sys_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(sys_db))
    conn.row_factory = sqlite3.Row
    for alias, path in (("chat", roots.chat_db), ("biblio", roots.biblio_db),
                        ("reference", roots.reference_db)):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        conn.execute("ATTACH DATABASE ? AS %s" % alias, (str(p),))
    return conn


def run_migrations(roots, *, target_format: int, dry_run: bool = False) -> dict:
    """把 `data/` 里的库迁移到 `target_format`；返回报告（供启动日志/发布检查用）。

    数据格式落后 → 迁移；数据格式相同但**同格式内漏跑的步** → 自证后补跑（漏跑修复）；
    数据更新由 `manifest.ensure_manifest` 提前拦住。
    """
    from ..manifest import adopt_data_format, read_manifest, write_manifest
    from ..version import MIN_READABLE_DATA_FORMAT

    man = read_manifest(roots) or {}
    # 缺 manifest（历史数据）→ 按最旧可读格式起步，保证迁移一定会跑
    cur = int(man.get("data_format")
              or adopt_data_format(roots, data_format=target_format,
                                   min_readable=MIN_READABLE_DATA_FORMAT))
    mods = available()
    # 探测：五库里一个文件都没有 → 全新安装，没有可迁移的东西（也不去建空库文件）
    # 注意 `roots.main_db` 是 `biblio_db` 的兼容旧名，不能只探测 system_db（实测踩过：
    # 老单库/老测试把 papers_meta 建在 biblio，只探 system 会漏掉整条迁移链）。
    db_files = [Path(p) for p in (roots.system_db, roots.chat_db, roots.diary_db,
                                  roots.biblio_db, roots.reference_db)]
    if not any(p.exists() for p in db_files):
        todo, healed = [], []
    else:
        conn = _connect(roots)
        try:
            todo, healed = plan_migrations(conn, mods, cur, target_format)
        finally:
            conn.close()

    planned = [_name_of(m) for m in todo]
    report = {"from": cur, "to": target_format, "applied": [], "backup": "",
              "needs_migration": bool(todo), "planned": planned,
              "self_healed": healed,
              "reason": ("数据格式落后，需迁移" if cur < target_format and todo else
                         ("同格式有漏跑的迁移，需补跑" if todo else "已是最新"))}
    if dry_run or not todo:
        return report

    from ..backup import backup_databases

    report["backup"] = backup_databases(roots, tag=f"pre-v{cur}-to-v{target_format}")
    if not report["backup"]:
        # VERSIONING §2.6「升级前必做备份」：没备份就绝不改用户库（宁可启动报错让人处理）
        raise RuntimeError(
            "迁移前备份失败（VACUUM INTO 无产物）→ 已中止迁移，数据保持原样；"
            "请检查 data/ 可写与磁盘空间")

    conn = _connect(roots)
    try:
        conn.execute("BEGIN")
        try:
            applied = apply(conn, todo)
            if cur <= target_format:          # 自证已生效的步一并登记（库内自证）
                for name in healed:
                    _record(conn, name, cur)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()
    report["applied"] = applied
    report["self_healed"] = healed

    if cur < target_format:
        man["data_format"] = target_format
    man["needs_migration"] = False
    man["migrated_at"] = datetime.now().isoformat(timespec="seconds")
    write_manifest(roots, man)
    return report
