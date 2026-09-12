#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/middleware/audit.py
功能: 审计监督：每次运行的阶段事件写入 JSONL（只存数据摘要，不落全文），
      可查询过滤，并渲染 report.html 供用户观察数据传输过程
对外接口: Auditor / new_run_id
版本: v1.0.0 (2026-08-19)
版本历史:
  v1.0.0 初始版本
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from paperparse.config import templates_dir
from paperparse.middleware.schema import AuditEvent

__all__ = ["Auditor", "new_run_id"]

EventKind = Literal["input", "output", "error", "warning", "degrade", "info"]


def new_run_id() -> str:
    """[全局] 生成 run_id：UTC 时间戳 + 短随机串"""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{uuid.uuid4().hex[:8]}"


class Auditor:
    """[全局] 审计器：管理单个 run 的事件记录与报告

    参数:
        run_id: 运行 ID（new_run_id() 生成）
        out_dir: 审计输出目录（output/<DOI>/audit/<run_id>）
    """

    def __init__(self, run_id: str, out_dir: str | Path):
        self.run_id = run_id
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.out_dir / "events.jsonl"
        self._events: list[AuditEvent] = []

    # ---------- 写入 ----------

    def log_event(self, stage: str, kind: EventKind,
                  summary: str = "", payload: Optional[dict] = None,
                  tokens: Optional[int] = None,
                  duration_ms: Optional[int] = None) -> AuditEvent:
        """[全局] 记录一条事件（输入/输出/错误/警告/降级/信息）

        参数:
            stage: 阶段名（S0~S7 / api 函数名）
            kind: 事件类型
            summary: 人类可读摘要（一句话）
            payload: 数据摘要（只放条数/大小/关键字段，禁止放全文）
            tokens: 本次消耗 token（AI 相关事件）
            duration_ms: 耗时（毫秒）
        返回:
            AuditEvent（同时追加到 events.jsonl）
        """
        ev = AuditEvent(
            run_id=self.run_id,
            ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            stage=stage,
            kind=kind,
            summary=summary,
            payload=dict(payload) if payload else {},
            tokens=tokens,
            duration_ms=duration_ms,
        )
        self._events.append(ev)
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(ev.model_dump_json() + "\n")
        return ev

    def log_error(self, stage: str, envelope, duration_ms: Optional[int] = None) -> AuditEvent:
        """[全局] 快捷记录错误信封"""
        return self.log_event(stage, "error",
                              summary=envelope.user_message,
                              payload={"code": envelope.code, "retryable": envelope.retryable},
                              duration_ms=duration_ms)

    def log_warning(self, stage: str, code: str, summary: str) -> AuditEvent:
        """[全局] 快捷记录警告"""
        return self.log_event(stage, "warning", summary=summary, payload={"code": code})

    def log_degrade(self, stage: str, summary: str, payload: Optional[dict] = None) -> AuditEvent:
        """[全局] 快捷记录降级（如 MinerU → PyMuPDF）"""
        return self.log_event(stage, "degrade", summary=summary, payload=payload)

    def relocate(self, new_dir: str | Path) -> None:
        """[全局] 目录移动后重定位（DOI 目录重命名场景：事件文件路径跟随）"""
        self.out_dir = Path(new_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.out_dir / "events.jsonl"

    # ---------- 查询 ----------

    def query(self, stage: Optional[str] = None,
              kind: Optional[EventKind] = None) -> list[AuditEvent]:
        """[全局] 过滤查询已记录事件（内存态）

        参数:
            stage: 按阶段过滤（None=全部）
            kind: 按类型过滤（None=全部）
        返回:
            匹配的事件列表（按记录顺序）
        """
        out = self._events
        if stage:
            out = [e for e in out if e.stage == stage]
        if kind:
            out = [e for e in out if e.kind == kind]
        return out

    def load_from_disk(self) -> list[AuditEvent]:
        """[全局] 从 events.jsonl 恢复全部事件（断点/监督用）"""
        events: list[AuditEvent] = []
        if self.events_path.exists():
            with open(self.events_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        events.append(AuditEvent.model_validate_json(line))
        self._events = events
        return events

    # ---------- 报告 ----------

    def render_report(self, title: str = "PaperAIReader 运行监督报告") -> Path:
        """[全局] 渲染 report.html（jinja2 模板：audit_report.html.j2）

        返回:
            报告文件路径
        报错:
            PAPER-0040（模板缺失/渲染失败，由调用方包装）
        """
        env = Environment(
            loader=FileSystemLoader(str(templates_dir())),
            autoescape=select_autoescape(["html"]),
        )
        template = env.get_template("audit_report.html.j2")
        html = template.render(
            title=title,
            run_id=self.run_id,
            events=self._events,
        )
        report_path = self.out_dir / "report.html"
        report_path.write_text(html, encoding="utf-8")
        return report_path
