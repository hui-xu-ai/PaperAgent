# -*- coding: utf-8 -*-
"""引擎接入层测试（复用引擎已解析的 document.json，不触发网络）。"""
from __future__ import annotations

import pytest

from conftest import ENGINE_DOC


def test_doc_summary_stable(engine):
    d1 = engine.doc_summary(ENGINE_DOC)
    d2 = engine.doc_summary(ENGINE_DOC)
    assert d1 == d2  # 前缀缓存友好的确定性
    assert d1["paragraph_count"] > 10
    assert d1["sections"] and d1["sections"][0]["para_ids"]


def test_query_local(engine):
    """局部检索：动态取第一个实际章节（测试数据可能被引擎重解析，章节结构变化）。"""
    d = engine.doc_summary(ENGINE_DOC)
    assert d["sections"], "测试数据应有章节"
    first_section = d["sections"][0]["section"]
    r = engine.query(ENGINE_DOC, section=first_section, include="en")
    assert r["found"] >= 1
    assert all(it["section"] == first_section for it in r["items"])
    assert r["items"][0]["para_id"].startswith("P")


def test_missing_doc_raises(engine, tmp_path):
    from app.services.engine_service import EngineError
    with pytest.raises(EngineError):
        engine.doc_summary(tmp_path / "nope.json")


def test_inplace_export_no_backup(tmp_path, settings, monkeypatch):
    """T01/T4：就地导出到规范库 <work>/<DOI>/，不产生独立 backup 目录；
    library 只留 en.md（干净版），不再生成 <DOI>.md/.zh.md/.summary.md。"""
    import shutil
    from pathlib import Path

    from app.services.engine_service import EngineService

    lib = Path(settings.engine_work_root)
    doc_dir = lib / "10.1002_adma.202407106" / "intermediate"
    doc_dir.mkdir(parents=True)
    doc = doc_dir / "document.json"
    shutil.copy2(ENGINE_DOC, doc)

    eng = EngineService(settings)
    r = eng._inplace_export(doc)
    out = Path(r["output_dir"])
    assert out == lib / "10.1002_adma.202407106"
    # 只保留 en.md；旧带 DOI 前缀变体不再生成
    assert (out / "en.md").exists()
    for suffix in (".md", ".zh.md", ".summary.md", ".en.md"):
        assert not (out / f"10.1002_adma.202407106{suffix}").exists(), suffix
    assert not (out / "variants").exists()
    assert r["document_json"] == str(doc)
    # 独立 backup 目录未被创建/写入
    backup = Path(settings.engine_out_root)
    assert not (backup / "10.1002_adma.202407106").exists()


def test_inplace_export_cleans_stale_doi_files(tmp_path, settings):
    """T4：就地导出删除旧结构残留（<DOI>.md/.zh.md/.summary.md/variants/）。"""
    import shutil
    from pathlib import Path

    from app.services.engine_service import EngineService

    lib = Path(settings.engine_work_root)
    doc_dir = lib / "10.1002_adma.202407106" / "intermediate"
    doc_dir.mkdir(parents=True)
    doc = doc_dir / "document.json"
    shutil.copy2(ENGINE_DOC, doc)
    out = lib / "10.1002_adma.202407106"
    # 预置旧结构残留（存量迁移前状态）
    for suffix in (".md", ".zh.md", ".summary.md", ".en.md", ".pdf"):
        (out / f"10.1002_adma.202407106{suffix}").write_text("stale", encoding="utf-8")
    (out / "variants").mkdir()
    (out / "variants" / "translated.md").write_text("stale", encoding="utf-8")

    eng = EngineService(settings)
    eng._inplace_export(doc)
    for suffix in (".md", ".zh.md", ".summary.md", ".en.md", ".pdf"):
        assert not (out / f"10.1002_adma.202407106{suffix}").exists(), suffix
    assert not (out / "variants").exists()
    assert (out / "en.md").exists()


