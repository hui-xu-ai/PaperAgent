#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_api.py
功能: T15 统一门面 api.py 测试（合成 PDF 端到端 + 工具发现/监督查询）
对外接口: 无（测试）
版本: v1.0.0 (2025-xx-xx)
版本历史:
  v1.0.0 初始版本
"""
import pymupdf
import pytest

import paperparse.api as api


def _make_pdf(path):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "API Facade Test Paper", fontsize=16)
    page.insert_text((72, 100), "Alice Doe, Bob Smith", fontsize=11)
    page.insert_text((72, 130), "Abstract paragraph for facade test.", fontsize=10)
    doc.save(str(path))
    doc.close()


def test_convert_pdf_success(tmp_work):
    pdf = tmp_work / "facade.pdf"
    _make_pdf(pdf)
    result = api.convert_pdf(str(pdf), out_dir=str(tmp_work / "out"))
    assert result.status == "success"
    assert result.outputs.md
    md = result.outputs.md
    text = __import__("pathlib").Path(md).read_text(encoding="utf-8")
    assert "tags:" in text
    assert "API Facade Test Paper" in text


def test_convert_pdf_missing_file(tmp_work):
    result = api.convert_pdf(str(tmp_work / "nope.pdf"), out_dir=str(tmp_work / "out"))
    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == "PAPER-0001"


def test_process_pdf_one_shot(tmp_work):
    """process_pdf 默认模式：解析成功返回 document_json（翻译/总结由 paperkb 流水线处理）。"""
    from pathlib import Path
    pdf = tmp_work / "facade.pdf"
    _make_pdf(pdf)
    r = api.process_pdf(str(pdf), parser="pymupdf", out_dir=str(tmp_work / "out"))
    assert r["status"] == "success" and r["mode"] == "default"
    assert r["document_json"] and Path(r["document_json"]).exists()
    assert r["prompt_path"] is None            # 瘦身后不再生成网页往返 prompt
    assert "paperkb" in r["note"]


def test_process_pdf_v4_requires_key_but_v1_free(monkeypatch, tmp_work):
    """v2.1 监督：mineru-v4 无 key → 立即报错停止（提示换通道）；mineru（v1 免费）无 key 不触发 key 检查"""
    import os
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    monkeypatch.setenv("MINERU_API_KEY", "")            # 确保空 key
    pdf = tmp_work / "facade.pdf"
    _make_pdf(pdf)
    # v4 无 key → 立即报错，不执行解析
    r = api.process_pdf(str(pdf), parser="mineru-v4", out_dir=str(tmp_work / "out"))
    assert r["status"] == "error"
    assert "MINERU_API_KEY" in (r.get("error") or "")
    assert "mineru" in (r.get("error") or "")          # 提示可改用 v1 免费
    assert r["document_json"] is None                   # 未执行解析
    assert not (tmp_work / "out").exists()              # 无任何产出
    # v1 免费（mineru）无 key 不触发 key 检查（v1 通道无需密钥）；本地 pymupdf 可走通
    r2 = api.process_pdf(str(pdf), parser="pymupdf", out_dir=str(tmp_work / "out2"))
    assert r2["status"] == "success"


def test_process_pdf_parse_only(tmp_work):
    """v2.0 解析模式：导出 en.md+images+document.json 全套，不翻译不总结（0 AI token）"""
    from pathlib import Path
    pdf = tmp_work / "facade.pdf"
    _make_pdf(pdf)
    r = api.process_pdf(str(pdf), parser="pymupdf", out_dir=str(tmp_work / "out"),
                        parse_only=True)
    assert r["status"] == "success" and r["mode"] == "parse_only"
    assert r["prompt_path"] is None                     # 不生成翻译 prompt
    assert "未翻译" in r["note"]
    ex = Path(r["export_dir"])
    assert list(ex.glob("*.en.md"))                      # 英文版 markdown
    assert Path(r["document_json"]).exists()             # 全套含 document.json
    assert (ex / "images").exists() or not list(Path(r["document_json"]).parent.parent.glob("images/*"))


def test_list_tools_and_help():
    """v2.0：list_tools 默认只返回 core(process_pdf)；scope=all 含 advanced；dev 隐藏；help 仍可查"""
    tools = api.list_tools()
    names = {t["name"] for t in tools}
    assert names == {"process_pdf"}                 # 默认模式只 1 个工具（AI 零选择）
    # 高级模式：core + advanced
    all_names = {t["name"] for t in api.list_tools(scope="all")}
    assert {"process_pdf", "query", "help", "list_tools"} <= all_names
    # dev 工具永不返回
    assert not ({"convert_pdf", "audit", "status", "selftest", "m5", "measure",
                 "study", "render_md"} & all_names)
    # help 仍可查全部（含 dev）
    detail = api.tool_help("convert_pdf")
    assert detail["input_schema"]["required"] == ["pdf_path"]
    with pytest.raises(Exception):
        api.tool_help("no_such_tool")


def test_selftest():
    report = api.selftest()
    assert report["python"]
    assert "pymupdf" in report["deps"]


def test_status_and_audit(tmp_work):
    pdf = tmp_work / "facade.pdf"
    _make_pdf(pdf)
    result = api.convert_pdf(str(pdf), out_dir=str(tmp_work / "out2"))
    st = api.status(result.run_id, out_dir=str(tmp_work / "out2"))
    assert st["found"] is True
    assert st["event_count"] > 0
    events = api.audit(result.run_id, out_dir=str(tmp_work / "out2"))
    assert len(events) > 0
    assert events[0]["run_id"] == result.run_id


def test_verify_html_reserved_interface():
    """T19 HTML→MD 预留接口已随 HTML 模块剥离（2026-08-26 归档
    archive/html-20260826/）——接口不存在，测试退役。"""
    assert not hasattr(api, "verify_html")


def test_query_paragraphs_partial(tmp_work):
    """局部查询：按段号/章节返回限定片段（不返回全文；超长截断；默认只回英文）"""
    from paperparse.core.document_builder import save_document
    from paperparse.middleware.schema import ArticleDocument, ArticleMetadata, Paragraph
    long_text = "Results paragraph " + "y" * 5000
    doc = ArticleDocument(
        metadata=ArticleMetadata(title="T", abstract="Abs", extraction_time="x"),
        paragraphs=[
            Paragraph(para_id="P001", order=0, section="1. Intro",
                      text_en="Intro.", text_zh="引言。", confidence=0.9),
            Paragraph(para_id="P002", order=1, section="2. Results",
                      text_en=long_text, confidence=0.9),
        ])
    p = tmp_work / "document.json"
    save_document(doc, p)

    r = api.query_paragraphs(str(p), para_ids=["P002"], limit_chars=100)
    assert r["found"] == 1
    assert len(r["items"][0]["text"]) <= 100 + 6        # 100 + "…(截断)"
    assert r["items"][0]["text"].endswith("(截断)")
    # 章节过滤 + 默认 en（不含中文译文）
    r2 = api.query_paragraphs(str(p), section="1. Intro")
    assert r2["found"] == 1 and r2["items"][0]["para_id"] == "P001"
    assert "引言" not in r2["items"][0]["text"]
    # include=both 含中文
    r3 = api.query_paragraphs(str(p), para_ids=["P001"], include="both")
    assert "引言。" in r3["items"][0]["text"]
    assert r["sections"] == ["1. Intro", "2. Results"]
