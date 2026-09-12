# -*- coding: utf-8 -*-
"""P2-11 迁移：消除 md5/run_id 前缀文件名与 paper_<hash> 目录。
- input/<run_id>/<原始文件名>.pdf（新上传方案）；旧带前缀文件移入对应 run 子目录
- library/paper_<hash> → 无 DOI 论文改回 PDF 文件名目录
- document.json.source_pdf 与 DB pdf_path/doc_json 同步
"""
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, r"D:\Python\DeepSeek\PaperAgent\backend")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.config import get_settings  # noqa: E402
from app.services.store import Store  # noqa: E402
from paperparse.core.document_builder import output_dir_name  # noqa: E402

root = Path(r"D:\Python\DeepSeek\PaperAgent")
s = get_settings()
store = Store(s.db_path)
input_root = Path(s.engine_input_root)
lib_root = Path(s.engine_work_root)


def sanitize_stem(name: str) -> str:
    name = Path(name).stem
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    name = re.sub(r"[^\w.\-]", "_", name)
    return name[:120] or "paper"


moved_pdfs, moved_dirs = [], []

for p in store.list_papers():
    pid = p["id"]
    pdf_name = p.get("pdf_name") or p.get("title") or ""
    stem = sanitize_stem(pdf_name)
    old_pdf = Path(p.get("pdf_path", ""))
    run_id = p.get("run_id") or (old_pdf.parent.name if old_pdf.parent != input_root else "")
    if not run_id:
        run_id = "legacy-%d" % pid

    # 1) 带前缀/散列的 PDF → input/<run_id>/<原始文件名>.pdf
    if old_pdf.exists() and old_pdf.stem != stem:
        new_pdf = input_root / run_id / (pdf_name or old_pdf.name)
        new_pdf.parent.mkdir(parents=True, exist_ok=True)
        if not new_pdf.exists():
            shutil.move(str(old_pdf), str(new_pdf))
            moved_pdfs.append(f"{old_pdf.name} -> {new_pdf.relative_to(input_root)}")
        old_pdf = new_pdf
    store.update_paper(pid, pdf_path=str(old_pdf))

    # 2) library 目录：paper_<hash> / run_xxx → DOI 或 PDF 文件名
    doc_json = p.get("doc_json") or ""
    if not doc_json:
        continue
    doc_p = Path(doc_json)
    if not doc_p.exists():
        continue
    doi = ""
    try:
        from paperparse.core.document_builder import load_document
        doc = load_document(str(doc_p))
        doi = doc.metadata.doi or ""
    except Exception:
        doc = None
    target = output_dir_name(doi or None, pdf_name)  # 用原始文件名（去扩展名）
    cur = doc_p.parent.parent
    if cur.name != target and re.match(r"^(paper_|run_)", cur.name):
        dest = lib_root / target
        if not dest.exists():
            shutil.move(str(cur), str(dest))
            moved_dirs.append(f"{cur.name} -> {target}")
            doc_p = dest / "intermediate" / doc_p.name
            store.update_paper(pid, doc_json=str(doc_p))
    # 3) document.json.source_pdf 同步
    try:
        d = json.loads(doc_p.read_text(encoding="utf-8"))
        if d.get("metadata", {}).get("source_pdf") != str(old_pdf):
            d["metadata"]["source_pdf"] = str(old_pdf)
            doc_p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print("  source_pdf 同步失败:", e)

print("=== 迁移结果 ===")
print("PDF 移动:", moved_pdfs or "无")
print("目录重命名:", moved_dirs or "无")
print("library 现状:", [d.name for d in lib_root.iterdir() if d.is_dir()])
print("input 顶层散列孤儿（未动）:", [
    f.name for f in input_root.iterdir()
    if f.is_file() and re.match(r"^[0-9a-f]{16,}\.pdf$", f.name)])
