# -*- coding: utf-8 -*-
"""黄金夹具脱敏（发布前必跑）：**只改夹具，不动任何用户数据**。

背景（2026-09-12 发布前审计实测）：`tests/fixtures/data-v2/data/system/app.db` 的
`settings.providers` 里带着**三个真实 API Key**（DeepSeek / SiliconFlow / 智谱），
`chat.db` 里带着用户真实提问与回答，`biblio.db` 里带着用户文献标题与**本机绝对路径**
——夹具是 `tools/freeze_data_fixture.py` 从真实实例直接拷贝的，于是这些内容随仓库公开。

策略（脱敏后仍需能跑迁移演练：结构、行数、schema 一律不动）：
  · 密钥形态 → `sk-REDACTED-EXAMPLE` / `REDACTED-EXAMPLE.KEY`
  · 本机绝对路径 → `C:/example/path`
  · 用户生成文本（对话消息、回答缓存、笔记、标题）→ 占位文本（保留行数）
  · FTS5 影子索引重建（否则索引里仍残留原文分词）

用法：
  python tools\\sanitize_fixtures.py --dry-run      # 先看会改哪些值
  python tools\\sanitize_fixtures.py                # 执行（幂等）
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_GLOB = "tests/fixtures/data-v*"

# 值级脱敏（按列名 / 内容判据分工，见 _redact_value）
RX_SECRET = re.compile(r"(sk-[A-Za-z0-9_\-]{12,}|[0-9a-f]{32}\.[A-Za-z0-9]{12,})")
RX_WINPATH = re.compile(r"[A-Za-z]:\\\\?[^\s\"'|<>]*")
RX_POSIXPATH = re.compile(r"/(?:home|Users|mnt)/[^\s\"'|<>]*")

# 这些列承载**用户生成内容** ⇒ 直接换成占位（保留行数，不影响迁移演练）
TEXT_COLUMNS = {"content", "text", "answer", "question", "title", "note", "summary"}
PLACEHOLDER = "（示例内容·发布前已脱敏）"


def _redact_value(col: str, val: object) -> object:
    if not isinstance(val, str) or not val:
        return val
    out = RX_SECRET.sub(lambda m: "sk-REDACTED-EXAMPLE" if m.group(0).startswith("sk-")
                        else "REDACTED-EXAMPLE.KEY", val)
    out = RX_WINPATH.sub("C:/example/path", out)
    out = RX_POSIXPATH.sub("/example/path", out)
    if col.lower() in TEXT_COLUMNS and out.strip():
        # 保留 JSON 结构（providers 之类的大对象不能整体替换，只替换密钥字段）
        if not out.lstrip().startswith(("{", "[")):
            return PLACEHOLDER
    return out


def sanitize_db(db: Path, dry: bool) -> list[str]:
    changes: list[str] = []
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for t in tables:
        if t.startswith("sqlite_") or t.endswith(("_data", "_idx", "_docsize", "_config")):
            continue
        try:
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
            if not cols:
                continue
            try:
                rows = con.execute(f"SELECT rowid AS __rid, * FROM {t}").fetchall()
                pk = "__rid"
            except sqlite3.OperationalError:
                # 无 rowid 的表（WITHOUT ROWID / FTS5 影子表）：不做行级 UPDATE（只读跳过）
                changes.append(f"  - {t}: 无 rowid，跳过")
                continue
        except Exception as e:  # noqa: BLE001
            changes.append(f"  ! {t}: 跳过（{str(e)[:60]}）")
            continue
        for row in rows:
            updates = {}
            for c in cols:
                new = _redact_value(c, row[c])
                if new != row[c]:
                    updates[c] = new
                    changes.append(f"  {t}.{c}: {str(row[c])[:60]!r} → {str(new)[:60]!r}")
            if updates and not dry:
                sets = ", ".join(f"{k}=?" for k in updates)
                con.execute(f"UPDATE {t} SET {sets} WHERE rowid=?",
                            (*updates.values(), row[pk]))
    # FTS5 影子索引重建（脱敏后必须重建，否则索引里仍存原文分词）
    if not dry:
        for (name,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%fts5%'"):
            try:
                con.execute(f"INSERT INTO {name}({name}) VALUES('rebuild')")
                changes.append(f"  ↻ FTS5 重建：{name}")
            except Exception as e:  # noqa: BLE001
                changes.append(f"  ! FTS5 重建失败 {name}: {str(e)[:60]}")
    if not dry:
        con.commit()
        con.execute("VACUUM")
    con.close()
    return changes


def sanitize_all(dry: bool = False) -> int:
    total = 0
    for fixture in sorted(ROOT.glob(FIXTURE_GLOB)):
        for db in sorted(fixture.rglob("*.db")):
            ch = sanitize_db(db, dry)
            real = [c for c in ch if " → " in c]     # 只统计真实改值（不含 FTS 重建/跳过提示）
            print(f"\n=== {db.relative_to(ROOT)}（{len(real)} 处）===")
            for c in ch[:40]:
                print(c)
            total += len(real)
    print(f"\n{'[dry-run] 将修改' if dry else '已修改'} {total} 处")
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    sanitize_all(dry=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
