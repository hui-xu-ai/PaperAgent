# -*- coding: utf-8 -*-
"""对话服务测试：稳定前缀、局部检索、回答缓存、消息入库（FakeChat 不联网）。"""
from __future__ import annotations

import pytest

from app.services.chat_service import ChatService, _is_report_task
from conftest import ENGINE_DOC


class _StubSettingsService:
    """最小 settings_service：检索模式 notes / 无附加指令（测试隔离）。"""
    def get_system_prompt_extra(self):
        return ""

    def get_retrieval_mode(self):
        return "notes"


class _StubKbMeta:
    """最小 kbmeta 访问器：编译状态/召回为空（不联网，测试隔离）。"""
    def paper_compiled(self, doi):
        return []

    def recall_paper(self, doi, query, top_k=4):
        return []

    def recall(self, query, top_k=6):
        return []


@pytest.fixture
def chat(settings, store, engine, fake_chat):
    return ChatService(settings, store, engine, fake_chat,
                       settings_service=_StubSettingsService(),
                       kbmeta=lambda: _StubKbMeta())


def _make_translated_paper(store, tmp_path):
    """构造一篇已解析论文（复用引擎现有 document.json）。"""
    pid = store.create_paper(r"input\a.pdf", "test.pdf")
    store.update_paper(pid, doc_json=str(ENGINE_DOC), status="translated")
    return pid


def test_system_prompt_stable(chat, store, tmp_path):
    pid = _make_translated_paper(store, tmp_path)
    paper = store.get_paper(pid)
    p1 = chat._paper_system_prompt(paper)
    p2 = chat._paper_system_prompt(paper)
    assert p1 == p2  # 前缀逐字节稳定 → 前缀缓存命中前提
    assert "章节索引" in p1
    assert "P001" in p1


def test_retrieve_local(chat, store, tmp_path):
    pid = _make_translated_paper(store, tmp_path)
    paper = store.get_paper(pid)
    items = chat._retrieve(paper, "What is the artificial muscle actuation performance?")
    assert len(items) >= 1
    # 只返回片段：单段不超过 limit_chars 上限
    for it in items:
        assert len(it["text"]) <= 1500 + 50


def test_ask_stream_and_persist(chat, store, tmp_path):
    pid = _make_translated_paper(store, tmp_path)
    sid = chat.create_session(pid)
    events = list(chat.ask_stream(sid, "核心贡献是什么？"))
    types = [e["type"] for e in events]
    # 契约 2（思考强度/流式体验）：检索**开始前**追加 status 事件（追加式演进，
    # 老前端忽略未知 type 不受影响）；论文路径的 start 仍在检索后才发（含真实片段数）。
    assert types == ["status", "start", "delta", "done"]
    assert events[0]["text"] == "正在检索知识库…"
    assert not events[1]["cached"]
    # 消息入库（user + assistant）
    msgs = store.recent_messages(sid, 10)
    assert msgs[-2]["role"] == "user"
    assert msgs[-1]["role"] == "assistant"
    assert msgs[-1]["content"] == "这是基于论文片段的测试回答。"
    # 历史消息与 user/assistant 一一对应（无全文混入）
    for m in msgs:
        assert m["content"] != "" and len(m["content"]) < 2000


def test_answer_cache_hit(chat, store, tmp_path):
    pid = _make_translated_paper(store, tmp_path)
    sid = chat.create_session(pid)
    list(chat.ask_stream(sid, "同一个问题"))
    n_calls = len(chat.chat.calls)
    events = list(chat.ask_stream(sid, "同一个问题"))
    assert events[0]["cached"] is True
    assert events[-1]["cached"] is True
    assert len(chat.chat.calls) == n_calls  # 未再次调用 LLM → 0 token


def test_normalize_question():
    assert ChatService._norm_question("  What   is  X?!! ") == ChatService._norm_question("what is x")
    assert ChatService._norm_question("不同") != ChatService._norm_question("相同")


def test_retrieve_full_mode(chat, store, tmp_path):
    """T05 L2：授权全文读取 library/<DOI>/en.md（T4：<DOI>.md 已废弃）。"""
    from pathlib import Path
    import shutil
    lib = Path(tmp_path) / "library" / "10.1002_adma.202407106"
    doc_dir = lib / "intermediate"
    doc_dir.mkdir(parents=True)
    doc = doc_dir / "document.json"
    shutil.copy2(ENGINE_DOC, doc)
    (lib / "en.md").write_text("FULL TEXT " * 1000, encoding="utf-8")

    pid = store.create_paper(str(tmp_path / "a.pdf"), "t.pdf")
    store.update_paper(pid, doc_json=str(doc), status="translated")
    paper = store.get_paper(pid)

    items = chat._retrieve_full(paper, max_chars=2000)
    assert items and items[0]["para_id"] == "full"
    assert len(items[0]["text"]) <= 2000


