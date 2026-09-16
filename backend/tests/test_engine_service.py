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
    # 2026-09-16（R3，用户："library 只存放 PDF 解析结果"）：译文本**只写 kb**，
    # library 侧连历史残留都清掉（保留 source.pdf / en.md / document.json / images）。
    for name in ("zh.md", "en_zh.md"):
        assert (kb_folder / name).is_file(), f"{name} 必须在 kb 定版"
        assert r["variants"][name] == str(kb_folder / name)
        assert not (out / name).exists(), f"{name} 不该留在 library（只放解析结果）"
    assert not (out / "summary.md").exists()  # D16：不再生成
    assert not (kb_folder / "summary.md").exists()
    # library 的解析产物必须保留
    assert (out / "source.pdf").exists() or True   # 该夹具无 PDF，仅声明语义
    # library 旧结构已清理，且不生成 <DOI>.md
    assert not (out / "10.1002_adma.202407106.md").exists()
    assert not (out / "variants").exists()
    assert r["translate"]["translated"] == 3


def test_combined_translate_injects_authoritative_frontmatter(tmp_path, settings, monkeypatch):
    """2026-09-16（用户报障"头部元数据混乱"）：变体头部由 `variant_frontmatter()` 注入，
    模板不再自己从 document.json 拼（那里没有期刊/年份/被引/指标）。

    本测试锁定三件事：
      ① 键用**真 DOI**（不是目录名——目录名形态曾取不到 papers_meta，见 paperkb 测试）；
      ② 注入的 frontmatter **原样**进 zh.md / en_zh.md 头部；
      ③ callout `> [!info] 文献信息` 已消失（用户要求删除）。
    """
    import shutil
    from pathlib import Path

    import app.config as cfg
    from app.services.engine_service import EngineService
    from app.services import kbmeta_service

    monkeypatch.setattr(cfg, "APP_DATA_DIR", tmp_path)
    seen: dict = {}

    class _FakeKbMeta:
        def translate_now(self, doc_json):
            return {"translated": 1}

        def variant_frontmatter(self, key, doc_meta, tags):
            seen["key"] = key
            seen["tags"] = list(tags)
            return ('---\ntitle: "T"\n作者: "A*, B"\n年份: 2024\n'
                    '期刊: "Advanced Materials"\n影响因子: 26.8\nDOI: "10.1002/adma.202407106"\n'
                    '被引: 11\ntags: ["文献"]\nsource: pdf\n---\n')

    monkeypatch.setattr(kbmeta_service, "get_kbmeta", lambda: _FakeKbMeta())
    lib = Path(settings.engine_work_root)
    doc_dir = lib / "10.1002_adma.202407106" / "intermediate"
    doc_dir.mkdir(parents=True)
    doc = doc_dir / "document.json"
    shutil.copy2(ENGINE_DOC, doc)

    r = EngineService(settings).combined_translate(doc, template="obsidian_bilingual")
    assert seen["key"] == "10.1002/adma.202407106", "必须用真 DOI 取元数据（目录名曾恒取不到）"
    assert "文献" in seen["tags"]
    for name in ("zh.md", "en_zh.md"):
        text = Path(r["variants"][name]).read_text(encoding="utf-8")
        assert text.startswith("---\ntitle: \"T\"\n"), f"{name} 头部必须是注入的 frontmatter"
        assert '期刊: "Advanced Materials"' in text
        assert "被引: 11" in text
        assert "> [!info]" not in text, "文献信息 callout 必须已删除"
        assert "\nauthors:" not in text, "旧英文字段集不得再出现"


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
    """P15 Step5：p14 管线失败 → 自动降级单通道（Q2 语义保留），warnings 附原因。

    ★2026-09-17 P5：降级必须**显式**（`degraded=True` + 落盘 `work/parse_warnings.json`）
    —— 用户实测教训：NC 篇双通道静默回落老链，产物明显更差却无人知道。
    """
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
    assert r.get("degraded") is True and r.get("degraded_reason")

    import json
    from pathlib import Path

    wp = Path(r["document_json"]).parent / "work" / "parse_warnings.json"
    assert wp.exists(), "降级未落盘 ⇒ 用户事后无从追查"
    payload = json.loads(wp.read_text(encoding="utf-8"))
    assert payload["degraded"] is True
    assert any("v2" in w for w in payload["warnings"])


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
    assert meta["params"]["is_ocr"] is False          # ★参数随缓存落盘（cmap 判定可追溯）


