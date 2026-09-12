#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: paperparse/core/paddleocr_client.py
功能: PaddleOCR-VL 官方云 API 客户端（P11 双通道第二来源）：
      连通性自检 / 本地文件 multipart 上传 / 任务提交与轮询 /
      结果 JSONL 下载解析（→ ParserBlocks）/ 原始结果备份
对外接口: PaddleOCRClient
版本: v1.0.0 (2026-08-22)
版本历史:
  v1.0.0 初始版本（协议实测自官方 paddleocr/_api_client 源码 main 分支：
         POST/GET https://paddleocr.aistudio-app.com/api/v2/ocr/jobs[/{job_id}]
         Bearer token（AI Studio Access Token），multipart 提交 → data.jobId →
         轮询 data.state(pending/running/done/failed) + extractProgress →
         done 后 data.resultUrl.jsonUrl 拉 JSONL（每页一行原始结构））
"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import requests

from paperparse.config import AppConfig, load_config
from paperparse.middleware.errors import PaperError
from paperparse.middleware.schema import ParserBlocks, TextBlock

__all__ = ["PaddleOCRClient"]

DEFAULT_BACKUP_DIR = "work/paddleocr_backup"   # PaddleOCR 原始识别结果备份根目录
POLL_INTERVAL_SEC = 5.0
MAX_POLL_SEC = 1800                            # 单任务最长等待（秒）
QUEUE_FULL_CODE = 10010                        # 官方云「队列繁忙」业务码（P11 实证 400 + code 10010）
QUEUE_RETRY_INTERVAL_SEC = 30.0                # 队列满重试间隔
DEFAULT_QUEUE_WAIT_SEC = 600                   # 队列满最长等待（用户确认：10 分钟）

STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"


_KIND_MAP = {
    "doc_title": "title", "title": "title", "paragraph_title": "heading",
    "text": "body", "abstract": "body",
    "header": "header", "footer": "footer",
    "header_image": "figure", "footer_image": "figure", "image": "figure",
    "chart": "figure", "figure_title": "caption", "table_title": "caption",
    "footnote": "other", "number": "other", "seal": "other", "aside_text": "other",
    "table": "table", "formula": "equation", "equation": "equation",
    "reference_content": "reference", "reference_title": "heading",
}


def _brief(body: dict) -> dict:
    """[局部] 响应摘要（避免把大响应塞进错误信息）"""
    out = {}
    for k in ("code", "msg", "errorMsg", "message"):
        if k in body:
            out[k] = body[k]
    return out


