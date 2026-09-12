#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建期：把 JCR 分区表 xlsx 解析成**可直接随包分发**的 `journals.db`。

为什么在构建期做（用户 2026-09-12 拍板）：
- 运行期**不再现场解析**（2.2 万行要 20+ 秒，会把首启卡成"双击没反应"）；
- 随包放**已解析好的 db**，首启只需把文件拷进 `data/reference/`（毫秒级）；
- 同时把**原始 Excel** 也打进包里（`share/reference/JCR分区.xlsx`），用户日后按同格式更新时直接导入即可。

产物（默认 `build/reference_seed/journals.db`）：
- 用 `VACUUM INTO` 导出 ⇒ 单文件、无 `-wal/-shm` 残留、无碎片，直接拷即可用；
- 合并 xlsx 里所有 sheet/年份（与运行期 `journals_import` 同一解析器 `paperkb.xlsx_import`）。

用法：
    python tools/build_reference_seed.py                     # 默认读用户语料里的 xlsx
    python tools/build_reference_seed.py --xlsx <路径> --out <路径>
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packages" / "paperkb"))

DEFAULT_XLSX = ROOT / "用户提供的文献" / "影响因子和分区" / "JCR分区.xlsx"
DEFAULT_OUT = ROOT / "build" / "reference_seed" / "journals.db"


def build(xlsx: Path, out: Path) -> dict:
    from paperkb.config import Roots
    from paperkb.journals import JournalsDB
    from paperkb.xlsx_import import import_xlsx

    if not xlsx.exists():
        raise FileNotFoundError(f"找不到 JCR xlsx: {xlsx}")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    t0 = time.time()
    # ignore_cleanup_errors：paperkb 的 JournalsDB 用完连接后靠 GC 释放，Windows 上临时目录
    # 可能一时删不掉（WinError 32）——那是清理噪音，不该让构建失败。
    with tempfile.TemporaryDirectory(prefix="ref-seed-",
                                     ignore_cleanup_errors=True) as tmp:
        t = Path(tmp)
        roots = Roots(data_dir=t / "data", library_dir=t / "library",
                      kb_dir=t / "kb").ensure()
        jdb = JournalsDB(roots)
        jdb.init_schema()
        stats = import_xlsx(xlsx, jdb)
        # 单文件导出：用 sqlite backup API（比 VACUUM INTO 稳——Windows 上源库若有连接未释放，
        # VACUUM INTO 会报"另一个程序正在使用此文件"，实测踩到）
        src = sqlite3.connect(str(roots.reference_db))
        dst = sqlite3.connect(str(out))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    # 自检：导出的 db 必须能读出数据
    con = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    try:
        jcr = con.execute("SELECT COUNT(*) FROM jcr").fetchone()[0]
        cas = con.execute("SELECT COUNT(*) FROM cas").fetchone()[0]
    finally:
        con.close()
    if not jcr and not cas:
        raise RuntimeError("导出的 journals.db 是空库——检查 xlsx 格式/工作表名")
    return {"xlsx": str(xlsx), "out": str(out), "jcr": jcr, "cas": cas,
            "mb": round(out.stat().st_size / 1024 / 1024, 2),
            "elapsed_s": round(time.time() - t0, 1),
            "imported": {k: v for k, v in stats.items() if "import" in k or "total" in k}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xlsx", default=str(DEFAULT_XLSX))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()
    try:
        r = build(Path(args.xlsx), Path(args.out))
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] 生成期刊分区表种子失败: {e}")
        return 1
    print(f"[PASS] 期刊分区表种子已生成: {r['out']}")
    print(f"       jcr={r['jcr']} cas={r['cas']} 体积={r['mb']}MB 用时={r['elapsed_s']}s")
    print(f"       源（随包提供，供日后更新）: {r['xlsx']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
