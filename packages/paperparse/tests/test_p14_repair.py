# -*- coding: utf-8 -*-
"""P14-M6 单测：拼接修复（跨页/跨图断段合并）"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paperparse.core.repair_paragraphs import repair_md_paragraphs


class TestRepair:
    MD = """# Title

## 1. Introduction

The first paragraph about sensors is complete here. [1]

Polymer-based ionic sensor consists of layers. [12] However, the water
will evaporate (Fig. 1a)

during the working process and lead to the reduction. [22]

![](images/a.jpg)
Fig. 1. caption of figure one.

To avoid the problem of instability, ionic liquid is used. [26]

## 2. Results
"""

    def _skeleton(self):
        """本地骨架：跨页断段（"…will evaporate" + "during…" 是同一段）"""
        from paperparse.core.skeleton_local import LocalSkeleton, LocalPara, LocalLine
        lines = [
            LocalLine(line_id="L001", page=1, bbox=(37.6, 100, 300, 112),
                      text="1. Introduction", kind="heading"),
            LocalLine(line_id="L002", page=1, bbox=(49.5, 120, 300, 132),
                      text="The first paragraph about sensors is complete here. [1]",
                      kind="body"),
            LocalLine(line_id="L003", page=1, bbox=(49.5, 140, 300, 152),
                      text="Polymer-based ionic sensor consists of layers. [12] However, the water will evaporate (Fig. 1a)",
                      kind="body"),
            LocalLine(line_id="L004", page=2, bbox=(37.6, 100, 300, 112),
                      text="during the working process and lead to the reduction. [22]",
                      kind="body"),
            LocalLine(line_id="L005", page=2, bbox=(37.6, 200, 300, 212),
                      text="To avoid the problem of instability, ionic liquid is used. [26]",
                      kind="body"),
        ]
        # 本地骨架把 L003+L004 合并成一段（跨页续接）
        p1 = LocalPara(para_id="LP001", lines=[lines[1]], text=lines[1].text,
                       kind="body", closed=True, pages=[1])
        p2 = LocalPara(para_id="LP002", lines=[lines[2], lines[3]],
                       text=lines[2].text + " " + lines[3].text,
                       kind="body", closed=True, pages=[1, 2])
        p3 = LocalPara(para_id="LP003", lines=[lines[4]], text=lines[4].text,
                       kind="body", closed=True, pages=[2])
        return LocalSkeleton(lines=lines, paragraphs=[p1, p2, p3])

    def test_cross_page_merge(self):
        """跨页断段（"(Fig. 1a)" + "during…" 小写）→ 自动合并"""
        sk = self._skeleton()
        res = repair_md_paragraphs(self.MD, sk)
        merged = [r for r in res.paragraphs if r.source == "merged"]
        assert any("will evaporate" in r.text and "during the working" in r.text
                   for r in merged), "跨页断段应合并：'will evaporate'+'during…'"
        assert res.stats["merged"] >= 1

    def test_uppercase_next_not_merged(self):
        """下段大写开头 = 新段，不得合并（n_lower 恒真 bug 回归：norm 已全
        小写导致 islower() 恒 True → md#33 '…decreases with the' + md#44
        'In terms…' 被误合并）"""
        sk = self._skeleton()
        md = self.MD.replace(
            "during the working process and lead to the reduction. [22]",
            "In terms of the electrochemical performance, the CV curves")
        res = repair_md_paragraphs(md, sk)
        assert not any(r.source == "merged" and "will evaporate" in r.text
                       and "In terms" in r.text for r in res.paragraphs), \
            "下段大写开头（新段）不得合并"

    def test_image_marker_line_not_merged(self):
        """图注残留行（含图片标记 "d ![](images/…)"）不得与正文合并"""
        sk = self._skeleton()
        md = """# Title

## 1. Introduction

The first paragraph about sensors is complete here. [1]

Firstly, the effects of IL impregnation temperature was studied. [38]

![](images/a.jpg)
Fig. 1. caption of figure one.

d ![](images/b.jpg)

## 2. Results
"""
        res = repair_md_paragraphs(md, sk)
        assert not any(r.source == "merged" and "![" in r.text
                       for r in res.paragraphs), "含图片标记的段不得合并"

    def test_fake_heading_to_body(self):
        """假标题（"## increase of immersion time." = 本地 body 段尾续行）
        → 转 body 并并入前段"""
        sk = self._skeleton()
        # 本地骨架：LP002 含 "…decreases with the increase of immersion time."
        from paperparse.core.skeleton_local import LocalPara, LocalLine
        lp2 = next(p for p in sk.paragraphs if p.para_id == "LP002")
        lp2.text = lp2.text + " increase of immersion time."
        md = """# Title

## 1. Introduction

The first paragraph about sensors is complete here. [1]

After the optimum temperature was determined, the stiffness of IPS
decreases with the

## increase of immersion time.

To avoid the problem of instability, ionic liquid is used. [26]

## 2. Results
"""
        res = repair_md_paragraphs(md, sk)
        merged = [r for r in res.paragraphs if r.source == "merged"]
        assert any("decreases with the" in r.text
                   and "increase of immersion time." in r.text
                   for r in merged), "假标题应转 body 并与前段合并"
        assert not any("## " in r.text for r in res.paragraphs
                       if "immersion time" in r.text), "假标题前缀应剥离"

    def test_short_para_merge(self):
        """短段规则：前段字数 < 40 且本地同一段覆盖两者 → 合并（即使句子完整）"""
        sk = self._skeleton()
        from paperparse.core.skeleton_local import LocalLine, LocalPara
        l_short = LocalLine(line_id="L006", page=2, bbox=(37.6, 300, 300, 312),
                            text="Then the cation was fixed as EMIM, and the "
                                 "influence of the anion types on the "
                                 "performance of the IPS was studied.",
                            kind="body")
        l_long = LocalLine(line_id="L007", page=2, bbox=(37.6, 320, 300, 332),
                           text="The mass change ratio and stiffness of IPS "
                                "immersed in IL with different anions are "
                                "shown in Fig. 5a and b.", kind="body")
        sk.lines.extend([l_short, l_long])
        sk.paragraphs = [p for p in sk.paragraphs if p.para_id != "LP003"]
        sk.paragraphs.append(LocalPara(
            para_id="LP003", lines=[l_short, l_long],
            text=l_short.text + " " + l_long.text, kind="body",
            closed=True, pages=[2]))
        md = """# Title

