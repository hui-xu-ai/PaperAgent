# -*- coding: utf-8 -*-
"""身份与命名工具（DOI / 统一资源键 RID）。

关联键约定：papers_meta.doi 与 library/kb 目录名（DOI 规范化）配对。

2026-09-11（P0-B 统一身份）三处修正：
1. `doi_to_dirname` 改为**单射**——旧实现把 "/" ":" 与非法字符统一折成 "_"，
   使 10.1002/a_b == 10.1002/a/b == 10.1002/a:b（不同文献落到同一目录 = 串档）。
2. `dirname_to_doi` 只认**真实 DOI 形态**（10.xxxx/…）——旧实现会把
   JMADE-D-26-04277_R1_reviewer 这类文件名反推成"DOI"，制造幽灵文献。
3. 新增 `make_rid`：内部统一资源键（DOI>ISBN>CNKI>CSTR>arXiv>报告号>内容指纹），
   供无 DOI 的中文文献/学位论文/书籍/章节/支撑信息/审稿意见共用一套身份。
"""
from __future__ import annotations

import hashlib
import re

_INVALID_DIR_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# 允许 "~"（":" 的转义占位）保留在目录名里
_NON_SLUG = re.compile(r"[^\w.~\-]")

# DOI 形态（唯一判定入口：拒绝"文件名当 DOI"）
_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")

# DOI 中"可安全进目录名"的字符集；含其他字符时加消歧后缀保证单射
_UNSAFE_FOR_DIRNAME = re.compile(r"[^A-Za-z0-9._/:\-]")

# 消歧后缀（--<md5前6>）：仅在原始 DOI 含不安全字符时追加
_DISAMBIG_SUFFIX = re.compile(r"--[0-9a-f]{6}$")

# 目录名反推：单个 "_" = 原 "/"，"__" = 原字面 "_"
_SINGLE_UNDERSCORE = re.compile(r"(?<!_)_(?!_)")


def normalize_doi(raw: str | None) -> str:
    """规范化 DOI：strip + 去首尾空白；空/None → ""。保留大小写（WOS 大写原样）。"""
    if not raw:
        return ""
    return raw.strip().strip("\"'")


def is_doi(value: str | None) -> bool:
    """是否为合法 DOI 形态（10.NNNN/…）。全库唯一判据，用于拒绝幽灵身份。"""
    return bool(_DOI_RE.match((value or "").strip()))


def doi_to_dirname(doi: str) -> str:
    """DOI → 目录名（**单射**；与 kb_service.paper_dir 语义一致）。

    规则：
      1) 含 "/" 或 ":" 时按可逆映射转义：字面 "_" → "__"、"/" → "_"、":" → "~"；
         **不含分隔符的值（`_global` 等保留键、md5 目录名）原样保留**——否则
         `_global`/`_qa`/`_reports` 会被改成 `__global` 而破坏现有数据与 QA 卡片路径；
      2) 其余非法字符 → "-"；
      3) 若原值含不安全字符（空格/括号等）→ 追加 "--<md5前6>" 消歧后缀，
         保证**不同 DOI 永不得到同一目录名**（幂等、确定性）。
    向后兼容：不含上述特殊字符的常见 DOI 目录名与旧实现完全一致（实测现有
    10.1002/adma…、10.1016/j.cej…、10.1038/ncomms8258、10.1063/1.5004573 均不变）。
    """
    raw = (doi or "").strip()
    if "/" in raw or ":" in raw:
        base = raw.replace("_", "__").replace("/", "_").replace(":", "~")
    else:
        base = raw
    base = _INVALID_DIR_CHARS.sub("-", base)
    base = _NON_SLUG.sub("-", base)
    if raw and _UNSAFE_FOR_DIRNAME.search(raw):
        base = base[:110] + "--" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:6]
    return base[:120] or "paper"


def extract_doi_from_text(text: str) -> str:
    """从文本中提取 DOI（文件名/元数据兜底）。形如 10.xxxx/xxx，剥离行尾标点。"""
    if not text:
        return ""
    m = re.search(r"\b10\.\d{4,9}/[^\s,;()]+", text)
    if not m:
        return ""
    doi = m.group(0).rstrip(".,;:()!?\"'")
    return normalize_doi(doi)


def dirname_to_doi(dirname: str) -> str:
    """目录名/文件名（doi_to_dirname 产物）反推 DOI；非 DOI 形态一律返回 ""。

    与 doi_to_dirname 严格互逆：剥消歧后缀 → "~"→":" → 单个 "_"→"/" → "__"→"_"。
    最后用 is_doi 兜底（**修复幽灵文献**：旧实现把
    JMADE-D-26-04277_R1_reviewer 反推成"DOI"，导致它进入 papers_meta/kb 索引）。

    **带消歧后缀（--xxxxxx）的目录名一律返回 ""**：那类 DOI 含空格/括号等被净化掉的
    字符，从名字反推不出原值；其真实 DOI 由 doi_md5_map 表（dir_to_key 会查）解析。
    """
    if not dirname or dirname.startswith("_"):
        return ""
    if _DISAMBIG_SUFFIX.search(dirname):
        return ""   # 不可逆：交由 doi_md5_map 解析
    doi = dirname.replace("~", ":")
    doi = _SINGLE_UNDERSCORE.sub("/", doi)
    doi = doi.replace("__", "_")
    doi = normalize_doi(doi)
    return doi if is_doi(doi) else ""