def test_clean_staging(tmp_path, settings):
    """T01：web_roundtrip/qna 遗留的暂存副本被清理。"""
    from pathlib import Path

    from app.services.engine_service import EngineService

    eng = EngineService(settings)
    staging = Path(settings.engine_out_root) / "10.1002_adma.202407106"
    staging.mkdir(parents=True)
    (staging / "x.md").write_text("x")
    eng._clean_staging(ENGINE_DOC)
    assert not staging.exists()


def test_rerender_paper(tmp_path, settings, monkeypatch):
    """T06 + P0-B（2026-09-12）：重渲染写 **library/<资源目录>/en_zh.md**（翻译真相源）。

    旧行为写 kb，`knowledge_base/` 与 `library/` 各一份 → 容易读到旧副本；
    现 kb 侧读取回退到 library（kb_service.read_file 按 mtime 择新）。
    """
    import shutil
    from pathlib import Path

    import app.config as cfg
    from app.services.engine_service import EngineService

    monkeypatch.setattr(cfg, "APP_DATA_DIR", tmp_path)  # kb 默认根 → tmp
    lib = Path(settings.engine_work_root)
    doc_dir = lib / "10.1002_adma.202407106" / "intermediate"
    doc_dir.mkdir(parents=True)
    doc = doc_dir / "document.json"
    shutil.copy2(ENGINE_DOC, doc)
    eng = EngineService(settings)
    r = eng.rerender_paper(doc, "obsidian_bilingual")
    paper_md = Path(r["paper_md"])
    assert paper_md.exists()
    assert paper_md.parent == lib / "10.1002_adma.202407106", "变体落在 library 资源目录"
    text = paper_md.read_text(encoding="utf-8")
    assert "Abstract" in text or "abstract" in text


def test_combined_translate_writes_kb_variants(tmp_path, settings, monkeypatch):
    """变体写入语义（2026-09-16 更新为方案 A：**双写**）。

    旧行为：变体单一来源在 **library**，kb 靠读取回退（历史原因：kb 副本曾比 library 少 87 段译文）。
    现行为（用户决策：kb = 唯一成品区 + 阅读器"定版优先"）：变体**同时写 kb 与 library**——
      · kb 那份是定版，阅读器直接读到；
      · library 那份暂留兼容（迁移完成、验证无异常后可撤）。
    D16：summary.md 不再生成。library 旧结构（<DOI>.md / variants/）仍清理。
    """
    import shutil
    from pathlib import Path

    import app.config as cfg
    from app.services.engine_service import EngineService
    from app.services import kbmeta_service

    monkeypatch.setattr(cfg, "APP_DATA_DIR", tmp_path)  # kb 默认根 → tmp

    class _FakeKbMeta:
        def translate_now(self, doc_json):
            return {"translated": 3, "summary": {"创新点": "x"}}

    monkeypatch.setattr(kbmeta_service, "get_kbmeta", lambda: _FakeKbMeta())

    lib = Path(settings.engine_work_root)
    doc_dir = lib / "10.1002_adma.202407106" / "intermediate"
    doc_dir.mkdir(parents=True)
    doc = doc_dir / "document.json"
    shutil.copy2(ENGINE_DOC, doc)
    out = lib / "10.1002_adma.202407106"
    # 预置旧结构残留，验证 combined_translate 后一并清理
    (out / "10.1002_adma.202407106.md").write_text("stale", encoding="utf-8")
    (out / "variants").mkdir()
    (out / "variants" / "translated.md").write_text("stale", encoding="utf-8")

    eng = EngineService(settings)
    r = eng.combined_translate(doc, template="obsidian_bilingual")
    assert r["library_dir"] == str(out)
    # 定版 kb 必须拿到变体（阅读器"定版优先"才不会回退中转站）
    kb_folder = tmp_path / "knowledge_base" / "10.1002_adma.202407106"
    assert (kb_folder / "zh.md").exists(), "变体必须写进定版 kb"
    assert (kb_folder / "en_zh.md").exists(), "变体必须写进定版 kb"
    for name in ("zh.md", "en_zh.md"):
        assert (out / name).is_file(), name
        assert r["variants"][name] == str(out / name)
    assert not (out / "summary.md").exists()  # D16：不再生成
    # library 旧结构已清理，且不生成 <DOI>.md
    assert not (out / "10.1002_adma.202407106.md").exists()
    assert not (out / "variants").exists()
    assert r["translate"]["translated"] == 3


