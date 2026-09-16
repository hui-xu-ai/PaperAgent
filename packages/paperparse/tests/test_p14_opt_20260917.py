# -*- coding: utf-8 -*-
"""2026-09-17 批次单测（T1–T5 + T9）：
· T1 台账/埋点：`TokenGuard.record_usage` 契约（backend 侧单测在 backend/tests）
· T2 第三信号判据 + 护栏（`apply_third_decide`）
· T3 AI 证据窗口化（`local_text.locate_window`）
· T4 决策阶梯顺序（免费信号先于 AI —— 由 `apply_third_decide` 可独立调用锚定）
· T5 参考文献闸门（`references_para_ids` + `block_references_ai`，用户约束：
  "参考文献部分不需要经过 AI 仲裁处理"）
· T9 短 schema + 关思考（`synthesize` 参数/解析/降级）+ 公式保形护栏（`_form_only_change`）
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from paperparse.core.dual_ai_review import (                      # noqa: E402
    Arbitration, OpenAICompatProvider, _SYSTEM_SYNTH, _SYSTEM_SYNTH_SHORT,
    _parse_suggestions, synthesize,
)
from paperparse.core.local_text import locate_window              # noqa: E402
from paperparse.core.p14_pipeline import (                        # noqa: E402
    references_para_ids, apply_third_decide, block_references_ai, build_markdown,
    _form_only_change,
)


def _item(kind, text, pid):
    from paperparse.core.repair_paragraphs import RepairItem
    return RepairItem(para_id=pid, text=text, kind=kind)


def _conf(para_id, m="Co", p="Co ", vote=None):
    c = {"para_id": para_id, "type": "text_conflict", "page": 1,
         "mineru": {"text": m}, "paddleocr": {"text": p}}
    if vote is not None:
        c["third_vote"] = vote
    return c


class TestReferencesParaIds:
    """T5：参考文献区判据（口径与 build_markdown 的 in_refs 分流一致）"""

    def test_heading_then_entries(self):
        items = [
            _item("body", "Body paragraph about actuators.", "RP001"),
            _item("heading", "## References", "RP002"),
            _item("body", "[1] Y. Zhang, J. Mater. Sci. 2020, 5, 1.", "RP003"),
            _item("body", "[2] L. Li, Adv. Funct. Mater. 2021, 31, 2.", "RP004"),
        ]
        ids = references_para_ids(items)
        assert ids == {"RP002", "RP003", "RP004"}

    def test_no_heading_fallback_entry_run(self):
        """adma 真实形态：源 md 无 References 标题，靠 `[N] 条目`连续识别"""
        items = [
            _item("body", "Body paragraph.", "RP001"),
            _item("body", "[62] J. Huang, P. F. Miller, Anal. Chem. 2019.", "RP002"),
            _item("body", "[63] L. Yang, J. Yan, Nature 2020, 12, 3.", "RP003"),
            _item("body", "Supported by the National Key R&D Program.", "RP004"),
        ]
        ids = references_para_ids(items)
        assert ids == {"RP002", "RP003"}          # RP004 非条目 → 离开该区

    def test_inline_citation_not_reference(self):
        """正文里的内联引用 `As shown in [3], ...` 不算参考文献条目"""
        items = [
            _item("body", "As shown in [3], the value increases.", "RP001"),
            _item("caption", "Figure 2. [1] sample.", "RP002"),
        ]
        assert references_para_ids(items) == set()

    def test_sup_form_entry(self):
        items = [_item("body", "<sup>[41]</sup> Y. Zhang, Chem. Rev. 2018.", "RP009")]
        assert references_para_ids(items) == {"RP009"}

    def test_build_markdown_same_predicate(self):
        """单一来源回归：build_markdown 仍按同一判据分流（含无标题兜底）"""
        items = [
            _item("body", "Body text.", "RP001"),
            _item("body", "[1] Y. Zhang, J. Mater. Sci. 2020.", "RP002"),
            _item("body", "[2] L. Li, Adv. Mater. 2021.", "RP003"),
        ]
        md_ref = build_markdown(items, include_references=True)
        md_no = build_markdown(items, include_references=False)
        assert "| [1] |" in md_ref and "J. Mater. Sci." in md_ref
        assert "J. Mater. Sci." not in md_no


class TestApplyThirdDecide:
    """T2/T4：第三信号裁决语义 + `both`(公式等价) 护栏"""

    def _mk(self, i, verdict, tv):
        c = _conf("RP%03d" % i, vote=tv)
        return c, Arbitration(id=i, verdict=verdict)

    def test_unresolved_paddle_wins(self):
        c, a = self._mk(1, "unresolved",
                        {"decisive": True, "verdict": "paddleocr"})
        by = {0: a}
        src = {0: "rule"}
        assert apply_third_decide(by, [c], src) == 1
        assert a.verdict == "paddleocr" and src[0] == "third"

    def test_both_rule_not_overridden(self):
        """★护栏：公式等价(both) 不允许被翻转成 paddleocr（防 LaTeX→明文形式降级）"""
        c, a = self._mk(1, "both", {"decisive": True, "verdict": "paddleocr"})
        by, src = {0: a}, {0: "rule"}
        assert apply_third_decide(by, [c], src) == 0
        assert a.verdict == "both" and src[0] == "rule"

    def test_paddle_reverted_by_local(self):
        """文本层反对 → 自动撤销已落地的 P 替换（保留 MinerU）"""
        c, a = self._mk(1, "paddleocr", {"decisive": True, "verdict": "mineru"})
        by, src = {0: a}, {0: "rule"}
        assert apply_third_decide(by, [c], src) == 1
        assert a.verdict == "mineru" and src[0] == "third"

    def test_unresolved_mineru_keeps(self):
        c, a = self._mk(1, "unresolved", {"decisive": True, "verdict": "mineru"})
        by, src = {0: a}, {0: "rule"}
        assert apply_third_decide(by, [c], src) == 1
        assert a.verdict == "mineru" and src[0] == "third"

    def test_non_decisive_and_disabled(self):
        c, a = self._mk(1, "unresolved", {"decisive": False, "verdict": "paddleocr"})
        by, src = {0: a}, {0: "rule"}
        assert apply_third_decide(by, [c], src) == 0
        assert a.verdict == "unresolved"
        c2, a2 = self._mk(2, "unresolved", {"decisive": True, "verdict": "paddleocr"})
        by2, src2 = {0: a2}, {0: "rule"}
        assert apply_third_decide(by2, [c2], src2, enabled=False) == 0
        assert a2.verdict == "unresolved"


class TestReferencesAiGate:
    """T5 闸门：参考文献区**不进 AI**（用户约束），只排除 AI、不动本地结论"""

    def test_ref_unresolved_blocked(self):
        cs = [_conf("RP002"), _conf("RP010")]
        a0 = Arbitration(id=0, verdict="unresolved")
        a1 = Arbitration(id=1, verdict="unresolved")
        by, src = {0: a0, 1: a1}, {0: "rule", 1: "rule"}
        blocked = block_references_ai(by, cs, src, {"RP002"})
        assert blocked == 1
        assert a0.verdict == "mineru" and src[0] == "ref_skip"
        assert a1.verdict == "unresolved" and src[1] == "rule"   # 正文照常送 AI

    def test_ref_already_decided_untouched(self):
        """档位②：参考文献区的**规则/第三信号**结论保留（只排除 AI）"""
        cs = [_conf("RP002")]
        a0 = Arbitration(id=0, verdict="paddleocr", confidence=1.0)
        by, src = {0: a0}, {0: "rule"}
        assert block_references_ai(by, cs, src, {"RP002"}) == 0
        assert a0.verdict == "paddleocr"


class TestLocateWindow:
    """T3：AI 证据窗口化（替代"页首 400 字符"）"""

    def _raw(self):
        return ("Reinforced Magnetic-Responsive Electro-Ionic Artificial Muscles\n"
                + ("filler word " * 120)
                + "the ionic conductivity of the film was measured at 25 C "
                + ("tail filler " * 120))

    def test_locates_neighborhood_not_head(self):
        raw = self._raw()
        win = locate_window(raw, ["the ionic conductivity of the film was measured"])
        assert "conductivity" in win
        assert not win.startswith("Reinforced Magnetic")
        assert len(win) < len(raw) // 2

    def test_fallback_to_head(self):
        raw = self._raw()
        win = locate_window(raw, ["completely absent wording here"])
        assert win == raw[:240].strip()

    def test_empty(self):
        assert locate_window("", ["x y z"]) == ""
        assert locate_window("some text", []) == "some text"


class TestThirdDecideOrderContract:
    """T4：阶梯顺序契约——免费信号的调用点必须在 AI 调用之前（静态断言）"""

    def test_source_order_in_pipeline(self):
        src = Path(__file__).resolve().parents[1] / "paperparse/core/p14_pipeline.py"
        text = src.read_text(encoding="utf-8")
        i_third = text.index("apply_third_decide(")
        i_ref = text.index("block_references_ai(")
        i_ai = text.index("synthesize([conflicts_all[i] for i in ai_ids]")
        assert i_third < i_ai, "免费第三信号必须在 AI 调用之前"
        assert i_ref < i_ai, "参考文献闸门必须在 AI 调用之前"


# ----------------------------------------------------------------- T9（短 schema + 关思考）
class _FakeProvider:
    """记录调用参数的假 provider（不发网络请求）。"""

    def __init__(self, reply: str = '[{"id":0,"a":"r","t":"CoO$_x$@LIG"}]'):
        self.reply = reply
        self.calls: list[dict] = []

    def available(self) -> bool:
        return True

    def complete(self, messages, *, temperature=0.1, max_tokens=8192, extra_body=None):
        self.calls.append({"system": messages[0]["content"], "user": messages[1]["content"],
                           "max_tokens": max_tokens, "extra_body": extra_body})
        return self.reply


def _conf_item(i=0):
    return {"id": i, "type": "text_conflict", "page": 3,
            "mineru": {"text": "CoO @LIG"}, "paddleocr": {"text": " $ CoO_x@LIG $"},
            "evidence": {"m_ctx": "the CoO @LIG sample", "p_ctx": "the CoO_x@LIG sample",
                         "local_snippet": "the CoOx@LIG sample was prepared"}}


class TestT9ShortSchema:
    """T9：短 schema + 关思考（A/B 实测：输出 −98.9%，两者必须同时生效）"""

    def test_short_mode_default(self, monkeypatch):
        for k in ("PARSE_AI_SHORT", "PARSE_AI_THINKING"):
            monkeypatch.delenv(k, raising=False)
        p = _FakeProvider()
        out = synthesize([_conf_item()], provider=p, paper="adma")
        call = p.calls[0]
        assert call["system"] == _SYSTEM_SYNTH_SHORT
        assert call["max_tokens"] == 1024
        assert call["extra_body"] == {"thinking": {"type": "disabled"}}
        assert out[0].action == "replace" and out[0].suggested_text == "CoO$_x$@LIG"
        assert out[0].verdict == "paddleocr"

    def test_legacy_mode_via_env(self, monkeypatch):
        monkeypatch.setenv("PARSE_AI_SHORT", "0")
        monkeypatch.setenv("PARSE_AI_THINKING", "1")
        p = _FakeProvider(reply='[{"id":0,"action":"keep","suggested_text":"","confidence":1}]')
        synthesize([_conf_item()], provider=p, paper="adma")
        call = p.calls[0]
        assert call["system"] == _SYSTEM_SYNTH
        assert call["max_tokens"] == 4096 and call["extra_body"] is None

    def test_parse_short_and_long_forms(self):
        short = _parse_suggestions('[{"id":2,"a":"k"},{"id":3,"a":"i","t":"f"}]')
        assert short == [{"id": 2, "action": "keep", "suggested_text": "",
                          "confidence": 0.8, "reason": ""},
                         {"id": 3, "action": "insert", "suggested_text": "f",
                          "confidence": 0.8, "reason": ""}]
        long = _parse_suggestions('[{"id":1,"action":"replace","suggested_text":"x",'
                                  '"confidence":0.9,"reason":"r"}]')
        assert long[0]["action"] == "replace" and long[0]["suggested_text"] == "x"

    def test_complete_drops_extra_body_on_http_error(self, monkeypatch):
        """端点不认 `thinking`（400/422）→ 去掉扩展参数重试一次，不打死整轮仲裁。"""
        import requests
        from paperparse.core import dual_ai_review as dar_mod

        seen: list[dict] = []

        class _Resp:
            def __init__(self, ok):
                self.ok = ok

            def raise_for_status(self):
                if not self.ok:
                    raise requests.HTTPError("400 bad param")

            def json(self):
                return {"choices": [{"message": {"content": "[]"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

        def fake_post(url, headers=None, json=None, timeout=None):
            seen.append(json)
            return _Resp("thinking" not in json)

        monkeypatch.setattr(dar_mod.requests, "post", fake_post)
        prov = OpenAICompatProvider(api_key="k", base="http://x", model="m")
        prov.complete([{"role": "user", "content": "hi"}], extra_body={"thinking": {"type": "disabled"}})
        assert len(seen) == 2
        assert "thinking" in seen[0] and "thinking" not in seen[1]


class TestT9FormulaShapeGuard:
    """T9 公式保形：只把 LaTeX 改写成明文/HTML（内容等价）→ 拒绝落地"""

    def test_form_only_rewrites_blocked(self):
        assert _form_only_change(r"$\mathrm { B F _ { 4 } } ^ { - }$", r"$\mathrm{BF₄⁻}$")
        assert _form_only_change(r"$\mathrm { e V } ^ { [ 6 2 ] }$", "eV $ ^{[62]} $")
        assert _form_only_change(r"${\mathrm { C o } }$", "Co")

    def test_content_changes_allowed(self):
        # 补下标（内容变化）→ 允许落地（这是 AI 综合建议的价值所在）
        assert not _form_only_change("CoO @LIG", "CoO$_x$@LIG")
        assert not _form_only_change("M and H", r"$\mathrm{M_r}$ and $\mathrm{H_c}$")
        assert not _form_only_change(r"$\mathrm { B F _ { 4 } }$", r"$\mathrm { B F _ { 3 } }$")


class TestT9RetryTrigger:
    """T9 收口：第 2 次调用只服务"仍未判定"的残留（旧的"公式类 keep 再追问"已删）"""

    def test_only_unresolved_and_neither(self):
        from paperparse.core.p14_pipeline import ai_retry_ids
        by = {0: Arbitration(id=0, verdict="unresolved", action="keep"),
              1: Arbitration(id=1, verdict="neither"),
              2: Arbitration(id=2, verdict="mineru", action="keep"),
              3: Arbitration(id=3, verdict="paddleocr", action="replace")}
        confs = [{"mineru": {"text": "$x$"}, "paddleocr": {"text": "x"}}] * 4
        assert ai_retry_ids(by, confs) == [0, 1]


