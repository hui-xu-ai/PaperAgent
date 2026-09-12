# -*- coding: utf-8 -*-
"""迁移前的一致性备份（`VACUUM INTO`，见 `docs/VERSIONING.md` §7）。

为什么用 `VACUUM INTO` 而不是复制文件：WAL 模式下直接拷 `.db` 会漏掉 `-wal` 里的已提交事务，
`VACUUM INTO` 由 SQLite 自己产出**一致快照**，且体积紧凑。备份位置
`data/_backups/<tag>-<时间戳>/`，失败不阻塞迁移（但要记日志——迁移前无备份属高风险）。
"""
from __future__ import annotations

import logging
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["backup_databases"]


def backup_databases(roots, tag: str = "backup") -> str:
    """把四个库导出到 `data/_backups/<tag>-<ts>/`，返回备份目录（失败返回 ""）。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(roots.data_dir) / "_backups" / f"{tag}-{stamp}"
    try:
        out.mkdir(parents=True, exist_ok=True)
        for name, db in (("system", roots.system_db), ("chat", roots.chat_db),
                         ("diary", roots.diary_db), ("biblio", roots.biblio_db),
                         ("reference", roots.reference_db)):
            src = Path(db)
            if not src.exists():
                continue
            dst = out / f"{name}-{src.name}"
            try:
                conn = sqlite3.connect(str(src))
                try:
                    conn.execute("VACUUM INTO ?", (str(dst),))
                finally:
                    conn.close()
            except sqlite3.Error as e:       # VACUUM INTO 失败（旧 SQLite/占用）→ 退回文件拷贝
                logger.warning("VACUUM INTO 失败（退回文件拷贝）%s: %s", src.name, e)
                shutil.copy2(src, dst)
        manifest = Path(roots.data_dir) / "manifest.json"
        if manifest.exists():
            shutil.copy2(manifest, out / "manifest.json")
        logger.info("迁移前备份完成: %s", out)
        return str(out)
    except Exception as e:  # noqa: BLE001 - 备份失败只告警
        logger.warning("迁移前备份失败: %s", e)
        return ""