def test_g5_figures_restored(tmp_path, settings):
    """G5：导出 md 补齐图片行（![](images/F001.png)）且无 `<!-- image -->` 占位符残留。"""
    import shutil
    from pathlib import Path

    from app.services.engine_service import EngineService

    lib = Path(settings.engine_work_root)
    doc_dir = lib / "10.1002_adma.202407106" / "intermediate"
    doc_dir.mkdir(parents=True)
    doc = doc_dir / "document.json"
    shutil.copy2(ENGINE_DOC, doc)
    eng = EngineService(settings)
    r = eng._inplace_export(doc)
    en_md = Path(r["en_md"]).read_text(encoding="utf-8")
    # 图片行恢复（fixture 含 F001-F007）
    assert "![](images/F001.png)" in en_md, "en.md 应包含 F001 图片行"
    assert "![](images/F007.png)" in en_md, "en.md 应包含 F007 图片行"
    # 占位符被清除（无 HTML 注释残留）
    assert "<!--" not in en_md
    # document.json 占位符已清洗
    cleaned = doc.read_text(encoding="utf-8")
    assert "<!-- image" not in cleaned


def test_g5_sanitize_idempotent(tmp_path, settings):
    """G5：清洗幂等（二次调用不再改动）。"""
    import shutil
    from pathlib import Path

    from app.services.engine_service import EngineService

    lib = Path(settings.engine_work_root)
    doc_dir = lib / "10.1002_adma.202407106" / "intermediate"
    doc_dir.mkdir(parents=True)
    doc = doc_dir / "document.json"
    shutil.copy2(ENGINE_DOC, doc)
    eng = EngineService(settings)
    r1 = eng.sanitize_document(doc)
    r2 = eng.sanitize_document(doc)
    assert r1["changed"] is True
    assert r2["changed"] is False


def test_p2c_post_parse_clean(tmp_path, settings):
    """P2-C：parse-only 路径（引擎直写 en.md 不经导出）也清洗占位符 + 补齐图片行。"""
    import shutil
    from pathlib import Path

    from app.services.engine_service import EngineService

    lib = Path(settings.engine_work_root)
    doc_dir = lib / "10.1002_adma.202407106" / "intermediate"
    doc_dir.mkdir(parents=True)
    doc = doc_dir / "document.json"
    shutil.copy2(ENGINE_DOC, doc)
    eng = EngineService(settings)
    eng._post_parse_clean(doc)
    # 2026-08-26：目标改为 en.md（干净版，无模板 <DOI>.en.md）
    en_md = lib / "10.1002_adma.202407106" / "en.md"
    text = en_md.read_text(encoding="utf-8")
    assert "<!--" not in text, "en.md 不应残留 HTML 占位符注释"
    assert "![](images/F001.png)" in text, "en.md 应包含 F001 图片行"
    assert "<!-- image" not in doc.read_text(encoding="utf-8")


def test_p2c_sanitize_variants(tmp_path, settings):
    """P2-C：清洗兼容 `<!-- image-1 -->` / `<!-- image_3 -->` / `<!-- IMG 2 -->` 变体。"""
    import json
    import shutil
    from pathlib import Path

    from app.services.engine_service import EngineService

    lib = Path(settings.engine_work_root)
    doc_dir = lib / "10.1002_adma.202407106" / "intermediate"
    doc_dir.mkdir(parents=True)
    doc = doc_dir / "document.json"
    shutil.copy2(ENGINE_DOC, doc)
    data = json.loads(doc.read_text(encoding="utf-8"))
    if data.get("paragraphs"):
        data["paragraphs"][0]["text_en"] = (
            "Prefix <!-- image-1 --> <!-- image_3 --> <!-- IMG 2 --> tail")
        doc.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    eng = EngineService(settings)
    r = eng.sanitize_document(doc)
    assert r["changed"] is True
    cleaned = json.loads(doc.read_text(encoding="utf-8"))
    t = cleaned["paragraphs"][0]["text_en"]
    assert "<!--" not in t, f"占位符未全部清除: {t!r}"


