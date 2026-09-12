#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/middleware/errors.py
功能: 错误体系：错误码注册表、PaperError、错误信封生成（RFC 7807 风格，用户/AI 双视角）
对外接口: ERROR_REGISTRY / PaperError / to_envelope / wrap_unknown / register_code
版本: v1.0.0 (2026-08-19)
版本历史:
  v1.0.0 初始版本（错误码表见 PLAN.md §8）
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from paperparse.middleware.schema import ErrorEnvelope

__all__ = ["ERROR_REGISTRY", "PaperError", "to_envelope", "wrap_unknown", "register_code", "PaperErrorCode"]

# 注册表条目: {code: {severity, retryable, user_message(template), recovery, ai_hint}}
_ERROR_SPECS: dict[str, dict[str, Any]] = {
    "PAPER-0001": dict(severity="error", retryable=False,
                       user_message="输入文件不存在或不可读：{path}",
                       recovery="检查文件路径与读取权限后重试。",
                       ai_hint="验证 path 存在且可读"),
    "PAPER-0002": dict(severity="error", retryable=False,
                       user_message="文件不是有效的 PDF 或已损坏。",
                       recovery="确认文件为 PDF 且未损坏（可用阅读器打开验证）。",
                       ai_hint="检查文件头/魔数与页数"),
    "PAPER-0003": dict(severity="error", retryable=False,
                       user_message="PDF 无文本层（可能是扫描版），且 OCR 未启用。",
                       recovery="请提供带文本层的 PDF，或启用 OCR（预留接口，未实现）。",
                       ai_hint="PdfInfo.is_scanned_hint 为 True"),
    "PAPER-0010": dict(severity="error", retryable=True,
                       user_message="MinerU API 网络请求失败。",
                       recovery="检查网络/代理后重试（自动指数退避）。",
                       ai_hint="requests 异常信息见 ai_detail"),
    "PAPER-0011": dict(severity="error", retryable=True,
                       user_message="MinerU API 限流或超出每日配额（{limit} 页/日）。",
                       recovery="等待配额恢复或调高 MINERU_DAILY_PAGE_LIMIT。",
                       ai_hint="检查当日用量计数文件"),
    "PAPER-0012": dict(severity="error", retryable=False,
                       user_message="MinerU API 认证失败（token 无效或过期）。",
                       recovery="检查 .env 中 MINERU_API_KEY 并在 API 管理页重新创建。",
                       ai_hint="HTTP 401/403"),
    "PAPER-0013": dict(severity="error", retryable=True,
                       user_message="MinerU 解析任务超时或失败。",
                       recovery="重试任务；若持续失败请反馈任务 ID。",
                       ai_hint="task status 见 ai_detail"),
    "PAPER-0014": dict(severity="warning", retryable=False,
                       user_message="MinerU 云端解析未产出 full.md（结果异常或通道受限），解析已中止。",
                       recovery="请重试，或更换解析通道（mineru v1 免费 / pymupdf 本地）。",
                       ai_hint="full.md 缺失原因见 ai_detail；v2.1 起云端失败不再静默降级"),
    "PAPER-0015": dict(severity="error", retryable=False,
                       user_message="MinerU 上传通道不支持该文件大小（实测上限约 750KB，当前 {size} 字节）。",
                       recovery="请改用 mineru（v1 免费云端）或 pymupdf（本地）解析，或提供公网可访问的 PDF URL 走 url 通道。",
                       ai_hint="上传限制与文件大小见 ai_detail"),
    "PAPER-0016": dict(severity="error", retryable=True,
                       user_message="PaddleOCR-VL API 网络请求失败。",
                       recovery="检查网络/代理后重试（自动指数退避）。",
                       ai_hint="requests 异常信息见 ai_detail"),
    "PAPER-0017": dict(severity="error", retryable=False,
                       user_message="PaddleOCR-VL API 认证失败（token 无效或过期）。",
                       recovery="检查 .env 中 PADDLEOCR_ACCESS_TOKEN（AI Studio Access Token 页重新创建）。",
                       ai_hint="HTTP 401/403"),
    "PAPER-0018": dict(severity="error", retryable=True,
                       user_message="PaddleOCR-VL 解析任务超时或失败。",
                       recovery="重试任务；若持续失败请反馈 job_id。",
                       ai_hint="job state/errorMsg 见 ai_detail"),
    "PAPER-0019": dict(severity="error", retryable=False,
                       user_message="PaddleOCR-VL 结果解析失败（JSONL 结构异常）。",
                       recovery="检查备份原始结果 work/paddleocr_backup/ 确认结构；若结构变化请反馈。",
                       ai_hint="解析异常详情见 ai_detail"),
    "PAPER-0020": dict(severity="error", retryable=True,
                       user_message="LLM 请求失败（网络/超时）。",
                       recovery="检查网络后重试。",
                       ai_hint="LLM 异常信息见 ai_detail"),
    "PAPER-0021": dict(severity="error", retryable=False,
                       user_message="LLM 输入超出上下文长度。",
                       recovery="减小批大小（STITCH_BATCH_PAGES）或分页处理。",
                       ai_hint="给出 token 估算与建议批大小"),
    "PAPER-0022": dict(severity="error", retryable=True,
                       user_message="LLM 输出未通过结构校验。",
                       recovery="该段/该批译文已拒绝（不重试）；可重跑 run_m5 或检查提示词与模型。",
                       ai_hint="pydantic ValidationError 详情"),
    "PAPER-0030": dict(severity="error", retryable=True,
                       user_message="图片提取失败。",
                       recovery="重试；若持续失败，中间产物中保留页面渲染。",
                       ai_hint="渲染异常信息见 ai_detail"),
    "PAPER-0040": dict(severity="error", retryable=False,
                       user_message="Markdown 模板渲染失败。",
                       recovery="检查模板文件与 document.json 结构。",
                       ai_hint="jinja2 异常信息见 ai_detail"),
    "PAPER-0101": dict(severity="warning", retryable=False,
                       user_message="部分段落拼接置信度低，已在输出中标记，建议人工复核。",
                       recovery="查看 paragraphs.json 中 needs_ai_check 段落。",
                       ai_hint="低置信段落 ID 列表见 ai_detail"),
    "PAPER-0102": dict(severity="warning", retryable=False,
                       user_message="文献元数据提取不完整（缺失字段见详情）。",
                       recovery="可提供网页版用于交叉校验。",
                       ai_hint="缺失字段列表见 ai_detail"),
    "PAPER-0103": dict(severity="warning", retryable=False,
                       user_message="参考文献解析不完整。",
                       recovery="可用网页版（verify_html）补全。",
                       ai_hint="已解析/预期数量见 ai_detail"),
    "PAPER-0104": dict(severity="warning", retryable=False,
                       user_message="已降级使用本地解析（MinerU 不可用或未配置）。",
                       recovery="无需操作；功能可用但版面信息可能较少。",
                       ai_hint="降级原因见 ai_detail"),
    "PAPER-0105": dict(severity="warning", retryable=False,
                       user_message="图片与图注数量不一致（图 {figures} 张 / 图注 {captions} 条），请人工核对位置与数量。",
                       recovery="检查 images/ 与正文图注段落；可调整图片提取阈值。",
                       ai_hint="数量与差异列表见 ai_detail"),
    "PAPER-0501": dict(severity="error", retryable=False,
                       user_message="参数非法：{detail}",
                       recovery="按合法取值调整参数后重试。",
                       ai_hint="合法取值见 ai_detail"),
    "PAPER-0601": dict(severity="error", retryable=False,
                       user_message="批量任务执行失败：{detail}",
                       recovery="查看对应 job 的审计目录。",
                       ai_hint="job_id 见 ai_detail"),
    "PAPER-9999": dict(severity="error", retryable=False,
                       user_message="未知错误：{detail}",
                       recovery="请反馈该错误码与审计目录。",
                       ai_hint="原始异常类型与阶段见 ai_detail"),
}

