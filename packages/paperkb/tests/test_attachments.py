# -*- coding: utf-8 -*-
"""依附资料单测（P0-B step3，2026-09-12）。

锁定五条性质：
1. **资源根解析**——论文 RID 带 `doi-` 前缀，library 目录名是 `10.1002_adma…`；
   两者必须统一，否则附件落到平行目录（用户两处都找不到）。
2. 无父资源的资料落独立根 `attachments/<RID>/`。
3. **不覆盖用户资料**——同名重复导入自动加 `-2` 后缀。
4. **A5 索引约定**：`notes_fts.doi = 父 RID`、`filename = attachments/<kind>/<名>`；
   编译产物重索引（index_notes）**不得洗掉附件行**。
5. **A4 越权防护**：`..`/绝对路径/跨资源路径一律拒绝。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from paperkb import attachments as att
from paperkb.api import init_kb, kb_attachment_import, kb_attachment_read, kb_attachments
from paperkb.config import Roots
from paperkb.db import KBStore
from paperkb.doi import doi_to_dirname, make_rid
from paperkb.models import PaperMeta

DOI = "10.1002/adma.202407106"
RID = make_rid("paper", doi=DOI)          # doi-10.1002_adma.202407106
DIRNAME = doi_to_dirname(DOI)             # 10.1002_adma.202407106（现有目录名）


def _roots(tmp_path) -> Roots:
    return Roots(data_dir=tmp_path / "data", library_dir=tmp_path / "library",
                 kb_dir=tmp_path / "kb").ensure()


def _kb(tmp_path) -> Roots:
    r = _roots(tmp_path)
    init_kb(r)
    return r


# ---------------------------------------------------------------- 1. 资源根解析

def test_paper_rid_resolves_to_existing_dirname(tmp_path):
    roots = _kb(tmp_path)
    d = roots.library_dir / DIRNAME
    d.mkdir(parents=True)
    assert att.resource_dir(roots, RID) == d, \
        "论文 RID 必须解析到 doi_to_dirname 的既有目录（否则附件落平行目录）"
    assert att.resource_dir(roots, DIRNAME) == d, "按目录名给的键也要命中"


def test_book_rid_is_its_own_dir(tmp_path):
    roots = _kb(tmp_path)
    rid = make_rid("book", isbn="978-3-527-34567-8")
    d = roots.library_dir / rid
    d.mkdir(parents=True)
    assert att.resource_dir(roots, rid) == d


# ---------------------------------------------------------------- 2. 落位

def test_parented_attachment_lands_under_parent(tmp_path):
    roots = _kb(tmp_path)
    (roots.library_dir / DIRNAME).mkdir(parents=True)
    out = kb_attachment_import(RID, "si", "support.md", b"# SI\ncontent", index=True)
    assert out["ok"] and out["parented"] is True
    p = roots.library_dir / DIRNAME / "attachments" / "si" / "support.md"
    assert p.read_text(encoding="utf-8") == "# SI\ncontent"
    assert out["path"] == "si/support.md"


def test_parentless_attachment_goes_to_standalone_root(tmp_path):
    roots = _kb(tmp_path)
    rid = "nd-3f9a1c22b7d4"
    out = kb_attachment_import(rid, "data", "raw.csv", b"a,b\n1,2", index=True)
    assert out["ok"] and out["parented"] is False
    p = tmp_path / "attachments" / rid / "data" / "raw.csv"
    assert p.exists(), "无父资源资料落独立根 attachments/<RID>/"


def test_same_name_does_not_overwrite(tmp_path):
    roots = _kb(tmp_path)
    (roots.library_dir / DIRNAME).mkdir(parents=True)
    a = kb_attachment_import(RID, "review", "comments.md", b"first")
    b = kb_attachment_import(RID, "review", "comments.md", b"second")
    assert a["path"] == "review/comments.md"
    assert b["path"] == "review/comments-2.md", "用户已有资料不可被覆盖"
    assert (roots.library_dir / DIRNAME / "attachments" / "review" /
            "comments.md").read_bytes() == b"first"


def test_unknown_kind_falls_back_to_data(tmp_path):
    roots = _kb(tmp_path)
    (roots.library_dir / DIRNAME).mkdir(parents=True)
    out = kb_attachment_import(RID, "weird", "x.txt", b"x")
    assert out["kind"] == "data"


# ---------------------------------------------------------------- 3. 索引（A5）

def test_attachment_indexed_under_parent_rid(tmp_path):
    roots = _kb(tmp_path)
    (roots.library_dir / DIRNAME).mkdir(parents=True)
    kb_attachment_import(RID, "si", "support.md", b"unique-token-zzz", index=True)
    s = KBStore(roots)
    rows = s.search_notes("unique-token-zzz")
    assert rows, "附件文本必须能被 kb_recall（notes_fts）召回"
    assert rows[0]["doi"] == RID, "索引键 = 父 RID"
    assert rows[0]["file"] == "attachments/si/support.md", "file 自带附件相对路径"
    assert s.indexed_attachment_paths(RID) == {"attachments/si/support.md"}


def test_recompile_does_not_wipe_attachment_rows(tmp_path):
    """编译产物重索引（index_notes 先删后插）不得连带删掉用户附件索引。"""
    roots = _kb(tmp_path)
    (roots.library_dir / DIRNAME).mkdir(parents=True)
    kb_attachment_import(RID, "si", "support.md", b"si-unique-token", index=True)
    s = KBStore(roots)
    s.index_notes(RID, [{"filename": "_note.md", "content": "note body"}])
    assert s.search_notes("si-unique-token"), "附件索引被编译重索引洗掉了（回归）"
    assert s.indexed_attachment_paths(RID) == {"attachments/si/support.md"}


def test_reindex_from_kb_restores_attachments(tmp_path):
    """全量重建（reindex_from_kb）后附件索引仍在（旧实现整表重扫会丢）。"""
    roots = _kb(tmp_path)
    (roots.library_dir / DIRNAME).mkdir(parents=True)
    kb_attachment_import(RID, "review", "comments.md", b"review-token-abc", index=True)
    s = KBStore(roots)
    out = s.reindex_from_kb()
    assert out.get("attachments_indexed", 0) >= 1
    assert s.search_notes("review-token-abc"), "全量重建后附件仍可召回"


def test_reindex_prunes_rows_for_deleted_files(tmp_path):
    """用户手删附件文件后，索引不得留孤儿行（否则 agent 召回却读不回）。"""
    roots = _kb(tmp_path)
    (roots.library_dir / DIRNAME).mkdir(parents=True)
    kb_attachment_import(RID, "si", "gone.md", b"deleted-token-xyz", index=True)
    s = KBStore(roots)
    assert s.search_notes("deleted-token-xyz"), "先确认已索引"
    (roots.library_dir / DIRNAME / "attachments" / "si" / "gone.md").unlink()
    s.reindex_attachments()
    assert not s.search_notes("deleted-token-xyz"), "文件已删，索引必须跟着清"
    assert s.indexed_attachment_paths(RID) == set()


def test_attachment_listing_reports_index_state(tmp_path):
    roots = _kb(tmp_path)
    (roots.library_dir / DIRNAME).mkdir(parents=True)
    kb_attachment_import(RID, "si", "s1.md", b"one", index=True)
    kb_attachment_import(RID, "review", "r1.md", b"two", index=True)
    out = kb_attachments(RID)
    assert out["count"] == 2
    assert set(out["by_kind"]) == {"si", "review"}
    assert all(f["indexed"] for f in out["files"])
    assert out["kind"] == "paper"


# ---------------------------------------------------------------- 4. 越权防护（A4）

def test_read_attachment_whitelist(tmp_path):
    roots = _kb(tmp_path)
    (roots.library_dir / DIRNAME).mkdir(parents=True)
    kb_attachment_import(RID, "si", "support.md", b"hello si", index=True)
    ok = kb_attachment_read(RID, "si/support.md")
    assert ok["ok"] and "hello si" in ok["text"]
    # 资源目录里**真实存在**的解析产物也不能被当附件读（白名单只放行 attachments/）
    (roots.library_dir / DIRNAME / "en.md").write_text("body", encoding="utf-8")
    assert kb_attachment_read(RID, "en.md")["ok"] is False
    assert kb_attachment_read(RID, "../../en.md")["ok"] is False
    # 越权：穿越 / 绝对路径 / 不存在的资源
    assert kb_attachment_read(RID, "../../etc/passwd")["ok"] is False
    assert kb_attachment_read(RID, "/etc/passwd")["ok"] is False
    assert kb_attachment_read(RID, "C:/Windows/win.ini")["ok"] is False
    assert kb_attachment_read("nd-nope", "si/x.md")["ok"] is False
    # 索引里的库内路径写法（`attachments/si/x.md`）也要能读回（kb_recall → 读内容链路）
    assert att.resolve_attachment(roots, RID, "attachments/si/support.md") is not None


def test_read_attachment_rejects_other_resource(tmp_path):
    """另一篇文献的附件不能被本资源路径读到（共享父目录时防串档）。"""
    roots = _kb(tmp_path)
    other = make_rid("paper", doi="10.1002/other.1")
    (roots.library_dir / DIRNAME).mkdir(parents=True)
    (roots.library_dir / doi_to_dirname("10.1002/other.1")).mkdir(parents=True)
    kb_attachment_import(other, "si", "secret.md", b"other paper secret")
    assert kb_attachment_read(RID, "si/secret.md")["ok"] is False


def test_read_attachment_binary_reports_not_text(tmp_path):
    roots = _kb(tmp_path)
    (roots.library_dir / DIRNAME).mkdir(parents=True)
    kb_attachment_import(RID, "data", "blob.bin", b"\x00\x01\x02binary", index=False)
    out = kb_attachment_read(RID, "data/blob.bin")
    assert out["ok"] is False and "非文本" in out["error"]


# ------------------------------------------------- 4b. 附件全文镜像（fulltext_fts）

CJK_SENTENCE = "这句中文长句用于附件召回验证服务"


def test_attachment_cjk_mirrored_and_recalled_by_default(tmp_path):
    """附件文本镜像进 fulltext_fts（复合键），且**默认**可被 api.recall 召回。"""
    from paperkb.api import recall as kb_recall_api

    roots = _kb(tmp_path)
    (roots.library_dir / DIRNAME).mkdir(parents=True)
    kb_attachment_import(RID, "si", "cjk.md",
                         CJK_SENTENCE.encode("utf-8"), index=True)
    s = KBStore(roots)
    keys = [r["doi"] for r in s.search_fulltext(CJK_SENTENCE)]
    assert f"{RID}::attachments/si/cjk.md" in keys, \
        f"附件必须镜像进 fulltext_fts 且键为复合键，实际={keys}"
    hits = kb_recall_api(CJK_SENTENCE)           # 不传 include_fulltext
    mine = [h for h in hits if h["file"].startswith("attachments/")]
    assert mine, f"附件默认应可召回，实际={hits}"
    assert mine[0]["file"] == "attachments/si/cjk.md", "file 必须是附件相对路径"
    # api.recall 的 _enrich_recall 把 doi 归一成裸 DOI，rid 保留原键：
    # 复合键必须已切开（rid 里不能残留 `::`）。
    assert mine[0]["rid"] == RID, f"键必须还原成父资源键，实际={mine[0]['rid']}"
    assert KBStore.ATTACH_FT_SEP not in mine[0]["rid"], "复合键未切开"
    # 让 fulltext 分支**独立**被证明不是死代码：抹掉 notes 行后仍应召回。
    with s._conn() as conn:  # noqa: SLF001 - 测试内制造"只有全文索引有"的状态
        conn.execute("DELETE FROM notes_fts WHERE doi=? AND filename=?",
                     (RID, "attachments/si/cjk.md"))
    only_ft = [h for h in kb_recall_api(CJK_SENTENCE)
               if h["file"] == "attachments/si/cjk.md"]
    assert only_ft and only_ft[0]["source"] == "fulltext", \
        f"notes 行缺失时全文镜像必须兜底，实际={only_ft}"


def test_prune_clears_fulltext_orphan_too(tmp_path):
    """删附件文件后 reindex_attachments 既清 notes_fts 也清 fulltext_fts 孤儿行。"""
    roots = _kb(tmp_path)
    (roots.library_dir / DIRNAME).mkdir(parents=True)
    kb_attachment_import(RID, "si", "gone2.md",
                         CJK_SENTENCE.encode("utf-8"), index=True)
    s = KBStore(roots)
    assert s.search_fulltext(CJK_SENTENCE), "先确认全文镜像已写入"
    (roots.library_dir / DIRNAME / "attachments" / "si" / "gone2.md").unlink()
    s.reindex_attachments()
    assert not s.search_notes(CJK_SENTENCE), "notes_fts 孤儿行未清"
    left = [r["doi"] for r in s.search_fulltext(CJK_SENTENCE)]
    assert not [k for k in left if k.startswith(f"{RID}{KBStore.ATTACH_FT_SEP}")], \
        f"fulltext_fts 孤儿行未清：{left}"


def test_body_and_attachment_fulltext_do_not_wipe_each_other(tmp_path):
    """正文 index_fulltext（纯 doi 键整删）与附件复合键同一 RID 共存、互不误删。"""
    roots = _kb(tmp_path)
    (roots.library_dir / DIRNAME).mkdir(parents=True)
    s = KBStore(roots)
    s.index_fulltext(RID, "bodytokenalpha 正文全文")
    kb_attachment_import(RID, "data", "d.md",
                         "attachtokenbeta 数据附件".encode("utf-8"), index=True)
    s.index_fulltext(RID, "bodytokengamma 正文全文改版")   # 正文先删后插
    ft = {r["doi"] for r in s.search_fulltext("attachtokenbeta")}
    assert f"{RID}::attachments/data/d.md" in ft, f"正文整删洗掉了附件全文行：{ft}"
    assert not s.search_fulltext("bodytokenalpha"), "正文旧行应被整删（语义不变）"
    assert [r["doi"] for r in s.search_fulltext("bodytokengamma")] == [RID], \
        "正文全文行键必须仍是纯 doi"
    assert s.search_notes("attachtokenbeta"), "附件 notes 行也不受影响"


# ---------------------------------------------------------------- 5. kind 字段

def test_kind_from_explicit_meta_field(tmp_path):
    from paperkb.api import _kind_of_rid
    roots = _kb(tmp_path)
    s = KBStore(roots)
    rid = s.upsert_meta(PaperMeta(rid="doi-10.1002_x.9", doi="10.1002/x.9", kind="thesis"))
    assert _kind_of_rid(rid, s) == "thesis"
    assert _kind_of_rid("book__isbn-1", s) == "book"
    assert _kind_of_rid("nd-abc123", s) == "paper"
