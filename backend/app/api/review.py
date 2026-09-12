# -*- coding: utf-8 -*-
"""识别复核 API（P12-6，Q4）：复核清单 / PDF 页图高亮 / 选择题落地。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from ..services import container

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/papers", tags=["review"])


def _paper(paper_id: int) -> dict:
    paper = container.get_store().get_paper(paper_id)
    if not paper:
        raise HTTPException(404, "论文不存在")
    return paper


@router.get("/{paper_id}/review")
def get_review(paper_id: int) -> dict:
    """双通道复核清单（text_conflict/可疑公式 + bbox + 两版文本 + AI 建议）。"""
    return container.get_review().get_review(_paper(paper_id))


@router.get("/{paper_id}/review/pdf-page/{page}")
def review_pdf_page(paper_id: int, page: int,
                    hl: str | None = Query(None,
                                           description="高亮矩形 x0,y0,x1,y1;...（PDF 坐标）")) -> Response:
    """PyMuPDF 渲染整页 PNG + 高亮烘焙（复核视图左栏；复用通用渲染器）。"""
    highlights = []
    if hl:
        for part in hl.split(";"):
            try:
                highlights.append([float(v) for v in part.split(",")[:4]])
            except ValueError:
                continue
    try:
        data = container.get_review().pdf_page_png(_paper(paper_id), page, highlights)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        logger.warning("PDF 页图渲染失败 paper=%s page=%s: %s", paper_id, page, e)
        raise HTTPException(500, f"PDF 页图渲染失败: {e}")
    return Response(content=data, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


class ChoiceModel(BaseModel):
    item_idx: int
    choice: str      # mineru | paddleocr | both | neither


@router.post("/{paper_id}/review/choose")
def choose_item(paper_id: int, body: ChoiceModel) -> dict:
    """选择题落地（Q3）：A=mineru 保留 / B=paddleocr 替换 / C=both / D=neither。"""
    try:
        return container.get_review().apply_choice(_paper(paper_id),
                                                   body.item_idx, body.choice)
    except ValueError as e:
        raise HTTPException(400, str(e))


# 2026-09-12 批1：删除 `GET/POST /{paper_id}/review/rules`（与 /api/settings/rules 重复且
# 无调用方）。learned 挖掘规则已随 P12 退役——domain_ai 化学式词典闭环走 apply_choice，
# 不经这两个端点。
