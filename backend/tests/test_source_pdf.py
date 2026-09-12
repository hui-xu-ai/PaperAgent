# -*- coding: utf-8 -*-
"""T4 回归：重新导入后 kb/<DOI>/source.pdf 丢失。

根因（a）：P14 的 to_article_document 把 document.json 的 metadata.source_pdf 存为
**裸文件名**（pdf.name），_inplace_export 原用 `Path(...).resolve().exists()` 相对 CWD
判断恒 False → library/<DOI>/source.pdf 从未生成 → kb 经 sync_source_to_kb 也随之缺失。

修复：_inplace_export 对 source.pdf 增加三级兜底定位（原值 → 相对解析 → 按 basename
在 engine_input_root 暂存与规范库内 rglob），保证 library 始终有 source.pdf。
本测试验证：
  1) 裸文件名 source_pdf + input/<run_id>/<原始名>.pdf 暂存 → library 生成 source.pdf（字节一致）；
  2) 已被清理的绝对路径 source_pdf + 同名暂存 → 仍能兜底生成；
  3) sync_source_to_kb（_assemble_kb 实际调用链）→ kb/<DOI>/source.pdf 生成。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from conftest import ENGINE_DOC

# metadata.doi（带斜杠，合法 DOI）；library 下篇目录用下划线目录名（引擎/paperkb 统一规则）
DOI = "10.1002/adma.202407106"
DOI_DIR = "10.1002_adma.202407106"


def _prepare_doc(settings, src_pdf: str) -> Path:
    """把 fixture document.json 拷到 library/<DOI_DIR>/intermediate/，并把 metadata.source_pdf
    改写成给定值（模拟真实重新导入产物的 source_pdf），返回该 document.json 路径。"""
    lib = Path(settings.engine_work_root)
    doc_dir = lib / DOI_DIR / "intermediate"
    doc_dir.mkdir(parents=True, exist_ok=True)
    doc = doc_dir / "document.json"
    shutil.copy2(ENGINE_DOC, doc)
    data = json.loads(doc.read_text(encoding="utf-8"))
    data["metadata"]["source_pdf"] = src_pdf
    doc.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return doc


def _stage_input_pdf(settings, name: str, content: bytes) -> Path:
    """模拟 _save_upload：input/<run_id>/<原始文件名>.pdf。"""
    run_root = Path(settings.engine_input_root) / "abcd1234"
    run_root.mkdir(parents=True, exist_ok=True)
    staged = run_root / name
    staged.write_bytes(content)
    return staged


def test_inplace_export_bare_name_recovers_source_pdf(settings):
    """裸文件名 source_pdf（p14 现状）→ input/<run_id>/<原名>.pdf 兜底 → library 生成。"""
    from app.services.engine_service import EngineService

    eng = EngineService(settings)
    src_name = "10.1002_adma.202407106.pdf"
    pdf_bytes = b"%PDF-1.4 fake staged source\n"
    _stage_input_pdf(settings, src_name, pdf_bytes)
    # 模拟 p14 把 metadata.source_pdf 存成裸文件名（相对 CWD 不可达）
    doc = _prepare_doc(settings, src_name)

    r = eng._inplace_export(doc)
    out = Path(r["output_dir"])
    dest = out / "source.pdf"
    assert dest.exists(), "library/<DOI>/source.pdf 未生成（回归未修复）"
    assert dest.read_bytes() == pdf_bytes, "source.pdf 内容与暂存 PDF 不一致"
    assert r["pdf"] == str(dest)


def test_inplace_export_cleaned_abs_path_recovers_source_pdf(settings):
    """已被清理的绝对路径 source_pdf（fixture 场景）→ 按 basename 兜底 → library 生成。"""
    from app.services.engine_service import EngineService

    eng = EngineService(settings)
    src_name = "4c44cd2a4f3e4be2b39ee99264fc1a4d.pdf"
    pdf_bytes = b"%PDF-1.4 cleaned-abs path fallback\n"
    _stage_input_pdf(settings, src_name, pdf_bytes)
    # 绝对路径指向不存在的文件（input/ 曾被清理/改目录布局）
    doc = _prepare_doc(settings, f"C:\\work\\paperagent\\input\\{src_name}")

    r = eng._inplace_export(doc)
    dest = Path(r["output_dir"]) / "source.pdf"
    assert dest.exists()
    assert dest.read_bytes() == pdf_bytes


def test_sync_source_to_kb_after_export(settings):
    """_inplace_export 生成 library source.pdf 后，sync_source_to_kb（_assemble_kb 调用链）
    应同步 kb/<DOI>/source.pdf。"""
    from app.services.engine_service import EngineService
    from paperkb.config import Roots
    from paperkb.imports import sync_source_to_kb

    eng = EngineService(settings)
    src_name = "10.1002_adma.202407106.pdf"
    _stage_input_pdf(settings, src_name, b"%PDF-1.4 kb-sync source\n")
    doc = _prepare_doc(settings, src_name)
    eng._inplace_export(doc)

    kb_dir = Path(settings.engine_work_root).parent / "kb" / DOI_DIR
    roots = Roots(data_dir=Path(settings.engine_work_root).parent / "data",
                  library_dir=Path(settings.engine_work_root),
                  kb_dir=Path(settings.engine_work_root).parent / "kb")
    roots.ensure()
    result = sync_source_to_kb(DOI, roots, force=False)

    assert "source.pdf" in result["copied"], result
    assert (kb_dir / "source.pdf").exists(), "kb/<DOI>/source.pdf 未同步（回归未修复）"


# ── 用户反馈 2026-09-12：parse / parse_compile 模式不产 library/source.pdf ──
# 现象：知识库详情「原文层四件」里 en.md/document.json/images ✅ 而 source.pdf ❌。
# 根因：唯一写 library/source.pdf 的 `_inplace_export` 只被 full 模式的 export() 调用。
# 修法：`ensure_source_pdf`（幂等、不渲染变体）在 parse 分支补齐；「重新同步」也会先补。

def _plain_doc(settings, src_pdf: str) -> Path:
    """在 library/<DOI_DIR>/ 放一份最小 document.json（metadata.source_pdf = 给定值）。"""
    lib = Path(settings.engine_work_root) / DOI_DIR
    lib.mkdir(parents=True, exist_ok=True)
    doc = lib / "document.json"
    doc.write_text(json.dumps({"metadata": {"doi": DOI, "source_pdf": src_pdf}},
                              ensure_ascii=False), encoding="utf-8")
    return doc


def test_ensure_source_pdf_backfills_from_explicit_path(settings):
    """显式给 papers.pdf_path（权威原始上传 PDF）→ 幂等补出 library/source.pdf，字节一致。"""
    from app.services.engine_service import EngineService

    staged = _stage_input_pdf(settings, "uploaded.pdf", b"%PDF-1.7 real bytes\n")
    doc = _plain_doc(settings, "uploaded.pdf")
    lib = doc.parent
    out = EngineService(settings).ensure_source_pdf(doc, str(staged))

    assert out, "应补出 source.pdf"
    assert Path(out) == lib / "source.pdf"
    assert Path(out).read_bytes() == b"%PDF-1.7 real bytes\n"
    assert not (lib / "en_zh.md").exists(), "补齐不该渲染任何变体（那是 export() 的职责）"
    assert not (lib / "zh.md").exists()


def test_ensure_source_pdf_is_idempotent(settings):
    """已有 source.pdf → 原样返回、**不覆盖**（不能把已有源 PDF 换掉）。"""
    from app.services.engine_service import EngineService

    staged = _stage_input_pdf(settings, "uploaded.pdf", b"NEW\n")
    doc = _plain_doc(settings, "uploaded.pdf")
    lib = doc.parent
    (lib / "source.pdf").write_bytes(b"OLD\n")

    out = EngineService(settings).ensure_source_pdf(doc, str(staged))
    assert Path(out).read_bytes() == b"OLD\n"


def test_ensure_source_pdf_falls_back_to_metadata(settings):
    """不给显式路径时退回 document.json 的 metadata.source_pdf（含 basename 三级兜底）。"""
    from app.services.engine_service import EngineService

    _stage_input_pdf(settings, "10.1002_adma.202407106.pdf", b"FALLBACK\n")
    doc = _plain_doc(settings, "10.1002_adma.202407106.pdf")
    out = EngineService(settings).ensure_source_pdf(doc, None)

    assert out and Path(out).read_bytes() == b"FALLBACK\n"


def test_ensure_source_pdf_returns_empty_when_no_source(settings):
    """定位不到源 PDF → 返回 ""（不抛、也不建空文件）。"""
    from app.services.engine_service import EngineService

    doc = _plain_doc(settings, "")
    lib = doc.parent
    assert EngineService(settings).ensure_source_pdf(doc, None) == ""
    assert not (lib / "source.pdf").exists()
