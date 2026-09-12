# -*- coding: utf-8 -*-
"""资产门面 `Assets`——**业务层访问数据的唯一通道**（见 `docs/VERSIONING.md` §6）。

为什么要有它（用户 2026-09-12 拍板）：
  业务模块此前直调 `store.*` / `kbapi.*`，于是"存储细节"（哪个库、什么列、旧格式）
  渗透到 service/api 层 ⇒ 一旦升级要改存储，就得在业务代码里到处打补丁（冗余的根源）。
  门面把**五类资产**收成一组稳定方法名，业务层只认方法名，不认库、不认表、不认路径。

五个子门面：
  · `system`    系统数据：设置/供应商/用量/插件/任务
  · `chat`      会话与消息（+ 答案缓存）
  · `biblio`    文献元数据与文献库导入记录（papers/papers_meta/identifiers/compile_*）
  · `reference` 期刊分区与影响因子（JCR/CAS）
  · `content`   内容资产：knowledge_base / library 的文件与产物（含清理守卫）

**稳定性契约（SemVer）**：方法名与语义按版本管理——**加方法是 minor，改/删方法是 major**
（需先加新方法、保留旧名一个版本、并在 `docs/COMPAT-REGISTER.md` 登记移除条件）。
返回值一律纯 `dict`/基础类型，禁止把 sqlite 连接、Row、Path 泄漏给业务层。
契约由 `backend/tests/test_version_contract.py::TestAssetsContract` 固定。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

__all__ = ["Assets", "get_assets"]


class _SystemAssets:
    """系统数据：设置 / 供应商 / 用量 / 插件 / 任务。"""

    def __init__(self, store, settings_service=None):
        self._s = store
        self._settings = settings_service

    # —— 设置（键值）——
    def get_setting(self, key: str, default: str = "") -> str:
        return self._s.get_setting(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self._s.set_setting(key, value)

    # —— 用量 ——
    def record_usage(self, **fields: Any) -> None:
        self._s.record_llm_usage(**fields)

    def usage_recent(self, limit: int = 50) -> list[dict]:
        return self._s.recent_usage(limit)

    def usage_summary(self, context_prefix: str | None = None) -> dict:
        return self._s.usage_summary(context_prefix)

    # —— 任务 ——
    def create_task(self, paper_id: int, kind: str) -> int:
        return self._s.create_task(paper_id, kind)

    def latest_task(self, paper_id: int, kind: str) -> dict | None:
        return self._s.latest_task(paper_id, kind)


class _ChatAssets:
    """会话与消息（+ 答案缓存）。"""

    def __init__(self, store):
        self._s = store

    def create_session(self, paper_id: int | None, kind: str = "paper",
                       title: str = "") -> int:
        return self._s.create_session(paper_id, kind, title)

    def get_session(self, session_id: int) -> dict | None:
        return self._s.get_session(session_id)

    def list_sessions(self, paper_id: int | None = None) -> list[dict]:
        return self._s.list_sessions(paper_id)

    def add_message(self, session_id: int, role: str, content: str,
                    tokens: int = 0) -> int:
        return self._s.add_message(session_id, role, content, tokens)

    def recent_messages(self, session_id: int, limit: int = 20) -> list[dict]:
        return self._s.recent_messages(session_id, limit)

    def cached_answer(self, key: str) -> dict | None:
        return self._s.get_cached_answer(key)

    def cache_answer(self, key: str, question: str, answer: str) -> None:
        self._s.set_cached_answer(key, question, answer)


class _BiblioAssets:
    """文献元数据与文献库导入记录（paperkb 元数据 + backend papers 记录）。"""

    def __init__(self, store, kbapi_getter: Callable[[], Any] | None = None):
        self._s = store              # backend Store（papers 导入记录）
        self._kbapi = kbapi_getter   # paperkb 门面（papers_meta/identifiers/compile_*）

    # —— 文献库导入记录（papers 表）——
    def list_papers(self, limit: int = 50) -> list[dict]:
        return self._s.list_papers(limit)

    def get_paper(self, paper_id: int) -> dict | None:
        return self._s.get_paper(paper_id)

    def find_paper_by_md5(self, md5: str) -> dict | None:
        return self._s.find_paper_by_md5(md5)

    # —— 文献元数据（paperkb；键=RID/DOI/目录名）——
    def get_meta(self, key: str) -> Any:
        return self._kbapi().get_meta(key) if self._kbapi else None

    def upsert_meta(self, meta: Any) -> str:
        return self._kbapi().upsert_meta(meta)

    def shared_doc_json(self, key: str) -> str:
        """共享全文前缀的唯一取源（kb 优先 → library）；见 VERSIONING §6。"""
        return self._kbapi().shared_doc_json(key) if self._kbapi else ""

    def compile_queue(self, key: str, level: str = "L1", **kw: Any) -> dict:
        return self._kbapi().compile_queue(key, level, **kw)


class _ReferenceAssets:
    """期刊分区与影响因子（JCR/CAS）。"""

    def __init__(self, kbapi_getter: Callable[[], Any]):
        self._kbapi = kbapi_getter

    def lookup(self, journal: str) -> dict | None:
        return self._kbapi().journals_lookup(journal)

    def stats(self) -> dict:
        return self._kbapi().journals_stats()


class _ContentAssets:
    """内容资产：knowledge_base / library 的文件与产物（含清理守卫）。"""

    def __init__(self, roots):
        self._roots = roots

    @property
    def kb_dir(self) -> Path:
        return Path(self._roots.kb_dir)

    @property
    def library_dir(self) -> Path:
        return Path(self._roots.library_dir)

    def assert_safe_to_clear(self, path: str | Path) -> None:
        """删除任何资源目录前必须过的守卫（用户附件不可删；见 AGENTS.md）。"""
        from paperkb.layout import assert_safe_to_clear

        assert_safe_to_clear(Path(path))


class Assets:
    """资产门面（业务层唯一入口）。由 `container.get_assets()` 提供单例。"""

    def __init__(self, store, roots, settings_service=None,
                 kbapi_getter: Callable[[], Any] | None = None, kb_service=None):
        self.system = _SystemAssets(store, settings_service)
        self.chat = _ChatAssets(store)
        self.biblio = _BiblioAssets(store, kbapi_getter)
        self.reference = _ReferenceAssets(kbapi_getter) if kbapi_getter else None
        self.content = _ContentAssets(roots)
        self._kb = kb_service

    # —— 元信息 ——
    def data_format(self) -> int:
        from paperkb.manifest import read_manifest

        man = read_manifest(self.content._roots) or {}
        return int(man.get("data_format") or 0)

    def describe(self) -> dict:
        from paperkb.manifest import read_manifest

        man = read_manifest(self.content._roots) or {}
        return {"data_format": man.get("data_format"), "layout": man.get("layout"),
                "app_version": man.get("app_version"),
                "kb_dir": str(self.content.kb_dir),
                "library_dir": str(self.content.library_dir)}


def get_assets(store, roots, settings_service=None, kbapi_getter=None, kb_service=None) -> Assets:
    """构造门面（由 container 调用一次并缓存）。"""
    return Assets(store, roots, settings_service=settings_service,
                  kbapi_getter=kbapi_getter, kb_service=kb_service)
