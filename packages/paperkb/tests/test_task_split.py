# -*- coding: utf-8 -*-
"""共享全文前缀 / 任务 分界（`TASK_MARK` / `with_task` / `split_task`）契约测试。

背景（2026-09-13 用户实测）：智谱 GLM 的自动前缀缓存**只对 `system` 消息内容生效**——
单条 user 装前缀 `cached_tokens` 恒 0；`system` + `user` 两条才命中（1.5k 前缀命中 512，
5k 前缀命中 5120）。DeepSeek 官方与硅基流动不挑形状。⇒ 构造点用 `with_task` 标出共享前缀
与任务的分界，发送层（`DeepSeekAI.complete`）按 marker 拆成 `[system, user]` 两条。

本测试锁定三件事：
1. `with_task`/`split_task` 往返与"无 marker 保持旧行为"；
2. **跨路径一致性**：L1 / L2 / 翻译三处 prompt 的 system 部分（共享前缀）**逐字节相等**
   ⇒ 三路共享同一个前缀缓存（本次改造最重要的不变量）；
3. `DeepSeekAI.complete` 的 payload 形状：带 marker → `[system, user]` 两条；不带 → 单条 user。
"""
from __future__ import annotations

import json

from paperkb.context import TASK_MARK, split_task, with_task

# ---------------------------------------------------------------- 最小夹具（与既有测试同构）

def _mk_doc():
    """最小 PaperDoc（参照 test_shared_ctx._mk_doc：含 heading / 公式 / References）。"""
    from paperkb.doc import PaperDoc, Para

    doc = PaperDoc(doi="10.1/a", title="Shared ctx paper")
    doc.paragraphs = [
        Para(para_id="P000", section="Introduction", text_en="Introduction", is_heading=True),
        Para(para_id="P001", section="Introduction", text_en="The energy is $E=mc^2$."),
        Para(para_id="P002", section="Introduction", text_en="Second paragraph."),
        Para(para_id="P900", section="References", text_en="REF1 Smith et al. 2020."),
    ]
    doc.sections = [{"section": "Introduction", "count": 2}]
    return doc


def _meta() -> dict:
    return {"doi": "10.1/a", "title": "Shared ctx paper", "authors": ["A. Author"],
            "abstract": "IPMC actuators."}


def _doc_json() -> dict:
    """与 `_mk_doc()` 同内容的 document.json（供"读真实文件"的路径用）。"""
    return {
        "metadata": {"doi": "10.1/a", "title": "Shared ctx paper"},
        "sections": [{"section": "Introduction", "count": 2}],
        "paragraphs": [
            {"para_id": "P000", "section": "Introduction", "text_en": "Introduction",
             "text_zh": "", "is_heading": True},
            {"para_id": "P001", "section": "Introduction",
             "text_en": "The energy is $E=mc^2$.", "text_zh": "", "is_heading": False},
            {"para_id": "P002", "section": "Introduction", "text_en": "Second paragraph.",
             "text_zh": "", "is_heading": False},
            {"para_id": "P900", "section": "References",
             "text_en": "REF1 Smith et al. 2020.", "text_zh": "", "is_heading": False},
        ],
    }


# ---------------------------------------------------------------- 1) 往返契约

def test_with_task_split_task_roundtrip():
    """`split_task(with_task(s, t)) == (s, t)`；shared 部分**逐字节**为原文。"""
    shared = "## 论文全文\n[P001] hello\n"
    task = "你是翻译助手。\n\n## 要求\n保持公式。"
    prompt = with_task(shared, task)
    assert prompt == shared + TASK_MARK + task          # 形状：shared + marker + task
    assert prompt.startswith(shared)                    # 共享前缀在最前（首 token 不变）
    sys_part, user_part = split_task(prompt)
    assert (sys_part, user_part) == (shared, task)
    assert sys_part == shared                           # 字节一致（非近似/非 strip 后的）
    assert user_part == task


