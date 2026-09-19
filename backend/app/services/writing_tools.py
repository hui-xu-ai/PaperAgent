# -*- coding: utf-8 -*-
"""写作 Agent 工具集（T3）：自动计划→检索→写作→验证→迭代→保存。

工具设计原则：
- 最小化工具数量（6 个），每个工具职责单一
- 检索复用 kb_tools 的 kb_recall（不重复实现）
- 保存走 kb_write_report（统一报告存储位置）
- 验证工具返回结构化反馈（字数/连贯性/完整性评分）
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 工具结果截断长度（防止上下文爆炸）
_TRIM = 1200  # 写作工具返回可以稍长（草稿片段）


def _trim(text: str, limit: int = _TRIM) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（截断，总长 {len(text)} 字符）"


# ---------------------------------------------------------------- 工具定义（OpenAI function 格式）

TOOL_SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "writing_plan",
            "description": "生成写作大纲/计划。根据主题和目标字数，输出结构化大纲（章节标题+每章节要点+预计字数分配）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "写作主题"},
                    "target_words": {"type": "integer", "description": "目标字数（中文字符）", "default": 600},
                    "style": {"type": "string", "description": "写作风格：essay/review/report/summary", "enum": ["essay", "review", "report", "summary"], "default": "essay"},
                    "key_points": {"type": "array", "items": {"type": "string"}, "description": "用户指定的关键要点（可选）"}
                },
                "required": ["topic"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "writing_retrieve",
            "description": "从知识库检索与写作主题相关的内容。返回相关笔记片段和文献引用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索查询（自然语言）"},
                    "top_k": {"type": "integer", "description": "返回结果数", "default": 5, "minimum": 1, "maximum": 10}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "writing_draft",
            "description": "撰写章节草稿。根据大纲和检索结果，生成指定章节的完整草稿。",
            "parameters": {
                "type": "object",
                "properties": {
                    "section_title": {"type": "string", "description": "章节标题"},
                    "outline_points": {"type": "array", "items": {"type": "string"}, "description": "本章节要点"},
                    "retrieved_content": {"type": "string", "description": "相关检索内容（可选，用于引用）"},
                    "target_words": {"type": "integer", "description": "本章节目标字数", "default": 200}
                },
                "required": ["section_title", "outline_points"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "writing_validate",
            "description": "自我验证草稿质量。检查字数、连贯性、完整性、逻辑性，返回结构化反馈和修改建议。",
            "parameters": {
                "type": "object",
                "properties": {
                    "draft": {"type": "string", "description": "待验证的草稿文本"},
                    "target_words": {"type": "integer", "description": "目标字数"},
                    "requirements": {"type": "array", "items": {"type": "string"}, "description": "用户指定的要求（可选）"}
                },
                "required": ["draft", "target_words"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "writing_revise",
            "description": "根据验证反馈修订草稿。保留优点，改进不足，输出修订版本。",
            "parameters": {
                "type": "object",
                "properties": {
                    "draft": {"type": "string", "description": "原草稿"},
                    "feedback": {"type": "string", "description": "验证反馈（来自 writing_validate）"},
                    "focus_areas": {"type": "array", "items": {"type": "string"}, "description": "重点改进方向（可选）"}
                },
                "required": ["draft", "feedback"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "writing_save",
            "description": "保存最终报告到 .md 文件。存储到 knowledge_base/_reports/ 目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "报告标题（将用作文件名）"},
                    "content": {"type": "string", "description": "完整报告内容（Markdown 格式）"},
                    "metadata": {"type": "object", "description": "元数据（可选）：{topic, style, word_count, created_at}", "properties": {
                        "topic": {"type": "string"},
                        "style": {"type": "string"},
                        "word_count": {"type": "integer"},
                        "created_at": {"type": "string"}
                    }}
                },
                "required": ["title", "content"]
            }
        }
    }
]


def tool_specs() -> list[dict]:
    """返回工具规格列表（供 LLM 工具调用使用）。"""
    return TOOL_SPECS


# ---------------------------------------------------------------- 工具实现

def run_tool(name: str, args: dict, **kwargs) -> dict:
    """执行写作工具（统一入口，返回 {ok, result, error}）。"""
    try:
        if name == "writing_plan":
            return _writing_plan(args)
        elif name == "writing_retrieve":
            return _writing_retrieve(args, **kwargs)
        elif name == "writing_draft":
            return _writing_draft(args)
        elif name == "writing_validate":
            return _writing_validate(args)
        elif name == "writing_revise":
            return _writing_revise(args)
        elif name == "writing_save":
            return _writing_save(args)
        else:
            return {"ok": False, "error": f"未知工具：{name}"}
    except Exception as e:
        logger.exception("写作工具执行失败：%s", name)
        return {"ok": False, "error": str(e)}


def _writing_plan(args: dict) -> dict:
    """生成写作大纲。

    注意：此工具不直接调用 LLM，而是返回结构化提示，由上层 LLM 循环生成实际大纲。
    这样设计是为了让 LLM 在工具循环中保持上下文连贯性。
    """
    topic = args.get("topic", "")
    target_words = args.get("target_words", 600)
    style = args.get("style", "essay")
    key_points = args.get("key_points", [])

    if not topic:
        return {"ok": False, "error": "缺少主题（topic）"}

    # 返回结构化提示，引导 LLM 生成大纲
    prompt_parts = [
        f"主题：{topic}",
        f"目标字数：{target_words} 字",
        f"风格：{style}",
    ]
    if key_points:
        prompt_parts.append(f"关键要点：{', '.join(key_points)}")

    # 计算章节数和每章字数
    if style == "essay":
        num_sections = 3  # 引言 + 主体 + 结论
        section_words = target_words // num_sections
    elif style == "review":
        num_sections = 4  # 引言 + 方法 + 结果 + 讨论
        section_words = target_words // num_sections
    elif style == "report":
        num_sections = 5  # 摘要 + 引言 + 方法 + 结果 + 结论
        section_words = target_words // num_sections
    else:  # summary
        num_sections = 2  # 概述 + 要点
        section_words = target_words // num_sections

    structure = {
        "suggested_sections": num_sections,
        "words_per_section": section_words,
        "total_words": target_words,
        "style": style,
    }

    return {
        "ok": True,
        "result": {
            "prompt": "\n".join(prompt_parts),
            "structure": structure,
            "instruction": f"请根据以上信息生成 {num_sections} 个章节的大纲，每章约 {section_words} 字。输出格式：JSON 数组，每项包含 {{'title': str, 'points': list[str], 'words': int}}。"
        }
    }


def _writing_retrieve(args: dict, **kwargs) -> dict:
    """从知识库检索相关内容。

    复用 kb_tools 的 kb_recall 工具（通过导入调用）。
    """
    query = args.get("query", "")
    top_k = args.get("top_k", 5)

    if not query:
        return {"ok": False, "error": "缺少检索查询（query）"}

    try:
        # 导入 kb_tools 的 recall 功能
        from .kb_tools import run_tool as kb_run_tool

        result = kb_run_tool("kb_recall", {"query": query, "top_k": top_k})
        if result.get("ok"):
            return {"ok": True, "result": result["result"]}
        else:
            return {"ok": False, "error": result.get("error", "检索失败")}
    except ImportError:
        return {"ok": False, "error": "知识库工具不可用"}
    except Exception as e:
        return {"ok": False, "error": f"检索异常：{e}"}


def _writing_draft(args: dict) -> dict:
    """撰写章节草稿。

    注意：此工具不直接调用 LLM，而是返回结构化提示，由上层 LLM 循环生成实际草稿。
    """
    section_title = args.get("section_title", "")
    outline_points = args.get("outline_points", [])
    retrieved_content = args.get("retrieved_content", "")
    target_words = args.get("target_words", 200)

    if not section_title or not outline_points:
        return {"ok": False, "error": "缺少章节标题或要点"}

    prompt_parts = [
        f"章节标题：{section_title}",
        f"目标字数：{target_words} 字",
        f"要点：",
    ]
    for i, point in enumerate(outline_points, 1):
        prompt_parts.append(f"  {i}. {point}")

    if retrieved_content:
        prompt_parts.append(f"\n参考材料（请适当引用）：\n{_trim(retrieved_content, 800)}")

    return {
        "ok": True,
        "result": {
            "prompt": "\n".join(prompt_parts),
            "instruction": f"请根据以上要点撰写完整章节草稿，约 {target_words} 字。要求：逻辑连贯、论据充分、语言流畅。直接输出正文，不要输出标题。"
        }
    }


def _writing_validate(args: dict) -> dict:
    """自我验证草稿质量。

    返回结构化反馈：字数统计、连贯性评分、完整性评分、修改建议。
    """
    draft = args.get("draft", "")
    target_words = args.get("target_words", 600)
    requirements = args.get("requirements", [])

    if not draft:
        return {"ok": False, "error": "缺少草稿内容"}

    # 字数统计
    actual_words = len(draft.replace(" ", "").replace("\n", ""))
    word_ratio = actual_words / target_words if target_words > 0 else 0

    # 基础检查（启发式）
    issues = []
    suggestions = []

    # 字数检查
    if word_ratio < 0.8:
        issues.append(f"字数不足（{actual_words}/{target_words}，仅 {word_ratio*100:.0f}%）")
        suggestions.append(f"需要扩充约 {target_words - actual_words} 字")
    elif word_ratio > 1.2:
        issues.append(f"字数超出（{actual_words}/{target_words}，{word_ratio*100:.0f}%）")
        suggestions.append(f"需要精简约 {actual_words - target_words} 字")

    # 段落检查
    paragraphs = [p.strip() for p in draft.split("\n\n") if p.strip()]
    if len(paragraphs) < 2:
        issues.append("段落过少（仅 1 段）")
        suggestions.append("建议分成多个段落，提高可读性")

    # 句子长度检查（启发式：过长句子可能影响可读性）
    sentences = draft.replace("。", "\n").replace("！", "\n").replace("？", "\n").split("\n")
    long_sentences = [s for s in sentences if len(s.strip()) > 80]
    if len(long_sentences) > 3:
        issues.append(f"存在 {len(long_sentences)} 个过长句子（>80 字）")
        suggestions.append("建议拆分长句，提高可读性")

    # 用户要求检查
    for req in requirements:
        if req.lower() not in draft.lower():
            issues.append(f"未满足要求：{req}")
            suggestions.append(f"请确保包含：{req}")

    # 评分（启发式）
    coherence_score = 10 - len(issues)  # 简单扣分制
    coherence_score = max(0, min(10, coherence_score))

    completeness_score = 10 if word_ratio >= 0.9 else int(word_ratio * 10)
    completeness_score = max(0, min(10, completeness_score))

    return {
        "ok": True,
        "result": {
            "word_count": actual_words,
            "target_words": target_words,
            "word_ratio": f"{word_ratio*100:.0f}%",
            "paragraph_count": len(paragraphs),
            "coherence_score": coherence_score,
            "completeness_score": completeness_score,
            "issues": issues,
            "suggestions": suggestions,
            "passed": len(issues) == 0
        }
    }


def _writing_revise(args: dict) -> dict:
    """根据验证反馈修订草稿。

    注意：此工具不直接调用 LLM，而是返回结构化提示，由上层 LLM 循环生成修订版本。
    """
    draft = args.get("draft", "")
    feedback = args.get("feedback", "")
    focus_areas = args.get("focus_areas", [])

    if not draft or not feedback:
        return {"ok": False, "error": "缺少草稿或反馈"}

    prompt_parts = [
        "原草稿：",
        draft,
        "\n验证反馈：",
        feedback,
    ]

    if focus_areas:
        prompt_parts.append("\n重点改进方向：")
        for i, area in enumerate(focus_areas, 1):
            prompt_parts.append(f"  {i}. {area}")

    return {
        "ok": True,
        "result": {
            "prompt": "\n".join(prompt_parts),
            "instruction": "请根据验证反馈修订草稿。要求：保留优点，改进不足，输出完整修订版本（不要只输出修改部分）。"
        }
    }


def _writing_save(args: dict) -> dict:
    """保存最终报告到 .md 文件。

    复用 kb_tools 的 kb_write_report 工具。
    """
    title = args.get("title", "")
    content = args.get("content", "")
    metadata = args.get("metadata", {})

    if not title or not content:
        return {"ok": False, "error": "缺少标题或内容"}

    try:
        # 导入 kb_tools 的 save 功能
        from .kb_tools import run_tool as kb_run_tool

        # 添加元数据到内容头部
        header_parts = [f"# {title}", ""]
        if metadata:
            if metadata.get("topic"):
                header_parts.append(f"**主题**：{metadata['topic']}")
            if metadata.get("style"):
                header_parts.append(f"**风格**：{metadata['style']}")
            if metadata.get("word_count"):
                header_parts.append(f"**字数**：{metadata['word_count']}")
            if metadata.get("created_at"):
                header_parts.append(f"**创建时间**：{metadata['created_at']}")
            header_parts.append("")

        full_content = "\n".join(header_parts) + content

        result = kb_run_tool("kb_write_report", {"title": title, "content": full_content})
        if result.get("ok"):
            return {"ok": True, "result": result["result"]}
        else:
            return {"ok": False, "error": result.get("error", "保存失败")}
    except ImportError:
        return {"ok": False, "error": "知识库工具不可用"}
    except Exception as e:
        return {"ok": False, "error": f"保存异常：{e}"}
