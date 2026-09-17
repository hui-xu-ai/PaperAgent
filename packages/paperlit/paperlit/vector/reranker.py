# -*- coding: utf-8 -*-
"""SiliconFlow Reranker 客户端（bge-reranker-v2-m3）。

API 格式：POST /v1/rerank
输入 query + documents 列表，返回每条的相关性分数。
用于漏斗管线 L2 层：对 L1 粗筛候选精排。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_BASE = "https://api.siliconflow.cn/v1"
_TIMEOUT = 60
_BATCH_SIZE = 32


def _post_json(url: str, body: dict, api_key: str,
               timeout: int = _TIMEOUT) -> dict | None:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            logger.warning("SiliconFlow 速率限制（reranker）")
        else:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            logger.warning("SiliconFlow reranker %s → HTTP %s: %s",
                           url, e.code, body_text)
    except Exception as e:
        logger.warning("SiliconFlow reranker 请求失败: %s", e)
    return None


def rerank(query: str, documents: list[str],
           model: str = "BAAI/bge-reranker-v2-m3",
           api_key: str = "",
           top_n: int | None = None,
           batch_size: int = _BATCH_SIZE,
           rate_limit: float = 5.0) -> list[tuple[int, float]]:
    """对 (query, documents) 打分，返回按分数降序的 (原始索引, 分数) 列表。

    Args:
        query: 检索 query
        documents: 候选文档列表
        model: reranker 模型
        api_key: SiliconFlow API key
        top_n: 只返回前 N 条（None = 全部）
        batch_size: 每批文档数（API 单次上限）
        rate_limit: 每秒最大请求数

    Returns:
        [(原始索引, 相关性分数), ...] 按分数降序
    """
    if not api_key:
        raise ValueError("SiliconFlow API key 未配置")
    if not query or not documents:
        return []

    scored: list[tuple[int, float]] = []

    valid_items = [(i, d) for i, d in enumerate(documents) if d and d.strip()]
    if not valid_items:
        return []

    for batch_start in range(0, len(valid_items), batch_size):
        batch = valid_items[batch_start:batch_start + batch_size]
        batch_texts = [d for _, d in batch]
        batch_indices = [i for i, _ in batch]

        body = {
            "model": model,
            "query": query,
            "documents": batch_texts,
        }
        if top_n is not None:
            body["top_n"] = min(top_n, len(batch_texts))

        result = _post_json(f"{_BASE}/rerank", body, api_key)

        if result and "results" in result:
            for item in result["results"]:
                idx_in_batch = item.get("index", 0)
                score = item.get("relevance_score", 0.0)
                if idx_in_batch < len(batch_indices):
                    orig_idx = batch_indices[idx_in_batch]
                    scored.append((orig_idx, score))

        if batch_start + batch_size < len(valid_items):
            time.sleep(1.0 / rate_limit)

    scored.sort(key=lambda x: x[1], reverse=True)

    if top_n is not None:
        scored = scored[:top_n]

    return scored
