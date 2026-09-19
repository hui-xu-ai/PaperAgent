# -*- coding: utf-8 -*-
"""论文全文共享上下文（paper_context）——翻译与编译共用同一「全文前缀」。

设计意图（用户 M3b 重构，共享全文前缀 + 缓存友好）：
- 一篇文章的「干净全文」被抽象为**单一、字节稳定**的上下文块 ``paper_context(doc)``，
  同时作为「翻译」与「编译(_note)」两个请求的**同一前缀**（系统/结构前缀 + 全文块）。
- 因两处前缀**字节一致**，服务端提示词缓存对第 2+ 次调用命中缓存价（全文按缓存价），
  显著降低重复传输全文的成本。
- 全文块内容：标题 + 章节(## ) + 干净正文 text_en（**跳过 References**）；段落以
  ``[para_id]`` 标识，公式以 ``[[MATHn]]`` **全局**标签占位（翻译 reassemble 用全局
  公式表逐字节回填）。**绝不读 text_zh**（编译/翻译均不依赖翻译结果作为上下文）。

与其他模块耦合：仅依赖 ``translate.latextap.split_text`` 做公式占位；不写 text_zh。
"""
from __future__ import annotations

import re

from .doc import PaperDoc

# References 类章节：全文块跳过（参考文献无需进入翻译/编译上下文）。
_REF_SECTION_KEYWORDS = ("references", "bibliography", "参考文献")

# 共享系统/结构前缀：翻译与编译**共用**（字节一致 → 命中提示词缓存）。
CTX_HEADER = (
    "你将处理一篇学术论文。以下是论文全文上下文（标题 + 章节 + 干净正文，已跳过 References）。\n"
    "正文中的公式已用 [[MATHn]] 标签占位（公式对照附后，翻译时**用标签原样指代、绝不展开改写**）；"
    "段落以 [Pxxx]（para_id）标识，引用段落一律使用 [Pxxx]。\n\n"
    "## 论文全文\n"
)


def _is_ref_section(text: str) -> bool:
    """判断文本是否命中 References 类章节（大小写不敏感，子串匹配）。"""
    t = (text or "").lower()
    return any(kw in t for kw in _REF_SECTION_KEYWORDS)


# 结尾杂项（不该进 AI 上下文）：References 之外的致谢/利益冲突/作者贡献/数据可用性/
# 支撑信息等。**2026-09-12 用户实测**：PNAS 篇 42 段（ACKNOWLEDGMENTS + 参考文献条目）
# 混进了共享前缀（7040 字符 ≈1828 token，占 11.7%）——根因是这些段落的 `section` 字段
# 继承的是前一个真章节名（"Materials and Methods"），既不是 References 也没有 heading 标记，
# 单靠 `_is_ref_section(section)` 拦不住。⇒ 增加"**首段文本形态**"判据 + 尾部截断。
_TAIL_NOISE_RE = re.compile(
    r"^\s*(references|bibliography|literature cited|works cited|"
    r"acknowledg(e)?ments?|conflict of interest|declarations? of (competing|conflicting) interest|"
    r"competing interests?|author contributions?|authors'? contributions?|credit authorship|"
    r"data availability|data statement|"
    r"supporting information(?!\s*(fig|figure|table|appendix|section|movie|note|scheme|"
    r"dataset|data set|ref)\b)|"
    r"supplementary (material|information|data)(?!\s*(fig|figure|table|appendix|section|movie)\b)|"
    r"associated content|additional information|author information|"
    r"ethics statement|funding|notes\b|this article references|orcid)",
    re.IGNORECASE)

