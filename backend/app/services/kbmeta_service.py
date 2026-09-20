# -*- coding: utf-8 -*-
"""KbMetaService：backend 侧 paperkb 门面**薄访问器**（R2/R3 收敛）。

目标：去掉「逐方法 1:1 复制 paperkb.api」的 god-class 透传层。

- 同名方法一律经 ``__getattr__`` 自动转嫁 paperkb.api（唯一门面，无复制）。
- 仅保留「名称映射 / 额外行为」的少数方法（别名方法与 backend 侧适配，
  如 missing_dois 组合 wos_query、translate_now 前置清零防护计数、diary 走
  paperkb.diary + KBStore(roots)）。
- ROOTS 上提 container 注入（Roots(data/library/kb)），本模块不再持全局 ROOTS。
- ``get_kbmeta()`` 作为后向兼容注入点，指向 ``container.get_kbapi()``
  （chat_service / task_service / compile_worker / kb_tools 无需改动）。

设计约束保持：主库与 store 共用 data/system/app.db（表名不冲突，
WAL 并发）；期刊库/向量目录等根路径集中在注入的 Roots。
"""
from __future__ import annotations

import logging
from pathlib import Path

from paperkb import api as kbapi
from paperkb.config import Roots
from paperkb.db import KBStore

logger = logging.getLogger(__name__)


class KbMetaService:
    """paperkb 门面薄访问器（backend 侧唯一入口；不做逐方法复制）。"""

    def __init__(self, roots: Roots | None = None) -> None:
        self._roots = roots
        self._ready = False

    # ---------------------------------------------------------- 初始化（懒一次）
    def ensure(self) -> None:
        """确保 paperkb 已初始化（懒初始化的显式入口）。

        2026-09-12：`doi_meta.enrich_paper_meta` 直接用 `paperkb.api._need_store()`，
        绕过了本服务的懒初始化 ⇒ 后端刚启动、还没人碰过 kbmeta 时调用会抛
        "paperkb 未初始化：请先调用 init_kb(roots)"（实测踩到）。这里给一个公开入口。
        """
        self._ensure()

    def _ensure(self) -> None:
        """懒初始化 paperkb（首次调用建表迁移）+ 注入 LLM 适配。

        ``_ready=True`` 可跳过真实 init_kb（单测 monkeypatch 场景），后端需先注入 Roots。
        """
        if not self._ready:
            if self._roots is None:
                raise RuntimeError(
                    "KbMetaService 未注入 Roots（请经 container.get_kbapi() 获取）")
            kbapi.init_kb(self._roots)
            self._ready = True
            logger.info("paperkb 初始化完成（db=%s）", self._roots.main_db)
        # 注入 LLM（backend llm_service 适配 paperkb.LLMClient；动态解析，无需传引用）
        try:
            kbapi.configure_llm(_KBLLMAdapter())
        except Exception as e:  # noqa: BLE001
            logger.debug("paperkb LLM 未注入（编译需先配置供应商）: %s", e)

    @property
    def roots(self) -> Roots | None:
        """访问器持有的 Roots（kb_tools 计算根路径用）。"""
        return self._roots

    # ---------------------------------------------------------- 同名方法自动转嫁
    def __getattr__(self, name: str):
        """忽略 dunder，其余属性/方法转嫁 paperkb.api（唯一门面，不自建副本）。"""
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        self._ensure()
        return getattr(kbapi, name)

    # ---------------------------------------------------------- 名称映射 + 额外行为
    def list_papers(self, limit: int = 500) -> list[dict]:
        """别名：paperkb.list_papers_meta（书名与 api 不一致的转发）。"""
        self._ensure()
        return kbapi.list_papers_meta(limit)

    def get_paper(self, doi: str) -> dict | None:
        self._ensure()
        return kbapi.get_paper_meta(doi)

    def search(self, q: str, limit: int = 20) -> list[dict]:
        self._ensure()
        return kbapi.search_papers_meta(q, limit)

    def value_score(self, doi: str) -> dict | None:
        self._ensure()
        return kbapi.value_score_for(doi)

    def source_sync(self, doi: str, force: bool = False) -> dict:
        # 2026-09-12 修复既有 bug：这里曾写成 `kbapi.sync_source_to_kb(doi, self._roots, force=force)`
        # —— facade 签名早已是 `(doi, force=False)`（roots 由 paperkb 内部 store 提供），
        # 多传的位置参数被绑到 `force` 上 → 与 `force=force` 冲突 →
        # **「纳入kb」/「重新同步」按钮恒返回 400 "got multiple values for argument 'force'"**。
        self._ensure()
        return kbapi.sync_source_to_kb(doi, force=force)

    def verify_doc(self, doi: str, base: str = "kb") -> dict:
        self._ensure()
        return kbapi.verify_kb_doc(doi, base=base)

    def shared_doc_json(self, key: str) -> str:
        """共享全文前缀的唯一取用入口（kb 优先 → library）——问答前缀与编译/翻译同源。

        2026-09-12 用户实测修复：问答曾读 library 正本、编译读 kb 快照，复核写回重写
        library 后两侧 `shared_ctx` 从第 317 个字符分叉 ⇒ 前缀缓存命中 0。
        """
        self._ensure()
        try:
            return kbapi.shared_doc_json(key)
        except Exception as e:  # noqa: BLE001 - 解析失败退回调用方原逻辑
            logger.warning("共享文档定位失败（退回原 doc_json）: key=%s err=%s", key, e)
            return ""

    def missing_dois(self) -> dict:
        """缺失 DOI + WOS 检索式（一次性给出，前端直接展示复制）。"""
        self._ensure()
        missing = kbapi.missing_dois(self._roots)
        return {"missing": missing,
                "count": len(missing),
                "queries": kbapi.wos_query(missing)}

    def translate_now(self, doc_json: str | Path) -> dict:
        """paperkb 翻译+总结（写回 document.json 的 text_zh/ai_summary）。

        前置按任务粒度清零 translate 防护计数（防跨篇/跨服务生命周期累计误拦）。
        翻译模型路由由 _KBLLMAdapter 内部动态处理（优先翻译专用 AI，回落主模型）。
        若配置了翻译专用 AI，自动启用紧凑模式（小上下文分块，段落内联，无共享前缀）。
        """
        self._ensure()
        try:
            from .llm_service import get_guard

            get_guard().reset_context("translate")
        except Exception:  # noqa: BLE001
            pass
        # 检测是否配置了翻译专用 AI → 自动启用紧凑模式
        compact = False
        try:
            from .llm_service import get_translation_ai
            compact = get_translation_ai() is not None
        except Exception:  # noqa: BLE001
            pass
        return kbapi.translate_paper(doc_json, compact=compact)

    def compile_now(self, doi: str, level: str = "L1", force: bool = False) -> dict:
        """立即编译（同步；LLM 调用可能较慢）。

        2026-09-19：为并行翻译+编译端点而暴露。
        """
        self._ensure()
        return kbapi.compile_now(doi, level, force)

    # ---------------------------------------------------------- 文献阅读日记
    # 数据聚合 + 用户笔记。与 paperkb.api 解耦：直接构造 KBStore(ROOTS)，
    # 不依赖 init_kb 后的 _store 单例（避免与 api.py 并行改动冲突）。
    def _diary_store(self) -> KBStore:
        return KBStore(self._roots)

    def diary_days(self, month: str | None = None) -> dict:
        self._ensure()
        from paperkb import diary as _diary

        return _diary.diary_days(self._diary_store(), self._roots, month=month)

    def diary_day(self, date: str) -> dict:
        self._ensure()
        from paperkb import diary as _diary

        return _diary.diary_day(self._diary_store(), self._roots, date)

    def diary_note_write(self, date: str, text: str) -> dict:
        self._ensure()
        from paperkb import diary as _diary

        return _diary.diary_note_write(self._roots, date, text)

    def diary_export(self) -> dict:
        self._ensure()
        from paperkb import diary as _diary

        return _diary.diary_export(self._roots)