def _backup_dir() -> Path:
    """[局部] 备份目录：work/paddleocr_backup/<时间戳>/（每次调用独立目录）"""
    d = Path(DEFAULT_BACKUP_DIR) / time.strftime("%Y%m%d-%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    return d


class PaddleOCRClient:
    """[全局] PaddleOCR-VL 官方云 API 客户端（适配器：可被 mock / 替换）

    协议（已实测）:
        POST/GET <base>/api/v2/ocr/jobs[/{job_id}]
        鉴权: Authorization: Bearer <token>
        提交: multipart {model, optionalPayload(json), pageRanges?, batchId?} + file
        轮询: data.state ∈ pending/running/done/failed; extractProgress{totalPages, extractedPages}
        结果: done → data.resultUrl.jsonUrl → JSONL（每页一行）
    """

    def __init__(self, cfg: AppConfig | None = None):
        self.cfg = cfg or load_config()
        self.base = self.cfg.paddleocr_base_url.rstrip("/")
        self.jobs_url = "%s/api/v2/ocr/jobs" % self.base
        self.model = self.cfg.paddleocr_model_version or "PaddleOCR-VL-1.6"
        self.timeout = self.cfg.paddleocr_timeout_sec or 120
        self._session = requests.Session()
        self._session.headers["Authorization"] = "Bearer %s" % self.cfg.paddleocr_access_token

    # ---------- 连通性自检 ----------

    def check_connectivity(self) -> tuple[bool, str]:
        """[全局] 分层诊断：DNS → HTTPS + 鉴权；返回 (ok, 详情)"""
        host = self.base.split("//")[-1].split("/")[0]
        try:
            socket.getaddrinfo(host, 443)
        except Exception as e:
            return False, "DNS 解析失败: %s (%s)" % (host, e)
        try:
            r = self._session.get(self.jobs_url, timeout=10)
            return True, "可达（HTTP %s）；鉴权 %s" % (
                r.status_code, "有效" if r.status_code != 401 else "失败(401)")
        except Exception as e:
            return False, "HTTPS 失败: %s" % e

    # ---------- HTTP 基础 ----------

    # P12F 批量限速：限流退避（初始 + 3 次，20s/40s/60s 递增）
    _RETRY_DELAYS = (20, 40, 60)

    def _request(self, method: str, url: str, **kw) -> requests.Response:
        """带限流退避的请求：429/503/504 → 退避重试 3 次（递增）；其他异常照常。
        按 method 走 _session.get/post（保持测试对 get/post 的 mock 有效）。"""
        import time as _t
        for attempt in range(len(self._RETRY_DELAYS) + 1):
            try:
                if method == "GET":
                    resp = self._session.get(url, timeout=self.timeout, **kw)
                else:
                    resp = self._session.post(url, timeout=self.timeout, **kw)
            except requests.Timeout as e:
                raise PaperError("PAPER-0016", stage="S1-paddleocr",
                                 detail={"reason": "请求超时"}) from e
            except requests.ConnectionError as e:
                raise PaperError("PAPER-0016", stage="S1-paddleocr",
                                 detail={"reason": "连接失败: %s" % e}) from e
            if resp.status_code in (429, 503, 504) and attempt < len(self._RETRY_DELAYS):
                print("  [paddleocr] 限流 HTTP %s attempt=%d 退避 %ss" % (
                    resp.status_code, attempt + 1, self._RETRY_DELAYS[attempt]))
                _t.sleep(self._RETRY_DELAYS[attempt])
                continue
            return resp
        raise PaperError("PAPER-0018", stage="S1-paddleocr",
                         detail={"reason": "限流重试耗尽"})

    def _raise_for_response(self, resp: requests.Response) -> None:
        if 200 <= resp.status_code < 300:
            return
        msg = ""
        try:
            body = resp.json()
            msg = str(_brief(body))
        except Exception:
            msg = resp.text[:200]
        if resp.status_code in (401, 403):
            raise PaperError("PAPER-0017", stage="S1-paddleocr", detail={
                "http": resp.status_code, "msg": msg})
        if resp.status_code in (429, 503, 504):
            raise PaperError("PAPER-0018", stage="S1-paddleocr", detail={
                "http": resp.status_code, "msg": msg, "retryable": True})
        raise PaperError("PAPER-0016", stage="S1-paddleocr", detail={
            "http": resp.status_code, "msg": msg})

    def _unwrap(self, resp: requests.Response) -> dict:
        try:
            payload = resp.json()
        except ValueError as e:
            raise PaperError("PAPER-0019", stage="S1-paddleocr", detail={
                "reason": "响应非 JSON", "raw": resp.text[:200]}) from e
        if not isinstance(payload, dict):
            raise PaperError("PAPER-0019", stage="S1-paddleocr", detail={
                "reason": "响应非对象", "raw": str(payload)[:200]})
        code = payload.get("code", 0)
        if code not in (0, None):
            raise PaperError("PAPER-0016", stage="S1-paddleocr", detail={
                "code": code, "msg": _brief(payload)})
        data = payload.get("data")
        if not isinstance(data, dict):
            raise PaperError("PAPER-0019", stage="S1-paddleocr", detail={
                "reason": "缺少 data 对象", "raw": str(payload)[:200]})
        return data

    # ---------- 任务提交 / 轮询 ----------

    def submit_file(self, pdf_path: str | Path, *,
                    options: dict | None = None,
                    page_ranges: str | None = None,
                    batch_id: str | None = None,
                    queue_wait_sec: int = DEFAULT_QUEUE_WAIT_SEC,
                    queue_retry_interval: float = QUEUE_RETRY_INTERVAL_SEC,
                    cancel_check=None, on_wait=None) -> str:
        """[全局] multipart 上传本地 PDF → 返回 job_id

        P15（2026-08-25）官方云**队列满等待机制**（用户决策：官方云唯一通道，
        不做失败重试/不切替代通道）：
        - 提交遇 400 + code=10010（队列繁忙）→ **后台等待重试**（间隔
          queue_retry_interval，默认 30s），不做快速失败；
        - cancel_check() 回调每轮调用（返回 True → 抛"用户取消"）；
        - on_wait(elapsed_sec, attempt) 每轮调用（backend 用它更新任务状态：
          "官方云队列繁忙，等待中"——用户可见原因）；
        - 等待超 queue_wait_sec（默认 600s）→ 抛"队列持续繁忙"。
        """
        p = Path(pdf_path)
        if not p.exists():
            raise FileNotFoundError(pdf_path)
        data = {
            "model": self.model,
            "optionalPayload": json.dumps(options or {}, ensure_ascii=False),
        }
        if page_ranges is not None:
            data["pageRanges"] = page_ranges
        if batch_id is not None:
            data["batchId"] = batch_id
        started = time.time()
        attempt = 0
        while True:
            try:
                with open(p, "rb") as f:
                    resp = self._request("POST", self.jobs_url, data=data,
                                         files={"file": f})
            except PaperError:
                raise
            # 队列满提前判定（400 + code 10010）→ 等待重试；其余 400 走正常抛错
            if resp.status_code == 400:
                try:
                    _code = (resp.json() or {}).get("code")
                except Exception:  # noqa: BLE001
                    _code = None
                if _code == QUEUE_FULL_CODE:
                    elapsed = int(time.time() - started)
                    if elapsed >= queue_wait_sec:
                        raise PaperError("PAPER-0018", stage="S1-paddleocr", detail={
                            "reason": "官方云队列持续繁忙，等待 %ss 超时（已放弃）"
                                      % int(elapsed)})
                    attempt += 1
                    if cancel_check is not None and cancel_check():
                        raise PaperError("PAPER-0018", stage="S1-paddleocr", detail={
                            "reason": "用户取消（等待官方云队列期间）"})
                    if on_wait is not None:
                        on_wait(elapsed, attempt)
                    print("  [paddleocr] 官方云队列繁忙(code=10010) 已等待 %ss，"
                          "%ss 后重试（第 %d 次）" % (
                              elapsed, queue_retry_interval, attempt))
                    time.sleep(queue_retry_interval)
                    continue
            self._raise_for_response(resp)
            data = self._unwrap(resp)
            job_id = data.get("jobId")
            if not isinstance(job_id, str) or not job_id:
                raise PaperError("PAPER-0019", stage="S1-paddleocr", detail={
                    "reason": "响应缺少 jobId", "raw": str(data)[:200]})
            return job_id

    def get_status(self, job_id: str) -> dict:
        """[全局] 单次非阻塞状态查询；返回 data dict"""
        resp = self._request("GET", "%s/%s" % (self.jobs_url, job_id))
        self._raise_for_response(resp)
        return self._unwrap(resp)

    def wait_done(self, job_id: str, max_sec: int | None = None) -> dict:
        """[全局] 轮询直到 done/failed；返回最终 status data dict"""
        max_sec = max_sec or MAX_POLL_SEC
        start = time.time()
        while True:
            data = self.get_status(job_id)
            state = data.get("state")
            ep = data.get("extractProgress") or {}
            if state == STATE_DONE:
                return data
            if state == STATE_FAILED:
                raise PaperError("PAPER-0018", stage="S1-paddleocr", detail={
                    "job_id": job_id, "state": state,
                    "errorMsg": data.get("errorMsg")})
            if state not in (STATE_PENDING, STATE_RUNNING):
                raise PaperError("PAPER-0019", stage="S1-paddleocr", detail={
                    "job_id": job_id, "state": state, "raw": str(data)[:200]})
            if time.time() - start > max_sec:
                raise PaperError("PAPER-0018", stage="S1-paddleocr", detail={
                    "job_id": job_id, "state": state, "reason": "轮询超时 %ss" % max_sec})
            # 日志: 进度摘要（不刷屏：每 30s 一次）
            if int(time.time() - start) % 30 == 0:
                print("  [paddleocr] job=%s state=%s progress=%s/%s" % (
                    job_id, state, ep.get("extractedPages"), ep.get("totalPages")))
            time.sleep(POLL_INTERVAL_SEC)

    def fetch_jsonl(self, url: str) -> list[dict]:
        """[全局] 下载结果 JSONL（每行一页的原始结构 dict）"""
        try:
            resp = requests.get(url, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise PaperError("PAPER-0016", stage="S1-paddleocr",
                             detail={"reason": "结果下载失败: %s" % e}) from e
        out: list[dict] = []
        for line in resp.text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise PaperError("PAPER-0019", stage="S1-paddleocr", detail={
                    "reason": "JSONL 行解析失败", "line": line[:200]}) from e
            out.append(obj if isinstance(obj, dict) else {"_raw": obj})
        return out

    # ---------- 结果 → ParserBlocks ----------

    def _jsonl_to_blocks(self, jsonl_lines: list[dict]) -> tuple[list[TextBlock], int]:
        """[局部] JSONL 分块条目 → TextBlock 列表 + 总页数

        实测结构（2026-08-22 adma 15 页）：
          JSONL 每行 = 一个分块 {logId, result, errorCode, errorMsg}
          result.layoutParsingResults = 页面列表（每项一页，分块内连续）
          result.dataInfo.numPages = 本块页数（15 页 = 4/4/4/3 四块）
          页面.prunedResult.parsing_res_list = 元素列表
            元素: {block_label, block_content, block_bbox:[x0,y0,x1,y1], block_id, block_order}
          页面.markdown.text = 整页 MD（含 LaTeX，供对齐/融合使用）
        """
        blocks: list[TextBlock] = []
        global_page = 0
        for line in jsonl_lines:
            result = line.get("result") if isinstance(line, dict) else None
            if not isinstance(result, dict):
                continue
            lpr = result.get("layoutParsingResults")
            if not isinstance(lpr, list):
                continue
            for page in lpr:
                global_page += 1
                pr = page.get("prunedResult") if isinstance(page, dict) else None
                res_list = pr.get("parsing_res_list") if isinstance(pr, dict) else None
                if not isinstance(res_list, list):
                    continue
                for el in res_list:
                    if not isinstance(el, dict):
                        continue
                    text = str(el.get("block_content") or "")
                    if not text:
                        continue
                    bbox_raw = el.get("block_bbox") or [0, 0, 0, 0]
                    try:
                        x0, y0, x1, y1 = (float(v) for v in bbox_raw[:4])
                    except (TypeError, ValueError):
                        x0, y0, x1, y1 = 0.0, 0.0, 0.0, 0.0
                    kind = _KIND_MAP.get(str(el.get("block_label", "")).lower(), "other")
                    blocks.append(TextBlock(
                        block_id="P%04d" % (len(blocks) + 1), page=global_page,
                        bbox=(x0, y0, x1, y1), text=text, kind=kind,
                        source="paddleocr"))
        return blocks, global_page

    def parse_pdf(self, pdf_path: str | Path, *, backup: bool = True,
                  options: dict | None = None,
                  queue_wait_sec: int = DEFAULT_QUEUE_WAIT_SEC,
                  cancel_check=None, on_wait=None) -> ParserBlocks:
        """[全局] 主入口：PDF → ParserBlocks（source="paddleocr"）

        流程: 上传 → 轮询 → 拉 JSONL → 转 blocks；原始 JSONL/JSON 备份到
        work/paddleocr_backup/<时间戳>/（备份前用户要求保留原始识别结果）。
        queue_wait_sec/cancel_check/on_wait：官方云队列满等待机制（透传
        submit_file；backend 任务取消/状态更新用）。
        """
        p = Path(pdf_path)
        job_id = self.submit_file(p, options=options,
                                  queue_wait_sec=queue_wait_sec,
                                  cancel_check=cancel_check, on_wait=on_wait)
        print("[paddleocr] submitted job=%s model=%s file=%s (%d bytes)" % (
            job_id, self.model, p.name, p.stat().st_size))
        data = self.wait_done(job_id)
        ru = data.get("resultUrl") or {}
        json_url = ru.get("jsonUrl")
        if not json_url:
            raise PaperError("PAPER-0019", stage="S1-paddleocr", detail={
                "job_id": job_id, "reason": "done 但缺少 resultUrl.jsonUrl",
                "raw": str(ru)[:200]})
        pages = self.fetch_jsonl(json_url)
        blocks, max_page = self._jsonl_to_blocks(pages)

        raw_path = None
        if backup:
            d = _backup_dir()
            (d / "job.json").write_text(
                json.dumps({"job_id": job_id, "state_data": data}, ensure_ascii=False, indent=2),
                encoding="utf-8")
            jsonl_path = d / "result.jsonl"
            jsonl_path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in pages),
                                  encoding="utf-8")
            (d / "blocks.json").write_text(
                json.dumps([b.model_dump() for b in blocks], ensure_ascii=False, indent=2),
                encoding="utf-8")
            raw_path = str(jsonl_path)
            print("[paddleocr] backup -> %s (pages=%d blocks=%d)" % (d, max_page, len(blocks)))

        return ParserBlocks(
            source="paddleocr", pages=max_page, blocks=blocks,
            raw_path=raw_path, raw_content_list=str(json_url),
            warnings=[])
