#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: tests/test_mineru_client.py
功能: T5 MinerU 客户端单元测试（HTTP 打桩，不消耗真实配额）
对外接口: 无（测试）
版本: v1.0.0 (2025-xx-xx)
版本历史:
  v1.0.0 初始版本
"""
import json
import socket
import zipfile
from pathlib import Path

import pytest

from paperparse.config import AppConfig
from paperparse.core.mineru_client import MineruClient, QuotaTracker, _items_to_blocks
from paperparse.middleware.errors import PaperError


@pytest.fixture
def client(monkeypatch, tmp_work):
    cfg = AppConfig(mineru_api_key="sk-test", mineru_base_url="https://mineru.net/api/v4")
    return MineruClient(cfg)


# ---------- content_list 映射 ----------

def test_items_to_blocks():
    items = [
        {"type": "text", "text": "hello", "bbox": [0, 0, 100, 20], "page_idx": 0},
        {"type": "image", "text": "", "bbox": [0, 30, 90, 60], "page_idx": 1},
        {"type": "table", "text": "| a |", "bbox": [0, 0, 10, 10], "page_idx": 1},
        {"bad": "item"},
    ]
    blocks, max_page = _items_to_blocks(items)
    assert max_page == 2
    assert len(blocks) == 3
    assert blocks[0].kind == "body" and blocks[0].page == 1
    assert blocks[1].kind == "figure" and blocks[1].page == 2
    assert blocks[2].kind == "table"
    assert blocks[0].source == "mineru"


def test_parse_zip(tmp_work):
    cl = [
        {"type": "text", "text": "para one", "bbox": [0, 0, 100, 20], "page_idx": 0},
        {"type": "text", "text": "para two", "bbox": [0, 25, 100, 45], "page_idx": 0},
    ]
    zip_path = tmp_work / "r.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("x_content_list.json", json.dumps(cl))
        z.writestr("full.md", "# demo")
    client = MineruClient(AppConfig(mineru_api_key="k"))
    result = client.parse_zip(zip_path)
    assert result.source == "mineru"
    assert len(result.blocks) == 2
    # 修复后：full.md 解压到 zip 同目录，raw_path 指向本地真实路径（非 zip 内部名）
    assert result.raw_path == str(zip_path.parent / "full.md")
    assert Path(result.raw_path).read_text(encoding="utf-8") == "# demo"
    assert result.pages == 1


# ---------- HTTP 路径（打桩） ----------

def test_submit_auth_error(client, monkeypatch):
    class Resp:
        status_code = 401
        def json(self):
            return {}
        @property
        def text(self):
            return "unauthorized"
    monkeypatch.setattr("requests.post", lambda *a, **k: Resp())
    with pytest.raises(PaperError) as ei:
        client.submit("https://x/y.pdf")
    assert ei.value.code == "PAPER-0012"


def test_submit_ok(client, monkeypatch):
    class Resp:
        status_code = 200
        def json(self):
            return {"code": 0, "data": {"task_id": "t-1"}}
    monkeypatch.setattr("requests.post", lambda *a, **k: Resp())
    assert client.submit("https://x/y.pdf") == "t-1"


def test_poll_done(client, monkeypatch):
    class Resp:
        status_code = 200
        def json(self):
            return {"code": 0, "data": {"state": "done",
                                        "full_zip_url": "https://cdn/x.zip"}}
    monkeypatch.setattr("requests.get", lambda *a, **k: Resp())
    data = client.poll("t-1", pages=1)
    assert data["state"] == "done"
    assert "full_zip_url" in data


def test_poll_failed(client, monkeypatch):
    class Resp:
        status_code = 200
        def json(self):
            return {"code": 0, "data": {"state": "failed", "err_msg": "boom"}}
    monkeypatch.setattr("requests.get", lambda *a, **k: Resp())
    with pytest.raises(PaperError) as ei:
        client.poll("t-1", pages=1)
    assert ei.value.code == "PAPER-0013"
    assert ei.value.detail["state"] == "failed"


def test_connectivity_dns_fail(client, monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("no such host")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    ok, msg = client.check_connectivity()
    assert ok is False
    assert "DNS" in msg


def test_upload_size_limit(tmp_work):
    """超过上传上限 → PAPER-0015（调度层应降级）"""
    import os
    cfg = AppConfig(mineru_api_key="k", mineru_max_upload_bytes=1024)
    c = MineruClient(cfg)
    big = tmp_work / "big.pdf"
    big.write_bytes(b"x" * 2048)
    with pytest.raises(PaperError) as ei:
        c.extract(big)
    assert ei.value.code == "PAPER-0015"
    assert ei.value.detail["size"] == 2048


def test_413_maps_to_size_error(client, monkeypatch, tmp_work):
    fake = tmp_work / "fake.pdf"
    fake.write_bytes(b"%PDF-1.7\nsmall")
    class Resp:
        status_code = 413
        def json(self):
            return {}
        @property
        def text(self):
            return ""
    monkeypatch.setattr("requests.post", lambda *a, **k: Resp())
    with pytest.raises(PaperError) as ei:
        client.upload_file(str(fake))
    assert ei.value.code == "PAPER-0015"


# ---------- 配额 ----------

def test_quota(tmp_work):
    q = QuotaTracker(tmp_work / "quota" / "daily_pages.json", daily_limit=100)
    q.check(10)
    q.record(10)
    assert q._used == 10
    q2 = QuotaTracker(tmp_work / "quota" / "daily_pages.json", daily_limit=100)
    assert q2._used == 10  # 落盘恢复
    with pytest.raises(PaperError) as ei:
        q2.check(95)
    assert ei.value.code == "PAPER-0011"
