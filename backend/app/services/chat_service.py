# -*- coding: utf-8 -*-
"""对话服务（T06）—— Token 优化核心。

策略（对应需求）：
1. **稳定前缀**：system = 论文元数据 + 章节索引（doc_summary 输出逐字节固定，
   DeepSeek 前缀缓存命中）；历史消息只追加不修改 → 前缀保持稳定。
2. **局部检索**：本地读取 document.json 做关键词匹配选 top-k 段落，
   再用 engine.query(para_ids) 取**限长片段**进 LLM 上下文（全文绝不进上下文）。
3. **回答缓存**：规范化问题 hash（按论文隔离）→ 命中直接返回，0 token。
4. **对话历史精简**：messages 只存 user 问题 + assistant 回答（讨论消息）；
   翻译全文走任务通道，不进这里。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path

from ..config import Settings
from .engine_service import EngineService
from .llm_service import ChatCompleter, TokenBudgetExceeded
from .store import Store

logger = logging.getLogger(__name__)

_EN_STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "with",
    "is", "are", "was", "were", "be", "this", "that", "these", "those",
    "what", "how", "why", "does", "do", "did", "can", "could", "about",
    "from", "by", "at", "as", "it", "its", "their", "our", "your", "we",
    "you", "they", "not", "no", "than", "then", "so", "if", "has", "have",
    "had", "paper", "article", "study", "work", "research", "please",
}


def _tokenize(text: str) -> list[str]:
    """轻量分词：英文单词（≥2 字母）+ 中文单字（英文查询词用全小写）。"""
    en = [w for w in re.findall(r"[a-zA-Z]{2,}", text.lower())
          if w not in _EN_STOPWORDS]
    cn = re.findall(r"[\u4e00-\u9fff]", text)
    return en + cn


def _fmt_recall_item(it: dict) -> str:
    """格式化检索片段（paperkb 编译笔记项 与 engine.query 段落项 两种形态）。"""
    if "snippet" in it:  # paperkb 编译笔记
        return f"[{it.get('file') or it.get('doi') or 'note'}] {it.get('snippet') or ''}"
    return f"[{it.get('para_id')}] ({it.get('section') or ''}) {it.get('text') or ''}"


# 综述/写作类任务关键词（触发更大工具轮次上限与召回预算）
_REPORT_HINTS = (
    "综述", "文献综述", "报告", "成文", "写一份", "撰写", "汇总成", "整理成",
    "总结报告", "总结成文", "梳理", "回顾", "对比总结", "研究脉络",
    "review", "survey", "synthesize", "overview", "report", "write",
)


def _is_report_task(question: str) -> bool:
    """判断是否为综述/写作类任务（需要更大轮次与召回预算）。"""
    q = (question or "").lower()
    return any(h in q for h in _REPORT_HINTS)


# ---------------------------------------------------------------- 思考强度（契约）
# 请求级 `effort` 合法值：只有 low/high 才会真的发送 reasoning_effort。
# 缺省 / "auto" / None / 非法值 ⇒ None = **不发送**（用供应商默认，契约 1）。
LEGAL_CHAT_EFFORTS = ("low", "high")
# 检索开始前发的 status 文案（前端可据此显示进度；契约固定文本）
RETRIEVAL_STATUS_TEXT = "正在检索知识库…"


def _norm_effort(effort: str | None) -> str | None:
    """请求级 effort 归一化：合法（low/high，忽略大小写/空白）→ 原值；其余 → None。"""
    v = (effort or "").strip().lower()
    return v if v in LEGAL_CHAT_EFFORTS else None


class ChatService:
    """对话编排：前缀组装 + 检索 + 流式回答 + 缓存。"""

    def __init__(self, settings: Settings, store: Store,
                 engine: EngineService, chat: ChatCompleter, *,
                 settings_service=None, kbmeta=None, kb=None):
        self.settings = settings
        self.store = store
        self.engine = engine
        self.chat = chat
        # P0/P1：依赖改为构造注入（settings_service / kbmeta 访问器 / kb 访问器），
        # 不再在本类内 `import container` 摸全局单例 → 可测试、可替换、降低 god-class 耦合。
        self._settings_service = settings_service
        self._kbmeta = kbmeta      # 可调用 accessor（返回 KbMetaService）或实例
        self._kb = kb              # 可调用 accessor（返回 KnowledgeBaseService）或实例
        # P2-4：论文系统前缀缓存（key=doc_json+mtime+检索模式+附加指令）。
        # 同一篇文献的所有会话共享同一稳定前缀 → DeepSeek 前缀缓存命中最大化；
        # document.json 被清洗/重解析后 mtime 变化 → 自动失效重建。
        self._sys_cache: dict[str, str] = {}
        self._sys_cache_max = 100

    # ---------------------------------------------------------- 依赖访问器（DI）
    def _get_settings_service(self):
        return self._settings_service

    def _get_kbmeta(self):
        kbmeta = self._kbmeta
        if kbmeta is not None:
            return kbmeta() if callable(kbmeta) else kbmeta
        from .kbmeta_service import get_kbmeta
        return get_kbmeta()

    def _get_kb(self):
        kb = self._kb
        if kb is not None:
            return kb() if callable(kb) else kb
        return None

    # ---------------------------------------------------------- 管理模式轮次预算
    def _manage_round_limit(self, question: str) -> int:
        """工具循环轮次上限：综述/写作类任务取大值，其余取默认值（均来自 config）。"""
        return (self.settings.manage_review_max_rounds if _is_report_task(question)
                else self.settings.manage_tools_max_rounds)

    def _final_answer_no_tools(self, session_id: int, messages: list[dict],
                               max_rounds: int) -> str:
        """工具轮次耗尽的兜底：去掉 tools 强制模型基于已检索内容给出终答。

        旧行为是直接回"超限，请简化提问"——检索明明做了却拿不到任何答案
        （token 已消耗，用户侧表现为白烧）。强制终答失败才回退超限提示。
        """
        try:
            forced = messages + [{
                "role": "user",
                "content": (f"工具调用轮次已用尽（{max_rounds} 轮）。"
                            "请基于以上已获得的结果直接给出最终回答；"
                            "结果不足以完全回答时，说明已查到什么、缺什么。")}]
            text = self.chat.complete(forced, f"session:{session_id}")
            if (text or "").strip():
                return text.strip()
        except Exception:  # noqa: BLE001
            logger.exception("工具轮次耗尽后强制终答失败")
        return f"工具调用次数超限（{max_rounds} 轮），请简化操作或分步提问。"

    @staticmethod
    def _reflect_on_limit(context: str, messages: list[dict],
                          exc: TokenBudgetExceeded) -> str:
        """触及 TokenGuard 硬限制时的自我反思：分析工具调用历史，总结进度和问题。

        不调用 LLM（已超限），纯从 messages 历史提取工具调用统计。
        """
        tool_counts: dict[str, int] = {}
        tool_failures: list[str] = []
        for m in messages:
            if m.get("role") == "tool":
                try:
                    data = json.loads(m.get("content") or "{}")
                    ok = data.get("ok", True)
                    if not ok:
                        err = data.get("error", "") or str(data.get("result", ""))[:80]
                        tool_failures.append(err)
                except (json.JSONDecodeError, TypeError):
                    pass
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function", {})
                name = fn.get("name", "unknown")
                tool_counts[name] = tool_counts.get(name, 0) + 1
        parts = ["\u26d4 **已达调用限制，任务暂停**\n"]
        parts.append(f"原因：{exc}\n")
        if tool_counts:
            parts.append("**已执行的工具调用：**")
            for name, count in sorted(tool_counts.items(), key=lambda x: -x[1]):
                parts.append(f"- {name}：{count} 次")
        if tool_failures:
            parts.append(f"\n**失败记录（{len(tool_failures)} 条）：**")
            for f in tool_failures[:5]:
                parts.append(f"- {f}")
        parts.append("\n**建议：**")
        if any("search" in n or "retrieve" in n for n in tool_counts):
            parts.append("- 检索次数较多，可能查询词不够精准，建议缩小范围或换关键词")
        if any("draft" in n or "revise" in n for n in tool_counts):
            parts.append("- 写作/修订次数较多，建议降低字数要求或简化章节结构")
        parts.append("- 可在新消息中回复「继续」以重置限制并继续任务")
        parts.append("- 或简化任务要求（如减少字数、缩减章节）后重新提问")
        return "\n".join(parts)

    # ---------------------------------------------------------- 会话
    def create_session(self, paper_id: int | None = None, kind: str = "paper",
                       mode: str = "", title: str = "") -> int:
        """创建会话：kind=paper（绑定论文）/ global（知识库问答，mode=qa|manage）/
        chat（普通聊天）。paper 会话 title 默认取论文 md 文件前缀名（N2）。"""
        if kind == "paper" and not title:
            paper = self.store.get_paper(paper_id) if paper_id else None
            if paper:
                from pathlib import Path
                title = Path(paper.get("pdf_name") or paper.get("title") or "").stem
        session_id = self.store.create_session(paper_id, kind, mode, title)
        if kind == "global":
            if mode == "manage":
                note = ("⚙️ 知识库管理模式：可自然语言完成知识库操作"
                        "（如\u201c编译 cej\u201d、\u201c列出库内文献价值评分\u201d、\u201c纠正某篇期刊名\u201d、"
                        "\u201c扫描缺失 DOI\u201d），也支持知识库问答。")
            else:
                note = ("📚 知识库精简问答模式：询问知识库任意文献内容"
                        "（基于编译产物与元数据，不读全文），如"
                        "\u201c知识库里有哪些关于人工肌肉的文献？\u201d")
            self.store.add_message(session_id, "assistant", note, tokens=0)
            return session_id
        if kind == "lit":
            note = ("🔍 AI 文献检索模式：可自然语言完成文献检索与管理操作"
                    "（如\u201c检索钙钛矿太阳能电池\u201d、\u201c补全元数据\u201d、\u201c计算 PaperRank\u201d、"
                    "\u201c构建向量索引\u201d、\u201c编译主题\u201d），也支持文献库问答。")
            self.store.add_message(session_id, "assistant", note, tokens=0)
            return session_id
        if kind == "writing":
            note = ("✍️ AI 写作模式：自动完成计划→检索→写作→验证→迭代→保存的完整写作流程。"
                    "支持 essay/review/report/summary 等文体，可指定目标字数。"
                    "例如：\u201c写一篇 600 字关于人工智能的综述\u201d、\u201c生成一份实验报告\u201d。")
            self.store.add_message(session_id, "assistant", note, tokens=0)
            return session_id
        paper = self.store.get_paper(paper_id) if paper_id else None
        if paper and paper["status"] == "translated" and paper["doc_json"]:
            summary = self._safe_doc_summary(paper)
            note = (f"📄《{summary.get('title') or paper['title']}》已完成翻译与总结，"
                    f"共 {summary.get('paragraph_count', 0)} 段（已译 "
                    f"{summary.get('translated_paragraphs', 0)} 段），可开始提问。")
            self.store.add_message(session_id, "assistant", note, tokens=0)
        return session_id

    def _safe_doc_summary(self, paper: dict) -> dict:
        try:
            return self.engine.doc_summary(paper["doc_json"])
        except Exception:  # noqa: BLE001 - 摘要失败不阻塞会话
            return {}

    def _paper_doi(self, paper: dict) -> str:
        """论文 DOI（从 doc_summary 元数据取；失败回空）。"""
        try:
            return self._safe_doc_summary(paper).get("doi") or ""
        except Exception:  # noqa: BLE001
            return ""

    # ---------------------------------------------------------- 稳定前缀
    def _system_extra(self) -> str:
        """用户自定义系统提示词附加（V11，设置中心可编辑）。"""
        try:
            svc = self._get_settings_service()
            if svc is None:
                return ""
            return (svc.get_system_prompt_extra() or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _paper_system_prompt(self, paper: dict) -> str:
        """系统前缀：论文元数据 + 章节索引（固定输出 → 前缀缓存命中）。

        P2-4：结果按 (doc_json, mtime, 检索模式, 附加指令) 缓存——
        同一文献多会话/多轮提问复用同一前缀；缓存超限时整体清空（保守策略）。
        """
        doc_json = paper.get("doc_json") or ""
        try:
            mtime = Path(doc_json).stat().st_mtime
        except OSError:
            mtime = 0.0
        mode = self._retrieval_mode()
        extra = self._system_extra()
        key = f"{doc_json}|{mtime}|{mode}|{extra}"
        hit = self._sys_cache.get(key)
        if hit is not None:
            return hit
        if len(self._sys_cache) >= self._sys_cache_max:
            self._sys_cache.clear()
        prompt = self._build_paper_system_prompt(paper, mode, extra)
        self._sys_cache[key] = prompt
        return prompt

    def _build_paper_system_prompt(self, paper: dict, mode: str, extra: str) -> str:
        """组装论文系统前缀（仅 _paper_system_prompt 调用，保持缓存 key 一致）。"""
        summary = self._safe_doc_summary(paper)
        lines = ["你是一名科研文献阅读助手，基于给定论文回答用户问题。",
                 "回答须基于论文内容，引用具体段落时用 [P012] 标注段落 ID；",
                 "不确定的内容明确说明，不编造。",
                 "",
                 "=== 论文信息 ===",
                 f"标题：{summary.get('title') or '(未知)'}",
                 f"作者：{', '.join(summary.get('authors', [])[:5]) or '(未知)'}"]
        if summary.get("journal") or summary.get("doi"):
            lines.append(f"期刊/年份：{summary.get('journal') or '—'} | {summary.get('year') or '—'} | DOI：{summary.get('doi') or '—'}")
        if summary.get("abstract_preview"):
            lines.append(f"摘要：{summary['abstract_preview']}")
        if summary.get("sections"):
            lines.append("=== 章节索引（段落 ID）===")
            for sec in summary["sections"]:
                ids = ",".join(sec["para_ids"][:8])
                more = "…" if len(sec["para_ids"]) > 8 else ""
                lines.append(f"- {sec['section']} [{ids}{more}]")
        if extra:
            lines.append("")
            lines.append("=== 用户附加指令 ===")
            lines.append(extra)
        mode_hint = {
            "notes": "已基于该论文原文（en.md 单语）+ 知识库编译笔记检索片段作答；未翻译文献基于英文原文，请勿编造正文细节。",
            "fragments": "可引用 [Pxxx] 段落 ID。",
            "full": "已授权论文全文阅读（内容可能截断）。",
        }[mode]
        lines.append("\n=== 检索模式 ===")
        lines.append(mode_hint)
        # Q5 阶段2：编译状态提示（notes 模式基于编译笔记回答）
        try:
            kbmeta = self._get_kbmeta()
            compiled = kbmeta.paper_compiled(self._paper_doi(paper)) if kbmeta else []
        except Exception:  # noqa: BLE001 - 编译状态查询失败不阻塞
            compiled = []
        if compiled:
            lines.append("\n=== 编译状态 ===")
            lines.append(f"该篇已完成知识库编译（{', '.join(compiled)}），回答优先基于编译笔记。")
        # 注：论文全文**不在**这里（2026-09-12 调整）——它必须与编译/翻译共享同一字节前缀，
        # 而编译是把 `shared_ctx` 放在 `role=user` 消息的最前面，所以问答侧也放在
        # `_paper_prefix_messages()` 的 user 消息开头（见该方法注释）。
        return "\n".join(lines)

    def _paper_shared_ctx(self, paper: dict) -> str:
        """编译/翻译共享的全文前缀 `paperkb.context.shared_ctx(doc)`（字节一致 ⇒ 缓存可继承）。

        **取哪份 document.json（2026-09-12 用户实测修复）**：走 `shared_doc_json`
        （kb 优先 → library 兜底）——编译/翻译读 kb 快照，问答此前读 `papers.doc_json`
        （library 正本），而复核写回会重写 library ⇒ 两侧 `shared_ctx` 从第 317 个字符
        分叉、缓存命中 0（实测 14834 token 全按未命中计费）。三处同源后：
          · 没编译/没翻译 → kb 无副本 → 回退 library（提问照样带全文）；
          · 已编译 → 取 kb 快照 = 编译当时那份 ⇒ 问答继承编译建立的缓存。
        按 `paper_fulltext_prefix_chars` 处理（批3 修正语义）：
          -1 = **不截断**（默认：与编译/翻译侧的 `shared_ctx` 全文逐字节一致 ⇒ 继承其整段缓存）；
           0 = 关闭该机制（返回空串，退回"每轮检索片段"）；
          >0 = 截断（会让问答前缀短于编译前缀，命中上限被砍 ⇒ 仅明确要压 token 时用）。
        """
        max_chars = int(getattr(self.settings, "paper_fulltext_prefix_chars", -1) or 0)
        doc_json = paper.get("doc_json") or ""
        if max_chars == 0 or not doc_json:
            return ""
        # 与编译/翻译同源：kb 快照优先（未纳入 kb 时内部回退 library）
        kbmeta = self._get_kbmeta()
        key = self._paper_doi(paper)
        if not key and doc_json:
            key = Path(doc_json).parent.name   # 无 DOI 文献：目录名（nd-<指纹>）也是合法键
        if kbmeta is not None and key:
            resolver = getattr(kbmeta, "shared_doc_json", None)   # 测试替身可能未实现
            same_source = resolver(key) if callable(resolver) else ""
            if same_source:
                doc_json = same_source
        try:
            from paperkb.context import shared_ctx
            from paperkb.doc import read_document

            # ⚠️ 必须用 `paperkb.doc.read_document`（PaperDoc）而不是
            # `paperparse...load_document`（ArticleDocument）——`shared_ctx` 读的是 PaperDoc
            # 的 title/sections/paragraphs 字段（实测用错类型报 'ArticleDocument' object has
            # no attribute 'title'）。这也保证了与编译侧**同一个构造函数** ⇒ 字节一致。
            text = shared_ctx(read_document(doc_json))
        except Exception as e:  # noqa: BLE001 - 构造失败退回检索模式
            logger.warning("构造共享全文前缀失败（退回检索模式）: %s", e)
            return ""
        # 批3：max_chars < 0 = 不截断（默认，与编译侧全文一致）；>0 = 显式截断
        return text if max_chars < 0 else text[:max_chars]

    def _paper_prefix_messages(self, paper: dict, shared: str,
                               instructions: str) -> list[dict[str, str]]:
        """该篇的**稳定前缀消息**：跨会话共享、会话之间互不干扰。

        结构（`messages[0]` 的前 |shared| 个 token 与编译请求**完全一致** ⇒ 继承其缓存）：
            [0] user      : shared_ctx + 空行 + 本篇问答指令(+已编译笔记)
            [1] assistant : 固定确认语（保证角色交替、且字节稳定）
        其后由调用方接**本会话自己的历史** + 当前问题 ⇒ 分支内容互不串味。
        """
        tail = shared + "\n\n" + instructions
        # 已编译笔记一并进前缀（同一篇字节稳定，重编译后才变一次）——用户要求前缀包含"编译输出"。
        try:
            doi = self._paper_doi(paper)
            kbmeta = self._get_kbmeta()
            notes = kbmeta.paper_notes_all(doi, max_files=1, max_chars=6000) if (kbmeta and doi) else []
            if notes:
                note = (notes[0].get("snippet") or "").strip()
                if note:
                    tail += ("\n\n=== 该篇已编译知识笔记（_note.md，供问答参考）===\n" + note)
        except Exception as e:  # noqa: BLE001 - 笔记缺失不影响前缀
            logger.warning("前缀内联编译笔记失败: %s", e)
        return [{"role": "user", "content": tail},
                {"role": "assistant", "content": "已通读该论文全文与编译笔记，请提问。"}]

    # ---------------------------------------------------------- 局部检索
    def _retrieve(self, paper: dict, question: str, top_k: int = 4) -> list[dict]:
        """本地关键词匹配段落 → engine.query 取限长片段（不返回全文）。
        T05：仅 retrieval_mode=fragments（L1 显式授权）时调用。

        中文问题匹配 text_zh（如有译文），英文问题匹配 text_en；
        纯中文问题对未翻译段落无法匹配，由上层回退策略兜底（摘要+开头段）。
        """
        doc_json = paper.get("doc_json")
        if not doc_json:
            return []
        try:
            from paperparse.core.document_builder import load_document
            doc = load_document(doc_json)
        except Exception as e:  # noqa: BLE001
            logger.warning("检索读取 document.json 失败: %s", e)
            return []
        q_tokens = _tokenize(question)
        if not q_tokens:
            return []
        q_set = set(q_tokens)
        scored: list[tuple[float, str]] = []
        for para in doc.paragraphs:
            if para.is_heading or para.is_caption or not (para.text_en or para.text_zh):
                continue
            p_tokens = set(_tokenize(para.text_en or "")) | set(_tokenize(para.text_zh or ""))
            hit = len(q_set & p_tokens)
            if hit > 0:
                scored.append((hit, para.para_id))
        scored.sort(key=lambda x: (-x[0], x[1]))
        top_ids = [pid for _, pid in scored[:top_k]]
        if not top_ids:
            return []
        result = self.engine.query(doc_json, para_ids=top_ids, include="both")
        return result.get("items", [])

    def _sample_body(self, paper: dict, budget_chars: int = 9000) -> list[dict]:
        """结构采样兜底：关键词检索拿不到正文时，按论文结构取**代表性正文片段**。

        为什么必须有（2026-09-12 用户反馈 + 实测取证）：
        `_retrieve` 用 `_tokenize(question) ∩ 段落词集` 做逐词交集，中文问题对**未翻译**的
        英文段落交集恒为空——实测该篇 95 个正文段落命中 **0**（`text_zh` 段落数 = 0），
        而同篇的英文问题命中 14~26 段。于是"针对这篇文献提问"实际拿不到任何正文，
        模型只能拿题名 + 章节索引 + 六维笔记泛泛而谈。

        采样策略（覆盖"创新点/方法/结论"这类通问）：摘要段 → **每章首段**（章节首段通常
        陈述该章在做什么）→ 结论段，按 `budget_chars` 截断。返回 `engine.query` 的 items
        形态（与 `_retrieve` 一致，上层无需区分）。
        """
        doc_json = paper.get("doc_json")
        if not doc_json:
            return []
        try:
            summary = self.engine.doc_summary(doc_json)
        except Exception as e:  # noqa: BLE001 - 采样失败由上层继续兜底
            logger.warning("结构采样读取 doc_summary 失败: %s", e)
            return []
        sections = (summary or {}).get("sections") or []
        pick: list[str] = []

        def _add(ids: list[str], n: int) -> None:
            for pid in ids[:n]:
                if pid not in pick:
                    pick.append(pid)

        for sec in sections:
            ids = list(sec.get("para_ids") or [])
            if not ids:
                continue
            name = str(sec.get("section") or "")
            low = name.lower()
            if "abstract" in low or "摘要" in name:
                _add(ids, 4)
            elif "conclusion" in low or "结论" in name:
                _add(ids, 4)
            else:
                _add(ids, 2)      # 每章首段
            if len(pick) >= 24:
                break
        if not pick:
            return []
        try:
            r = self.engine.query(doc_json, para_ids=pick[:24], include="both",
                                  limit_chars=budget_chars)
            items = r.get("items", []) if isinstance(r, dict) else []
            logger.info("结构采样兜底：取 %d 段正文进上下文（budget=%d 字符）",
                        len(items), budget_chars)
            return items
        except Exception as e:  # noqa: BLE001
            logger.warning("结构采样取正文失败: %s", e)
            return []

    # ---------------------------------------------------------- 缓存
    @staticmethod
    def _norm_question(q: str) -> str:
        return re.sub(r"[\s\W_]+", "", q.lower())

    def _cache_key(self, namespace: str, question: str, *,
                   kind: str = "", retrieval_mode: str = "",
                   fingerprint: str = "", effort: str = "") -> str:
        """回答缓存键：命名空间 + 会话 kind + 检索模式 + 源指纹 + 思考强度 + 规范化问题。

        P0：键加入 retrieval_mode（paper 的 notes/full/fragments；global 的 kb/manage）
        与 session_kind（paper/global/chat），并把源指纹（doc_json / kb 目录 mtime）写入
        ——检索模式或源变化 ⇒ 键变化 ⇒ 自动失效，跨模式/跨会话不再命中过期答案。
        manage 与 qa 用不同命名空间前缀（kbmanage / kbqa），避免共用同一键。
        effort：思考强度进键（"low"/"high"，无则空串）——**换档必须不复用旧答案**，
        否则切 high 会命中 low 档缓存（回答质量与预期不符）。
        """
        norm = self._norm_question(question)
        payload = "|".join([namespace, kind, retrieval_mode, fingerprint, effort, norm])
        h = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"{namespace}:{kind}:{h}"

    @staticmethod
    def _md_mtime(path: str | Path) -> float:
        try:
            return float(Path(path).stat().st_mtime)
        except OSError:
            return 0.0

    def _paper_fingerprint(self, paper: dict, mode: str) -> str:
        """论文会话源指纹：doc_json mtime（重解析即失效）；full 模式另含全文 mtime。

        2026-09-12：加入前缀形态标记（`shared`/`plain`）——问答 prompt 结构变了，
        旧结构下缓存的答案不得复用。
        """
        doc_json = paper.get("doc_json") or ""
        shape = "shared" if int(getattr(self.settings, "paper_fulltext_prefix_chars", 0) or 0) > 0 else "plain"
        parts = [f"{doc_json}:{self._md_mtime(doc_json)}:{shape}"]
        if mode == "full":
            try:
                folder = Path(doc_json).resolve().parent
                if folder.name == "intermediate":
                    folder = folder.parent
                for cand in ("en.md",):   # 2026-09-12：只跟当前格式（旧 paper.md 回退已清零）
                    p = folder / cand
                    if p.exists():
                        parts.append(f"{p}:{self._md_mtime(p)}")
            except OSError:
                pass
        return ";".join(parts)

    def _kb_fingerprint(self) -> str:
        """知识库会话源指纹：kb 根目录 mtime（目录级结构变更即失效）。"""
        try:
            kb = self._get_kb()
            if kb is None:
                return ""
            root = kb.root()
            return f"{root}:{self._md_mtime(str(root))}"
        except Exception:  # noqa: BLE001
            return ""

    def _fit_history(self, messages: list[dict]) -> list[dict]:
        """按 token 预算裁剪历史（messages 已按时间先后排序）：从最新往回保留，
        超出 history_token_budget 即丢最旧；至少保留最后 1 条。"""
        budget = self.settings.history_token_budget
        if budget <= 0 or not messages:
            return messages
        kept: list[dict] = []
        used = 0
        for m in reversed(messages):
            cost = self._estimate_tokens(m.get("content") or "")
            if used + cost > budget and kept:
                break
            kept.append(m)
            used += cost
        kept.reverse()
        return kept

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """粗略估算（中文≈1.5字符/token，英文≈4字符/token，取 len//2 折中）。"""
        return max(1, len(text) // 2)

    # ---------------------------------------------------------- 知识库（V07 / 2026-08-27 新知识库）
    # A1（P0-B step3）：存储与检索约定——**稳定前缀**（所有 Kb 会话字节一致，
    # 不掺入用户数据），保服务端提示词缓存命中。改这段文字会让缓存失效一次。
    _KB_STORAGE_TREATY = (
        "\n\n=== 存储与检索约定（库内文件怎么找）===\n"
        "1) 单一物理根：所有资源都在 `library/` 下，一个资源 = 一个目录；**资源类型在目录名"
        "（RID）前缀里**：论文无前缀（`10.1002_adma…`）/ `book__` 书 / `thesis__` 学位论文 / "
        "`std__` 标准 / `patent__` 专利 / `chapter__` 章节 / `nd-<指纹>` 无编号资料"
        "（内容指纹去重）。类型也在元数据 `kind` 字段里（papers_meta）。\n"
        "2) 依附资料（支撑信息 SI、审稿意见、原始数据）**挂父资源目录**："
        "`library/<父目录>/attachments/{si,review,data}/`；无父资源的零散资料放独立根 "
        "`attachments/<RID>/`。附件**不是**独立文献，不进知识库列表。\n"
        "3) `library/` = 解析库（document.json / en.md / images，PDF 解析产物）；"
        "`knowledge_base/` = 知识库（编译产物 `_note.md/_wiki.md/_relations.md`、用户笔记、"
        "QA 卡片、综述报告 `_reports/`）。两者不是副本关系，内容层不要混引。\n"
        "4) 引用：用 `[[DOI目录]]` 标注正文来源；引用附件内容时注明是「该文献的支撑信息/"
        "审稿意见」。无编号资源用其 RID（`nd-…`/`book__…`）标注以免歧义。")

    # 工具选择指引**只进 manage 模式**（带 tools 的请求）：qa 是不带 tools 的流式请求，
    # 提示词里出现工具名会诱导 deepseek-flash 把工具调用以 DSML 标记当正文吐出来
    # （2026-09-19 实测：qa 回答整段 `<｜｜DSML｜｜ invoke …>` 垃圾文本）。
    _KB_TOOL_TREATY = (
        "\n\n=== 找资料的工具选择 ===\n"
        "元数据/分类检索 → `kb_search_papers`（可用 kind / has_attachment "
        "只找书或只找带 SI 的）；**问「某篇的支撑信息/审稿意见提了什么」→ 先 `kb_attachments(rid)` "
        "看清单，再 `kb_attachment_text(rid, path)` 读内容**；泛化内容检索 → `kb_recall`"
        "（附件文本已按父资源入索引，命中条目的 file 以 `attachments/` 开头即为附件）。")

    @staticmethod
    def _kb_system_prompt(mode: str = "qa") -> str:
        base = ("你是一名文献知识库助手。回答基于知识库中**已编译的文献笔记**"
                "（_note/_details/_wiki 六维知识编译）与 bib 权威元数据（papers_meta）。"
                "引用来源时用 [[DOI目录]] 格式标注文献；不确定的内容明确说明，不编造。"
                "检索片段已在下文提供，优先基于片段回答，可结合历史对话追问。")
        base += ChatService._KB_STORAGE_TREATY
        if mode == "manage":
            base += ChatService._KB_TOOL_TREATY
            base += (
                "\n\n=== 知识库管理能力 ===\n"
                "你是知识库管理员，可调用工具完成管理操作：\n"
                "- 知识库问答：先调用 kb_recall 检索相关片段，再基于片段回答（不要臆造）。\n"
                "- 编译管理：kb_list_scores 了解优先级；kb_compile_queue/now/process 编译。\n"
                "- 元数据：kb_search_papers / kb_paper_detail 查询；kb_import_bib 导入 bib；"
                "kb_missing_dois 扫描缺失 DOI 并给 WOS 检索式。\n"
                "- 期刊：kb_journal_override 纠正期刊名；kb_journals_stats 查看导入状态。\n"
                "- 知识库结构：kb_kb_status 查看原文层；kb_source_sync 纳入/重新同步；"
                "kb_verify_doc 一致性校验。\n"
                "- 依附资料：kb_attachments 列某篇的 SI/审稿意见清单；"
                "kb_attachment_text 读其文本片段（只允许该资源 attachments 内的相对路径）。\n"
                "- 成稿输出：kb_write_report 把综述/报告写为知识库 _reports/<标题>.md 并返回路径。"
                "成文前先 kb_recall / kb_search_papers / kb_paper_detail 检索证据，"
                "引用用 [[DOI目录]] 标注**来库内真实文献**（不编造 DOI/标题），"
                "文末可附参考文献列表。\n"
                "规则：① 涉及库内任何数据（文献列表/评分/编译状态/期刊指标/缺失 DOI）"
                "必须先调用对应工具获取，**禁止凭记忆编造库内容**；"
                "② 写操作（编译/入队/纠正期刊/导入 bib/同步原文/写报告）必须用户明确意图；"
                "③ 不要臆造 DOI 或文件路径，信息不足先问用户或调用查询工具；"
                "④ 综述/写作类任务可多轮检索补证据（kb_recall）与修订（可写多次覆盖/新建）。")
        return base

    @staticmethod
    def _lit_system_prompt() -> str:
        return ("你是一名 AI 文献检索助手。可调用工具完成文献检索与管理操作：\n"
                "- 检索：lit_search（三层漏斗检索）、lit_vector_search（向量语义检索）。\n"
                "- 文献浏览：lit_list_papers（分页列表）、lit_get_paper（按 DOI 查详情）。\n"
                "- 元数据补全：lit_enrich_one（单篇）、lit_enrich_pending（批量）。\n"
                "- 引用图谱：lit_compute_rank（PaperRank 计算）、lit_top_papers（排名列表）、"
                "lit_compute_clusters（共被引聚类）、lit_cluster_papers（聚类文献）。\n"
                "- 向量索引：lit_build_vector（构建索引）、lit_vector_search（语义检索）。\n"
                "- 导入：lit_ingest_bib（单文件）、lit_ingest_bib_dir（目录批量）。\n"
                "- 主题编译：lit_compile_topics（聚合）、lit_list_topics（列表）、lit_get_topic（详情）。\n"
                "- 缓存：lit_cache_stats（统计）、lit_cache_clear（清空）。\n"
                "- 状态：lit_status（文献库总览）。\n"
                "规则：① 涉及库内任何数据必须先调用对应工具获取，**禁止凭记忆编造**；"
                "② 写操作（导入/补全/计算/构建/编译/清空缓存）必须用户明确意图；"
                "③ 信息不足先问用户或调用查询工具。")

    @staticmethod
    def _writing_system_prompt() -> str:
        return ("你是一名 AI 写作助手，擅长自动计划→检索→写作→验证→迭代的完整写作流程。\n"
                "可用工具：\n"
                "- writing_plan：生成写作大纲（章节标题 + 要点 + 字数分配）。\n"
                "- writing_retrieve：从知识库检索相关内容（自然语言查询）。\n"
                "- writing_draft：撰写章节草稿（根据大纲要点和检索内容）。\n"
                "- writing_validate：自我验证草稿质量（字数/连贯性/完整性评分 + 修改建议）。\n"
                "- writing_revise：根据验证反馈修订草稿。\n"
                "- writing_save：保存最终报告到 .md 文件（存储到 knowledge_base/_reports/）。\n"
                "工作流程：\n"
                "1. **计划**：调用 writing_plan 生成结构化大纲。\n"
                "2. **检索**：对每个章节调用 writing_retrieve 获取相关素材。\n"
                "3. **写作**：调用 writing_draft 逐章撰写草稿。\n"
                "4. **验证**：调用 writing_validate 检查质量（字数/连贯性/完整性）。\n"
                "5. **迭代**：若验证不通过，调用 writing_revise 修订，重复步骤 4。\n"
                "6. **保存**：验证通过后调用 writing_save 保存最终报告。\n"
                "规则：① 必须按流程顺序执行，不可跳过验证步骤；"
                "② 每章写作前必须先检索相关素材；"
                "③ 验证评分<8 分必须修订；"
                "④ 最终保存前确保总字数达标（±20%）。")

    @staticmethod
    def _hybrid_system_prompt() -> str:
        """混合模式系统提示：AI检索 + 写作 + 知识库能力（lit 会话检测到写作意图时启用）。"""
        return ("你是一名 AI 文献检索与写作助手，同时具备文献检索、知识库检索和结构化写作能力。\n\n"
                "=== AI检索库工具（lit_*）—— 文献元数据/聚类/统计 ===\n"
                "- lit_search：三层漏斗检索（标题/摘要/关键词）\n"
                "- lit_vector_search：向量语义检索\n"
                "- lit_list_papers：分页列表\n"
                "- lit_get_paper：按 DOI 查详情（含全文段落）\n"
                "- lit_top_papers / lit_compute_rank：PaperRank 排名\n"
                "- lit_compute_clusters / lit_cluster_papers：共被引聚类\n"
                "- lit_status：文献库总览\n\n"
                "=== 知识库工具（kb_*）—— 编译产物/深度笔记/引用关系 ===\n"
                "- kb_recall：知识库语义检索，召回编译产物和元数据片段（含 [[DOI目录]] 引用）\n"
                "- kb_search_papers：按标题/作者/期刊搜索库内文献元数据\n"
                "- kb_paper_detail：单篇详情（元数据 + 引用/被引关系 + 编译状态）\n\n"
                "=== 写作工具（writing_*）===\n"
                "- writing_plan：生成写作大纲（章节标题 + 要点 + 字数分配）\n"
                "- writing_draft：撰写章节草稿\n"
                "- writing_validate：自我验证草稿质量（字数/连贯性/完整性评分）\n"
                "- writing_revise：根据验证反馈修订草稿\n"
                "- writing_save：保存最终报告到 .md 文件\n\n"
                "=== 写作工作流程 ===\n"
                "1. 计划：调用 writing_plan 生成大纲\n"
                "2. 检索：对每个章节**同时**使用两个数据源检索文献证据：\n"
                "   - lit_search / lit_vector_search → AI检索库（文献元数据+摘要）\n"
                "   - kb_recall → 知识库（编译笔记、深度摘要、引用关系）\n"
                "   综合两个来源的信息作为写作素材\n"
                "3. 写作：调用 writing_draft 逐章撰写，引用文献用 [[DOI目录]] 标注\n"
                "4. 验证：调用 writing_validate 检查质量\n"
                "5. 迭代：验证评分<8 分则 writing_revise 修订\n"
                "6. 保存：验证通过后 writing_save 保存\n\n"
                "规则：① 涉及库内数据必须调用工具获取，禁止编造；"
                "② 写作必须按流程执行，不可跳过验证；"
                "③ 每章写作前必须先从两个数据源检索证据；"
                "④ 接近调用限制时主动总结进度并告知用户。")

    # ---------------------------------------------------------- 检索分级（T05）
    def _retrieval_mode(self) -> str:
        try:
            svc = self._get_settings_service()
            if svc is None:
                return "notes"
            return svc.get_retrieval_mode() or "notes"
        except Exception:  # noqa: BLE001
            return "notes"

    def _retrieve_full(self, paper: dict, max_chars: int = 60_000) -> list[dict]:
        """L2 授权全文：读取规范库 library/<DOI>/en.md（干净版主产物）限长注入。

        T4：中英对照主产物已迁 kb（en_zh.md），library 全文源统一为 en.md。
        2026-09-12（COMPAT-REGISTER A2）：**删除 `paper.md`/`paper.en.md` 旧产物回退**——
        实测全库已无此类文件（`library/`、`knowledge_base/` 扫描 0 命中），
        `engine_service._remove_legacy_paper_md` 也在导出路径持续清理历史残留。
        """
        from pathlib import Path

        doc_json = Path(paper["doc_json"]).resolve()
        folder = doc_json.parent
        if folder.name == "intermediate":
            folder = folder.parent  # 兼容 parse-only 中间层 library/<DOI>/intermediate/
        en_md = folder / "en.md"
        if en_md.exists():
            text = en_md.read_text(encoding="utf-8")[:max_chars]
            return [{"para_id": "full", "section": "全文", "text": text}]
        return []

    def _llm_events(self, context: str, messages: list[dict[str, str]],
                    effort: str | None) -> Iterator[dict]:
        """统一 LLM 流式调用：产出 `{"type": "delta"|"reasoning", "text": ...}`。

        `effort` 透传到对话通道（None ⇒ 不发 reasoning_effort）。
        真实通道 `ChatCompleter.stream_events` 同时给出正文与思维链；
        仅有 `stream`（旧替身/旧实现）时退化为只发 delta。
        """
        stream_events = getattr(self.chat, "stream_events", None)
        if callable(stream_events):
            yield from stream_events(context, messages, effort=effort)
            return
        for text in self.chat.stream(context, messages, effort=effort):
            yield {"type": "delta", "text": text}

    def _ask_global(self, session_id: int, question: str,
                    mode: str = "qa", effort: str | None = None) -> Iterator[dict]:
        """知识库全局会话：mode=qa 精简问答（FTS5 编译产物召回+流式）；
        mode=manage 管理模式（工具调用循环，见 _ask_manage_tools）。"""
        # P0：manage 与 qa 拆分命名空间前缀（kbmanage / kbqa），并写入检索模式与源指纹；
        #     知识库目录变化 ⇒ 键变化 ⇒ 自动失效，不再跨模式/跨会话命中过期答案。
        namespace = "kbmanage" if mode == "manage" else "kbqa"
        kind = mode or "qa"
        fp = self._kb_fingerprint()
        key = self._cache_key(namespace, question, kind=kind,
                              retrieval_mode="manage" if mode == "manage" else "kb",
                              fingerprint=fp, effort=effort or "")
        cached = self.store.get_cached_answer(key)
        if cached:
            yield {"type": "start", "cached": True}
            yield {"type": "delta", "text": cached["answer"]}
            yield {"type": "done", "cached": True, "message_id": None}
            return

        self.store.add_message(session_id, "user", question,
                               tokens=self._estimate_tokens(question))
        yield {"type": "start", "cached": False, "retrieved": 0}
        if mode == "manage":
            yield from self._ask_manage_tools(session_id, question, key)
            return

        # ---- qa：新知识库检索（notes/meta FTS5，编译产物）→ 流式 ----
        # 契约 2：检索**开始前**发 status —— 检索耗时期间前端不再只有空白气泡。
        yield {"type": "status", "text": RETRIEVAL_STATUS_TEXT}
        try:
            kbmeta = self._get_kbmeta()
            retrieved = kbmeta.recall(question, top_k=6) if kbmeta else []
        except Exception as e:  # noqa: BLE001
            logger.warning("知识库检索失败: %s", e)
            retrieved = []
        # 检索片段拼注入上下文：按边界裁剪（旧实现 [:8000] 会把最后一条片段切进句中）
        from paperkb.textseg import boundary_trim

        context = boundary_trim(
            "\n\n".join(
                f"[{it['doi']}/{it.get('file') or 'meta'}] {it.get('snippet') or ''}"
                for it in retrieved),
            8000)
        system = self._kb_system_prompt("qa")
        extra = self._system_extra()
        if extra:
            system += "\n\n=== 用户附加指令 ===\n" + extra
        # P1：历史按 token 预算裁剪（不再是纯条数 12）
        history = self.store.recent_messages(session_id, self.settings.history_max_messages)
        history = [m for m in history if m["role"] in ("user", "assistant")]
        history = self._fit_history(history)
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        for m in history:
            msg = {"role": m["role"], "content": m["content"]}
            # 思考模式：历史 assistant 消息的 reasoning_content 必须回传 API
            if m["role"] == "assistant" and m.get("reasoning_content"):
                msg["reasoning_content"] = m["reasoning_content"]
            messages.append(msg)
        user_content = question
        if context:
            user_content = f"以下为知识库相关片段（标注来源）：\n{context}\n\n问题：{question}"
        messages.append({"role": "user", "content": user_content})
        parts: list[str] = []
        reasoning_parts: list[str] = []
        try:
            for ev in self._llm_events(f"session:{session_id}", messages, effort):
                if ev["type"] == "reasoning":
                    reasoning_parts.append(ev["text"])
                    yield ev
                    continue
                parts.append(ev["text"])
                yield ev
        except Exception as e:  # noqa: BLE001
            logger.exception("知识库对话流式失败")
            yield {"type": "error", "message": f"对话失败: {e}"}
            return
        answer = "".join(parts).strip()
        reasoning_content = "".join(reasoning_parts).strip() if reasoning_parts else None
        mid = self.store.add_message(session_id, "assistant", answer,
                                     tokens=self._estimate_tokens(answer),
                                     reasoning_content=reasoning_content)
        self.store.set_cached_answer(key, question, answer)
        yield {"type": "done", "cached": False, "message_id": mid,
               "tokens": self._estimate_tokens(question) + self._estimate_tokens(answer)}

    def _ask_manage_tools(self, session_id: int, question: str,
                          cache_key: str) -> Iterator[dict]:
        """管理模式：工具调用循环（complete_with_tools → 执行 → 回填 → 再请求）。

        工具调用中间消息不入库（messages 只存最终问答，历史精简）；
        每轮结果截断回填；轮次上限取自 config（综述/写作类任务可放大，防死循环）。
        """
        from .kb_tools import run_tool, tool_specs

        system = self._kb_system_prompt("manage")
        extra = self._system_extra()
        if extra:
            system += "\n\n=== 用户附加指令 ===\n" + extra
        history = self.store.recent_messages(session_id, self.settings.history_max_messages)
        history = [m for m in history if m["role"] in ("user", "assistant")]
        history = self._fit_history(history)
        messages: list[dict] = [{"role": "system", "content": system}]
        for m in history:
            msg = {"role": m["role"], "content": m["content"]}
            # 思考模式：历史 assistant 消息的 reasoning_content 必须回传 API
            if m["role"] == "assistant" and m.get("reasoning_content"):
                msg["reasoning_content"] = m["reasoning_content"]
            messages.append(msg)
        messages.append({"role": "user", "content": question})
        tools = tool_specs()
        # 轮次上限按任务类型取 config；综述/写作类可放大（如 12）；召回预算同步放大。
        max_rounds = self._manage_round_limit(question)
        recall_budget = self.settings.manage_retrieval_budget_chars
        answer = ""
        reasoning = None
        for _round in range(max_rounds):
            try:
                content, tool_calls, reasoning = self.chat.complete_with_tools(
                    f"session:{session_id}", messages, tools)
            except Exception as e:  # noqa: BLE001
                logger.exception("管理模式工具循环失败")
                yield {"type": "error", "message": f"对话失败: {e}"}
                return
            if not tool_calls:
                answer = (content or "").strip()
                break
            # 同轮多个 tool_calls 的合法消息形状：**一条** assistant（带全部 tool_calls，
            # 思考模式还必须带 reasoning_content，否则 API 400）+ 每调用一条 tool 消息。
            # 旧实现按 tc 各挂一条 assistant → assistant/tool/assistant/tool 非法交错，
            # 模型上下文错乱 → 持续调工具烧满轮次不给终答（2026-09-19 实测根因）。
            asst: dict = {"role": "assistant", "content": content,
                          "tool_calls": [{"id": tc["id"], "type": "function",
                                          "function": tc["function"]}
                                         for tc in tool_calls]}
            if reasoning:
                asst["reasoning_content"] = reasoning
            messages.append(asst)
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except ValueError:
                    args = {}
                result = run_tool(name, args, recall_budget=recall_budget)
                yield {"type": "tool", "name": name, "args": args,
                       "summary": result["result"][:160], "ok": result["ok"]}
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": json.dumps(result, ensure_ascii=False)})
        else:
            answer = self._final_answer_no_tools(session_id, messages, max_rounds)
        if not answer:
            answer = "（无回答）"
        mid = self.store.add_message(session_id, "assistant", answer,
                                     tokens=self._estimate_tokens(answer),
                                     reasoning_content=reasoning)
        self.store.set_cached_answer(cache_key, question, answer)
        yield {"type": "delta", "text": answer}
        yield {"type": "done", "cached": False, "message_id": mid,
               "tokens": self._estimate_tokens(question) + self._estimate_tokens(answer)}

    def _ask_lit_tools(self, session_id: int, question: str,
                       effort: str | None = None) -> Iterator[dict]:
        """AI 文献检索模式：工具调用循环（complete_with_tools → 执行 → 回填 → 再请求）。

        写作混合模式：检测到写作意图时自动合并 writing_tools，启用混合系统提示，
        并提高轮次上限。接近 TokenGuard 软限制时发预警事件，硬限制触发时
        生成反思摘要并停止。
        """
        from .lit_tools import run_tool as lit_run_tool, tool_specs as lit_tool_specs
        hybrid = _is_report_task(question)
        if hybrid:
            from .writing_tools import run_tool as writing_run_tool, tool_specs as writing_tool_specs
            from .kb_tools import run_tool as kb_run_tool, TOOL_SPECS as _KB_ALL_SPECS
            # 写作混合模式只挂载只读检索工具，避免 LLM 误调用写操作
            _kb_read_names = {"kb_recall", "kb_search_papers", "kb_paper_detail"}
            kb_tool_specs = [s for s in _KB_ALL_SPECS
                             if s["function"]["name"] in _kb_read_names]

        def _run_tool(name: str, args: dict) -> dict:
            if name.startswith("writing_") and hybrid:
                return writing_run_tool(name, args)
            if name.startswith("kb_") and hybrid:
                return kb_run_tool(name, args)
            return lit_run_tool(name, args)

        system = self._hybrid_system_prompt() if hybrid else self._lit_system_prompt()
        extra = self._system_extra()
        if extra:
            system += "\n\n=== 用户附加指令 ===\n" + extra
        history = self.store.recent_messages(session_id, self.settings.history_max_messages)
        history = [m for m in history if m["role"] in ("user", "assistant")]
        history = self._fit_history(history)
        messages: list[dict] = [{"role": "system", "content": system}]
        for m in history:
            msg = {"role": m["role"], "content": m["content"]}
            if m["role"] == "assistant" and m.get("reasoning_content"):
                msg["reasoning_content"] = m["reasoning_content"]
            messages.append(msg)
        messages.append({"role": "user", "content": question})
        self.store.add_message(session_id, "user", question,
                               tokens=self._estimate_tokens(question))
        yield {"type": "start", "cached": False, "retrieved": 0}
        if hybrid:
            tools = lit_tool_specs() + writing_tool_specs() + kb_tool_specs
            max_rounds = max(self._manage_round_limit(question), 20)
        else:
            tools = lit_tool_specs()
            max_rounds = self._manage_round_limit(question)
        ctx = f"session:{session_id}"
        # 用户回复「继续」→ 重置 TokenGuard 计数，允许写作任务续写
        _q = (question or "").strip().lower()
        if _q in ("继续", "continue", "请继续", "继续写", "接着写"):
            guard = getattr(self.chat, "guard", None)
            if guard:
                guard.reset_context(ctx)
                logger.info("用户请求继续，已重置 %s 的 TokenGuard 计数", ctx)
        answer = ""
        reasoning = None
        soft_limit_warned = False
        for _round in range(max_rounds):
            # 软限制预警：接近 TokenGuard 红线时通知前端
            guard = getattr(self.chat, "guard", None)
            if guard and not soft_limit_warned:
                soft = guard.check_soft_limit(ctx, threshold=0.8)
                if soft:
                    soft_limit_warned = True
                    yield {"type": "limit_warning",
                           "message": f"⚠ 接近调用限制（{soft['call_pct']}% 次数 / "
                                      f"{soft['char_pct']}% 输入），将尽快完成剩余工作",
                           "usage": soft}
            try:
                content, tool_calls, reasoning = self.chat.complete_with_tools(
                    ctx, messages, tools)
            except TokenBudgetExceeded as e:
                logger.warning("lit 工具循环触及 TokenGuard 硬限制：%s", e)
                answer = self._reflect_on_limit(ctx, messages, e)
                yield {"type": "limit_reached",
                       "message": answer,
                       "reason": str(e)}
                break
            except Exception as e:  # noqa: BLE001
                logger.exception("AI 检索模式工具循环失败")
                yield {"type": "error", "message": f"对话失败: {e}"}
                return
            if not tool_calls:
                answer = (content or "").strip()
                break
            asst: dict = {"role": "assistant", "content": content,
                          "tool_calls": [{"id": tc["id"], "type": "function",
                                          "function": tc["function"]}
                                         for tc in tool_calls]}
            if reasoning:
                asst["reasoning_content"] = reasoning
            messages.append(asst)
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except ValueError:
                    args = {}
                result = _run_tool(name, args)
                summary = result.get("result", "")
                if isinstance(summary, str):
                    summary = summary[:160]
                else:
                    summary = str(summary)[:160]
                yield {"type": "tool", "name": name, "args": args,
                       "summary": summary, "ok": result.get("ok", False)}
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": json.dumps(result, ensure_ascii=False)})
        else:
            answer = self._final_answer_no_tools(session_id, messages, max_rounds)
        if not answer:
            answer = "（无回答）"
        mid = self.store.add_message(session_id, "assistant", answer,
                                     tokens=self._estimate_tokens(answer),
                                     reasoning_content=reasoning)
        yield {"type": "delta", "text": answer}
        yield {"type": "done", "cached": False, "message_id": mid,
               "tokens": self._estimate_tokens(question) + self._estimate_tokens(answer)}

    def _ask_writing_tools(self, session_id: int, question: str,
                           effort: str | None = None) -> Iterator[dict]:
        """AI 写作模式：工具调用循环（计划→检索→写作→验证→迭代→保存）。

        写作任务需要更多轮次（默认 15 轮），因为完整流程包括：
        1. 生成大纲（1 轮）
        2. 每章检索 + 写作（N 章 × 2 轮）
        3. 验证 + 可能的修订（2-4 轮）
        4. 保存（1 轮）
        """
        from .writing_tools import run_tool, tool_specs

        system = self._writing_system_prompt()
        extra = self._system_extra()
        if extra:
            system += "\n\n=== 用户附加指令 ===\n" + extra
        history = self.store.recent_messages(session_id, self.settings.history_max_messages)
        history = [m for m in history if m["role"] in ("user", "assistant")]
        history = self._fit_history(history)
        messages: list[dict] = [{"role": "system", "content": system}]
        for m in history:
            msg = {"role": m["role"], "content": m["content"]}
            if m["role"] == "assistant" and m.get("reasoning_content"):
                msg["reasoning_content"] = m["reasoning_content"]
            messages.append(msg)
        messages.append({"role": "user", "content": question})
        self.store.add_message(session_id, "user", question,
                               tokens=self._estimate_tokens(question))
        yield {"type": "start", "cached": False, "retrieved": 0}
        tools = tool_specs()
        # 写作任务需要更多轮次（默认 15 轮，综述类 20 轮）
        max_rounds = 20 if _is_report_task(question) else 15
        answer = ""
        reasoning = None
        for _round in range(max_rounds):
            try:
                content, tool_calls, reasoning = self.chat.complete_with_tools(
                    f"session:{session_id}", messages, tools)
            except Exception as e:  # noqa: BLE001
                logger.exception("AI 写作模式工具循环失败")
                yield {"type": "error", "message": f"对话失败：{e}"}
                return
            if not tool_calls:
                answer = (content or "").strip()
                break
            asst: dict = {"role": "assistant", "content": content,
                          "tool_calls": [{"id": tc["id"], "type": "function",
                                          "function": tc["function"]}
                                         for tc in tool_calls]}
            if reasoning:
                asst["reasoning_content"] = reasoning
            messages.append(asst)
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except ValueError:
                    args = {}
                result = run_tool(name, args)
                yield {"type": "tool", "name": name, "args": args,
                       "summary": result.get("result", "")[:160] if isinstance(result.get("result"), str) else str(result.get("result", ""))[:160],
                       "ok": result.get("ok", False)}
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": json.dumps(result, ensure_ascii=False)})
        else:
            answer = self._final_answer_no_tools(session_id, messages, max_rounds)
        if not answer:
            answer = "（无回答）"
        mid = self.store.add_message(session_id, "assistant", answer,
                                     tokens=self._estimate_tokens(answer),
                                     reasoning_content=reasoning)
        yield {"type": "delta", "text": answer}
        yield {"type": "done", "cached": False, "message_id": mid,
               "tokens": self._estimate_tokens(question) + self._estimate_tokens(answer)}

    # ---------------------------------------------------------- 普通聊天（V11）
    @staticmethod
    def _chat_system_prompt() -> str:
        return ("你是一个乐于助人的 AI 助手。回答简洁、准确、有条理；"
                "涉及具体文献内容时，建议用户使用「文献会话」或「知识库会话」提问。")

    def _ask_chat(self, session_id: int, question: str,
                  effort: str | None = None) -> Iterator[dict]:
        """普通聊天：无检索，纯对话（不绑定论文/知识库）。"""
        key = self._cache_key("chat", question, kind="chat", retrieval_mode="chat",
                              effort=effort or "")
        cached = self.store.get_cached_answer(key)
        if cached:
            yield {"type": "start", "cached": True}
            yield {"type": "delta", "text": cached["answer"]}
            yield {"type": "done", "cached": True, "message_id": None}
            return

        system = self._chat_system_prompt()
        extra = self._system_extra()
        if extra:
            system += "\n\n=== 用户附加指令 ===\n" + extra
        history = self.store.recent_messages(session_id, self.settings.history_max_messages)
        history = [m for m in history if m["role"] in ("user", "assistant")]
        history = self._fit_history(history)
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        for m in history:
            msg = {"role": m["role"], "content": m["content"]}
            # 思考模式：历史 assistant 消息的 reasoning_content 必须回传 API
            if m["role"] == "assistant" and m.get("reasoning_content"):
                msg["reasoning_content"] = m["reasoning_content"]
            messages.append(msg)
        messages.append({"role": "user", "content": question})

        self.store.add_message(session_id, "user", question,
                               tokens=self._estimate_tokens(question))
        yield {"type": "start", "cached": False, "retrieved": 0}
        parts: list[str] = []
        reasoning_parts: list[str] = []
        try:
            for ev in self._llm_events(f"session:{session_id}", messages, effort):
                if ev["type"] == "reasoning":
                    reasoning_parts.append(ev["text"])
                    yield ev
                    continue
                parts.append(ev["text"])
                yield ev
        except Exception as e:  # noqa: BLE001
            logger.exception("普通聊天流式失败")
            yield {"type": "error", "message": f"对话失败: {e}"}
            return
        answer = "".join(parts).strip()
        reasoning_content = "".join(reasoning_parts).strip() if reasoning_parts else None
        mid = self.store.add_message(session_id, "assistant", answer,
                                     tokens=self._estimate_tokens(answer),
                                     reasoning_content=reasoning_content)
        self.store.set_cached_answer(key, question, answer)
        yield {"type": "done", "cached": False, "message_id": mid,
               "tokens": self._estimate_tokens(question) + self._estimate_tokens(answer)}

    # ---------------------------------------------------------- 回答
    def ask_stream(self, session_id: int, question: str,
                   effort: str | None = None) -> Iterator[dict]:
        """流式回答事件生成器：start / status / reasoning / delta / tool / done / error。

        V07：会话 kind=global → 知识库问答；kind=paper → 论文问答。
        V11：kind=chat → 普通聊天。
        effort：请求级思考强度（"low"/"high"）；缺省/"auto"/None/**非法值**一律归一化为
        None ⇒ **不发送** reasoning_effort（用供应商默认，契约 1）。归一化后的值进回答缓存
        key —— 换档不复用旧档答案。
        """
        effort = _norm_effort(effort)
        session = self.store.get_session(session_id)
        if not session:
            yield {"type": "error", "message": "会话不存在"}
            return
        if session.get("kind") == "global":
            mode = session.get("mode") or "qa"
            yield from self._ask_global(session_id, question, mode, effort=effort)
            return
        if session.get("kind") == "chat":
            yield from self._ask_chat(session_id, question, effort=effort)
            return
        if session.get("kind") == "lit":
            yield from self._ask_lit_tools(session_id, question, effort=effort)
            return
        if session.get("kind") == "writing":
            yield from self._ask_writing_tools(session_id, question, effort=effort)
            return
        paper = self.store.get_paper(session["paper_id"])
        if not paper or not paper.get("doc_json"):
            yield {"type": "error",
                   "message": "论文尚未解析完成，请等待翻译任务结束再提问"}
            return

        # 1) 回答缓存命中 → 0 token。键含检索模式、会话 kind 与源指纹（doc_json mtime）：
        #    换检索模式或源变化 ⇒ 键变化 ⇒ 自动失效（跨模式/跨会话不再命中过期答案）。
        mode = self._retrieval_mode()
        fp = self._paper_fingerprint(paper, mode)
        key = self._cache_key(f"paper:{paper['id']}", question, kind="paper",
                              retrieval_mode=mode, fingerprint=fp,
                              effort=effort or "")
        cached = self.store.get_cached_answer(key)
        if cached:
            yield {"type": "start", "cached": True}
            yield {"type": "delta", "text": cached["answer"]}
            yield {"type": "done", "cached": True, "message_id": None}
            return

        # 契约 2：检索**开始前**发 status（检索期前端不再只有空白气泡）。
        yield {"type": "status", "text": RETRIEVAL_STATUS_TEXT}

        # 2) 组装消息：稳定前缀（已含检索模式提示，缓存复用）+ 历史（只追加） + 检索片段 + 问题
        #    T05 检索分级：notes（默认，仅笔记）/ fragments（授权片段）/ full（授权全文）
        system = self._paper_system_prompt(paper)
        # 全文前缀（与编译/翻译共享的 `shared_ctx`；关闭时为空 → 退回检索模式）
        shared_ctx_text = self._paper_shared_ctx(paper)
        prefix_full = bool(shared_ctx_text)
        if mode == "full":
            retrieved = self._retrieve_full(paper)
            context = "\n\n".join(
                f"[{it['section']}] {it['text']}" for it in retrieved)
            ctx_label = "以下为论文全文（已授权，可能截断）："
        elif mode == "fragments":
            retrieved = self._retrieve(paper, question)
            if not retrieved:
                try:
                    r = self.engine.query(paper["doc_json"], section="Abstract",
                                          include="both", limit_chars=2500)
                    retrieved = r.get("items", [])[:2]
                except Exception:  # noqa: BLE001
                    retrieved = []
            context = "\n\n".join(
                f"[{it['para_id']}] ({it['section']}) {it['text']}" for it in retrieved)
            ctx_label = "以下为论文相关片段（标注了段落 ID，可用于引用）："
        else:  # 默认（P5 点8）：原文(document.json/en.md 单语)优先 + 编译笔记补充，不依赖翻译
            doi = self._paper_doi(paper)
            # 0) 全文已在 system 稳定前缀里（用户设计 2026-09-12）→ 本轮**不再**塞检索片段：
            #    既不重复付 token，也让 user 消息只剩问题 ⇒ 前缀缓存最大化复用（同一篇第 2 轮起
            #    按缓存价计费）。此前的检索片段放在 user 消息里，每轮都变 ⇒ 永远付全价。
            prefix_full = bool(shared_ctx_text)
            if prefix_full:
                retrieved = []
                context = ""
                ctx_label = ""
            # 1) 原文检索（en.md 单语，按问题语言匹配；不要求已翻译）
            retrieved = [] if prefix_full else self._retrieve(paper, question, top_k=4)
            # 2) **结构采样兜底**（2026-09-12 用户反馈"针对该文献提问没有利用已上传的文献信息"）：
            #    `_retrieve` 是逐词交集匹配，**中文问题 × 未翻译的英文正文 = 恒 0 命中**
            #    （实测该篇 text_zh 段落 0 个，95 个正文段落命中 0；英文问题则命中 14~26）。
            #    而旧代码把"编译笔记补充"放在 `if not retrieved` 兜底**之前** → 笔记（中文六维）
            #    一旦被 FTS 命中，摘要兜底就永不执行 ⇒ 模型只拿到 题名 + 章节索引 + 六维笔记，
            #    于是回答里出现"以上是基于题名、章节标题和段落分布…需核对原文段落"。
            #    现在：原文检索为空就先按论文结构取**真实正文**，再追加笔记。
            if not prefix_full and not retrieved:
                retrieved = self._sample_body(paper)
            # 3) 编译笔记补充：**检索命中**的按相关性进，再兜底把该篇**全部已编译笔记**补齐
            if doi and not prefix_full:
                try:
                    kbmeta = self._get_kbmeta()
                    notes = kbmeta.recall_paper(doi, question, top_k=3) if kbmeta else []
                    # 2026-09-12 用户反馈：中文问句（无空格）在 FTS/LIKE 上都可能 0 命中
                    # ⇒ 笔记编译好了却一份都进不了上下文。单篇会话目标文档已知，
                    # 这里直接把该篇笔记全部补上（通常 1~3 份、每份 ≤4KB）。
                    if kbmeta:
                        try:
                            notes = list(notes) + list(kbmeta.paper_notes_all(doi))
                        except Exception as e2:  # noqa: BLE001
                            logger.warning("单篇笔记全量兜底失败: %s", e2)
                except Exception as e:  # noqa: BLE001
                    logger.warning("单篇编译笔记召回失败: %s", e)
                    notes = []
                seen = {(it.get("para_id") or it.get("file")) for it in retrieved}
                for n in notes:
                    k = n.get("file") or n.get("para_id")
                    if k and k not in seen:
                        retrieved.append(n)
                        seen.add(k)
            # 4) 极端兜底：结构采样也拿不到（例如 document.json 无章节信息）→ 取摘要
            if not prefix_full and not retrieved:
                try:
                    r = self.engine.query(paper["doc_json"], section="Abstract",
                                          include="both", limit_chars=2500)
                    retrieved = r.get("items", [])[:2]
                except Exception:  # noqa: BLE001
                    retrieved = []
            context = "\n\n".join(_fmt_recall_item(it) for it in retrieved)
            # 全文在 system 前缀里 ⇒ 本轮 context 为空（user 消息只留问题，最大化前缀复用）；
            # 否则给片段加标签（标注来源/段落 ID，便于引用）。
            ctx_label = ("以下为论文原文/编译笔记片段（单语，标注来源，未翻译文献基于英文原文）："
                         if context else "")
        history = self.store.recent_messages(session_id, self.settings.history_max_messages)
        history = [m for m in history if m["role"] in ("user", "assistant")]
        history = self._fit_history(history)
        # ── 消息组装（2026-09-12 用户设计：分支各自独立，但共享"全文+编译输出"前缀）──
        # 旧实现把论文全文放进 **system** 末尾，而编译/翻译把同一份 `shared_ctx` 放在
        # **role=user** 消息的**最前面**（`compile.py:435-437` → `llm_service.py:256`
        # `messages=[{"role":"user","content":prompt}]`）⇒ **首 token 就不同**，
        # 编译已经 warm 出来的前缀缓存对问答完全不可用：
        # 于是"第 1 个会话全价重传全文、第 2 个会话才命中"（用户实测 session:14 命中 256、
        # session:15 命中 14208）。
        # 现在：问答也以 `role=user` + **字节一致的 shared_ctx 开头** ⇒ 直接继承编译/翻译
        # 已经建立的前缀缓存；其后才是本篇的问答指令，再往后是本会话自己的历史
        # ⇒ **跨会话共享前缀、会话之间内容互不干扰**（历史按 session_id 各自取）。
        messages: list[dict[str, str]] = []
        if prefix_full:
            messages.extend(self._paper_prefix_messages(paper, shared_ctx_text, system))
        else:
            messages.append({"role": "system", "content": system})
        for m in history:
            msg = {"role": m["role"], "content": m["content"]}
            # 思考模式：历史 assistant 消息的 reasoning_content 必须回传 API
            if m["role"] == "assistant" and m.get("reasoning_content"):
                msg["reasoning_content"] = m["reasoning_content"]
            messages.append(msg)
        user_content = question
        if context:
            user_content = f"{ctx_label}\n{context}\n\n问题：{question}"
        messages.append({"role": "user", "content": user_content})

        # 3) 流式回答并入库
        self.store.add_message(session_id, "user", question,
                               tokens=self._estimate_tokens(question))
        yield {"type": "start", "cached": False, "retrieved": len(retrieved),
               "fulltext_prefix": bool(prefix_full)}
        parts: list[str] = []
        reasoning_parts: list[str] = []
        try:
            for ev in self._llm_events(f"session:{session_id}", messages, effort):
                if ev["type"] == "reasoning":
                    reasoning_parts.append(ev["text"])
                    yield ev
                    continue
                parts.append(ev["text"])
                yield ev
        except Exception as e:  # noqa: BLE001
            logger.exception("对话流式失败")
            yield {"type": "error", "message": f"对话失败: {e}"}
            return
        answer = "".join(parts).strip()
        reasoning_content = "".join(reasoning_parts).strip() if reasoning_parts else None
        mid = self.store.add_message(session_id, "assistant", answer,
                                     tokens=self._estimate_tokens(answer),
                                     reasoning_content=reasoning_content)
        self.store.set_cached_answer(key, question, answer)
        yield {"type": "done", "cached": False, "message_id": mid,
               "tokens": self._estimate_tokens(question) + self._estimate_tokens(answer)}
