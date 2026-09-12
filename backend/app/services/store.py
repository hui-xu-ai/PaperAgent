# -*- coding: utf-8 -*-
"""SQLite 存储层（T04）。

表：
- papers    论文（pdf 路径 / document.json 路径 / 状态）
- tasks     后台任务（解析/翻译/导出 阶段进度）
- sessions  对话会话（一论文可多会话）
- messages  对话消息（**只存讨论消息**，翻译全文绝不入库）
- answer_cache  回答缓存（规范化问题 hash → 答案，重复问题零 token）

并发：单用户单进程；写操作加进程内锁，WAL 模式允许并发读。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 常量

PAPER_STATUS = {
    "pending": "待处理", "parsing": "解析中", "parsed": "已解析",
    "translating": "翻译中", "translated": "已完成", "failed": "失败",
}
TASK_STATES = ("pending", "running", "done", "failed")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Store:
    """SQLite 访问层（每次操作新建连接；写操作加锁）。"""

    def __init__(self, db_path: str, chat_db: str | Path | None = None,
                 biblio_db: str | Path | None = None):
        """多库构造（2026-09-12 用户拍板 · 工业级数据布局）。

        main = **系统库** `data/system/app.db`（settings/plugins/llm_usage/tasks）；
        `chat` 附加库 = `data/chat/chat.db`（sessions/messages/answer_cache）；
        `biblio` 附加库 = `data/biblio/biblio.db`（papers + paperkb 的元数据表）。
        用 `ATTACH DATABASE` 而不是拆成多个 Store：SQLite 的**未限定表名按
        main → 附加库顺序解析**，所以既有的 100+ 条 SQL 一行都不用改就落到正确的库；
        建表语句按库限定（见 `_init_db`）。
        缺省参数按 main 同根推导（兼容单测只传一个路径的用法）。
        """
        self.db_path = str(db_path)
        base = Path(self.db_path).parent
        # 缺省按"同目录同名库"推导（单测只传一个路径时不会写到 tmp 之外）；
        # 生产由 container 显式注入 data/{chat,biblio}/*.db。
        self.chat_db = str(chat_db or (base / "chat.db"))
        self.biblio_db = str(biblio_db or (base / "biblio.db"))
        self._lock = threading.Lock()
        self._init_db()

    # ---------------------------------------------------------- 基础
    @contextmanager
    def _conn(self):
        """每次操作新建连接并确保关闭（with 只管理事务，不关闭连接）。

        三个库都开 WAL；附加库在建连接时挂上（路径目录不存在则自动建）。
        """
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            for alias, path in (("chat", self.chat_db), ("biblio", self.biblio_db)):
                if not Path(path).is_absolute():
                    path = str((Path(self.db_path).parent / path).resolve())
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                conn.execute("ATTACH DATABASE ? AS %s" % alias, (path,))
                conn.execute("PRAGMA %s.journal_mode=WAL" % alias)
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._conn() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS biblio.papers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT DEFAULT '',
                pdf_name TEXT DEFAULT '',
                pdf_path TEXT NOT NULL,
                doc_json TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                error TEXT DEFAULT '',
                run_id TEXT DEFAULT '',
                pipeline_mode TEXT DEFAULT 'full',
                md_template TEXT DEFAULT '',
                pdf_md5 TEXT DEFAULT '',
                parse_source TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id INTEGER NOT NULL,          -- 跨库引用 papers（biblio），SQLite 不支持跨库 FK
                kind TEXT NOT NULL,
                state TEXT DEFAULT 'pending',
                progress INTEGER DEFAULT 0,
                message TEXT DEFAULT '',
                error TEXT DEFAULT '',
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS chat.sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id INTEGER,                   -- 跨库引用 papers（biblio），不建 FK
                kind TEXT DEFAULT 'paper',
                mode TEXT DEFAULT '',               -- 知识库会话模式（qa/manage）；老库由迁移层补列
                title TEXT DEFAULT '',
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS chat.messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES sessions(id),
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tokens INTEGER DEFAULT 0,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS chat.answer_cache (
                key TEXT PRIMARY KEY,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS llm_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                context TEXT NOT NULL,
                provider TEXT DEFAULT '',
                model TEXT DEFAULT '',
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                cache_hit_tokens INTEGER DEFAULT 0,
                cost REAL DEFAULT 0,
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_llm_usage_context ON llm_usage(context);
            CREATE INDEX IF NOT EXISTS idx_llm_usage_created ON llm_usage(created_at);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS plugins (
                id TEXT PRIMARY KEY,
                enabled INTEGER DEFAULT 1,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS biblio.kb_edited (
                path TEXT PRIMARY KEY,
                ts TEXT
            );
            """)
            # 2026-09-12：**内联"试探式补列"已全部搬进迁移层**
            # `paperkb/migrations/0001_baseline.py`（见 docs/VERSIONING.md §3、
            # docs/COMPAT-REGISTER.md A3/A4）：业务/数据层只建"当前格式"，
            # 旧库由启动时的迁移 runner 先迁移再打开；此处不再做 ALTER 试探。

    # ---------------------------------------------------------- papers
    def create_paper(self, pdf_path: str, title: str = "", pdf_md5: str = "",
                     pdf_name: str = "") -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO papers(title, pdf_name, pdf_path, pdf_md5, status, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (title, pdf_name or title, pdf_path, pdf_md5, "pending", _now(), _now()))
            return int(cur.lastrowid)

    def find_paper_by_md5(self, md5: str) -> dict | None:
        """G11：按 PDF md5 查已处理记录（批量去重）。"""
        if not md5:
            return None
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM papers WHERE pdf_md5=? ORDER BY id DESC LIMIT 1", (md5,)).fetchone()
        return dict(row) if row else None

    def get_paper(self, paper_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        return dict(row) if row else None

    def list_papers(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM papers ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def update_paper(self, paper_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        keys = ", ".join(f"{k}=?" for k in fields)
        with self._lock, self._conn() as conn:
            conn.execute(f"UPDATE papers SET {keys} WHERE id=?",
                         (*fields.values(), paper_id))

    def delete_paper(self, paper_id: int) -> dict:
        """删除论文的**导入记录**（T5：删除后同 md5 可重新导入）。

        级联范围**仅限 backend store 内部导入记录**，绝不触碰 library/<DOI>/ 与
        kb/<DOI>/ 下的用户产物（知识库/翻译资产属用户资产，只删"记录"，不删产物）。
        删除后该 PDF 的 pdf_md5 不再命中（find_paper_by_md5 返回 None）→ 可重新导入；
        重导会重新解析并重建 library/kb（设计上接受）。

        需按外键顺序删除（PRAGMA foreign_keys=ON，缺省 NO ACTION）：
          messages.session_id → sessions   → 先删 messages
          sessions.paper_id  → papers      → 再删 sessions
          tasks.paper_id     → papers      → 再删 tasks（NOT NULL，不能置空）
          最后删 papers 本行
        任一环节失败整体回滚（单事务）。

        幂等安全：paper 不存在时返回全 0 计数（无副作用，可安全重复调用）。
        """
        with self._lock, self._conn() as conn:
            row = conn.execute(
                "SELECT pdf_md5 FROM papers WHERE id=?", (paper_id,)).fetchone()
            md5 = str(row["pdf_md5"]) if row else ""
            if row is None:
                return {"paper_id": paper_id, "md5": md5, "removed": 0,
                        "removed_tasks": 0, "removed_sessions": 0,
                        "removed_messages": 0}
            session_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM sessions WHERE paper_id=?", (paper_id,)).fetchall()]
            removed_messages = 0
            if session_ids:
                ph = ",".join("?" * len(session_ids))
                removed_messages = conn.execute(
                    f"DELETE FROM messages WHERE session_id IN ({ph})",
                    session_ids).rowcount
            removed_sessions = conn.execute(
                "DELETE FROM sessions WHERE paper_id=?", (paper_id,)).rowcount
            removed_tasks = conn.execute(
                "DELETE FROM tasks WHERE paper_id=?", (paper_id,)).rowcount
            conn.execute("DELETE FROM papers WHERE id=?", (paper_id,))
            return {"paper_id": paper_id, "md5": md5, "removed": 1,
                    "removed_tasks": int(removed_tasks or 0),
                    "removed_sessions": int(removed_sessions or 0),
                    "removed_messages": int(removed_messages or 0)}

    # ---------------------------------------------------------- tasks
    def create_task(self, paper_id: int, kind: str) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO tasks(paper_id, kind, state, created_at, updated_at) VALUES(?,?,?,?,?)",
                (paper_id, kind, "pending", _now(), _now()))
            return int(cur.lastrowid)

    def update_task(self, task_id: int, state: str | None = None,
                    progress: int | None = None, message: str | None = None,
                    error: str | None = None) -> None:
        fields: dict[str, Any] = {"updated_at": _now()}
        if state is not None:
            fields["state"] = state
        if progress is not None:
            fields["progress"] = progress
        if message is not None:
            fields["message"] = message
        if error is not None:
            fields["error"] = error
        keys = ", ".join(f"{k}=?" for k in fields)
        with self._lock, self._conn() as conn:
            conn.execute(f"UPDATE tasks SET {keys} WHERE id=?", (*fields.values(), task_id))

    def get_task(self, task_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, paper_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM tasks"
        args: tuple = ()
        if paper_id is not None:
            sql += " WHERE paper_id=?"
            args = (paper_id,)
        sql += " ORDER BY id"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def latest_task(self, paper_id: int, kind: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE paper_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                (paper_id, kind)).fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------- sessions
    def create_session(self, paper_id: int | None = None, kind: str = "paper",
                       mode: str = "", title: str = "") -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO sessions(paper_id, kind, mode, title, created_at) VALUES(?,?,?,?,?)",
                (paper_id, kind, mode, title, _now()))
            return int(cur.lastrowid)

    def get_session(self, session_id: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def list_sessions(self, paper_id: int | None = None) -> list[dict]:
        with self._conn() as conn:
            if paper_id is None:
                rows = conn.execute(
                    "SELECT * FROM sessions ORDER BY id").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM sessions WHERE paper_id=? ORDER BY id", (paper_id,)).fetchall()
        return [dict(r) for r in rows]

    def rename_session(self, session_id: int, title: str) -> bool:
        """P2-7：会话改名（title 列，前端显示优先 title）。"""
        with self._lock, self._conn() as conn:
            cur = conn.execute("UPDATE sessions SET title=? WHERE id=?",
                               (title, session_id))
            return cur.rowcount > 0

    def delete_session(self, session_id: int) -> bool:
        """删除会话及其全部消息（U2：仅删除，不可恢复）。

        注意：先删 messages 再删 session——messages.session_id 有外键约束
        （RESTRICT），反序会触发 IntegrityError。
        """
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            cur = conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            return cur.rowcount > 0

    def first_user_message(self, session_id: int) -> str:
        """会话首条用户消息（展示标题用，U2 动态生成）。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT content FROM messages WHERE session_id=? AND role='user' "
                "ORDER BY id LIMIT 1", (session_id,)).fetchone()
        return row["content"] if row else ""

    # ---------------------------------------------------------- messages
    def add_message(self, session_id: int, role: str, content: str,
                    tokens: int = 0) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO messages(session_id, role, content, tokens, created_at) VALUES(?,?,?,?,?)",
                (session_id, role, content, tokens, _now()))
            return int(cur.lastrowid)

    def recent_messages(self, session_id: int, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def count_messages(self, session_id: int) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE session_id=?",
                (session_id,)).fetchone()
        return int(row["n"]) if row else 0

    def tokens_total(self, session_id: int) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(tokens),0) AS t FROM messages WHERE session_id=?",
                (session_id,)).fetchone()
        return int(row["t"]) if row else 0

    # ---------------------------------------------------------- answer cache
    def get_cached_answer(self, key: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT question, answer FROM answer_cache WHERE key=?", (key,)).fetchone()
        return dict(row) if row else None

    def set_cached_answer(self, key: str, question: str, answer: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO answer_cache(key, question, answer, created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET answer=excluded.answer, created_at=excluded.created_at",
                (key, question, answer, _now()))

    # ---------------------------------------------------------- llm_usage（V02）
    def record_llm_usage(self, context: str, provider: str, model: str,
                         prompt_tokens: int, completion_tokens: int,
                         cache_hit_tokens: int, cost: float) -> int:
        with self._lock, self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO llm_usage(context, provider, model, prompt_tokens, "
                "completion_tokens, cache_hit_tokens, cost, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (context, provider, model, prompt_tokens, completion_tokens,
                 cache_hit_tokens, cost, _now()))
            return int(cur.lastrowid)

    def usage_summary(self, context_prefix: str | None = None) -> dict:
        """按 context 前缀（如 session:1）或全局统计 token 与费用。"""
        where, args = "", ()
        if context_prefix:
            where = "WHERE context LIKE ?"
            args = (context_prefix + "%",)
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS calls, "
                f"COALESCE(SUM(prompt_tokens),0) AS prompt, "
                f"COALESCE(SUM(completion_tokens),0) AS completion, "
                f"COALESCE(SUM(cache_hit_tokens),0) AS cache_hit, "
                f"COALESCE(SUM(cost),0) AS cost "
                f"FROM llm_usage {where}", args).fetchone()
        return {
            "calls": int(row["calls"]),
            "prompt_tokens": int(row["prompt"]),
            "completion_tokens": int(row["completion"]),
            "cache_hit_tokens": int(row["cache_hit"]),
            "total_tokens": int(row["prompt"]) + int(row["completion"]),
            "cost": round(float(row["cost"]), 6),
        }

    def recent_usage(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM llm_usage ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def clear_usage(self, context_prefix: str | None = None) -> int:
        """清空 token 用量（P5 点2：用户可清零重新统计）。prefix 为空=清全局。
        返回删除行数。"""
        where, args = "", ()
        if context_prefix:
            where = "WHERE context LIKE ?"
            args = (context_prefix + "%",)
        with self._lock, self._conn() as conn:
            cur = conn.execute(f"DELETE FROM llm_usage {where}", args)
            return int(cur.rowcount)

    # ---------------------------------------------------------- settings（V03）
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))

    def all_settings(self) -> dict[str, str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # ---------------------------------------------------------- plugins（V05）
    def get_plugin_enabled(self, plugin_id: str, default: bool = True) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT enabled FROM plugins WHERE id=?", (plugin_id,)).fetchone()
        return bool(row["enabled"]) if row else default

    def set_plugin_enabled(self, plugin_id: str, enabled: bool) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO plugins(id, enabled, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled, "
                "updated_at=excluded.updated_at",
                (plugin_id, 1 if enabled else 0, _now()))

    # ---------------------------------------------------------- kb 编辑标记（U5）
    def mark_kb_edited(self, path: str) -> None:
        """记录用户手动编辑过的知识库文件（插件自动生成不再覆盖）。"""
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO kb_edited(path, ts) VALUES(?,?) "
                "ON CONFLICT(path) DO UPDATE SET ts=excluded.ts",
                (path, _now()))

    def is_kb_edited(self, path: str) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT 1 FROM kb_edited WHERE path=?", (path,)).fetchone()
        return row is not None
