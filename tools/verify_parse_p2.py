# -*- coding: utf-8 -*-
"""P2-V 实测：用修复后的 .env 真实解析一篇 PDF（parse-only，0 AI token）。

验证：1) 解析来源应为 mineru-v4（精准）；2) en.md 无 <!-- image --> 占位符；
      3) en.md 含图片行 ![](images/Fxxx.png)；4) document.json 已清洗。
"""
import logging
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, r"D:\Python\DeepSeek\PaperAgent\backend")
if hasattr(sys.stdout, "reconfigure"):  # GBK 控制台兜底，防 emoji 打印崩溃
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from app.config import get_settings  # noqa: E402
from app.services.engine_service import EngineService, parse_source_label  # noqa: E402

PDF = r"D:\Python\DeepSeek\PaperAgent\10.1002_adma.202407106.pdf"

s = get_settings()
print("[1] resolve_mineru_parser =", s.resolve_mineru_parser(),
      "| key_set =", bool(s.mineru_api_key))
assert s.resolve_mineru_parser() == "mineru-v4", "精准解析未启用！"

eng = EngineService(s)
run_id = "verify-" + uuid.uuid4().hex[:8]
print("[2] 开始真实解析（MinerU v4 云端，约 1-5 分钟）...")
r = eng.parse_pdf(PDF, run_id=run_id)  # parse-only，默认降级链
src = r.get("parse_source", "")
print("[3] parse_source =", src, "| label =", parse_source_label(src))

doc = Path(r["document_json"])
out_dir = doc.parent.parent
en_md = out_dir / (out_dir.name + ".en.md")
print("[4] out_dir =", out_dir)
print("[5] en.md exists =", en_md.exists())
if en_md.exists():
    text = en_md.read_text(encoding="utf-8")
    imgs = re.findall(r"!\[\]\(([^)]+)\)", text)
    print("[6] image_lines =", len(imgs), "| 前5个:", imgs[:5])
    print("[7] 占位符残留 =", "<!--" in text)
    print("[8] doc_json 占位符 =", "<!-- image" in doc.read_text(encoding="utf-8"))
    assert "<!--" not in text, "en.md 仍残留占位符"
    assert imgs, "en.md 无任何图片行"
    print("[9] 验证通过 ✅  en.md 前 300 字:",
          text[:300].replace("\n", " "))
else:
    print("[9] ❌ en.md 未生成")
    sys.exit(1)
