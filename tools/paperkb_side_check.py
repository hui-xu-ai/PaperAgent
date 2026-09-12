# -*- coding: utf-8 -*-
"""磁盘侧校验辅助（CDP 实测用）：导入后检查 library/kb 四件 + 一致性。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "paperkb"))

from paperkb.config import Roots  # noqa: E402
from paperkb.imports import verify_kb_doc  # noqa: E402

DIR = "10.1063_1.5004573"


def check_import(doi: str = "10.1063/1.5004573") -> dict:
    root = Path(__file__).resolve().parents[1]
    roots = Roots(data_dir=root / "data", library_dir=root / "library",
                  kb_dir=root / "knowledge_base")
    lib = root / "library" / DIR
    kb = root / "knowledge_base" / DIR
    v = verify_kb_doc(doi, roots, base="kb")
    return {
        "library_doc": (lib / "document.json").exists(),
        "library_en": (lib / "en.md").exists(),
        "kb_doc": (kb / "document.json").exists(),
        "kb_en": (kb / "en.md").exists(),
        "verify_ok": v["ok"],
        "expected": v["expected_count"],
        "found": v["found_count"],
        "mismatches": len(v.get("mismatches", [])),
    }


if __name__ == "__main__":
    print(check_import())