def test_fit_history_token_budget(settings, store, engine, fake_chat):
    """P1：历史改按 token 预算裁剪（不再纯条数 12）——超预算丢最旧、至少保最后 1 条。"""
    from dataclasses import replace

    chat = ChatService(replace(settings, history_token_budget=140), store, engine,
                       fake_chat, settings_service=_StubSettingsService(),
                       kbmeta=lambda: _StubKbMeta())
    msgs = [
        {"role": "user", "content": "A" * 50},        # ~25 token
        {"role": "assistant", "content": "B" * 50},   # ~25 token
        {"role": "user", "content": "C" * 200},       # ~100 token（最新）
    ]
    fitted = chat._fit_history(msgs)
    assert fitted[-1]["content"] == "C" * 200     # 最新必保留
    assert fitted[0]["content"] == "B" * 50       # 预算内向前保留到 B
    assert len(fitted) == 2                       # 最旧 A 超出预算被丢

    tiny = ChatService(replace(settings, history_token_budget=5), store, engine,
                       fake_chat, settings_service=_StubSettingsService(),
                       kbmeta=lambda: _StubKbMeta())
    assert len(tiny._fit_history(msgs)) == 1      # 预算极小也至少保最后 1 条
    assert tiny._fit_history(msgs)[-1]["content"] == "C" * 200


def test_manage_rounds_config(chat):
    """工具循环轮次上限取自 config：综述/写作类任务放大，其余用默认。"""
    # 默认 config 值（conftest settings 未覆盖 manage_* → 默认生效）
    assert chat.settings.manage_tools_max_rounds == 6
    assert chat.settings.manage_review_max_rounds == 12
    assert chat.settings.manage_retrieval_budget_chars > 0
    # 综述任务放大
    assert chat._manage_round_limit("帮我写一份人工肌肉的文献综述") == 12
    assert chat._manage_round_limit("写一篇关于 DRL 的 review") == 12
    # 非综述任务保持默认
    assert chat._manage_round_limit("编译 cej") == 6
    assert chat._manage_round_limit("知识库里有哪些关于人工肌肉的文献？") == 6


def test_is_report_task_keywords():
    assert _is_report_task("写一份文献综述")
    assert _is_report_task("汇总成报告")
    assert _is_report_task("write a survey")
    assert not _is_report_task("编译 cej")
    assert not _is_report_task("知识库里有哪些文献")


# ── 用户反馈 2026-09-12：针对文献提问「没有利用已上传的文献信息」 ──
# 机理：`_retrieve` 是逐词交集匹配，**中文问题 × 未翻译的英文正文 = 恒 0 命中**
# （实测该篇 text_zh 段落 0 个 / 95 个正文段落命中 0；英文问题命中 14~26）；
# 而旧代码把"编译笔记补充"放在 `if not retrieved` 兜底**之前** ⇒ 笔记（中文六维）
# 一旦被 FTS 命中，正文兜底就永不执行 → 模型只拿到 题名 + 章节索引 + 六维笔记。

def test_sample_body_returns_real_paragraphs(chat, store, tmp_path):
    """结构采样兜底本身要能取到**真实正文**（不是标题/大纲）。"""
    pid = _make_translated_paper(store, tmp_path)
    paper = store.get_paper(pid)
    items = chat._sample_body(paper)
    assert items, "结构采样应返回正文片段"
    assert all(it.get("para_id") for it in items)
    total = sum(len(it.get("text") or "") for it in items)
    assert total > 300, f"采样内容太短（{total} 字符），疑似只取到标题"


def test_chinese_question_gets_body_even_when_notes_hit(settings, store, engine,
                                                         fake_chat, tmp_path):
    """核心回归（**关闭共享前缀**时的检索兜底路径）：中文问题 + 笔记命中时，
    上下文里**仍必须有正文段落**（结构采样），不能只有题名+章节索引+笔记。"""
    from dataclasses import replace

    class _NotesStub(_StubKbMeta):
        def recall_paper(self, doi, query, top_k=4):
            return [{"doi": doi, "file": "_note.md",
                     "snippet": "六维笔记：创新点……", "source": "notes"}]

        def paper_notes_all(self, doi, max_files=2, max_chars=4000):
            return []

    c = ChatService(replace(settings, paper_fulltext_prefix_chars=0), store, engine,
                    fake_chat, settings_service=_StubSettingsService(),
                    kbmeta=lambda: _NotesStub())
    pid = _make_translated_paper(store, tmp_path)
    paper = store.get_paper(pid)
    q = "这篇文献的创新点是什么"
    # 前提：中文问题在正文里 0 命中（bug 的触发条件，必须成立否则测不到东西）
    assert c._retrieve(paper, q) == [], "前置条件失效：该问题竟然命中了正文"
    body = c._sample_body(paper)
    assert body, "结构采样必须有内容"

    sid = c.create_session(pid)
    list(c.ask_stream(sid, q))
    user_msg = c.chat.calls[-1][1][-1]["content"]
    assert "六维笔记" in user_msg, "编译笔记仍应进上下文（不能为了兜底把它挤掉）"
    probe = (body[0].get("text") or "")[:40]
    assert probe and probe in user_msg, "正文段落必须进上下文（旧代码这里只有题名+章节索引+笔记）"