_MD5_DIR_RE = re.compile(r"[0-9a-f]{32}", re.IGNORECASE)


def is_md5_dir(name: str) -> bool:
    """是否为 md5 目录名（32 位 hex；T5 起无 DOI 文献以 md5(PDF 全文) 命名目录）。"""
    return bool(name) and _MD5_DIR_RE.fullmatch(name) is not None


def dir_to_key(dirname: str, store=None) -> tuple[str, str]:
    """目录名 → (标识, 类型)：kb/library 目录统一识别入口（T6 DOI↔md5 双映射）。

    - DOI 目录（dirname_to_doi 成功）→ (doi, "doi")
    - md5 目录（32 位 hex）→ 经 store.get_doi_md5_map 反查 DOI（可空）：
      返回 (DOI 或 md5, "md5")——map 命中用 DOI 作标识，未命中用 md5 本身
    - 其他目录 → 先查 doi_md5_map（覆盖"含特殊字符、目录名不可逆"的 DOI），
      命中返回 (doi, "doi")；未命中返回 **(目录名, "dir")**：2026-09-11 起保留
      （旧实现返回 ("", "")，会让无 DOI 且未按 md5 命名的历史目录在列表中"消失"，
      用户无法管理）。调用方需自行判断是否 DOI 形态（`is_doi(key)`）。
    store：可选，含 get_doi_md5_map(key) 的 KBStore（目录名 ↔ DOI 反查）。
    """
    doi = dirname_to_doi(dirname)
    if doi:
        return doi, "doi"
    if is_md5_dir(dirname):
        mapped = ""
        if store is not None:
            try:
                row = store.get_doi_md5_map(dirname)
                mapped = (row or {}).get("doi") or ""
            except Exception:  # noqa: BLE001 - 反查失败按未映射处理
                mapped = ""
        return (mapped or dirname), "md5"
    if store is not None and dirname and not dirname.startswith((".", "_")):
        try:
            row = store.get_doi_md5_map(dirname)
            mapped = (row or {}).get("doi") or ""
        except Exception:  # noqa: BLE001
            mapped = ""
        if mapped and is_doi(mapped):
            return mapped, "doi"
    if dirname and not dirname.startswith((".", "_")):
        return dirname, "dir"
    return "", ""


# ---------------------------------------------------------------- 统一资源键（RID，P0-B）

# 类型前缀：子资源（支撑信息/审稿意见/章节等）挂父 RID，物理上与正文隔离
KIND_PREFIX = {
    "paper": "", "si": "si__", "review": "review__", "note": "note__",
    "thesis": "thesis__", "book": "book__", "chapter": "chapter__",
    "patent": "patent__", "standard": "std__",
}


def _slug(value: str) -> str:
    """外部标识 → 目录安全片段。"""
    return re.sub(r"[^A-Za-z0-9._\-]+", "-", (value or "").strip()).strip("-")


def make_rid(kind: str = "paper", *, doi: str = "", isbn: str = "", cnki: str = "",
             cstr: str = "", arxiv: str = "", report_no: str = "",
             fingerprint: str = "", parent_rid: str = "") -> str:
    """生成统一资源键 RID（内部主键；外部标识只是别名）。

    优先级：DOI > ISBN > CNKI > CSTR > arXiv > 报告号 > 内容指纹（md5 等）。
    什么都没有时用内容指纹 `nd-<指纹前12>`（同一文件重复导入 → 同一 RID，天然去重）。

    子资源：kind 命中 KIND_PREFIX 且给了 parent_rid → `<前缀><父RID>`
    （例：`si__doi-10.1002-adma.202407106`、`chapter__isbn-9783527345678-ch03`）。

    返回值为**目录安全**字符串（可直接用作 library/attachments 下的目录名）。
    """
    k = (kind or "paper").strip().lower()
    prefix = KIND_PREFIX.get(k, "")
    if prefix and parent_rid:
        return (prefix + parent_rid)[:150]
    body = ""
    d = normalize_doi(doi)
    if is_doi(d):
        body = "doi-" + doi_to_dirname(d)
    elif isbn:
        body = "isbn-" + _slug(isbn)
    elif cnki:
        body = "cnki-" + _slug(cnki)
    elif cstr:
        body = "cstr-" + _slug(cstr)
    elif arxiv:
        body = "arxiv-" + _slug(arxiv)
    elif report_no:
        body = "rep-" + _slug(report_no)
    elif fingerprint:
        body = "nd-" + _slug(fingerprint).lower()[:12]
    else:
        body = "nd-untitled"
    return (prefix + body)[:150].strip("-_") or "nd-untitled"
