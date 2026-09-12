# -*- coding: utf-8 -*-
"""P2-6 迁移：恢复原始 PDF 文件名（历史 input/<run_id>.pdf → 有意义文件名），
并据此重命名 library/kb 目录（消除 run_xxx / paper_<hash> / 32位hex 目录名）。

规则：目录名 = 有效 DOI → DOI；否则净化后的**原始 PDF 文件名**（去扩展名）。
"""
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


def sanitize_stem(name: str) -> str:
    name = Path(name).stem
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    name = re.sub(r"[^\w.\-]", "_", name)
    return name[:120] or "paper"


def safe_input_name(input_root: Path, pdf_name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", pdf_name).strip(" .") or "paper.pdf"
    p = input_root / safe
    if p.exists():
        safe = f"{p.stat().st_mtime_ns % 100000}_{safe}"
    return safe


s = get_settings()
store = Store(s.db_path)
input_root = Path(s.engine_input_root)
lib_root = Path(s.engine_work_root)
kb_root = Path(s.engine_work_root).parent / "knowledge_base"

papers = store.list_papers(limit=500)
moved_inputs, moved_libs, moved_kbs = [], [], []

for p in papers:
    pid = p["id"]
    pdf_name = (p.get("pdf_name") or p.get("title") or "").strip()
    if not pdf_name:
        continue
    stem = sanitize_stem(pdf_name)

    # 1) input 文件重命名（原始文件名）
    old_pdf = Path(p.get("pdf_path", ""))
    if old_pdf.exists() and old_pdf.stem != stem:
        new_name = safe_input_name(input_root, pdf_name)
        new_pdf = input_root / new_name
        shutil.move(str(old_pdf), str(new_pdf))
        store.update_paper(pid, pdf_path=str(new_pdf))
        moved_inputs.append(f"{old_pdf.name} -> {new_name}")
        old_pdf = new_pdf
    store.update_paper(pid, pdf_name=pdf_name)

    # 2) library 目录重命名（目标 = DOI 或文件名）
    doc_json = p.get("doc_json") or ""
    if not doc_json:
        continue
    doc_p = Path(doc_json)
    if not doc_p.exists():
        continue
    doi = ""
    try:
        from paperparse.core.document_builder import load_document
        doi = load_document(str(doc_p)).metadata.doi or ""
    except Exception:
        doi = ""
    target = output_dir_name(doi, pdf_name)  # 用原始文件名（不是 input 路径）
    cur = doc_p.parent.parent  # library/<old>/
    if cur.name != target and cur.name not in (stem, target):
        dest = lib_root / target
        if not dest.exists():
            shutil.move(str(cur), str(dest))
            moved_libs.append(f"{cur.name} -> {target}")
            store.update_paper(pid, doc_json=str(dest / "intermediate" / doc_p.name))
            doc_p = dest / "intermediate" / doc_p.name

    # 3) kb 目录重命名（同名规则）
    if kb_root.is_dir():
        for kb_dir in kb_root.iterdir():
            if kb_dir.is_dir() and kb_dir.name in (cur.name, str(Path(pdf_name).stem), pdf_name):
                kb_target = output_dir_name(doi, pdf_name)
                if kb_dir.name != kb_target:
                    kb_dest = kb_root / kb_target
                    if not kb_dest.exists():
                        shutil.move(str(kb_dir), str(kb_dest))
                        moved_kbs.append(f"{kb_dir.name} -> {kb_target}")

print("=== 迁移结果 ===")
print("input 重命名:", moved_inputs or "无")
print("library 目录:", moved_libs or "无")
print("kb 目录:", moved_kbs or "无")
print("孤儿目录（未动，需人工确认）:", [
    d.name for d in lib_root.iterdir() if d.is_dir()
    and not any(p.get("doc_json") and Path(p["doc_json"]).parent.parent == d
                for p in papers)])
