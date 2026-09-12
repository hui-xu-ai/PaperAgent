# -*- coding: utf-8 -*-
"""/api/kb/pdf 读取知识库 source.pdf 二进制端点单测。

最小 FastAPI + TestClient + monkeypatch container._kb（FakeKB 只提供 root()，
不触碰真实 APP_DATA_DIR/knowledge_base）。走真实 HTTP 管线，覆盖：
成功（200 + application/pdf + 原样字节）、缺 source.pdf（404）、
空/`..` DOI（400 路径穿越拒绝）、缺 doi 查询参数（422）。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.kb as kb_api
from app.services import container
from paperkb.doi import doi_to_dirname


class FakeKB:
    """最小知识库服务替身：仅提供 root()（指向 tmp_path/knowledge_base）。"""

    def __init__(self, root: Path):
        self._root = root

    def root(self) -> Path:
        return self._root


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    """最小 app（仅挂 kb 路由）+ FakeKB；monkeypatch 自动恢复 container._kb。"""
    kb_root = tmp_path / "knowledge_base"
    kb_root.mkdir(parents=True)
    monkeypatch.setattr(container, "_kb", FakeKB(kb_root))
    app = FastAPI()
    app.include_router(kb_api.router)
    return TestClient(app), kb_root


# ---------------------------------------------------------------- 成功路径

def test_pdf_returned_ok(env):
    client, root = env
    doi = "10.1000/a.1"
    d = root / doi_to_dirname(doi)
    d.mkdir(parents=True)
    payload = b"%PDF-1.4\n%%fake content"
    (d / "source.pdf").write_bytes(payload)

    r = client.get(f"/api/kb/pdf?doi={doi}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.content == payload


def test_pdf_filename_is_dirname_pdf(env):
    client, root = env
    doi = "10.1000/a.1"
    d = root / doi_to_dirname(doi)
    d.mkdir(parents=True)
    (d / "source.pdf").write_bytes(b"%PDF-1.4")

    r = client.get(f"/api/kb/pdf?doi={doi}")
    assert "filename=" in r.headers.get("content-disposition", "")


# ---------------------------------------------------------------- 缺失 / 非法

def test_pdf_missing_source_404(env):
    client, root = env
    doi = "10.1000/b.2"
    (root / doi_to_dirname(doi)).mkdir(parents=True)  # 目录存在但无 source.pdf
    assert client.get(f"/api/kb/pdf?doi={doi}").status_code == 404


def test_pdf_missing_dir_404(env):
    client, _ = env
    assert client.get("/api/kb/pdf?doi=10.1000/never.9").status_code == 404


def test_pdf_empty_doi_400(env):
    client, _ = env
    assert client.get("/api/kb/pdf?doi=").status_code == 400
    assert client.get("/api/kb/pdf?doi=%20%20").status_code == 400  # 纯空白


def test_pdf_traversal_dotdot_400(env):
    client, _ = env
    # doi_to_dirname("..") 不净化 "." → 必须被显式拒绝（防越出 root）
    assert client.get("/api/kb/pdf?doi=..").status_code == 400


def test_pdf_safe_dirname_with_dot_not_overblock(env):
    client, root = env
    # 正常目录名含 "."（DOI 目录），跨请求不被误拦
    doi = "10.1000/a.1"
    d = root / doi_to_dirname(doi)
    d.mkdir(parents=True)
    (d / "source.pdf").write_bytes(b"%PDF-1.4")
    assert client.get(f"/api/kb/pdf?doi={doi}").status_code == 200


def test_pdf_missing_doi_param_422(env):
    client, _ = env
    assert client.get("/api/kb/pdf").status_code == 422
