#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_paddleocr_client.py
功能: P11-1 PaddleOCR-VL 客户端单元测试（HTTP 打桩，不消耗真实配额）
对外接口: 无（测试）
版本: v1.0.0 (2026-08-22)
版本历史:
  v1.0.0 初始版本（对齐实测 JSONL 分块结构：result.layoutParsingResults=页面列表）
"""
import json
import socket
from pathlib import Path

import pytest

from paperparse.config import AppConfig
from paperparse.core.paddleocr_client import PaddleOCRClient
from paperparse.middleware.errors import PaperError


@pytest.fixture
def client():
    cfg = AppConfig(paddleocr_access_token="tok-test",
                    paddleocr_base_url="https://paddleocr.aistudio-app.com",
                    paddleocr_model_version="PaddleOCR-VL-1.6")
    return PaddleOCRClient(cfg)


def _resp(status=200, payload=None, text=None):
    class Resp:
        status_code = status
        def __init__(self):
            self._payload = payload
            self._text = text if text is not None else (json.dumps(payload) if payload else "")
        def json(self):
            return self._payload
        @property
        def text(self):
            return self._text
        def raise_for_status(self):
            if not (200 <= self.status_code < 300):
                raise RuntimeError("HTTP %s" % self.status_code)
    return Resp()


def _page(pr_list):
    """构造一个页面对象（分块内）"""
    return {
        "prunedResult": {"parsing_res_list": pr_list, "width": 1191, "height": 1565},
        "markdown": {"text": "# page md", "images": {}},
        "outputImages": {}, "inputImage": "",
    }


def _chunk(pages):
    return {"logId": "L", "result": {
        "layoutParsingResults": pages,
        "dataInfo": {"numPages": len(pages), "pages": [{"width": 1191, "height": 1565}] * len(pages)},
    }, "errorCode": None, "errorMsg": None}


# ---------- JSONL → blocks（实测分块结构） ----------

def test_jsonl_to_blocks_chunked(client):
    chunks = [
        _chunk([
            _page([
                {"block_label": "doc_title", "block_content": "Title Here", "block_bbox": [50, 30, 500, 60]},
                {"block_label": "text", "block_content": "para one", "block_bbox": [50, 80, 500, 100]},
                {"block_label": "header", "block_content": "RESEARCH ARTICLE", "block_bbox": [0, 0, 100, 20]},
                {"block_label": "", "block_content": ""},  # 空内容跳过
            ]),
            _page([
                {"block_label": "formula", "block_content": "x^2", "block_bbox": [50, 80, 500, 100]},
            ]),
        ]),
        _chunk([
            _page([
                {"block_label": "reference_content", "block_content": "[1] ref", "block_bbox": [0, 0, 10, 10]},
            ]),
        ]),
        {"logId": "L2", "result": {"dataInfo": {}}, "errorCode": None, "errorMsg": None},  # 脏块跳过
    ]
    blocks, max_page = client._jsonl_to_blocks(chunks)
    assert max_page == 3
    assert len(blocks) == 5
    assert blocks[0].kind == "title" and blocks[0].page == 1
    assert blocks[1].kind == "body" and blocks[1].page == 1 and blocks[1].text == "para one"
    assert blocks[2].kind == "header" and blocks[2].page == 1
    assert blocks[3].kind == "equation" and blocks[3].page == 2
    assert blocks[4].kind == "reference" and blocks[4].page == 3
    assert all(b.source == "paddleocr" for b in blocks)


def test_jsonl_to_blocks_bad_bbox(client):
    chunks = [_chunk([_page([
        {"block_label": "text", "block_content": "bad bbox", "block_bbox": ["a", "b"]},
        {"block_label": "text", "block_content": "ok", "block_bbox": [1, 2, 3, 4]},
    ])])]
    blocks, max_page = client._jsonl_to_blocks(chunks)
    assert max_page == 1
    assert blocks[0].bbox == (0.0, 0.0, 0.0, 0.0)
    assert blocks[1].bbox == (1.0, 2.0, 3.0, 4.0)


# ---------- HTTP 路径（打桩 session / requests） ----------

def test_submit_auth_error(client, monkeypatch, tmp_work):
    fake = tmp_work / "f.pdf"
    fake.write_bytes(b"%PDF-1.7")
    monkeypatch.setattr(client._session, "post",
                        lambda *a, **k: _resp(401, {"msg": "bad token"}))
    with pytest.raises(PaperError) as ei:
        client.submit_file(fake)
    assert ei.value.code == "PAPER-0017"


def test_submit_ok(client, monkeypatch, tmp_work):
    fake = tmp_work / "f.pdf"
    fake.write_bytes(b"%PDF-1.7")
    monkeypatch.setattr(client._session, "post",
                        lambda *a, **k: _resp(200, {"code": 0, "data": {"jobId": "j-1"}}))
    assert client.submit_file(fake) == "j-1"


# ---------- P15 官方云队列满等待机制 ----------

def test_submit_queue_full_waits_then_ok(client, monkeypatch, tmp_work):
    """400+code 10010（队列满）→ 等待重试 → 第二次成功；on_wait 上报、不抛错"""
    fake = tmp_work / "f.pdf"
    fake.write_bytes(b"%PDF-1.7")
    calls = {"n": 0}
    waits = []

    def fake_post(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return _resp(400, {"code": 10010, "msg": "queue full"})
        return _resp(200, {"code": 0, "data": {"jobId": "j-2"}})

    monkeypatch.setattr(client._session, "post", fake_post)
    job_id = client.submit_file(fake, queue_retry_interval=0.01,
                                on_wait=lambda sec, at: waits.append((sec, at)))
    assert job_id == "j-2"
    assert calls["n"] == 2
    assert len(waits) == 1 and waits[0][1] == 1


def test_submit_queue_full_cancel(client, monkeypatch, tmp_work):
    """等待期间用户取消（cancel_check=True）→ 抛"用户取消" PAPER-0018"""
    fake = tmp_work / "f.pdf"
    fake.write_bytes(b"%PDF-1.7")
    monkeypatch.setattr(client._session, "post",
                        lambda *a, **k: _resp(400, {"code": 10010, "msg": "queue full"}))
    with pytest.raises(PaperError) as ei:
        client.submit_file(fake, queue_retry_interval=0.01,
                           cancel_check=lambda: True)
    assert ei.value.code == "PAPER-0018"
    assert "用户取消" in ei.value.detail["reason"]


def test_submit_queue_full_timeout(client, monkeypatch, tmp_work):
    """等待超时（queue_wait_sec）→ 抛"队列持续繁忙" PAPER-0018"""
    fake = tmp_work / "f.pdf"
    fake.write_bytes(b"%PDF-1.7")
    monkeypatch.setattr(client._session, "post",
                        lambda *a, **k: _resp(400, {"code": 10010, "msg": "queue full"}))
    with pytest.raises(PaperError) as ei:
        client.submit_file(fake, queue_wait_sec=1, queue_retry_interval=0.01)
    assert ei.value.code == "PAPER-0018"
    assert "超时" in ei.value.detail["reason"]


def test_submit_other_400_not_queued(client, monkeypatch, tmp_work):
    """非队列满的 400（code≠10010）→ 正常抛 PAPER-0016（不进入等待）"""
    fake = tmp_work / "f.pdf"
    fake.write_bytes(b"%PDF-1.7")
    monkeypatch.setattr(client._session, "post",
                        lambda *a, **k: _resp(400, {"code": 9999, "msg": "bad request"}))
    with pytest.raises(PaperError) as ei:
        client.submit_file(fake)
    assert ei.value.code == "PAPER-0016"


def test_wait_done(client, monkeypatch):
    monkeypatch.setattr(client._session, "get",
                        lambda *a, **k: _resp(200, {"code": 0, "data": {
                            "state": "done",
                            "resultUrl": {"jsonUrl": "https://cdn/r.jsonl"}}}))
    data = client.wait_done("j-1", max_sec=30)
    assert data["state"] == "done"
    assert data["resultUrl"]["jsonUrl"]


def test_wait_failed(client, monkeypatch):
    monkeypatch.setattr(client._session, "get",
                        lambda *a, **k: _resp(200, {"code": 0, "data": {
                            "state": "failed", "errorMsg": "boom"}}))
    with pytest.raises(PaperError) as ei:
        client.wait_done("j-1", max_sec=30)
    assert ei.value.code == "PAPER-0018"


def test_connectivity_dns_fail(client, monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("no such host")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    ok, msg = client.check_connectivity()
    assert ok is False
    assert "DNS" in msg


def test_parse_pdf_full(client, monkeypatch, tmp_work):
    """全链路打桩：submit → done → jsonl → blocks + 备份"""
    import requests
    fake = tmp_work / "f.pdf"
    fake.write_bytes(b"%PDF-1.7")
    chunk_json = json.dumps(_chunk([_page([
        {"block_label": "text", "block_content": "para", "block_bbox": [0, 0, 10, 10]}])]))

    def fake_post(*a, **k):
        return _resp(200, {"code": 0, "data": {"jobId": "j-9"}})

    def fake_get(*a, **k):
        url = a[0] if a else k.get("url", "")
        if "jobs/j-9" in url:
            return _resp(200, {"code": 0, "data": {
                "state": "done",
                "resultUrl": {"jsonUrl": "https://cdn/r.jsonl"}}})
        return _resp(200, None, text=chunk_json)

    monkeypatch.setattr(client._session, "post", fake_post)
    monkeypatch.setattr(client._session, "get", fake_get)
    monkeypatch.setattr(requests, "get", fake_get)   # fetch_jsonl 用全局 requests.get
    blocks = client.parse_pdf(fake, backup=True)
    assert blocks.source == "paddleocr"
    assert len(blocks.blocks) == 1
    assert Path(blocks.raw_path).exists()
