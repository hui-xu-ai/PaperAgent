# -*- coding: utf-8 -*-
"""扩容 P0 规模验证：N 篇模拟，量**写放大 / 检索延迟 / 压实**。

判据（可当回归门）：
- 单篇写入字节 < 1MB，且**与总篇数无关**（追加写，不整份重写）；
- 单篇写入耗时在首尾 10% 无系统抬升（O(N²) 会表现为后段显著变慢）；
- 向量检索 p95 < 50ms（1 万篇 ≈10 万块，numpy 暴力；ANN 是 P1 的活）。

用法：
    python tools/index_scale_sim.py                      # 1 万篇
    python tools/index_scale_sim.py --papers 2000 --chunks 10
"""
from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "paperkb"))

from paperkb.config import Roots          # noqa: E402
from paperkb.index_store import reset_index_store   # noqa: E402
from paperkb.vector import (EMBEDDING_DIM, get_kb_vector_index,   # noqa: E402
                            reset_kb_vector_index)

SECTION_CHARS = 880


def _fake_vec(text: str) -> list[float]:
    rng = np.random.RandomState(abs(hash(text)) % (2**31))
    v = rng.randn(EMBEDDING_DIM).astype(np.float32)
    n = float(np.linalg.norm(v))
    return (v / n if n > 0 else v).tolist()


def _encode_texts(texts, **kw):
    return [_fake_vec(t) for t in texts]


def _encode_query(q, **kw):
    return _fake_vec(q)


def _note_body(doi: str, chunks: int) -> str:
    """生成含 `chunks` 个小节的 _note.md 正文（每节 ≈880 字 → 切块数≈chunks）。"""
    parts = [f"---\ndoi: {doi}\n---\n# {doi} \u6807\u9898\n"]
    for i in range(chunks):
        parts.append(f"\n## \u5c0f\u8282 {i}\n")
        parts.append(("\u8fd9\u662f\u7b2c%d\u8282\u7684\u5185\u5bb9\u3002" % i) * 55 + "\n")
    return "".join(parts)


