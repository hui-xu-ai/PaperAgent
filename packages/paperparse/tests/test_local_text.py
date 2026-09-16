# -*- coding: utf-8 -*-
"""第三信号（PDF 自带文本层）单测（2026-09-16）。

覆盖：归一化两套（去空白 / 含空格）、可判定性（短片段不投票）、token 级共识检查、
两段式冲突投票（小窗判空格/断词、宽窗兜底）。
已知正例来自真实踩过的坑：`BF₄⁻` 下标、`\\Nu_{2}`、`480 µm`、`150 °C`。
"""
from paperparse.core.local_text import (MIN_PAGE_CHARS, dice, normalize,
                                        normalize_spaced, page_texts,
                                        salient_tokens, unverified_tokens,
                                        usable_pages, vote, vote_conflict)

# 一页"PDF 文本层"（模拟电子版 PDF 抽出的文本；真实链路里由 page_texts() 得到）。
# 长度必须超过 MIN_PAGE_CHARS（页门控），所以这里用真实篇幅的句子重复填充。
_BASE = ("The actuator reached 480 µm displacement at 150 °C under 1 V. "
         "The sample BF 4 − was measured with 9.80 A m−2 kg−1 at room temperature. "
         "Multiday operable ionic polymer-metal composites were tested for stability. ")
PAGE = _BASE * 2


class TestNormalize:
    def test_unspaced_strips_latex_and_space(self):
        assert normalize(r"$\mathrm { C o } _ { x }$") == "cox"
        assert normalize("of conductivity") == "ofconductivity"

    def test_spaced_keeps_word_boundaries(self):
        assert normalize_spaced("of conductivity") == "of conductivity"
        assert normalize_spaced("of\nconductivity") == "of conductivity"
        assert normalize_spaced("Eﬃcient ion") == "efficient ion"


class TestSalientTokens:
    def test_extracts_units_and_formulas(self):
        toks = salient_tokens("reached 480 µm at 150 °C with $\\mathrm{BF_4^-}$")
        assert "480m" in toks            # 480 µm（µm → NFKD → μm → m）
        assert "150c" in toks            # 150 °C
        assert "bf4" in toks             # BF_4^-
        assert all(any(ch.isdigit() for ch in t) for t in toks), "只取含数字的高信号 token"

    def test_ignores_plain_words(self):
        assert salient_tokens("conductivity and capacitance") == []


class TestUnverifiedTokens:
    def test_absent_token_flagged(self):
        """`BF₄⁻` 的下标被识别错（文本层里不存在 BF7）→ 必须被检出（P12 共识错误类）。"""
        bad = unverified_tokens("the sample $\\mathrm{BF_7^-}$ was measured", PAGE)
        assert [b["token"] for b in bad] == ["bf7"]

    def test_present_tokens_not_flagged(self):
        assert unverified_tokens("reached 480 µm at 150 °C", PAGE) == []

    def test_no_page_text_no_flags(self):
        assert unverified_tokens("anything 123 µm", "") == []


class TestVote:
    def test_short_fragments_do_not_vote(self):
        """单字符/空格差异（实测占 80%+）不得投票——否则统计口径全是无意义的 neither。"""
        r = vote("", " ", PAGE)
        assert r["verdict"] == "unknown" and r["judged"] == []

    def test_side_supported_wins(self):
        r = vote("Multiday operable ionic", "Multiday operable inoic", PAGE)
        assert r["verdict"] == "mineru" and r["m_hit"] and not r["p_hit"]

    def test_page_too_thin(self):
        r = vote("Multiday operable ionic", "x", "short page")
        assert r["verdict"] == "unknown" and "文本层" in r["reason"]


class TestVoteConflict:
    def _c(self, m, p, i1, j1, pad=60, text_m="", text_p=""):
        tm = text_m or m
        tp = text_p or p
        return {"mineru": {"text": m}, "paddleocr": {"text": p},
                "evidence": {"i1": i1, "j1": j1,
                             "m_ctx": tm[max(0, i1 - pad): i1 + len(m) + pad],
                             "p_ctx": tp[max(0, j1 - pad): j1 + len(p) + pad]}}

    def test_space_conflict_judged_by_tight_window(self):
        """断词/空格类：`of conductivity` vs `ofconductivity` —— 文本层里有空格的一侧胜。"""
        page = "the thermal stability of conductivity was measured at room temperature. " * 3
        m = "ofconductivity"
        p = "of conductivity"
        c = self._c(m, p, i1=22, j1=22, text_m="the thermal stability ofconductivity was",
                    text_p="the thermal stability of conductivity was")
        r = vote_conflict(c, page)
        assert r["verdict"] == "paddleocr", r
        assert r["decisive"] is True and r["stage"] == "tight"

    def test_content_equivalent_formula_not_decisive(self):
        """`$1 5 0 ^ { \\circ } \\mathrm{C}$` 与 `150 °C` **内容等价**（只差排版）⇒ 判 both、
        不下结论（与规则层 `_formula_equivalent` 同精神：等价就保留 MinerU 的 LaTeX）。"""
        page = "the actuator reached 480 µm displacement at 150 °C under 1 V. " * 4
        m = r"$1 5 0 ^ { \circ } \mathrm { C }$"
        p = "150 °C"
        c = self._c(m, p, i1=40, j1=40,
                    text_m="the actuator reached 480 µm displacement at $1 5 0 ^ { \\circ } \\mathrm { C }$ under",
                    text_p="the actuator reached 480 µm displacement at 150 °C under")
        r = vote_conflict(c, page)
        assert r["verdict"] == "both", r
        assert r["decisive"] is False

    def test_wrong_number_side_loses(self):
        """MinerU 把 480 µm 识别成 460 µm（PaddleOCR/文本层都是 480）⇒ 判 paddleocr。"""
        page = ("the actuator reached 480 µm displacement at 150 °C under 1 V. "
                "The same sample also showed 480 µm in the second measurement. ") * 3
        m = "460 µm"
        p = "480 µm"
        c = self._c(m, p, i1=28, j1=28, text_m="the actuator reached 460 µm displacement at",
                    text_p="the actuator reached 480 µm displacement at")
        r = vote_conflict(c, page)
        assert r["verdict"] == "paddleocr", r
        assert r["decisive"] is True

    def test_no_page_text_is_unknown(self):
        c = self._c("M and H", "Mr and Hc", 5, 5)
        assert vote_conflict(c, "")["verdict"] == "unknown"


class TestPageTexts:
    def test_missing_pdf_is_graceful(self, tmp_path):
        assert page_texts(tmp_path / "nope.pdf") == {}

    def test_usable_pages_gate(self):
        pages = {1: "x" * (MIN_PAGE_CHARS + 1), 2: "short", 3: ""}
        assert usable_pages(pages) == {1}

    def test_dice_bounds(self):
        assert dice("abc", "abc") == 1.0
        assert dice("", "abc") == 0.0
