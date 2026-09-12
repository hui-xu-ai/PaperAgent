# -*- coding: utf-8 -*-
"""架构快照测试（2026-08-26）：锁定 P14 生产链模块与关键符号，
防未来"清理/重构"误删生产依赖；同时断言退役模块已归档（不存在于包内）。

生产链（P14 主链 + 老 parse 降级链 + M8 挖掘）：
- 主链：p14_pipeline/skeleton_local/md_align/repair_paragraphs/latex_normalize/
  para_verify/image_extract/document_builder/markdown_render/mineru_client/
  paddleocr_client/dual_ai_review/rule_mining/para_align/calibration_md
- 门面：paperparse.api.process_pdf_v2（P14 主入口）
"""
import importlib

PROD_MODULES = [
    "paperparse.core.p14_pipeline",
    "paperparse.core.skeleton_local",
    "paperparse.core.md_align",
    "paperparse.core.repair_paragraphs",
    "paperparse.core.latex_normalize",
    "paperparse.core.para_verify",
    "paperparse.core.image_extract",
    "paperparse.core.document_builder",
    "paperparse.core.markdown_render",
    "paperparse.core.mineru_client",
    "paperparse.core.paddleocr_client",
    "paperparse.core.dual_ai_review",
    "paperparse.core.rule_mining",
    "paperparse.core.para_align",
    "paperparse.core.calibration_md",     # 老 parse 降级链
    "paperparse.core.stitch_code",        # 老 parse 降级链
    "paperparse.middleware.schema",
]

# 已归档退役（2026-08-26）：P12 双通道/规则学习/HTML/硅基流动
RETIRED_MODULES = [
    "paperparse.core.dual_pipeline",
    "paperparse.core.dual_fuse",
    "paperparse.core.dual_align",
    "paperparse.core.self_learn",
    "paperparse.core.sf_ocr_client",
    "paperparse.core.html_elements",
    "paperparse.core.html_to_md",
    "paperparse.html",
]

# 瘦身移除（2026-08-27）：翻译/总结/网页往返/cost 等已被 paperkb 取代的 llm 模块
SLIMMED_LLM_MODULES = [
    "paperparse.llm.calibrate_translate",
    "paperparse.llm.cost",
    "paperparse.llm.journal",
    "paperparse.llm.latextap",
    "paperparse.llm.m5combined",
    "paperparse.llm.measure",
    "paperparse.llm.prompts",
    "paperparse.llm.summarize",
    "paperparse.llm.tracking",
    "paperparse.llm.web_roundtrip",
]


def test_prod_modules_importable():
    for mod in PROD_MODULES:
        importlib.import_module(mod)


def test_retired_modules_archived():
    for mod in RETIRED_MODULES:
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError:
            continue
        raise AssertionError("退役模块仍存在（应归档 archive/）: %s" % mod)


def test_slimmed_llm_modules_removed():
    """2026-08-27 瘦身：翻译/总结/网页往返等 llm 模块已被 paperkb 取代移除（client 保留）。"""
    from paperparse.llm import client  # noqa: F401  client 必须保留
    for mod in SLIMMED_LLM_MODULES:
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError:
            continue
        raise AssertionError("瘦身应移除的 llm 模块仍存在: %s" % mod)


def test_p14_entry_point_exists():
    from paperparse.api import process_pdf_v2
    assert callable(process_pdf_v2)


def test_key_symbols_present():
    from paperparse.core.p14_pipeline import (
        char_conflicts, _apply_arbitrations, _is_paddle_authoritative,
        _formula_equivalent, _sup_inline_refs,
    )
    assert callable(char_conflicts) and callable(_apply_arbitrations)
    assert callable(_is_paddle_authoritative) and callable(_formula_equivalent)
    assert callable(_sup_inline_refs)