def _seg_bytes(idx) -> int:
    return sum(p.stat().st_size for p in idx._store.index_dir.glob("*.f32"))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("--papers", type=int, default=10000)
    ap.add_argument("--chunks", type=int, default=10)
    ap.add_argument("--queries", type=int, default=20)
    ap.add_argument("--max-write-kb", type=float, default=1024.0,
                    help="单篇写入上限（KB），默认 1MB")
    ap.add_argument("--max-search-ms", type=float, default=50.0)
    ap.add_argument("--max-write-ratio", type=float, default=2.5,
                    help="尾 10%% / 首 10%% 单篇写入耗时上限（O(N\u00b2) 会远超）")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="kb_scale_"))
    roots = Roots(data_dir=tmp / "data", library_dir=tmp / "library",
                  kb_dir=tmp / "kb").ensure()
    reset_index_store(roots)
    reset_kb_vector_index(roots)

    print(f"\u89c4\u6a21\u6a21\u62df: papers={args.papers} chunks/papers={args.chunks} "
          f"dim={EMBEDDING_DIM}")

    with patch("paperlit.vector.encode_texts", _encode_texts), \
            patch("paperlit.vector.encode_query", _encode_query):
        idx = get_kb_vector_index(roots, api_key="sim-key")

        # ---- 写入 ----
        per_paper_ms: list[float] = []
        per_paper_bytes: list[int] = []
        t_all = time.perf_counter()
        for i in range(args.papers):
            doi = f"10.9999/sim.{i}"
            body = _note_body(doi, args.chunks)
            before = _seg_bytes(idx)
            t0 = time.perf_counter()
            idx.index_paper(doi, note_text=body, title=f"Sim Paper {i}")
            per_paper_ms.append((time.perf_counter() - t0) * 1000)
            per_paper_bytes.append(_seg_bytes(idx) - before)
        write_sec = time.perf_counter() - t_all

        rows = idx.size
        tail_n = max(1, args.papers // 10)
        head_ms = statistics.mean(per_paper_ms[:tail_n])
        tail_ms = statistics.mean(per_paper_ms[-tail_n:])
        write_kb = statistics.mean(per_paper_bytes) / 1024

        # ---- 单例（再取一次不应重新读盘）----
        t0 = time.perf_counter()
        again = get_kb_vector_index(roots, api_key="sim-key")
        reuse_ms = (time.perf_counter() - t0) * 1000

        # ---- 检索（先热身：首查含惰性 import 与查询缓存建库，非稳态）----
        for w in range(3):
            idx.search(f"warmup-{w}", top_k=10)
        lat: list[float] = []
        nn_lat: list[float] = []
        for q in range(args.queries):
            qv = _fake_vec(f"query-{q}")
            t0 = time.perf_counter()
            idx._search_by_vector(qv, 30)
            nn_lat.append((time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter()
            hits = idx.search(f"query-{q}", top_k=10)
            lat.append((time.perf_counter() - t0) * 1000)
        lat.sort()
        nn_lat.sort()
        p50 = lat[len(lat) // 2]
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
        nn_p95 = nn_lat[min(len(nn_lat) - 1, int(len(nn_lat) * 0.95))]

        # ---- 压实 ----
        t0 = time.perf_counter()
        stats = idx.compact()
        compact_ms = (time.perf_counter() - t0) * 1000
        after = idx._store.health()

    print(f"\n\u5199\u5165: {args.papers} \u7bc7 / {rows} \u5757 / {write_sec:.1f}s "
          f"({write_sec / max(1, args.papers) * 1000:.1f} ms/\u7bc7)")
    print(f"  \u5355\u7bc7\u5199\u5165: {write_kb:.1f} KB  (\u4e0a\u9650 {args.max_write_kb:.0f} KB)")
    print(f"  \u9996 10% \u5747\u503c {head_ms:.1f} ms  vs  \u5c3e 10% \u5747\u503c {tail_ms:.1f} ms"
          f"  (\u62ac\u5347 {tail_ms / max(0.01, head_ms):.2f}x)")
    print(f"  \u5355\u4f8b\u590d\u7528: {reuse_ms:.3f} ms (\u4e0d\u91cd\u8bfb\u76d8)  "
          f"same={again is idx}")
    print(f"\u68c0\u7d22: p50={p50:.1f} ms  p95={p95:.1f} ms  hits={len(hits)}  "
          f"(\u7eaf NN p95={nn_p95:.1f} ms; \u4e0a\u9650 {args.max_search_ms:.0f} ms)")
    print(f"\u538b\u5b9e: {compact_ms:.0f} ms  {stats}")
    print(f"\u5065\u5eb7: live={after['live_passages']} garbage={after['garbage_rows']} "
          f"segments={after['segments']} papers={after['papers']} "
          f"missing={after['missing_segments']} db={after['bytes'] / 1e6:.1f} MB")

    ok = True
    if write_kb > args.max_write_kb:
        print(f"FAIL: \u5355\u7bc7\u5199\u5165 {write_kb:.1f} KB > {args.max_write_kb:.0f} KB")
        ok = False
    if tail_ms / max(0.01, head_ms) > args.max_write_ratio:
        print(f"FAIL: \u5355\u7bc7\u5199\u5165\u8017\u65f6\u62ac\u5347 "
              f"{tail_ms / max(0.01, head_ms):.2f}x > {args.max_write_ratio:.2f}x"
              "（\u7591\u4f3c O(N\u00b2)\uff09")
        ok = False
    if p95 > args.max_search_ms:
        print(f"FAIL: \u68c0\u7d22 p95 {p95:.1f} ms > {args.max_search_ms:.0f} ms")
        ok = False
    if after["garbage_rows"] != 0 or after["missing_segments"]:
        print("FAIL: \u538b\u5b9e\u540e\u4ecd\u6709\u5783\u573e\u884c/\u7f3a\u5931\u6bb5")
        ok = False
    if after["live_passages"] != rows:
        print(f"FAIL: \u538b\u5b9e\u540e\u6d3b\u884c\u6570 {after['live_passages']} != {rows}")
        ok = False
    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