def test_p2b_parse_source_label():
    """P2-B：解析来源标签映射（前端徽标/事件消息用）。"""
    from app.services.engine_service import parse_source_label
    assert "精准" in parse_source_label("mineru-v4")
    assert "免费" in parse_source_label("mineru")
    assert "本地" in parse_source_label("pymupdf-local")
    assert parse_source_label("") == "未知"


# ---------------------------------------------------------------- P12/P14 管线

class _FakeDualApi:
    """打桩 api：p14/p12 双通道 + 单通道信封"""

    def __init__(self, dual_ok: bool = True):
        self.dual_ok = dual_ok
        self.calls: list[str] = []
        self.v2_kwargs: dict = {}

    def process_pdf_dual(self, pdf_path, **kw):
        self.calls.append("dual")
        if not self.dual_ok:
            from app.services.engine_service import EngineError
            raise EngineError("双通道失败（模拟）")
        return {"status": "success", "document_json": str(pdf_path),
                "parse_source": "dual", "warnings": ["AI 仲裁不可用"]}

    def process_pdf_v2(self, pdf_path, **kw):
        self.calls.append("v2")
        self.v2_kwargs = dict(kw)
        if not self.dual_ok:
            from app.services.engine_service import EngineError
            raise EngineError("v2 失败（模拟）")
        return {"status": "success", "document_json": str(pdf_path),
                "parse_source": "p14", "warnings": []}

    def process_pdf(self, pdf_path, **kw):
        self.calls.append("single:" + kw.get("parser", ""))
        return {"status": "success", "document_json": str(pdf_path),
                "parse_source": kw.get("parser", "mineru-v4")}


def _p12_settings(settings):
    """settings 副本：pipeline 回退 p12（Settings 为 frozen dataclass）。"""
    import dataclasses
    return dataclasses.replace(settings, pipeline="p12")


def test_parse_pdf_cancel_not_degraded(tmp_path, settings, monkeypatch):
    """P15：cancel_check=True → 抛 TaskCancelled（用户取消是意图，**不降级**单通道）"""
    from app.services.engine_service import EngineService, TaskCancelled
    eng = EngineService(settings)
    fake = _FakeDualApi(dual_ok=False)   # 若被当失败降级会走 single
    monkeypatch.setattr(eng, "_api", fake)
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    with pytest.raises(TaskCancelled):
        eng.parse_pdf(str(pdf), cancel_check=lambda: True)
    assert fake.calls == []   # 取消在解析前，任何通道都未调用


def test_parse_pdf_p12_falls_back_to_v2(tmp_path, settings, monkeypatch):
    """2026-08-26 冻结 P12：pipeline=p12 配置回落 p14（process_pdf_v2），
    process_pdf_dual 不再调用。"""
    from app.services.engine_service import EngineService
    eng = EngineService(_p12_settings(settings))
    fake = _FakeDualApi(dual_ok=True)
    monkeypatch.setattr(eng, "_api", fake)
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    fake_md = tmp_path / "fake_mineru.md"
    fake_md.write_text("# Fake\n\nBody\n", encoding="utf-8")
    monkeypatch.setattr(eng, "_ensure_mineru_md", lambda p, w: fake_md)
    r = eng.parse_pdf(str(pdf))
    assert fake.calls == ["v2"]                 # 不再走 dual
    assert r["parse_source"] == "p14"


