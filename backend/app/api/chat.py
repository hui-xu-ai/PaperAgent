# -*- coding: utf-8 -*-
"""对话 API：会话管理 + SSE 流式问答 + qna 写回。"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterator

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..services import container

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


class SessionOut(BaseModel):
    id: int
    paper_id: int | None = None
    kind: str = "paper"
    mode: str = ""
    created_at: str


class ChatRequest(BaseModel):
    session_id: int
    question: str
    # 思考强度（可选，契约 1）："low" | "high" | "auto" | null。
    # 缺省 / "auto" / null / **非法值** 一律视为"不发 reasoning_effort"（用供应商默认）。
    effort: str | None = None


class CreateSessionRequest(BaseModel):
    kind: str = "paper"          # paper / global
    paper_id: int | None = None


class CreateSessionRequest(BaseModel):
    kind: str = "paper"          # paper / global / chat
    paper_id: int | None = None
    mode: str = ""               # global 会话模式：qa（精简问答）/ manage（管理模式）


class QnaWritebackRequest(BaseModel):
    session_id: int
    label: str = "追问与回答"


# ---------------------------------------------------------------- 会话
@router.post("/api/papers/{paper_id}/sessions")
def create_session(paper_id: int) -> SessionOut:
    store = container.get_store()
    if not store.get_paper(paper_id):
        raise HTTPException(404, "论文不存在")
    sid = container.get_chat().create_session(paper_id, kind="paper")
    sess = store.get_session(sid)
    return SessionOut(**sess)


@router.post("/api/sessions")
def create_any_session(req: CreateSessionRequest) -> SessionOut:
    """通用会话创建（V07/V11/2026-08-27/T3）：kind=global 知识库（mode=qa|manage）/
    chat 普通聊天 / paper 文献 / lit 文献检索 / writing 写作。"""
    if req.kind not in ("paper", "global", "chat", "lit", "writing"):
        raise HTTPException(400, "kind 仅支持 paper/global/chat/lit/writing")
    if req.kind == "paper" and not req.paper_id:
        raise HTTPException(400, "文献会话需要 paper_id")
    if req.kind == "global" and req.mode not in ("", "qa", "manage"):
        raise HTTPException(400, "global 会话 mode 仅支持 qa/manage")
    sid = container.get_chat().create_session(req.paper_id, kind=req.kind,
                                              mode=req.mode)
    sess = container.get_store().get_session(sid)
    return SessionOut(**sess)


@router.get("/api/sessions")
def list_all_sessions() -> dict:
    """全部会话（跨论文，V07/U2），带论文标题 + 展示标题（首问摘要）。"""
    store = container.get_store()
    out = []
    for s in store.list_sessions():
        title = ""
        if s.get("paper_id"):
            p = store.get_paper(s["paper_id"])
            # P2-6：显示原始 PDF 文件名（用户文件名有意义），不用论文标题
            title = (p or {}).get("pdf_name") or (p or {}).get("title", "")
        first_q = store.first_user_message(s["id"])
        display = first_q[:24] + ("…" if len(first_q) > 24 else "")
        out.append({**s, "paper_title": title, "display_title": display,
                    "title": s.get("title", ""),   # P2-7：用户自定义会话名（可改名）
                    "messages": store.count_messages(s["id"]),
                    "tokens": store.tokens_total(s["id"])})
    return {"sessions": out}


class RenameSessionRequest(BaseModel):
    title: str


@router.post("/api/sessions/{session_id}/rename")
def rename_session(session_id: int, req: RenameSessionRequest) -> dict:
    """P2-7：会话改名（所有对话名均可修改）。"""
    store = container.get_store()
    title = req.title.strip()
    if not title:
        raise HTTPException(400, "名称不能为空")
    if not store.rename_session(session_id, title):
        raise HTTPException(404, "会话不存在")
    return {"ok": True, "title": title}


@router.delete("/api/sessions/{session_id}")
def delete_session(session_id: int) -> dict:
    """删除会话（含消息，不可恢复；U2 仅删除）。"""
    store = container.get_store()
    if not store.delete_session(session_id):
        raise HTTPException(404, "会话不存在")
    container.get_event_bus().publish("info", "chat", "session_deleted",
                                      f"会话 {session_id} 已删除", {"session_id": session_id})
    return {"ok": True}


@router.get("/api/papers/{paper_id}/sessions")
def list_sessions(paper_id: int) -> dict:
    store = container.get_store()
    sessions = store.list_sessions(paper_id)
    out = []
    for s in sessions:
        out.append({**s, "messages": store.count_messages(s["id"]),
                    "tokens": store.tokens_total(s["id"])})
    return {"sessions": out}


# ---------------------------------------------------------------- 历史
@router.get("/api/chat/{session_id}/history")
def chat_history(session_id: int, limit: int = 100) -> dict:
    store = container.get_store()
    if not store.get_session(session_id):
        raise HTTPException(404, "会话不存在")
    messages = store.recent_messages(session_id, limit)
    return {"messages": messages, "total": len(messages)}


# ---------------------------------------------------------------- 流式问答
@router.post("/api/chat/stream")
def chat_stream(req: ChatRequest) -> StreamingResponse:
    """SSE 流式回答，事件追加式演进（未知 type 老前端直接忽略即可）：

    - `{"type":"start", "cached":bool, "retrieved":int}`；
    - `{"type":"status", "text":"正在检索知识库…"}` —— 检索**开始前**发（空白气泡兜底）；
    - `{"type":"reasoning", "text":"<思维链增量>"}` —— 供应商返回 delta.reasoning_content 时逐片发；
    - `{"type":"delta", "text":"<正文增量>"}` / `{"type":"tool", ...}`；
    - `{"type":"done", "cached":bool, "message_id":int|null, "tokens":int}`；
    - `{"type":"error", "message":str}`。

    body 可选 `effort`（"low"/"high"/"auto"/null）：只有 low/high 才会发送
    `reasoning_effort`；缺省/auto/null/非法值 = 不发送（用供应商默认）。该值进回答缓存
    key —— 换档不会命中旧档缓存。
    """
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "问题不能为空")

    def gen() -> Iterator[str]:
        # 生成器内任何异常（含首个事件前的 DB/容器错误）都必须变成 error 事件：
        # 否则 StreamingResponse 已发 200 头，异常只进 uvicorn stderr（不落日志文件），
        # 前端拿到空流 → 表现为"（无回答）"且无现场（2026-09-19 实测踩坑）。
        try:
            chat = container.get_chat()
            for event in chat.ask_stream(req.session_id, question, effort=req.effort):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            logger.exception("SSE 问答流中断 session=%s", req.session_id)
            payload = {"type": "error", "message": f"服务端异常: {e}"}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------- qna 写回
@router.post("/api/chat/{session_id}/qna-writeback")
def qna_writeback(req: QnaWritebackRequest) -> dict:
    """把会话中讨论的问答写回 ai_summary（**仅用户明确要求时调用**）。"""
    store = container.get_store()
    session = store.get_session(req.session_id)
    if not session:
        raise HTTPException(404, "会话不存在")
    paper = store.get_paper(session["paper_id"])
    if not paper or not paper["doc_json"]:
        raise HTTPException(404, "论文未解析")
    messages = store.recent_messages(req.session_id, 200)
    qa_pairs = []
    i = 0
    while i < len(messages) - 1:
        m, nxt = messages[i], messages[i + 1]
        if m["role"] == "user" and nxt["role"] == "assistant":
            qa_pairs.append({"question": m["content"], "answer": nxt["content"]})
            i += 2
        else:
            i += 1
    if not qa_pairs:
        raise HTTPException(400, "会话中没有可写回的问答对")
    result = container.get_engine().qna_writeback(
        paper["doc_json"], qa_pairs, label=req.label)
    return {"written": len(qa_pairs), **result}