# 尾部判定：结尾杂项大多出现在文档后半段；但 Wiley 等版式的 "Supporting Information"
# 声明可能排在 40% 位置（adma 实测 idx=43/113），故对**强模式**放宽到全篇，并加长度闸门
# （正文里的引用句 "Supporting Information Figure S1 shows …" 通常是长段落，不截）。
_TAIL_START_FRAC = 0.5
_TAIL_STRONG_RE = re.compile(
    r"^\s*(references|bibliography|literature cited|works cited|"
    r"acknowledg(e)?ments?|conflict of interest|declarations? of (competing|conflicting) interest|"
    r"competing interests?|author contributions?|authors'? contributions?|"
    r"data availability|"
    # "Supporting Information **Figure S1** shows …" 是正文交叉引用，不是结尾声明 → 负向前瞻排除
    r"supporting information(?!\s*(fig|figure|table|appendix|section|movie|note|scheme|"
    r"dataset|data set|ref)\b)|"
    r"supplementary (material|information|data)(?!\s*(fig|figure|table|appendix|section|movie)\b)|"
    r"associated content|author information|ethics statement|this article references)",
    re.IGNORECASE)
_TAIL_SHORT_CHARS = 200      # 位于前半段时，只有"短声明段"才认作结尾杂项


def _is_tail_noise(text: str, idx: int, total: int, section: str = "",
                   is_heading: bool = False) -> bool:
    """[局部] 该段是否"结尾杂项"（致谢/利益冲突/参考文献…，出现即截断其后全部段落）

    2026-09-19 修复：尾部声明的**标题段** text_en 带 markdown "## " 前缀
    （"## CRediT authorship contribution statement"），旧判据 `^\\s*(credit authorship|…)`
    匹配不到 → 标题段漏网，只有恰好命中的正文段才触发截断（snb 整篇没截、cej/ncomms
    漏掉声明标题）。这里先剥 "#+ " 前缀再匹配，并**同时匹配 section 字段**（节名干净，
    声明段落的 section 即 "Acknowledgements"/"Data availability" 等）。
    **标题段无歧义**：is_heading 命中即截断，不受"后半段/短段"位置门限约束
    （修 cej：CRediT 标题在 50% 线前一段、且 "credit authorship" 不在强模式表 → 漏截）。
    """
    t = re.sub(r"^#+\s*", "", (text or "")).strip()   # 剥 markdown 标题前缀
    sec = (section or "").strip()
    if not (_TAIL_NOISE_RE.match(t) or _TAIL_NOISE_RE.match(sec)):
        return False
    if is_heading:
        return True                       # 尾部声明标题段：无歧义，直接截断
    if total > 4 and idx >= total * _TAIL_START_FRAC:
        return True                       # 后半段：命中原样截断
    return len(t) <= _TAIL_SHORT_CHARS and bool(_TAIL_STRONG_RE.match(t) or _TAIL_STRONG_RE.match(sec))


def tail_cut_index(doc: PaperDoc) -> int:
    """[全局] 第一个"结尾杂项"段落的下标；无则返回段落总数。

    单点判据（供共享上下文与 L2 章节片段**共用**，避免两处口径漂移）。
    """
    paras = doc.paragraphs
    for i, p in enumerate(paras):
        if _is_tail_noise((p.text_en or "").strip(), i, len(paras),
                          getattr(p, "section", "") or "",
                          bool(getattr(p, "is_heading", False))):
            return i
    return len(paras)


def context_paragraphs(doc: PaperDoc) -> list:
    """[全局] 允许进入 AI 上下文的段落：尾部杂项之前 + 跳过 References 类章节。"""
    cut = tail_cut_index(doc)
    out = []
    for p in doc.paragraphs[:cut]:
        if _is_ref_section(p.section) or (p.is_heading and _is_ref_section(p.text_en or "")):
            continue
        out.append(p)
    return out


def _shift_math_labels(text: str, base: int) -> str:
    """把段内 [[MATHk]] 全局偏移到 base+k（跨段全局唯一编号）。"""
    if base <= 0:
        return text
    return re.sub(r"\[\[MATH(\d+)\]\]",
                  lambda m: "[[MATH%d]]" % (base + int(m.group(1))), text)


