# -*- coding: utf-8 -*-
"""翻译批次「安全上限」探测（2026-09-22 用户要求：点一下自动测出并给建议值）。

用户要求原话："可以生成一个代码，让用户点击之后，自动测试一下模型的安全能力，然后自动填写，
后续实际使用时，自动按这个计算值设置安全上限" + "在测安全上限时，实际填写值，不能按照测试极限
来填，你可以按照你的经验，设置一个安全系数"。

本文件锁定：
1. **阶梯 + 首败即停**（截断随批次单调 ⇒ 更大批不必再试）；
2. **双判据**：finish_reason=length **或** 应译段没回全，任一命中即判该档失败
   （思考型模型常报 stop 却只译一半）；
3. **安全系数**：推荐值 = 最高通过档 × 0.6（**绝不等于测试极限**）；
4. **不碰语义**：整段取用，绝不切段中；单段超上限则独占一批；
5. 语料不足/调用报错/进度回调等边界行为。
"""
from __future__ import annotations

import json
import re

from paperkb.translate.probe import (_floor100, _pick_corpus_paras, _take,
                                     collect_english_text, probe_safe_batch_chars)

_UNIT = "electrochemical behaviour of the composite electrode "


def _body(size: int) -> str:
    return (_UNIT * (size // len(_UNIT) + 1))[:size]


def _paras(n: int, size: int) -> list[str]:
    """n 段、每段**恰好 size 字符**的英文段（长度精确 ⇒ 档位断言可算）。"""
    return ["P%02d " % i + _body(size - 4) for i in range(1, n + 1)]


class CapLLM:
    """按"输出容量"截断的假模型：译文长度 1:1 于源文，超容量就只译前几段。

    capacity=None ⇒ 不限容量（全译）。hide_truncation=True ⇒ 模拟"供应商不自报截断"
    （finish_reason=stop 但只回了一半）⇒ 专门验证第二判据独立生效。
    boom_on_call=N ⇒ 第 N 次调用抛错（模拟超时）。
    """

    def __init__(self, capacity: int | None = None, *, hide_truncation: bool = False,
                 boom_on_call: int | None = None):
        self.capacity = capacity
        self.hide_truncation = hide_truncation
        self.boom_on_call = boom_on_call
        self.last_finish_reason: str | None = None
        self.prompts: list[str] = []
        self.calls = 0

    def complete(self, prompt: str, context: str = "compile") -> str:
        self.calls += 1
        self.prompts.append(prompt)
        if self.boom_on_call and self.calls == self.boom_on_call:
            raise RuntimeError("模拟调用失败（超时）")
        # 只解析「待翻译段落：」之后的正文——提示词规则里也有一个示例标记 [P001]
        body = prompt.split("待翻译段落：", 1)[-1]
        ids = list(dict.fromkeys(re.findall(r"\[(P\d{3})\]", body)))
        chunks = re.split(r"\[P\d{3}\]", body)
        lens = [len(c.strip()) for c in chunks[1:]]
        out, used = [], 0
        for pid, n in zip(ids, lens):
            if self.capacity is not None and used + n > self.capacity:
                break
            used += n
            out.append({"para_id": pid, "zh": "中" * n})
        complete = (self.capacity is None or len(out) == len(ids))
        self.last_finish_reason = "stop" if (complete or self.hide_truncation) else "length"
        return json.dumps({"translations": out}, ensure_ascii=False)


# ---------------------------------------------------------------- 阶梯与首败即停
def test_ladder_stops_at_first_failure():
    """6000 档失败 ⇒ 12000/24000 不再试（截断单调），调用次数 = 已试档数。"""
    llm = CapLLM(capacity=5000)
    r = probe_safe_batch_chars(llm, _paras(60, 1000), ladder=(3000, 6000, 12000, 24000))
    assert [t["tier"] for t in r["tested"]] == [3000, 6000], r["tested"]
    assert [t["ok"] for t in r["tested"]] == [True, False]
    assert r["highest_pass"] == 3000
    assert r["stop_reason"] == "failed"
    assert llm.calls == 2, "首败之后不应再发请求"


def test_all_tiers_pass_when_model_has_headroom():
    llm = CapLLM(capacity=None)
    r = probe_safe_batch_chars(llm, _paras(60, 1000), ladder=(3000, 6000, 12000))
    assert all(t["ok"] for t in r["tested"])
    assert r["stop_reason"] == "passed_all"
    assert llm.calls == 3


# ---------------------------------------------------------------- 双判据
def test_finish_reason_length_alone_fails_the_tier():
    """段落全回全了，但供应商自报 finish_reason=length ⇒ 仍判失败（触顶的另一形态）。"""
    llm = CapLLM(capacity=None)

    def complete(prompt, context="compile"):
        llm.calls += 1
        ids = list(dict.fromkeys(re.findall(r"\[(P\d{3})\]", prompt)))
        llm.last_finish_reason = "length"
        return json.dumps({"translations": [{"para_id": i, "zh": "中"} for i in ids]})

    llm.complete = complete
    r = probe_safe_batch_chars(llm, _paras(20, 1000), ladder=(3000,))
    row = r["tested"][0]
    assert row["ok"] is False
    assert row["finish_reason"] == "length"
    assert row["missed"] == 0 and row["got"] == row["paras"]
    assert "触顶" in row["reason"]


def test_missing_segments_fail_even_without_finish_reason():
    """供应商不自报截断（finish_reason=stop）但只回了一部分 ⇒ 第二判据必须抓住。"""
    llm = CapLLM(capacity=2500, hide_truncation=True)
    r = probe_safe_batch_chars(llm, _paras(20, 1000), ladder=(3000,))
    row = r["tested"][0]
    assert row["finish_reason"] == "stop"
    assert row["missed"] > 0 and row["got"] > 0
    assert row["ok"] is False
    assert "未自报" in row["reason"]


# ---------------------------------------------------------------- 安全系数
def test_recommended_is_below_tested_limit():
    """推荐值 = 最高通过档 × 0.6，**绝不等于测试极限**。"""
    llm = CapLLM(capacity=6500)
    r = probe_safe_batch_chars(llm, _paras(60, 1000), ladder=(3000, 6000, 12000))
    assert r["highest_pass"] == 6000
    assert r["recommended"] == 3600 == _floor100(6000 * 0.6)
    assert r["recommended"] < r["highest_pass"]
    assert r["safety_factor"] == 0.6


def test_custom_safety_factor_is_honored():
    llm = CapLLM(capacity=None)
    r = probe_safe_batch_chars(llm, _paras(60, 1000), ladder=(3000, 6000),
                               safety_factor=0.5)
    assert r["recommended"] == 3000
    assert r["safety_factor"] == 0.5


def test_first_tier_failure_points_at_max_tokens():
    """首档就失败 ⇒ 瓶颈不是批次上限（应指向 max_tokens），且不给推荐值。"""
    llm = CapLLM(capacity=100)
    r = probe_safe_batch_chars(llm, _paras(60, 1000), ladder=(3000, 6000),
                               max_tokens=8192)
    assert r["recommended"] == 0 and r["ok"] is False
    assert "max_tokens" in r["hint"] and "8192" in r["hint"]


# ---------------------------------------------------------------- 语义完整（不切段）
def test_prompts_contain_whole_paragraphs_only():
    """任何档位发出去的 prompt 里，源段都是**整段原样出现**（绝不切段中）。"""
    corpus = _paras(40, 900)
    llm = CapLLM(capacity=None)
    probe_safe_batch_chars(llm, corpus, ladder=(3000, 6000, 12000))
    assert len(llm.prompts) == 3
    for prompt in llm.prompts:
        for t in corpus:
            if t[:30] in prompt:              # 这段被本批选中 ⇒ 必须是完整段落
                assert t in prompt, "段落被切断了：%r" % t[:40]


def test_take_packs_whole_paragraphs_only():
    paras = ["a" * 700, "b" * 500, "c" * 400, "d" * 2000]
    got = _take(paras, 1000)
    assert sum(len(t) for t in got) >= 1000            # 装够档位
    assert got == ["a" * 700, "b" * 500]               # 只在段边界切
    # 首段自身超上限 ⇒ 独占一批（真实路径的超长单段行为）
    assert _take(["x" * 5000, "y" * 100], 3000) == ["x" * 5000]


# ---------------------------------------------------------------- 语料挑选
def test_pick_corpus_prefers_long_paragraphs_but_keeps_doc_order():
    paras = ["short" * 10, "L" * 3000, "m" * 1500, "x" * 200, "L" * 3000, "n" * 1200]
    picked = _pick_corpus_paras(paras, 3000)
    assert "L" * 3000 in picked
    assert picked == [p for p in paras if any(p is q for q in picked)], "应保持原文顺序"


def test_insufficient_text_skips_untestable_tiers():
    llm = CapLLM(capacity=None)
    r = probe_safe_batch_chars(llm, _paras(5, 500), ladder=(3000, 12000))
    # 5×500=2500 < 3000 ⇒ 首档就没法测
    assert r["stop_reason"] == "insufficient_text"
    assert r["recommended"] == 0 and r["ok"] is False
    assert llm.calls == 0, "语料不足时不应发起任何调用"
    assert "正文不足" in r["hint"]


def test_insufficient_text_mid_ladder_reports_measured_ceiling():
    """只够测到 6000 档 ⇒ 报出已测到的上限 + 0.6 推荐值，并说明为何没测更高档。"""
    llm = CapLLM(capacity=None)
    r = probe_safe_batch_chars(llm, _paras(10, 800), ladder=(3000, 6000, 48000))
    assert r["stop_reason"] == "insufficient_text"
    assert r["highest_pass"] == 6400 and r["recommended"] == _floor100(6400 * 0.6)
    assert [t["tier"] for t in r["tested"]] == [3000, 6000, 48000]
    assert r["tested"][-1].get("skipped") is True
    assert llm.calls == 2
    assert "没能测" in r["hint"]


# ---------------------------------------------------------------- 调用报错 / 进度
def test_call_error_marks_tier_failed_and_hints_retry():
    llm = CapLLM(boom_on_call=1)
    r = probe_safe_batch_chars(llm, _paras(20, 1000), ladder=(3000, 6000))
    row = r["tested"][0]
    assert row["error"] is True and r["stop_reason"] == "error"
    assert "重试" in r["hint"] and r["recommended"] == 0
    assert llm.calls == 1, "报错后不应继续试更高档"


def test_progress_cb_called_once_per_attempted_tier():
    seen: list[tuple[int, int, str]] = []
    llm = CapLLM(capacity=5000)
    probe_safe_batch_chars(llm, _paras(60, 1000), ladder=(3000, 6000, 12000),
                           progress_cb=lambda c, t, p: seen.append((c, t, p)))
    assert [(c, t) for c, t, _ in seen] == [(1, 3), (2, 3)]


def test_progress_cb_exception_does_not_break_probe():
    def boom(*_a):
        raise RuntimeError("前端回调炸了")
    llm = CapLLM(capacity=None)
    r = probe_safe_batch_chars(llm, _paras(20, 1000), ladder=(3000,), progress_cb=boom)
    assert r["tested"][0]["ok"] is True


# ---------------------------------------------------------------- 语料收集
def test_collect_english_text_skips_non_paper_dirs(tmp_path, monkeypatch):
    """只认 <dir>/document.json；跳过 _trash/.cache 等目录；kb 与 library 都扫。"""
    kb, lib = tmp_path / "kb", tmp_path / "library"
    for base, names in ((kb, ["10.1_ok", "_trash", ".cache"]),
                        (lib, ["10.2_ok"])):
        for name in names:
            d = base / name
            d.mkdir(parents=True)
            (d / "document.json").write_text("{}", encoding="utf-8")

    class FakePara:
        def __init__(self, text, heading=False):
            self.text_en, self.is_heading = text, heading

    monkeypatch.setattr("paperkb.translate.probe.read_document", lambda _p: object())
    monkeypatch.setattr("paperkb.translate.probe.context_paragraphs",
                        lambda _doc: [FakePara("body " * 40), FakePara("Title", True)])

    class Roots:
        pass
    roots = Roots()
    roots.kb_dir, roots.library_dir = kb, lib
    r = collect_english_text(roots, 200)
    assert r["docs"] == 2, r
    assert {s["dir"] for s in r["sources"]} == {"10.1_ok", "10.2_ok"}
    assert len(r["paras"]) == 2 and all(p.startswith("body") for p in r["paras"])