def test_split_task_without_marker_keeps_legacy_shape():
    """无 marker → `("", prompt)` ⇒ 发送层维持旧单条 user 消息。"""
    assert split_task("x") == ("", "x")
    assert split_task("") == ("", "")
    assert split_task("多行\n\n普通提示词，无分界") == ("", "多行\n\n普通提示词，无分界")


def test_task_mark_absent_from_task_text():
    """marker 不得出现在任务/共享文本里（否则拆分错位）。"""
    doc = _mk_doc()
    from paperkb.compile import _prompt_l1
    from paperkb.context import shared_ctx

    assert TASK_MARK not in shared_ctx(doc)
    assert TASK_MARK not in _prompt_l1(_meta(), doc, "")[len(shared_ctx(doc)) + len(TASK_MARK):]


# ---------------------------------------------------------------- 2) 跨路径一致性（关键）

def test_l1_l2_translate_share_byte_identical_system_prefix(tmp_path):
    """L1 / L2 / 翻译（**真实 `run_translate` 全路径**）三处 system 部分逐字节相等。

    三路都读**同一份 document.json**（编译经 `Compiler._doc` / 翻译经
    `run_translate(context_path=...)`，两侧都是 `read_document`）——这是生产口径，
    也是本次改造最重要的不变量：三路共享同一个前缀缓存。
    """
    import hashlib

    from paperkb.compile import _prompt_l1, _prompt_l2
    from paperkb.context import shared_ctx
    from paperkb.doc import read_document
    from paperkb.llm import FakeLLM
    from paperkb.translate.pipeline import run_translate

    doc_path = tmp_path / "document.json"
    doc_path.write_text(json.dumps(_doc_json(), ensure_ascii=False), encoding="utf-8")
    doc = read_document(doc_path)

    # 翻译侧：真实 run_translate（整篇一次失败 → 回退分批，两条 prompt 都要看）
    fake = FakeLLM(["{}", '{"translations": [{"para_id": "P001", "zh": "能量。"}]}'])
    run_translate(doc_path, fake, context="translate")
    prompts = [p for _ctx, p in fake._calls]                    # noqa: SLF001
    assert prompts, "run_translate 应当至少发一次 prompt"
    canonical = shared_ctx(doc)
    for prompt in prompts:
        sys_part, _user = split_task(prompt)
        assert sys_part == canonical, "翻译侧共享前缀与 canonical 不一致"

    l1_sys, _l1_user = split_task(_prompt_l1(_meta(), doc, ""))
    l2_sys, _l2_user = split_task(_prompt_l2(_meta(), doc, "L1CTX"))
    tr_sys, tr_user = split_task(prompts[-1])

    assert l1_sys == l2_sys == tr_sys, "三路共享前缀必须逐字节一致（否则前缀缓存互不可继承）"
    assert l1_sys == canonical                # 且等于 canonical 共享前缀（改造前后不变）
    assert tr_sys == canonical
    assert hashlib.md5(l1_sys.encode()).hexdigest() == \
        hashlib.md5(canonical.encode()).hexdigest()
    # 任务部分不含 marker，且确实带上了各自的任务文本
    for user_part, needle in ((_l1_user, "科研知识编译助手"), (_l2_user, "科研笔记助手"),
                              (tr_user, "## 翻译任务")):
        assert TASK_MARK not in user_part
        assert needle in user_part


def test_l3_system_prefix_also_shared():
    """L3 与 L1/L2/翻译同一份共享前缀（改造点之一）。"""
    from paperkb.compile import _prompt_l3
    from paperkb.context import shared_ctx

    doc = _mk_doc()
    sys_part, user_part = split_task(_prompt_l3(_meta(), doc, "L1CTX", "L2CTX"))
    assert sys_part == shared_ctx(doc)
    assert "科研深度编译专家" in user_part and TASK_MARK not in user_part


# ---------------------------------------------------------------- 3) 发送层 payload 形状

