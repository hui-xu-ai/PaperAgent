# -*- coding: utf-8 -*-
"""SiliconFlow Embedding 客户端（bge-m3）。

API 兼容 OpenAI 格式：POST /v1/embeddings
免费额度：bge-m3 约 500 万 token/天（够 10 万篇文献的 title+abstract）。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

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
            logger.warning("SiliconFlow 速率限制")
        else:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            logger.warning("SiliconFlow %s → HTTP %s: %s",
                           url, e.code, body_text)
    except Exception as e:
        logger.warning("SiliconFlow 请求失败: %s", e)
    return None


def encode_texts(texts: list[str], model: str = "BAAI/bge-m3",
                 api_key: str = "", batch_size: int = _BATCH_SIZE,
                 rate_limit: float = 5.0) -> list[list[float]]:
    """批量文本编码为向量。

    Args:
        texts: 文本列表
        model: 模型名称
        api_key: SiliconFlow API key
        batch_size: 每批文本数
        rate_limit: 每秒最大请求数

    Returns:
        向量列表（与输入顺序一致）
    """
    if not api_key:
        raise ValueError("SiliconFlow API key 未配置")

    if not texts:
        return []

    all_embeddings: list[list[float]] = [[] for _ in texts]
    indexed_texts = [(i, t) for i, t in enumerate(texts) if t and t.strip()]

    for batch_start in range(0, len(indexed_texts), batch_size):
        batch = indexed_texts[batch_start:batch_start + batch_size]
        batch_texts = [t for _, t in batch]
        batch_indices = [i for i, _ in batch]

        body = {"model": model, "input": batch_texts}
        result = _post_json(f"{_BASE}/embeddings", body, api_key)

        if result and "data" in result:
            for item in result["data"]:
                idx_in_batch = item.get("index", 0)
                if idx_in_batch < len(batch_indices):
                    orig_idx = batch_indices[idx_in_batch]
                    all_embeddings[orig_idx] = item.get("embedding", [])

        if batch_start + batch_size < len(indexed_texts):
            time.sleep(1.0 / rate_limit)

    return all_embeddings


def encode_query(query: str, model: str = "BAAI/bge-m3",
                 api_key: str = "") -> list[float]:
    """单条查询编码。"""
    results = encode_texts([query], model=model, api_key=api_key, batch_size=1)
    return results[0] if results else []
