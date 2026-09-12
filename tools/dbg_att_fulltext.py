# -*- coding: utf-8 -*-
"""真实链路自证：附件文本镜像进 fulltext_fts 后，中文关键词默认可召回。

临时 Roots（不碰真实知识库）：init_kb → 导入含中文长句的 SI（index=True）
→ 查 fulltext_fts 复合键 → 调 paperkb.api.recall() 打印命中条目。

运行（工作目录 = 项目根）：
  & '.venv\\Scripts\\python.exe' tools\\dbg_att_fulltext.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "paperkb"))

from paperkb.api import init_kb, kb_attachment_import, recall  # noqa: E402
from paperkb.config import Roots  # noqa: E402
from paperkb.db import KBStore  # noqa: E402
from paperkb.doi import doi_to_dirname, make_rid  # noqa: E402

DOI = "10.1002/dbg.202600001"
RID = make_rid("paper", doi=DOI)
DIRNAME = doi_to_dirname(DOI)
QUESTION = "那句话中文关键词"          # 必须是附件正文里的连续子串（trigram 短语匹配）
SI_TEXT = (
    "# 支撑信息\n\n"
    "补充实验表明，那句话中文关键词在附件里也必须被检索命中，"
    "否则用户的 SI 资料进不了知识库召回链路。\n"
)


def main() -> int:
    base = Path(__file__).resolve().parent.parent / "work" / "dbg-att-fulltext"
    shutil.rmtree(base, ignore_errors=True)     # 上一轮残留（sqlite 连接未关，Windows 下可能删不掉）
    roots = Roots(data_dir=base / "data", library_dir=base / "library",
                  kb_dir=base / "kb").ensure()
    init_kb(roots)
    (roots.library_dir / DIRNAME).mkdir(parents=True)
    out = kb_attachment_import(RID, "si", "si.md", SI_TEXT.encode("utf-8"), index=True)
    print(f"[0] 临时知识库：{base}")
    print(f"[1] 导入附件：ok={out['ok']} path={out['path']} indexed={out['indexed']}")

    store = KBStore(roots)
    keys = [r["doi"] for r in store.search_fulltext(QUESTION)]
    print(f"[2] fulltext_fts 命中键：{keys}")

    hits = recall(QUESTION)                 # 默认调用，不传 include_fulltext
    print(f"[3] api.recall({QUESTION!r}) 命中 {len(hits)} 条：")
    for it in hits:
        print(f"    - rid={it.get('rid')} file={it['file']} "
              f"source={it['source']} score={it['score']}")
        print(f"      snippet={it['snippet'][:80]!r}")
    ok = any(h["file"].startswith("attachments/") for h in hits)
    print(f"[4] 附件相对路径命中：{ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