ERROR_REGISTRY: dict[str, dict[str, Any]] = dict(_ERROR_SPECS)
"""[全局] 错误码注册表（只读约定；新增错误码用 register_code）"""


def register_code(code: str, severity: str, retryable: bool,
                  user_message: str, recovery: str, ai_hint: str = "") -> None:
    """[全局] 注册/更新错误码（供扩展包使用）

    报错:
        ValueError: code 格式非法（必须 PAPER-NNNN）或字段缺失
    """
    import re
    if not re.fullmatch(r"PAPER-\d{4}", code):
        raise ValueError(f"错误码格式非法: {code}")
    _ERROR_SPECS[code] = dict(severity=severity, retryable=retryable,
                              user_message=user_message, recovery=recovery, ai_hint=ai_hint)
    ERROR_REGISTRY.clear()
    ERROR_REGISTRY.update(_ERROR_SPECS)


class PaperError(Exception):
    """[全局] 业务异常：携带错误码与阶段上下文

    参数:
        code: 错误码（PAPER-NNNN）
        stage: 出错阶段（如 S1 / convert_pdf）
        detail: 附加结构化信息（给 AI 的诊断数据）
        path: 关联文件路径（可选，用于 PAPER-0001 等模板）
    """

    def __init__(self, code: str, stage: str = "", detail: Optional[dict] = None,
                 path: str = "", message: str = ""):
        if code not in ERROR_REGISTRY:
            raise ValueError(f"未注册的错误码: {code}（请先 register_code）")
        self.code = code
        self.stage = stage
        self.detail = dict(detail) if detail else {}
        self.path = path
        self.message = message
        super().__init__(self._render_user_message())

    def _render_user_message(self) -> str:
        spec = ERROR_REGISTRY[self.code]
        tmpl = spec["user_message"]
        ctx: dict[str, Any] = {"detail": self.message or "", "path": self.path or ""}
        ctx.update(self.detail)
        try:
            return tmpl.format(**ctx)
        except (KeyError, IndexError):
            return tmpl


def to_envelope(err: PaperError) -> ErrorEnvelope:
    """[全局] PaperError → 错误信封（AI 与用户双视角）

    参数:
        err: PaperError 实例
    返回:
        ErrorEnvelope
    """
    spec = ERROR_REGISTRY[err.code]
    ai_detail = dict(err.detail)
    ai_detail["hint"] = spec.get("ai_hint", "")
    if err.path:
        ai_detail["path"] = err.path
    return ErrorEnvelope(
        code=err.code,
        severity=spec["severity"],
        stage=err.stage,
        retryable=spec["retryable"],
        user_message=str(err),
        recovery=spec["recovery"],
        ai_detail=ai_detail,
    )


def wrap_unknown(exc: BaseException, stage: str = "") -> PaperError:
    """[全局] 未知异常 → PAPER-9999（保证任何异常都可被调度层捕获并溯源）

    参数:
        exc: 原始异常
        stage: 出错阶段
    返回:
        PaperError（PAPER-9999，原始信息进 detail）
    """
    return PaperError("PAPER-9999", stage=stage, detail={
        "exc_type": type(exc).__name__,
        "exc_message": str(exc)[:500],
    })
