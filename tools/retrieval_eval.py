# -*- coding: utf-8 -*-
"""检索质量离线评测：三档消融（notes / +vector / +rerank）+ 阈值回归门。

为什么需要（2026-09-21）：融合口径（RRF）、重排开关、分块与注入口径同时在改，
没有离线指标就只能靠"感觉"。本脚本把召回质量变成可回归的数字：

    recall@k —— 前 k 条里覆盖了多少相关文献
    MRR      —— 第一个相关结果排名的倒数（首位命中质量）
    nDCG@10  —— 排序质量（二元增益）

用法：
    # 1) 从当前知识库生成「自检索」评测集（查询摘自已编译笔记原文 → 标签 = 该篇）
    python tools/retrieval_eval.py --bootstrap

    # 2) 人工核对标签 / 补充真实问句后跑消融
    python tools/retrieval_eval.py --verbose

    # 3) 固化为回归门（阈值不过 → 退出码 1）
    python tools/retrieval_eval.py --golden tools/retrieval_eval/queries.jsonl

⚠️ `--bootstrap` 的标签是**合成**的（查询直接摘自笔记正文），会**偏向向量路**
（文本近似 → 向量天然占优），只能当冒烟基线；真实结论要人工标注集：
`{"query": "...", "relevant": ["10.1016/j.xxx"], "kind": "human"}`。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "paperkb"))

from paperkb import api  # noqa: E402
from paperkb.config import KbSettings, Roots  # noqa: E402
from paperkb.textseg import boundary_trim, split_chunks, strip_frontmatter  # noqa: E402
from paperkb.vector import KbVectorIndex, _key_aliases  # noqa: E402

DEFAULT_GOLDEN = ROOT / "tools" / "retrieval_eval" / "queries.jsonl"
BOOTSTRAP_OUT = ROOT / "work" / "retrieval_eval" / "queries.jsonl"
ARMS = ("notes", "vector", "rerank")
_DOI_RE = re.compile(r"^doi:\s*(.+)$", re.M)


# ---------------------------------------------------------------- 指标
# 口径说明：一条召回结果可能同时带 DOI / RID / 目录名（`_key_aliases` 家族），
# 因此**必须先归并到"相关文献"这一层再算指标**——否则同一篇被两种键写法召回会重复计分
# （实测 nDCG 算出 1.22 > 1）。
def rank_hits(ranked: list[set[str]], groups: list[set[str]]) -> list[int]:
    """每篇相关文献**首次**被命中的排名（未命中则不出现）。

    一条召回只归一篇（按 groups 顺序取第一个命中）——一条结果就是一篇文献。
    """
    seen: set[int] = set()
    hits: list[int] = []
    for rank, aliases in enumerate(ranked, 1):
        for gi, g in enumerate(groups):
            if gi not in seen and (aliases & g):
                seen.add(gi)
                hits.append(rank)
                break
    return hits


def recall_at_k(hits: list[int], n_rel: int, k: int) -> float:
    if not n_rel:
        return 0.0
    return sum(1 for r in hits if r <= k) / n_rel


def mrr(hits: list[int]) -> float:
    return 1.0 / min(hits) if hits else 0.0


def ndcg_at_k(hits: list[int], n_rel: int, k: int = 10) -> float:
    dcg = sum(1.0 / math.log2(r + 1) for r in hits if r <= k)
    ideal = sum(1.0 / math.log2(j + 1) for j in range(1, min(n_rel, k) + 1))
    return dcg / ideal if ideal else 0.0


def _item_aliases(item: dict) -> set[str]:
    """一条召回结果的所有键写法（DOI / RID / 目录名）。"""
    out: set[str] = set()
    for k in ((item.get("doi") or "").strip(), (item.get("rid") or "").strip()):
        if k:
            out |= _key_aliases(k)
    return out


def _label_aliases(doi: str) -> set[str]:
    return _key_aliases((doi or "").strip()) if doi else set()


# ---------------------------------------------------------------- 评测
def run_arm(name: str, queries: list[dict], k: int, store) -> list[list[dict]]:
    """按档位跑一遍召回：notes=只留 FTS 路；vector=多路但不重排；rerank=多路+精排。"""
    with ExitStack() as st:
        if name == "notes":
            st.enter_context(patch("paperkb.retrieve._vector_recall",
                                   lambda q, n: []))
        do_rerank = name == "rerank"
        return [api.recall(q["query"], top_k=k, rerank=do_rerank) for q in queries]


def score_arm(items_per_query: list[list[dict]], queries: list[dict],
              k: int) -> dict:
    rec, rrs, ndcgs, misses = [], [], [], []
    for items, q in zip(items_per_query, queries):
        groups = [_label_aliases(d) for d in q["relevant"] if d]
        ranked = [_item_aliases(it) for it in items]
        hits = rank_hits(ranked, groups)
        r = recall_at_k(hits, len(groups), k)
        rec.append(r)
        rrs.append(mrr(hits))
        ndcgs.append(ndcg_at_k(hits, len(groups)))
        if r < 1.0:
            top3 = [it.get("doi") or it.get("rid") or "?" for it in items[:3]]
            misses.append((q["query"], q["relevant"], top3))
    n = max(1, len(queries))
    return {"n": len(queries), "recall": sum(rec) / n, "mrr": sum(rrs) / n,
            "ndcg": sum(ndcgs) / n, "misses": misses}


# ---------------------------------------------------------------- 评测集
def load_golden(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(
            f"评测集不存在: {path}\n"
            f"先生成冒烟基线: python tools/retrieval_eval.py --bootstrap")
    out: list[dict] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except ValueError as e:
            raise SystemExit(f"{path}:{i} 不是合法 JSON: {e}") from e
        if row.get("query") and row.get("relevant"):
            out.append(row)
    if not out:
        raise SystemExit(f"{path} 里没有可用条目（需 query + relevant）")
    return out


def bootstrap(store, out: Path) -> int:
    """从已编译笔记生成「自检索」评测集：查询摘原文，标签 = 该篇本身。"""
    rows: list[dict] = []
    kb_dir = store.roots.kb_dir
    for note_path in sorted(kb_dir.rglob("_note.md")):
        if ".trash" in note_path.parts:
            continue
        text = note_path.read_text(encoding="utf-8", errors="replace")
        m = _DOI_RE.search(text)
        doi = (m.group(1).strip() if m else "") or note_path.parent.name
        body = strip_frontmatter(text).strip()
        title = ""
        for line in body.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        if title:
            rows.append({"query": title, "relevant": [doi], "kind": "title"})
        chunks = split_chunks(body, max_chars=600)
        # 取中段偏后的块（首块多是标题/一句话贡献，区分度低）
        picks = chunks[len(chunks) // 2: len(chunks) // 2 + 2] or chunks[:1]
        for ch in picks:
            body_line = "\n".join(ln for ln in ch["text"].splitlines()
                                 if not ln.lstrip().startswith("#")).strip()
            q = boundary_trim(body_line, 60)
            if len(q) >= 12:
                rows.append({"query": q, "relevant": [doi], "kind": "body"})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                   encoding="utf-8")
    print(f"已生成合成评测集: {out}（{len(rows)} 条 / {len({r['relevant'][0] for r in rows})} 篇）")
    print("⚠️ 查询摘自已编译笔记原文 → 偏向向量路，属冒烟基线；请人工核对/补充真实问句后再做结论。")
    return 0


# ---------------------------------------------------------------- 主流程
def main() -> int:
    # Windows 控制台默认 GBK，打印 ⚠️/✓ 会 UnicodeEncodeError → 退出码恒为 1（信号失效）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 老解释器/重定向场景
        pass
    ap = argparse.ArgumentParser(description="检索质量三档消融评测")
    ap.add_argument("--golden", default="", help="评测集 jsonl（缺省：tools/… 否则 work/…）")
    ap.add_argument("--bootstrap", action="store_true", help="从当前 KB 生成合成评测集")
    ap.add_argument("--out", default=str(BOOTSTRAP_OUT), help="--bootstrap 的输出路径")
    ap.add_argument("--k", type=int, default=10, help="评测的 top-k（默认 10）")
    ap.add_argument("--limit", type=int, default=0, help="只评前 N 条（0=全部）")
    ap.add_argument("--min-recall", type=float, default=0.8, help="全档 recall@k 下限")
    ap.add_argument("--min-mrr", type=float, default=0.6, help="全档 MRR 下限")
    ap.add_argument("--tolerance", type=float, default=0.05,
                    help="全档相对 notes 档允许的最大退步（默认 0.05）")
    ap.add_argument("--verbose", action="store_true", help="逐条打印未完全命中的 query")
    args = ap.parse_args()

    roots = Roots(data_dir=ROOT / "data", library_dir=ROOT / "library",
                  kb_dir=ROOT / "knowledge_base").ensure()
    api.init_kb(roots, KbSettings(vector_impl="kb", rerank_enabled=True))
    store = api._need_store()  # noqa: SLF001

    if args.bootstrap:
        return bootstrap(store, Path(args.out))

    golden = Path(args.golden) if args.golden else (
        DEFAULT_GOLDEN if DEFAULT_GOLDEN.exists() else BOOTSTRAP_OUT)
    synthetic = not args.golden and not DEFAULT_GOLDEN.exists()
    queries = load_golden(golden)
    if args.limit > 0:
        queries = queries[:args.limit]

    key = _api_key()
    idx = KbVectorIndex(roots, api_key=key) if key else None
    if idx is None:
        print("⚠️ 未配置 SILICONFLOW_API_KEY：+vector/+rerank 档会退化成 notes 档")

    def _cached_vector_search(query, top_k=20, exclude_doi="", with_snippet=True):
        return idx.search(query, top_k=top_k, exclude_doi=exclude_doi,
                          with_snippet=with_snippet, store=store)

    print(f"评测集: {golden}（{len(queries)} 条）  k={args.k}  向量索引: "
          f"{'有' if idx and idx.size else '无'}")
    rerank_ready = bool(_rerank_key())
    if not rerank_ready:
        print("⚠️ 未配置 SILICONFLOW_RERANK_API_KEY：+rerank 档不会真的重排")
    if synthetic:
        print("⚠️ 用的是 --bootstrap 合成标签（偏向向量路）→ 只当冒烟基线，"
              "结论请用人工标注集 --golden")
    print()

    results: dict[str, dict] = {}
    ctx = patch("paperkb.api.kb_vector_search", _cached_vector_search) \
        if idx is not None else ExitStack()
    with ctx:
        for arm in ARMS:
            items = run_arm(arm, queries, args.k, store)
            results[arm] = score_arm(items, queries, args.k)

    print(f"{'档位':<10}{'recall@'+str(args.k):>12}{'MRR':>10}{'nDCG@10':>10}")
    for arm in ARMS:
        r = results[arm]
        print(f"{arm:<10}{r['recall']:>12.3f}{r['mrr']:>10.3f}{r['ndcg']:>10.3f}")

    if args.verbose:
        for arm in ARMS:
            ms = results[arm]["misses"]
            if not ms:
                continue
            print(f"\n[{arm}] 未完全命中 {len(ms)}/{results[arm]['n']} 条：")
            for q, want, top3 in ms[:20]:
                print(f"  · {q[:56]!r}\n    期望 {want} → 实得前三 {top3}")

    full = results["rerank" if rerank_ready else "vector"]
    base = results["notes"]
    ok = True
    if full["recall"] < args.min_recall:
        print(f"\n✗ recall@{args.k} {full['recall']:.3f} < 下限 {args.min_recall}")
        ok = False
    if full["mrr"] < args.min_mrr:
        print(f"\n✗ MRR {full['mrr']:.3f} < 下限 {args.min_mrr}")
        ok = False
    if full["mrr"] + args.tolerance < base["mrr"]:
        print(f"\n✗ 多路融合+精排反而退步：MRR {full['mrr']:.3f} < notes {base['mrr']:.3f}"
              f" - {args.tolerance}")
        ok = False
    print("\n" + ("✓ 通过" if ok else "✗ 未通过"))
    return 0 if ok else 1


def _api_key() -> str:
    import os

    return os.environ.get("SILICONFLOW_API_KEY", "").strip()


def _rerank_key() -> str:
    import os

    return (os.environ.get("SILICONFLOW_RERANK_API_KEY", "").strip()
            or _api_key())


if __name__ == "__main__":
    raise SystemExit(main())
