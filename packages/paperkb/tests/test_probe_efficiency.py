# -*- coding: utf-8 -*-
"""探测的**效率评价**与单档墙钟上限（2026-09-23 用户要求）。

用户原话："发现在测试 glm-4.5-air 模型的单批上限时，直接卡在第 5 批，没有结果返回" +
"如果是这样，应该增加一个翻译效率评价，如果监测到模型翻译效率很低很慢后，则认为这就是极限"。

本文件锁定：
1. 每档记录 `sec` / `sec_per_1k`（按**输入字符**归一：批次上限这个旋钮就是一批装多少源文）；
2. **效率过低即极限**：译完了但每千字符慢于阈值 ⇒ 该档标 `slow`、阶梯停在它、
   **不计入 highest**（推荐值仍取更快的那一档），stop_reason="inefficient"；
3. 单档**墙钟上限**：LLM 客户端 timeout 是"每次读"级别，服务端持续吐 token 就不触发 ⇒
   必须由探测自建 deadline（挂死的假模型必须在预算内返回结果，不能把界面拖死）；
4. 总预算用尽 ⇒ 余下档位逐档标 `skipped`、用已测到的结果给建议；
5. `efficiency` 评价块：节奏 + 整篇估算分钟数。

耗时怎么造：`_install_clock` 把 probe 模块里的 `time` 换成假时钟，假模型每"译"一次就把时钟往前
拨 self.delays[i] 秒 —— **`sec_per_1k` 是纯逻辑量**，用真 sleep 会因机器抖动而随机判失败
（本文件第一版就栽在这上面），假时钟既确定又秒级跑完。真挂死的用例（`HangLLM`）不装假时钟。
"""
from __future__ import annotations

import json
import re
import time

from paperkb.translate.probe import (_floor100, _tier_budget, probe_safe_batch_chars)

_UNIT = "electrochemical behaviour of the composite electrode "


