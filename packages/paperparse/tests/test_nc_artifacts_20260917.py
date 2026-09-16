# -*- coding: utf-8 -*-
"""★2026-09-17 NC 用户实测反馈的 3 类修复：
· P6 乱码：PDF 自定义字体把 `×` 映射成 U+0003、上标负号映射成 U+0002 ⇒ 产物里出现不可见控制字符
  （用户报："The 20  2.5 mm sized strips" 原文是 `20 × 2.5`）
· P1 首字母夸大写（drop cap）+ 首行小型大写被 MinerU 渲染成 `<sup>词</sup>` 序列
· P2 单字符冲突（`o`→`<`）因"片段多次出现"落不了地 ⇒ 上下文消歧
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paperparse.core.p14_pipeline import (        # noqa: E402
    normalize_text_artifacts, _locate_conflict,
)


class TestControlCharRepair:
    """P6：控制字符 → ×/⁻；其余控制字符清除"""

    def test_times_between_digits(self):
        t, info = normalize_text_artifacts("The 20 \x03 2.5 mm sized strips")
        assert t == "The 20 \u00d7 2.5 mm sized strips"
        assert info["times"] == 1 and info["ctrl_removed"] == 0

    def test_times_without_spaces(self):
        t, _ = normalize_text_artifacts("casted on a 7.5\x032.5 cm2 area")
        assert t == "casted on a 7.5 \u00d7 2.5 cm2 area"

    def test_superscript_minus(self):
        t, info = normalize_text_artifacts("(2.5 \x03 10 \x02 4 S cm \x02 1)")
        assert "2.5 \u00d7 10\u207b4 S cm\u207b1" in t
        assert info["times"] == 1 and info["exp_minus"] == 2

    def test_other_control_chars_removed(self):
        t, info = normalize_text_artifacts("soft\u00adhyphen zer\u200bwidth \x07bell\x04")
        assert t == "softhyphen zerwidth bell"
        assert info["ctrl_removed"] == 4

    def test_plain_text_untouched(self):
        src = "Normal sentence with 3 numbers and a 10–20 range."
        t, info = normalize_text_artifacts(src)
        assert t == src and not any(info.values())


class TestDropCapSmallCaps:
    """P1：drop cap + 小型大写首行（NC p2 实测 `E<sup>lectrochemical</sup> <sup>actuators</sup>…`）"""

    def test_dropcap_and_smallcaps_run(self):
        src = ("E<sup>lectrochemical</sup> <sup>actuators</sup> <sup>that</sup> "
               "<sup>can</sup> <sup>store</sup> electric energy and convert it into motion.")
        t, info = normalize_text_artifacts(src)
        assert t.startswith("Electrochemical actuators that can store electric energy")
        assert "<sup>" not in t
        assert info["dropcap"] == 1 and info["smallcaps"] == 4

    def test_real_superscripts_untouched(self):
        src = ("The value is 10<sup>-3</sup> as reported<sup>[12]</sup>, see also "
               "Supplementary Fig. 1<sup>a</sup>.")
        t, info = normalize_text_artifacts(src)
        assert t == src and not any(info.values())

    def test_single_sup_word_kept(self):
        src = "The 4<sup>th</sup> sample was tested."
        t, info = normalize_text_artifacts(src)
        assert t == src and info["smallcaps"] == 0


class TestConflictDisambiguation:
    """P2：单字符片段多次出现时定位唯一位置（NC `o` → `<`，实测 i1=844/807 与打分最优一致）"""

    def _para(self):
        return ("The pores smaller than o2 nm give a high SSA, and the hierarchical "
                "structure with dominant size o2 nm has a leading effect on the "
                "conductivity because o is a common letter in this sentence.")

    def test_unique_occurrence(self):
        t = "value at o2 nm is small"
        c = {"mineru": {"text": "o2"}, "paddleocr": {"text": "<2"},
             "evidence": {"m_ctx": "at o2 nm is"}}
        assert _locate_conflict(t, "o2", c) == t.find("o2")

    def test_exact_offset_wins(self):
        """① 首选路径：evidence.i1 精确命中（NC 实测值就是段内绝对偏移）"""
        t = self._para()
        core = t.index("dominant size o") + len("dominant size ")
        c = {"mineru": {"text": "o"}, "paddleocr": {"text": "<"},
             "evidence": {"i1": core, "i2": core + 1,
                          "m_ctx": t[core - 60:core + 60]}}
        assert _locate_conflict(t, "o", c) == core
        assert t[core:core + 4] == "o2 n"

    def test_context_scoring_fallback(self):
        """② 退路：i1 失配（段落被前面的替换改动过）→ 用等长窗口打分定位"""
        t = self._para()
        core = t.index("dominant size o") + len("dominant size ")
        c = {"mineru": {"text": "o"}, "paddleocr": {"text": "<"},
             "evidence": {"i1": 1, "m_ctx": t[core - 60:core + 60]}}
        assert _locate_conflict(t, "o", c) == core
        assert t[core:core + 4] == "o2 n"

    def test_ambiguous_without_evidence_gives_up(self):
        t = self._para()
        assert _locate_conflict(t, "o", {"evidence": {}}) == -1

    def test_stale_offset_and_no_context_gives_up(self):
        t = self._para()
        assert _locate_conflict(t, "o", {"evidence": {"i1": 3}}) == -1

    def test_missing_fragment(self):
        assert _locate_conflict("no such fragment", "zzz", {"evidence": {}}) == -1
