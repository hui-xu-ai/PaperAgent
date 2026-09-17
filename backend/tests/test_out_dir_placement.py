# -*- coding: utf-8 -*-
"""产物目录归位单测（★2026-09-18）。

用户实测缺陷：GUI「重试」把同一篇写成**两个目录**。
链路：`process_pdf_v2` 出图目录 = `out_dir/<输入PDF文件名>/`（`p14_pipeline.py:1961`）。
重试的输入是 `library/<RID>/source.pdf` ⇒ 目录算成 `library/source/`，用户原来看的
`library/<RID>/` 仍是旧图（**看到的等于没修**）。

修复（两处配合，缺一不可）：
  ① `EngineService._resolve_out_dir` → 返回**篇目录**（既有 document.json 所在目录 /
     同篇 md5 命中 `*/source.pdf` 的父目录 / 退回 `library/<stem>`）；
  ② `TaskManager._stage_input_with_canonical_name` → 把输入 PDF 暂存成**与篇目录同名**，
     使管线拼接出的路径正好等于篇目录（不再多一层）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.config import Settings
from app.services.engine_service import EngineService
from app.services.task_service import TaskManager


@pytest.fixture
def env(tmp_path):
    lib = tmp_path / "library"
    inp = tmp_path / "upload"
    lib.mkdir()
    inp.mkdir()
    settings = Settings(db_path=str(tmp_path / "t.db"),
                        engine_work_root=str(lib),
                        engine_input_root=str(inp),
                        dual_work_root=str(tmp_path / "dual"))
    return {"tmp": tmp_path, "lib": lib, "inp": inp, "settings": settings,
            "svc": EngineService(settings)}


def final_dir(env, pdf_path, doc_json=None) -> Path:
    """还原管线**真实落点**：`resolve_output` 给出 (产物根, 规范篇目录名)，
    输入按规范名暂存 ⇒ `process_pdf_v2` 拼接出的目录 = 根/规范名。"""
    out_root, canon = env["svc"].resolve_output(str(pdf_path), doc_json)
    staged = TaskManager._stage_input_with_canonical_name(env["settings"], str(pdf_path), canon)
    return Path(out_root) / Path(staged).stem


def test_retry_input_lands_in_canonical_dir(env):
    """① 既有 document.json + 输入是篇目录里的 source.pdf ⇒ 产物回到该篇目录"""
    d = env["lib"] / "10.1038_ncomms8258"
    d.mkdir()
    doc = d / "document.json"
    doc.write_text('{"metadata":{"doi":"10.1038/ncomms8258"}}', encoding="utf-8")
    src = d / "source.pdf"
    src.write_bytes(b"%PDF-1.4 x")
    assert final_dir(env, src, str(doc)) == d, "重试必须写回原篇目录（不得另建 source/）"


def test_first_import_keeps_stem_dir(env):
    """② 首次导入（无 doc_json，输入是上传暂存件）⇒ 落 `library/<stem>`（旧行为不变）"""
    stage = env["tmp"] / "upload" / "abc" / "10.1038_ncomms8258.pdf"
    stage.parent.mkdir(parents=True)
    stage.write_bytes(b"%PDF-1.4 y")
    assert final_dir(env, stage, None) == env["lib"] / "10.1038_ncomms8258"


def test_md5_match_recovers_canonical_dir(env):
    """③ doc_json 已失效（指向已消失目录）⇒ 按同篇 md5 找回规范篇目录"""
    d = env["lib"] / "10.1038_ncomms8258"
    d.mkdir()
    pdf = b"%PDF-1.4 same-content"
    (d / "source.pdf").write_bytes(pdf)
    other = env["tmp"] / "input.pdf"
    other.write_bytes(pdf)
    assert final_dir(env, other, str(env["lib"] / "source" / "document.json")) == d


def test_unknown_paper_falls_back_to_stem(env):
    """④ 库内无同篇 ⇒ 退回 <stem>（不会误写进别人的目录）"""
    p = env["tmp"] / "brand_new.pdf"
    p.write_bytes(b"%PDF-1.4 new")
    assert final_dir(env, p, None) == env["lib"] / "brand_new"


def test_staging_is_idempotent_when_name_already_matches(env):
    """暂存**幂等**：输入名已等于规范名时不再拷贝，路径原样返回"""
    stage = env["lib"] / "10.1038_ncomms8258.pdf"      # 名字已是规范名
    stage.write_bytes(b"%PDF-1.4 z")
    got = TaskManager._stage_input_with_canonical_name(
        env["settings"], str(stage), "10.1038_ncomms8258")
    assert got == str(stage), "名字已一致 ⇒ 不得再拷贝一次"


def test_without_the_fix_source_input_would_split(env):
    """反例固化：空库时 `source.pdf` 仍会落 `library/source`（首次导入语义）；
    但库内一旦有同篇（md5 命中），必须改判到既有篇目录。"""
    p = env["tmp"] / "source.pdf"
    p.write_bytes(b"%PDF-1.4 y")
    assert final_dir(env, p, None) == env["lib"] / "source"
    d = env["lib"] / "10.1038_ncomms8258"
    d.mkdir()
    (d / "source.pdf").write_bytes(p.read_bytes())
    assert hashlib.md5((d / "source.pdf").read_bytes()).hexdigest() == \
        hashlib.md5(p.read_bytes()).hexdigest()
    assert final_dir(env, p, None) == d, "库内已有同篇 ⇒ 必须写回该篇目录"
