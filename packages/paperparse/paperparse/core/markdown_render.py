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

__all__ = ["render", "list_templates", "render_variant", "variant_tags",
           "fallback_frontmatter", "FIELD_ORDER"]

# recognized 变体排除的章节（翻译/识别校准版不需要）
EXCLUDE_SECTIONS = {
    "references", "acknowledgements", "acknowledgment", "conflict of interest",
    "data availability statement", "supporting information", "keywords",
    "author contributions", "funding",
}

# ---------------------------------------------------------------------------
# 尾部杂项截断（en.md 干净版 / 双语主产物）——判据单一来源在 block_classify.is_tail_noise
# （与 paperkb.context.tail_cut_index 同口径）。en.md 既是阅读区原文，又被 chat L2
# 授权全文问答直接注入 AI，故与翻译/编译上下文同样剔除致谢/利益冲突/作者贡献/数据
# 可用性/支撑信息等尾部声明（2026-09-19 用户要求补全这部分清洗规则）。
def _is_tail_noise_para(p, idx: int, total: int) -> bool:
    """[局部] 段落是否"结尾杂项"（委托 block_classify.is_tail_noise）。"""
    from paperparse.core.block_classify import is_tail_noise
    return is_tail_noise(getattr(p, "text_en", "") or "", idx, total,
                         is_heading=bool(getattr(p, "is_heading", False)),
                         section=getattr(p, "section", "") or "")


def _tail_cut_index(paragraphs) -> int:
    """[局部] 第一个"结尾杂项"段落下标；无则返回总数（render_clean/_build_items 共用）。"""
    total = len(paragraphs)
    for i, p in enumerate(paragraphs):
        if _is_tail_noise_para(p, i, total):
            return i
    return total


def _tag(keyword: str) -> str:
    """[局部] 关键词 → Obsidian 标签（空格/特殊字符 → 下划线）"""
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]", "_", keyword.strip()).strip("_") or "keyword"


def variant_tags(doc: ArticleDocument, base: str = "文献") -> list[str]:
    """[全局] 变体 frontmatter 的 tags（**唯一来源**：模板兜底与 backend 注入共用）。

    = 基础标签 + 文章类型 + 关键词（逐项过 `_tag` 净化）。backend 注入 frontmatter 时
    必须用它取值——否则两侧各算一套，tags 会随路径不同而不同。
    """
    tags = [base]
    if doc.metadata.article_type:
        tags.append(_tag(doc.metadata.article_type))
    tags += [_tag(k) for k in doc.metadata.keywords]
    return tags


# 头部字段顺序（**用户 2026-09-16 给定**）。与 `paperkb.headmeta.FIELD_ORDER` 必须一致；
# 两个包**互不依赖**（paperkb 不 import paperparse，反之亦然），故顺序在两处各写一次，
# 由 `backend/tests/test_headmeta_contract.py` 跨包断言守卫（backend 同时依赖两者）。
FIELD_ORDER = ("作者", "通讯作者", "研究单位", "年份", "期刊", "影响因子",
               "JCR分区", "中科院分区", "DOI", "被引", "关键词")


