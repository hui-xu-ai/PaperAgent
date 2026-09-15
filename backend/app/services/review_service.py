# -*- coding: utf-8 -*-
"""识别复核服务（P12-6，Q4）：双通道解析的不确定仲裁点 → GUI 交互复核。

- 清单：work/dual/<pdf_stem>/review.json（text_conflict + 可疑公式）+ 块级 bbox/全文
- PDF 页图：PyMuPDF 渲染整页 PNG + 高亮矩形烘焙（PaddleOCR bbox 为 2× 画布 → 缩放 0.5，
  越界钳制到页内）
- 选择落地（Q3 分级）：A=用 mineru（保留）B=用 PaddleOCR（替换）C=两者皆可 D=都错；
  B 落地 = 段落级 before→after 替换（source_block_ids 定位），A/C/D 仅标记；
  全部记 audit + 重渲染 en.md
- 规则待确认：learned 挖掘规则（direction 缺失/未 auto）→ 批准（方向保守化，只作用
  脏侧通道）/ 丢弃
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# PaddleOCR-VL 块 bbox 坐标系为 PDF 页的 2 倍画布（实测 9 篇语料一致）
BBOX_SCALE = 0.5


def _learned_domain_path() -> Path:
    """学习闭环落点：外部规则根 rules/learned/domain.json（RULES_DIR > 向上探测）"""
    env = __import__("os").getenv("RULES_DIR", "").strip()
    if env:
        return Path(env) / "learned" / "domain.json"
    for parent in [Path.cwd(), *Path.cwd().parents]:
        if (parent / "rules").is_dir():
            return parent / "rules" / "learned" / "domain.json"
    return Path.cwd() / "rules" / "learned" / "domain.json"


class ReviewService:
    def __init__(self, settings):
        self.settings = settings

    # ---------------------------------------------------------- 定位
    # 双通道痕迹（任一存在即视为"走过双通道"）：review.json 是**有待复核项时**才写的主清单，
    # 零待复核时管线只留 arbitration_audit.jsonl / char_conflicts.json / verify.json
    # ⇒ 判据必须放宽，否则"零待复核"会被误报成"未走双通道解析"（2026-09-12 用户实测报障）。
    _DUAL_MARKERS = ("review.json", "arbitration_audit.jsonl", "char_conflicts.json",
                     "verify.json", "mineru_blocks.json", "paddleocr_blocks.json")

    def dual_dir(self, paper: dict) -> Path | None:
        """复核数据归位（2026-08-26 用户决策）：双通道产物在**解析产物目录**
        library/<DOI>/work/（doc_json 推导）——与 library 自包含一致，清理简单；
        不再用全局 work/dual/（避免与测试/他篇互相污染）。
        兼容回退：旧 dual_work_root/<stem>/（P12 遗留产物）。"""
        doc_json = paper.get("doc_json")
        if doc_json and Path(doc_json).exists():
            d = Path(doc_json).resolve().parent / "work"
            if any((d / n).exists() for n in self._DUAL_MARKERS):
                return d
        try:
            stem = Path(paper.get("pdf_path", "")).stem
        except Exception:  # noqa: BLE001
            stem = ""
        if stem:
            d = Path(self.settings.dual_work_root) / stem
            if any((d / n).exists() for n in self._DUAL_MARKERS):
                return d
        return None

    def _blocks_map(self, dual_dir: Path, name: str) -> dict:
        try:
            data = json.loads((dual_dir / name).read_text(encoding="utf-8"))
            return {b["block_id"]: b for b in data if b.get("block_id")}
        except Exception:  # noqa: BLE001
            return {}

    def _kb_root(self) -> Path:
        """知识库根（**运行时**取：`app.config.APP_DATA_DIR` 属性查找 ⇒ 测试/换根可 monkeypatch）。"""
        try:
            from . import container
            root = container.get_kb().root()
            if root:
                return Path(root)
        except Exception:  # noqa: BLE001 - 未装配容器（单测直调）走 APP_DATA_DIR
            pass
        from ..config import APP_DATA_DIR
        return Path(APP_DATA_DIR) / "knowledge_base"

    def _resource_dir(self, root: Path, names: list[str]) -> Path | None:
        """`<root>/<资源目录>` 存在性命中（复用 `paperkb.resource.candidate_dirnames`，
        **不新造目录规则**——RID / 裸 DOI / 目录名三种写法归一到同一目录）。"""
        from paperkb.resource import candidate_dirnames

        for key in names:
            if not key:
                continue
            for name in candidate_dirnames(str(key)):
                cand = Path(root) / name
                if cand.is_dir():
                    return cand
        return None

    def _pdf_path(self, paper: dict) -> str:
        """源 PDF 定位（F1 修复 2026-09-12）。

        背景（用户实测报障：复核页左侧 PDF「阅读区」加载不出、`page_count=0`）：
        `papers.pdf_path` 指向解析期**暂存** `work/upload/<uuid>/xxx.pdf`（work 是
        "随时可清"区，解析完即删）⇒ 旧实现的兜底 `doc_json.parent.parent` 少了一层
        （glob 到 `library/` 根而非 `library/<资源>/`），且**从不看知识库副本**——
        于是暂存被清理的文献源 PDF 定位失败（页面 500、页数取不到），而已解析文献的
        暂存还在，表现为"算法区别对待"。

        顺序（命中即返回）：① `pdf_path` 原值（存在才用）② `library/<资源>/source.pdf`
        ③ `knowledge_base/<资源>/source.pdf` ④ `library/<资源>/*.pdf`（历史 `<DOI>.pdf`）
        ⑤ `doc_json` 同目录任意 `*.pdf`。全不命中 → 回退 `pdf_path` 原值（供上层报"源文件缺失"）。
        """
        p = Path(str(paper.get("pdf_path") or ""))
        if str(p) not in ("", ".") and p.is_file():
            return str(p)

        doc_json = Path(str(paper.get("doc_json") or ""))
        doc_ok = str(doc_json) not in ("", ".") and doc_json.exists()
        lib_dir = doc_json.resolve().parent if doc_ok else None

        # 资源目录名候选：doc_json 父目录名（权威，就是 library/<资源>/）> pdf_name stem > doi
        names: list[str] = []
        for cand in (lib_dir.name if lib_dir else "",
                     Path(str(paper.get("pdf_name") or "")).stem,
                     str(paper.get("doi") or "")):
            if cand and cand not in names:
                names.append(cand)

        for root in (Path(str(getattr(self.settings, "engine_work_root", "") or "")), self._kb_root()):
            if str(root) in ("", "."):
                continue
            res = self._resource_dir(root, names)
            if res is None:
                continue
            src = res / "source.pdf"
            if src.is_file():
                return str(src)
            for other in sorted(res.glob("*.pdf")):     # 历史命名 <DOI>.pdf
                return str(other)
        if lib_dir is not None:
            for other in sorted(lib_dir.glob("*.pdf")):  # doc_json 同目录兜底
                return str(other)
        return str(paper.get("pdf_path") or "")   # 原值（供上层报"源文件缺失"，不臆造路径）

    # ---------------------------------------------------------- 复核清单
    def pending_review_count(self, paper: dict) -> int:
        """P12F 复核门控：待人工复核项数（与前端 isHandled 一致：
        user_choice / auto_resolved / ai.applied 之外 + 非正文区排除 才算待处理）。
        轻量实现（只读 review.json，不做 diff/bbox——供任务门控/队列高频调用）。"""
        d = self.dual_dir(paper)
        if d is None:
            return 0
        try:
            review = json.loads((d / "review.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return 0
        pending = 0
        for it in review.get("items", []):
            if it.get("user_choice") or it.get("auto_resolved") or \
                    (it.get("ai") or {}).get("applied"):
                continue
            if ((it.get("mineru") or {}).get("kind", "") in ("meta", "footer", "header")):
                continue
            pending += 1
        return pending

    def get_review(self, paper: dict) -> dict:
        d = self.dual_dir(paper)
        if d is None:
            return {"available": False, "dual": False,
                    "reason": "该文献未走双通道解析（解析产物里没有双通道痕迹）"}
        if not (d / "review.json").exists():
            # 走过双通道、但零待复核项（差异已自动仲裁/无冲突）——文案必须区分，
            # 否则用户以为"没走双通道"（2026-09-12 实测报障）
            return {"available": False, "dual": True, "work_dir": str(d),
                    "reason": "已走双通道解析，本次无待复核项（差异已被自动仲裁或本页无冲突）"}
        try:
            review = json.loads((d / "review.json").read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            return {"available": False, "dual": True, "work_dir": str(d),
                    "reason": f"复核清单读取失败: {e}"}
        m_blocks = self._blocks_map(d, "mineru_blocks.json")
        p_blocks = self._blocks_map(d, "paddleocr_blocks.json")
        pdf_path = self._pdf_path(paper)
        items = []
        for idx, it in enumerate(review.get("items", [])):
            mid = (it.get("mineru") or {}).get("block_id", "")
            pid = (it.get("paddleocr") or {}).get("block_id", "")
            mb = m_blocks.get(mid) or {}
            pb = p_blocks.get(pid) or {}
            ai = it.get("ai") or {}
            po_text = (pb.get("text") or "") or (it.get("paddleocr") or {}).get("text", "")
            mi_text = (mb.get("text") or "") or (it.get("mineru") or {}).get("text", "")
            # 差异字段标红（P12 反馈：人工只需关注差异处）——先去 HTML 标签（<sup> 等）
            # 再字符级 diff（标签不参与比对，避免乱码/无意义标红），公式 $ 保留
            mi_clean, po_clean = self._clean_html_tags(mi_text), self._clean_html_tags(po_text)
            mi_html, po_html = self._diff_html(mi_clean, po_clean)
            # 非正文区（meta/footer/header）展示层也标记已处理（D11；已有清单立即生效）
            kind = (mb or {}).get("kind", "")
            auto_resolved = it.get("auto_resolved", "")
            if kind in ("meta", "footer", "header") and not it.get("user_choice"):
                auto_resolved = "non_body"
            items.append({
                "idx": idx,
                "page": it.get("page", 0),
                "bbox": (pb.get("bbox") or mb.get("bbox") or [0, 0, 0, 0]),
                # 行级精确高亮（P12 反馈修复）：PDF 文本层定位（PDF 坐标，与页图同系）
                "line_bboxes": self._line_bboxes(pdf_path, it.get("page", 1),
                                                 po_text, pb.get("bbox")),
                "mineru_text": mi_text[:800],
                "paddleocr_text": po_text[:800],
                "mineru_html": mi_html,
                "paddleocr_html": po_html,
                "ai_verdict": ai.get("verdict", ""),
                "ai_reason": ai.get("reason", ""),
                "confidence": ai.get("confidence", 0),
                "applied": bool(ai.get("applied")),
                "user_choice": it.get("user_choice", ""),
                "auto_resolved": auto_resolved,
                "latex_valid": it.get("latex_valid"),
            })
        page_count = 0
        try:
            import pymupdf
            _doc = pymupdf.open(pdf_path)
            page_count = _doc.page_count
            _doc.close()
        except Exception:  # noqa: BLE001 - 页数取不到则前端退回 items 页集合
            page_count = 0
        return {"available": True, "items": items, "page_count": page_count,
                "ai_stats": review.get("ai", {}),
                "work_dir": str(d), "total": len(items)}

    @staticmethod
    def _clean_html_tags(t: str) -> str:
        """去掉 HTML 标签（<sup>/<sub>/<i>/<b> 等，内容保留）——diff 前清洗，
        避免把标签本身当差异标红（P12 反馈：<sup> 标红乱码）。"""
        import re as _re
        return _re.sub(r"</?(?:sup|sub|i|em|b|strong|u|span|font)[^>]*>", "", t or "")

    @staticmethod
    def _diff_tokens(t: str) -> list[tuple[int, int]]:
        """token 化：公式（$...$/$$...$$）整体 / 空白 / 词块为单元，
        返回 (start, end) 原文区间。公式永不拆散（P12F：字符级 diff 会把
        `$1 0 0 ~ ^ { \\circ } \\mathrm { C } .$` 逐字符切进 <mark>，
        `$` 孤立标红且 KaTeX 无法配对渲染 = "markdown 与标红语法冲突"）。"""
        import re as _re
        out: list[tuple[int, int]] = []
        pos = 0
        for m in _re.finditer(r"\$\$[\s\S]+?\$\$|\$[^\$\n]+?\$|\s+", t or ""):
            if m.start() > pos:
                out.append((pos, m.start()))        # 词块（含未闭合 $ 等）
            out.append((m.start(), m.end()))
            pos = m.end()
        if pos < len(t or ""):
            out.append((pos, len(t)))
        return out

    @staticmethod
    def _diff_html(a: str, b: str) -> tuple[str, str]:
        """token 级 diff → 两版 HTML（差异 token 包 <mark class="rv-diff">）。
        replace 两侧标红；delete/insert 仅一侧有内容 → 缺失侧插灰色「∅」占位。
        标红最小粒度 = token（公式/词/空白单元），公式整体保留可渲染。
        **文本段一律 html.escape（& < >）**，只保留生成的结构标签 →
        输出可直接 innerHTML（防 PDF 文本注入标签）。"""
        import difflib
        import html as _html
        esc = lambda s: _html.escape(s or "", quote=False)
        ta, tb = ReviewService._diff_tokens(a or ""), ReviewService._diff_tokens(b or "")
        seq_a = [a[s:e] for s, e in ta]
        seq_b = [b[s:e] for s, e in tb]
        sm = difflib.SequenceMatcher(None, seq_a, seq_b, autojunk=False)
        parts_a: list[str] = []
        parts_b: list[str] = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            seg_a = esc(a[ta[i1][0]:ta[i2 - 1][1]]) if i1 < i2 else ""
            seg_b = esc(b[tb[j1][0]:tb[j2 - 1][1]]) if j1 < j2 else ""
            if tag == "equal":
                if seg_a:
                    parts_a.append(seg_a)
                if seg_b:
                    parts_b.append(seg_b)
            elif tag == "replace":
                if seg_a:
                    parts_a.append(f'<mark class="rv-diff">{seg_a}</mark>')
                if seg_b:
                    parts_b.append(f'<mark class="rv-diff">{seg_b}</mark>')
            elif tag == "delete":      # 仅 a 有 → a 标红，b 插占位（长段加缺失提示）
                if seg_a:
                    note = ('<span class="rv-note">（此段另一版缺失）</span>'
                            if len(seg_a) > 80 else '')
                    parts_a.append(f'<mark class="rv-diff">{seg_a}</mark>{note}')
                parts_b.append('<mark class="rv-diff rv-miss">∅</mark>')
            else:                      # insert：仅 b 有 → b 标红，a 插占位
                parts_a.append('<mark class="rv-diff rv-miss">∅</mark>')
                if seg_b:
                    note = ('<span class="rv-note">（此段另一版缺失）</span>'
                            if len(seg_b) > 80 else '')
                    parts_b.append(f'<mark class="rv-diff">{seg_b}</mark>{note}')
        return "".join(parts_a), "".join(parts_b)

    @staticmethod
    def _line_bboxes(pdf_path: str, page: int, text: str,
                     fallback_bbox: list | None = None) -> list[list[float]]:
        """行级 bbox：PyMuPDF 文本层 search_for 定位仲裁片段（PDF 坐标）；
        失败/无文本层 → 块级 bbox × 0.5 兜底（2× 画布 → PDF 坐标）。"""
        import re as _re
        probe = _re.sub(r"\s+", " ", text or "")[:60].strip()
        if len(probe) >= 4:
            try:
                import pymupdf
                doc = pymupdf.open(pdf_path)
                try:
                    if 1 <= page <= doc.page_count:
                        rects = doc[page - 1].search_for(probe)
                        if rects:
                            return [[round(r.x0, 1), round(r.y0, 1),
                                     round(r.x1, 1), round(r.y1, 1)]
                                    for r in rects[:8]]
                finally:
                    doc.close()
            except Exception:  # noqa: BLE001 - 定位失败走兜底
                pass
        try:
            x0, y0, x1, y1 = (float(v) * BBOX_SCALE for v in (fallback_bbox or [0, 0, 0, 0]))
            return [[x0, y0, x1, y1]]
        except (TypeError, ValueError):
            return []

    # ---------------------------------------------------------- PDF 页图（高亮）
    def pdf_page_png(self, paper: dict, page: int,
                     highlights: list[list[float]] | None = None,
                     zoom: float = 2.0) -> bytes:
        """PyMuPDF 渲染整页 PNG；highlights=[x0,y0,x1,y1...]（**PDF 坐标**，
        来自 _line_bboxes 行级定位；越界钳制到页内）。"""
        import pymupdf
        pdf_path = self._pdf_path(paper)
        doc = pymupdf.open(pdf_path)
        if page < 1 or page > doc.page_count:
            doc.close()
            raise ValueError(f"页码越界: {page}（共 {doc.page_count} 页）")
        p = doc[page - 1]
        rect = p.rect
        for hl in highlights or []:
            try:
                x0, y0, x1, y1 = (float(v) for v in hl[:4])
            except (TypeError, ValueError):
                continue
            # 钳制到页内（行级定位可能有细微越界）
            x0 = max(0.0, min(x0, rect.width))
            x1 = max(0.0, min(x1, rect.width))
            y0 = max(0.0, min(y0, rect.height))
            y1 = max(0.0, min(y1, rect.height))
            if x1 - x0 < 1 or y1 - y0 < 1:
                continue
            # 粗红框（无填充）：绝不遮挡原文（P12 反馈：填充色在多高亮叠加/渲染合成
            # 时出现"盖住内容"观感 → 弃用填充，只描边框）
            p.draw_rect(pymupdf.Rect(x0, y0, x1, y1),
                        color=(0.88, 0.08, 0.04), width=2.0)
        pix = p.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        data = pix.tobytes("png")
        doc.close()
        return data

    # ---------------------------------------------------------- 选择落地
    def apply_choice(self, paper: dict, item_idx: int, choice: str) -> dict:
        """Q3 分级落地：A=mineru（保留）/ B=paddleocr（替换）/ C=both / D=neither。
        B 落地=段落级替换（source_block_ids 定位 + 归一化匹配兜底），全部记 audit。"""
        if choice not in ("mineru", "paddleocr", "both", "neither"):
            raise ValueError(f"choice 非法: {choice}")
        d = self.dual_dir(paper)
        if d is None:
            raise ValueError("该文献无双通道复核清单")
        review = json.loads((d / "review.json").read_text(encoding="utf-8"))
        if not (0 <= item_idx < len(review.get("items", []))):
            raise ValueError(f"复核项索引越界: {item_idx}")
        it = review["items"][item_idx]
        mid = (it.get("mineru") or {}).get("block_id", "")
        pid = (it.get("paddleocr") or {}).get("block_id", "")
        m_blocks = self._blocks_map(d, "mineru_blocks.json")
        p_blocks = self._blocks_map(d, "paddleocr_blocks.json")
        before = (m_blocks.get(mid) or {}).get("text", "") or (it.get("mineru") or {}).get("text", "")
        after = (p_blocks.get(pid) or {}).get("text", "") or (it.get("paddleocr") or {}).get("text", "")

        doc_json = paper.get("doc_json")
        changed = 0
        detail = []
        # ⚠️ 必须在使用点**之前** import：下面 domain_ai 提升段用 `_time.strftime`，
        # 而函数内任何位置赋值都会让 `_time` 成为局部名 —— 旧实现把 `import time as _time`
        # 放在函数末尾，导致提升段抛 UnboundLocalError 并被 `except Exception` 静默吞掉
        # （2026-09-12 批1 实测复现：learned/domain.json 从未被写过，闭环形同不存在）。
        import time as _time
        want_po = (choice == "paddleocr")
        if doc_json and Path(doc_json).exists():
            from paperparse.core.document_builder import load_document, save_document
            doc = load_document(doc_json)
            for para in doc.paragraphs:
                if mid not in (para.source_block_ids or []):
                    continue
                t = para.text_en or ""
                applied = False
                if want_po and before and after:
                    # 落地：before → after（B=用 PaddleOCR）
                    if before and before in t:
                        para.text_en = t.replace(before, after)
                        applied = True
                    elif after and after in t:      # AI 已落地 → 无需再替换
                        applied = True
                        detail.append("已包含 PaddleOCR 文本")
                    else:                            # 归一化兜底（空白差异）
                        import re
                        tb = re.sub(r"\s+", " ", before or "").strip()
                        ta = re.sub(r"\s+", " ", after or "").strip()
                        tt = re.sub(r"\s+", " ", t).strip()
                        if tb and ta and tb in tt:
                            para.text_en = t.replace(before, after) if before in t else \
                                re.sub(r"\s+", " ", para.text_en).replace(tb, ta)
                            applied = True
                elif not want_po and before and after:
                    # 回滚（A/C/D）：段落当前是 PaddleOCR（AI 已落地/曾选 B）→ 还原 MinerU
                    # （P12F：此前选 mineru 只标记不还原，AI 误落地无法撤销 → 最终结果错）
                    import re as _re
                    tb = _re.sub(r"\s+", " ", before or "").strip()
                    ta = _re.sub(r"\s+", " ", after or "").strip()
                    tt = _re.sub(r"\s+", " ", t).strip()
                    if after and after in t:
                        para.text_en = t.replace(after, before)
                        applied = True
                    elif ta and tb and ta in tt:
                        para.text_en = _re.sub(r"\s+", " ", para.text_en).replace(ta, tb)
                        applied = True
                    elif before and before in t:
                        detail.append("已保留 MinerU 文本")   # 无需改动，不计数
                if applied:
                    changed += 1
                    detail.append(f"段落 {para.para_id} 已{'替换为 PaddleOCR' if want_po else '还原为 MinerU'}")
            if changed:
                # 2026-09-16（方案 A）：复核改动写回**定版（kb）**——与编译/翻译同一份文件。
                # 旧行为只写 library，而编译读 kb ⇒ 复核结果对编译不可见（审计 C2）。
                # 定位失败时原样写回传入路径（旧行为兜底，绝不丢用户改动）。
                try:
                    from paperkb.api import translation_target as _target

                    _t = _target(doc_json)
                except Exception as e:  # noqa: BLE001
                    logger.warning("复核写回目标定位失败（写回原路径）: %s", e)
                    _t = str(doc_json)
                save_document(doc, _t)
                try:
                    from .container import get_engine
                    # 重渲染 en.md：定版与 library 两侧都刷一次（library 仅作中转缓存的视图）
                    get_engine()._post_parse_clean(_t)   # 重渲染 en.md
                except Exception:  # noqa: BLE001 - 重渲染失败不阻塞
                    logger.warning("复核落地后重渲染失败: %s", _t)

        # 学习闭环：domain_ai 项（C 层 AI 共识扫描）确认 B → 提升为 learned 词典
        # 条目（下次解析自动应用；校验：元素表解析成功 + 带电荷）
        ev = it.get("evidence") or {}
        if want_po and ev.get("kind") == "domain_ai" and ev.get("suggestion"):
            try:
                from paperparse.core.consensus_fix import _canonical_form, load_domain_dict
                _d = load_domain_dict()
                _chk = _canonical_form(str(ev["suggestion"]), _d["_elements_set"])
                if _chk and _chk["has_charge"]:
                    learned = _learned_domain_path()
                    data = json.loads(learned.read_text(encoding="utf-8")) \
                        if learned.exists() else {"chemistry": []}
                    entry = {
                        "id": "R-DOM-L-%s-%03d" % (
                            _time.strftime("%Y%m%d"), len(data.get("chemistry", [])) + 1),
                        "formula": ev["suggestion"], "elements": _chk["elements"],
                        "charge": _chk["charge"], "unicode": ev.get("unicode", ""),
                        "latex": "", "name": "", "confidence": 0.9,
                        "enabled": True, "note": "learned from GUI review (AI consensus scan)",
                        "source": "ai_review",
                    }
                    if not any(e.get("formula") == ev["suggestion"]
                               for e in data.get("chemistry", [])):
                        data.setdefault("chemistry", []).append(entry)
                        learned.parent.mkdir(parents=True, exist_ok=True)
                        learned.write_text(
                            json.dumps(data, ensure_ascii=False, indent=1),
                            encoding="utf-8")
                        detail.append("已提升 learned 词典: %s" % ev["suggestion"])
            except Exception as _e:  # noqa: BLE001 - 提升失败不阻塞落地
                logger.warning("learned 词典提升失败: %s", _e)

        # 标记 + audit
        it["user_choice"] = choice
        it["resolved_at"] = _time.strftime("%Y-%m-%d %H:%M:%S")
        audit_entry = {"item_idx": item_idx, "choice": choice, "page": it.get("page"),
                       "block_id": mid, "before": before[:200], "after": after[:200],
                       "changed_paragraphs": changed, "detail": detail}
        audit_path = d / "review_audit.json"
        try:
            audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else []
            audit.append(audit_entry)
            audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        (d / "review.json").write_text(
            json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": True, "item_idx": item_idx, "choice": choice,
                "changed_paragraphs": changed, "detail": detail}

    # 2026-09-12 批1：删除 pending_rules / decide_rule（learned **挖掘**规则，随 P12 退役）。
    # 依据（已核代码）：pending_rules 只列 `source=="mining"` 的规则，而 P14 生产链不产生
    # 这类规则、rule_engine 只在已归档的老降级链应用它们 ⇒ 清单恒空、批准无实际作用。
    # ⚠️ domain_ai 化学式词典闭环**不在这里**：它由 apply_choice 直接写
    #    rules/learned/domain.json（见上方"学习闭环"段），与规则库无关，未受影响。
        return {"ok": False}