def _body(size: int) -> str:
    return (_UNIT * (size // len(_UNIT) + 1))[:size]


def _paras(n: int, size: int) -> list[str]:
    """n 段、每段**恰好 size 字符**的英文段（长度精确 ⇒ 档位断言可算）。"""
    return ["P%02d " % i + _body(size - 4) for i in range(1, n + 1)]


def _echo_ids(prompt: str) -> list[str]:
    body = prompt.split("待翻译段落：", 1)[-1]
    return list(dict.fromkeys(re.findall(r"\[(P\d{3})\]", body)))


class Clock:
    """假时钟：只替换 probe 模块里的 `time` 引用（`monotonic` 是它唯一的用法）。"""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def monotonic(self) -> float:
        return self.t


def _install(fake_llm, monkeypatch) -> Clock:
    clock = Clock()
    fake_llm.clock = clock
    monkeypatch.setattr("paperkb.translate.probe.time", clock)
    return clock


class PacedLLM:
    """按档位**指定耗时**的假模型：全译（finish_reason=stop），耗时由假时钟记录。

    `delays`：第 N 次调用的耗时秒数（用完沿用最后一个）——用列表就能造出"小档快、大档慢到
    不可用"的真实形状，而不必真等几十秒。
    """

    def __init__(self, delays: list[float]):
        self.delays = delays or [0.0]
        self.clock: Clock | None = None
        self.last_finish_reason: str | None = None
        self.calls = 0

    def complete(self, prompt: str, context: str = "translate") -> str:
        self.calls += 1
        sec = self.delays[min(self.calls - 1, len(self.delays) - 1)]
        if self.clock is not None:
            self.clock.t += sec
        else:
            time.sleep(sec)
        ids = _echo_ids(prompt)
        self.last_finish_reason = "stop"
        return json.dumps({"translations": [{"para_id": i, "zh": "中" * 4} for i in ids]},
                          ensure_ascii=False)


class HangLLM:
    """永不返回的假模型（模拟"持续吐 token 直到天荒地老"的思考型模型）。"""

    def __init__(self) -> None:
        self.clock: Clock | None = None
        self.last_finish_reason: str | None = None
        self.calls = 0

    def complete(self, prompt: str, context: str = "translate") -> str:
        self.calls += 1
        time.sleep(60)
        return "{}"


# ---------------------------------------------------------------- 墙钟上限的推导
def test_tier_budget_has_floor_and_ceiling():
    """小档走地板（固定开销/网络抖动也要给足），大档走天花板（不无限等）。"""
    assert _tier_budget(3000) == 90.0          # 3×6=18 < 地板
    assert _tier_budget(24000) == 144.0        # 24×6
    assert _tier_budget(48000) == 288.0        # 48×6
    assert _tier_budget(200000) == 300.0       # 封顶


# ---------------------------------------------------------------- 效率记数
def test_tier_records_pace_per_1k_input_chars(monkeypatch):
    llm = PacedLLM([30.0])
    _install(llm, monkeypatch)
    r = probe_safe_batch_chars(llm, _paras(20, 1000), ladder=(3000,))
    row = r["tested"][0]
    assert row["ok"] is True and row["chars"] == 3000
    assert row["sec"] == 30.0
    assert row["sec_per_1k"] == 10.0                 # 30s / 3 千字符
    assert row["out_cps"] > 0
    assert r["efficiency"]["tier"] == 3000
    assert r["efficiency"]["sec_per_1k"] == 10.0
    assert r["efficiency"]["paper_minutes"] == round(40 * 10.0 / 60.0, 1)   # 4 万字符 ≈ 6.7 分钟
    assert "效率" in r["efficiency"]["note"] and "分钟" in r["efficiency"]["note"]


# ---------------------------------------------------------------- 效率过低 = 极限
def test_slow_first_tier_is_the_limit_and_gives_no_recommendation(monkeypatch):
    """首档就译完但很慢 ⇒ 判极限、不出推荐值，并让用户去换更快的模型。"""
    llm = PacedLLM([300.0])                          # 300s / 3 千字符 = 100 s/千字符 ≫ 阈值 25
    _install(llm, monkeypatch)
    r = probe_safe_batch_chars(llm, _paras(60, 1000), ladder=(3000, 6000, 12000))
    assert [t["tier"] for t in r["tested"]] == [3000], "慢档之后不应再试更高档"
    assert r["tested"][0]["ok"] is True and r["tested"][0]["slow"] is True
    assert r["stop_reason"] == "inefficient"
    assert r["highest_pass"] == 0 and r["recommended"] == 0 and r["ok"] is False
    assert llm.calls == 1
    assert "效率过低" in r["tested"][0]["reason"]
    assert "不可用" in r["hint"] or "不实用" in r["hint"]
    assert r["efficiency"]["sec_per_1k"] == 100.0


def test_slow_tier_after_a_fast_tier_keeps_the_faster_recommendation(monkeypatch):
    """小档快、下一档慢 ⇒ 停在慢档、推荐值取**快的那一档**（慢档不计入 highest）。"""
    llm = PacedLLM([0.0, 200.0])                     # 200s / 6 千字符 = 33.3 s/千字符 > 25
    _install(llm, monkeypatch)
    r = probe_safe_batch_chars(llm, _paras(60, 1000), ladder=(3000, 6000, 12000))
    assert [t["tier"] for t in r["tested"]] == [3000, 6000]
    assert r["tested"][0]["ok"] is True and not r["tested"][0].get("slow")
    assert r["tested"][1]["slow"] is True
    assert r["stop_reason"] == "inefficient" and llm.calls == 2
    assert r["highest_pass"] == 3000
    assert r["recommended"] == _floor100(3000 * 0.6) == 1800
    assert "效率过低" in r["hint"]


def test_several_minutes_per_tier_is_still_acceptable(monkeypatch):
    """阈值 25 s/千字符**不误伤**正常节奏：12000 档 250s（20.8 s/千字符）仍算通过。"""
    llm = PacedLLM([250.0])
    _install(llm, monkeypatch)
    r = probe_safe_batch_chars(llm, _paras(60, 1000), ladder=(12000,))
    assert r["tested"][0]["ok"] is True and not r["tested"][0].get("slow")
    assert r["tested"][0]["sec_per_1k"] == 20.8


def test_fast_model_is_not_flagged_slow(monkeypatch):
    llm = PacedLLM([0.0])
    _install(llm, monkeypatch)
    r = probe_safe_batch_chars(llm, _paras(60, 1000), ladder=(3000, 6000, 12000))
    assert r["stop_reason"] == "passed_all"
    assert all(not t.get("slow") for t in r["tested"])
    assert r["recommended"] == _floor100(12000 * 0.6)


# ---------------------------------------------------------------- 墙钟上限 / 总预算
def test_hung_call_is_cut_by_wall_clock_and_still_reports():
    """挂死的模型必须在单档上限内被截断并**如实出结果**（就是用户"卡在第 5 批"那个毛病）。

    这条**不装假时钟**：要的就是真挂住 + 真墙钟。
    """
    llm = HangLLM()
    t0 = time.monotonic()
    r = probe_safe_batch_chars(llm, _paras(20, 1000), ladder=(3000, 6000),
                               tier_timeout=0.2, total_budget=30.0)
    took = time.monotonic() - t0
    row = r["tested"][0]
    assert row["timed_out"] is True and row["error"] is True
    assert row["ok"] is False and row["sec"] >= 0.2 and row["sec_per_1k"] > 0
    assert r["stop_reason"] == "timed_out"
    assert r["recommended"] == 0 and r["ok"] is False
    assert llm.calls == 1, "超时后不应继续试更高档"
    assert took < 10, "挂死的调用必须被墙钟上限截断，不能把界面拖死"
    assert "未译完" in row["reason"] and "超时" in row["reason"]
    assert "下界" in r["hint"] and "翻译模型" in r["hint"], "要指向换模型，而不是让用户重试"


def test_total_budget_marks_remaining_tiers_skipped(monkeypatch):
    """总预算用尽 ⇒ 余下档位逐档标未测（界面要看到整条阶梯的处置），已测结果照给。"""
    llm = PacedLLM([100.0])                          # 首档（假时钟）就用掉 100s
    _install(llm, monkeypatch)
    r = probe_safe_batch_chars(llm, _paras(60, 1000), ladder=(3000, 6000, 12000),
                               total_budget=50.0, slow_sec_per_1k=1000.0)
    assert r["stop_reason"] == "time_budget"
    assert [t["tier"] for t in r["tested"]] == [3000, 6000, 12000]
    assert r["tested"][0]["ok"] is True
    assert [t.get("skipped") for t in r["tested"][1:]] == [True, True]
    assert "预算用尽" in r["tested"][1]["reason"]
    assert r["highest_pass"] == 3000 and r["recommended"] == _floor100(3000 * 0.6)
    assert "预算上限" in r["hint"]
    assert llm.calls == 1


def test_exhausted_total_budget_measures_nothing_and_says_so(monkeypatch):
    """预算一开始就超支（传负值，确定性触发）：一档没测 ⇒ 不能谎称"瓶颈是 max_tokens"。"""
    llm = PacedLLM([0.0])
    _install(llm, monkeypatch)
    r = probe_safe_batch_chars(llm, _paras(60, 1000), ladder=(3000, 6000), total_budget=-1.0)
    assert [t.get("skipped") for t in r["tested"]] == [True, True]
    assert r["stop_reason"] == "time_budget" and llm.calls == 0
    assert r["recommended"] == 0 and "预算" in r["hint"]
    assert "max_tokens" not in r["hint"]
