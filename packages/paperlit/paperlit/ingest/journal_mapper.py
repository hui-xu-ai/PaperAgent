# -*- coding: utf-8 -*-
"""期刊名映射：缩写 → 全称的快速查询与自动填充。

设计目标：
- 导入时自动查表补全期刊全称（本地 DB 查询，毫秒级）
- 支持 50 万篇文献规模（预建映射表，避免实时 API 调用）
- 后台异步填充新映射（OpenAlex API）
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class JournalMapper:
    """期刊名映射器（缩写 → 全称）。"""

    def __init__(self, journals_db_path: Path | str):
        self.db_path = Path(journals_db_path)
        self._cache: dict[str, str] = {}
        self._load_cache()

    def _load_cache(self) -> None:
        """加载映射表到内存缓存。"""
        if not self.db_path.exists():
            logger.warning("journals.db not found: %s", self.db_path)
            return

        conn = sqlite3.connect(self.db_path)
        try:
            # 检查表是否存在
            table_exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='journal_mappings'"
            ).fetchone()

            if not table_exists:
                logger.info("journal_mappings table not found, creating...")
                self._create_table(conn)
                return

            rows = conn.execute(
                "SELECT abbreviation, full_name FROM journal_mappings"
            ).fetchall()

            for abbrev, full_name in rows:
                # 缓存两种形式：原样和大写
                self._cache[abbrev] = full_name
                self._cache[abbrev.upper()] = full_name

            logger.info("Loaded %d journal mappings into cache", len(self._cache))
        finally:
            conn.close()

    def _create_table(self, conn: sqlite3.Connection) -> None:
        """创建映射表。"""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS journal_mappings (
                abbreviation TEXT PRIMARY KEY,
                full_name TEXT NOT NULL,
                source TEXT DEFAULT 'openalex',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

    def lookup(self, journal_name: str) -> Optional[str]:
        """查询期刊全称。

        Args:
            journal_name: 期刊名（可能是缩写）

        Returns:
            全称（如果找到映射），否则返回 None
        """
        if not journal_name:
            return None

        # 直接查缓存
        if journal_name in self._cache:
            return self._cache[journal_name]

        # 大写查缓存
        upper_name = journal_name.upper()
        if upper_name in self._cache:
            return self._cache[upper_name]

        return None

    def normalize(self, journal_name: str) -> str:
        """规范化期刊名（有映射则返回全称，否则返回原样）。

        Args:
            journal_name: 期刊名

        Returns:
            全称或原样
        """
        mapped = self.lookup(journal_name)
        return mapped if mapped else journal_name

    def add_mapping(self, abbreviation: str, full_name: str,
                    source: str = "manual") -> bool:
        """添加映射到数据库和缓存。

        Args:
            abbreviation: 缩写
            full_name: 全称
            source: 来源（manual/openalex/user）

        Returns:
            是否成功添加
        """
        if not abbreviation or not full_name:
            return False

        if abbreviation == full_name:
            return False  # 不需要映射

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO journal_mappings (abbreviation, full_name, source) "
                "VALUES (?, ?, ?)",
                (abbreviation, full_name, source),
            )
            conn.commit()

            # 更新缓存
            self._cache[abbreviation] = full_name
            self._cache[abbreviation.upper()] = full_name

            return True
        except Exception as e:
            logger.error("Failed to add mapping %s -> %s: %s", abbreviation, full_name, e)
            return False
        finally:
            conn.close()

    def bulk_add_mappings(self, mappings: dict[str, str],
                          source: str = "openalex") -> int:
        """批量添加映射。

        Args:
            mappings: {abbreviation: full_name, ...}
            source: 来源

        Returns:
            成功添加的数量
        """
        if not mappings:
            return 0

        conn = sqlite3.connect(self.db_path)
        added = 0
        try:
            for abbrev, full_name in mappings.items():
                if not abbrev or not full_name or abbrev == full_name:
                    continue

                conn.execute(
                    "INSERT OR REPLACE INTO journal_mappings (abbreviation, full_name, source) "
                    "VALUES (?, ?, ?)",
                    (abbrev, full_name, source),
                )
                added += 1

                # 更新缓存
                self._cache[abbrev] = full_name
                self._cache[abbrev.upper()] = full_name

            conn.commit()
            logger.info("Bulk added %d journal mappings", added)
        except Exception as e:
            logger.error("Failed to bulk add mappings: %s", e)
            conn.rollback()
        finally:
            conn.close()

        return added

    def get_stats(self) -> dict:
        """获取映射表统计。"""
        conn = sqlite3.connect(self.db_path)
        try:
            total = conn.execute("SELECT COUNT(*) FROM journal_mappings").fetchone()[0]
            by_source = conn.execute(
                "SELECT source, COUNT(*) FROM journal_mappings GROUP BY source"
            ).fetchall()

            return {
                "total": total,
                "cache_size": len(self._cache),
                "by_source": {src: cnt for src, cnt in by_source},
            }
        finally:
            conn.close()