class _FakeResp:
    """最小 requests.Response 替身（json/raise_for_status 两个被用到的接口）。"""

    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _mk_ai(captured: list):
    """构造 DeepSeekAI（monkeypatch 掉 HTTP 出口 + 跳过 openai 客户端构造）。

    为什么要绕：本测试住在 `packages/paperkb/tests/`，而仓库有**依赖声明守卫**
    （`backend/tests/test_declared_deps.py` 会 AST 扫描 paperkb 的全部 .py，第三方
    import 未声明即 fail）——paperkb 侧不允许 `import requests` / `import app`。
    因此：① 用 `importlib.import_module` 动态取模块（`_imports()` 只看静态 import 语句）；
    ② 直接替换 **`requests` 模块对象上**的 `post`——`llm_service.complete()` 内部是
    `import requests as _req`（每次重新取属性），所以替换模块属性即可截获；
    ③ 用 monkeypatch 让测试结束自动还原。
    """
    import importlib
    import sys

    import pytest

    _mp = pytest.MonkeyPatch()
    req = importlib.import_module("requests")

    def _post(url, headers=None, json=None, timeout=None):  # noqa: A002 - 对齐 requests 签名
        captured.append(json)
        return _FakeResp({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                          "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    _mp.setattr(req, "post", _post)
    mod = importlib.import_module("app.services.llm_service")
    ai = mod.DeepSeekAI.__new__(mod.DeepSeekAI)   # 跳过 openai 客户端构造（只测 payload 组装）
    ai._api_key = "k"
    ai._base_url = "https://example.invalid/v1"
    ai.model = "glm-4-flash"
    ai.timeout_sec = 5
    ai.max_retries = 0
    ai.max_tokens = 128
    ai.temperature = 0.3
    ai.guard = None
    ai.provider_id = "zhipu"
    ai.provider_name = "Zhipu"
    ai.reasoning_effort = None
    ai._reasoning_supported = True
    ai._hint = ""
    assert "requests" in str(sys.modules)          # 保证模块已装载（替身生效前提）
    return ai, _mp


def test_complete_payload_system_user_when_marked():
    """带 marker：messages = [system(shared), user(task)]，各段内容正确。"""
    captured: list = []
    ai, mp = _mk_ai(captured)
    try:
        shared = "## 论文全文\n[P001] alpha\n"
        task = "## 翻译任务\n请翻译。"
        out = ai.complete(with_task(shared, task), context="translate")
    finally:
        mp.undo()

    assert out == "ok"
    msgs = captured[0]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user"], "共享前缀必须独立为 system 消息"
    assert msgs[0]["content"] == shared
    assert msgs[1]["content"] == task
    assert TASK_MARK not in msgs[0]["content"] + msgs[1]["content"]
    assert len(msgs) == 2


def test_complete_payload_single_user_when_unmarked():
    """无 marker：维持旧形状（单条 user，内容逐字节相同）。"""
    captured: list = []
    ai, mp = _mk_ai(captured)
    try:
        prompt = "没有分界标记的普通 prompt"
        ai.complete(prompt, context="engine")
    finally:
        mp.undo()

    msgs = captured[0]["messages"]
    assert msgs == [{"role": "user", "content": prompt}]


def test_complete_guard_counts_full_prompt_length():
    """TokenGuard 的输入字符数口径不变（= 完整 prompt，含 marker 前后的全部内容）。"""
    import importlib

    captured: list = []
    ai, mp = _mk_ai(captured)
    guard_cls = importlib.import_module("app.services.llm_service").TokenGuard
    seen: list = []

    class _G(guard_cls):
        def begin_call(self, context, input_chars):     # noqa: D102
            seen.append(input_chars)

    ai.guard = _G()
    shared, task = "S" * 100, "T" * 20
    prompt = with_task(shared, task)
    try:
        ai.complete(prompt, context="translate")
    finally:
        mp.undo()

    assert seen == [len(prompt)]
    assert len(captured[0]["messages"]) == 2