def test_english_question_still_uses_keyword_hits(chat, store, tmp_path):
    """反向保护：英文问题命中正文时**不该**被结构采样替换掉（保持精确检索优先）。"""
    pid = _make_translated_paper(store, tmp_path)
    paper = store.get_paper(pid)
    hits = chat._retrieve(paper, "What is the artificial muscle actuation performance?")
    assert hits, "英文问题应命中正文"
    ids = {it.get("para_id") for it in hits}
    sampled = {it.get("para_id") for it in chat._sample_body(paper)}
    assert ids != sampled or len(ids) < len(sampled), "精确命中不应等同于全篇采样"


# ── 用户设计 2026-09-12：原文全文进 **system 稳定前缀**（上传一次、之后按缓存价提问）──

def _paper_with_en_md(store, tmp_path, doi_dir="10.1002_adma.202407106"):
    """造一篇：document.json + 同目录 en.md（模拟 library/<资源>/ 的真实布局）。"""
    from pathlib import Path
    import shutil
    lib = Path(tmp_path) / "library" / doi_dir
    lib.mkdir(parents=True, exist_ok=True)
    doc = lib / "document.json"
    shutil.copy2(ENGINE_DOC, doc)
    (lib / "en.md").write_text("FULLTEXT-MARKER " + ("paper body sentence. " * 50),
                               encoding="utf-8")
    pid = store.create_paper(str(tmp_path / "a.pdf"), "t.pdf")
    store.update_paper(pid, doc_json=str(doc), status="translated")
    return store.get_paper(pid)


def test_fulltext_goes_into_stable_user_prefix(chat, store, tmp_path):
    """全文前缀必须**字节稳定**，且以 `role=user` 打头（与编译请求同形 ⇒ 可继承其缓存）。"""
    paper = _paper_with_en_md(store, tmp_path)
    shared = chat._paper_shared_ctx(paper)
    assert shared, "应能构造共享全文前缀（paperkb.context.shared_ctx）"
    assert "## 论文全文" in shared, "应含编译侧 CTX_HEADER 的同一段标题"
    import re
    assert re.search(r"\[P\d", shared), f"应带段落 ID（[Pxxx]）便于引用: {shared[:200]}"
    msgs = chat._paper_prefix_messages(paper, shared, chat._paper_system_prompt(paper))
    assert msgs[0]["role"] == "user", "编译侧是 role=user 承载 shared_ctx，问答必须一致"
    assert msgs[0]["content"].startswith(shared), "shared_ctx 必须在最前面（首 token 决定缓存）"
    assert msgs[1]["role"] == "assistant"
    # 同一篇两次构造逐字节一致
    again = chat._paper_prefix_messages(paper, chat._paper_shared_ctx(paper),
                                        chat._paper_system_prompt(paper))
    assert again[0]["content"] == msgs[0]["content"]


def test_ask_stream_uses_shared_prefix_messages(chat, store, tmp_path):
    """全文前缀可用 ⇒ 本轮不塞检索片段；且 messages 结构 = [user 前缀, assistant, 问题]。"""
    paper = _paper_with_en_md(store, tmp_path)
    sid = chat.create_session(paper["id"])
    events = list(chat.ask_stream(sid, "这篇文献的创新点是什么"))
    start = next(e for e in events if e["type"] == "start")
    assert start["fulltext_prefix"] is True
    assert start["retrieved"] == 0
    msgs = chat.chat.calls[-1][1]
    assert msgs[0]["role"] == "user" and "论文全文" in msgs[0]["content"]
    assert msgs[1]["role"] == "assistant"
    assert msgs[-1] == {"role": "user", "content": "这篇文献的创新点是什么"}


def test_fulltext_prefix_can_be_disabled(settings, store, engine, fake_chat, tmp_path):
    """`PAPER_FULLTEXT_PREFIX_CHARS=0` → 退回"每轮检索片段"模式（可回退的开关）。"""
    from dataclasses import replace

    c = ChatService(replace(settings, paper_fulltext_prefix_chars=0), store, engine,
                    fake_chat, settings_service=_StubSettingsService(),
                    kbmeta=lambda: _StubKbMeta())
    paper = _paper_with_en_md(store, tmp_path)
    assert c._paper_shared_ctx(paper) == "", "关闭后不应构造共享前缀"
    sid = c.create_session(paper["id"])
    events = list(c.ask_stream(sid, "这篇文献的创新点是什么"))
    start = next(e for e in events if e["type"] == "start")
    assert start["fulltext_prefix"] is False
    assert start["retrieved"] > 0, "关闭前缀后应有检索片段兜底（结构采样）"
    assert c.chat.calls[-1][1][0]["role"] == "system", "退回旧结构（system 承载指令）"
