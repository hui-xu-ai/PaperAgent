# -*- coding: utf-8 -*-
"""AI 综合建议（用户 2026-09-16："让 AI 综合这些解析结果进行综合判断，给出自己的修改建议"）。

与旧 `arbitrate()`（只选 M/P）的关键差别：`synthesize()` 让 AI 给出**自己的最终片段**
`suggested_text`（可与两侧都不同，例如 MinerU 的 LaTeX 结构 + PaddleOCR 的下标）。
本文件锁定：schema 解析、散文容错、两道新护栏（④来源可溯 / ⑤最小编辑）、落地分支。
"""
from __future__ import annotations

from paperparse.core.dual_ai_review import (_parse_suggestions, _SYSTEM_SYNTH,
                                            synthesize)
from paperparse.core.p14_pipeline import (_apply_arbitrations, _novel_tokens,
                                          _synthesis_shape_ok)


class _StubProvider:
    """假 provider：按脚本返回固定文本；记录收到的 prompt 便于断言证据面。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def available(self) -> bool:
        return True

    def complete(self, messages, max_tokens=4096, extra_body=None):
        self.prompts.append(messages[-1]["content"])
        return self.replies.pop(0) if self.replies else "[]"


class _LegacyStubProvider(_StubProvider):
    """不认识 `extra_body` 的旧 provider（注入的第三方 adapter）：必须走 TypeError 降级，
    不能让整篇 AI 判定全部静默丢失（2026-09-17 T9 回归防线）。"""

    def complete(self, messages, max_tokens=4096):
        return super().complete(messages, max_tokens=max_tokens)


class _NoKeyProvider(_StubProvider):
    def available(self) -> bool:
        return False


ITEM = {"mineru": {"text": "CoO @LIG"}, "paddleocr": {"text": "CoO_x@LIG"},
        "type": "text_conflict", "page": 3,
        "evidence": {"m_ctx": "however, CoO @LIG is", "p_ctx": "however, CoO_x@LIG is",
                     "local_snippet": "however, CoOx@LIG is"},
        "third_vote": {"verdict": "paddleocr", "decisive": True, "reason": "M✗P✓"}}


class TestParseSuggestions:
    def test_parses_array(self):
        got = _parse_suggestions(
            '[{"id":0,"action":"replace","suggested_text":"$CoO_x$","confidence":0.9,'
            '"reason":"合并下标","evidence":["M","P"]}]')
        assert got and got[0]["id"] == 0 and got[0]["suggested_text"] == "$CoO_x$"

    def test_tolerates_prose(self):
        raw = ('我们需要判断。{"id":0,"action":"keep","suggested_text":"",'
               '"confidence":0.4,"reason":"M对"}  结论如上。')
        got = _parse_suggestions(raw)
        assert got and got[0]["action"] == "keep"

    def test_prompt_mentions_local_evidence(self):
        assert "PDF 自带文本层" in _SYSTEM_SYNTH and "综合" in _SYSTEM_SYNTH


class TestSynthesize:
    def test_returns_suggested_text(self):
        prov = _StubProvider(['[{"id":0,"action":"replace","suggested_text":"$\\\\mathrm{CoO}_x@LIG$",'
                              '"confidence":0.85,"reason":"合并M结构+P下标","evidence":["M","P","local"]}]'])
        out = synthesize([ITEM], provider=prov, paper="t")
        assert out[0].action == "replace"
        assert out[0].suggested_text == "$\\mathrm{CoO}_x@LIG$"
        assert out[0].verdict == "paddleocr"          # 兼容旧统计
        assert out[0].evidence == ["M", "P", "local"]
        # 证据面：M/P 片段 + 上下文 + 文本层原文都要进 prompt
        p = prov.prompts[0]
        assert "CoO @LIG" in p and "CoO_x@LIG" in p and "CoOx@LIG" in p

    def test_prose_only_response_is_unresolved(self):
        prov = _StubProvider(["我先分析一下：M 与 P 的差异在于下标……（没有 JSON）",
                              "还是没有 JSON"])
        out = synthesize([ITEM], provider=prov, paper="t")
        assert out[0].verdict == "unresolved" and not out[0].suggested_text

    def test_no_provider_key(self):
        out = synthesize([ITEM], provider=_NoKeyProvider([]), paper="t")
        assert out[0].verdict == "unresolved" and "不可用" in out[0].reason

    def test_split_retry_when_batch_all_unparsed(self):
        """整批散文丢失 → 自动降半批重试（实测：思考型模型大批时会输出长篇推理）。"""
        items = [dict(ITEM, mineru={"text": "a b"}, paddleocr={"text": "a  b"}) for _ in range(4)]
        prov = _StubProvider(["散文1", "散文2",                     # 整批 2 次尝试失败
                              '[{"id":0,"action":"keep","suggested_text":"","confidence":0.5,"reason":"r"}]',
                              '[{"id":1,"action":"keep","suggested_text":"","confidence":0.5,"reason":"r"}]'])
        out = synthesize(items, provider=prov, batch_size=4, paper="t")
        assert sum(1 for a in out if a.verdict != "unresolved") >= 1

    def test_legacy_provider_without_extra_body(self):
        """旧 provider 不认识 `extra_body` → 降级重调，不得让整篇判定丢失。"""
        prov = _LegacyStubProvider(['[{"id":0,"response":1,"a":"k"}]'])
        out = synthesize([ITEM], provider=prov, paper="t")
        assert out[0].verdict != "unresolved" and out[0].action == "keep"
        assert "CoO_x@LIG" in prov.prompts[0]



class TestGuards:
    def test_novel_tokens_flag_hallucination(self):
        assert _novel_tokens("CoO_x@LIG", ["CoO @LIG", "CoO_x@LIG"]) == []
        assert "fake7" in _novel_tokens("$\\mathrm{fake7}$", ["CoO @LIG", "CoO_x@LIG"])

    def test_shape_guard_blocks_hallucination_and_blowup(self):
        ok, _ = _synthesis_shape_ok("CoO @LIG", "CoO_x@LIG", "$\\mathrm{CoO}_x@LIG$",
                                    "however, CoOx@LIG is")
        assert ok is True
        bad, why = _synthesis_shape_ok("CoO @LIG", "CoO_x@LIG", "CoO_z9@LIG", "")
        assert bad is False and "依据" in why

    def test_shape_guard_blocks_content_loss(self):
        bad, why = _synthesis_shape_ok("conventional method", "conventio", "c")
        assert bad is False


class TestApplySynthesis:
    def test_applies_ai_suggested_text(self):
        class _A:
            id = 0
            verdict = "paddleocr"
            confidence = 0.9
            reason = "合并"
            action = "replace"
            suggested_text = "$\\mathrm{CoO}_x@LIG$"

        text = "however, CoO @LIG is stable"
        conflicts = [{"mineru": {"text": "CoO @LIG"}, "paddleocr": {"text": "CoO_x@LIG"},
                      "evidence": {"local_snippet": "however, CoOx@LIG is"}}]
        out, audit = _apply_arbitrations(text, conflicts, [_A()], apply_min_conf=0.0)
        assert out == "however, $\\mathrm{CoO}_x@LIG$ is stable"
        assert audit[0]["action"] == "ai_synth_replace"

    def test_keep_action_changes_nothing(self):
        class _A:
            id = 0
            verdict = "mineru"
            confidence = 0.9
            reason = "M 正确"
            action = "keep"
            suggested_text = ""

        text = "however, CoO @LIG is stable"
        conflicts = [{"mineru": {"text": "CoO @LIG"}, "paddleocr": {"text": "CoO_x@LIG"}}]
        out, audit = _apply_arbitrations(text, conflicts, [_A()], apply_min_conf=0.0)
        assert out == text and audit == []

    def test_hallucinated_suggestion_rejected(self):
        class _A:
            id = 0
            verdict = "paddleocr"
            confidence = 0.9
            reason = "编的"
            action = "replace"
            suggested_text = "CoO_fake9@LIG"

        text = "however, CoO @LIG is stable"
        conflicts = [{"mineru": {"text": "CoO @LIG"}, "paddleocr": {"text": "CoO_x@LIG"},
                      "evidence": {"local_snippet": "however, CoOx@LIG is"}}]
        out, audit = _apply_arbitrations(text, conflicts, [_A()], apply_min_conf=0.0)
        assert out == text, "幻觉内容必须被拒"
        assert audit and audit[0]["action"] == "skip_synth_guard"