def paper_context_with_math(doc: PaperDoc) -> tuple[str, list[str]]:
    """构造共享全文块 + 全局公式表。

    返回 (block, math_list)：
    - block：标题 + 章节 + 干净正文 text_en（跳过 References），公式 [[MATHn]] **全局**编号，
      末尾附「公式对照表」；
    - math_list[n]：第 n 个全局公式的原文 token（split_text 序），供翻译 reassemble 逐字节回填。

    注释/确定性：逐段串行、只读 text_en、跳 References → 同 document.json 恒得相同 block。
    """
    from .translate.latextap import split_text  # 惰性导入：避免 context↔translate 循环

    math_list: list[str] = []
    lines: list[str] = [f"# {doc.title or ''}", ""]
    cur = ""
    # 单点判据：尾部杂项截断 + 跳 References（`context_paragraphs` 与 L2 章节片段共用）
    for p in context_paragraphs(doc):
        if p.is_heading:
            cur = p.text_en.strip() or cur
            lines.append(f"## {cur}")
            continue
        en = (p.text_en or "").strip()
        if not en:
            continue
        labeled, ml = split_text(en)
        base = len(math_list)
        if base:
            labeled = _shift_math_labels(labeled, base)
        math_list.extend(ml)
        marker = "(题注/Caption) " if p.is_caption else ""
        lines.append(f"{marker}[{p.para_id}] {labeled}")
    block = "\n".join(lines).strip()
    if math_list:
        table = ["", "## 公式对照表"]
        table += ["[[MATH%d]] = %s" % (i, tok) for i, tok in enumerate(math_list)]
        block += "\n" + "\n".join(table)
    return block, math_list


def paper_context(doc: PaperDoc) -> str:
    """共享全文块（纯文本，供提示词前缀）。与 ``paper_context_with_math`` 的 block 相同。"""
    return paper_context_with_math(doc)[0]


def shared_ctx(doc: PaperDoc) -> str:
    """共享前缀 = 系统/结构前缀 + 全文块（翻译与编译同用，字节一致）。"""
    return CTX_HEADER + paper_context(doc)


# ---------------------------------------------------------------- 共享前缀 / 任务 分界
# 2026-09-13 用户实测（智谱 GLM 自动前缀缓存**只对 `system` 消息内容生效**）：
#   · 单条 user 装前缀（本仓库旧形状）→ `cached_tokens=0`（1.5k/5k 前缀都 0）；
#   · `system` + `user` 两条 → 1.5k 前缀命中 512、5k 前缀命中 5120。
# DeepSeek 官方与硅基流动不挑形状（旧形状也有命中），智谱恒 0。⇒ 统一改成
# **共享全文前缀独立为 `system` 消息**（三家都能命中），任务指令留 `user`。
#
# 分界方式：构造点用 `with_task(shared, task)` 显式标界（不靠猜分隔符——任务文本里
# 可能出现任意空行/标题），发送层用 `split_task(prompt)` 拆成 (system, user)。
# `TASK_MARK` 只作**内部分界标记**，不含任何会被模型当内容的东西；命中时它留在
# system 串末尾（各请求同一篇文档的 system 逐字节稳定 ⇒ 不破坏前缀缓存）。
TASK_MARK = "\n\n<<<PAPERAGENT_TASK>>>\n\n"


def with_task(shared: str, task: str) -> str:
    """把共享前缀与任务部分合成一条 prompt，并用 `TASK_MARK` 标出分界。

    返回 ``shared + TASK_MARK + task``：**shared 部分逐字节不变**（翻译/编译/问答三路
    共享同一字节前缀）；任务文本原样保留在 marker 之后。
    """
    return (shared or "") + TASK_MARK + (task or "")


def split_task(prompt: str) -> tuple[str, str]:
    """按 `TASK_MARK` 把 prompt 拆成 ``(system, user)``。

    - 命中 marker：``(shared, task)`` → 发送层发 `[system, user]` 两条消息
      （智谱等供应商的前缀缓存只认 `system` 内容）；
    - **无 marker**：``("", prompt)`` —— 保持旧行为（单条 user 消息）。
    """
    text = prompt or ""
    idx = text.find(TASK_MARK)
    if idx < 0:
        return "", text
    return text[:idx], text[idx + len(TASK_MARK):]
