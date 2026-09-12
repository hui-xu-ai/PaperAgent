# -*- coding: utf-8 -*-
"""M2 真实链路：正式导入 data/journals.db + 真实文献（bib 已入库）评分验证。"""
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/paperkb"))

from paperkb import api
from paperkb.config import Roots

XLSX = Path(r"用户提供的文献\影响因子和分区\JCR分区.xlsx")
roots = Roots(data_dir=Path("data"), library_dir=Path("library"), kb_dir=Path("knowledge_base"))
api.init_kb(roots)

# ISSN 匹配验证（真实 bib 期刊：SCIENCE CHINA-MATERIALS ISSN 2095-8226）
hit = api.journals_lookup_issn("2095-8226")
print("ISSN 2095-8226 ->", (hit or {}).get("jcr", {}).get("journal_name"), "Q:", (hit or {}).get("jcr", {}).get("quartile"))

t0 = time.time()
r = api.journals_import(XLSX)
print("import:", {k: r[k] for k in ("jcr_imported", "cas_imported")}, "耗时 %.1fs" % (time.time() - t0))

# 真实文献评分（M1 已导入 savedrecs_1.bib 到 data/paperagent.db）
s = api.value_score_for("10.1007/s40843-026-4226-3")
print("score:", s)
print("done")
