# -*- coding: utf-8 -*-
"""P2-V 辅助：把实测解析产物登记进应用 DB，GUI 打开即可看到（不重复解析）。"""
import hashlib
import sys

sys.path.insert(0, r"D:\Python\DeepSeek\PaperAgent\backend")

from app.config import get_settings  # noqa: E402
from app.services.store import Store  # noqa: E402

PDF = r"D:\Python\DeepSeek\PaperAgent\10.1002_adma.202407106.pdf"
DOC_JSON = (r"D:\Python\DeepSeek\PaperAgent\library\10.1002_adma.202407106"
            r"\intermediate\document.json")

s = get_settings()
store = Store(s.db_path)
md5 = hashlib.md5(open(PDF, "rb").read()).hexdigest()
existing = store.find_paper_by_md5(md5)
if existing:
    print("已在库（跳过）: paper_id =", existing["id"],
          "| status =", existing["status"], "| parse_source =", existing.get("parse_source"))
else:
    pid = store.create_paper(
        PDF,
        title=("Reinforced Magnetic-Responsive Electro-Ionic Artificial Muscles "
               "by 3D Laser-Induced Graphene Nano-Heterostructures"),
        pdf_md5=md5)
    store.update_paper(
        pid, doc_json=DOC_JSON, status="parsed", pipeline_mode="parse",
        parse_source="mineru-v4", run_id="verify-dde6ab7f")
    print("已登记: paper_id =", pid, "| parse_source = mineru-v4")