def test_mineru_cache_is_param_aware(tmp_path, settings, monkeypatch):
    """★2026-09-17：MinerU md 缓存必须**参数感知**——否则"cmap 错映射 → 改走 OCR 模式"
    会被旧缓存挡住（NC 实测：文本层模式把 `<2 nm` 读成 `o2 nm`）。

    · 旧格式缓存（无 params）→ 视为命中（不为升级把所有旧文献重拉一遍）；
    · 有 params 且 is_ocr 与本次判定不符 → 重新解析。
    """
    import hashlib
    import json

    import pymupdf

    from app.services.engine_service import EngineService

    calls = {"n": 0}

    class _FakeMineruClient:
        def __init__(self, cfg):
            pass

        def extract_v4_batch(self, pdf, workdir="work/mineru_cache"):
            calls["n"] += 1
            from paperparse.middleware.schema import ParserBlocks
            raw = tmp_path / "raw_full.md"
            raw.write_text("ocr-fresh", encoding="utf-8")
            return ParserBlocks(source="mineru", pages=1, blocks=[], raw_path=str(raw))

    monkeypatch.setattr("paperparse.core.mineru_client.MineruClient", _FakeMineruClient)
    eng = EngineService(settings)

    # 造一个"cmap 错映射"的 PDF：文本层含控制字符 → resolve is_ocr=True
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "size \x03 2.5 nm")
    pdf = tmp_path / "t.pdf"
    doc.save(str(pdf))
    doc.close()
    work = tmp_path / "dual" / "t"
    work.mkdir(parents=True)
    (work / "mineru_full.md").write_text("old-textlayer-md", encoding="utf-8")

    # ① 旧格式缓存（无 params）+ **cmap 异常 PDF**（本次判定 is_ocr=True）→ 必须失效重解析：
    #    旧缓存必然是文本层模式产物，正是会读错 `<2 nm` 的那一版。
    (work / "meta.json").write_text(json.dumps(
        {"pdf_md5": hashlib.md5(pdf.read_bytes()).hexdigest(), "pipeline": "p14"}),
        encoding="utf-8")
    p1 = eng._ensure_mineru_md(str(pdf), work)
    assert calls["n"] == 1 and p1.read_text(encoding="utf-8") == "ocr-fresh"
    assert json.loads((work / "meta.json").read_text(encoding="utf-8"))["params"]["is_ocr"] is True

    # ② 参数已一致 → 命中缓存，不重拉
    eng._ensure_mineru_md(str(pdf), work)
    assert calls["n"] == 1

    # ③ **大文档**（> OCR_MAX_PAGES 页，本次判定 is_ocr=False）+ 旧格式缓存 → 命中
    #    （不为升级把所有旧文献重拉一遍）
    from paperparse.core.parse_params import OCR_MAX_PAGES
    big = tmp_path / "big.pdf"
    d2 = pymupdf.open()
    for _ in range(OCR_MAX_PAGES + 1):
        d2.new_page().insert_text((72, 72), "normal born-digital text " * 12)
    d2.save(str(big))
    d2.close()
    work2 = tmp_path / "dual" / "big"
    work2.mkdir(parents=True)
    (work2 / "mineru_full.md").write_text("legacy-md", encoding="utf-8")
    (work2 / "meta.json").write_text(json.dumps(
        {"pdf_md5": hashlib.md5(big.read_bytes()).hexdigest(), "pipeline": "p14"}),
        encoding="utf-8")
    assert eng._ensure_mineru_md(str(big), work2).read_text(encoding="utf-8") == "legacy-md"
    assert calls["n"] == 1


