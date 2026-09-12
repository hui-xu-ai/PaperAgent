# -*- coding: utf-8 -*-
"""M3 真实链路准备：cej 篇 kb 纳入（document.json 复制 + 元数据导入，模拟 M5 纳入）。

只做数据准备，不调 LLM（编译走 backend API 验证）。
"""
import json
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/paperkb"))

from paperkb import api
from paperkb.config import Roots
from paperkb.models import PaperMeta

DOI = "10.1016/j.cej.2025.167798"
roots = Roots(data_dir=Path("data"), library_dir=Path("library"),
              kb_dir=Path("knowledge_base"))
api.init_kb(roots)

# 1) kb 纳入：document.json 复制（M5 逻辑）
src = Path("library/10.1016_j.cej.2025.167798/document.json")
dst = Path("knowledge_base/10.1016_j.cej.2025.167798/document.json")
dst.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(src, dst)
print("kb document.json:", dst.exists())

# 2) 元数据导入（无 bib → 从 document.json metadata 构造，source_file="" 标记无 bib）
data = json.loads(src.read_text(encoding="utf-8", errors="replace"))
md = data.get("metadata") or {}
meta = PaperMeta(
    doi=md.get("doi") or DOI,
    title=md.get("title") or "",
    abstract=md.get("abstract") or "",
    authors=md.get("authors") or [],
    journal=md.get("journal") or "",
    year=str(md.get("year") or ""),
    keywords=md.get("keywords") or [],
    source_file="",  # 无 bib
)
api._need_store().upsert_meta(meta)  # noqa: SLF001
print("meta:", meta.doi, "|", meta.title[:40])

# 3) 评分（无 bib → 缺失归一化；期刊名匹配 journals.db）
s = api.value_score_for(DOI)
print("score:", s)
print("done")
