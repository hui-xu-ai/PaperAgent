#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/markdown_render.py
功能: S7 Markdown 渲染：jinja2 模板引擎（Obsidian 兼容），多模板可换
      obsidian_bilingual（默认）/ obsidian_bilingual_alt / plain / recognized（识别校准版）
      渲染净化：
      - 跳过重复的标题/作者段落（正文不重复 front matter）
      - 有摘要时插入 "## Abstract" 标题，跳过 front matter 裸段落
      - 变体可排除章节（recognized：不含参考文献/致谢/COI 等）
      - article_type 进 frontmatter type 与 tags
对外接口: render / list_templates / render_variant
版本: v1.2.0 (2026-08-19)
版本历史:
  v1.2.0 修复摘要重复（用户反馈）：_norm_caption 连字展开 + 摘要正文段（section==Abstract）
          优先承载译文并在其前插 ## Abstract 标题，不再与 metadata.abstract 重复渲染
  v1.1.0 净化渲染：标题/作者去重、Abstract 插入、章节排除、article_type
  v1.0.0 初始版本
"""
from __future__ import annotations

import json
import re

from jinja2 import Environment, FileSystemLoader, select_autoescape

from paperparse.config import templates_dir
from paperparse.middleware.errors import PaperError
from paperparse.middleware.schema import ArticleDocument

__all__ = ["render", "list_templates", "render_variant"]

# recognized 变体排除的章节（翻译/识别校准版不需要）
EXCLUDE_SECTIONS = {
    "references", "acknowledgements", "acknowledgment", "conflict of interest",
    "data availability statement", "supporting information", "keywords",
    "author contributions", "funding",
}


def _tag(keyword: str) -> str:
    """[局部] 关键词 → Obsidian 标签（空格/特殊字符 → 下划线）"""
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]", "_", keyword.strip()).strip("_") or "keyword"


_LIG_FOLD = str.maketrans({"\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl"})


def _norm_caption(text: str) -> str:
    """[局部] 图注/摘要归一化（用于 图↔图注 / 摘要去重 匹配）：
    连字展开（fi/ffi 差异）+ 小写 + 空白折叠"""
    return re.sub(r"\s+", " ", text.translate(_LIG_FOLD).strip().lower())


_FIG_NUM_RE = re.compile(r"^(?:fig(?:ure)?|table|scheme)\.?\s*(\d+)", re.IGNORECASE)


def _fig_key(text: str) -> str | None:
    """[局部] 图注编号键（"Figure 1. ..." → "1"；用于图↔图注匹配，图注段合并后仍可匹配）"""
    m = _FIG_NUM_RE.match(text.strip())
    return m.group(1) if m else None


def _build_items(doc: ArticleDocument) -> list[dict]:
    """[局部] 段落/图/标题 → 渲染项序列（净化：去重 front matter/摘要、Abstract 插入、
    图插在对应题注之前——T-C）"""
    fig_by_key: dict[str, dict] = {}
    for f in doc.figures:
        key = _fig_key(f.caption or "")
        if key:
            fig_by_key.setdefault(key, {"file": f.file, "caption": f.caption or ""})
    title = (doc.metadata.title or "").strip()
    author_texts = {a.strip(" *") for a in doc.metadata.authors if a.strip()}
    abstract_norm = _norm_caption(doc.metadata.abstract or "")
    items: list[dict] = []
    has_abstract_heading = False

    for p in doc.paragraphs:
        if p.is_heading:
            t = p.text_en.strip()
            # P15：p14 的 heading 段 text 保留 md 的 "## " 前缀，模板输出会再
            # 加 "## " → 剥掉（"## A B S T R A C T" → "A B S T R A C T"）
            t = re.sub(r"^#+\s*", "", t).strip()
            # P15：Elsevier 装饰标题（"A B S T R A C T" / "A R T I C L E I N F O"
            # 字母间空格）→ 规范化正常词（"Abstract" / "Article Info"）
            if re.match(r"^(?:[A-Z] ?)+$", t):
                t = t.replace(" ", "").title()
            # 连字归一化后与元数据标题比较（块文本含 ﬁ，metadata.title 已清理）
            t_norm = t.translate(str.maketrans({"\ufb01": "fi", "\ufb02": "fl"}))
            if t_norm == title or t == title:
                continue                       # 标题已由 H1 渲染，跳过重复
            if t in author_texts or any(a and t.startswith(a[:20]) for a in author_texts):
                continue                       # 作者行被误判为标题 → 跳过
            if t.lower().replace(" ", "").startswith("abstract"):
                has_abstract_heading = True    # "A B S T R A C T"（Elsevier 空格排版）
            items.append({"type": "heading", "text": t,
                          "section": p.section or t})   # 标题段落的章节即其自身文本
            continue
        if p.is_caption:
            # 图插在题注之前（Obsidian 习惯：先图后题注）
            fig = fig_by_key.get(_fig_key(p.text_en) or "")
            if fig is not None:
                items.append({"type": "figure", "file": fig["file"],
                              "caption": fig["caption"], "section": p.section})
            items.append({"type": "para", "text_en": p.text_en.strip(),
                          "text_zh": p.text_zh, "section": p.section})
            continue
        # P15：标题段可能被误标为 body（is_heading=False，text_en 带 "# " 前缀）——与
        # render_clean 的标题去重保持一致：剥 "# " 前缀后等于 metadata.title → 跳过。
        # 否则该 body 标题段会连同其 text_zh 一起渲染；当 text_zh 被误填为摘要译文时，
        # 会造成摘要重复/乱序（用户反馈"摘要区 中/英/中/中"）。
        if title:
            _body_t = re.sub(r"^#+\s*", "", (p.text_en or "").strip()).strip()
            # 与 heading 标题去重一致：折叠连字（ﬃ/ffi 差异，PDF 文本常有）后比对 title
            _body_t_norm = _body_t.translate(str.maketrans({"\ufb01": "fi", "\ufb02": "fl"}))
            if _body_t_norm == title or _body_t == title:
                continue
        if p.section == "" and doc.metadata.abstract:
            continue                           # front matter 裸段落（摘要已由 Abstract 节提供）
        if not (p.text_en or "").strip():
            continue                           # R10：规则删除后的空段落不渲染
        is_abstract_body = ((p.section or "").replace(" ", "").lower()
                            == "abstract"
                            and not p.is_heading and not p.is_caption)
        if not is_abstract_body and abstract_norm and _norm_caption(p.text_en) == abstract_norm:
            continue                           # 非摘要节重复的摘要段 → 去重
        items.append({"type": "para", "text_en": p.text_en.strip(),
                      "text_zh": p.text_zh, "section": p.section})

    # 摘要节：
    # ① 存在摘要正文段（section=='Abstract'，如 mark_abstract_section 标记的 P002，含译文）
    #    → 在其前插 ## Abstract 标题，且不再重复插入 metadata.abstract（正文段已承载内容与译文）
    abstract_body_idx = next((i for i, it in enumerate(items)
                              if it["type"] == "para"
                              and (it.get("section") or "").replace(" ", "")
                              .lower() == "abstract"), None)
    if abstract_body_idx is not None:
        if not has_abstract_heading:
            items.insert(abstract_body_idx,
                         {"type": "heading", "text": "Abstract", "section": "Abstract"})
        abstract_body_idx += 1          # 摘要正文段之后插摘要图
        _insert_abstract_figure(items, doc, abstract_body_idx)
    # ② 无摘要正文段但元数据有摘要 → 在首个章节标题前插 ## Abstract + metadata.abstract
    elif doc.metadata.abstract and not has_abstract_heading:
        idx = next((i for i, it in enumerate(items) if it["type"] == "heading"), len(items))
        items.insert(idx, {"type": "heading", "text": "Abstract", "section": "Abstract"})
        items.insert(idx + 1, {"type": "para", "text_en": doc.metadata.abstract.strip(),
                               "text_zh": None, "section": "Abstract"})
        _insert_abstract_figure(items, doc, idx + 2)
    return items


def _insert_abstract_figure(items: list[dict], doc: ArticleDocument, at: int) -> None:
    """[局部] R10：摘要图（graphical abstract）插在 Abstract 标题/正文之后"""
    abs_figs = [f for f in doc.figures if getattr(f, "abstract", False)]
    if not abs_figs:
        return
    for i, f in enumerate(abs_figs):
        items.insert(at + i, {"type": "figure", "file": f.file,
                              "caption": f.caption or "", "section": "Abstract"})


def _filter_excluded(items: list[dict], exclude: set[str]) -> list[dict]:
    """[局部] 按章节名排除（recognized 变体）"""
    if not exclude:
        return items
    out = []
    for it in items:
        sec = (it.get("section") or "").strip().lower()
        if sec in exclude:
            continue
        out.append(it)
    return out


def _strip_ref_num(raw: str) -> str:
    """[局部] 剥离参考文献开头的 "[n] "（列表序号已有编号；避免 Obsidian 把 "1. [1]"
    解析为任务复选框——用户问题 6）"""
    return re.sub(r"^\[\d+\]\s*", "", raw or "")


def render(doc: ArticleDocument, template: str = "obsidian_bilingual",
           exclude_sections: set[str] | None = None,
           note_header: str = "") -> str:
    """[全局] 渲染 document.json → Obsidian 兼容 Markdown

    参数:
        doc: ArticleDocument（单一事实源）
        template: 模板名（skill/templates/<name>.md.j2）
        exclude_sections: 需排除的章节名集合（小写；recognized 变体用）
        note_header: `> [!info] 文献信息` 的正文行（**由 backend 用 L1 同一套元数据渲染后传入**）。
            2026-09-16 用户要求：变体头部必须与编译 L1 开头一致——模板自己只能拿到 `doc.metadata`
            （解析产物，**没有期刊/被引**，所以曾恒显示 `期刊: —`），故改为由调用方注入权威文本；
            为空时模板回退旧的 `doc.metadata` 渲染（保持旧行为可跑）。
    """
    env = Environment(
        loader=FileSystemLoader(str(templates_dir())),
        autoescape=select_autoescape(disabled_extensions=("j2",)),
    )
    env.filters["strip_ref_num"] = _strip_ref_num
    try:
        tpl = env.get_template("%s.md.j2" % template)
        tags = ["文献"]
        if doc.metadata.article_type:
            tags.append(_tag(doc.metadata.article_type))
        tags += [_tag(k) for k in doc.metadata.keywords]
        tags_json = json.dumps(tags, ensure_ascii=False)
        items = _filter_excluded(_build_items(doc), exclude_sections or set())
        return tpl.render(doc=doc, items=items, tags=tags, tags_json=tags_json,
                          note_header=note_header)
    except Exception as exc:
        raise PaperError("PAPER-0040", stage="S7",
                         detail={"template": template, "exc": str(exc)[:300]}) from exc


def render_variant(doc: ArticleDocument, variant: str = "recognized",
                   note_header: str = "") -> str:
    """[全局] 变体渲染（M5 输出结构）：
      recognized：识别校准版（英文正文+图，排除参考文献/致谢/COI 等章节）
      translated ：中英对照版（英文上中文下，不折叠）
      zh         ：纯中文版（保留标题结构，移除英文对照，未译段回退英文）——备份包 .zh.md
      summary    ：AI 阅读总结版（仅 frontmatter + 总结 callout + 标题）
    """
    if variant == "recognized":
        return render(doc, template="recognized", exclude_sections=EXCLUDE_SECTIONS,
                      note_header=note_header)
    if variant == "translated":
        return render(doc, template="obsidian_bilingual", note_header=note_header)
    if variant == "zh":
        return render(doc, template="zh_only", note_header=note_header)     # 纯中文版（保留标题结构，移除英文对照）
    if variant == "summary":
        return render(doc, template="summary_variant", note_header=note_header)
    raise PaperError("PAPER-0501", stage="S7",
                     detail={"reason": "未知变体",
                             "available": ["recognized", "translated", "zh", "summary"]})


def render_clean(doc: ArticleDocument, *, include_references: bool = False) -> str:
    """[全局] 干净版渲染（2026-08-26 用户决策：en.md 为**与 PDF 等价的原始版本**——
    无 frontmatter/笔记属性，默认无参考文献表；空行规范）。

    供 backend 复核落地后重渲染 en.md（_post_parse_clean），与 process_pdf_v2
    build_markdown 产物格式一致；**不经过任何 J2 模板**（模板曾致 <DOI>.en.md
    空行失控/格式不可控，用户明确要求脱离模板）。

    2026-08-26 修复：**不复用 _build_items**（它为模板设计会跳过标题段，
    曾致 en.md 丢失文献标题）——直接遍历 doc.paragraphs 拼接，H1 标题显式输出。
    """
    title = (doc.metadata.title or "").strip()
    fig_by_key: dict[str, str] = {}
    for f in doc.figures:
        key = _fig_key(f.caption or "")
        if key:
            fig_by_key[key] = f.file
    out: list[str] = []
    if title:
        out.append("# " + title)
    in_refs = False
    for p in doc.paragraphs:
        t = (p.text_en or "").strip()
        if not t:
            continue
        if p.is_heading:
            t2 = re.sub(r"^#+\s*", "", t).strip()
            if re.match(r"^(?:[A-Z] ?)+$", t2):     # 装饰标题（A B S T R A C T）
                t2 = t2.replace(" ", "").title()
            if t2 == title:
                continue                             # 标题段重复 → 跳过（H1 已输出）
            if re.match(r"^references$", t2, re.I):
                in_refs = True
                continue
            out.append("## " + t2)
            continue
        if p.is_caption:
            key = _fig_key(t)
            if key and key in fig_by_key:
                out.append("![](%s)" % fig_by_key[key])
            out.append(t)
            continue
        if not p.is_heading:
            # 标题段可能被误标 body（text_en 含 "# " 前缀）→ 剥前缀比对 title 跳过
            t2c = re.sub(r"^#+\s*", "", t).strip()
            if t2c == title:
                continue
        # 2026-08-26：参考文献条目（含 <sup>[N]</sup> 形态，无标题时靠条目连续触发）
        if in_refs or re.match(r"^(?:<sup>)?\[\d+\](?:</sup>)?\s+\S", t):
            in_refs = True
            continue
        in_refs = False                       # 非条目段 → 退出参考文献区
        out.append(t)
    md = "\n\n".join(out).strip() + "\n"
    md = re.sub(r"\n{3,}", "\n\n", md)     # 空行规范（连续空行 → 单个空行）
    if not include_references:
        md = _strip_refs(md)               # 输出前保险：尾部参考文献特征兜底
    return md


_REF_LINE_RES = (
    re.compile(r"^\|?\s*\[\d+\]\s*\|?\s+\S"),            # 表格行 | [1] | Y. ...
    re.compile(r"^(?:<sup>)?\[\d+\](?:</sup>)?\s+[A-Z]"),  # 上标/裸 [N] 条目
)


def _strip_refs(md: str) -> str:
    """[局部] 输出前保险：删除**尾部参考文献区**（References 标题直接删；
    否则尾部连续 ≥2 行 [N] 特征删到尾部）。正文内联引用在行内不匹配。"""
    lines = md.splitlines()
    for i, ln in enumerate(lines):
        if re.match(r"^#{0,3}\s*references\s*$", ln, re.I):
            return "\n".join(lines[:i]).rstrip() + "\n"

    def _is_ref(ln: str) -> bool:
        return any(r.match(ln) for r in _REF_LINE_RES)
    region_end = len(lines)
    nonempty_refs = 0
    while region_end > 0:
        ln = lines[region_end - 1]
        if not ln.strip():
            region_end -= 1
            continue
        if _is_ref(ln):
            region_end -= 1
            nonempty_refs += 1
            continue
        break
    if nonempty_refs >= 2:
        return "\n".join(lines[:region_end]).rstrip() + "\n"
    return md


def list_templates() -> list[str]:
    """[全局] 可用模板列表（供配置校验）"""
    names = []
    for p in templates_dir().glob("*.md.j2"):
        names.append(p.name[: -len(".md.j2")])
    return sorted(names)