def _yaml_scalar(value) -> str:
    """[局部] 标量 → YAML 行内文本（字符串一律 JSON 双引号转义；数字裸写）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def fallback_frontmatter(doc: ArticleDocument, tags: list[str] | None = None) -> str:
    """[全局] 未注入 `frontmatter` 时的**兜底头部**（仅有 document.json 能给的字段）。

    为什么需要：paperparse 独立 CLI / parse-only 导出（`api._export_parse_only`）不经过
    backend，拿不到 `papers_meta`/`journals.db`。此时输出**同一套键名与顺序**（字段更少：
    无期刊指标/分区/被引），而不是退回旧的英文字段集（`authors:` / `journal:` 那套）。
    正常链路（backend 的 `variant_frontmatter()`）永远注入，不走这里。
    """
    m = doc.metadata
    fields: dict = {}
    if m.authors:
        fields["作者"] = ", ".join(str(a).strip() for a in m.authors if str(a).strip())
    year = str(m.year or "").strip()
    if year:
        fields["年份"] = int(year) if year.isdigit() else year
    if (m.journal or "").strip():
        fields["期刊"] = str(m.journal).strip()
    if (m.doi or "").strip():
        fields["DOI"] = str(m.doi).strip()
    if m.keywords:
        fields["关键词"] = ", ".join(str(k).strip() for k in m.keywords if str(k).strip())

    lines = ["---"]
    if (m.title or "").strip():
        lines.append(f"title: {_yaml_scalar(m.title)}")
    for key in FIELD_ORDER:
        if key in fields:
            lines.append(f"{key}: {_yaml_scalar(fields[key])}")
    lines.append("tags: " + json.dumps(list(tags or []), ensure_ascii=False))
    lines.append("source: pdf")
    if (m.extraction_time or "").strip():
        lines.append(f"created: {m.extraction_time}")
    lines.append("---")
    return "\n".join(lines) + "\n"


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

    # 尾部杂项截断（与 render_clean / AI 上下文同口径）：双语主产物同样不含致谢/声明等
    _cut = _tail_cut_index(doc.paragraphs)
    for p in doc.paragraphs[:_cut]:
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
           frontmatter: str = "") -> str:
    """[全局] 渲染 document.json → Obsidian 兼容 Markdown

    参数:
        doc: ArticleDocument（单一事实源）
        template: 模板名（skill/templates/<name>.md.j2）
        exclude_sections: 需排除的章节名集合（小写；recognized 变体用）
        frontmatter: **完整 YAML frontmatter 块**（含首尾 `---`），由调用方注入。

    2026-09-16 用户要求（逐字）："元数据，按照作者、通讯作者、研究单位、年份、期刊、影响因子、
    JCR分区、中科院分区、DOI、被引、关键词排列。模板统一更换成这个样式。元数据显示，模板中重复的
    这个：`[!info] 文献信息`，这部分直接删除。"
    ⇒ 本模块**不再自己拼头部**：模板只输出 `{{ frontmatter }}`。
      为什么必须外部注入：期刊/年份/被引/影响因子/分区都不在 `document.json`（本模块唯一能拿到的
      `doc.metadata`）里，而在 `papers_meta` + `journals.db` —— 模板自己拼必然拼出空值
      （实测变体显示 `期刊: ""`、`被引：0`，而 `_note.md` 有真值）。唯一装配入口 =
      `paperkb.api.variant_frontmatter()`（backend 侧）⇒ 头部只有一份实现，不再两处分叉。
      未注入（如 tools/测试直调）时用 `fallback_frontmatter()` 兜底——**同一套键名与顺序**，
      只是字段少（document.json 里没有期刊指标/分区/被引）。
    """
    env = Environment(
        loader=FileSystemLoader(str(templates_dir())),
        autoescape=select_autoescape(disabled_extensions=("j2",)),
    )
    env.filters["strip_ref_num"] = _strip_ref_num
    try:
        tpl = env.get_template("%s.md.j2" % template)
        tags = variant_tags(doc)
        tags_json = json.dumps(tags, ensure_ascii=False)
        items = _filter_excluded(_build_items(doc), exclude_sections or set())
        fm = frontmatter or fallback_frontmatter(doc, tags)
        return tpl.render(doc=doc, items=items, tags=tags, tags_json=tags_json,
                          frontmatter=fm)
    except Exception as exc:
        raise PaperError("PAPER-0040", stage="S7",
                         detail={"template": template, "exc": str(exc)[:300]}) from exc


def render_variant(doc: ArticleDocument, variant: str = "recognized",
                   frontmatter: str = "") -> str:
    """[全局] 变体渲染（M5 输出结构）：
      recognized：识别校准版（英文正文+图，排除参考文献/致谢/COI 等章节）
      translated ：中英对照版（英文上中文下，不折叠）
      zh         ：纯中文版（保留标题结构，移除英文对照，未译段回退英文）——备份包 .zh.md
      summary    ：AI 阅读总结版（仅 frontmatter + 总结 callout + 标题）

    `frontmatter`：见 `render()`（由 `paperkb.api.variant_frontmatter()` 装配后注入）。
    """
    if variant == "recognized":
        return render(doc, template="recognized", exclude_sections=EXCLUDE_SECTIONS,
                      frontmatter=frontmatter)
    if variant == "translated":
        return render(doc, template="obsidian_bilingual", frontmatter=frontmatter)
    if variant == "zh":
        return render(doc, template="zh_only", frontmatter=frontmatter)     # 纯中文版（保留标题结构，移除英文对照）
    if variant == "summary":
        return render(doc, template="summary_variant", frontmatter=frontmatter)
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
    seen_fig: set[str] = set()   # 同一图文件只 emit 一次（防误判段与真题注段共享图号 key 致重复插图）
    if title:
        out.append("# " + title)
    in_refs = False
    # 尾部杂项截断（致谢/利益冲突/作者贡献/数据可用性/支撑信息…）——与 AI 上下文同口径
    _cut = _tail_cut_index(doc.paragraphs)
    for p in doc.paragraphs[:_cut]:
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
                _f = fig_by_key[key]
                if _f not in seen_fig:
                    out.append("![](%s)" % _f)
                    seen_fig.add(_f)
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
