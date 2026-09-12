# -*- coding: utf-8 -*-
"""M4 单测：检索（notes_fts/多路召回/问答编排/rebuild）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperkb import api
from paperkb.config import Roots
from paperkb.llm import FakeLLM


@pytest.fixture()
def env(tmp_path: Path):
    roots = Roots(data_dir=tmp_path / "data",
                  library_dir=tmp_path / "library",
                  kb_dir=tmp_path / "kb").ensure()
    api.init_kb(roots)
    api.configure_llm(FakeLLM())
    yield roots
    api._store = None
    api._journals = None
    api._compiler = None
    api._settings = None
    from paperkb import llm
    llm._client = None  # noqa: SLF001


def _mk_paper(roots: Roots, doi: str, title: str, body: str) -> None:
    """造一篇：meta + kb/<DOI>/ 编译产物（_note.md）+ document.json。"""
    from paperkb.models import PaperMeta
    from paperkb.doi import doi_to_dirname

    api._need_store().upsert_meta(  # noqa: SLF001
        PaperMeta(doi=doi, title=title, journal="J", year="2024",
                  abstract=body[:200], source_file="t.bib"))
    d = roots.kb_dir / doi_to_dirname(doi)
    d.mkdir(parents=True, exist_ok=True)
    (d / "_note.md").write_text(
        f"# 笔记：{title}\n\n一句话贡献：{body}\n\n概念标签 #soft-robot\n",
        encoding="utf-8")
    (d / "_details.md").write_text(f"# 详细\n- {body[:100]}\n", encoding="utf-8")
    (d / "document.json").write_text(json.dumps({
        "metadata": {"doi": doi, "title": title},
        "paragraphs": [{"para_id": "P001", "section": "S", "text_en": body}],
    }), encoding="utf-8")


def test_index_and_search_notes(env, tmp_path):
    roots = env
    _mk_paper(roots, "10.1000/a.1", "Ionic polymer actuators", "IPMC actuators bend under voltage")
    _mk_paper(roots, "10.1000/b.2", "Nafion membranes", "Nafion membrane transport properties")
    r = api.rebuild_fts()
    assert r["notes_indexed"] == 2

    hits = api.recall("IPMC bending")
    assert hits and hits[0]["doi"] == "10.1000/a.1"
    assert hits[0]["source"] == "notes"
    assert hits[0]["snippet"]

    hits2 = api.recall("Nafion membrane")
    assert hits2 and hits2[0]["doi"] == "10.1000/b.2"


def test_chinese_query_trigram_hit(env, tmp_path):
    """trigram：中文短语（≥3 字符连续子串）命中编译产物（2026-08-27 修复，
    unicode61 时中文整句不成 token 无法命中）。"""
    roots = env
    _mk_paper(roots, "10.1000/a.1", "Ionic polymer actuators",
              "离子液体增强的Nafion传感器，浸泡温度与咪唑鎓类型影响性能")
    api.rebuild_fts()
    hits = api.recall("浸泡温度")
    assert hits and hits[0]["doi"] == "10.1000/a.1", hits


def test_chinese_query_like_fallback(env, tmp_path):
    """CJK LIKE 兜底：短中文词（<3 字符，trigram 无法索引）按分块子串匹配。"""
    roots = env
    _mk_paper(roots, "10.1000/a.1", "Ionic polymer actuators",
              "离子液体增强的Nafion传感器，浸泡温度与咪唑鎓类型影响性能")
    api.rebuild_fts()
    # "浸泡 温度" 两个 2 字词：trigram 短语 <3 字符 miss → LIKE AND 兜底命中
    hits = api.recall("浸泡 温度")
    assert hits and hits[0]["doi"] == "10.1000/a.1", hits


def test_recall_meta_fallback(env, tmp_path):
    """笔记无命中时 meta_fts 元数据召回兜底。"""
    roots = env
    _mk_paper(roots, "10.1000/a.1", "Soft robotics review", "review content")
    # 删掉 notes 索引（模拟笔记缺失）
    api._need_store()._conn  # noqa: SLF001
    import sqlite3
    conn = sqlite3.connect(roots.main_db)
    conn.execute("DELETE FROM notes_fts").connection.commit() if False else None
    conn.close()
    # 直接搜 meta 字段内容（abstract）
    hits = api.recall("Soft robotics")
    assert hits and hits[0]["doi"] == "10.1000/a.1"


def test_answer_orchestration(env, tmp_path):
    """问答编排：召回 → 上下文（预算）→ LLM 回答。"""
    roots = env
    _mk_paper(roots, "10.1000/a.1", "Ionic polymer actuators", "IPMC bend via ion migration")
    api.rebuild_fts()
    from paperkb import llm as llm_mod
    fake: FakeLLM = llm_mod.get_llm()
    fake._responses.append("IPMC 通过离子迁移驱动弯曲（[[10.1000/a.1]]）。")
    r = api.ask("IPMC 怎么驱动？")
    assert r["answer"].startswith("IPMC")
    assert r["items"] and r["items"][0]["doi"] == "10.1000/a.1"
    assert r["context_chars"] > 0
    assert fake._calls[0][0] == "ask"  # context 分组
    assert "知识库片段" in fake._calls[0][1]


def test_answer_no_hit(env, tmp_path):
    r = api.ask("完全不相关的问题 zzzz")
    assert "未检索到" in r["answer"]


def test_recall_paper_single_doi(env, tmp_path):
    """Q5 阶段2：recall_paper 只召回指定 DOI 的编译笔记；无编译产物回空。"""
    roots = env
    _mk_paper(roots, "10.1000/a.1", "Ionic polymer actuators",
              "IPMC actuators bend under voltage")
    _mk_paper(roots, "10.1000/b.2", "Nafion membranes",
              "Nafion membrane transport properties")
    api.rebuild_fts()

    hits = api.recall_paper("10.1000/a.1", "IPMC bending")
    assert hits and all(it["doi"] == "10.1000/a.1" for it in hits), hits
    assert hits[0]["file"] in ("_note.md", "_details.md")
    assert hits[0]["snippet"]

    # 该篇无命中词 → 空
    assert api.recall_paper("10.1000/a.1", "Nafion transport") == []

    # 未编译篇（仅 b.2 有笔记，c.3 无）→ 空
    _mk_paper(roots, "10.1000/c.3", "Graphene", "graphene properties")
    from paperkb.db import KBStore
    st = KBStore(roots)
    st._conn  # noqa: SLF001
    import sqlite3
    conn = sqlite3.connect(roots.main_db)
    conn.execute("DELETE FROM notes_fts WHERE doi='10.1000/c.3'").connection  # noqa
    conn.commit(); conn.close()
    assert api.recall_paper("10.1000/c.3", "graphene") == []


def test_paper_compiled(env, tmp_path):
    """paper_compiled 返回该篇编译产物文件名列表；未编译空。"""
    roots = env
    _mk_paper(roots, "10.1000/a.1", "Ionic polymer actuators", "IPMC bend")
    _mk_paper(roots, "10.1000/c.3", "Graphene", "graphene")
    api.rebuild_fts()
    from paperkb.db import KBStore
    st = KBStore(roots)
    st._conn  # noqa: SLF001
    import sqlite3
    conn = sqlite3.connect(roots.main_db)
    conn.execute("DELETE FROM notes_fts WHERE doi='10.1000/c.3'").connection  # noqa
    conn.commit(); conn.close()
    files = api.paper_compiled("10.1000/a.1")
    assert set(files) == {"_note.md", "_details.md"}
    assert api.paper_compiled("10.1000/c.3") == []


def test_summary_md_not_retrieval_source(env, tmp_path):
    """D16：检索源为 L1 编译笔记 _note.md；存量 summary.md 不再作为检索来源。"""
    from paperkb.doi import doi_to_dirname

    roots = env
    _mk_paper(roots, "10.1000/a.1", "Ionic polymer actuators", "IPMC bend via ion migration")
    d = roots.kb_dir / doi_to_dirname("10.1000/a.1")
    # 旧存量 summary.md（D16 不再生成、不入索引；用户自行清空重建）
    (d / "summary.md").write_text("# 总结\nIPMC bend\n", encoding="utf-8")
    api.rebuild_fts()
    files = api.paper_compiled("10.1000/a.1")
    assert "summary.md" not in files
    assert "_note.md" in files          # 以 _note.md 为检索源
    hits = api.recall("IPMC bend")
    assert hits
    assert all(it["file"] != "summary.md" for it in hits)


def test_regenerate_index(env, tmp_path):
    """P3：regenerate_index 扫描 kb 重建 _index.md（含全部文献 + 编译状态）。"""
    roots = env
    _mk_paper(roots, "10.1000/a.1", "Ionic polymer actuators", "IPMC bend")
    _mk_paper(roots, "10.1000/b.2", "Nafion membranes", "transport properties")
    r = api.regenerate_index()
    assert r["papers"] == 2
    content = (roots.kb_dir / "_index.md").read_text(encoding="utf-8")
    assert "10.1000/a.1" in content and "10.1000/b.2" in content
    assert "| L2 |" in content  # _mk_paper 建 _note+_details → 编译状态 L2


def test_regenerate_index_md5_dir(env, tmp_path):
    """T6：md5 目录（无 DOI 文献，T5 命名）也能进索引；map 关联 DOI 后索引显示 DOI。"""
    roots = env
    md5 = "a" * 32  # 32 位 hex 目录名（无 DOI 文献）
    d = roots.kb_dir / md5
    d.mkdir(parents=True, exist_ok=True)
    (d / "_note.md").write_text("# 笔记\n\nmd5 无 DOI 文献\n", encoding="utf-8")

    # 未映射：索引识别 md5 目录，DOI 列显示 md5
    r = api.regenerate_index()
    assert r["papers"] == 1, "md5 目录应计入索引"
    content = (roots.kb_dir / "_index.md").read_text(encoding="utf-8")
    assert md5 in content

    # 登记映射（解析完成后 backend 写入）→ 索引关联真实 DOI
    api.set_doi_md5_map(md5, doi="10.1000/md5p.1", pdf_md5=md5, paper_id=1)
    r2 = api.regenerate_index()
    assert r2["papers"] == 1
    content2 = (roots.kb_dir / "_index.md").read_text(encoding="utf-8")
    assert "10.1000/md5p.1" in content2, "map 关联后索引应显示 DOI"
    assert api.get_doi_md5_map(md5)["doi"] == "10.1000/md5p.1"
    assert api.list_doi_md5_map()[0]["paper_id"] == 1

    # stats 计入 md5 目录
    st = api.stats()
    assert st["scale"]["kb_papers"] == 1
    assert any(row["dir"] == md5 for row in st["kb_papers_detail"])

    # 有 meta 时关联后标题/期刊用 bib 权威元数据
    from paperkb.models import PaperMeta
    api._need_store().upsert_meta(  # noqa: SLF001
        PaperMeta(doi="10.1000/md5p.1", title="MD5 论文", journal="J", year="2025",
                  abstract="x", source_file="t.bib"))
    r3 = api.regenerate_index()
    content3 = (roots.kb_dir / "_index.md").read_text(encoding="utf-8")
    assert "MD5 论文" in content3


def test_compile_backfill(env, tmp_path):
    """P3：compile_backfill 为未完成 L1 的文献入队，跳过已 done 的。"""
    roots = env
    _mk_paper(roots, "10.1000/a.1", "Ionic polymer actuators", "IPMC bend")
    _mk_paper(roots, "10.1000/b.2", "Nafion membranes", "transport properties")
    api._need_store().upsert_job("10.1000/a.1", "L1", status="done")  # noqa: SLF001
    r = api.compile_backfill()
    assert "10.1000/b.2" in r["queued"]
    assert "10.1000/a.1" in r["skipped_done"]
    assert r["missing_document"] == []


def test_stats_overview(env, tmp_path):
    """P4：stats 聚合规模/编译完成度/缺失清单。"""
    roots = env
    _mk_paper(roots, "10.1000/a.1", "Ionic polymer actuators", "IPMC bend")  # note+details
    s = api.stats()
    assert s["scale"]["kb_papers"] == 1
    assert s["scale"]["meta_count"] >= 1
    assert s["compile"]["l1_done"] == 1
    assert s["compile"]["l2_done"] == 1
    assert s["compile"]["l3_done"] == 0
    assert s["missing"]["uncompiled"] == []


def test_reindex_includes_qa_cards(env, tmp_path):
    """O5：reindex_from_kb 需把 QA 卡片（文献 <DOI>/cards 与全局 _global/cards）纳入
    notes_fts，全库重建后仍可检索（否则 QA 从索引消失）。"""
    roots = env
    _mk_paper(roots, "10.1000/a.1", "Ionic polymer actuators", "IPMC actuators bend under voltage")
    rp = api.save_qa("What drives IPMC?", "Ionic migration drives IPMC bending.",
                     doi="10.1000/a.1", scope="paper")
    rg = api.save_qa("Global QA", "Ionic migration actuates global knowledge.",
                     doi="", scope="global")
    assert rp["ok"] and rg["ok"]
    # 模拟全库重建（rebuild_fts → reindex_from_kb；init_schema 升级 trigram 也走此路径）
    r = api.rebuild_fts()
    assert r["notes_indexed"] >= 2, r
    hits = api.recall("Ionic migration")
    assert any(h["doi"] == "_global" for h in hits), f"_global 卡丢失: {hits}"
    assert any(h["doi"] == "10.1000/a.1" for h in hits), f"该篇 QA 卡丢失: {hits}"


def test_namespace_not_mixed_after_reindex(env, tmp_path):
    """O5：paper 与 global 命名空间互不串。recall_paper(doi) 只回该篇（含其 QA 卡，
    不含 _global）；recall() 全局含 _global。"""
    roots = env
    _mk_paper(roots, "10.1000/a.1", "Ionic polymer actuators", "IPMC actuators bend under voltage")
    _mk_paper(roots, "10.1000/b.2", "Nafion membranes", "Nafion membrane transport properties")
    api.save_qa("Paper QA", "Ionic migration drives IPMC bending.",
                doi="10.1000/a.1", scope="paper")
    api.save_qa("Global QA", "Ionic migration actuates global knowledge.",
                doi="", scope="global")
    api.rebuild_fts()
    # recall_paper(a.1) 只回单篇（含其 QA 卡片），不含 _global 与 b.2
    pa = api.recall_paper("10.1000/a.1", "Ionic migration")
    assert pa, f"应命中该篇 QA 卡: {pa}"
    assert all(it["doi"] == "10.1000/a.1" for it in pa), f"跨命名空间污染: {pa}"
    # _global 不进单篇召回
    assert not any(it["doi"] == "_global" for it in pa)
    # recall() 全局召回含 _global
    g = api.recall("Ionic migration")
    assert any(it["doi"] == "_global" for it in g), f"全局应含 _global: {g}"


# ── 用户反馈 2026-09-12：中文问句召不回笔记（"针对该文献提问没有利用文献信息"）──
# 根因：无空格中文整句被 `_fts_query`/LIKE 当成**一个整串**匹配，恒不命中（实测 rows=0）。

def test_chinese_whole_sentence_hits_via_bigram_fallback(env, tmp_path):
    """无空格中文问句应能召回笔记（bigram LIKE 兜底）。"""
    roots = env
    _mk_paper(roots, "10.1000/cn.1", "磁响应人工肌肉",
              "本文的创新点在于把磁响应引入电离子人工肌肉")
    api.rebuild_fts()

    # 整句 19 字必然不在笔记里 → 旧实现返回 []
    assert api.recall_paper("10.1000/cn.1", "这篇文献的创新点是什么"), \
        "中文整句应能召回（bigram 兜底）"
    assert api.recall("这篇文献的创新点是什么"), "全局召回同样受益于 bigram 兜底"


def test_paper_notes_all_ignores_query(env, tmp_path):
    """单篇会话兜底口径：目标文档已知时，不做问句匹配也要把笔记给到模型。"""
    roots = env
    _mk_paper(roots, "10.1000/cn.2", "T", "一句话贡献内容")
    api.rebuild_fts()

    # 检索口径保持原契约（无命中 → 空），未被兜底污染
    assert api.recall_paper("10.1000/cn.2", "zzz完全不相关zzz") == []
    items = api.paper_notes_all("10.1000/cn.2")
    assert items and items[0]["file"] == "_note.md" and items[0]["snippet"]
    assert api.paper_notes_all("10.1000/不存在.9") == []
    # 截断上限生效（防止将来误把整篇产物全塞进上下文）
    big = api.paper_notes_all("10.1000/cn.2", max_chars=10)
    assert big and all(len(it["snippet"]) <= 10 for it in big)
