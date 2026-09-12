# -*- coding: utf-8 -*-
"""识别复核服务测试（P12-6）：清单/PDF 页图/选择落地/规则决策。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.services.review_service import ReviewService

MINERU_TEXT = "Reinforced magnetic responsive electro ionic artificial muscles are promising."
PADDLEOCR_TEXT = "Reinforced magnetic-responsive electro-ionic artificial muscles are promising."


@pytest.fixture
def env(tmp_path):
    work = tmp_path / "dual"
    rules = tmp_path / "rules"
    (rules / "learned").mkdir(parents=True)
    settings = Settings(
        db_path=str(tmp_path / "t.db"),
        engine_work_root=str(tmp_path / "library"),
        dual_work_root=str(work),
        rules_dir=str(rules),
    )
    return {"tmp": tmp_path, "work": work, "rules": rules, "settings": settings}


def _make_paper(tmp_path, work, doc_json: Path) -> dict:
    stem = "testpaper"
    d = work / stem
    d.mkdir(parents=True, exist_ok=True)
    (d / "mineru_blocks.json").write_text(json.dumps([
        {"block_id": "M0001", "page": 1, "bbox": [100, 100, 800, 150], "text": MINERU_TEXT,
         "kind": "body", "source": "mineru"}]), encoding="utf-8")
    (d / "paddleocr_blocks.json").write_text(json.dumps([
        {"block_id": "P0001", "page": 1, "bbox": [90, 90, 810, 160], "text": PADDLEOCR_TEXT,
         "kind": "body", "source": "paddleocr"}]), encoding="utf-8")
    (d / "review.json").write_text(json.dumps({
        "count": 1, "ai": {"arbitrated": 1, "applied": 0, "pending_review": 1},
        "items": [{"report_idx": 2, "page": 1, "dice": 0.86,
                   "mineru": {"block_id": "M0001", "text": MINERU_TEXT[:160]},
                   "paddleocr": {"block_id": "P0001", "text": PADDLEOCR_TEXT[:160]},
                   "ai": {"verdict": "paddleocr", "reason": "拼写", "confidence": 0.95,
                          "applied": False}}]}), encoding="utf-8")
    # 真实可渲染 PDF（pymupdf 生成两页，供页图渲染测试）
    import pymupdf
    pdf = pymupdf.open()
    pdf.new_page(width=595, height=780)
    pdf.new_page(width=595, height=780)
    pdf.save(str(tmp_path / f"{stem}.pdf"))
    pdf.close()
    return {"id": 1, "pdf_path": str(tmp_path / f"{stem}.pdf"), "doc_json": str(doc_json)}


def _make_document(doc_json: Path):
    from paperparse.core.document_builder import build_document, save_document
    from paperparse.middleware.schema import ArticleMetadata, Paragraph, StitchResult
    doc = build_document(
        ArticleMetadata(title="t", authors=[], abstract="", keywords=[], doi=""),
        StitchResult(paragraphs=[Paragraph(
            para_id="P001", order=0, section="",
            text_en=MINERU_TEXT, source_block_ids=["M0001"])]),
        figures=[], references=[])
    save_document(doc, doc_json)
    return doc_json


def test_get_review(env):
    doc_json = _make_document(env["tmp"] / "doc.json")
    paper = _make_paper(env["tmp"], env["work"], doc_json)
    svc = ReviewService(env["settings"])
    r = svc.get_review(paper)
    assert r["available"] is True
    assert r["total"] == 1
    it = r["items"][0]
    assert it["page"] == 1 and it["ai_verdict"] == "paddleocr"
    assert it["mineru_text"] == MINERU_TEXT
    assert it["bbox"] == [90, 90, 810, 160]     # 以 PaddleOCR bbox 为准


def test_pdf_page_png(env):
    doc_json = _make_document(env["tmp"] / "doc.json")
    paper = _make_paper(env["tmp"], env["work"], doc_json)
    svc = ReviewService(env["settings"])
    data = svc.pdf_page_png(paper, 1, highlights=[[90, 90, 810, 160]])
    assert data[:4] == b"\x89PNG"
    with pytest.raises(ValueError):
        svc.pdf_page_png(paper, 99)


def test_apply_choice_paddleocr_replaces(env):
    doc_json = _make_document(env["tmp"] / "doc.json")
    paper = _make_paper(env["tmp"], env["work"], doc_json)
    svc = ReviewService(env["settings"])
    r = svc.apply_choice(paper, 0, "paddleocr")
    assert r["changed_paragraphs"] >= 1
    doc = json.loads(doc_json.read_text(encoding="utf-8"))
    assert PADDLEOCR_TEXT in doc["paragraphs"][0]["text_en"]
    assert MINERU_TEXT not in doc["paragraphs"][0]["text_en"]
    # review.json 已标记 + audit 落盘
    review = json.loads((env["work"] / "testpaper" / "review.json").read_text(encoding="utf-8"))
    assert review["items"][0]["user_choice"] == "paddleocr"
    audit = json.loads((env["work"] / "testpaper" / "review_audit.json").read_text(encoding="utf-8"))
    assert audit[0]["choice"] == "paddleocr"


def test_apply_choice_mineru_keeps(env):
    doc_json = _make_document(env["tmp"] / "doc.json")
    paper = _make_paper(env["tmp"], env["work"], doc_json)
    svc = ReviewService(env["settings"])
    r = svc.apply_choice(paper, 0, "mineru")
    assert r["changed_paragraphs"] == 0
    doc = json.loads(doc_json.read_text(encoding="utf-8"))
    assert MINERU_TEXT in doc["paragraphs"][0]["text_en"]


def test_mined_rules_api_removed(env):
    """2026-09-12 批1：learned **挖掘**规则卡片/端点/服务方法整体退役（僵尸清理）。

    守卫目的：这批规则恒空且批准无实际作用（P14 链不产生、rule_engine 只在归档老链用），
    若有人误把它们加回来，这里会失败。domain_ai 词典闭环**不走**这些方法，见
    test_apply_choice_domain_ai_promotes。
    """
    from app.services.review_service import ReviewService
    svc = ReviewService(env["settings"])
    assert not hasattr(svc, "pending_rules")
    assert not hasattr(svc, "decide_rule")


def test_apply_choice_domain_ai_promotes(env, monkeypatch):
    """回归守卫：删掉挖掘规则面后，**domain_ai 化学式词典闭环仍在**
    （复核页批准 B → 写 rules/learned/domain.json，consensus_fix 下次解析读它）。"""
    monkeypatch.setenv("RULES_DIR", str(env["rules"]))   # 落点判据用 RULES_DIR（见 _learned_domain_path）
    BEFORE = "The salt NaBF4x was measured at 300 K."
    AFTER = "The salt NaBF4- was measured at 300 K."
    d = env["work"] / "testpaper"
    d.mkdir(parents=True, exist_ok=True)
    (d / "mineru_blocks.json").write_text(json.dumps([
        {"block_id": "M0001", "page": 1, "bbox": [100, 100, 800, 150], "text": BEFORE,
         "kind": "body", "source": "mineru"}]), encoding="utf-8")
    (d / "paddleocr_blocks.json").write_text(json.dumps([
        {"block_id": "P0001", "page": 1, "bbox": [90, 90, 810, 160], "text": AFTER,
         "kind": "body", "source": "paddleocr"}]), encoding="utf-8")
    (d / "review.json").write_text(json.dumps({
        "count": 1, "items": [{
            "page": 1,
            "mineru": {"block_id": "M0001", "text": BEFORE},
            "paddleocr": {"block_id": "P0001", "text": AFTER},
            "evidence": {"kind": "domain_ai", "suggestion": "BF4-", "unicode": "BF₄⁻"},
        }]}), encoding="utf-8")
    doc_json = env["tmp"] / "doc.json"
    from paperparse.core.document_builder import build_document, save_document
    from paperparse.middleware.schema import ArticleMetadata, Paragraph, StitchResult
    doc = build_document(
        ArticleMetadata(title="t", authors=[], abstract="", keywords=[], doi=""),
        StitchResult(paragraphs=[Paragraph(
            para_id="P001", order=0, section="",
            text_en=BEFORE, source_block_ids=["M0001"])]),
        figures=[], references=[])
    save_document(doc, doc_json)
    paper = {"id": 1, "pdf_path": str(env["tmp"] / "testpaper.pdf"),
             "doc_json": str(doc_json)}      # dual_dir 回退按 pdf stem 找 work/testpaper

    svc = ReviewService(env["settings"])
    r = svc.apply_choice(paper, 0, "paddleocr")
    assert r["changed_paragraphs"] == 1
    learned = env["rules"] / "learned" / "domain.json"
    assert learned.exists(), "domain_ai 批准必须写 learned/domain.json"
    data = json.loads(learned.read_text(encoding="utf-8"))
    assert any(e.get("formula") == "BF4-" for e in data.get("chemistry", []))


def test_diff_html_marks_differences():
    """P12 反馈：字符级 diff 标红差异字段（空格/LaTeX 差异标红；完全一致无 mark）"""
    from app.services.review_service import ReviewService
    # 空格差异
    m, p_ = ReviewService._diff_html(
        "Ionic polymer sensors exhibit high sensitivity 0.1 mV.",
        "Ionic polymer sensors exhibit high sensitivity 0.1mV.")
    assert "<mark" in m and "<mark" in p_
    # LaTeX 表示差异
    m2, p2 = ReviewService._diff_html("The equation is $E = mc^2$ here.",
                                      "The equation is E = mc^2 here.")
    assert "<mark" in m2 and "<mark" in p2
    # 完全一致 → 无 mark
    m3, p3 = ReviewService._diff_html("same text", "same text")
    assert "<mark" not in m3 and "<mark" not in p3


def test_diff_html_escapes_text_segments():
    """P12F：diff 输出文本段一律 HTML 转义（& < >），仅保留生成的结构标签——
    输出可直接 innerHTML（复核区直插渲染，防 PDF 文本注入标签/公式跨标签配对）。"""
    from app.services.review_service import ReviewService
    m, p_ = ReviewService._diff_html("a < b & c > d", "a < b & c > d")  # 完全一致
    assert m == "a &lt; b &amp; c &gt; d"      # 等段也被转义
    # 生成标签完好（含空格属性分隔，ASCII 连字符）
    m2, p2 = ReviewService._diff_html("x=1", "x=2")
    assert '<mark class="rv-diff">' in m2 and '<mark class="rv-diff">' in p2
    assert "rv-diff" in m2 and "\u2212" not in m2    # 无 U+2212
    # 文本中的尖括号不会变成标签（安全）
    m3, _ = ReviewService._diff_html("<script>alert(1)</script>", "<script>x</script>")
    assert "<script>" not in m3 and "&lt;script&gt;" in m3


def test_diff_html_keeps_formula_whole():
    """P12F：token 级 diff——公式 $...$ 作为整体标红，不逐字符切碎
    （旧字符级 diff 把 `$1 0 0 ~ ^ { \\circ } \\mathrm { C } .$` 切成
    `<mark>$</mark>1<mark> </mark>0...`，$ 孤立标红且 KaTeX 无法配对渲染）。"""
    from app.services.review_service import ReviewService
    a = "optimal performance is $1 0 0 ~ ^ { \\circ } \\mathrm { C } .$ here."
    b = "optimal performance is ${ 1 0 0 } ^ { \\circ } \\mathrm { C } ,$ here."
    m, p_ = ReviewService._diff_html(a, b)
    # 公式整体出现在单个 <mark> 内（不被拆散）
    assert '<mark class="rv-diff">$1 0 0 ~ ^ { \\circ } \\mathrm { C } .$</mark>' in m
    assert '<mark class="rv-diff">${ 1 0 0 } ^ { \\circ } \\mathrm { C } ,$</mark>' in p_
    # 周围词块不被误标红（仅公式差异标红）
    assert '<mark class="rv-diff">performance' not in m
    assert '<mark class="rv-diff">here' not in m
    # 相同公式 → 不标红且完整保留
    m2, p2 = ReviewService._diff_html("x $E=mc^2$ y", "x $E=mc^2$ y")
    assert "<mark" not in m2 and "$E=mc^2$" in m2
    # 词级差异（不缺公式时正常标红）
    m3, p3 = ReviewService._diff_html("fast response time", "fast response speed")
    assert '<mark class="rv-diff">time</mark>' in m3
    assert '<mark class="rv-diff">speed</mark>' in p3


def test_apply_choice_mineru_rolls_back_ai_applied(env):
    """P12F：AI 已落地（段落=PaddleOCR 文本）后选 A=mineru → 段落还原为 MinerU
    （此前只标记不还原，AI 误落地无法撤销 → 最终结果错）。"""
    doc_json = _make_document(env["tmp"] / "doc.json")
    paper = _make_paper(env["tmp"], env["work"], doc_json)
    svc = ReviewService(env["settings"])
    # 1) 模拟 AI 已落地：先选 B 替换
    r1 = svc.apply_choice(paper, 0, "paddleocr")
    assert r1["changed_paragraphs"] >= 1
    doc = json.loads(doc_json.read_text(encoding="utf-8"))
    assert PADDLEOCR_TEXT in doc["paragraphs"][0]["text_en"]
    # 2) 复核发现 B 错 → 选 A 回滚
    r2 = svc.apply_choice(paper, 0, "mineru")
    assert r2["changed_paragraphs"] >= 1
    doc2 = json.loads(doc_json.read_text(encoding="utf-8"))
    assert MINERU_TEXT in doc2["paragraphs"][0]["text_en"]
    assert PADDLEOCR_TEXT not in doc2["paragraphs"][0]["text_en"]


# ---------------------------------------------------------- P15 p14 格式兼容
# p14 复核清单：无 mineru_blocks/paddleocr_blocks 文件，items 内联段落全文
# （block_id="mdN"/"paddle-<para>"），doc 段落 source_block_ids=["mdN"]


def _make_dual_p14(work, doc_json: Path) -> dict:
    stem = "p14paper"
    d = work / stem
    d.mkdir(parents=True, exist_ok=True)
    (d / "review.json").write_text(json.dumps({
        "count": 1, "ai": {"arbitrated": 1, "unresolved": 0, "applied_p": 0,
                           "pending_review": 1, "source": "p14"},
        "items": [{"report_idx": 0, "page": 1,
                   "mineru": {"block_id": "md3", "kind": "body",
                              "text": MINERU_TEXT},
                   "paddleocr": {"block_id": "paddle-RP001", "kind": "body",
                                 "text": PADDLEOCR_TEXT},
                   "ai": {"verdict": "paddleocr", "reason": "低置信待复核",
                          "confidence": 0.8, "applied": False},
                   "user_choice": "", "auto_resolved": "",
                   "evidence": {"para_id": "RP001", "md_idx": [3],
                                "pending": 1}}]}),
        encoding="utf-8")
    from paperparse.core.document_builder import build_document, save_document
    from paperparse.middleware.schema import ArticleMetadata, Paragraph, StitchResult
    doc = build_document(
        ArticleMetadata(title="t", authors=[], abstract="", keywords=[], doi=""),
        StitchResult(paragraphs=[Paragraph(
            para_id="P001", order=0, section="",
            text_en=MINERU_TEXT, source_block_ids=["md3"])]),
        figures=[], references=[])
    save_document(doc, doc_json)
    import pymupdf
    pdf = pymupdf.open()
    pdf.new_page(width=595, height=780)
    pdf.save(str(Path(doc_json).parent / f"{stem}.pdf"))
    pdf.close()
    return {"id": 1, "pdf_path": str(Path(doc_json).parent / f"{stem}.pdf"),
            "doc_json": str(doc_json)}


def test_get_review_p14_format(env):
    """p14 复核清单（无 blocks 文件，items 内联文本）→ get_review 兼容"""
    doc_json = env["tmp"] / "doc.json"
    paper = _make_dual_p14(env["work"], doc_json)
    svc = ReviewService(env["settings"])
    r = svc.get_review(paper)
    assert r["available"] is True
    assert r["total"] == 1
    it = r["items"][0]
    assert it["mineru_text"] == MINERU_TEXT
    assert it["paddleocr_text"] == PADDLEOCR_TEXT
    assert "<mark" in it["mineru_html"] or "<mark" in it["paddleocr_html"]  # diff 标红
    assert it["ai_verdict"] == "paddleocr" and it["confidence"] == 0.8


def test_apply_choice_p14_format(env):
    """p14 复核清单 → 选 B 段落级落地（source_block_ids 匹配 mdN）"""
    doc_json = env["tmp"] / "doc.json"
    paper = _make_dual_p14(env["work"], doc_json)
    svc = ReviewService(env["settings"])
    r = svc.apply_choice(paper, 0, "paddleocr")
    assert r["changed_paragraphs"] >= 1
    doc = json.loads(doc_json.read_text(encoding="utf-8"))
    assert PADDLEOCR_TEXT in doc["paragraphs"][0]["text_en"]
    assert MINERU_TEXT not in doc["paragraphs"][0]["text_en"]


# ---------------------------------------------------------- F1 源 PDF 定位兜底链
# 用户实测报障（2026-09-12，打包版 paper3 = 10.1038_ncomms8258）：
#   `papers.pdf_path` 指向解析期**暂存** `work/upload/<uuid>/xxx.pdf`（work 是"随时可清"区，
#   解析完即删）→ 复核页左侧 PDF 页图 500、`page_count=0`（"阅读区没有加载"）；
#   已解析文献暂存文件还在 ⇒ 表现为"算法区别对待"。
# 旧实现兜底 glob 到 `doc_json.parent.parent`（= library 根，**少一层**）且从不看知识库副本。
# 下列测试锁死新兜底链：library 副本 → 知识库副本 → 历史 `<DOI>.pdf` 命名；暂存仍优先。

STEM = "10.1038_ncomms8258"


def _write_pdf(path: Path, pages: int = 2) -> Path:
    import pymupdf
    path.parent.mkdir(parents=True, exist_ok=True)
    pdf = pymupdf.open()
    for _ in range(pages):
        pdf.new_page(width=595, height=780)
    pdf.save(str(path))
    pdf.close()
    return path


def _lib_env(env, monkeypatch):
    """library/<资源>/{document.json, source.pdf, work/review.json} + 暂存 pdf_path **缺失**。"""
    monkeypatch.setattr("app.config.APP_DATA_DIR", env["tmp"])   # kb 根 = <tmp>/knowledge_base
    lib = env["tmp"] / "library" / STEM                          # = settings.engine_work_root/<资源>
    (lib / "work").mkdir(parents=True, exist_ok=True)
    (lib / "work" / "review.json").write_text(json.dumps({
        "count": 1, "ai": {"source": "p14"},
        "items": [{"report_idx": 0, "page": 1,
                   "mineru": {"block_id": "md3", "kind": "body", "text": MINERU_TEXT},
                   "paddleocr": {"block_id": "paddle-RP001", "kind": "body",
                                 "text": PADDLEOCR_TEXT}}]}), encoding="utf-8")
    doc_json = _make_document(lib / "document.json")
    _write_pdf(lib / "source.pdf")
    paper = {"id": 3, "pdf_name": STEM + ".pdf",
             "pdf_path": str(env["tmp"] / "work" / "upload" / "gone" / (STEM + ".pdf")),
             "doc_json": str(doc_json)}
    return lib, doc_json, paper


def test_pdf_path_uses_library_copy_when_staging_gone(env, monkeypatch):
    """暂存被清理 → 命中 `library/<资源>/source.pdf`（页数不再为 0，页图可渲染）。"""
    lib, _doc_json, paper = _lib_env(env, monkeypatch)
    svc = ReviewService(env["settings"])
    assert svc._pdf_path(paper) == str(lib / "source.pdf")
    r = svc.get_review(paper)
    assert r["available"] is True and r["total"] == 1
    assert r["page_count"] == 2, "旧实现兜底 glob 错层 ⇒ page_count=0（复核页一页都加载不出）"
    assert svc.pdf_page_png(paper, 1)[:4] == b"\x89PNG"


def test_pdf_path_falls_back_to_kb_copy(env, monkeypatch):
    """library 副本也缺 → 命中 `knowledge_base/<资源>/source.pdf`（知识库有文件就能读）。"""
    lib, _doc_json, paper = _lib_env(env, monkeypatch)
    (lib / "source.pdf").unlink()
    kb = env["tmp"] / "knowledge_base" / STEM
    kb.mkdir(parents=True, exist_ok=True)
    _write_pdf(kb / "source.pdf")
    svc = ReviewService(env["settings"])
    assert svc._pdf_path(paper) == str(kb / "source.pdf")
    assert svc.pdf_page_png(paper, 2)[:4] == b"\x89PNG"


def test_pdf_path_accepts_legacy_pdf_name(env, monkeypatch):
    """历史命名 `library/<资源>/<DOI>.pdf`（无 source.pdf）同样命中。"""
    lib, _doc_json, paper = _lib_env(env, monkeypatch)
    legacy = lib / (STEM + ".pdf")
    (lib / "source.pdf").rename(legacy)
    svc = ReviewService(env["settings"])
    assert svc._pdf_path(paper) == str(legacy)


def test_pdf_path_prefers_live_staging_pdf(env, monkeypatch):
    """暂存文件仍在 → 优先用 `pdf_path` 原值（**不改变**旧行为/不引入额外 IO 语义）。"""
    _lib, _doc_json, paper = _lib_env(env, monkeypatch)
    stage = env["tmp"] / "work" / "upload" / "live" / (STEM + ".pdf")
    _write_pdf(stage, pages=1)
    paper["pdf_path"] = str(stage)
    svc = ReviewService(env["settings"])
    assert svc._pdf_path(paper) == str(stage)
