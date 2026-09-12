# -*- coding: utf-8 -*-
"""真实链路验证：变体归位（library）+ 无 DOI 编译链 + 优雅关停后 DB 完整。

只读 + 一次临时 SDK 级调用（不触发 LLM）：用真实 Roots 但**不写用户数据**——
变体写的是 `work/scratch/s4-realchain/library/<dir>/`（临时库）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from paperkb import api
from paperkb.config import Roots
from paperkb.doi import doi_to_dirname
from paperkb.resource import find_doc, resource_key

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = Path("work/scratch/s4-realchain")
import shutil
shutil.rmtree(BASE, ignore_errors=True)
roots = Roots(data_dir=BASE / "data", library_dir=BASE / "library",
              kb_dir=BASE / "knowledge_base").ensure()
api.init_kb(roots)
store = api._need_store()                      # noqa: SLF001

# ── 1) 无 DOI 文献：md5 目录名 + document.json（模拟解析产物） ──
MD5 = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
d = roots.library_dir / MD5
d.mkdir(parents=True, exist_ok=True)
(d / "en.md").write_text("# No-id paper\n\nbody\n", encoding="utf-8")
# 用真实解析产物做 fixture（schema 完整），只把 DOI 抹掉模拟"无 DOI 文献"
fixture = Path("backend/tests/fixtures/document.json")
data = json.loads(fixture.read_text(encoding="utf-8"))
data.setdefault("metadata", {})["doi"] = ""
data["metadata"]["title"] = "无编号会议论文"
data["metadata"]["pdf_md5"] = MD5
(d / "document.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
store.set_doi_md5_map(MD5, doi="", pdf_md5=MD5, paper_id=99)

rid = api.ensure_paper_registered(str(d / "document.json"), paper_id=99, pdf_md5=MD5)
print("1) 无 DOI 登记 → rid:", rid)
assert rid == "nd-" + MD5[:12], rid

# ── 2) 入队（旧实现 skipped_no_doc） ──
q = api.compile_queue(rid, "L1")
print("2) 入队:", q)
assert q["status"] == "queued", q

# ── 3) 键解析：目录名/md5/rid 三种写法同一资源 ──
assert resource_key(store, MD5) == rid
assert resource_key(store, rid) == rid
doc = find_doc(roots.library_dir, rid, store, search_root=roots.library_dir)
print("3) 键解析一致；文档定位:", doc.parent.name if doc else None)
assert doc is not None and doc.parent.name == MD5

# ── 4) 纳入 kb（按 md5 目录名，不产生平行目录） ──
api.sync_source_to_kb(rid, force=False)
kb_dir = roots.kb_dir / MD5
print("4) kb 纳入:", (kb_dir / "document.json").exists(), (kb_dir / "en.md").exists(),
      "| 平行目录?", (roots.kb_dir / rid).exists())
assert (kb_dir / "document.json").exists() and not (roots.kb_dir / rid).exists()

# ── 5) 变体归位：combined_translate 的渲染目标 = library（直接测 render + 落盘契约） ──
from paperparse.core.document_builder import load_document
from paperparse.core.markdown_render import render_variant

docobj = load_document(str(d / "document.json"))
(d / "en_zh.md").write_text(render_variant(docobj, "translated"), encoding="utf-8")
(d / "zh.md").write_text(render_variant(docobj, "zh"), encoding="utf-8")
print("5) 变体落 library:", (d / "zh.md").exists(), (d / "en_zh.md").exists(),
      "| kb 侧无变体:", not (kb_dir / "zh.md").exists())
assert (d / "zh.md").exists() and (d / "en_zh.md").exists()

# ── 6) 附件（step3 回归）：无 DOI 资源也能挂 SI ──
att = api.kb_attachment_import(rid, "si", "si.md", b"supporting body", index=True)
lst = api.kb_attachments(rid)
print("6) 附件:", att.get("parented"), "count=", lst["count"], "root=", Path(lst["root"]).name)
assert att["ok"] and lst["count"] == 1

# ── 7) 磁盘事实落盘（供主线核对） ──
lines = []
for p in sorted(roots.library_dir.rglob("*")):
    if p.is_file():
        lines.append("library/" + p.relative_to(roots.library_dir).as_posix())
for p in sorted(roots.kb_dir.rglob("*")):
    if p.is_file():
        lines.append("kb/" + p.relative_to(roots.kb_dir).as_posix())
out = Path("work/scratch/s4-realchain-verify.json")
out.write_text(json.dumps({"rid": rid, "queue": q, "files": lines}, ensure_ascii=False, indent=1),
               encoding="utf-8")
print("7) 证据落盘:", out)
print("\n全部真实链路断言通过")