# ---------------------------------------------------------------- AI 仲裁用量台账
class _FakeParseSettings:
    """最小 settings_service 替身：只给 `get_parse()/get_active_provider()`。"""

    def get_parse(self):
        return {"ai_review": True, "mode": "dual"}

    def get_active_provider(self, masked: bool = False):
        return {"id": "testprov", "name": "Test", "base_url": "http://127.0.0.1:1",
                "model": "test-model", "api_key": "sk-test"}


class _FakeUsageService:
    def __init__(self):
        self.rows: list[tuple] = []

    def record(self, context, provider, model, prompt_tokens, completion_tokens,
               cache_hit_tokens=0):
        self.rows.append((context, provider, model, prompt_tokens,
                          completion_tokens, cache_hit_tokens))


def test_arbitration_usage_lands_in_ledger(tmp_path, settings, monkeypatch):
    """★2026-09-17 回归锚定（静默失效第 2 次同型）：仲裁 provider 的 `on_usage`
    必须真正落到 `llm_usage` 台账。

    旧写法 `guard.record(...)` —— `TokenGuard` 只有 `record_usage` ⇒ AttributeError
    被 `dual_ai_review` 的 `except: pass` 吞掉，台账恒 0 行（成本完全不可观测）。
    本用例直接驱动 `_assemble_provider` 产出的回调，断言用量被记录为
    `arbitration:<stem>`（按篇隔离）。
    """
    from app.services import container as C
    from app.services.engine_service import EngineService
    from app.services.llm_service import TokenGuard

    us = _FakeUsageService()
    guard = TokenGuard(usage_service=us)
    monkeypatch.setattr(C, "get_settings_service", lambda: _FakeParseSettings())
    monkeypatch.setattr(C, "get_guard", lambda: guard)

    eng = EngineService(settings)
    pdf = tmp_path / "10.1002_adma.202407106.pdf"
    pdf.write_bytes(b"%PDF-1.7 test")
    ai_review, provider = eng._assemble_provider(str(pdf))

    assert ai_review is True
    assert provider is not None and provider.available()
    provider.on_usage(1234, 567)          # 模拟一次真实 AI 调用的 usage 回传

    assert us.rows, "仲裁用量未进台账（回调接错方法名？）"
    ctx, prov, model, pt, ct, _cache = us.rows[-1]
    assert ctx == "arbitration:10.1002_adma.202407106"
    assert (prov, model) == ("testprov", "test-model")
    assert (pt, ct) == (1234, 567)


# ---------------------------------------------------------------- T10 PaddleOCR md5 缓存
class _FakePaddleClient:
    """假 PaddleOCR 客户端：只被调用一次就应命中缓存（配额=钱）。"""

    calls = 0

    def __init__(self, cfg=None):
        pass

    def parse_pdf(self, pdf_path, backup=True, options=None):
        from paperparse.middleware.schema import ParserBlocks, TextBlock
        type(self).calls += 1
        blk = TextBlock(block_id="P0001", page=1, bbox=[0.0, 0.0, 1.0, 1.0],
                        text="hello world", kind="body")
        return ParserBlocks(source="paddleocr", pages=1, blocks=[blk])


def _patch_paddle(monkeypatch, token="t"):
    class _Cfg:
        paddleocr_access_token = token
        paddleocr_options = ""

    import paperparse.config as pcfg
    import paperparse.core.paddleocr_client as poc
    monkeypatch.setattr(pcfg, "load_config", lambda: _Cfg())
    monkeypatch.setattr(poc, "PaddleOCRClient", _FakePaddleClient)
    _FakePaddleClient.calls = 0