## 1. Introduction

The first paragraph about sensors is complete here. [1]

Then the cation was fixed as EMIM, and the influence of the anion types on
the performance of the IPS was studied.

The mass change ratio and stiffness of IPS immersed in IL with different
anions are shown in Fig. 5a and b.

## 2. Results
"""
        res = repair_md_paragraphs(md, sk)
        merged = [r for r in res.paragraphs if r.source == "merged"]
        assert any("was studied." in r.text and "The mass change ratio" in r.text
                   for r in merged), "短段应合并到下一段"

    def test_sort_by_local_reading_order(self):
        """M6 排序（阶段8 修正）：排序键=本地阅读序查表（local_of_md 行区间
        重叠=位置证据）。Wiley 双栏错位场景：md 段序 A→B→C，本地骨架阅读序
        B→A→C（B 在左栏、A/C 在右栏）→ 排序后应 B→A→C。原实现全文覆盖矩阵
        60s，查表毫秒级；此测试同时作语义回归（adma 首页 B→A→C 的单测版）。"""
        from paperparse.core.skeleton_local import (LocalSkeleton, LocalPara,
                                                    LocalLine)
        l_b = LocalLine(line_id="L001", page=1, bbox=(37.6, 400, 300, 412),
                        text="The actuator consists of a Nafion membrane "
                             "sandwiched between two electrodes. [2]",
                        kind="body")
        l_a = LocalLine(line_id="L002", page=1, bbox=(318.6, 100, 600, 112),
                        text="Artificial muscles based on ionic polymer-metal "
                             "composites can generate large bending "
                             "deformation. [1]", kind="body")
        l_c = LocalLine(line_id="L003", page=1, bbox=(318.6, 140, 600, 152),
                        text="The ionic liquid is used as the solvent to "
                             "improve the stability. [3]", kind="body")
        # 本地阅读序 = B → A → C（Wiley 双栏：左栏 B 先读，右栏 A/C 后读）
        sk = LocalSkeleton(
            lines=[l_b, l_a, l_c],
            paragraphs=[
                LocalPara(para_id="LP001", lines=[l_b], text=l_b.text,
                          kind="body", closed=True, pages=[1]),
                LocalPara(para_id="LP002", lines=[l_a], text=l_a.text,
                          kind="body", closed=True, pages=[1]),
                LocalPara(para_id="LP003", lines=[l_c], text=l_c.text,
                          kind="body", closed=True, pages=[1]),
            ])
        md = """# Title

## 1. Introduction

Artificial muscles based on ionic polymer-metal composites can generate large bending deformation. [1]

The actuator consists of a Nafion membrane sandwiched between two electrodes. [2]

The ionic liquid is used as the solvent to improve the stability. [3]

## 2. Results
"""
        res = repair_md_paragraphs(md, sk)
        bodies = [r.text for r in res.paragraphs if r.kind == "body"]
        i_actuator = next(i for i, t in enumerate(bodies)
                          if t.startswith("The actuator"))
        i_muscles = next(i for i, t in enumerate(bodies)
                         if t.startswith("Artificial muscles"))
        i_ionic = next(i for i, t in enumerate(bodies)
                       if t.startswith("The ionic"))
        assert i_actuator < i_muscles < i_ionic, \
            "排序应按本地阅读序 B→A→C（md 原序 A→B→C）"

    def test_sort_unaligned_sinks(self):
        """M6 排序兜底：本地完全无锚的 md 段（line_map/local_of_md 均无，
        文本覆盖 <0.5）→ 排序沉底（宁缺毋滥，不误排到中间）"""
        from paperparse.core.skeleton_local import (LocalSkeleton, LocalPara,
                                                    LocalLine)
        l_b = LocalLine(line_id="L001", page=1, bbox=(37.6, 400, 300, 412),
                        text="The actuator consists of a Nafion membrane "
                             "sandwiched between two electrodes. [2]",
                        kind="body")
        sk = LocalSkeleton(
            lines=[l_b],
            paragraphs=[LocalPara(para_id="LP001", lines=[l_b], text=l_b.text,
                                  kind="body", closed=True, pages=[1])])
        md = """# Title

## 1. Introduction

The actuator consists of a Nafion membrane sandwiched between two electrodes. [2]

This is an entirely new paragraph which does not appear anywhere in the local skeleton at all. [9]

## 2. Results
"""
        res = repair_md_paragraphs(md, sk)
        bodies = [r.text for r in res.paragraphs if r.kind == "body"]
        assert bodies[-1].startswith("This is an entirely new"), \
            "本地无锚段应排序沉底（最后）"

    def test_frontmatter_not_merged(self):
        """作者/机构/Keywords 等 frontmatter 不做合并"""
        sk = self._skeleton()
        md = """# Title

## A B S T R A C T

Keywords: sensor liquid

## 1. Introduction

"""
        res = repair_md_paragraphs(md, sk)
        # 无正文断段 → 不应有合并
        assert res.stats["merged"] == 0 or all(
            r.source != "merged" for r in res.paragraphs
            if "Keywords" in r.text or "sensor liquid" in r.text)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
