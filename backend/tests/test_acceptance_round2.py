# -*- coding: utf-8 -*-
"""用户三问修复的回归测试（2026-09-12 验收轮 2）。

① L1 产物模板精简（P2：元数据移至翻译 frontmatter，_note.md 只留一句话+六维+概念标签）
② 复核页把"零待复核项"误报成"未走双通道解析"（判据只认 review.json）
③ 期刊分区表（决定 L2 的 IF 档）默认要随包可用（首启自动导入）
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from paperkb.models import PaperMeta


# ────────────────────────────────────────── ① L1 产物精简模板（P2：元数据移至翻译 frontmatter）
class TestNoteRenderMetadata:
    def _meta(self) -> PaperMeta:
        return PaperMeta(
            doi="10.1002/adma.202407106", rid="doi-10.1002_adma.202407106",
            title="Reinforced Magnetic-Responsive …", journal="Advanced Materials",
            year="2024", times_cited=11,
            authors=["A One", "B Two", "C Three", "D Four", "E Five", "F Six",
                     "G Seven", "H Eight", "I Nine", "Jianyi Zheng", "Dezhi Wu"],
            corresponding=["Jianyi Zheng", "Dezhi Wu"],
            affiliations=["Xiamen University", "Pen-Tung Sah Institute"],
            keywords=["Graphene", "Artificial muscle", "Soft robotics"],
        )

    def test_note_has_no_metadata_section(self):
        """P2：_note.md 不再含作者/单位/期刊/关键词等元数据（已移至翻译 frontmatter）。"""
        from paperkb.compile import _render_note

        note = _render_note(self._meta(), {"one_liner": "x", "tags": ["t"]}, "Q1 IF 27.4")
        assert "## 基本信息" not in note
        assert "通信作者：" not in note
        assert "研究单位：" not in note
        assert "关键词：" not in note

    def test_note_still_has_core_sections(self):
        """精简后仍保留一句话贡献 + 六维总结 + 概念标签。"""
        from paperkb.compile import _render_note

        note = _render_note(self._meta(), {"one_liner": "贡献", "tags": ["t1", "t2"]}, "—")
        assert "## 一句话贡献" in note
        assert "## 六维总结" in note
        assert "## 概念标签" in note
        assert "贡献" in note

    def test_missing_fields_do_not_break_template(self):
        from paperkb.compile import _render_note

        bare = PaperMeta(doi="10.1/x", title="T")
        note = _render_note(bare, {}, "")
        assert "## 一句话贡献" in note
        assert "## 六维总结" in note


# ────────────────────────────────────────── ② 复核判据
class TestReviewDetection:
    def _svc(self, tmp_path, work_files: dict):
        from app.services.review_service import ReviewService

        lib = tmp_path / "library" / "10.1002_adma.202407106"
        work = lib / "work"
        work.mkdir(parents=True)
        (lib / "document.json").write_text("{}", encoding="utf-8")
        for name, payload in work_files.items():
            (work / name).write_text(
                payload if isinstance(payload, str) else json.dumps(payload),
                encoding="utf-8")

        class _S:
            dual_work_root = str(tmp_path / "work" / "dual")

        return ReviewService(_S()), {"doc_json": str(lib / "document.json")}

    def test_zero_items_still_counts_as_dual(self, tmp_path):
        """只有 arbitration_audit.jsonl（零待复核）时：必须报"已走双通道、无待复核项"。"""
        svc, paper = self._svc(tmp_path, {"arbitration_audit.jsonl": "{}\n",
                                          "char_conflicts.json": "[]"})
        r = svc.get_review(paper)
        assert r["available"] is False and r["dual"] is True
        assert "已走双通道" in r["reason"] and "无待复核项" in r["reason"]

    def test_empty_review_json_is_dual_with_zero_items(self, tmp_path):
        svc, paper = self._svc(tmp_path, {"review.json": {"count": 0, "items": [],
                                                          "ai": {"pending_review": 0}}})
        r = svc.get_review(paper)
        assert r["available"] is True and r["total"] == 0
        assert svc.pending_review_count(paper) == 0

    def test_no_dual_artifacts_reports_not_dual(self, tmp_path):
        svc, paper = self._svc(tmp_path, {"something_else.txt": "x"})
        r = svc.get_review(paper)
        assert r["available"] is False and r["dual"] is False
        assert "未走双通道" in r["reason"]


# ────────────────────────────────────────── ③ 期刊分区表种子
class TestReferenceSeed:
    def test_skip_when_library_not_empty(self, tmp_path, monkeypatch):
        from app.services import reference_seed as rs

        monkeypatch.setattr(rs, "reference_stats",
                            lambda roots: {"jcr_rows": 22249, "cas_rows": 21772})
        out = rs.ensure_reference_seeded(None, object(), tmp_path, tmp_path)
        assert out["seeded"] is False and out["jcr_rows"] == 22249

    def test_copies_bundled_db_when_empty(self, tmp_path, monkeypatch):
        """用户拍板：随包放**已解析好的 db**，首启只做文件拷贝（毫秒级，不解析 xlsx）。"""
        from app.services import reference_seed as rs

        seed = tmp_path / rs.SEED_DB_REL
        seed.parent.mkdir(parents=True)
        seed.write_bytes(b"SQLite format 3\x00fake-db")

        dest = tmp_path / "install" / "data" / "reference" / "journals.db"
        monkeypatch.setattr(rs, "reference_stats",
                            lambda roots: {"jcr_rows": 0, "cas_rows": 0})
        monkeypatch.setattr(rs, "_copy_db", lambda s, d: (d.parent.mkdir(parents=True, exist_ok=True),
                                                          __import__("shutil").copy2(s, d)))
        called = {"xlsx": 0}
        kb = type("K", (), {"journals_import": lambda self, p: called.__setitem__("xlsx", 1) or {}})()

        class _Roots:
            reference_db = str(dest)

        # 拷贝后统计仍为 0（假 db）→ 但路径必须走 copy 且**不碰 xlsx 导入**
        monkeypatch.setattr(rs, "reference_stats",
                            lambda roots: {"jcr_rows": 0, "cas_rows": 0})
        out = rs.ensure_reference_seeded(_Roots(), kb, tmp_path, tmp_path)
        assert out["mode"] == "copy" and out["seeded"] is True
        assert called["xlsx"] == 0, "有随包 db 时不该再去解析 xlsx（慢 20s）"

    def test_real_copy_produces_usable_db(self, tmp_path):
        """真拷贝（不 mock）：随包 db 内容必须原样落到 data/reference/journals.db。"""
        from app.services import reference_seed as rs

        seed = tmp_path / rs.SEED_DB_REL
        seed.parent.mkdir(parents=True)
        seed.write_bytes(b"SQLite format 3\x00payload")
        dest = tmp_path / "data" / "reference" / "journals.db"
        rs._copy_db(seed, dest)
        assert dest.read_bytes() == seed.read_bytes()
        assert not list(dest.parent.glob("*.tmp")), "不许留半截临时文件"

    def test_xlsx_fallback_when_no_db(self, tmp_path, monkeypatch):
        from app.services import reference_seed as rs

        xlsx = tmp_path / rs.SEED_XLSX_REL
        xlsx.parent.mkdir(parents=True)
        xlsx.write_bytes(b"xlsx")
        monkeypatch.setattr(rs, "reference_stats", lambda roots: {"jcr_rows": 0, "cas_rows": 0})
        seen = {}

        class _K:
            def journals_import(self, path):
                seen["path"] = path
                return {"jcr_total": 22249, "cas_total": 21772}

        class _Roots:
            reference_db = str(tmp_path / "data" / "reference" / "journals.db")

        out = rs.ensure_reference_seeded(_Roots(), _K(), tmp_path, tmp_path)
        assert out["mode"] == "xlsx" and out["jcr_rows"] == 22249
        assert seen["path"].endswith("JCR分区.xlsx")

    def test_no_seed_logs_and_does_not_raise(self, tmp_path, monkeypatch):
        from app.services import reference_seed as rs

        monkeypatch.setattr(rs, "reference_stats", lambda roots: {"jcr_rows": 0, "cas_rows": 0})
        out = rs.ensure_reference_seeded(type("R", (), {"reference_db": str(tmp_path / "x.db")})(),
                                         object(), tmp_path, tmp_path)
        assert out["ok"] is False and out["seeded"] is False

    def test_spec_ships_db_and_xlsx(self):
        """spec 必须随包：① 已解析 db（首启拷）② 原始 Excel（用户日后更新）。"""
        root = Path(__file__).resolve().parents[2]
        spec = (root / "PaperAgent.spec").read_text(encoding="utf-8")
        assert "share/reference" in spec
        assert "reference_seed" in spec and "journals.db" in spec, "缺随包 db（首启毫秒级拷贝）"
        assert "JCR分区.xlsx" in spec, "缺原始 Excel（用户无法自行更新分区表）"
