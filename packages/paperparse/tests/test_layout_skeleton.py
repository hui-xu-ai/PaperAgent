# -*- coding: utf-8 -*-
"""layout_skeleton 单测（P-ENHANCE R02 / S1.5）"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "paperparse"))

from paperparse.core.layout_skeleton import build_skeleton  # noqa: E402

CORPUS_RAW = Path(__file__).resolve().parents[3] / "learning_workspace" / "corpus" / "raw"


def _item(t, text, page=0, bbox=(100, 100, 500, 150), level=None):
    d = {"type": t, "text": text, "bbox": list(bbox), "page_idx": page}
    if level is not None:
        d["text_level"] = level
    return d


def _simple_items():
    return [
        _item("header", "Adv. Mater. 2024", page=0),
        _item("text", "A Great Paper Title Here", page=0, level=1, bbox=(100, 50, 500, 90)),
        _item("text", "Alice, Bob", page=0, bbox=(100, 95, 500, 120)),
        _item("text", "1. Introduction", page=0, level=2),
        _item("text", "This is the first paragraph of introduction.", page=0),
        _item("equation", "$E = mc^2$", page=0),
        _item("chart", "Figure 1. A plot.", page=0),
        _item("text", "method via cost-efective procedures is challenging.", page=0),
        _item("footer", "page 1 of 10", page=0),
        _item("ref_text", "[1] Some reference", page=1),
    ]


def test_filter_noise_and_roles():
    sk = build_skeleton(_simple_items())
    texts = [i.text for i in sk.items]
    assert "Adv. Mater. 2024" not in texts          # header 过滤
    assert "page 1 of 10" not in texts              # footer 过滤
    roles = {i.text: i.role for i in sk.items}
    assert roles.get("A Great Paper Title Here") == "title"
    assert roles.get("1. Introduction") == "heading"
    assert roles.get("This is the first paragraph of introduction.") == "body"
    assert roles.get("$E = mc^2$") == "equation"
    assert roles.get("Figure 1. A plot.") == "caption"   # chart 内题注 → caption
    assert roles.get("method via cost-efective procedures is challenging.") == "body"  # 续段防误判
    assert roles.get("[1] Some reference") == "reference"


def test_reading_order_and_sections():
    sk = build_skeleton(_simple_items())
    assert sk.stats["kept"] == 8
    assert sk.stats["filtered"] == 2
    assert len(sk.section_tree) == 1                 # 仅 Introduction 一节
    assert sk.section_tree[0]["heading"] == "1. Introduction"


def test_two_column_detection():
    items = []
    for p in range(3):
        for y in range(0, 900, 100):
            items.append(_item("text", "word%d" % y, page=p, bbox=(60, y, 300, y + 30)))
            items.append(_item("text", "word%d" % y, page=p, bbox=(400, y, 640, y + 30)))
    sk = build_skeleton(items)
    assert sk.is_two_column is True


def test_single_column_not_two():
    items = [_item("text", "line%d" % i, page=i // 10, bbox=(80, 100 + i, 500, 130 + i))
             for i in range(30)]
    sk = build_skeleton(items)
    assert sk.is_two_column is False


def test_title_only_once():
    items = _simple_items()
    items.insert(2, _item("text", "Another long first page block that is not the title",
                          page=0, bbox=(100, 100, 500, 130)))
    sk = build_skeleton(items)
    titles = [i for i in sk.items if i.role == "title"]
    assert len(titles) == 1


def test_real_adma():
    f = CORPUS_RAW / "10.1002_adma.202407106" / "content_list.json"
    if not f.exists():
        return
    sk = build_skeleton(json.loads(f.read_text(encoding="utf-8")))
    assert sk.is_two_column is False
    assert sk.stats["filtered"] > 50
    assert len([i for i in sk.items if i.role == "title"]) == 1
    assert len(sk.section_tree) >= 8
    # 续段不得出现在 heading 序列
    heads = [s["heading"] for s in sk.section_tree]
    assert not any(h.startswith("method via") for h in heads)


def test_real_scirobotics():
    f = CORPUS_RAW / "10.1126_scirobotics.abo6463" / "content_list.json"
    if not f.exists():
        return
    sk = build_skeleton(json.loads(f.read_text(encoding="utf-8")))
    assert sk.is_two_column is True
    assert sk.stats["roles"].get("equation", 0) >= 5
    assert len(sk.section_tree) >= 12