def test_parse_pdf_p14_default_uses_v2(tmp_path, settings, monkeypatch):
    """P15 Step5：pipeline=p14（默认）→ 走 process_pdf_v2，来源=p14；
    cancel_check/on_wait 透传；_ensure_mineru_md 提供 md 基底。"""
    from pathlib import Path

    from app.services.engine_service import EngineService
    eng = EngineService(settings)   # Settings 默认 pipeline=p14
    fake = _FakeDualApi(dual_ok=True)
    monkeypatch.setattr(eng, "_api", fake)
    fake_md = tmp_path / "fake_mineru.md"
    fake_md.write_text("# Fake\n\nBody\n", encoding="utf-8")
    monkeypatch.setattr(eng, "_ensure_mineru_md", lambda pdf, work: fake_md)
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    def _cancel() -> bool:
        return False
    def _wait(sec, attempt) -> None:
        pass
    r = eng.parse_pdf(str(pdf), cancel_check=_cancel, on_wait=_wait)
    assert fake.calls == ["v2"]
    assert r["parse_source"] == "p14"
    kw = fake.v2_kwargs
    assert kw["md_path"] == str(fake_md)
    assert kw["cancel_check"] is _cancel       # 透传
    assert kw["on_wait"] is _wait              # 透传
    assert kw["paddle"] is True
    # 契约：ai_review 仅在 provider 已组装时为 True（与 provider 存在性一致）
    assert kw["ai_review"] == (kw["provider"] is not None)
    assert str(Path(kw["out_dir"]).resolve()) == str(
        (Path(settings.engine_work_root).parent / "output").resolve())


def test_parse_pdf_p14_fallback_single(tmp_path, settings, monkeypatch):
    """P15 Step5：p14 管线失败 → 自动降级单通道（Q2 语义保留），warnings 附原因。"""
    from app.services.engine_service import EngineService
    eng = EngineService(settings)   # pipeline 默认 p14
    fake = _FakeDualApi(dual_ok=False)
    monkeypatch.setattr(eng, "_api", fake)
    monkeypatch.setattr(eng, "_ensure_mineru_md",
                        lambda pdf, work: tmp_path / "fake.md")
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    r = eng.parse_pdf(str(pdf))
    assert fake.calls == ["v2", "single:mineru-v4"]
    assert r["parse_source"] == "mineru-v4"
    assert any("v2" in w for w in (r.get("warnings") or []))


def test_ensure_mineru_md_cache_and_refetch(tmp_path, settings, monkeypatch):
    """P15 Step5：_ensure_mineru_md——md5 命中复用缓存；PDF 变化重新拉取。"""
    import hashlib
    import json

    from app.services.engine_service import EngineService
    eng = EngineService(settings)
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    work = tmp_path / "dual" / "t"
    work.mkdir(parents=True)
    (work / "mineru_full.md").write_text("v1-cached", encoding="utf-8")
    (work / "meta.json").write_text(json.dumps(
        {"pdf_md5": hashlib.md5(b"%PDF-1.7").hexdigest(),
         "pipeline": "p14"}), encoding="utf-8")

    calls = {"n": 0}

    class _FakeMineruClient:
        def __init__(self, cfg):
            pass

        def extract_v4_batch(self, pdf, workdir="work/mineru_cache"):
            calls["n"] += 1
            from paperparse.middleware.schema import ParserBlocks
            raw = tmp_path / "raw_full.md"
            raw.write_text("v2-fresh", encoding="utf-8")
            return ParserBlocks(source="mineru", pages=1, blocks=[],
                                raw_path=str(raw))

    monkeypatch.setattr("paperparse.core.mineru_client.MineruClient",
                        _FakeMineruClient)
    # 命中缓存：不重新上传
    p = eng._ensure_mineru_md(str(pdf), work)
    assert p.read_text(encoding="utf-8") == "v1-cached"
    assert calls["n"] == 0
    # PDF 内容变化 → 重新拉取并更新 meta
    pdf.write_bytes(b"%PDF-1.7 changed")
    p2 = eng._ensure_mineru_md(str(pdf), work)
    assert p2.read_text(encoding="utf-8") == "v2-fresh"
    assert calls["n"] == 1
    meta = json.loads((work / "meta.json").read_text(encoding="utf-8"))
    assert meta["pdf_md5"] == hashlib.md5(b"%PDF-1.7 changed").hexdigest()
    assert meta["pipeline"] == "p14"