class _KBLLMAdapter:
    """paperkb LLM 客户端适配：context 映射到 backend 防护组。

    - translate：独立组（80 次/300 万字符；translate_now 前置按任务粒度清零）
    - 编译等：归入 engine 组（12 次/800k；task 翻译前也会 reset）
    - **动态解析**（LLM 架构优化）：每次 complete() 实时获取当前 AI 实例，
      热切换供应商/翻译模型立即生效（旧实现构造时捕获引用 ⇒ 热切换不传播）。
    - **翻译路由**：translate context 优先用翻译专用 AI（若已配置），否则回落主模型。
    """

    def complete(self, prompt: str, context: str = "compile") -> str:
        mapped = {"translate": "translate", "ask": "ask"}.get(context, "engine")
        # 翻译路由：轮询取池内下一个专用 AI（多模型并行时分发批次），空池回落主模型
        if context == "translate":
            try:
                from .llm_service import next_translation_ai
                t_ai = next_translation_ai()
                if t_ai is not None:
                    return t_ai.complete(prompt, context=mapped, effort_context=context)
            except Exception as e:  # noqa: BLE001
                logger.warning("翻译路由异常: %s", e)
        from .llm_service import get_ai
        base = get_ai()
        return base.complete(prompt, context=mapped, effort_context=context)


def get_kbmeta() -> KbMetaService:
    """container 注入点（懒单例，指向 container 持有的 paperkb 访问器）。"""
    from . import container

    return container.get_kbapi()
