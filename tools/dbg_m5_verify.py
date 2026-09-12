# -*- coding: utf-8 -*-
"""M5 真实链路验证（dbg）：真实 cej 产物一致性校验 + 真实 en.md 模拟导入。

- verify: 真实 library/<DOI>/（解析链产物：装饰标题/References/图/公式）
- import: 真实 en.md 内容 → 模拟导入（temp roots）→ 校验通过
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "paperkb"))

from paperkb.config import Roots  # noqa: E402
from paperkb.imports import (import_markdown, segment_markdown,  # noqa: E402
                             sync_source_to_kb, verify_kb_doc)

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "library"
DOI = "10.1016/j.cej.2025.167798"
DIR = "10.1016_j.cej.2025.167798"

# ---- 1) 真实解析产物一致性校验（library 侧）----
roots = Roots(data_dir=ROOT / "data", library_dir=LIB, kb_dir=ROOT / "knowledge_base")
v = verify_kb_doc(DOI, roots, base="library")
print(f"[verify-library] ok={v['ok']} expected={v['expected_count']} found={v['found_count']}")
if v["mismatches"]:
    for m in v["mismatches"][:5]:
        print("   MISMATCH", m["para_id"], repr(m["expected"][:60]), "|", repr(m["found"][:60]))
    sys.exit(1)

# ---- 2) 真实 en.md 内容 → 模拟导入（temp roots，含同步 kb + 校验）----
en_md = (LIB / DIR / "en.md").read_text(encoding="utf-8")
with tempfile.TemporaryDirectory() as td:
    t = Path(td)
    tmp_roots = Roots(data_dir=t / "data", library_dir=t / "library",
                      kb_dir=t / "kb").ensure()
    md = t / "10.1016_j.cej.2025.167798.md"
    md.write_text(en_md, encoding="utf-8")
    doc = segment_markdown(en_md, doi=DOI)
    print(f"[segment] paragraphs={len(doc['paragraphs'])} sections={len(doc['sections'])} "
          f"title={doc['metadata']['title'][:50]!r}")
    r = import_markdown(md, tmp_roots, doi=DOI)
    print(f"[import] {r['doi']} paras={r['paragraphs']}")
    synced = sync_source_to_kb(DOI, tmp_roots)
    print(f"[sync] copied={synced['copied']} verify_ok={synced['verify']['ok']}")
    if not synced["verify"]["ok"]:
        for m in synced["verify"]["mismatches"][:5]:
            print("   MISMATCH", m["para_id"], repr(m["expected"][:60]), "|", repr(m["found"][:60]))
        sys.exit(1)
    # 校验新产物 en.md（真实文件）↔ document.json 完全一致
    v2 = verify_kb_doc(DOI, tmp_roots, base="kb")
    print(f"[verify-import] ok={v2['ok']} expected={v2['expected_count']} found={v2['found_count']}")
    assert v2["ok"], v2
print("ALL REAL-CHAIN CHECKS PASSED")
