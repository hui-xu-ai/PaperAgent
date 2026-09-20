#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: skill/src/paperparse/core/mineru_client.py
功能: MinerU 官方 v4 API 客户端：连通性自检 / 本地上传 / 任务提交与轮询 /
      结果 zip 下载解析（content_list.json → ParserBlocks）/ 每日页数配额
对外接口: MineruClient / QuotaTracker
版本: v1.0.0 (2026-08-19)
版本历史:
  v1.0.0 初始版本（基于实测：POST /extract/task + GET /extract/task/{id} → data.state/full_zip_url）
"""
from __future__ import annotations

import base64
import json
import logging
import shutil
import socket
import time
import zipfile
from pathlib import Path

import requests

from paperparse.config import AppConfig, load_config
from paperparse.middleware.errors import PaperError
from paperparse.middleware.schema import ParserBlocks, TextBlock

logger = logging.getLogger(__name__)

__all__ = ["MineruClient", "QuotaTracker"]

POLL_INTERVAL_SEC = 3.0
MIN_WAIT_SEC = 300        # 轮询至少等待（秒）
WAIT_PER_PAGE_SEC = 30    # 每页额外等待（秒）

# v1 签名上传通道（Mineru使用说明.md 实测验证：无需 Authorization，200MB/200页上限）
V1_PARSE_URL = "https://mineru.net/api/v1/agent/parse/file"
V1_TASK_URL = "https://mineru.net/api/v1/agent/parse/{task_id}"

DEFAULT_BACKUP_DIR = "work/mineru_backup"   # MinerU 原始识别结果备份根目录（用户要求）


def _backup_dir(channel: str) -> Path:
    """[局部] 备份目录：work/mineru_backup/<时间戳-通道>/（每次调用独立目录）"""
    d = Path(DEFAULT_BACKUP_DIR) / ("%s_%s" % (time.strftime("%Y%m%d-%H%M%S"), channel))
    d.mkdir(parents=True, exist_ok=True)
    return d

# 实测 content_list 类型（2026-08-23 adma）：text/ref_text/footer/header/chart/
# aside_text/image/page_footnote（+ 文档中可能 title/heading/table/formula/caption）
_KIND_MAP = {
    "text": "body", "image": "figure", "table": "table", "formula": "equation",
    "title": "title", "heading": "heading", "caption": "caption",
    "ref_text": "reference", "footer": "footer", "header": "header",
    "chart": "figure", "aside_text": "other", "page_footnote": "footnote",
    "interline_equation": "equation", "inline_equation": "equation",
}


def _brief(body: dict) -> dict:
    """[局部] 响应摘要（避免把大响应塞进错误信息）"""
    out = {}
    for k in ("code", "msg", "trace_id"):
        if k in body:
            out[k] = body[k]
    return out


def _items_to_blocks(items: list) -> tuple[list[TextBlock], int]:
    """[局部] content_list.json 条目 → TextBlock 列表 + 最大页数"""
    blocks: list[TextBlock] = []
    max_page = 0
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "")
        itype = str(it.get("type") or "")
        if not itype and not text:      # 无类型无文本的脏条目（如空字典）
            continue
        bbox = it.get("bbox") or [0, 0, 0, 0]
        try:
            x0, y0, x1, y1 = (float(v) for v in bbox[:4])
        except (TypeError, ValueError):
            continue
        page = int(it.get("page_idx", 0)) + 1  # page_idx 为 0 基
        max_page = max(max_page, page)
        kind = _KIND_MAP.get(str(it.get("type", "")), "other")
        blocks.append(TextBlock(
            block_id="B%04d" % (i + 1), page=page, bbox=(x0, y0, x1, y1),
            text=text, kind=kind, source="mineru"))
    return blocks, max_page


class QuotaTracker:
    """[全局] 每日页数配额（落盘 work/quota/daily_pages.json，按日期滚动）"""

    def __init__(self, quota_file: str | Path, daily_limit: int):
        self.quota_file = Path(quota_file)
        self.daily_limit = daily_limit
        self._today = time.strftime("%Y-%m-%d")
        self._used = self._load()

    def _load(self) -> int:
        if self.quota_file.exists():
            try:
                data = json.loads(self.quota_file.read_text(encoding="utf-8"))
                if data.get("date") == self._today:
                    return int(data.get("used", 0))
            except Exception:
                pass
        return 0

    def _save(self) -> None:
        self.quota_file.parent.mkdir(parents=True, exist_ok=True)
        self.quota_file.write_text(
            json.dumps({"date": self._today, "used": self._used}), encoding="utf-8")

    def check(self, pages: int) -> None:
        """[全局] 检查配额；超出抛 PAPER-0011"""
        if self._used + pages > self.daily_limit:
            raise PaperError("PAPER-0011", stage="S1", detail={
                "limit": self.daily_limit, "used": self._used, "need": pages})

    def record(self, pages: int) -> int:
        """[全局] 记录本次消耗，返回累计使用量"""
        self._used += pages
        self._save()
        return self._used


class MineruClient:
    """[全局] MinerU v4 API 客户端（适配器：可被 mock / 替换）"""

    def __init__(self, cfg: AppConfig | None = None):
        self.cfg = cfg or load_config()
        self.base = self.cfg.mineru_base_url.rstrip("/")
        self.headers = {
            "Authorization": "Bearer %s" % self.cfg.mineru_api_key,
            "Content-Type": "application/json",
        }
        # 批2：最近一次 v4 批量请求实际下发的解析参数 + 首页探测结果（排障/回溯用）
        self.last_params: dict = {}

    # ---------- 连通性自检 ----------

    def check_connectivity(self) -> tuple[bool, str]:
        """[全局] 分层诊断：DNS → HTTPS；返回 (ok, 详情)"""
        host = self.base.split("//")[-1].split("/")[0]
        try:
            socket.getaddrinfo(host, 443)
        except Exception as e:
            return False, "DNS 解析失败: %s (%s)" % (host, e)
        try:
            r = requests.get(self.base, timeout=10)
            return True, "可达（HTTP %s）" % r.status_code
        except Exception as e:
            return False, "HTTPS 失败: %s" % e

    # ---------- HTTP 基础 ----------

    # P12F 批量限速：限流退避（初始 + 3 次，20s/40s/60s 递增）
    _RETRY_DELAYS = (20, 40, 60)

    def _post(self, path: str, payload: dict) -> dict:
        import time as _t
        last_exc: Exception | None = None
        for attempt in range(len(self._RETRY_DELAYS) + 1):
            try:
                resp = requests.post(self.base + path, headers=self.headers,
                                     json=payload, timeout=self.cfg.mineru_timeout_sec)
            except requests.RequestException as exc:
                raise PaperError("PAPER-0010", stage="S1",
                                 detail={"exc": str(exc)[:300]}) from exc
            if resp.status_code in (429, 503, 504) and attempt < len(self._RETRY_DELAYS):
                last_exc = PaperError("PAPER-0010", stage="S1", detail={
                    "http": resp.status_code, "retryable": True,
                    "body": resp.text[:200]})
                logger.warning("MinerU 限流 HTTP %s attempt=%d 退避 %ss",
                               resp.status_code, attempt + 1,
                               self._RETRY_DELAYS[attempt])
                _t.sleep(self._RETRY_DELAYS[attempt])
                continue
            return self._check_response(resp)
        assert last_exc is not None
        raise last_exc

    def _check_response(self, resp: requests.Response) -> dict:
        """[局部] 统一响应检查：认证/HTTP/业务码（413 上传超限 → PAPER-0015）"""
        if resp.status_code == 413:
            raise PaperError("PAPER-0015", stage="S1", detail={
                "http": 413, "hint": "上传请求体超限（实测约 750KB）；请降级本地解析或使用 url 通道"})
        if resp.status_code in (401, 403):
            raise PaperError("PAPER-0012", stage="S1", detail={"http": resp.status_code})
        if resp.status_code >= 400:
            raise PaperError("PAPER-0010", stage="S1",
                             detail={"http": resp.status_code, "body": resp.text[:300]})
        try:
            body = resp.json()
        except Exception:
            raise PaperError("PAPER-0010", stage="S1", detail={"reason": "响应非 JSON"})
        if body.get("code") not in (0, None):
            raise PaperError("PAPER-0010", stage="S1", detail=_brief(body))
        return body

    # ---------- 上传 / 提交 / 轮询 ----------

    def upload_file(self, pdf_path: str | Path) -> str:
        """[全局] 本地 PDF → 文件 URL（POST /file-urls/batch；实测字段为 name+file_content）"""
        p = Path(pdf_path)
        b64 = base64.b64encode(p.read_bytes()).decode("ascii")
        payload = {"files": [{"name": p.name, "file_content": b64}]}
        r = self._post(self.cfg.mineru_upload_path, payload)
        urls = (r.get("data") or {}).get("file_urls") or []
        if not urls:
            raise PaperError("PAPER-0010", stage="S1",
                             detail={"reason": "上传响应无 file_urls", "resp": _brief(r)})
        return str(urls[0])

    def submit(self, file_url: str) -> str:
        """[全局] 提交解析任务 → task_id"""
        r = self._post(self.cfg.mineru_submit_path, {
            "url": file_url, "model_version": self.cfg.mineru_model_version})
        task_id = (r.get("data") or {}).get("task_id")
        if not task_id:
            raise PaperError("PAPER-0010", stage="S1",
                             detail={"reason": "提交响应无 task_id", "resp": _brief(r)})
        return str(task_id)

    def poll(self, task_id: str, pages: int = 1,
             on_state=None) -> dict:
        """[全局] 轮询任务直到完成；返回 data（含 full_zip_url）

        参数:
            task_id: 任务 ID
            pages: 页数（用于估算等待上限）
            on_state: 状态回调 on_state(state)（供进度/审计）
        报错:
            PAPER-0013（失败/超时）
        """
        poll_url = self.cfg.mineru_poll_path.replace("{task_id}", task_id)
        deadline = time.time() + max(MIN_WAIT_SEC, pages * WAIT_PER_PAGE_SEC)
        while time.time() < deadline:
            try:
                resp = requests.get("%s%s" % (self.base, poll_url),
                                    headers=self.headers, timeout=30)
                body = self._check_response(resp)
            except requests.RequestException as exc:
                raise PaperError("PAPER-0010", stage="S1",
                                 detail={"exc": str(exc)[:300]}) from exc
            data = body.get("data") or {}
            state = data.get("state")
            if on_state:
                on_state(state)
            if state == "done":
                return data
            if state in ("failed", "error", "canceled"):
                raise PaperError("PAPER-0013", stage="S1", detail={
                    "state": state, "err_msg": str(data.get("err_msg", ""))[:300]})
            time.sleep(POLL_INTERVAL_SEC)
        raise PaperError("PAPER-0013", stage="S1",
                         detail={"reason": "轮询超时", "task_id": task_id})

    # ---------- 结果解析 ----------

    def download_zip(self, zip_url: str, dest_dir: str | Path) -> Path:
        """[全局] 下载结果 zip → 返回本地路径"""
        try:
            resp = requests.get(zip_url, timeout=180)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise PaperError("PAPER-0013", stage="S1",
                             detail={"exc": str(exc)[:300]}) from exc
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        zip_path = dest / ("result_%s.zip" % time.strftime("%H%M%S"))
        zip_path.write_bytes(resp.content)
        return zip_path

    def parse_zip(self, zip_path: str | Path) -> ParserBlocks:
        """[全局] zip → ParserBlocks（content_list.json → 块；full.md 解压到本地并写入 raw_path）

        修复：原 raw_path 直接指向 zip 内部文件名（如 "full.md"），未解压到本地，
        导致 StageParse 的 exists() 检查失败 → 误报 PAPER-0014，full.md LaTeX 融合从未生效。
        R02：content_list.json 同样解压到本地并写入 raw_content_list（S1.5 布局骨架消费）。
        """
        zip_path = Path(zip_path)
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
            cl_name = next((n for n in names if n.endswith("content_list.json")), None)
            md_name = next((n for n in names if n.endswith(".md")), None)
            if not cl_name:
                raise PaperError("PAPER-0013", stage="S1", detail={
                    "reason": "zip 缺少 content_list.json", "files": names[:10]})
            items = json.loads(z.read(cl_name).decode("utf-8", errors="replace"))
            raw_path = None
            if md_name:
                raw = zip_path.parent / Path(md_name).name   # 解压到 zip 同目录（取 basename 防路径穿越）
                raw.write_bytes(z.read(md_name))
                raw_path = str(raw)
            raw_cl = zip_path.parent / "content_list.json"
            raw_cl.write_bytes(z.read(cl_name))
        blocks, max_page = _items_to_blocks(items)
        return ParserBlocks(source="mineru", pages=max_page, blocks=blocks,
                            raw_path=raw_path, raw_content_list=str(raw_cl),
                            warnings=[])

    # ---------- 全流程 ----------

    def extract(self, pdf_path: str | Path,
                quota: QuotaTracker | None = None,
                on_state=None, workdir: str | Path = "work/mineru_cache") -> ParserBlocks:
        """[全局] 全流程（本地文件）：配额检查 → 大小检查 → 上传 → 提交 → 轮询 → 下载 → 解析

        参数:
            pdf_path: 本地 PDF 路径
            quota: 配额跟踪器（None=不检查）
            on_state: 轮询状态回调
            workdir: zip/中间文件目录（默认 work/mineru_cache）
        返回:
            ParserBlocks（source="mineru"）
        报错:
            PAPER-0015: 文件超过上传上限（调度层应降级 PyMuPDF 并记 PAPER-0104）
        """
        import pymupdf
        try:
            with pymupdf.open(str(pdf_path)) as doc:
                pages = doc.page_count
        except Exception:
            pages = 1
        if quota:
            quota.check(pages)
        size = Path(pdf_path).stat().st_size
        if size > self.cfg.mineru_max_upload_bytes:
            raise PaperError("PAPER-0015", stage="S1", path=str(pdf_path), detail={
                "size": size, "limit": self.cfg.mineru_max_upload_bytes,
                "hint": "降级本地解析，或提供公网 URL 使用 extract_from_url"})

        file_url = self.upload_file(pdf_path)
        task_id = self.submit(file_url)
        data = self.poll(task_id, pages=pages, on_state=on_state)
        zip_url = data.get("full_zip_url")
        if not zip_url:
            raise PaperError("PAPER-0013", stage="S1",
                             detail={"reason": "任务完成但无 full_zip_url"})
        zip_path = self.download_zip(str(zip_url), Path(workdir) / task_id)
        self._backup_v4_zip(pdf_path, task_id, zip_path)   # 原始结果备份（供排查问题归属）
        result = self.parse_zip(zip_path)
        if quota:
            quota.record(pages)
        return result

    def _backup_v4_zip(self, pdf_path: str | Path, task_id: str,
                       zip_path: str | Path) -> Path:
        """[局部] v4 通道原始识别结果备份：zip + content_list.json + full.md 复制到
        work/mineru_backup/<时间戳-v4>/（用户要求：区分程序问题还是 MinerU 识别问题）"""
        dest = _backup_dir("v4")
        shutil.copy2(zip_path, dest / ("result_%s.zip" % task_id))
        try:
            with zipfile.ZipFile(zip_path) as z:
                for name in z.namelist():
                    if name.endswith("content_list.json") or name.endswith(".md"):
                        (dest / name.replace("/", "_")).write_bytes(z.read(name))
        except Exception:
            pass
        (dest / "manifest.json").write_text(
            json.dumps({"channel": "v4", "task_id": task_id,
                        "pdf": str(Path(pdf_path).name),
                        "time": time.strftime("%Y-%m-%d %H:%M:%S")},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return dest

    # ---------- v1 签名上传通道（Mineru使用说明.md 实测验证可用） ----------

    def extract_v1(self, pdf_path: str | Path,
                   quota: QuotaTracker | None = None,
                   on_state=None, out_md: str | Path | None = None) -> str:
        """[全局] v1 签名上传全流程：POST 申请签名 URL → PUT 文件二进制 → 轮询 → 下载 Markdown

        该通道为"本地文件 → MinerU 解析（已排阅读顺序的 Markdown）"，无 Authorization、
        上限 200MB/200 页（实测：tiny2.pdf 36 秒 done）。
        返回的 Markdown 作为**第二校准源**（与校准 MD 同路径逻辑）参与拼接校准。

        参数:
            pdf_path: 本地 PDF 路径
            quota: 配额跟踪器（None=不检查）
            on_state: 轮询状态回调 on_state(state)
            out_md: Markdown 落盘路径（None=不落盘）
        返回:
            MinerU 解析出的 Markdown 文本
        报错:
            PAPER-0010/0012/0013（网络/认证/任务失败）
        """
        import pymupdf
        try:
            with pymupdf.open(str(pdf_path)) as doc:
                pages = doc.page_count
        except Exception:
            pages = 1
        if quota:
            quota.check(pages)

        try:
            resp = requests.post(V1_PARSE_URL, headers={"Content-Type": "application/json"},
                                 json={"file_name": Path(pdf_path).name},
                                 timeout=60)
        except requests.RequestException as exc:
            raise PaperError("PAPER-0010", stage="S1",
                             detail={"exc": str(exc)[:300], "hint": "v1 签名上传"}) from exc
        if resp.status_code in (401, 403):
            raise PaperError("PAPER-0012", stage="S1", detail={"http": resp.status_code})
        if resp.status_code >= 400:
            raise PaperError("PAPER-0010", stage="S1",
                             detail={"http": resp.status_code, "body": resp.text[:300]})
        body = resp.json()
        data = body.get("data") or {}
        task_id = data.get("task_id")
        file_url = data.get("file_url")
        if not task_id or not file_url:
            raise PaperError("PAPER-0010", stage="S1",
                             detail={"reason": "v1 申请响应缺 task_id/file_url", "resp": body})

        try:
            with open(pdf_path, "rb") as f:
                up = requests.put(file_url, data=f, timeout=180)
            up.raise_for_status()
        except requests.RequestException as exc:
            raise PaperError("PAPER-0010", stage="S1",
                             detail={"exc": str(exc)[:300], "hint": "PUT 签名 URL"}) from exc

        # 轮询 v1 任务
        deadline = time.time() + max(MIN_WAIT_SEC, pages * WAIT_PER_PAGE_SEC)
        markdown_url = None
        while time.time() < deadline:
            try:
                r = requests.get(V1_TASK_URL.format(task_id=task_id),
                                 headers={"Content-Type": "application/json"}, timeout=30)
                r.raise_for_status()
                rbody = r.json()
            except requests.RequestException as exc:
                raise PaperError("PAPER-0010", stage="S1",
                                 detail={"exc": str(exc)[:300]}) from exc
            d = rbody.get("data") or {}
            state = d.get("state")
            if on_state:
                on_state(state)
            if state == "done":
                markdown_url = d.get("markdown_url")
                break
            if state in ("failed", "error", "canceled"):
                raise PaperError("PAPER-0013", stage="S1", detail={
                    "state": state, "err_msg": str(d.get("err_msg", ""))[:300]})
            time.sleep(POLL_INTERVAL_SEC)
        if not markdown_url:
            raise PaperError("PAPER-0013", stage="S1",
                             detail={"reason": "v1 轮询超时", "task_id": task_id})

        try:
            mr = requests.get(markdown_url, timeout=180)
            mr.raise_for_status()
            text = mr.text
        except requests.RequestException as exc:
            raise PaperError("PAPER-0013", stage="S1",
                             detail={"exc": str(exc)[:300], "hint": "下载 markdown_url"}) from exc

        if quota:
            quota.record(pages)
        if out_md:
            p = Path(out_md)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        self._backup_v1_md(pdf_path, task_id, text)        # 原始识别结果备份（独立目录）
        return text

    def _backup_v1_md(self, pdf_path: str | Path, task_id: str,
                      markdown: str) -> Path:
        """[局部] v1 通道原始识别结果备份：markdown + manifest 到
        work/mineru_backup/<时间戳-v1>/（用户要求：区分程序问题还是 MinerU 识别问题）"""
        dest = _backup_dir("v1")
        (dest / "mineru_v1_raw.md").write_text(markdown, encoding="utf-8")
        (dest / "manifest.json").write_text(
            json.dumps({"channel": "v1", "task_id": task_id,
                        "pdf": str(Path(pdf_path).name),
                        "bytes": len(markdown),
                        "time": time.strftime("%Y-%m-%d %H:%M:%S")},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return dest

    def extract_from_url(self, file_url: str, pages: int = 1,
                         quota: QuotaTracker | None = None,
                         on_state=None, workdir: str | Path = "work/mineru_cache") -> ParserBlocks:
        """[全局] 全流程（公网 URL 通道，无上传大小限制；官方示例方式）

        参数:
            file_url: 公网可访问的 PDF URL
            pages: 预估页数（配额与等待时长）
            quota: 配额跟踪器（None=不检查）
            on_state: 轮询状态回调
            workdir: zip/中间文件目录
        返回:
            ParserBlocks（source="mineru"）
        """
        if quota:
            quota.check(pages)
        task_id = self.submit(file_url)
        data = self.poll(task_id, pages=pages, on_state=on_state)
        zip_url = data.get("full_zip_url")
        if not zip_url:
            raise PaperError("PAPER-0013", stage="S1",
                             detail={"reason": "任务完成但无 full_zip_url"})
        zip_path = self.download_zip(str(zip_url), Path(workdir) / task_id)
        result = self.parse_zip(zip_path)
        if quota:
            quota.record(pages)
        return result

    # ---------- v4 批量文件解析通道（临时上传链接，Mineru使用说明-批量文件解析.md） ----------

    def extract_v4_batch(self, pdf_path: str | Path,
                         quota: QuotaTracker | None = None,
                         on_state=None, workdir: str | Path = "work/mineru_cache") -> ParserBlocks:
        """[全局] v4 批量文件解析全流程（本地文件 → 临时上传链接 → 上传 → 自动提交 → 轮询 → 下载 zip → 解析）

        与 upload_file()（错误的 file_content=base64 直传，750KB 上限致 6.7MB 失败）不同，
        本通道按官方文档正确实现：POST /file-urls/batch 申请上传链接（24h 有效）→
        PUT 流式上传（body 为原始字节，不设 Content-Type，勿 base64）→ 系统自动提交解析任务
        （无须再调 /extract/task）→ 轮询 /extract-results/batch/{batch_id} → full_zip_url。

        参数:
            pdf_path: 本地 PDF 路径
            quota: 配额跟踪器（None=不检查）
            on_state: 轮询状态回调 on_state(state)
            workdir: 本地缓存目录（zip/中间文件）
        返回:
            ParserBlocks（source="mineru"）
        报错:
            PAPER-0012: HTTP 401/403（token 错误/过期）
            PAPER-0010: HTTP>=400 或业务码非 0（含 -600xx 错误码原文，见 detail.msg）
            PAPER-0013: 任务失败 / 轮询超时
        """
        import pymupdf
        try:
            with pymupdf.open(str(pdf_path)) as doc:
                pages = doc.page_count
        except Exception:
            pages = 1
        if quota:
            quota.check(pages)

        fname = Path(pdf_path).name
        data_id = ("pr-%.9f" % time.time()).replace(".", "-")
        file_size = Path(pdf_path).stat().st_size

        # (1) 申请上传链接
        # 2026-09-12 批2：参数**显式下发**（此前 language 硬编码 "en"、enable_table/is_ocr 缺失
        # ⇒ 中文文献走英文 OCR、扫描件近乎无输出）。判据集中在 core/parse_params.py。
        from paperparse.core.parse_params import mineru_payload_params, resolve_mineru_params
        _params = resolve_mineru_params(self.cfg, pdf_path)
        self.last_params = _params            # 排障/UI：本次实际生效的参数与探测结果
        claim_payload = {
            "files": [{"name": fname, "data_id": data_id}],
            **mineru_payload_params(_params),
        }
        try:
            resp = requests.post("%s%s" % (self.base, self.cfg.mineru_upload_path),
                                 headers=self.headers, json=claim_payload,
                                 timeout=self.cfg.mineru_timeout_sec)
        except requests.RequestException as exc:
            raise PaperError("PAPER-0010", stage="S1",
                             detail={"exc": str(exc)[:300], "hint": "POST %s" % self.cfg.mineru_upload_path}) from exc
        if resp.status_code in (401, 403):
            raise PaperError("PAPER-0012", stage="S1", detail={"http": resp.status_code})
        if resp.status_code >= 400:
            raise PaperError("PAPER-0010", stage="S1",
                             detail={"http": resp.status_code, "body": resp.text[:300]})
        body = resp.json()
        if body.get("code") not in (0, None):
            raise PaperError("PAPER-0010", stage="S1", detail=_brief(body))
        cdata = body.get("data") or {}
        batch_id = cdata.get("batch_id")
        urls = cdata.get("file_urls") or []
        if not batch_id or not urls:
            raise PaperError("PAPER-0010", stage="S1",
                             detail={"reason": "申请响应缺 batch_id/file_urls", "resp": _brief(body)})
        file_url = str(urls[0])

        # (2) 备份上传链接到本地
        claim_time = time.strftime("%Y-%m-%d %H:%M:%S")
        backup_dir = _backup_dir("v4batch")
        backup_dir.mkdir(parents=True, exist_ok=True)
        (backup_dir / "upload_link.json").write_text(
            json.dumps({
                "batch_id": batch_id,
                "file_url": file_url,
                "data_id": data_id,
                "apply_time": claim_time,
                "expire_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 86400)),
                "valid_hours": 24,
                "note": "上传链接 24 小时内有效，请在有效期内完成上传",
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        (backup_dir / "manifest.json").write_text(
            json.dumps({
                "channel": "v4batch", "batch_id": batch_id,
                "pdf": fname, "size_bytes": file_size, "pages": pages,
                "model_version": self.cfg.mineru_model_version,
                "apply_time": claim_time,
            }, ensure_ascii=False, indent=2), encoding="utf-8")

        # (3) PUT 流式上传（原始字节，勿设 Content-Type，勿 base64）
        try:
            with open(pdf_path, "rb") as f:
                up = requests.put(file_url, data=f, timeout=180)
        except requests.RequestException as exc:
            raise PaperError("PAPER-0010", stage="S1",
                             detail={"exc": str(exc)[:300], "hint": "PUT 上传链接"}) from exc
        if up.status_code == 413:
            raise PaperError("PAPER-0015", stage="S1", detail={
                "http": 413, "hint": "上传请求体超限（临时链接通道理论 200MB，实际受限）"})
        if up.status_code in (401, 403):
            raise PaperError("PAPER-0012", stage="S1", detail={"http": up.status_code})
        if up.status_code >= 400:
            raise PaperError("PAPER-0010", stage="S1",
                             detail={"http": up.status_code, "body": up.text[:300], "hint": "PUT 上传失败"})
        # 上传后系统自动提交解析任务（勿再调 /extract/task）

        # (4) 轮询批量结果
        batch_results_url = self.cfg.mineru_batch_results_path.replace("{batch_id}", batch_id)
        deadline = time.time() + max(MIN_WAIT_SEC, pages * WAIT_PER_PAGE_SEC)
        zip_url = None
        err_msg = ""
        while time.time() < deadline:
            try:
                r = requests.get("%s%s" % (self.base, batch_results_url),
                                 headers=self.headers, timeout=30)
                rb = self._check_response(r)
            except requests.RequestException as exc:
                raise PaperError("PAPER-0010", stage="S1",
                                 detail={"exc": str(exc)[:300], "hint": "轮询 %s" % self.cfg.mineru_batch_results_path}) from exc
            results = (rb.get("data") or {}).get("extract_result") or []
            st = None
            if results:
                st = results[0].get("state")
                zip_url = results[0].get("full_zip_url")
                err_msg = str(results[0].get("err_msg", ""))[:300]
            if on_state:
                on_state(st)
            if st == "done":
                break
            if st in ("failed", "error", "canceled"):
                raise PaperError("PAPER-0013", stage="S1",
                                 detail={"state": st, "err_msg": err_msg or "任务失败"})
            time.sleep(POLL_INTERVAL_SEC)
        if not zip_url:
            raise PaperError("PAPER-0013", stage="S1",
                             detail={"reason": "批量轮询超时", "batch_id": batch_id})

        # (5) 下载 zip + 解析
        zip_path = self.download_zip(str(zip_url), Path(workdir) / batch_id)
        self._backup_v4batch(pdf_path, batch_id, zip_path, backup_dir)   # 备份原始结果
        result = self.parse_zip(zip_path)
        if quota:
            quota.record(pages)
        return result

    def _backup_v4batch(self, pdf_path: str | Path, batch_id: str,
                        zip_path: str | Path, backup_dir: str | Path | None = None) -> Path:
        """[局部] v4 批量通道原始结果备份：zip + content_list.json + full.md 复制到
        work/mineru_backup/<时间戳-v4batch>/（区分程序问题还是 MinerU 识别问题）"""
        dest = Path(backup_dir) if backup_dir else _backup_dir("v4batch")
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(zip_path, dest / ("result_%s.zip" % batch_id))
        try:
            with zipfile.ZipFile(zip_path) as z:
                for name in z.namelist():
                    if name.endswith("content_list.json") or name.endswith(".md"):
                        (dest / name.replace("/", "_")).write_bytes(z.read(name))
        except Exception:
            pass
        (dest / "manifest.json").write_text(
            json.dumps({"channel": "v4batch", "batch_id": batch_id,
                        "pdf": str(Path(pdf_path).name),
                        "time": time.strftime("%Y-%m-%d %H:%M:%S")},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return dest