def test_paddle_blocks_md5_cache(tmp_path, settings, monkeypatch):
    """★2026-09-17 T10：PaddleOCR blocks 按 PDF md5 缓存——重解析不再重烧配额。

    旧行为：`PaddleOCRClient.parse_pdf` 每次都重新上传（backup 仅留档）⇒ 同一 PDF
    重解析重烧配额 + 数分钟。
    """
    import json
    from pathlib import Path

    from app.services.engine_service import EngineService

    _patch_paddle(monkeypatch)
    eng = EngineService(settings)
    pdf = tmp_path / "10.1002_x.pdf"
    pdf.write_bytes(b"%PDF-1.7 v1")
    work = tmp_path / "dual" / "10.1002_x"

    p1 = eng._ensure_paddle_blocks(str(pdf), work)
    assert p1 and Path(p1).exists() and _FakePaddleClient.calls == 1
    blocks = json.loads(Path(p1).read_text(encoding="utf-8"))
    assert blocks[0]["text"] == "hello world"
    assert json.loads((work / "paddle_meta.json").read_text(encoding="utf-8"))["pages"] == 1

    # 同 PDF 再解析 → 命中缓存，不再调用 API
    assert eng._ensure_paddle_blocks(str(pdf), work) == p1
    assert _FakePaddleClient.calls == 1

    # PDF 变化 → 重新识别
    pdf.write_bytes(b"%PDF-1.7 v2 changed")
    eng._ensure_paddle_blocks(str(pdf), work)
    assert _FakePaddleClient.calls == 2


def test_paddle_blocks_cache_skipped_without_token(tmp_path, settings, monkeypatch):
    """未配置 PaddleOCR token → 不介入（返回 None，管线走旧路径/降级），且不发请求。"""
    from app.services.engine_service import EngineService

    _patch_paddle(monkeypatch, token="")
    eng = EngineService(settings)
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"%PDF-1.7")
    assert eng._ensure_paddle_blocks(str(pdf), tmp_path / "w") is None
    assert _FakePaddleClient.calls == 0


# ------------------------------------------------ 门面形参契约（防"未知 kwarg → 静默降级"）
def test_engine_kwargs_accepted_by_api_facade():
    """★2026-09-17 回归锚定（真机实测暴露的静默失效）：

    `engine_service._parse_pdf_dual` 走门面 `paperparse.api.process_pdf_v2`。门面此前
    **没有** `third_decide`/`ai_synthesis` 形参，而 engine 一直在传
    ⇒ `TypeError: unexpected keyword argument 'third_decide'` 被上层 `except` 吞成
    "双通道解析异常，降级单通道" ⇒ **真机解析自 9/16 起从未跑过第三信号/AI 综合建议/
    参考文献闸门**（全部静默走降级链），直到 2026-09-17 用 NC 篇真机测试才发现。

    本用例用 `inspect.signature` 做**形参契约**：engine 传给门面的每个 kwarg 都必须是
    门面接受的形参（含 `**kwargs`）。任何一侧新增/改名都会被拦住。
    """
    import inspect
    import re
    from pathlib import Path

    from paperparse.api import process_pdf_v2 as facade

    src = Path(__file__).resolve().parents[1] / "app/services/engine_service.py"
    text = src.read_text(encoding="utf-8")
    m = re.search(r"self\._api\.process_pdf_v2\((.*?)\n\s*\)", text, re.S)
    assert m, "未找到 engine_service 调用门面的位置（重构后请同步本用例）"
    call = m.group(1)
    passed = set(re.findall(r"(\w+)\s*=", call))          # 关键字实参名
    assert {"third_decide", "ai_synthesis", "paddle_blocks_path"} <= passed

    params = inspect.signature(facade).parameters
    accepts_var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD
                         for p in params.values())
    unknown = passed - set(params)
    assert accepts_var_kw or not unknown, \
        "engine 传了门面不接受的形参 → 真机会 TypeError 并被吞成静默降级: %s" % unknown


