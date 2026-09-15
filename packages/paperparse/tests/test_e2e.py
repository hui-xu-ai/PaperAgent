#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_e2e.py
功能: 端到端验收测试：真实样本（PDF + 校准 MD）→ paper.md 结构断言
      （D9：只断言结构/统计，不读取/断言正文内容）
对外接口: 无（测试）
版本: v1.0.0 (2025-xx-xx)
版本历史:
  v1.0.0 初始版本
"""
from pathlib import Path

import pytest

import paperparse.api as api

SAMPLES = Path(__file__).resolve().parent / "samples"
PDF = SAMPLES / "10.1002_adma.202407106.pdf"
CALIB = SAMPLES / "10.1002_adma.202407106.md"


@pytest.mark.skipif(not PDF.exists(), reason="样本缺失")
def test_e2e_real_sample(tmp_work):
    result = api.convert_pdf(str(PDF), calibration_md=str(CALIB),
                             out_dir=str(tmp_work / "out"))
    assert result.status == "success", result.error
    assert result.stats.pages == 15
    assert result.stats.parser == "pymupdf"
    assert result.outputs.md
    assert result.outputs.images_dir

    md = Path(result.outputs.md).read_text(encoding="utf-8")

    # 结构断言（不读正文）
    assert md.startswith("---")                       # frontmatter
    assert "tags:" in md                              # Obsidian 全局搜索
    assert "> [!info]" in md                          # 文献信息 callout
    # 2026-09-16（用户指示）：删除 `> [!summary] AI 阅读总结 … 由 AI 总结阶段（M5）填充` 占位块
    # ——模板仅在 doc.ai_summary 非空时才输出该 callout ⇒ 此处断言占位文本不再出现。
    assert "由 AI 总结阶段（M5）填充" not in md, "无信息量的总结占位块必须已删除"
    assert "## References" in md                      # 参考文献
    assert "![](images/F" in md                       # 图片嵌入
    assert "<details>" not in md                      # 未翻译时无折叠（M5 才有）

    # 中间产物与监督文件
    doc_json = Path(result.outputs.document_json)
    assert doc_json.exists()
    assert (Path(result.outputs.images_dir) / "F001.png").exists()
    audit_dir = Path(result.outputs.audit_dir)
    assert (audit_dir / "events.jsonl").exists()
    assert (audit_dir / "report.html").exists()

    # 元数据关键字段（DOI/年份）
    import json
    doc = json.loads(doc_json.read_text(encoding="utf-8"))
    assert doc["metadata"]["doi"] == "10.1002/adma.202407106"
    assert doc["metadata"]["year"] == 2024
    assert len(doc["references"]) > 30
