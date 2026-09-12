# -*- coding: utf-8 -*-
"""metadata 骨架增强单测（P-ENHANCE R06）"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "paperparse"))

from paperparse.core.layout_skeleton import build_skeleton  # noqa: E402
from paperparse.core.metadata import enhance_with_skeleton  # noqa: E402
from paperparse.middleware.schema import ArticleMetadata  # noqa: E402

CORPUS_RAW = next(
    (p for p in [
        Path(__file__).resolve().parents[3] / "PaperAgent" / "learning_workspace" / "corpus" / "raw",
        Path(__file__).resolve().parents[4] / "learning_workspace" / "corpus" / "raw",
    ] if p.exists()),
    Path(__file__).resolve().parents[3] / "PaperAgent" / "learning_workspace" / "corpus" / "raw")


def _skeleton(title, author_line, keyword_line):
    """[局部] 合成骨架（items 顺序：title → 作者行 → 正文；Keywords 节）"""
    items = [
        {"type": "text", "text": title, "text_level": 1, "bbox": [80, 50, 500, 90], "page_idx": 0},
        {"type": "text", "text": author_line, "text_level": None, "bbox": [80, 95, 500, 120], "page_idx": 0},
        {"type": "text", "text": "Some abstract body text here.", "text_level": None,
         "bbox": [80, 130, 500, 160], "page_idx": 0},
        {"type": "text", "text": "1. Introduction", "text_level": 2,
         "bbox": [80, 200, 500, 220], "page_idx": 0},
        {"type": "text", "text": keyword_line, "text_level": None,
         "bbox": [80, 300, 500, 320], "page_idx": 3},
    ]
    return build_skeleton(items)


def test_title_from_skeleton():
    sk = _skeleton("A Great Paper Title on Advanced Materials", "Alice Smith, Bob Jones",
                   "kw1, kw2")
    meta = ArticleMetadata(title="Wrong Local Title")
    enhance_with_skeleton(meta, sk)
    assert meta.title == "A Great Paper Title on Advanced Materials"


def test_authors_with_superscripts():
    line = ("Jongkuk Ko<sup>1</sup>, Changhwan Kim<sup>2</sup>, "
            "June Huh<sup>1,4</sup>, Je-Sung Koh<sup>2</sup>\\*, Jinhan Cho<sup>1,5</sup>\\*")
    sk = _skeleton("Title", line, "kw1")
    meta = ArticleMetadata()
    enhance_with_skeleton(meta, sk)
    assert meta.authors == ["Jongkuk Ko", "Changhwan Kim", "June Huh",
                            "Je-Sung Koh", "Jinhan Cho"]


def test_authors_not_overwritten():
    sk = _skeleton("Title", "Alice Smith, Bob Jones", "kw1")
    meta = ArticleMetadata(authors=["Alice Smith", "Bob Jones"])
    enhance_with_skeleton(meta, sk)
    assert meta.authors == ["Alice Smith", "Bob Jones"]


def test_keywords_from_section_skips_refs():
    # Keywords 节 items 混入参考文献行（[26] ...），应跳过取关键词行
    items = [
        {"type": "text", "text": "Title", "text_level": 1, "bbox": [80, 50, 500, 90], "page_idx": 0},
        {"type": "text", "text": "Alice, Bob", "text_level": None, "bbox": [80, 95, 500, 120], "page_idx": 0},
        {"type": "text", "text": "Keywords", "text_level": 2, "bbox": [80, 300, 500, 320], "page_idx": 3},
        {"type": "ref_text", "text": "[26] X. Chen, Z. Hou, G. Li", "text_level": None,
         "bbox": [80, 330, 500, 350], "page_idx": 3},
        {"type": "text", "text": "artificial muscle, automated culture platform, ionic actuato",
         "text_level": None, "bbox": [80, 355, 500, 375], "page_idx": 3},
        {"type": "ref_text", "text": "[27] Z. Liu, Z. Zhao", "text_level": None,
         "bbox": [80, 380, 500, 400], "page_idx": 3},
    ]
    sk = build_skeleton(items)
    meta = ArticleMetadata()
    enhance_with_skeleton(meta, sk)
    assert "artificial muscle" in meta.keywords
    assert not any(k.startswith("[") for k in meta.keywords)


def test_keywords_truncated_removed():
    sk = _skeleton("Title", "Alice, Bob", "artificial muscle, laser-")
    meta = ArticleMetadata()
    enhance_with_skeleton(meta, sk)
    assert "laser-" not in meta.keywords


def test_real_scirobotics():
    f = CORPUS_RAW / "10.1126_scirobotics.abo6463" / "content_list.json"
    if not f.exists():
        return
    sk = build_skeleton(json.loads(f.read_text(encoding="utf-8")))
    meta = ArticleMetadata()
    enhance_with_skeleton(meta, sk)
    assert len(meta.title) > 30
    assert len(meta.authors) >= 10        # 11 位作者
    assert "Jongkuk Ko" in meta.authors


def test_real_adma_keywords():
    f = CORPUS_RAW / "10.1002_adma.202407106" / "content_list.json"
    if not f.exists():
        return
    sk = build_skeleton(json.loads(f.read_text(encoding="utf-8")))
    meta = ArticleMetadata()
    enhance_with_skeleton(meta, sk)
    assert len(meta.title) > 30
    assert "laser-" not in (meta.keywords or [])
    assert len(meta.authors) >= 10        # adma 11 位作者
